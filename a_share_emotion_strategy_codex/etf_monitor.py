"""Offline ETF evidence adapter and private, read-only dashboard publication.

This module never fetches quotes. Price labels describe completed daily evidence,
not orders or live eligibility. Returns/volatility use compatible adjusted closes;
drawdown60 is the (non-positive) largest peak-to-trough loss in 60 closes.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
from urllib.parse import parse_qsl, unquote, urlsplit
from zoneinfo import ZoneInfo

from research_store import atomic_json, read_json, research_path, update_lock

TZ = ZoneInfo('Asia/Shanghai')
NY = ZoneInfo('America/New_York')
METRICS = ('pct1', 'pct5', 'pct20', 'pct60', 'ma5', 'ma20', 'ma60',
           'ma20_5ago', 'vol20', 'drawdown60', 'rs20')
BUCKETS = frozenset(('trend', 'turn', 'repair', 'weak', 'unknown'))
PRIVATE = re.compile(r'/(?:Users|home|private|var|tmp)/|file://|(?:password|token|secret|api[_-]?key|credential)\s*[=:]', re.I)
ACCOUNT_TEXT = re.compile(r'账户|持仓|本金|生活费|成本价|总资产|可用资金|亏损|融资余额|account|portfolio|balance', re.I)
COMMON_GAPS = (
    '只覆盖预设ETF样本，不代表全市场最优；价格标签与产品适配、可参与条件分开。',
    '未取得两次盘中报价、同刻可比分钟量及VWAP，不生成确认买入信号。',
    'ETF份额变化、申赎、规模、实时价差与溢价尚未全量核验；成交活跃不等于资金净流入。',
    '行业成分广度、涨停率及五日可比扩散数据不完整，不据涨停数量推断真实资金净流向。',
    '美国政治、政策原文与利率、美元、VIX及盈利估值尚未完成本轮核验。',
    'ETF费用、跟踪差异、成分重合与集中度尚未完成同口径产品比较。',
    '中美交易日历、跨市场因果及60/120日领先联动尚未全量验证；美股强势不授予A股参与资格。',
    '价格动量与均线同源，未作为多份独立确认；初始阈值未经策略收益回测。',
)


def _now(value=None):
    value = value or dt.datetime.now(TZ)
    if not isinstance(value, dt.datetime):
        raise ValueError('A datetime is required')
    return value.replace(tzinfo=TZ) if value.tzinfo is None else value.astimezone(TZ)


def _stamp(value):
    try:
        result = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result.astimezone(TZ) if result.tzinfo is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _date(value):
    try:
        return dt.date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _text(value, limit=900):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ''
    value = str(value).replace('\n', ' ').replace('\r', ' ')[:limit]
    return '私有内容已省略' if PRIVATE.search(value) else value


def _url(value):
    if not isinstance(value, str) or re.search(r'[\s<>"\\]', value) or PRIVATE.search(unquote(value)):
        return ''
    try:
        parts = urlsplit(value)
        if (parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password
                or any(re.search(r'token|secret|password|api.?key|credential', key, re.I)
                       for key, _ in parse_qsl(parts.query))):
            return ''
        return value
    except ValueError:
        return ''


def _market_name(value):
    name = _text(value, 100)
    return '市场名称待核验' if ACCOUNT_TEXT.search(name) else name


def _load(path):
    if path is None:
        return {}
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        raise ValueError('Offline evidence document is unavailable') from None
    if not isinstance(value, dict):
        raise ValueError('Offline evidence must be an object')
    return value


def _code(value):
    value = re.sub(r'^(?:sh|sz)', '', str(value or '').lower())
    return value if re.fullmatch(r'\d{6}', value) else ''


def _unique(items):
    return list(dict.fromkeys(item for item in items if item))


def _round(value):
    return round(value, 6) if value is not None else None


def _metrics(bars):
    closes = [bar['close'] for bar in bars]
    def ret(n):
        return 100 * (closes[-1] / closes[-n - 1] - 1) if len(closes) > n else None
    def ma(n, lag=0):
        return statistics.mean(closes[-n-lag:len(closes)-lag or None]) if len(closes) >= n + lag else None
    volatility = None
    if len(closes) >= 21:
        returns = [math.log(b / a) for a, b in zip(closes[-21:-1], closes[-20:])]
        volatility = statistics.stdev(returns) * math.sqrt(252) * 100
    drawdown = None
    if len(closes) >= 60:
        peak, drawdown = closes[-60], 0.0
        for close in closes[-60:]:
            peak = max(peak, close)
            drawdown = min(drawdown, 100 * (close / peak - 1))
    return {key: _round(value) for key, value in dict(
        pct1=ret(1), pct5=ret(5), pct20=ret(20), pct60=ret(60), ma5=ma(5),
        ma20=ma(20), ma60=ma(60), ma20_5ago=ma(20, 5), vol20=volatility,
        drawdown60=drawdown, rs20=None).items()}


def _parse_bars(raw, expected, now, market):
    """Reject malformed calendars and exclude future/uncompleted daily observations."""
    bars, issues, previous = [], [], None
    if not isinstance(raw, list) or not raw:
        return [], ['缺少可重算的兼容复权日线原证据。']
    for row in raw[:10000]:
        if market == 'CN':
            if not isinstance(row, list) or len(row) < 6:
                issues.append('日线字段不完整。'); continue
            day, close, low = _date(row[0]), _number(row[2]), _number(row[4])
        else:
            if not isinstance(row, dict):
                issues.append('日线字段不完整。'); continue
            day, close = _date(row.get('date')), _number(row.get('adjusted_close'))
            raw_close, raw_low = _number(row.get('close')), _number(row.get('low'))
            low = raw_low * close / raw_close if raw_close and raw_low is not None and close is not None else None
        if day is None or close is None or close <= 0 or (low is not None and low <= 0):
            issues.append('日线日期或价格无效。'); continue
        if previous is not None and day <= previous:
            issues.append('日线日期重复或未按交易日升序。')
        previous = day
        if day > now.date() or (expected is not None and day > expected):
            issues.append('原证据含未来或晚于信号日的日线，已排除，不据此形成当前结论。'); continue
        if market == 'CN' and day == now.date() and now.time().replace(tzinfo=None) < dt.time(15):
            issues.append('当日日线尚未收盘，已排除，不升级趋势或拐点。'); continue
        bars.append({'date': day.isoformat(), 'close': close, 'low': low})
    if len(raw) > 10000:
        issues.append('原始日线超过离线导入上限。')
    # A corrupt calendar cannot become a plausible series merely by sorting it.
    if any('重复' in item for item in issues):
        bars = []
    if expected is None or not bars or bars[-1]['date'] != expected.isoformat():
        issues.append('日线末日与报告信号日错配或缺失，保留问题，不授予当前结论。')
    return bars, _unique(issues)


def _classify(bars, metrics, blocked=False):
    missing = []
    if len(bars) < 65:
        missing.append('不足65个完整兼容日线，趋势与拐点只能待验证；不足窗口的指标留空。')
    if blocked or len(bars) < 65:
        return '待验证 · 日期、日线或覆盖不足', 'unknown', missing
    closes = [bar['close'] for bar in bars]
    close, ma20, ma60, before = closes[-1], metrics['ma20'], metrics['ma60'], metrics['ma20_5ago']
    if close > ma20 > ma60 and ma20 > before:
        return '中期上升 · 连续趋势确认仍需后续完整交易日', 'trend', missing
    prior_downtrend = any(
        statistics.mean(closes[i-19:i+1]) < statistics.mean(closes[i-59:i+1])
        and statistics.mean(closes[i-19:i+1]) < statistics.mean(closes[i-24:i-4])
        for i in range(max(64, len(closes)-21), len(closes)-1))
    two_above = close > ma20 and closes[-2] > statistics.mean(closes[-21:-1])
    lows = [bar['low'] for bar in bars[-10:]]
    if all(low is not None for low in lows):
        higher_low = min(lows[-5:]) > min(lows[:5])
    else:
        higher_low = False
        missing.append('缺少兼容复权低点，低点抬升未确认。')
    if prior_downtrend and two_above and higher_low and ma20 >= before:
        return '初步拐点 · 广度及成交确认仍待核验', 'turn', missing
    if metrics['pct20'] < 0 and metrics['pct5'] > 0 and close > metrics['ma5']:
        return '止跌观察 · 尚未达到中期上升', 'repair', missing
    if close > ma20 and metrics['pct5'] > 0:
        return '价格修复待确认 · 低点结构或趋势持续性未齐', 'repair', missing
    return '仍弱或分化 · 等待结构改善', 'weak', missing


def _row(code, name, bars, source, issues, *, us=False):
    metrics = _metrics(bars)
    state, bucket, structural = _classify(bars, metrics, bool(issues))
    conditions = [
        '仅观察；须以新的完整交易日复核价格、MA20方向和原整理低点，盘中不升级未完成日线。',
        '参与前另核分钟承接、广度、份额及产品适配；现有日线不构成确认买入信号。',
    ]
    if us:
        conditions.append('美股绝对20日收益与相对SPY强度须同时为正，且MA20抬升；A股仍须自身确认。')
    missing = list(issues) + structural
    if not us:
        missing.append('未取得同口径沪深300历史，RS20留空，不以样本内部收益代替。')
    missing += ['分钟承接、ETF份额及成分广度尚未完整核验。']
    return {'code': code, 'name': _market_name(name),
            'as_of': bars[-1]['date'] if bars else None, 'close': bars[-1]['close'] if bars else None,
            'metrics': metrics, 'state': state, 'bucket': bucket,
            'bars': [{'date': bar['date'], 'close': bar['close']} for bar in bars],
            'conditions': conditions,
            'invalid_condition': '跌破事前记录整理低点或趋势证据转弱则撤销原观察；本轮未冻结支撑，不补造精确止损线。',
            'missing': _unique(missing), 'source': _url(source)}


def _intraday(value, now):
    """Whitelist observations without converting imported text into confirmation."""
    absent = {'status': 'not_available', 'observed_at': None,
              'note': '尚无本轮分钟观察；日线与美股收盘不能代替盘中承接。', 'rows': []}
    if not isinstance(value, dict) or not isinstance(value.get('rows'), list):
        return absent
    observed = _stamp(value.get('observed_at'))
    valid_observed = (observed is not None and observed.date() == now.date()
                      and 0 <= (now-observed).total_seconds() <= 90)
    rows, fresh = [], 0
    for item in value['rows'][:100]:
        if not isinstance(item, dict) or not _code(item.get('code')):
            continue
        at = _stamp(item.get('as_of'))
        valid = (at is not None and at.date() == now.date()
                 and 0 <= (now-at).total_seconds() <= 90 and valid_observed
                 and at <= observed)
        # Imported notes can contain account amounts. Only market observations,
        # never arbitrary personal plans, are copied into this served snapshot.
        conditions = ['仅作观察记录；未形成两次新行情及全部执行条件确认，不授予买入信号。']
        if not valid:
            conditions.append('时效不足、日期不符或未来时点，仅保留历史参考，不作为新行情。')
        else:
            fresh += 1
        rows.append({'code': _code(item.get('code')), 'name': _market_name(item.get('name')),
                     **{key: _number(item.get(key)) for key in ('price', 'pct', 'vwap', 'volume_ratio')},
                     'as_of': at.isoformat() if at else None, 'conditions': _unique(conditions)})
    if not rows:
        return absent
    note = '导入盘中观察，未生成确认买入信号。'
    if fresh != len(rows):
        note += ' 部分或全部记录时效不足，仅作历史参考；不能称为本轮新行情。'
    if not valid_observed:
        note += ' 观察取得时点缺失、过期、非当前上海日期或晚于当前时点。'
    return {'status': 'observation_only', 'observed_at': observed.isoformat() if observed else None,
            'note': note, 'rows': rows}


def import_snapshot(close_path, evidence_path=None, us_path=None, now=None):
    """Import supplied private files only. No network, account or raw-path output."""
    now = _now(now)
    close, evidence, us_data = _load(close_path), _load(evidence_path), _load(us_path)
    signal = _date(close.get('signal_date'))
    trend_data = close.get('etf_trends') if isinstance(close.get('etf_trends'), dict) else {}
    gaps, sources, cn, us = list(COMMON_GAPS), [], [], []
    generated = _stamp(close.get('computed_at'))
    retrieved_cn = _stamp(evidence.get('retrieved_at'))
    cn_close = dt.datetime.combine(signal, dt.time(15), tzinfo=TZ) if signal else None
    invalid_cn_clock = (signal is None or signal > now.date() or (generated is not None and generated > now)
                        or (retrieved_cn is not None and retrieved_cn > now)
                        or (retrieved_cn is not None and cn_close is not None and retrieved_cn < cn_close)
                        or (generated is not None and cn_close is not None and generated < cn_close)
                        or (generated is not None and retrieved_cn is not None and generated < retrieved_cn))
    if invalid_cn_clock:
        gaps.append('A股报告日期缺失、未来或取得/计算时点无效；相关记录仅保留问题。')
    if not retrieved_cn:
        gaps.append('A股原证据取得时刻缺失。')
    if not generated:
        gaps.append('原A股汇总计算时刻缺失；本页生成时刻只表示离线重算。')
    if signal and signal < now.date():
        gaps.append('所示为最近导入的已完成日线，不能当作今日盘中报价；须按后续实际交易日复核。')
    raw_cn = {}
    def register(code, raw, source, basis, retrieved, issues=()):
        record = {'raw': raw, 'source': source, 'basis': basis,
                  'retrieved_at': retrieved, 'issues': list(issues)}
        previous = raw_cn.get(code)
        if previous is None or (not previous['raw'] and raw):
            raw_cn[code] = record
        elif previous['raw'] and raw:
            raw_cn[code] = dict(record, raw=None, issues=['同代码兼容日线原证据重复，来源冲突待核验。'])
    for item in evidence.get('sources', []) if isinstance(evidence.get('sources'), list) else []:
        if not isinstance(item, dict):
            continue
        payload = item.get('payload')
        data = payload.get('data', {}) if isinstance(payload, dict) else {}
        source = _url(item.get('source'))
        source_retrieved = _stamp(item.get('retrieved_at')) or retrieved_cn
        if item.get('format') == 'eastmoney_qfq':
            code = _code(data.get('code') if isinstance(data, dict) else None) or _code(item.get('code'))
            if not code:
                gaps.append('东财前复权来源未给有效证券代码，未导入。'); continue
            issues = []
            if _code(item.get('code')) and _code(item.get('code')) != code:
                issues.append('东财来源证券代码与原响应错配。')
            if (not source or urlsplit(source).hostname not in ('push2his.eastmoney.com', 'push2.eastmoney.com')
                    or [value for key, value in parse_qsl(urlsplit(source).query) if key == 'fqt'] != ['1']):
                issues.append('东财来源未明确fqt=1，不能当作前复权证据。')
            klines = data.get('klines') if isinstance(data, dict) else None
            raw = [line.split(',') if isinstance(line, str) else line for line in klines] if isinstance(klines, list) else None
            register(code, raw, source, '东方财富历史日线 fqt=1 前复权；与原报告收盘交叉核验，未把腾讯day冒称qfqday。',
                     source_retrieved, issues)
            continue
        for symbol, values in data.items() if isinstance(data, dict) else []:
            code = _code(symbol)
            if code and isinstance(values, dict):
                register(code, values.get('qfqday'), source, '腾讯qfqday前复权收盘；非原生day替代。', source_retrieved)
    rows = trend_data.get('results', [])
    seen = set()
    for item in rows if isinstance(rows, list) else []:
        if not isinstance(item, dict):
            continue
        code = _code(item.get('code'))
        if not code or code in seen:
            gaps.append('A股样本含无效或重复代码，未重复计入覆盖。'); continue
        seen.add(code)
        origin = raw_cn.get(code, {})
        source, source_retrieved = origin.get('source', ''), origin.get('retrieved_at', retrieved_cn)
        bars, issues = _parse_bars(origin.get('raw'), signal, now, 'CN')
        issues += origin.get('issues', [])
        if source_retrieved is not None and (source_retrieved > now or (cn_close and source_retrieved < cn_close)):
            issues.append('原日线来源取得时点早于完整收盘或晚于当前时点。')
        if invalid_cn_clock:
            issues.append('报告或取得时点无效，不形成当前趋势结论。')
        if _date(item.get('as_of')) != signal or _date(trend_data.get('as_of')) != signal:
            issues.append('ETF汇总日期与报告信号日错配。')
        stated = _number(item.get('close'))
        if stated is not None and bars and abs(stated-bars[-1]['close']) > 0.000001:
            issues.append('汇总收盘与原日线冲突，不选择其一冒充一致。')
        row = _row(code, item.get('name', code), bars, source or item.get('source'), issues)
        cn.append(row)
        sources.append({'label': 'ETF前复权日线 · ' + code, 'url': row['source'],
                        'as_of': row['as_of'], 'retrieved_at': source_retrieved.isoformat() if source_retrieved else None,
                        'basis': origin.get('basis', '兼容复权原证据缺失。') + '收益按N交易日前收盘；20日日对数收益样本波动年化sqrt(252)，60收盘窗口最大回撤为非正百分比。'})
    if not cn:
        gaps.append('未导入A股ETF样本，不能据此判断全市场没有机会。')
    us_signal = _date(us_data.get('as_of'))
    beijing_close = _stamp(us_data.get('session_close_beijing'))
    declared_close = _stamp(us_data.get('session_close_new_york'))
    us_close = beijing_close or declared_close
    ny_close = us_close.astimezone(NY) if us_close else None
    calendar = us_data.get('session_calendar')
    calendar = calendar if isinstance(calendar, dict) else {}
    calendar_end = _stamp(calendar.get('regular_session_end_new_york'))
    calendar_url = _url(calendar.get('source'))
    calendar_host = urlsplit(calendar_url).hostname if calendar_url else None
    early_close_verified = (calendar.get('verified') is True and _date(calendar.get('date')) == us_signal
                            and calendar_end is not None and calendar_end == us_close
                            and calendar_host in ('www.nyse.com', 'nyse.com', 'www.nasdaq.com', 'nasdaq.com'))
    close_clock_valid = (ny_close is not None
                         and (ny_close.time().replace(tzinfo=None) == dt.time(16)
                              or (ny_close.time().replace(tzinfo=None) == dt.time(13) and early_close_verified)))
    retrieved_us = _stamp(us_data.get('retrieved_at_utc') or us_data.get('retrieved_at'))
    invalid_us_clock = (us_signal is None or us_signal > now.date() or us_close is None or us_close > now
                        or not close_clock_valid
                        or (us_close is not None and us_close.astimezone(NY).date() != us_signal)
                        or (beijing_close is not None and declared_close is not None and beijing_close != declared_close)
                        or (bool(us_data.get('session_close_new_york')) and declared_close is None)
                        or (retrieved_us is not None and (retrieved_us > now or (us_close is not None and retrieved_us < us_close))))
    if us_data and not retrieved_us:
        gaps.append('美国原证据取得时刻缺失，需补核验；页面生成时间不代替行情时间。')
    if us_data and not close_clock_valid:
        gaps.append('美东常规收盘时钟未通过：正常16:00，13:00早收须匹配交易所日历原文证据；不把早盘时点当完整日线。')
    if early_close_verified:
        sources.append({'label': '美国交易所早收日历证据', 'url': calendar_url,
                        'as_of': us_signal.isoformat() if us_signal else None,
                        'retrieved_at': retrieved_us.isoformat() if retrieved_us else None,
                        'basis': '本轮私有证据声明已核验的实际早收日与regular session end；不硬编码假日。'})
    us_items = us_data.get('etfs', [])
    for item in us_items if isinstance(us_items, list) else []:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get('symbol', '')).upper()
        if not re.fullmatch(r'[A-Z][A-Z0-9.-]{0,11}', symbol) or any(row['code'] == symbol for row in us):
            gaps.append('美股样本含无效或重复代码，未重复计入覆盖。'); continue
        bars, issues = _parse_bars(item.get('bars'), us_signal, now, 'US')
        if invalid_us_clock:
            issues.append('美股交易日、美东日期、收盘声明或取得时点不一致，不作已完成收盘结论。')
        if _date(item.get('as_of')) != us_signal or item.get('status') != 'verified':
            issues.append('美股样本日期错配或原证据未通过核验。')
        validation = item.get('validation') if isinstance(item.get('validation'), dict) else {}
        regular_close = _stamp(validation.get('regular_market_time_new_york'))
        if (validation.get('regular_market_quote_at_close') is False
                or (regular_close is not None and (us_close is None
                    or regular_close.astimezone(NY).date() != us_signal
                    or abs((regular_close-us_close).total_seconds()) > 2))):
            issues.append('美股样本常规收盘声明与全池收盘时点不一致。')
        source = item.get('source') if isinstance(item.get('source'), dict) else {}
        row = _row(symbol, item.get('label_zh') or item.get('long_name') or symbol,
                   bars, source.get('url'), issues, us=True)
        us.append(row)
        sources.append({'label': 'Yahoo兼容复权日线 · ' + symbol, 'url': row['source'],
                        'as_of': row['as_of'], 'retrieved_at': retrieved_us.isoformat() if retrieved_us else None,
                        'basis': '美元adjusted_close重算；RS20为ETF20日收益减SPY20日收益，单位百分点；非收益比值。'})
    spy = next((row for row in us if row['code'] == 'SPY' and row['bucket'] != 'unknown'), None)
    for row in us:
        if (spy is not None and row['bucket'] != 'unknown' and len(row['bars']) >= 21
                and [bar['date'] for bar in row['bars'][-21:]] == [bar['date'] for bar in spy['bars'][-21:]]):
            row['metrics']['rs20'] = _round(row['metrics']['pct20'] - spy['metrics']['pct20'])
        else:
            row['missing'].append('缺少同一21个交易日SPY可比序列，RS20留空。')
        if row['metrics']['rs20'] is not None and row['metrics']['rs20'] > 0 and (row['metrics']['pct20'] or 0) <= 0:
            row['missing'].append('仅相对抗跌，绝对20日收益非正，不称上涨主线。')
    if not us:
        gaps.append('美国观察池尚未导入已完成收盘数据，跨市场强弱待验证。')
    elif invalid_us_clock:
        gaps.append('美国交易日或实际收盘时钟未通过核验，相关形态降级待验证。')
    trends = close.get('industry_trend_reference')
    trends = trends if isinstance(trends, dict) else {}
    mapping = {'种植业': '种植业与林业', '农产品加': '农产品加工', '铁路公路': '公路铁路运输'}
    industry_trends = {item.get('name'): item for item in trends.get('rows', []) if isinstance(item, dict)} if isinstance(trends.get('rows', []), list) else {}
    ranking = close.get('industry_ranking')
    ranking = ranking if isinstance(ranking, dict) else {}
    heat = []
    heat_valid = _date(ranking.get('signal_date')) == signal and not invalid_cn_clock
    heat_source = _url(ranking.get('source'))
    if not heat_source and signal is not None and 'eastmoney' in str(ranking.get('classification', '')).lower():
        heat_source = 'https://push2ex.eastmoney.com/getTopicZTPool?date=' + signal.strftime('%Y%m%d')
    directions = {'up': '上升', 'down': '下降', 'flat': '走平',
                  '上升': '上升', '下降': '下降', '走平': '走平'}
    for item in ranking.get('ranking', []) if isinstance(ranking.get('ranking'), list) else []:
        if not isinstance(item, dict) or not isinstance(item.get('industry'), str):
            continue
        count, amount = _number(item.get('count')), _number(item.get('amount_yuan'))
        if count is None or count < 0 or int(count) != count:
            gaps.append('热度榜存在无效数量，相关行业未展示。'); continue
        name = _market_name(item['industry'])
        trend = industry_trends.get(item['industry']) or industry_trends.get(mapping.get(item['industry']))
        trend_result = None
        if isinstance(trend, dict) and _date(trends.get('as_of')) == signal and heat_valid:
            trend_result = {'pct5': _number(trend.get('pct5')), 'pct20': _number(trend.get('pct20')),
                            'ma20_direction': directions.get(trend.get('ma20_direction'), ''),
                            'close_vs_ma20': trend.get('close_vs_ma20') if trend.get('close_vs_ma20') in ('上方', '略上方', '下方', '略下方', '相等') else '', 'source': _url(trend.get('source'))}
        stocks = [{'code': _code(stock.get('code')), 'name': _market_name(stock.get('name'))}
                  for stock in item.get('stocks', []) if isinstance(stock, dict) and _code(stock.get('code'))] if isinstance(item.get('stocks'), list) else []
        heat.append({'industry': name, 'count': int(count), 'amount_yuan': amount,
                     'stocks': stocks, 'trend': trend_result,
                     'source': heat_source})
    if heat and not heat_valid:
        gaps.append('涨停行业榜日期与报告错配或未来，已撤下热度榜；原始计数仅保留于私有输入档案。')
        heat = []
    if heat:
        gaps.append('涨停行业为东财分类，走势指数采用同花顺，成分不完全一致；农业等近似映射只作参考。')
        heat_retrieved = _stamp(ranking.get('retrieved_at'))
        if heat_retrieved is None:
            gaps.append('涨停原池取得时刻未提供；汇总计算时刻不冒充原始取得时刻。')
        sources.append({'label': '涨停行业与走势对照', 'url': heat_source, 'as_of': signal.isoformat() if signal else None,
                        'retrieved_at': heat_retrieved.isoformat() if heat_retrieved else None,
                        'basis': '东财hybk唯一行业分类，覆盖原全涨跌幅层级涨停池，不是主板可交易池；原始涨停数顺序不重排；金额为榜内股票成交额合计，不是资金净流入。'})
    selection = close.get('selection') if isinstance(close.get('selection'), dict) else {}
    valid_codes = {row['code'] for row in cn if row['bucket'] != 'unknown'}
    priority = _code(selection.get('priority_observation'))
    priority = priority if priority in valid_codes else None
    alternatives = [_code(code) for code in selection.get('hot_theme_observations', [])] if isinstance(selection.get('hot_theme_observations'), list) else []
    alternatives = _unique(code for code in alternatives if code in valid_codes and code != priority)[:2]
    phase = close.get('phase') if isinstance(close.get('phase'), str) and re.fullmatch(r'[a-z][a-z0-9_-]{0,59}', close['phase']) else 'offline_import'
    backfill = ('backfill' in phase or close.get('not_a_historical_intraday_signal') is True
                or (signal is not None and _date(close.get('prepared_on')) is not None and _date(close['prepared_on']) > signal))
    if backfill:
        gaps.append('盘后回补：计算及取得发生在信号日之后，不是当时盘前或盘中已经发出的信号。')
    return {'schema_version': 1, 'as_of_cn': signal.isoformat() if signal else None,
            'as_of_us': us_signal.isoformat() if us_signal else None,
            'generated_at': now.isoformat(timespec='seconds'), 'published_at': None,
            'phase': phase, 'status': 'partial', 'backfill': backfill,
            'coverage': {'cn': len(cn), 'us': len(us), 'cn_scope': f'{len(cn)}只预设ETF样本，非全市场；美股含基准与子行业代理。'},
            'cn': cn, 'us': us, 'heat': heat,
            'focus': {'priority_code': priority, 'alternatives': alternatives,
                      'basis': '仅作已覆盖样本的原报告观察顺序，未完成全产品适配比较，不是买入指令或确认买入信号。'},
            'gaps': _unique(gaps), 'events': [], 'sources': sources,
            'intraday': _intraday(close.get('intraday'), now)}


def _validate_report(report):
    allowed = {'schema_version', 'as_of_cn', 'as_of_us', 'generated_at', 'published_at', 'phase', 'status', 'backfill', 'coverage', 'cn', 'us', 'heat', 'focus', 'gaps', 'events', 'sources', 'intraday'}
    if not isinstance(report, dict) or set(report) != allowed or report.get('schema_version') != 1 or report.get('status') != 'partial':
        raise ValueError('Invalid ETF monitor snapshot')
    if not _stamp(report.get('generated_at')) or report.get('events') != []:
        raise ValueError('Unsupported ETF monitor clocks or events')
    intraday = report.get('intraday')
    intraday_fields = {'code', 'name', 'price', 'pct', 'vwap', 'volume_ratio', 'as_of', 'conditions'}
    if (not isinstance(intraday, dict) or set(intraday) != {'status', 'observed_at', 'note', 'rows'}
            or intraday.get('status') not in ('not_available', 'observation_only')
            or not isinstance(intraday.get('rows'), list)
            or any(not isinstance(row, dict) or set(row) != intraday_fields
                   or any(row[key] is not None and _number(row[key]) is None for key in ('price', 'pct', 'vwap', 'volume_ratio'))
                   for row in intraday['rows'])):
        raise ValueError('Invalid intraday observation evidence')
    row_keys = {'code', 'name', 'as_of', 'close', 'metrics', 'state', 'bucket', 'bars', 'conditions', 'invalid_condition', 'missing', 'source'}
    for market in ('cn', 'us'):
        if not isinstance(report[market], list):
            raise ValueError('Invalid ETF monitor rows')
        for row in report[market]:
            if (not isinstance(row, dict) or set(row) != row_keys or row.get('bucket') not in BUCKETS
                    or not isinstance(row.get('metrics'), dict) or set(row['metrics']) != set(METRICS)
                    or any(value is not None and _number(value) is None for value in row['metrics'].values())
                    or not isinstance(row.get('bars'), list)
                    or any(not isinstance(bar, dict) or set(bar) != {'date', 'close'} for bar in row['bars'])):
                raise ValueError('Invalid ETF monitor evidence')
    encoded = json.dumps(report, ensure_ascii=False, allow_nan=False)
    if PRIVATE.search(encoded) or any(key in encoded for key in ('"payload"', '"raw_path"', '"portfolio_snapshot"', '"account_total_cny"')):
        raise ValueError('Private or noncanonical ETF monitor data')


def _fingerprint(report):
    content = {key: value for key, value in report.items() if key not in ('generated_at', 'published_at')}
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def publish_snapshot(report):
    """Atomically archive canonical evidence by date; return the stored snapshot."""
    _validate_report(report)
    stored = copy.deepcopy(report)
    folder = research_path('etf_monitor')
    digest = _fingerprint(stored)
    with update_lock():
        current = read_json(folder / 'current.json', {})
        if isinstance(current, dict) and current:
            if _fingerprint(current) == digest:
                return current
            old_day, new_day = _date(current.get('as_of_cn')), _date(stored.get('as_of_cn'))
            if (old_day and new_day and new_day < old_day) or (_stamp(current.get('generated_at')) and _stamp(stored['generated_at']) < _stamp(current['generated_at'])):
                raise ValueError('A newer ETF monitor snapshot is already published')
        stored['published_at'] = _now().isoformat(timespec='seconds')
        day = _date(stored.get('as_of_cn')) or _date(stored.get('as_of_us')) or _stamp(stored['generated_at']).date()
        atomic_json(folder / 'archive' / day.isoformat() / (digest + '.json'), stored)
        atomic_json(folder / 'current.json', stored)
    return stored


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    load = sub.add_parser('import', help='Import supplied offline evidence and publish the private view')
    load.add_argument('--close', required=True, type=Path)
    load.add_argument('--cn-evidence', type=Path)
    load.add_argument('--us', type=Path)
    load.add_argument('--build', action='store_true')
    args = parser.parse_args(argv)
    report = publish_snapshot(import_snapshot(args.close, args.cn_evidence, args.us))
    if args.build:
        from build_investment_site import build
        build()
    print(json.dumps({'status': report['status'], 'as_of_cn': report['as_of_cn'],
                      'as_of_us': report['as_of_us'], 'coverage': report['coverage'],
                      'backfill': report['backfill']}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
