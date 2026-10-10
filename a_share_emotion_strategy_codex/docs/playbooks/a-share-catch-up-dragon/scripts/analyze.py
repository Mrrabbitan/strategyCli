#!/usr/bin/env python3
"""Deterministic, offline daily-bar diagnostics; never emits trading eligibility.

Input: signal_date, adjustment='qfq', source, volume_unit ('shares' or
'lots_100'), trading_days, t0, bars; optionally leader_bars. Dates are ISO
YYYY-MM-DD and arrays must be strictly ascending. trading_days contains
verified session dates, or {date, status: 'trading'|'suspended'} entries.
The caller attests that signal_date is a completed session and the supplied
adjustment/calendar/source are correct; this helper only checks consistency.
Explicit suspension slots are never filled or replaced with earlier bars.
Optional leader_trading_days uses the same calendar contract. The leader must
have both the exact t0 and signal_date to permit a return-gap comparison.

Usage: python3 analyze.py INPUT.json   (or '-' to read JSON from stdin).
Percent returns use percentage points; volumes are normalized to shares.
MACD uses first-close EMA seeds, 12/26/9 and 2*(DIF-DEA). Its labels are
verified only after 130 valid completed bars; shorter histories are diagnostic.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import date
from pathlib import Path
from typing import Any


MACD_MIN_BARS = 130


def _date(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f'{name} must be an ISO date string')
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{name} must be a valid YYYY-MM-DD date') from exc
    if parsed.isoformat() != value:
        raise ValueError(f'{name} must use YYYY-MM-DD')
    return value


def _number(value: Any, name: str, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{name} must be numeric')
    try:
        result = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f'{name} must be finite') from exc
    if not math.isfinite(result):
        raise ValueError(f'{name} must be finite')
    if result < 0 or (positive and result == 0):
        raise ValueError(f'{name} must be {"positive" if positive else "nonnegative"}')
    return result


def _calendar(raw: Any, name: str) -> list[dict[str, str]]:
    if not isinstance(raw, list) or not raw:
        raise ValueError(f'{name} must be a nonempty verified session sequence')
    result = []
    previous = ''
    for index, item in enumerate(raw):
        if isinstance(item, str):
            day, status = _date(item, f'{name}[{index}]'), 'trading'
        elif isinstance(item, dict):
            day = _date(item.get('date'), f'{name}[{index}].date')
            status = item.get('status')
            if status not in ('trading', 'suspended'):
                raise ValueError(f'{name}[{index}].status must be trading or suspended')
        else:
            raise ValueError(f'{name}[{index}] must be a date or session object')
        if day <= previous:
            raise ValueError(f'{name} dates must be unique and strictly ascending')
        previous = day
        result.append({'date': day, 'status': status})
    return result


def _bars(raw: Any, name: str, multiplier: int) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError(f'{name} must be a list')
    result = []
    previous = ''
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f'{name}[{index}] must be a bar object')
        day = _date(item.get('date'), f'{name}[{index}].date')
        if day <= previous:
            raise ValueError(f'{name} dates must be unique and strictly ascending')
        previous = day
        bar = {'date': day}
        for field in ('open', 'high', 'low', 'close'):
            bar[field] = _number(item.get(field), f'{name}[{index}].{field}', True)
        if not (bar['low'] <= min(bar['open'], bar['close'])
                <= max(bar['open'], bar['close']) <= bar['high']):
            raise ValueError(f'{name}[{index}] has inconsistent OHLC')
        volume = _number(item.get('volume'), f'{name}[{index}].volume') * multiplier
        if not math.isfinite(volume):
            raise ValueError(f'{name}[{index}] normalized volume must be finite')
        bar['volume'] = volume
        if 'completed' in item and not isinstance(item['completed'], bool):
            raise ValueError(f'{name}[{index}].completed must be boolean')
        bar['completed'] = item.get('completed', True)
        if 'adjustment' in item and item['adjustment'] != 'qfq':
            raise ValueError(f'{name}[{index}] adjustment conflicts with qfq')
        result.append(bar)
    return result


def _check_calendar(bars: list[dict], calendar: list[dict], name: str) -> None:
    indexed = {bar['date']: bar for bar in bars}
    sessions = {entry['date']: entry['status'] for entry in calendar}
    missing = [day for day, status in sessions.items()
               if status == 'trading' and day not in indexed]
    if missing:
        raise ValueError(f'{name} missing verified trading dates: {", ".join(missing)}')
    outside = [day for day in indexed if day not in sessions]
    if outside:
        raise ValueError(f'{name} dates absent from trading_days: {", ".join(outside)}')
    suspended = [day for day in indexed if sessions[day] == 'suspended']
    if suspended:
        raise ValueError(f'{name} includes bars on suspended dates: {", ".join(suspended)}')


def _mean(values: list[float]) -> float:
    return sum(value / len(values) for value in values)


def _window(calendar: list[dict], indexed: dict, length: int,
            include_signal: bool) -> tuple[dict, list[dict]]:
    end = len(calendar) if include_signal else len(calendar) - 1
    slots = calendar[max(0, end - length):end]
    missing = [slot['date'] for slot in slots if slot['date'] not in indexed]
    available = len(slots) == length and not missing
    metadata = {
        'status': 'verified' if available else 'insufficient_window',
        'required_sessions': length,
        'available_sessions': len(slots),
        'start_date': slots[0]['date'] if slots else None,
        'end_date': slots[-1]['date'] if slots else None,
        'missing_dates': missing,
        'suspended_dates': [slot['date'] for slot in slots if slot['status'] == 'suspended'],
    }
    return metadata, [indexed[slot['date']] for slot in slots] if available else []


def _return(calendar: list[dict], indexed: dict, periods: int) -> dict:
    metadata, selected = _window(calendar, indexed, periods + 1, True)
    metadata['periods'] = periods
    metadata['value_pct'] = (selected[-1]['close'] / selected[0]['close'] - 1) * 100 if selected else None
    return metadata


def _macd(bars: list[dict]) -> dict:
    ema12 = ema26 = bars[0]['close']
    dea = 0.0
    histograms = []
    dif = 0.0
    for index, bar in enumerate(bars):
        if index:
            ema12 = (2 / 13) * bar['close'] + (11 / 13) * ema12
            ema26 = (2 / 27) * bar['close'] + (25 / 27) * ema26
        dif = ema12 - ema26
        dea = (2 / 10) * dif + (8 / 10) * dea
        histograms.append(2 * (dif - dea))
    last_three = histograms[-3:]
    increasing = (last_three[0] < last_three[1] < last_three[2]) if len(last_three) == 3 else None
    supportive = dif >= 0 and dif >= dea and histograms[-1] > 0
    verified = len(bars) >= MACD_MIN_BARS
    return {
        'status': 'verified' if verified else 'insufficient_history',
        'parameters': {'fast': 12, 'slow': 26, 'signal': 9, 'histogram_multiplier': 2},
        'ema_seed': 'first_close',
        'required_bars': MACD_MIN_BARS,
        'completed_bars': len(bars),
        'ema12': ema12, 'ema26': ema26, 'dif': dif, 'dea': dea,
        'histogram': histograms[-1],
        'last_three_histograms': last_three,
        'momentum_label': ('supportive' if supportive else 'unsupported') if verified else 'insufficient_history',
        'diagnostic_momentum_label': 'supportive' if supportive else 'unsupported',
        'three_histograms_strictly_increasing': increasing,
        'enhancement_evidence': bool(verified and supportive and increasing),
    }


def _t0_returns(t0: str, signal_date: str, indexed: dict, leader: dict | None) -> dict:
    candidate_available = t0 in indexed and signal_date in indexed
    leader_available = leader is not None and t0 in leader and signal_date in leader
    stock_return = ((indexed[signal_date]['close'] / indexed[t0]['close'] - 1) * 100
                    if candidate_available else None)
    leader_return = ((leader[signal_date]['close'] / leader[t0]['close'] - 1) * 100
                     if candidate_available and leader_available else None)
    if not candidate_available:
        status = 'candidate_t0_unavailable'
    elif leader is None:
        status = 'leader_not_supplied'
    elif not leader_available:
        status = 'incomparable_dates'
    else:
        status = 'comparable'
    return {
        'status': status, 't0': t0, 'end_date': signal_date,
        'verification_scope': 'same_date_endpoints_only',
        'candidate_return_pct': stock_return,
        'leader_return_pct': leader_return,
        'leader_minus_candidate_pct': leader_return - stock_return if leader_return is not None else None,
        'candidate_missing_dates': [day for day in dict.fromkeys((t0, signal_date)) if day not in indexed],
        'leader_missing_dates': ([day for day in dict.fromkeys((t0, signal_date)) if day not in leader]
                                 if leader is not None else []),
    }


def analyze(payload: dict[str, Any]) -> dict[str, Any]:
    """Return provenance and diagnostics, or raise ValueError on invalid input."""
    if not isinstance(payload, dict):
        raise ValueError('input must be a JSON object')
    try:
        canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'),
                               ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('all input must be finite JSON data') from exc
    signal_date = _date(payload.get('signal_date'), 'signal_date')
    t0 = _date(payload.get('t0'), 't0')
    if t0 > signal_date:
        raise ValueError('t0 must not be after signal_date')
    if payload.get('adjustment') != 'qfq':
        raise ValueError('adjustment must be qfq')
    source = payload.get('source')
    if not isinstance(source, str) or not source.strip():
        raise ValueError('source must be a nonempty provenance string')
    unit = payload.get('volume_unit')
    if unit not in ('shares', 'lots_100'):
        raise ValueError('volume_unit must be shares or lots_100')
    multiplier = 100 if unit == 'lots_100' else 1
    all_calendar = _calendar(payload.get('trading_days'), 'trading_days')
    calendar = [slot for slot in all_calendar if slot['date'] <= signal_date]
    if not calendar or calendar[-1]['date'] != signal_date:
        raise ValueError('signal_date missing from verified trading_days')
    if calendar[-1]['status'] != 'trading':
        raise ValueError('signal_date is suspended and has no completed daily bar')
    all_bars = _bars(payload.get('bars'), 'bars', multiplier)
    bars = [bar for bar in all_bars if bar['date'] <= signal_date]
    if any(not bar['completed'] for bar in bars):
        raise ValueError('bars through signal_date must all be completed')
    _check_calendar(bars, calendar, 'bars')
    indexed = {bar['date']: bar for bar in bars}
    if signal_date not in indexed:
        raise ValueError('signal_date missing from bars')

    leader = None
    all_leader_bars = []
    all_leader_calendar = []
    leader_suspended = []
    if 'leader_bars' in payload:
        if payload.get('leader_adjustment', 'qfq') != 'qfq':
            raise ValueError('leader_adjustment must match qfq')
        all_leader_bars = _bars(payload['leader_bars'], 'leader_bars', multiplier)
        leader_bars = [bar for bar in all_leader_bars if bar['date'] <= signal_date]
        if any(not bar['completed'] for bar in leader_bars):
            raise ValueError('leader_bars through signal_date must all be completed')
        if 'leader_trading_days' in payload:
            all_leader_calendar = _calendar(payload['leader_trading_days'], 'leader_trading_days')
            leader_calendar = [slot for slot in all_leader_calendar if slot['date'] <= signal_date]
            _check_calendar(leader_bars, leader_calendar, 'leader_bars')
            leader_suspended = [slot['date'] for slot in leader_calendar if slot['status'] == 'suspended']
        leader = {bar['date']: bar for bar in leader_bars}

    ma, ma_bars = _window(calendar, indexed, 20, True)
    ma['value'] = _mean([bar['close'] for bar in ma_bars]) if ma_bars else None
    volume, volume_bars = _window(calendar, indexed, 5, False)
    prior_mean = _mean([bar['volume'] for bar in volume_bars]) if volume_bars else None
    volume.update({'signal_volume_shares': indexed[signal_date]['volume'],
                   'previous_mean_volume_shares': prior_mean,
                   'value': indexed[signal_date]['volume'] / prior_mean if prior_mean else None})
    if prior_mean == 0:
        volume['status'] = 'undefined_zero_baseline'
    breakout, breakout_bars = _window(calendar, indexed, 10, False)
    line = max(bar['high'] for bar in breakout_bars) if breakout_bars else None
    breakout.update({'confirmation_line_10d': line,
                     'signal_close': indexed[signal_date]['close'],
                     'close_above_confirmation_line': indexed[signal_date]['close'] > line if line is not None else None})
    result = {
        'schema_version': 1,
        'input_fingerprint': 'sha256:' + hashlib.sha256(canonical.encode('utf-8')).hexdigest(),
        'signal_date': signal_date, 'source': source, 'adjustment': 'qfq',
        'volume_unit': 'shares', 'input_volume_unit': unit,
        'status': 'verified' if len(bars) >= MACD_MIN_BARS else 'insufficient_history',
        'verification_scope': 'supplied_input_consistency_only',
        'completion_basis': 'caller_attests_signal_date_is_completed',
        'calendar_basis': 'caller_supplied_verified_sessions',
        'completed_bars': len(bars),
        'suspended_dates': [slot['date'] for slot in calendar if slot['status'] == 'suspended'],
        'leader_suspended_dates': leader_suspended,
        'truncated_future': {
            'bars': [bar['date'] for bar in all_bars if bar['date'] > signal_date],
            'trading_days': [slot['date'] for slot in all_calendar if slot['date'] > signal_date],
            'leader_bars': [bar['date'] for bar in all_leader_bars if bar['date'] > signal_date],
            'leader_trading_days': [slot['date'] for slot in all_leader_calendar if slot['date'] > signal_date],
        },
        'metrics': {'return_5d': _return(calendar, indexed, 5),
                    'return_20d': _return(calendar, indexed, 20),
                    'ma20': ma, 'volume_ratio_previous5': volume,
                    'breakout': breakout},
        'macd': _macd(bars),
        't0_comparison': _t0_returns(t0, signal_date, indexed, leader),
    }
    result['all_numeric_windows_verified'] = all(
        metric['status'] == 'verified' for metric in result['metrics'].values())
    result['t0_comparison']['leader_calendar_checked'] = 'leader_trading_days' in payload
    try:
        json.dumps(result, allow_nan=False)
    except (ValueError, OverflowError) as exc:
        raise ValueError('numeric calculation exceeded finite output range') from exc
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f'nonfinite JSON constant {value} is not allowed')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', help='JSON input path, or - for stdin')
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.read() if args.input == '-' else Path(args.input).read_text(encoding='utf-8')
        payload = json.loads(raw, parse_constant=_reject_constant)
        result = analyze(payload)
    except (OSError, ValueError) as exc:
        print(json.dumps({'status': 'rejected', 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
