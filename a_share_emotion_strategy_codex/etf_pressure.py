"""Offline ETF candle pressure and separately dated THS funding references.

No network, orders, account identification or fallback funding provider. A red
close versus yesterday is not necessarily a bearish open-to-close candle.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import statistics
from urllib.parse import parse_qsl, urlsplit

from etf_monitor import (ACCOUNT_TEXT, PRIVATE, TZ, _code, _date, _load, _market_name,
                         _now, _number, _parse_bars, _round, _stamp, _unique, _url)
from research_store import atomic_json, read_json, research_path, update_lock
from sector_fund_alternative import THS_PUBLIC_URLS

UNIVERSE_LABEL = '已覆盖ETF代理，不是全行业'
MAX_PROXIES = 1000
FLOW_URL = THS_PUBLIC_URLS[0]
FLOW_METRIC = '普通资金净额（未标主力）'
VALUES = ('open', 'close', 'high', 'low', 'change_pct', 'body_pct', 'body_range_pct',
          'close_position_pct', 'volume_ratio20')
GAPS = (
    '仅检查已覆盖ETF代理的最近导入完整日K，不是全行业或全部ETF的大阴线扫描。',
    '大阴线默认：收盘低于开盘、实体跌幅≤-2%、实体占振幅≥60%、收盘位置≤25%；阈值未作收益优势验证。',
    '收盘相对昨收的涨跌与开盘到收盘的实体变化分列；跳空低开后上涨不能只因日跌幅大就称大阴线。',
    '量比为当日量除以前20个完整交易日均量，排除当日；≥1.2只表示日线放量，不是同刻分钟量比。',
    '同花顺原站未给可核验资金日期且未标主力；普通资金净额不能解释为真实机构或游资账户净买入。',
    '资金日期未与日K匹配，不把大阴线与资金净流出合并成当日撤退信号，也不据此自动下单。',
)


def empty_flow():
    return {'status': 'unavailable', 'provider': '同花顺', 'metric': FLOW_METRIC,
            'as_of': None, 'retrieved_at': None, 'inflows': [], 'outflows': [],
            'source': FLOW_URL, 'note': '未导入同花顺两组完整公开资金证据；缺口不以其他来源补齐。',
            'coverage_kind': 'publisher_top10_each', 'universe_complete': False, 'is_main_metric': False}


def _origins(evidence):
    """One original provider/adjustment basis per code; never stitch price bars."""
    output = {}
    retrieved = _stamp(evidence.get('retrieved_at'))

    def register(code, raw, source, basis, at, issues):
        item = {'raw': raw, 'source': source, 'basis': basis, 'retrieved': at, 'issues': issues}
        old = output.get(code)
        if old is None or (not old['raw'] and raw):
            output[code] = item
        elif old['raw'] and raw:
            output[code] = dict(item, raw=None, issues=['同代码出现多份复权日线，来源冲突待核验，未拼接。'])

    for item in evidence.get('sources', []) if isinstance(evidence.get('sources'), list) else []:
        if not isinstance(item, dict):
            continue
        payload = item.get('payload')
        data = payload.get('data') if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            continue
        source = _url(item.get('source'))
        at = _stamp(item.get('retrieved_at')) or retrieved
        if item.get('format') == 'eastmoney_qfq':
            code = _code(data.get('code')) or _code(item.get('code'))
            if not code:
                continue
            parts = urlsplit(source)
            issues = []
            if (parts.hostname not in ('push2his.eastmoney.com', 'push2.eastmoney.com')
                    or [v for k, v in parse_qsl(parts.query) if k == 'fqt'] != ['1']):
                issues.append('价格来源未明确fqt=1，前复权口径缺失。')
            if _code(item.get('code')) and _code(item.get('code')) != code:
                issues.append('来源证券代码与原响应错配。')
            raw = data.get('klines')
            raw = [x.split(',') if isinstance(x, str) else x for x in raw] if isinstance(raw, list) else None
            register(code, raw, source, '东方财富日线 fqt=1 前复权；仅价格证据，不含资金数据。', at, issues)
        else:
            for symbol, data_row in data.items():
                code = _code(symbol)
                if code and isinstance(data_row, dict):
                    raw = data_row.get('qfqday')
                    issues = [] if raw else ['缺少qfqday前复权OHLC，不用未复权day替代。']
                    if not source:
                        issues.append('原始价格来源链接缺失。')
                    register(code, raw, source, '腾讯qfqday前复权OHLC；非day替代。' if raw else '', at, issues)
    return output


def _candle(item, origin, expected, now, blocked):
    code = _code(item.get('code'))
    result = {'code': code, 'name': _market_name(item.get('name', code)),
              'as_of': None, **{key: None for key in VALUES}, 'is_large_bear': None,
              'missing': [], 'source': origin.get('source', ''), 'price_basis': origin.get('basis', '')}
    missing = list(blocked) + origin.get('issues', [])
    raw = origin.get('raw')
    # A missing volume column is not a missing OHLC candle. Pad it explicitly
    # before the shared six-column parser; the volume overlay stays unknown.
    if isinstance(raw, list):
        raw = [row + [None] if isinstance(row, list) and len(row) == 5 else row for row in raw]
    parsed, issues = _parse_bars(raw, expected, now, 'CN')
    missing.extend(issues)
    at = origin.get('retrieved')
    close_time = dt.datetime.combine(expected, dt.time(15), TZ)
    if at is None:
        missing.append('原日线取得时刻缺失，未确认收盘后取得。')
    elif at > now or at < close_time:
        missing.append('日线取得时刻早于完整收盘或晚于当前时点。')
    if _date(item.get('as_of')) != expected:
        missing.append('ETF监控记录与报告日期错配。')
    if not origin.get('basis'):
        missing.append('同源复权口径缺失。')
    if parsed and _number(item.get('close')) is not None and abs(parsed[-1]['close'] - _number(item['close'])) > 1e-6:
        missing.append('OHLC原证据与ETF监控收盘冲突。')
    # The shared parser enforces future, intraday and sorted-calendar rules;
    # additional OHLC validation preserves all four fields on that same basis.
    valid_dates = {row['date'] for row in parsed}
    full = []
    for row in raw[:10000] if isinstance(raw, list) else []:
        if not isinstance(row, list) or len(row) < 6 or not isinstance(row[0], str) or row[0] not in valid_dates:
            continue
        prices = {k: _number(row[i]) for k, i in (('open', 1), ('close', 2), ('high', 3), ('low', 4))}
        if (any(v is None or v <= 0 for v in prices.values())
                or prices['low'] > min(prices['open'], prices['close'])
                or prices['high'] < max(prices['open'], prices['close'])):
            missing.append('OHLC范围或价格无效，不能确认大阴线。')
            continue
        volume = _number(row[5])
        full.append({'date': row[0], **prices, 'volume': volume if volume is not None and volume >= 0 else None})
    fatal = bool(missing)
    if not full or full[-1]['date'] != expected.isoformat():
        result['missing'] = _unique(missing + ['未取得目标日完整同源OHLC。'])
        return result
    last = full[-1]
    result.update({key: last[key] for key in ('open', 'close', 'high', 'low')})
    result['as_of'] = last['date']
    o, c, h, low = (last[key] for key in ('open', 'close', 'high', 'low'))
    result['body_pct'] = _round(100 * (c / o - 1))
    if len(full) >= 2:
        result['change_pct'] = _round(100 * (c / full[-2]['close'] - 1))
    else:
        missing.append('缺少同源昨收，昨收涨跌幅未知；不影响完整当日OHLC的形态计算。')
    span = h - low
    if span > 0:
        body = abs(c-o) / span * 100
        position = (c-low) / span * 100
        result['body_range_pct'], result['close_position_pct'] = _round(body), _round(position)
        large = c < o and 100 * (c/o-1) <= -2 + 1e-10 and body >= 60 - 1e-10 and position <= 25 + 1e-10
    else:
        large = False
        missing.append('最高价等于最低价，区间比例无定义，不满足大阴线。')
    if len(full) >= 21 and last['volume'] is not None and all(x['volume'] is not None for x in full[-21:-1]):
        average = statistics.mean(x['volume'] for x in full[-21:-1])
        if average > 0:
            result['volume_ratio20'] = _round(last['volume'] / average)
    if result['volume_ratio20'] is None:
        missing.append('当日量或前20日可比成交量不足，放量状态未知，不据此否定已合格形态。')
    result['is_large_bear'] = None if fatal else bool(large)
    result['missing'] = _unique(missing)
    return result


def compute_report(monitor, evidence=None, now=None):
    """Compute the last imported complete CN candle; do not infer fund flows."""
    now = _now(now)
    if (not isinstance(monitor, dict) or type(monitor.get('schema_version')) is not int
            or monitor['schema_version'] != 1 or not isinstance(monitor.get('cn'), list)
            or len(monitor['cn']) > MAX_PROXIES):
        raise ValueError('Expected an ETF monitor snapshot within the 1000-proxy offline budget')
    expected = _date(monitor.get('as_of_cn'))
    if expected is None:
        raise ValueError('ETF monitor has no valid CN observation date')
    evidence = evidence if isinstance(evidence, dict) else {}
    origins, blocked, gaps = _origins(evidence), [], list(GAPS)
    if expected > now.date() or (expected == now.date() and now.time().replace(tzinfo=None) < dt.time(15)):
        blocked.append('目标日尚未完整收盘或位于未来，不能确认日线形态。')
    for key in ('generated_at', 'published_at'):
        if monitor.get(key):
            at = _stamp(monitor[key])
            if at is None or at > now:
                blocked.append('ETF监控生成或发布时间无效，相关形态未知。')
    candles, seen = [], set()
    for item in monitor['cn']:
        if not isinstance(item, dict) or not _code(item.get('code')):
            gaps.append('监控样本含无效证券记录，未参与形态计数。')
            continue
        code = _code(item['code'])
        if code in seen:
            gaps.append('监控样本含重复代码，未重复计数。')
            continue
        seen.add(code)
        candles.append(_candle(item, origins.get(code, {}), expected, now, blocked))
    known = [x for x in candles if x['is_large_bear'] is not None]
    volume_known = [x for x in candles if x['volume_ratio20'] is not None]
    expected_count = len({_code(row.get('code')) for row in monitor['cn']
                          if isinstance(row, dict) and _code(row.get('code'))})
    if not candles:
        gaps.append('未取得有效ETF代理样本；不把数据空缺解释为没有大阴线。')
    if len(known) != len(candles):
        gaps.append('部分日K的时点、复权或OHLC未核验，is_large_bear为未知，不能按未发生统计。')
    return {'schema_version': 1, 'as_of': expected.isoformat(), 'generated_at': now.isoformat(),
            'universe_label': f'{expected_count}只ETF代理，不是全行业', 'candles': candles, 'flow': empty_flow(),
            'coverage': {'expected': expected_count, 'received': len(candles), 'shape_known': len(known),
                         'shape_unknown': len(candles) - len(known),
                         'large_bear': sum(x['is_large_bear'] is True for x in known),
                         'not_large_bear': sum(x['is_large_bear'] is False for x in known),
                         'volume_ratio_known': len(volume_known),
                         'large_bear_with_volume': sum(x['is_large_bear'] is True and x['volume_ratio20'] >= 1.2 for x in volume_known),
                         'flow_inflows': 0, 'flow_outflows': 0},
            'gaps': _unique(gaps + blocked)}


def _validate_flow(flow, generated):
    keys = {'status', 'provider', 'metric', 'as_of', 'retrieved_at', 'inflows', 'outflows',
            'source', 'note', 'coverage_kind', 'universe_complete', 'is_main_metric'}
    if (not isinstance(flow, dict) or set(flow) != keys or flow['status'] not in ('unavailable', 'reference_only')
            or flow['provider'] != '同花顺' or flow['metric'] != FLOW_METRIC or flow['as_of'] is not None
            or flow['coverage_kind'] != 'publisher_top10_each' or flow['universe_complete'] is not False
            or flow['is_main_metric'] is not False or not isinstance(flow['note'], str)):
        raise ValueError('Funding must remain the undated, non-main THS reference')
    if flow['source'] not in THS_PUBLIC_URLS:
        raise ValueError('Only the declared THS industry funding source is allowed')
    if ACCOUNT_TEXT.search(flow['note']) or PRIVATE.search(flow['note']):
        raise ValueError('Private account descriptions cannot be funding notes')
    retrieved = _stamp(flow['retrieved_at'])
    if flow['status'] == 'reference_only' and (retrieved is None or retrieved > generated):
        raise ValueError('Invalid funding retrieval time')
    if flow['retrieved_at'] is not None and (retrieved is None or retrieved > generated):
        raise ValueError('Invalid funding retrieval time')
    all_codes = set()
    for side in ('inflows', 'outflows'):
        rows = flow[side]
        if not isinstance(rows, list) or len(rows) > 10 or (flow['status'] == 'unavailable' and rows):
            raise ValueError('Funding groups must preserve at most ten verified source rows')
        previous_rank = 0
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {'code', 'name', 'net_yuan', 'rank', 'change_pct'}
                    or not isinstance(row['code'], str) or not re.fullmatch(r'881\d{3}', row['code'])
                    or row['code'] in all_codes or not isinstance(row['name'], str)
                    or type(row['rank']) is not int or not previous_rank < row['rank'] <= 10
                    or _number(row['net_yuan']) is None or isinstance(row['net_yuan'], str)
                    or (row['change_pct'] is not None and (_number(row['change_pct']) is None or isinstance(row['change_pct'], str)))
                    or (side == 'inflows' and row['net_yuan'] < 0) or (side == 'outflows' and row['net_yuan'] >= 0)):
                raise ValueError('Invalid funding row or original source rank')
            all_codes.add(row['code']); previous_rank = row['rank']
    if flow['status'] == 'reference_only' and not all_codes:
        raise ValueError('A funding reference needs at least one verified source row')


def _validate_report(report):
    keys = {'schema_version', 'as_of', 'generated_at', 'universe_label', 'candles', 'flow', 'coverage', 'gaps'}
    if (not isinstance(report, dict) or set(report) != keys or type(report['schema_version']) is not int
            or report['schema_version'] != 1 or not _date(report['as_of']) or not _stamp(report['generated_at'])
            or not isinstance(report['universe_label'], str) or not isinstance(report['candles'], list)
            or len(report['candles']) > MAX_PROXIES or not isinstance(report['coverage'], dict)
            or not isinstance(report['gaps'], list) or any(not isinstance(x, str) for x in report['gaps'])):
        raise ValueError('Invalid ETF pressure snapshot')
    coverage_keys = {'expected', 'received', 'shape_known', 'shape_unknown', 'large_bear',
                     'not_large_bear', 'volume_ratio_known', 'large_bear_with_volume', 'flow_inflows', 'flow_outflows'}
    if (set(report['coverage']) != coverage_keys
            or any(type(value) is not int or value < 0 for value in report['coverage'].values())
            or report['coverage']['expected'] > MAX_PROXIES
            or report['coverage']['received'] != len(report['candles'])
            or report['universe_label'] != f'{report["coverage"]["expected"]}只ETF代理，不是全行业'):
        raise ValueError('Invalid pressure coverage counters')
    row_keys = {'code', 'name', 'as_of', *VALUES, 'is_large_bear', 'missing', 'source', 'price_basis'}
    seen = set()
    for row in report['candles']:
        if (not isinstance(row, dict) or set(row) != row_keys or not isinstance(row['code'], str)
                or _code(row['code']) != row['code'] or not row['code'] or row['code'] in seen
                or not isinstance(row['name'], str) or not isinstance(row['price_basis'], str)
                or not isinstance(row['missing'], list) or any(not isinstance(x, str) for x in row['missing'])
                or any(row[k] is not None and (_number(row[k]) is None or isinstance(row[k], str)) for k in VALUES)
                or (row['is_large_bear'] is not None and type(row['is_large_bear']) is not bool)
                or (row['source'] and _url(row['source']) != row['source'])
                or (row['is_large_bear'] is not None and row['as_of'] != report['as_of'])):
            raise ValueError('Invalid ETF candle fields')
        seen.add(row['code'])
    _validate_flow(report['flow'], _stamp(report['generated_at']))
    encoded = json.dumps(report, ensure_ascii=False, allow_nan=False)
    if PRIVATE.search(encoded) or any(ACCOUNT_TEXT.search(x['name']) for x in report['candles']):
        raise ValueError('Private content is not allowed in a pressure snapshot')
    for side in ('inflows', 'outflows'):
        if any(ACCOUNT_TEXT.search(x['name']) for x in report['flow'][side]):
            raise ValueError('Account content cannot replace an industry name')


def publish_report(report):
    """Validate, archive and atomically publish; do not alter ETF monitor state."""
    stored = copy.deepcopy(report)
    # Recount attached funding evidence rather than trusting imported counters.
    if isinstance(stored, dict) and isinstance(stored.get('flow'), dict) and isinstance(stored.get('coverage'), dict):
        for side in ('inflows', 'outflows'):
            rows = stored['flow'].get(side)
            if isinstance(rows, list):
                stored['coverage']['flow_' + side] = len(rows)
    _validate_report(stored)
    if stored['flow']['status'] == 'reference_only':
        incoming, outgoing = (len(stored['flow'][key]) for key in ('inflows', 'outflows'))
        coverage_note = f'已核验流入{incoming}/10、流出{outgoing}/10条，保留原站名次，不补位；不是全行业覆盖。'
        if coverage_note not in stored['flow']['note']:
            stored['flow']['note'] += ' ' + coverage_note
    content = {k: v for k, v in stored.items() if k != 'generated_at'}
    digest = hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()
    folder = research_path('etf_pressure')
    with update_lock():
        previous = read_json(folder / 'current.json', {})
        if isinstance(previous, dict) and previous:
            old, new = _date(previous.get('as_of')), _date(stored['as_of'])
            if old and old > new:
                raise ValueError('A newer ETF pressure observation is already published')
            old_at = _stamp(previous.get('generated_at'))
            if old_at and old_at > _stamp(stored['generated_at']):
                raise ValueError('A newer ETF pressure computation is already published')
        atomic_json(folder / 'archive' / stored['as_of'] / (digest + '.json'), stored)
        atomic_json(folder / 'current.json', stored)
    return stored


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    load = sub.add_parser('import', help='Read offline OHLC evidence and publish the separate pressure view')
    load.add_argument('--cn-evidence', type=Path)
    load.add_argument('--flow', type=Path)
    load.add_argument('--monitor', type=Path, help='Optional monitor fixture; defaults to private current snapshot')
    load.add_argument('--build', action='store_true')
    args = parser.parse_args(argv)
    try:
        monitor = _load(args.monitor) if args.monitor else read_json(research_path('etf_monitor/current.json'), {})
        report = compute_report(monitor, _load(args.cn_evidence))
        if args.flow:
            report['flow'] = _load(args.flow)
        report = publish_report(report)
        if args.build:
            from build_investment_site import build
            build()
    except ValueError:
        parser.exit(2, 'ETF pressure import failed validation; verify dates, adjusted OHLC and THS evidence.\n')
    print(json.dumps({'as_of': report['as_of'], 'coverage': report['coverage'],
                      'flow_status': report['flow']['status']}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
