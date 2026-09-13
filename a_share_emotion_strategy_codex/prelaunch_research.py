"""V3.4 prelaunch research: deterministic gates plus dated evidence review.

No orders, accounts, scheduler, or publication side effects. ``research`` accepts
an immutable input bundle; a missing bundle invokes the bounded public adapter.
Enrichment must match ``signal_date`` and ``input_fingerprint``. It may provide
``market``, ``industries`` and per-code ``stocks`` evidence, never computed scores.
The adapter deliberately leaves announcement/industry/cost gates unknown.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import copy
import datetime as dt
import hashlib
import json
import math
import re
import statistics
import time
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

from market_calendar import is_trading_day

TZ = ZoneInfo('Asia/Shanghai')
VERSION = '3.4'
COMMIT = '21f0fa43c160e36f8fdfbe40d1ebabd652c16b80'
SOURCE_HASH = 'ad074406be40558efc51d4a2e2850cf9a2a3424bc13d5d53026a71da8115d682'
RULE_URL = 'https://github.com/Mrrabbitan/a-share-prelaunch-stock-skill/tree/' + COMMIT
PREFIXES = ('600', '601', '603', '605', '000', '001', '002', '003')
SINA_BASE = 'https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/'


def finite(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def fingerprint(value):
    """Exclude acquisition clocks; preserve source dates, values and evidence."""
    def clean(x):
        if isinstance(x, dict):
            return {k: clean(v) for k, v in x.items() if k not in ('generated_at', 'retrieved_at', 'fetched_at')}
        return [clean(v) for v in x] if isinstance(x, list) else x
    return hashlib.sha256(json.dumps(clean(value), ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _sessions(start, count, direction=1):
    days, cursor = [], start
    for _ in range(count * 4 + 30):
        cursor += dt.timedelta(days=direction)
        ok, reason = is_trading_day(cursor)
        if '缺少' in reason or '不可用' in reason:
            raise ValueError(reason)
        if ok:
            days.append(cursor.isoformat())
            if len(days) == count:
                return days if direction > 0 else list(reversed(days))
    raise ValueError('交易日窗口无法确认')


def signal_day(now):
    now = now.astimezone(TZ) if now.tzinfo else now.replace(tzinfo=TZ)
    day = now.date()
    ok, reason = is_trading_day(day)
    if '缺少' in reason or '不可用' in reason:
        raise ValueError(reason)
    return day.isoformat() if ok and now.time() >= dt.time(15, 5) else _sessions(day, 1, -1)[0]


def normalize_bars(rows, cutoff):
    result, seen = [], set()
    for row in rows or []:
        day = str(row.get('date', ''))
        try:
            dt.date.fromisoformat(day)
        except ValueError:
            raise ValueError('日线日期无效')
        if day > cutoff:
            continue
        if day in seen:
            raise ValueError('日线日期重复')
        seen.add(day)
        b = dict(row)
        for key in ('open', 'high', 'low', 'close', 'volume_shares'):
            b[key] = finite(row.get(key))
            if b[key] is None or b[key] <= 0:
                raise ValueError('日线价格/成交股数缺失或非正')
        if not b['low'] <= min(b['open'], b['close']) <= max(b['open'], b['close']) <= b['high']:
            raise ValueError('OHLC关系冲突')
        b['amount_cny'] = finite(row.get('amount_cny'))
        result.append(b)
    return sorted(result, key=lambda x: x['date'])


def _ret(bars, n):
    return bars[-1]['close'] / bars[-n-1]['close'] - 1 if len(bars) > n else None


def _metrics(bars):
    if len(bars) < 65:
        return {}
    closes = [b['close'] for b in bars]
    volumes = [b['volume_shares'] for b in bars]
    ma20 = statistics.mean(closes[-20:])
    ma60, old_ma60 = statistics.mean(closes[-60:]), statistics.mean(closes[-65:-5])
    trs = [max(bars[i]['high'] - bars[i]['low'], abs(bars[i]['high'] - closes[i-1]),
               abs(bars[i]['low'] - closes[i-1])) for i in range(len(bars)-20, len(bars))]
    atr = statistics.mean(trs)
    probe = None
    for i in range(len(bars)-10, len(bars)):
        b = bars[i]
        baseline = statistics.median(volumes[i-20:i])
        vr = b['volume_shares'] / baseline if baseline > 0 else None
        clv = (b['close'] - b['low']) / (b['high'] - b['low']) if b['high'] > b['low'] else None
        gain = b['close'] / closes[i-1] - 1
        if vr is not None and 1.5 - 1e-12 <= vr <= 3 + 1e-12 and -1e-12 <= gain <= .05 + 1e-12 and clv is not None and clv >= .6 - 1e-12:
            probe = {'date': b['date'], 'index': i, 'vr': vr, 'clv': clv,
                     'low': b['low'], 'volume_shares': b['volume_shares']}
    amounts = [b['amount_cny'] for b in bars[-20:]]
    return {'r5': _ret(bars, 5), 'r20': _ret(bars, 20), 'ma20': ma20, 'ma60': ma60,
            'ma60_prev5': old_ma60, 'atr20': atr if atr > 0 else None,
            'vr': volumes[-1] / statistics.median(volumes[-21:-1]),
            'trend': closes[-1] >= ma60 and ma60 >= old_ma60, 'probe': probe,
            'amount20_median': statistics.median(amounts) if all(a is not None and a > 0 for a in amounts) else None}


def _dated_evidence(record, day):
    if not (isinstance(record, dict) and record.get('verified') is True
            and record.get('as_of') == day and bool(record.get('sources'))):
        return False
    cutoff = dt.datetime.combine(dt.date.fromisoformat(day), dt.time(15), TZ)
    for source in record['sources']:
        if not isinstance(source, dict):
            return False
        for field in ('published_at', 'source_asof', 'available_at'):
            if source.get(field):
                stamp = _timestamp(source[field])
                if stamp is None or stamp > cutoff:
                    return False
    return True


def _complete_tail(bars, day, count=65):
    expected = _sessions(dt.date.fromisoformat(day), count - 1, -1) + [day]
    return len(bars) >= count and [b['date'] for b in bars[-count:]] == expected


def _condition(label, passed, detail):
    return {'label': label, 'passed': passed, 'detail': detail}


def _freeze(stock, bars, metrics, day, records):
    code, basis = stock['code'], stock.get('adjustment_basis')
    matching = [r for r in records if r.get('code') == code]
    active = next((r for r in reversed(matching) if r.get('state') == 'active'), None)
    if active and day > active['valid_until']:
        active.update(state='expired', ended_at=day, reason='五个交易日观察期限结束，须重新筛选')
        active = None
    probe = metrics.get('probe')
    if not active and probe:
        # The same old probe cannot create a new window after expiration/failure.
        if not any(r.get('probe_date') == probe['date'] for r in matching):
            i = probe['index']
            platform = bars[i-20:i]
            if len(platform) == 20:
                window = _sessions(dt.date.fromisoformat(day), 5)
                active = {'id': code + ':' + probe['date'] + ':' + day, 'code': code,
                          'probe_date': probe['date'], 'platform_start': platform[0]['date'],
                          'platform_end': platform[-1]['date'], 'support': min(b['low'] for b in platform),
                          'upper': max(b['high'] for b in platform), 'frozen_at': day,
                          'window_start': window[0], 'valid_until': window[-1],
                          'method': '试盘日前20个完整交易日高低价代理', 'adjustment_basis': basis, 'state': 'active'}
                active['anchor_fingerprint'] = fingerprint([
                    {k: b[k] for k in ('date', 'open', 'high', 'low', 'close')} for b in platform])
                records.append(active)
    if active and active.get('adjustment_basis') != basis:
        return active, '复权基准改变，冻结结构须同基准复核'
    if active:
        anchor = [b for b in bars if active['platform_start'] <= b['date'] <= active['platform_end']]
        current_hash = fingerprint([{k: b[k] for k in ('date', 'open', 'high', 'low', 'close')} for b in anchor])
        if len(anchor) != 20 or current_hash != active.get('anchor_fingerprint'):
            return active, '冻结区间价格发生复权/修订或缺失，须同基准复核，禁止重设支撑'
    return active, None


def _stock_result(stock, data, reviews, records, day, market, industries):
    code = str(stock.get('code', ''))
    row = {'code': code, 'name': stock.get('name', code), 'group': stock.get('industry') or '行业待核验',
           'status': '待验证', 'eligible': False, 'conditions': [], 'bars': [], 'levels': [],
           'metrics': {}, 'reasons': [], 'sources': list(stock.get('sources', [])),
           'scores': None, 'total_score': None, 'evidence_grade': 'C'}
    cond = row['conditions']
    mainboard = bool(re.fullmatch(r'\d{6}', code) and code.startswith(PREFIXES))
    cond.append(_condition('沪深主板普通A股范围', mainboard, '代码只作范围初筛，证券状态另行核验'))
    cap = finite(stock.get('float_cap_cny'))
    cap_known = (stock.get('cap_date') == day and stock.get('cap_verified') is True
                 and cap is not None and cap > 0 and bool(stock.get('sources')))
    cap_ok = cap <= 50_000_000_000 if cap_known else None
    cond.append(_condition('同日流通A股市值≤500亿元', cap_ok, f'{cap / 1e8:.2f}亿元' if cap_known else '口径、单位、日期或数值待核验'))
    row['metrics']['流通市值亿元'] = cap / 1e8 if cap_known else None
    try:
        bars = normalize_bars(stock.get('bars'), day)
    except ValueError as exc:
        bars = []; row['reasons'].append(str(exc))
    row['bars'] = bars[-90:]
    ready = _complete_tail(bars, day)
    cond.append(_condition('最近65个交易日日线完整', ready, f'有效日线{len(bars)}条，须逐日匹配交易日历；不填补缺失日'))
    if not mainboard or cap_ok is False or not ready:
        row['status'] = '排除' if not mainboard or cap_ok is False else '待验证'
        row['reasons'] += [c['detail'] for c in cond if c['passed'] is not True]
        return row
    evidence = reviews.get(code, {})
    industry_mapping = evidence.get('industry', {})
    industry_id = stock.get('industry')
    if _dated_evidence(industry_mapping, day) and industry_mapping.get('classification') and industry_mapping.get('id'):
        industry_id = industry_mapping['id']
        row['group'] = industry_id
    security = evidence.get('security', stock.get('security', {}))
    security_known = _dated_evidence(security, day)
    security_ok = (security.get('ordinary_a') is True and security.get('normal_limits') is True
                   and security.get('suspended') is False and security.get('risk_clear') is True) if security_known else None
    cond.append(_condition('证券状态及公开风险复核', security_ok, '需当时证券状态、审计/现金流偿债、调查及重大风险证据'))
    m = _metrics(bars)
    row['metrics'].update({'5日涨幅%': m['r5'] * 100, '20日涨幅%': m['r20'] * 100,
                          'MA20': m['ma20'], 'MA60': m['ma60'], 'ATR20': m['atr20'], '中位量比': m['vr']})
    history_review = evidence.get('history', {})
    reviewed_history = (_dated_evidence(history_review, day)
                        and history_review.get('adjustment_basis') == stock.get('adjustment_basis')
                        and set(history_review.get('verified_dates', [])) == {b['date'] for b in bars[-65:]})
    hist_ok = (stock.get('history_verified') is True or reviewed_history) and bool(stock.get('adjustment_basis'))
    cond.append(_condition('行情及复权基准核验', True if hist_ok else None, '双源OHLC、可比股数和冻结价基准须一致'))
    last5 = copy.deepcopy(bars[-5:])
    limits_review = evidence.get('limit_checks', {})
    limit_conflict = False
    if _dated_evidence(limits_review, day):
        for b in last5:
            attestation = limits_review.get('dates', {}).get(b['date'], {})
            raw_close, ceiling = finite(attestation.get('raw_close')), finite(attestation.get('official_limit_up'))
            if (attestation.get('sources') and raw_close is not None and raw_close > 0
                    and ceiling is not None and ceiling > 0 and attestation.get('normal_limits') is True):
                new_limit = abs(raw_close - ceiling) < .005
                if b.get('limit_verified') is True and isinstance(b.get('limit_up'), bool) and b['limit_up'] != new_limit:
                    limit_conflict = True
                    b['limit_up'] = b['limit_up'] or new_limit
                else:
                    b.update(limit_verified=True, limit_up=new_limit)
    actual_limit = any(b.get('limit_up') is True and b.get('limit_verified') is True for b in last5)
    limits_known = all(isinstance(b.get('limit_up'), bool) and b.get('limit_verified') is True for b in last5)
    accelerated_proxy = m['r5'] > .12 + 1e-12 or (m['atr20'] is not None and (bars[-1]['close'] - m['ma20']) / m['atr20'] > 2.5 + 1e-12)
    accelerated = actual_limit or accelerated_proxy
    cond.append(_condition('未加速且最近5日无实际收盘涨停', False if accelerated else True if limits_known else None,
                           '涨停按当日规则核验；5日涨幅>12%或偏离MA20>2.5ATR均转已启动'))
    if limit_conflict:
        cond.append(_condition('历史涨停来源无冲突', None, '人工复核与已核验日线存在冲突；保留涨停风险，不用新证据覆盖已知涨停'))
    frozen, freeze_error = _freeze(stock, bars, m, day, records)
    cond.append(_condition('冻结平台与结构支撑', None if freeze_error or not frozen else True,
                           freeze_error or (f"{frozen['platform_start']}至{frozen['platform_end']}，首次冻结于{frozen['frozen_at']}" if frozen else '无可用冻结平台，或本轮已到期')))
    close = bars[-1]['close']
    broken = bool(frozen and not freeze_error and close < frozen['support'])
    launched = bool(frozen and not freeze_error and close > frozen['upper'])
    if frozen and (broken or launched or accelerated or security_ok is False):
        frozen.update(state='invalid' if broken or security_ok is False else 'started', ended_at=day,
                      reason='结构支撑失效或证券硬风险' if broken or security_ok is False else '已突破冻结平台或触发加速')
    probe = m['probe']
    held = False
    if probe and frozen and not freeze_error:
        after = bars[probe['index']+1:]
        held = len(after) >= 2 and statistics.median(b['volume_shares'] for b in after) < probe['volume_shares'] and close >= probe['low']
        held = held and all(b['close'] >= frozen['support'] for b in after)
        held = held and all(b['close'] >= frozen['support'] for b in bars if b['date'] >= frozen['frozen_at'])
    cond += [_condition('MA60趋势与五日斜率', m['trend'], '收盘≥MA60且MA60≥五日前MA60'),
             _condition('温和试盘', bool(probe), f"{probe['date']}量比{probe['vr']:.2f}、CLV{probe['clv']:.2f}" if probe else '最近10日没有满足VR1.5—3、涨幅0—5%、CLV≥0.6的试盘'),
             _condition('试盘后至少两日缩量承接', held, '成交量中位数低于试盘日、最新收盘守试盘低点且冻结支撑未失效')]
    benchmark = data.get('benchmark', {})
    try:
        benchmark_bars = normalize_bars(benchmark.get('bars'), day)
    except ValueError:
        benchmark_bars = []
    benchmark_ok = (benchmark.get('code') == '000300' and benchmark.get('verified') is True
                    and _complete_tail(benchmark_bars, day))
    market_r5 = _ret(benchmark_bars, 5) if benchmark_ok else None
    breadth = finite(market.get('above_ma20_ratio'))
    environment_known = _dated_evidence(market, day) and breadth is not None and 0 <= breadth <= 1 and market_r5 is not None
    defensive = bool(environment_known and market_r5 < 0 and breadth < .35)
    cond.append(_condition('市场非防守且广度有效', not defensive if environment_known else None, '沪深300近5日下跌且市场MA20广度<35%时只观察；广度缺失不升级'))
    industry = industries.get(industry_id, {})
    industry_known = _dated_evidence(industry, day) and bool(industry.get('classification'))
    ir5, ir20, ibreadth = (finite(industry.get(k)) for k in ('r5', 'r20', 'above_ma20_ratio'))
    industry_known = industry_known and benchmark_ok and ir5 is not None and ir20 is not None and ibreadth is not None and 0 <= ibreadth <= 1
    sector_ok = ir5 - market_r5 > 0 and ibreadth >= .5 if industry_known else None
    cond.append(_condition('固定行业相对强度与广度', sector_ok, '行业RS5>0且成分站上MA20比例≥50%；不以概念热点替代'))
    rs5, rs20 = (m['r5'] - ir5, m['r20'] - ir20) if industry_known else (None, None)
    row['metrics'].update({'行业相对5日%': rs5 * 100 if rs5 is not None else None,
                           '行业相对20日%': rs20 * 100 if rs20 is not None else None})
    pressure = evidence.get('pressure', {})
    pressure_ok = _dated_evidence(pressure, day) and bool(pressure.get('method'))
    target = finite(pressure.get('lower')) if pressure_ok else None
    support = frozen['support'] if frozen and not freeze_error else None
    if target is not None and frozen and not freeze_error and close < frozen['upper'] < target:
        target = frozen['upper']
        row['reasons'].append('输入压力更远；已知冻结上沿位于现价上方，按更近上沿保守重算空间，不能跨过它抬高目标。')
    costs = evidence.get('costs', {})
    k = finite(costs.get('k_per_share')) if _dated_evidence(costs, day) and costs.get('assumptions') else None
    k = k if k is not None and k >= 0 else None
    geometry = support is not None and target is not None and 0 < support < close < target
    gross_rr = (target-close)/(close-support) if geometry else None
    net_rr = (target-close-k)/(close-support+k) if geometry and k is not None else None
    space_ok = net_rr >= 2 - 1e-12 and target-close > k if net_rr is not None else False if target is not None and support is not None and not geometry else None
    cond.append(_condition('最近压力及扣费净空间≥2', space_ok, '未知成本只列毛空间；不得跨过更近压力或改低支撑'))
    row['levels'] = [{'label': key, 'value': value} for key, value in (('参考收盘', close), ('冻结支撑', support), ('冻结上沿', frozen.get('upper') if frozen else None), ('最近压力', target)) if value is not None]
    row['metrics'].update({'试盘日期': probe['date'] if probe else None, '毛空间比': gross_rr, '净空间比': net_rr,
                          '每股往返成本情景': k, '20日成交额中位数': m['amount20_median']})
    business = evidence.get('business', {})
    business_level = business.get('level') if _dated_evidence(business, day) else None
    if business_level == 2:
        event_date = business.get('event_date', '')
        if not day < event_date <= (dt.date.fromisoformat(day) + dt.timedelta(days=21)).isoformat():
            business_level = None
    business_known = type(business_level) is int and business_level in (0, 1, 2)
    cond.append(_condition('业务/催化事实已完成核验', True if business_known else None, '催化可为0分，但未核验不填0；已发生事件不加未来催化分'))
    cond.append(_condition('20日成交额排序依据有效', True if m['amount20_median'] is not None else None, '不可用收盘价乘成交量估算真实成交额'))
    if broken or security_ok is False:
        row['status'] = '失效'
    elif accelerated or launched:
        row['status'] = '已启动'
    elif not frozen and any(r.get('code') == code and r.get('state') == 'expired'
                            and (not probe or r.get('probe_date') == probe['date']) for r in records):
        row['status'] = '观察到期'
        row['reasons'].append('五个交易日观察结束，旧试盘不得自动延长；等待新信号重新筛选')
    elif any(c['passed'] is None for c in cond):
        row['status'] = '待验证'
    elif all(c['passed'] is True for c in cond):
        distance = frozen['upper'] - close
        row['status'] = '待确认' if m['atr20'] and 0 <= distance <= m['atr20'] else '蓄势'
        row['eligible'] = True
    else:
        row['status'] = '观察'
    # Scores are complete only when all their inputs (not their outcomes) exist.
    outperform = finite(industry.get('outperform_market_days5'))
    if industry_known and rs5 is not None and net_rr is not None and business_known and outperform is not None:
        persistent = sector_ok and ir20 - _ret(benchmark_bars, 20) > 0 and outperform >= 3
        upward = bars[-1]['close'] >= bars[-2]['close'] >= bars[-3]['close']
        row['scores'] = {'板块持续性': (2 if persistent else 1 if sector_ok else 0) * 10,
                         '个股相对强度': ((rs5 > 0) + (rs20 > 0)) * 10,
                         '量价承接': (2 if held and upward else 1 if held else 0) * 10,
                         '结构空间': (2 if net_rr >= 3 else 1 if net_rr >= 2 else 0) * 10,
                         '催化与业务证据': business_level * 10}
        row['total_score'] = sum(row['scores'].values())
    if row['eligible'] and row['total_score'] is None:
        row['eligible'] = False; row['status'] = '待验证'
        row['reasons'].append('五维评分输入不完整，不进入核心排名')
    row['evidence_grade'] = ('B' if pressure.get('proxy', True) else 'A') if row['eligible'] else 'C'
    row['reasons'] += [c['label'] + '：' + c['detail'] for c in cond if c['passed'] is not True]
    row['reasons'].append(evidence.get('counterevidence') or '最强反证：试盘后失守低点、收盘跌破冻结支撑，或板块强而个股持续相对走弱。')
    row['confirmation'] = '仅资格完整时观察后续确认；突破冻结平台且量比≥1.5转已启动跟踪，不仍列潜伏买点。'
    row['invalidation'] = '支撑失效或硬风险提前结束；五个交易日无进展重筛；T+1及跌停可能无法退出。'
    row['frozen_id'] = frozen.get('id') if frozen else None
    row['valid_until'] = frozen.get('valid_until') if frozen else None
    row['catalyst_group'] = business.get('catalyst_group') or '无已核验催化'
    row['sources'] += [s for x in evidence.values() if isinstance(x, dict) for s in x.get('sources', [])]
    return row


def _timestamp(value):
    try:
        stamp = dt.datetime.fromisoformat(str(value))
        return stamp.astimezone(TZ) if stamp.tzinfo else stamp.replace(tzinfo=TZ)
    except (ValueError, TypeError):
        return None


def _continuous_session(stamp):
    clock = stamp.time().replace(tzinfo=None)
    return dt.time(9, 30) <= clock <= dt.time(11, 30) or dt.time(13) <= clock <= dt.time(15)


def _intraday(now, previous, input_data):
    """Overlay current quotes on a still-valid close report, never rescreen."""
    base = {'module_id': 'prelaunch', 'schema_version': 1, 'phase': 'intraday',
            'as_of': None, 'intraday_as_of': None, 'generated_at': dt.datetime.now(TZ).isoformat(),
            'valid_until': None, 'status': 'unavailable', 'summary': '', 'coverage': {},
            'missing': [], 'sources': [{'label': '固定V3.4规则', 'url': RULE_URL}],
            'rules': {'version': VERSION, 'source_hash': SOURCE_HASH, 'commit': COMMIT},
            'core': [], 'watch': [], 'started': [], 'invalid': [], 'frozen_records': [], 'candidates': []}
    previous = previous or {}
    cutoff = _timestamp(previous.get('as_of'))
    try:
        expiry = dt.date.fromisoformat(str(previous.get('valid_until'))[:10])
    except ValueError:
        expiry = None
    if (previous.get('module_id') != 'prelaunch' or previous.get('schema_version') != 1
            or previous.get('rules', {}).get('source_hash') != SOURCE_HASH or cutoff is None
            or cutoff >= now or expiry is None or expiry < now.date()
            or previous.get('status') not in ('complete', 'partial', 'empty')):
        base['summary'] = '缺少同规则且仍在有效窗口内的收盘观察报告；盘中不重跑全市场，等待收盘准备或重新研究。'
        base['missing'] = [base['summary']]
        return base
    result = copy.deepcopy(previous)
    result.update(phase='intraday', intraday_as_of=None, generated_at=base['generated_at'])
    result['close_status'] = previous.get('close_status', previous['status'])
    result.pop('intraday_context', None)
    # Retain exactly the close as_of, expiry, input fingerprint, bars, scores,
    # eligibility, and frozen records. The overlay is not a new close signal.
    scope = {}
    for key in ('core', 'watch'):
        for row in previous.get(key, []):
            row_expiry = str(row.get('valid_until') or previous['valid_until'])[:10]
            code = str(row.get('code', ''))
            if row_expiry >= now.date().isoformat() and re.fullmatch(r'\d{6}', code) and code.startswith(PREFIXES):
                scope[code] = row
    for key in ('core', 'watch', 'started', 'invalid', 'candidates'):
        for row in result.get(key, []):
            row.pop('intraday_context', None)
            for metric in list(row.get('metrics', {})):
                if metric.startswith('盘中'):
                    row['metrics'].pop(metric)
    result['coverage'] = dict(previous.get('coverage', {}), intraday_requested=len(scope), intraday_valid=0)
    result['intraday_context'] = {'mode': '仅原观察项价格风险背景', 'close_signal_unchanged': True,
                                  'quote_max_age_seconds': 90, 'codes': sorted(scope), 'errors': []}
    if not scope:
        result['summary'] = '原收盘报告没有仍有效的观察项，盘中不新增或重筛股票；原始收盘结果和期限保持不变。'
        result['status'] = 'empty' if previous.get('status') == 'empty' else 'partial'
        return result
    trade_day, reason = is_trading_day(now.date())
    if not trade_day or not _continuous_session(now):
        result['summary'] = '当前不是有效连续交易时段；保留原收盘观察，暂停盘中价格检查，不延长观察期限。'
        result['intraday_context']['errors'] = ['休市/午休/盘前等待；' + reason]
        result['status'] = 'partial'
        return result
    sources, errors = [], []
    try:
        if input_data is not None:
            quotes = input_data.get('quotes', {})
            sources = input_data.get('sources', [])
        else:
            quotes, sources = PublicFeed().quotes(sorted(scope))
        if not isinstance(quotes, dict):
            raise ValueError('盘中报价不是按代码组织的对象')
    except Exception as exc:
        quotes = {}; errors.append('观察项报价失败：' + type(exc).__name__)
    validated = {}
    for code in scope:
        quote = quotes.get(code, {})
        stamp = _timestamp(quote.get('timestamp') or quote.get('time') or quote.get('source_asof'))
        price = finite(quote.get('price'))
        valid = (stamp is not None and stamp.date() == now.date() and _continuous_session(stamp)
                 and 0 <= (now - stamp).total_seconds() <= 90 and price is not None and price > 0
                 and bool(quote.get('source') or quote.get('sources')))
        if valid:
            validated[code] = (quote, stamp, price)
        else:
            errors.append(code + '：当日报价缺失、超过90秒、来源缺失或不属于有效交易时段')
    for key in ('core', 'watch', 'candidates'):
        for row in result.get(key, []):
            code = row.get('code')
            if code not in scope:
                continue
            if code not in validated:
                row['intraday_context'] = {'status': 'unavailable', 'notes': ['盘中有效报价待取得；不能借收盘时间或旧股价补时间戳。']}
                continue
            quote, stamp, price = validated[code]
            levels = {x.get('label'): finite(x.get('value')) for x in row.get('levels', [])}
            support, upper, target = (levels.get(k) for k in ('冻结支撑', '冻结上沿', '最近压力'))
            notes = []
            if support is not None and price <= support:
                notes.append('盘中触及或低于冻结支撑：价格风险提示，尚不是收盘结构失效确认。')
            if upper is not None and price > upper:
                notes.append('盘中越过冻结上沿：等待完整收盘及量能核验，不能改列已启动或新增潜伏资格。')
            if target is not None and price >= target:
                notes.append('盘中已到最近压力附近或之上，原空间情景不能直接沿用。')
            k = finite(row.get('metrics', {}).get('每股往返成本情景'))
            geometry = support is not None and target is not None and 0 < support < price < target
            gross = (target - price) / (price - support) if geometry else None
            net = (target - price - k) / (price - support + k) if geometry and k is not None else None
            if target is None:
                notes.append('最近压力证据缺失，不编造盘中空间。')
            if net is not None and net < 2:
                notes.append('按当前价估算的净空间低于2，仅提示原计划需复核，不重写原收盘资格。')
            notes.append('盘中背景不改变收盘信号、评分、冻结支撑或五日到期日。')
            row['intraday_context'] = {'status': 'observed', 'price': price, 'source_asof': stamp.isoformat(),
                                      'source': quote.get('source') or quote.get('sources'), 'notes': notes}
            row['metrics'].update({'盘中参考价': price, '盘中行情时间': stamp.isoformat(),
                                   '盘中毛空间情景': gross, '盘中净空间情景': net,
                                   '盘中风险背景': '；'.join(notes)})
    result['coverage']['intraday_valid'] = len(validated)
    result['intraday_context']['errors'] = errors
    if validated:
        result['intraday_as_of'] = max(v[1] for v in validated.values()).isoformat()
    result['sources'] = previous.get('sources', []) + sources
    result['status'] = 'partial' if errors or result['close_status'] == 'partial' else result['close_status']
    result['summary'] = (f"收盘信号仍截至{cutoff.date()}；本次仅检查原观察项盘中报价{len(validated)}/{len(scope)}只。"
                         '日内支撑触及只提示风险，资格、日线与五日有效期未重算。')
    return result


def research(as_of: dt.datetime, phase: str = 'prepare', input_data: dict = None,
             enrichment: dict = None, previous: dict = None) -> dict:
    now = as_of.astimezone(TZ) if as_of.tzinfo else as_of.replace(tzinfo=TZ)
    if phase == 'intraday':
        return _intraday(now, previous, input_data)
    calendar_error = None
    try:
        day = signal_day(now)
    except ValueError as exc:
        day = None; calendar_error = str(exc)
    result = {'module_id': 'prelaunch', 'schema_version': 1,
              'as_of': day + 'T15:00:00+08:00' if day else None, 'generated_at': dt.datetime.now(TZ).isoformat(),
              'valid_until': None, 'phase': phase, 'status': 'unavailable', 'summary': '',
              'coverage': {}, 'missing': [], 'sources': [{'label': '固定V3.4规则', 'url': RULE_URL}],
              'rules': {'version': VERSION, 'source_hash': SOURCE_HASH, 'commit': COMMIT},
              'parameters': {'benchmark': '沪深300', 'cap_max_cny': 50_000_000_000,
                             'observation_sessions': 5, 'costs_default': None},
              'core': [], 'watch': [], 'started': [], 'invalid': [], 'frozen_records': [], 'candidates': []}
    try:
        if calendar_error:
            raise ValueError(calendar_error)
        data = copy.deepcopy(input_data) if input_data is not None else collect(now)
        data_day = str(data.get('signal_date', ''))
        if data_day != day:
            raise ValueError('输入信号日不是分析时点最近完整交易日；旧结果不得重新标成当前')
        digest = fingerprint(data)
        result['input_fingerprint'] = digest
        result['signal_date'] = day
        result['coverage'] = data.get('coverage', {})
        result['sources'] += data.get('sources', [])
        result['missing'] += data.get('missing', [])
        review = enrichment or {}
        bound = (review.get('signal_date') == day and review.get('input_fingerprint') == digest
                 and review.get('rules_source_hash') == SOURCE_HASH)
        if review and not bound:
            result['missing'].append('人工证据与信号日、输入指纹或规则版本不匹配，未应用')
        review = review if bound else {}
        result['evidence_review_applied'] = bool(review)
        records = copy.deepcopy((previous or {}).get('frozen_records', []))
        if any(r.get('frozen_at', '') > day or r.get('ended_at', '') > day for r in records):
            records = []; result['missing'].append('拒绝使用信号日之后的冻结/失效记录，避免未来信息')
        if previous and (previous.get('rules', {}).get('source_hash') != SOURCE_HASH):
            records = []; result['missing'].append('旧冻结记录规则版本不同，仅留历史，不作为本轮有效平台')
        stocks = data.get('stocks', [])
        if len({str(s.get('code')) for s in stocks}) != len(stocks):
            raise ValueError('输入股票代码重复')
        rows = [_stock_result(s, data, review.get('stocks', {}), records, day,
                              review.get('market', data.get('market', {})),
                              review.get('industries', data.get('industries', {}))) for s in stocks]
        qualified = sorted([r for r in rows if r['eligible']], key=lambda r: (
            -r['total_score'], -r['metrics']['净空间比'], -r['metrics']['行业相对20日%'],
            -r['metrics']['20日成交额中位数'], r['code']))
        groups, catalysts = {}, {}
        for r in qualified:
            group, catalyst = r['group'], r['catalyst_group']
            if len(result['core']) == 10 or groups.get(group, 0) >= 3 or catalysts.get(catalyst, 0) >= 3:
                r['eligible'] = False; r['status'] = '观察'; r['reasons'].insert(0, '符合个股数值资格，受Top10行业/催化组合上限限制')
                continue
            r['rank'] = len(result['core']) + 1
            result['core'].append(r)
            groups[group] = groups.get(group, 0) + 1; catalysts[catalyst] = catalysts.get(catalyst, 0) + 1
        # Keep full research records privately; UI rows favour evidence-bearing
        # near-candidates, never present their order as a qualified core ranking.
        result['watch'] = [r for r in rows if r['status'] in ('观察', '待验证')]
        result['started'] = [r for r in rows if r['status'] == '已启动']
        result['invalid'] = [r for r in rows if r['status'] in ('失效', '排除', '观察到期')]
        prior_invalid = (previous or {}).get('invalid', [])
        present = {r['code'] for r in result['invalid']}
        result['invalid'] += [r for r in prior_invalid if r.get('code') not in present and r.get('status') in ('失效', '观察到期')]
        result['frozen_records'] = records
        meaningful = sorted(result['watch'], key=lambda r: (-sum(c['passed'] is True for c in r['conditions']), r['code']))
        detail_room = max(0, 30 - len(result['core']))
        result['candidates'] = result['core'] + meaningful[:detail_room]
        result['detail_limit_note'] = '详情最多30只，仅为页面展示上限；完整观察/已启动/失效记录保存在本地，不改变核心排名。'
        result['coverage'].update({'processed': len(rows), 'core': len(result['core']),
                                   'watch': len(result['watch']), 'started': len(result['started']),
                                   'excluded_or_invalid': len(result['invalid'])})
        incomplete = bool(result['missing'] or any(any(c['passed'] is None for c in r['conditions']) for r in rows))
        result['status'] = 'unavailable' if not rows else 'partial' if incomplete else 'complete' if result['core'] else 'empty'
        result['valid_until'] = _sessions(dt.date.fromisoformat(day), 5)[-1]
        result['summary'] = (f"截至{day}完整日线：核心潜伏{len(result['core'])}只，未通过/待验证{len(result['watch'])}只，已启动{len(result['started'])}只。"
                             '研究优先级不等于买入指令；净成本和关键证据缺失时不升级核心。')
        result['evidence_request'] = {'signal_date': day, 'input_fingerprint': digest, 'rules_source_hash': SOURCE_HASH,
            'required': ['同日证券/风险公告', '固定行业分类与完整广度', '真实历史涨停规则',
                         '最近压力及证据日期', '每股往返费用情景', '业务与未来催化核验'],
            'codes': [r['code'] for r in meaningful[:20]]}
    except (ValueError, TypeError, KeyError, OSError) as exc:
        result['missing'].append(str(exc))
        result['summary'] = '启动前潜伏研究未完成，保留规则说明，不能把旧名单当作新候选。'
    return result


class PublicFeed:
    """Bounded HTTP and explicit units; caches are optional private artifacts."""
    def __init__(self, timeout=8, raw_directory=None):
        self.timeout = timeout
        self.raw_directory = Path(raw_directory) if raw_directory else None

    def read(self, url, encoding='utf-8'):
        request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.sina.com.cn/'})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = response.read().decode(encoding)
        source = {'label': '供应商原始行情', 'url': url, 'retrieved_at': dt.datetime.now(TZ).isoformat(),
                  'sha256': hashlib.sha256(body.encode()).hexdigest()}
        if self.raw_directory:
            self.raw_directory.mkdir(parents=True, exist_ok=True)
            (self.raw_directory / (source['sha256'] + '.json')).write_text(
                json.dumps({'body': body, 'source': source}, ensure_ascii=False), encoding='utf-8')
        return body, source

    def universe(self):
        body, source = self.read(SINA_BASE + 'Market_Center.getHQNodeStockCount?node=hs_a')
        match = re.search(r'\d+', body)
        if not match:
            raise ValueError('全市场数量不可用')
        expected = int(match.group())
        if not 1 <= expected <= 15000:
            raise ValueError('股票数量异常')
        def page(n):
            url = SINA_BASE + 'Market_Center.getHQNodeData?' + urllib.parse.urlencode(
                dict(page=n, num=80, sort='symbol', asc=1, node='hs_a', symbol='', _s_r_a='page'))
            body, src = self.read(url)
            rows = json.loads(body)
            if not isinstance(rows, list):
                raise ValueError('股票分页不是数组')
            return rows, src
        rows, sources, errors = [], [source], []
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(page, n): n for n in range(1, math.ceil(expected / 80) + 1)}
            for future in concurrent.futures.as_completed(futures):
                try:
                    batch, src = future.result(); rows.extend(batch); sources.append(src)
                except Exception as exc:
                    errors.append(f'股票分页{futures[future]}失败：{type(exc).__name__}')
        dedup = {str(r.get('code', '')): r for r in rows}
        if len(dedup) != expected:
            errors.append(f'原始市场覆盖{len(dedup)}/{expected}，只作局部扫描')
        return sorted(dedup.values(), key=lambda r: r['code']), expected, sources, errors

    def quotes(self, codes):
        if not codes:
            return {}, []
        rows, sources = {}, []
        for offset in range(0, len(codes), 60):
            subset = codes[offset:offset+60]
            url = 'https://qt.gtimg.cn/q=' + ','.join(('sh' if c.startswith('6') else 'sz') + c for c in subset)
            body, src = self.read(url, 'gb18030'); sources.append(src)
            for line in body.split(';'):
                f = line.split('~')
                if len(f) > 45 and f[2] in subset:
                    try:
                        timestamp = dt.datetime.strptime(f[30], '%Y%m%d%H%M%S').replace(tzinfo=TZ)
                        rows[f[2]] = {'date': timestamp.date().isoformat(), 'timestamp': timestamp.isoformat(),
                                      'price': float(f[3]), 'float_cap_cny': float(f[44]) * 1e8,
                                      'source': dict(src, unit='流通市值人民币亿元转元')}
                    except ValueError:
                        continue
        return rows, sources

    def daily(self, code, day, index=False):
        sym = 'sh000300' if index else ('sh' if code.startswith('6') else 'sz') + code
        url = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?' + urllib.parse.urlencode({'param': sym + ',day,,,120,qfq'})
        body, src = self.read(url)
        node = json.loads(body).get('data', {}).get(sym, {})
        original = node.get('qfqday') or node.get('day') or []
        bars = [{'date': r[0], 'open': float(r[1]), 'close': float(r[2]), 'high': float(r[3]), 'low': float(r[4]),
                 'volume_shares': float(r[5]) * 100, 'amount_cny': None,
                 'limit_up': None, 'limit_verified': False} for r in original if len(r) >= 6 and r[0] <= day]
        return normalize_bars(bars, day), dict(src, adjustment='供应商前复权同一批次', volume_unit='手×100转股')

    def cross_daily(self, code, day):
        url = 'https://d.10jqka.com.cn/v4/line/hs_' + code + '/01/last.js'
        body, source = self.read(url)
        match = re.fullmatch(r'\s*[\w.]+\((.*)\)\s*;?\s*', body, re.S)
        if not match:
            raise ValueError('同花顺JSONP格式不符')
        value = json.loads(match.group(1))
        bars = []
        for text in value.get('data', '').split(';'):
            f = text.split(',')
            if len(f) < 7:
                continue
            d = dt.datetime.strptime(f[0], '%Y%m%d').date().isoformat()
            if d <= day:
                bars.append({'date': d, 'open': float(f[1]), 'high': float(f[2]), 'low': float(f[3]), 'close': float(f[4]),
                             'volume_shares': float(f[5]), 'amount_cny': float(f[6])})
        return normalize_bars(bars, day), dict(source, adjustment='provider_01需与主源交叉核验', volume_unit='股', amount_unit='元')


def collect(as_of, max_stocks=240, workers=6, raw_directory=None):
    """Market-wide metadata, explicitly bounded systematic stock sampling.

    A larger caller-selected cap may cover the entire pool. Sampling is a
    collection budget, not a strategy ranking or an alternative stock filter.
    Industry breadth and announcement evidence remain missing, never estimated
    from this sampled universe. Latest quote timestamps cannot date old bars.
    """
    day = signal_day(as_of)
    feed = PublicFeed(raw_directory=raw_directory)
    data = {'signal_date': day, 'stocks': [], 'benchmark': {}, 'market': {}, 'industries': {},
            'coverage': {}, 'sources': [], 'missing': []}
    rows, expected, sources, errors = feed.universe()
    data['sources'] += sources; data['missing'] += errors
    candidates = []
    count_main = count_risk = count_cap = 0
    for r in rows:
        if not str(r.get('code', '')).startswith(PREFIXES):
            continue
        count_main += 1
        name = str(r.get('name', ''))
        if 'ST' in name.upper() or '退' in name or name.upper().startswith(('N', 'C')):
            count_risk += 1; continue
        cap = finite(r.get('nmc'))
        # Sina metadata has time-of-day only, so this is a provisional budget
        # filter, followed by independently dated Tencent cap verification.
        if cap is not None and cap * 10000 > 50_000_000_000:
            count_cap += 1; continue
        candidates.append(r)
    max_stocks = max(1, min(int(max_stocks), 10000))
    selected = candidates if len(candidates) <= max_stocks else [candidates[i * len(candidates) // max_stocks] for i in range(max_stocks)]
    data['coverage'] = {'universe_expected': expected, 'universe_received': len(rows), 'mainboard_metadata': count_main,
                        'name_risk_proxy_excluded': count_risk, 'provisional_cap_above_limit': count_cap,
                        'history_target': len(candidates), 'history_requested': len(selected),
                        'sampling': '全池' if len(selected) == len(candidates) else '按代码排序等距抽样；数据预算，不是核心排名'}
    if len(selected) < len(candidates):
        data['missing'].append(f'日线复核预算{len(selected)}/{len(candidates)}，局部扫描，不称全市场最优')
    try:
        quotes, src = feed.quotes([r['code'] for r in selected]); data['sources'] += src
    except Exception as exc:
        quotes = {}; data['missing'].append('同日流通市值源失败：' + type(exc).__name__)
    def stock(r):
        code, q = r['code'], quotes.get(r['code'], {})
        s = {'code': code, 'name': r['name'], 'industry': None, 'bars': [], 'sources': [],
             'float_cap_cny': q.get('float_cap_cny'), 'cap_date': q.get('date'),
             'cap_verified': q.get('date') == day, 'history_verified': False,
             'adjustment_basis': 'tencent-qfq', 'missing': []}
        if q.get('source'):
            s['sources'].append(q['source'])
        try:
            bars, src = feed.daily(code, day); s['bars'] = bars; s['sources'].append(src)
        except Exception as exc:
            s['missing'].append('主日线失败：' + type(exc).__name__); return s
        try:
            other, src = feed.cross_daily(code, day); s['sources'].append(src)
            mapping = {b['date']: b for b in other}
            required = bars[-65:]
            verified = len(required) == 65 and required[-1]['date'] == day and all(
                b['date'] in mapping and all(abs(b[k] - mapping[b['date']][k]) <= .011 for k in ('open', 'high', 'low', 'close'))
                and abs(b['volume_shares'] - mapping[b['date']]['volume_shares']) <= max(100, b['volume_shares'] * .002)
                for b in required)
            s['history_verified'] = verified
            if verified:
                for b in bars:
                    if b['date'] in mapping:
                        b['amount_cny'] = mapping[b['date']]['amount_cny']
            else:
                s['missing'].append('65日日线双源价格/股数未完整一致；不混合复权量价')
        except Exception as exc:
            s['missing'].append('交叉日线失败：' + type(exc).__name__)
        return s
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(workers, 8))) as pool:
        data['stocks'] = list(pool.map(stock, selected))
    try:
        bars, src = feed.daily('000300', day, index=True)
        data['benchmark'] = {'code': '000300', 'name': '沪深300', 'bars': bars,
                             'verified': bool(len(bars) >= 65 and bars[-1]['date'] == day), 'source': src}
        data['sources'].append(src)
    except Exception as exc:
        data['missing'].append('沪深300日线失败：' + type(exc).__name__)
    data['coverage']['history_dual_verified'] = sum(s['history_verified'] for s in data['stocks'])
    data['coverage']['cap_dated_verified'] = sum(s['cap_verified'] for s in data['stocks'])
    data['missing'] += ['固定行业归属/完整广度与市场MA20广度待证据复核，不能由局部样本估计',
                         '历史实际涨停规则、证券公告风险、最近压力、净成本和业务催化尚未完成Codex复核']
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--as-of', default=None)
    parser.add_argument('--phase', choices=('prepare', 'intraday'), default='prepare')
    parser.add_argument('--input', type=Path)
    parser.add_argument('--enrichment', type=Path)
    parser.add_argument('--previous', type=Path)
    parser.add_argument('--output-dir', type=Path, required=True, help='私有任务目录；不直接发布工作台')
    parser.add_argument('--max-stocks', type=int, default=240)
    args = parser.parse_args()
    now = dt.datetime.fromisoformat(args.as_of) if args.as_of else dt.datetime.now(TZ)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    load = lambda p: json.loads(p.read_text(encoding='utf-8')) if p else None
    data = load(args.input)
    if data is None and args.phase != 'intraday':
        data = collect(now, args.max_stocks, raw_directory=args.output_dir / 'raw')
    if data is not None:
        (args.output_dir / 'input.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    result = research(now, phase=args.phase, input_data=data, enrichment=load(args.enrichment), previous=load(args.previous))
    (args.output_dir / 'report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: result[k] for k in ('status', 'as_of', 'summary', 'coverage')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
