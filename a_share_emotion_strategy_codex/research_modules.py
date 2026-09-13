"""Publish independent research snapshots without exposing a web execution API."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from zoneinfo import ZoneInfo

from research_store import research_path, read_json, atomic_json, update_lock

TZ = ZoneInfo('Asia/Shanghai')
FOLDERS = {'hot': 'hot_sectors', 'dragon': 'dragon', 'yichujifa': 'yichujifa', 'prelaunch': 'prelaunch'}
STATUSES = {'complete', 'partial', 'empty', 'unavailable'}


def stamp(value):
    try:
        if not isinstance(value, str) or not value:
            return None
        if len(value) == 10:
            value += 'T15:00:00+08:00'
        result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.replace(tzinfo=TZ) if result.tzinfo is None else result.astimezone(TZ)
    except (TypeError, ValueError):
        return None


def module_path(module, name='current.json'):
    if module not in FOLDERS or name not in ('current.json', 'attempt.json'):
        raise ValueError('Unknown research module or record')
    return research_path(FOLDERS[module] + '/' + name)


def content_hash(data):
    """Execution clocks are not research changes; source clocks and evidence are."""
    ignore = {'generated_at', 'computed_at', 'published_at', 'imported_at', 'attempted_at', 'run_id', 'run_started_at'}
    def clean(value):
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items() if key not in ignore}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value
    return hashlib.sha256(json.dumps(clean(data), ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def load_module(module, now=None):
    now = now or dt.datetime.now(TZ)
    data = read_json(module_path(module), {})
    attempt = read_json(module_path(module, 'attempt.json'), {})
    if not isinstance(data, dict) or data.get('module_id') != module:
        failed = isinstance(attempt, dict) and attempt.get('status') == 'unavailable'
        return {'data': {}, 'attempt': attempt, 'state': 'unavailable' if failed else 'not_run',
                'note': '本次研究执行失败，尚无可用的新结果。' if failed else '尚无新版筛选结果；旧版资料留在本地历史档案，不取得当前资格。'}
    source = stamp(data.get('as_of'))
    expiry = stamp(data.get('valid_until'))
    if source is None or source > now:
        state, note = 'unavailable', '行情时间缺失或晚于当前时点，结果不可用。'
    elif expiry is None or expiry < now:
        state, note = 'expired', '观察窗口已结束；以下只作历史研究，须重新核验。'
    elif data.get('status') == 'partial':
        state, note = 'partial', '已完成本次研究，但关键证据或覆盖不足。'
    elif data.get('status') == 'empty':
        state, note = 'empty', '本次有效覆盖内没有合格候选；不补位。'
    elif data.get('status') == 'complete':
        state, note = 'complete', '本次筛选已完成；观察资格与参与条件分开展示。'
    else:
        state, note = 'unavailable', '本次数据不可用；不能据此认定没有候选。'
    # A failed retry does not erase history or rejuvenate an older successful run.
    if isinstance(attempt, dict) and attempt.get('status') == 'unavailable':
        state = 'unavailable'
        note = '最近更新失败；保留此前研究并按原时点阅读。' + note
    return {'data': data, 'attempt': attempt, 'state': state, 'note': note}


def eligible_now(module, row, data, now=None):
    """A positive label is a claim with its own evidence lifetime, not a bool."""
    now = now or dt.datetime.now(TZ)
    if row.get('eligible') is not True:
        return False
    if module == 'prelaunch':
        # A re-run may have a newer module window, but cannot extend the original
        # frozen structure's five-session observation period.
        until = stamp(row.get('valid_until'))
        return until is not None and now <= until
    if module == 'hot':
        auction = row.get('auction') or {}
        at = stamp(auction.get('source_asof') or auction.get('time'))
        from market_calendar import is_trading_day
        return (auction.get('verified') is True and auction.get('qualified') is True and at is not None
                and at <= now and at.date() == now.date() and is_trading_day(at.date())[0]
                and dt.time(9,25) <= at.time().replace(tzinfo=None) < dt.time(9,26))
    source = stamp(row.get('live_as_of') or data.get('live_as_of'))
    until = stamp(row.get('live_valid_until') or data.get('live_valid_until'))
    if data.get('phase') != 'intraday' or source is None or until is None:
        return False
    from market_calendar import is_trading_day
    opening = dt.time(9, 50) if module == 'yichujifa' else dt.time(9, 30)
    clock = source.time().replace(tzinfo=None)
    in_session = opening <= clock <= dt.time(11,30) or dt.time(13) <= clock <= dt.time(15)
    current_clock = now.time().replace(tzinfo=None)
    current_session = opening <= current_clock <= dt.time(11,30) or dt.time(13) <= current_clock <= dt.time(15)
    return (is_trading_day(source.date())[0] and source.date() == now.date() and in_session and current_session
            and source <= now <= until and 0 <= (until-source).total_seconds() <= 90
            and (now-source).total_seconds() <= 90)


def publish(module, data, *, attempted_at=None):
    """One module transaction. Call the site builder only after releasing this lock."""
    attempted_at = attempted_at or dt.datetime.now(TZ)
    if not isinstance(data, dict) or data.get('module_id') != module:
        raise ValueError('Research module mismatch')
    status = data.get('status')
    if status not in STATUSES:
        raise ValueError('Research status is missing or invalid')
    if status != 'unavailable' and data.get('schema_version') != 1:
        raise ValueError('Unsupported research schema version')
    # Work on our own object; imported historical positive flags cannot become a
    # current signal without timing evidence. Keep their history separately.
    data = json.loads(json.dumps(data, allow_nan=False))
    for row in data.get('candidates', []):
        if isinstance(row, dict) and row.get('eligible') is True and not eligible_now(module, row, data, attempted_at):
            row['historical_eligible'] = True
            row['eligible'] = False
            row['status'] = '历史或未取得实时确认；当前仅观察'
    run_started = stamp(data.get('run_started_at')) or attempted_at
    if run_started > attempted_at:
        raise ValueError('Research run cannot start after publication')
    data['run_started_at'] = run_started.isoformat()
    # Serialize once before entering the lock, rejecting NaN and unsupported values.
    json.dumps(data, allow_nan=False)
    source = stamp(data.get('as_of'))
    if status != 'unavailable' and (source is None or source > attempted_at):
        raise ValueError('Research source time is missing or in the future')
    record = {'module_id': module, 'attempted_at': attempted_at.isoformat(), 'run_started_at': run_started.isoformat(), 'status': status,
              'as_of': data.get('as_of'), 'summary': data.get('summary', ''),
              'missing': data.get('missing', [])}
    changed = False
    with update_lock():
        current = read_json(module_path(module), {})
        old_attempt = read_json(module_path(module, 'attempt.json'), {})
        if not isinstance(current, dict): current = {}
        if not isinstance(old_attempt, dict): old_attempt = {}
        old_start = stamp(old_attempt.get('run_started_at') or current.get('run_started_at'))
        if old_start and run_started < old_start:
            return {'module': module, 'changed': False, 'status': 'superseded'}
        # A slower run must not overwrite a later completed market observation.
        previous_time = stamp(current.get('as_of')) if isinstance(current, dict) else None
        if status != 'unavailable' and previous_time and source < previous_time:
            return {'module': module, 'changed': False, 'status': 'superseded'}
        old_time = stamp(old_attempt.get('attempted_at')) if isinstance(old_attempt, dict) else None
        if old_time and attempted_at < old_time:
            return {'module': module, 'changed': False, 'status': 'superseded'}
        fingerprint = content_hash(data)
        if status != 'unavailable' and content_hash(current) != fingerprint:
            folder = module_path(module).parent / 'history'
            if current:
                atomic_json(folder / (content_hash(current) + '.json'), current)
            atomic_json(folder / (fingerprint + '.json'), data)
            atomic_json(module_path(module), data)
            changed = True
        # Repeated identical faults need not rebuild a page just to change its clock.
        comparable = lambda x: {k: v for k, v in x.items() if k not in ('attempted_at', 'run_started_at')}
        if comparable(old_attempt) != comparable(record):
            changed = True
        atomic_json(module_path(module, 'attempt.json'), record)
    return {'module': module, 'changed': changed, 'status': status}


def record_failure(module, reason, *, now=None, run_started_at=None):
    now = now or dt.datetime.now(TZ)
    # Exception internals and local paths stay in the private raw run, not the UI.
    return publish(module, {'module_id': module, 'status': 'unavailable',
                           'run_started_at': (run_started_at or now).isoformat(),
                           'summary': reason, 'missing': ['本次执行未产生可发布的有效结果。']},
                   attempted_at=now)
