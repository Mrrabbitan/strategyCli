"""Read-only, field-allowlisted view of the existing local monitoring service."""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
from research_store import read_json

TZ = ZoneInfo('Asia/Shanghai')


def monitor_root() -> Path:
    override = os.environ.get('AUTOSTRATEGY_MONITOR_ROOT')
    return Path(override).expanduser() if override else Path.home() / 'Library/Application Support/AutoStrategy/game-monitor'


def stamp(value):
    try:
        t = dt.datetime.fromtimestamp(value, TZ) if isinstance(value, (int, float)) else dt.datetime.fromisoformat(str(value))
        return t.replace(tzinfo=TZ) if t.tzinfo is None else t.astimezone(TZ)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def safe(value):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        if any(x in value for x in ('/Users/', 'file://', 'Bearer ', 'ntfy_topic', 'ntfy_token')):
            return None
        return value[:1500]
    if isinstance(value, list):
        return [safe(v) for v in value[:5000] if isinstance(v, (str, int, float))]
    return None


def pick(value, keys):
    if not isinstance(value, dict):
        return {}
    return {k: safe(value[k]) for k in keys if k in value and safe(value[k]) is not None}


QUALITY_KEYS = ('status', 'scope', 'expected_count', 'fresh_count', 'fresh_coverage', 'window_coverage',
                'one_minute_coverage', 'missing_codes', 'quote_time', 'coverage', 'errors', 'reason')
FLOW_KEYS = ('code', 'name', 'status', 'reason', 'net_cny', 'net_inflow_cny', 'main_net_cny',
             'net_amount_cny', 'source_asof', 'last_valid_asof', 'negative_active', 'main_net_pct', 'main_net_inflow_cny', 'net_share_pct', 'quote_time', 'source_time',
             'observed_at', 'first_observed_at', 'source', 'provider', 'scope', 'watchlist',
             'price', 'pct', 'outflow', 'last_source_time', 'source_url')


def read_status(now=None, directory=None):
    now = now or dt.datetime.now(TZ)
    directory = directory or monitor_root()
    raw = read_json(directory / 'status.json', {})
    raw = raw if isinstance(raw, dict) else {}
    heartbeat = stamp(raw.get('heartbeat'))
    from market_calendar import is_trading_day
    trading, calendar_note = is_trading_day(now.date())
    clock = now.time().replace(tzinfo=None)
    market_open = trading and (dt.time(9, 30) <= clock < dt.time(11, 30) or dt.time(13) <= clock < dt.time(15))
    healthy_heartbeat = heartbeat is not None and 0 <= (now - heartbeat).total_seconds() <= 90
    quality = pick(raw.get('quality'), QUALITY_KEYS)
    if not healthy_heartbeat:
        state, note = 'offline', '监控服务离线或心跳过期；未知不等于无异动'
    elif not trading or not market_open:
        state, note = 'closed', calendar_note if not trading else '当前为休市时段，保留历史事件'
    elif raw.get('enabled') is not True:
        state, note = 'disabled', '监控未启用'
    elif raw.get('scope') != 'all_sectors' or raw.get('membership_date') != now.date().isoformat():
        state, note = 'degraded', '全板块范围或当日名单尚未核验'
    elif quality.get('status') != 'healthy':
        state, note = 'degraded', '行情覆盖不足或数据不可用'
    elif quality.get('window_coverage', 0) < 1:
        state, note = 'warming', '监控采样中，部分板块仍在积累完整窗口'
    else:
        state, note = 'healthy', '板块监控正常；下方为已经发生的事件'
    flow = read_json(directory / 'dragon-flow-status.json', {})
    flow = flow if isinstance(flow, dict) else {}
    clean_flow = pick(flow, ('status', 'scope', 'as_of', 'updated_at', 'heartbeat', 'reason', 'errors',
                            'coverage', 'expected_count', 'fresh_count', 'missing_codes', 'live_verified',
                            'watchlist_status', 'watchlist_reason', 'last_live_verified_at', 'tracked_count', 'active_outflow_count', 'last_attempt_at'))
    for key in ('stocks', 'items', 'rows'):
        if isinstance(flow.get(key), list):
            clean_flow['stocks'] = [pick(r, FLOW_KEYS) for r in flow[key] if isinstance(r, dict)]
    clean_flow['membership'] = pick(flow.get('membership'), ('status', 'reason', 'valid_until', 'errors'))
    if not flow:
        clean_flow = {'status': 'unavailable', 'reason': '资金监测尚无状态记录，盘中时效未验收', 'stocks': []}
    flow_heartbeat = stamp(flow.get('heartbeat'))
    if flow_heartbeat is None or not 0 <= (now - flow_heartbeat).total_seconds() <= 90:
        clean_flow.update(status='unavailable', reason='资金监控心跳缺失或过期，当前资金状态未知')
        for row in clean_flow.get('stocks', []):
            row.update(status='stale', reason='资金状态已过期', main_net_cny=None, main_net_pct=None)
    return {'as_of': now.isoformat(timespec='seconds'), 'state': state, 'note': note,
            'heartbeat': heartbeat.isoformat() if heartbeat else None, 'market_open': market_open,
            'sector_count': safe(raw.get('sector_count')), 'quality': quality,
            'category_counts': pick(raw.get('category_counts'), ('行业', '概念')),
            'membership_date': safe(raw.get('membership_date')), 'flow': clean_flow,
            'delivery_note': '本机30秒采样；页面可见时每30秒读取；对话沿用每5分钟复核。手机提醒未作本次验收。'}


def read_events(day=None, cutoff=None, directory=None):
    cutoff = cutoff or dt.datetime.now(TZ)
    day = day or cutoff.date()
    directory = directory or monitor_root()
    raw = read_json(directory / 'events.json', None)
    if not isinstance(raw, list):
        return {'date': day.isoformat(), 'events': [], 'status': 'unavailable'}
    result, seen = [], set()
    for batch in raw:
        if not isinstance(batch, dict):
            continue
        observed = stamp(batch.get('time'))
        if observed is None or observed > cutoff:
            continue
        signals = batch.get('signals')
        if not isinstance(signals, list):
            # Old free-text game messages remain archived, not reclassified as market-wide signals.
            title = str(batch.get('title', ''))
            if observed.date() == day and any(x in title for x in ('故障', '异常', '恢复')):
                identity = 'health:' + observed.isoformat() + title
                if identity not in seen:
                    seen.add(identity)
                    result.append({'id': hashlib.sha256(identity.encode()).hexdigest()[:20], 'type': 'health',
                                   'quote_time': observed.isoformat(), 'observed_at': observed.isoformat(),
                                   'name': '服务状态', 'kind': '故障恢复记录' if '恢复' in title else '数据故障记录'})
            continue
        for signal in signals:
            if not isinstance(signal, dict):
                continue
            kind = signal.get('type')
            if kind not in ('dragon_fund_outflow', 'dragon_fund_recovery'):
                if not str(signal.get('key', '')).startswith('sector:'):
                    continue
                kind = 'sector_movement'
            event_time = stamp(signal.get('quote_time') or signal.get('source_asof') or signal.get('source_time'))
            if event_time is None or event_time > cutoff or event_time > observed or event_time.date() != day:
                continue
            out = pick(signal, FLOW_KEYS + ('key', 'kind', 'direction', 'seconds', 'move_pct', 'day_pct',
                                            'window_start', 'window_seconds', 'categories', 'episode'))
            out.update(type=kind, quote_time=event_time.isoformat(), observed_at=observed.isoformat())
            identity = f'{kind}:{signal.get("code")}:{event_time.isoformat()}:{signal.get("key", "")}:{signal.get("episode", "")}'
            out['id'] = hashlib.sha256(identity.encode()).hexdigest()[:20]
            if out['id'] not in seen:
                seen.add(out['id']); result.append(out)
    result.sort(key=lambda x: (x['quote_time'], x['id']), reverse=True)
    return {'date': day.isoformat(), 'cutoff': cutoff.isoformat(), 'status': 'available', 'events': result}
