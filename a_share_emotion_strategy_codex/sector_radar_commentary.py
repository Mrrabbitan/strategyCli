"""Compact stock commentary from saved daily bars, without changing eligibility.

This module describes prices and traded volume only. It does not fetch quotes,
identify money flows, invent intraday confirmations, or move frozen references.
"""
from __future__ import annotations

import datetime as dt
import math


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _day(value):
    try:
        return dt.date.fromisoformat(value).isoformat() if isinstance(value, str) and len(value) == 10 else None
    except ValueError:
        return None


def _price(value):
    return f'{value:.4f}'.rstrip('0').rstrip('.')


def _pct(value, signed=False):
    return f'{value:+.2f}%' if signed else f'{value:.2f}%'


def _blocked(reason):
    return {'verdict': '日线证据不足，暂不判断强弱。', 'price_volume': '',
            'watch': '', 'risk': '', 'limitation': reason, 'metrics': {}}


def _same_bar(left, right):
    # Supplier names may differ; conflicting numerical observations may not.
    keys = ('open', 'high', 'low', 'close', 'volume_shares')
    return all(left.get(key) == right.get(key) for key in keys)


def _validated_bars(bars, signal):
    dated = {}
    for bar in bars if isinstance(bars, list) else []:
        if not isinstance(bar, dict):
            continue
        day = _day(bar.get('date'))
        if not day or day > signal:
            continue
        if day in dated and not _same_bar(dated[day], bar):
            return [], '同日行情记录冲突，不能拼成确定结论。'
        dated[day] = bar
    selected = [dated[day] for day in sorted(dated)]
    if not selected or selected[-1]['date'] != signal:
        return [], '缺少所选交易日的收盘日线。'
    if len(selected) < 2:
        return [], '缺少前一日完整日线，无法比较涨跌与量能。'
    for bar in selected[-5:]:
        values = [_number(bar.get(key)) for key in ('open', 'high', 'low', 'close')]
        if any(value is None or value <= 0 for value in values):
            return [], '日线价格缺失或无效。'
        opening, high, low, close = values
        if low > high or not (low <= opening <= high and low <= close <= high):
            return [], '日线高低价与开收盘价冲突。'
    return selected, ''


def _levels(row):
    values, conflict = {}, False
    allowed = {'冻结支撑': 'support', '冻结上沿': 'upper', '最近压力': 'pressure'}
    for level in row.get('levels', []) if isinstance(row.get('levels'), list) else []:
        if not isinstance(level, dict) or level.get('label') not in allowed:
            continue
        value = _number(level.get('value'))
        if value is None or value <= 0:
            continue
        key = allowed[level['label']]
        if key in values and values[key] != value:
            conflict = True
        values[key] = value
    return ({}, True) if conflict else (values, False)


def _known_failure(row):
    reasons = row.get('failure_reasons')
    reason = ' '.join(item for item in reasons if isinstance(item, str)) if isinstance(reasons, list) else ''
    findings = []
    if '承接' in reason:
        findings.append('原缩量承接条件未满足')
    if '空间' in reason:
        findings.append('原空间条件未满足')
    if '趋势' in reason:
        findings.append('原趋势条件未满足')
    return '；'.join(findings) + '。' if findings else ''


def describe_stock(row, bars, signal_date):
    """Return concise Chinese ``verdict/price_volume/watch/risk/limitation``.

    ``bars`` contains raw saved ``date/open/high/low/close/volume_shares``.
    Future bars are excluded before comparison; no current quotation is fetched.
    ``row.price`` must match the signal close when supplied. ``levels`` retains
    its explicit frozen-support / frozen-upper / nearest-pressure semantics.
    No return value establishes a trading or original-strategy qualification.
    """
    row = row if isinstance(row, dict) else {}
    signal = _day(signal_date)
    if signal is None:
        return _blocked('收盘日期无效。')
    selected, problem = _validated_bars(bars, signal)
    if problem:
        return _blocked(problem)
    last, previous = selected[-1], selected[-2]
    close, previous_close = float(last['close']), float(previous['close'])
    price = _number(row.get('price'))
    if row.get('price') is not None and (price is None or abs(price - close) > 1e-8):
        return _blocked('列表价格与该日收盘价冲突。')
    daily_return = (close / previous_close - 1) * 100
    volume, previous_volume = _number(last.get('volume_shares')), _number(previous.get('volume_shares'))
    ratio = None
    limitations = []
    if volume is None or previous_volume is None:
        limitations.append('成交股数缺失，未判断放缩量。')
    elif volume < 0 or previous_volume < 0:
        limitations.append('成交股数无效，未判断放缩量。')
    elif volume == 0 or previous_volume == 0:
        limitations.append('成交股数含零值，未判断放缩量。')
    else:
        ratio = volume / previous_volume
    high, low = float(last['high']), float(last['low'])
    from_high = (high / close - 1) * 100
    # This is geometric position, not the unobserved sequence of intraday trades.
    high_gap = (high - close) / high * 100
    parts = [f'收盘{_price(close)}，较前日{_pct(daily_return, True)}']
    if ratio is not None:
        parts.append(f'成交股数为前日{ratio:.2f}倍')
    if high == low:
        parts.append('当日最高、最低与收盘同价')
    elif close == high:
        parts.append('收在当日最高价')
    elif close == low:
        parts.append('收在当日最低价')
    else:
        parts.append(f'日内最高{_price(high)}，收盘较其低{_pct(high_gap)}')
    if daily_return > 0:
        verdict = ('放量上涨，价格与成交量同向增加。' if ratio is not None and ratio > 1
                   else '缩量上涨，价格走高但量能未同步增加。' if ratio is not None and ratio < 1
                   else '收盘上涨，继续看上方关键位置。')
    elif daily_return < 0:
        verdict = ('放量回落，短线表现偏弱。' if ratio is not None and ratio > 1
                   else '缩量回落，重点看下方支撑。' if ratio is not None and ratio < 1
                   else '收盘回落，重点看下方支撑。')
    else:
        verdict = ('收盘持平，成交量增加。' if ratio is not None and ratio > 1
                   else '收盘持平，成交量减少。' if ratio is not None and ratio < 1
                   else '收盘持平，尚未拉开价格空间。')

    levels, level_conflict = _levels(row)
    if level_conflict:
        limitations.append('关键价位记录冲突，仅解读量价。')
    support, upper, pressure = (levels.get(key) for key in ('support', 'upper', 'pressure'))
    resistance, label = (pressure, '最近压力') if pressure is not None else (upper, '原上沿')
    recent = selected[-5:]
    recent_high = max(float(bar['high']) for bar in recent)
    recent_low = min(float(bar['low']) for bar in recent)
    watch, risk = '', ''
    if resistance is not None:
        if close > resistance:
            watch = f'下一交易日看能否守住已超过的{label}{_price(resistance)}。'
        elif close == resistance:
            watch = f'收盘正处{label}{_price(resistance)}，下一交易日看能否站稳。'
        else:
            watch = f'下一交易日先看{label}{_price(resistance)}能否站稳。'
        if pressure is None and upper is not None and recent_high > close:
            if close < recent_high < upper:
                watch = f'先看近期高点{_price(recent_high)}，再看原上沿{_price(upper)}；高点仅作行情参考。'
            elif close <= upper < recent_high:
                watch = f'先看原上沿{_price(upper)}能否站稳，再看近期高点{_price(recent_high)}（行情参考）。'
    else:
        watch = f'近{len(recent)}个已存交易日高低价{_price(recent_high)}／{_price(recent_low)}，仅作参照。'
    if support is not None:
        if close < support:
            verdict = f'收盘已低于冻结支撑{_price(support)}，原结构承压。'
            risk = '不能继续沿用原支撑仍有效的判断，也不下移该参考价。'
        elif close == support:
            risk = f'收盘正处冻结支撑{_price(support)}，下一交易日重点看能否守住。'
        else:
            downside = (close - support) / close * 100
            risk = f'下方冻结支撑{_price(support)}，距收盘{_pct(downside)}。'
            if resistance is not None and resistance >= close:
                upside = (resistance - close) / close * 100
                risk = f'上至{label}{_price(resistance)}（{_pct(upside)}），下至冻结支撑{_price(support)}（{_pct(downside)}）。'
                if upside < downside:
                    risk += '向上结构距离较小。'
    if upper is not None and high > upper and close < upper and not (support is not None and close < support):
        verdict = f'盘中超过原上沿{_price(upper)}，收盘未站稳。'
    if not risk:
        risk = ('未给出有效冻结支撑，不能据此判断风险距离。'
                if support is None else '继续沿用原冻结结构，不因当日涨跌移动参考价。')
    risk += _known_failure(row)
    metrics = {'signal_date': signal, 'close': close, 'daily_return_pct': daily_return,
               'volume_ratio': ratio, 'close_below_high_pct': high_gap,
               'distance_to_high_pct': from_high,
               'support': support, 'frozen_upper': upper, 'nearest_pressure': pressure}
    return {'verdict': verdict, 'price_volume': '；'.join(parts) + '。',
            'watch': watch, 'risk': risk, 'limitation': limitations[0] if limitations else '', 'metrics': metrics}
