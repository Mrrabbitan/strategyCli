"""Scoped, timestamp-checked dragon-watchlist fund-flow observations.

The provider classifies trades, not investor identities or holdings.  Every
minute row is a cumulative intraday snapshot; rows must never be summed.
"""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

from market_calendar import is_trading_day

TZ = ZoneInfo('Asia/Shanghai')
PROVIDER = 'eastmoney'
SCOPE = 'dragon_watchlist'
POLL_SECONDS = 30
STALE_SECONDS = 90
ENDPOINT = 'https://push2.eastmoney.com/api/qt/stock/fflow/kline/get'
DEFINITION = '东方财富主力净额=大单净额+超大单净额；交易分类估算，不代表真实账户持仓。'
CODE = re.compile(r'(?:60[0135]|00[0123])\d{3}')


def market_session(now):
    now = now.astimezone(TZ)
    if not is_trading_day(now.date())[0]:
        return None
    clock = now.time().replace(tzinfo=None)
    part = 'am' if dt.time(9, 30) <= clock < dt.time(11, 30) else (
        'pm' if dt.time(13) <= clock < dt.time(15) else None)
    return f'{now.date()}-{part}' if part else None


def _number(value):
    if isinstance(value, bool):
        raise ValueError('boolean numeric field')
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('non-finite numeric field')
    return result


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('missing ISO date')
    return dt.date.fromisoformat(value)


def _save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as out:
        os.chmod(temporary, 0o600)
        json.dump(value, out, ensure_ascii=False, indent=2, allow_nan=False)
    temporary.replace(path)


def monitoring_members(document, today):
    """Expired observations cannot silently become a normal empty watchlist."""
    monitoring = document.get('monitoring') if isinstance(document, dict) else None
    if not isinstance(monitoring, dict):
        return {}, dict(status='missing', reason='缺少有效龙空龙监控名单', errors=[])
    members, errors = {}, []
    observed_valid = False
    expires = monitoring.get('valid_until')
    try:
        observed_valid = _date(expires) >= today
    except ValueError:
        errors.append('观察名单有效期缺失或无效')
    observed = monitoring.get('observed')
    pins = monitoring.get('pins', [])
    if not isinstance(observed, list):
        errors.append('观察名单格式无效')
        observed = []
        observed_valid = False
    if not isinstance(pins, list):
        errors.append('持续跟踪名单格式无效')
        pins = []

    def add(row, origin):
        if (not isinstance(row, dict) or not isinstance(row.get('code'), str)
                or not CODE.fullmatch(row['code']) or not isinstance(row.get('name'), str)
                or not row['name'].strip()):
            errors.append('名单存在无效主板代码或名称')
            return
        code = row['code']
        if code in members:
            if members[code]['name'] != row['name'].strip():
                errors.append('同一代码名称冲突：' + code)
                return
            members[code]['origins'].append(origin)
        else:
            members[code] = dict(code=code, name=row['name'].strip(), origins=[origin])

    if observed_valid:
        for row in observed:
            add(row, 'observed')
    pin_count = 0
    for row in pins:
        if not isinstance(row, dict) or row.get('confirmed') is not True:
            continue
        try:
            if _date(row.get('valid_until')) < today:
                continue
        except ValueError:
            errors.append('持续跟踪有效期缺失或无效')
            continue
        before = len(members)
        add(row, 'pin')
        if len(members) > before or row.get('code') in members:
            pin_count += 1
    if not observed_valid:
        status = 'partial' if members else 'expired' if expires and not errors else 'invalid'
        reason = '观察名单已过期；仅保留已确认且未到期的持续跟踪' if members else '观察名单已过期或无效，等待更新'
    elif errors:
        status, reason = 'partial' if members else 'invalid', '名单存在无效记录，相关标的不监控'
    else:
        status, reason = 'valid', '当前有效龙空龙观察股及已确认持续跟踪'
    return members, dict(status=status, reason=reason, valid_until=expires,
                        observed_count=sum('observed' in v['origins'] for v in members.values()),
                        pin_count=pin_count, errors=errors)


def load_members(now, path=None):
    if path is None:
        from research_store import research_path
        path = research_path('dragon/current.json')
    try:
        document = json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}, dict(status='missing', reason='未找到龙空龙监控名单', errors=[])
    except (OSError, ValueError):
        return {}, dict(status='invalid', reason='龙空龙监控名单不可读', errors=[])
    return monitoring_members(document, now.astimezone(TZ).date())


def parse_flow(payload, code, fetched_at, source_url=ENDPOINT):
    """Validate the provider's own minute timestamp and main/large/super sum."""
    if not isinstance(payload, dict) or payload.get('rc') != 0:
        raise ValueError('provider response unsuccessful')
    data = payload.get('data')
    if not isinstance(data, dict) or str(data.get('code')) != code:
        raise ValueError('fund-flow code mismatch')
    if str(data.get('market')) != ('1' if code.startswith('6') else '0'):
        raise ValueError('fund-flow market mismatch')
    rows = data.get('klines')
    if not isinstance(rows, list) or not rows:
        raise ValueError('fund-flow rows missing')
    previous, latest = None, None
    for line in rows:
        cells = line.split(',') if isinstance(line, str) else []
        if len(cells) < 6:
            raise ValueError('incomplete fund-flow row')
        stamp = dt.datetime.strptime(cells[0], '%Y-%m-%d %H:%M').replace(tzinfo=TZ)
        values = [_number(v) for v in cells[1:6]]
        if not math.isclose(values[0], values[3] + values[4], rel_tol=1e-9, abs_tol=0.02):
            raise ValueError('main flow differs from large plus super-large')
        if previous:
            if stamp < previous[0] or stamp.date() != previous[0].date():
                raise ValueError('unordered or mixed-date fund-flow rows')
            if stamp == previous[0] and values != previous[1]:
                raise ValueError('conflicting duplicate source timestamp')
        previous = stamp, values
        latest = dict(code=code, name=str(data.get('name') or code), provider=PROVIDER,
                      source_asof=stamp.isoformat(), source_ts=stamp.timestamp(),
                      fetched_at=fetched_at.isoformat(), main_net_cny=values[0],
                      large_net_cny=values[3], super_large_net_cny=values[4],
                      main_net_pct=None, period='day_to_source_asof',
                      source_url=source_url, delayed_transport='push2delay.' in source_url)
    periods = (data.get('tradePeriods') or {}).get('periods')
    if not isinstance(periods, list) or not periods:
        raise ValueError('source trading periods missing')
    day = previous[0].strftime('%Y%m%d')
    if any(not isinstance(p, dict) or not str(p.get('b', '')).startswith(day)
           or not str(p.get('e', '')).startswith(day) for p in periods):
        raise ValueError('source trading-period date mismatch')
    return latest


class DragonFlowFeed:
    def one(self, member):
        code = member['code']
        if not CODE.fullmatch(code):
            raise ValueError('unsupported main-board code')
        params = dict(secid=('1.' if code.startswith('6') else '0.') + code, klt='1', lmt='0',
                      fields1='f1,f2,f3,f7', fields2='f51,f52,f53,f54,f55,f56',
                      ut='b2884a393a59ad64002292a3e90d46a5')
        url = ENDPOINT + '?' + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0', 'Referer': 'https://data.eastmoney.com/'})
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read())
            actual_url = response.geturl()
        return parse_flow(payload, code, dt.datetime.now(TZ), actual_url)

    def snapshot(self, members):
        snapshots, errors = {}, {}
        # Scope is the explicit watchlist only, never a market-wide request.
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            jobs = {pool.submit(self.one, row): code for code, row in members.items()}
            for future in concurrent.futures.as_completed(jobs):
                code = jobs[future]
                try:
                    snapshots[code] = future.result()
                except Exception as exc:
                    errors[code] = '资金源不可用：' + type(exc).__name__
        return snapshots, errors


class DragonFlowEngine:
    def __init__(self, state=None):
        self.state = state if state is not None else dict(version=1, provider=PROVIDER, stocks={}, pending_signals=[])
        if (self.state.get('version') != 1 or self.state.get('provider') != PROVIDER
                or not isinstance(self.state.get('stocks'), dict)
                or not isinstance(self.state.get('pending_signals'), list)):
            raise ValueError('unsupported fund-flow state')
        for code, row in self.state['stocks'].items():
            if (not CODE.fullmatch(code) or not isinstance(row, dict)
                    or not isinstance(row.get('negative_active'), bool)
                    or type(row.get('episode')) is not int or row['episode'] < 0):
                raise ValueError('invalid persisted episode')
            _date(row.get('trade_date'))
            _number(row.get('source_ts'))
            _number(row.get('main_net_cny'))
        for signal in self.state['pending_signals']:
            if (not isinstance(signal, dict) or signal.get('scope') != SCOPE
                    or signal.get('type') not in ('dragon_fund_outflow', 'dragon_fund_recovery')
                    or not isinstance(signal.get('event_id'), str)
                    or any(key not in signal for key in ('name', 'code', 'kind', 'detail', 'observed_ts',
                                                        'source_asof', 'source_url'))):
                raise ValueError('invalid persisted pending signal')

    def evaluate(self, now, snapshots, members, errors=None):
        errors = errors or {}
        sid = market_session(now)
        if not sid:
            return [], []
        day, emitted, statuses = str(now.astimezone(TZ).date()), [], []
        for code, member in members.items():
            prior = self.state['stocks'].get(code, {})
            status = dict(code=code, name=member['name'], origins=member['origins'], status='unavailable',
                          reason=errors.get(code, '未取得资金快照'), source_asof=None, source_ts=None,
                          fetched_at=None, main_net_cny=None, main_net_pct=None, negative_active=bool(
                              prior.get('trade_date') == day and prior.get('negative_active')))
            quote = snapshots.get(code)
            try:
                if not quote or quote.get('code') != code or quote.get('provider') != PROVIDER:
                    raise ValueError(status['reason'])
                stamp = _number(quote['source_ts'])
                value = _number(quote['main_net_cny'])
                asof = dt.datetime.fromtimestamp(stamp, TZ)
                if (not 0 <= now.timestamp() - stamp <= STALE_SECONDS
                        or market_session(asof) != sid):
                    raise ValueError('资金源时间过期、未来时间或不属当前交易时段')
                if quote.get('period') != 'day_to_source_asof':
                    raise ValueError('资金累计口径不匹配')
                if prior.get('trade_date') != day:
                    prior = dict(trade_date=day, negative_active=False, episode=0)
                previous_ts = prior.get('source_ts')
                if previous_ts is not None and stamp < previous_ts:
                    raise ValueError('资金源时间倒退')
                if previous_ts == stamp and prior.get('main_net_cny') != value:
                    raise ValueError('同一源时间发生修订，等待新时间点确认')
                status.update(status='fresh', reason='', source_asof=asof.isoformat(), source_ts=stamp,
                              fetched_at=quote.get('fetched_at'), main_net_cny=value,
                              source_url=quote.get('source_url'), delayed_transport=quote.get('delayed_transport', False))
                if previous_ts != stamp:
                    kind = None
                    if value < 0 and not prior['negative_active']:
                        prior['episode'] += 1
                        prior['negative_active'] = True
                        kind = 'dragon_fund_outflow'
                    elif value >= 0 and prior['negative_active']:
                        prior['negative_active'] = False
                        kind = 'dragon_fund_recovery'
                    if kind:
                        key = f'dragon-flow:{day}:{code}:{prior["episode"]}:{kind}'
                        signal = dict(type=kind, scope=SCOPE, key=key, event_id=key, code=code,
                                      name=member['name'], provider=PROVIDER, episode=prior['episode'],
                                      main_net_cny=value, main_net_pct=None, period='day_to_source_asof',
                                      source_asof=asof.isoformat(), source_url=quote.get('source_url', ENDPOINT),
                                      observed_at=now.isoformat(), observed_ts=now.timestamp(),
                                      direction='净流出' if value < 0 else '恢复非负',
                                      kind='龙空龙资金异动' if value < 0 else '龙空龙资金恢复',
                                      detail=f'当日累计主力净额{value / 1e8:+.4f}亿元；仅风险提示，不自动买卖。')
                        emitted.append(signal)
                        self.state['pending_signals'].append(signal)
                    prior.update(source_ts=stamp, source_asof=asof.isoformat(), main_net_cny=value)
                    self.state['stocks'][code] = prior
                status['negative_active'] = prior['negative_active']
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                status['reason'] = str(exc)
                if prior.get('source_asof'):
                    status['last_valid_asof'] = prior['source_asof']
                    status['last_valid_main_net_cny'] = prior.get('main_net_cny')
            statuses.append(status)
        return emitted, statuses


class DragonFlowMonitor:
    """One independent future; durable pending signals survive restart safely."""
    def __init__(self, directory, feed=None, loader=None, executor=None, monotonic=None):
        self.directory = Path(directory)
        self.state_path = self.directory / 'dragon-flow-state.json'
        self.status_path = self.directory / 'dragon-flow-status.json'
        self.loader, self.feed = loader or load_members, feed or DragonFlowFeed()
        self.pool = executor or concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.monotonic = monotonic or time.monotonic
        self.future, self.next_poll, self.next_status = None, 0.0, 0.0
        self.rows, self.last_attempt_at = [], None
        self.state_error = False
        try:
            state = json.loads(self.state_path.read_text()) if self.state_path.exists() else None
            self.engine = DragonFlowEngine(state)
        except (OSError, ValueError, TypeError, AttributeError):
            self.engine, self.state_error = DragonFlowEngine(), True

    def pump(self, now, enabled=True):
        try:
            members, membership = self.loader(now)
        except Exception:
            members, membership = {}, dict(status='invalid', reason='龙空龙名单加载失败', errors=[])
        sid, clock = market_session(now), self.monotonic()
        if self.future is not None and self.future.done():
            try:
                snapshots, errors = self.future.result()
            except Exception as exc:
                snapshots, errors = {}, {code: '资金采集失败：' + type(exc).__name__ for code in members}
            if enabled and sid and not self.state_error:
                _, self.rows = self.engine.evaluate(now, snapshots, members, errors)
                _save(self.state_path, self.engine.state)
            self.future = None
        if enabled and sid and members and not self.state_error and self.future is None and clock >= self.next_poll:
            self.future = self.pool.submit(self.feed.snapshot, members)
            self.next_poll = clock + POLL_SECONDS
            self.last_attempt_at = now.isoformat()
        by_code = {r['code']: r for r in self.rows}
        stocks = []
        for code, member in members.items():
            row = dict(by_code.get(code, dict(code=code, name=member['name'], origins=member['origins'],
                        status='waiting', reason='等待当前时段资金快照', source_asof=None, source_ts=None,
                        fetched_at=None, main_net_cny=None, main_net_pct=None, negative_active=False)))
            row.update(name=member['name'], origins=member['origins'])
            if row['status'] == 'fresh' and (not sid or not 0 <= now.timestamp() - row['source_ts'] <= STALE_SECONDS
                    or market_session(dt.datetime.fromtimestamp(row['source_ts'], TZ)) != sid):
                row.update(status='stale', reason='资金源时间已过期或不属当前时段',
                           last_valid_asof=row.get('source_asof'), last_valid_main_net_cny=row['main_net_cny'],
                           main_net_cny=None, main_net_pct=None)
            stocks.append(row)
        fresh_count = sum(r['status'] == 'fresh' for r in stocks)
        status, reason = 'healthy', '有效资金数据已核验；负值首次提醒，持续负值不重复'
        if self.state_error:
            status, reason = 'unknown', '资金持久状态不可读，暂停告警以避免重复'
        elif membership['status'] not in ('valid', 'partial'):
            status, reason = 'unknown', membership['reason']
        elif not enabled:
            status, reason = 'disabled', '服务未启用，资金监控暂停'
        elif not sid:
            status, reason = 'closed', '非连续交易时段，不产生盘中资金异动'
        elif not members:
            status, reason = 'empty', '有效名单暂无观察股或持续跟踪股'
        elif fresh_count < len(members):
            status, reason = 'degraded', '部分或全部资金数据尚未通过90秒时效核验'
        elif membership['status'] == 'partial':
            status, reason = 'degraded', membership['reason']
        report = dict(version=1, scope=SCOPE, provider=PROVIDER, definition=DEFINITION,
                      status=status, reason=reason, heartbeat=now.isoformat(), enabled=bool(enabled),
                      phone_alerts_enabled=False, poll_seconds=POLL_SECONDS, stale_seconds=STALE_SECONDS,
                      membership=membership, tracked_count=len(members), fresh_count=fresh_count,
                      active_outflow_count=sum(r['status'] == 'fresh' and r.get('negative_active', False) for r in stocks),
                      pending_event_count=len(self.engine.state['pending_signals']),
                      inflight=self.future is not None, last_attempt_at=self.last_attempt_at,
                      stocks=stocks, cumulative_ratio_note='未取得同刻同口径成交额或原生比例，不计算净占比。')
        if clock >= self.next_status:
            _save(self.status_path, report)
            self.next_status = clock + 5
        return list(self.engine.state['pending_signals']) if not self.state_error else [], report

    def acknowledge(self, ids):
        ids = set(ids)
        if ids:
            self.engine.state['pending_signals'] = [s for s in self.engine.state['pending_signals'] if s['event_id'] not in ids]
            _save(self.state_path, self.engine.state)

    def close(self):
        self.pool.shutdown(wait=False)
