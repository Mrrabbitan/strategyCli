"""Intraday sector alerts. Standard library only; never places orders."""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import plistlib
import re
import secrets
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

from market_calendar import is_trading_day

ROOT = Path(__file__).resolve().parent
PRIVATE = Path.home() / 'Library/Application Support/AutoStrategy/game-monitor'
CONFIG = PRIVATE / 'config.json'
LABEL = 'local.autostrategy.game-monitor'
TZ = ZoneInfo('Asia/Shanghai')
DEFAULTS = dict(enabled=False, phone_verified=False, observe_only=False, poll_seconds=30,
                scope='game', sector_1m_pct=0.5, sector_5m_pct=1.0,
                board_pct=1.0, breadth_ratio=0.60, stock_pct=3.0,
                volume_ratio=2.0, cluster_pct=2.0, cluster_count=3,
                coverage_ratio=0.90, stale_seconds=90, cooldown_seconds=600,
                ntfy_server='https://ntfy.sh', ntfy_topic='', ntfy_token='')
HOSTS = ('push2.eastmoney.com', '82.push2.eastmoney.com', 'push2delay.eastmoney.com')


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix('.tmp')
    with tmp.open('w', encoding='utf-8') as out:
        os.chmod(tmp, 0o600)
        json.dump(value, out, ensure_ascii=False, indent=2)
    tmp.replace(path)


def read(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def request(url, data=None, headers=None):
    hdr = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://quote.eastmoney.com/'}
    hdr.update(headers or {})
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=hdr), timeout=5) as response:
        return response.read()


def session(now):
    now = now.astimezone(TZ)
    ok, _ = is_trading_day(now.date())
    if not ok:
        return None
    clock = now.time().replace(tzinfo=None)
    suffix = 'am' if dt.time(9, 30) <= clock < dt.time(11, 30) else (
        'pm' if dt.time(13) <= clock < dt.time(15) else None)
    return f'{now.date()}-{suffix}' if suffix else None


def finite(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('non-finite quote')
    return value


class Feed:
    def __init__(self):
        self.hosts = list(HOSTS)

    def eastmoney(self, endpoint, params):
        for host in list(self.hosts):
            try:
                url = f'https://{host}/api/qt/{endpoint}?' + urllib.parse.urlencode(params)
                result = json.loads(request(url))['data']
                if not result:
                    raise ValueError('empty data')
                self.hosts = [host] + [h for h in self.hosts if h != host]
                return result, host
            except Exception:
                continue
        raise RuntimeError('东方财富所有入口不可用')

    def listing(self, scope):
        rows, total, page, source = [], None, 1, ''
        while total is None or len(rows) < total:
            data, source = self.eastmoney('clist/get', dict(pn=page, pz=100, np=1,
                fs=scope, fields='f12,f14', fltt=2, fid='f12', po=0))
            batch = data.get('diff') or []
            if isinstance(batch, dict):
                batch = list(batch.values())
            if not batch:
                raise RuntimeError('成分列表不完整')
            rows.extend(batch)
            total = int(data['total'])
            page += 1
            if page > 20:
                raise RuntimeError('板块分页异常')
        if len({r['f12'] for r in rows}) != total:
            raise RuntimeError('板块列表数量校验失败')
        return rows, source

    def universe(self, day):
        boards, _ = self.listing('m:90+t:2+f:!50')
        matches = [r for r in boards if r['f14'] in ('游戏', '游戏Ⅱ')]
        if len(matches) != 1:
            raise RuntimeError('游戏行业板块无法唯一识别')
        board = matches[0]
        rows, source = self.listing('b:' + board['f12'])
        if not rows or any(not re.fullmatch(r'(?:00|30|60|68)\d{4}', r['f12']) for r in rows):
            raise RuntimeError('游戏成分股校验失败')
        return dict(date=str(day), board=board['f12'], name=board['f14'],
                    members={r['f12']: r['f14'] for r in rows}, source=source)

    def board(self, universe):
        data, host = self.eastmoney('stock/get', dict(secid='90.' + universe['board'],
                fields='f43,f57,f58,f86', fltt=2))
        if data['f57'] != universe['board']:
            raise RuntimeError('板块代码不匹配')
        return dict(name=data['f58'], price=finite(data['f43']),
                    ts=finite(data['f86']), amount=0, source=host)

    def stocks(self, universe):
        symbols = [('sh' if c.startswith('6') else 'sz') + c for c in universe['members']]
        raw = request('https://qt.gtimg.cn/q=' + ','.join(symbols)).decode('gb18030', 'replace')
        result = {}
        for match in re.finditer(r'="([^"]*)";', raw):
            p = match.group(1).split('~')
            if len(p) < 39 or p[2] not in universe['members']:
                continue
            try:
                stamp = dt.datetime.strptime(p[30], '%Y%m%d%H%M%S').replace(tzinfo=TZ).timestamp()
                result[p[2]] = dict(name=p[1], price=finite(p[3]), amount=finite(p[37]) * 10000,
                                     ts=stamp, source='qt.gtimg.cn')
            except (ValueError, IndexError):
                continue
        if not result:
            raise RuntimeError('腾讯个股行情不可用')
        return result

    def snapshot(self, universe):
        # Board failure must not suppress otherwise valid individual stock rules.
        result, errors = {}, []
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            calls = {pool.submit(self.board, universe): 'board', pool.submit(self.stocks, universe): 'stocks'}
            for future, kind in calls.items():
                try:
                    value = future.result()
                    result.update({'board': value} if kind == 'board' else value)
                except Exception:
                    errors.append(kind + '行情不可用')
        return result, errors


class Engine:
    def __init__(self, config, state=None):
        self.c = config
        self.state = state or {}
        self.state.setdefault('history', {})
        self.state.setdefault('latches', {})

    def evaluate(self, now, quotes, expected):
        sid, epoch = session(now), now.timestamp()
        if not sid:
            return [], {'status': 'closed'}
        if self.state.get('session') != sid:
            self.state.update(session=sid, history={}, latches={})
        hist = self.state['history']
        fresh = {}
        for code, q in quotes.items():
            if code != 'board' and code not in expected:
                continue
            if (not all(math.isfinite(q[k]) for k in ('price', 'amount', 'ts')) or
                    not 0 <= epoch - q['ts'] <= self.c['stale_seconds'] or q['price'] <= 0 or q['amount'] < 0 or
                    session(dt.datetime.fromtimestamp(q['ts'], TZ)) != sid):
                continue
            rows = hist.setdefault(code, [])
            if rows and (q['ts'] < rows[-1]['ts'] or q['amount'] < rows[-1]['amount']):
                rows.clear()
            if not rows or q['ts'] > rows[-1]['ts']:
                rows.append(q)
            hist[code] = [r for r in rows if r['ts'] >= epoch - 1350]
            fresh[code] = q

        def window(code, seconds):
            rows = hist.get(code, [])
            if code not in fresh or not rows:
                return None
            end = fresh[code]
            target = end['ts'] - seconds
            anchors = [r for r in rows if target - 40 <= r['ts'] <= target]
            if not anchors:
                return None
            start = anchors[-1]
            selected = [r for r in rows if start['ts'] <= r['ts'] <= end['ts']]
            if any(b['ts'] - a['ts'] > self.c['stale_seconds'] for a, b in zip(selected, selected[1:])):
                return None
            return start, end

        moves, volume = {}, {}
        for code in fresh:
            pair = window(code, 300)
            if not pair:
                continue
            moves[code] = (pair[1]['price'] / pair[0]['price'] - 1) * 100
            if code != 'board':
                anchors = [window(code, sec) for sec in (300, 600, 900, 1200)]
                if all(anchors):
                    amounts = [fresh[code]['amount']] + [p[0]['amount'] for p in anchors]
                    deltas = [a - b for a, b in zip(amounts, amounts[1:])]
                    baseline = sum(deltas[1:]) / 3
                    if min(deltas) >= 0 and baseline > 0:
                        volume[code] = deltas[0] / baseline
        eligible = {c: v for c, v in moves.items() if c != 'board'}
        coverage = len(eligible) / len(expected) if expected else 0
        fresh_coverage = len(set(fresh) & set(expected)) / len(expected) if expected else 0
        candidates, evaluated = [], set()

        def check(key, condition, event):
            evaluated.add(key)
            latch = self.state['latches'].setdefault(key, {'active': False, 'last': -1e20})
            if condition:
                if not latch['active'] and epoch - latch['last'] >= self.c['cooldown_seconds']:
                    candidates.append(dict(event, key=key))
                    latch['last'] = epoch
                latch['active'] = True
            else:
                latch['active'] = False

        for sign in (1, -1):
            direction = '上涨' if sign == 1 else '下跌'
            if coverage >= self.c['coverage_ratio']:
                count = sum(v * sign >= self.c['cluster_pct'] - 1e-9 for v in eligible.values())
                check(f'cluster:{sign}', count >= self.c['cluster_count'],
                      dict(kind='集体异动', name='游戏板块', direction=direction, detail=f'{count}只近5分钟{direction}≥{self.c["cluster_pct"]}%'))
                if 'board' in moves:
                    breadth = sum(v * sign > 0 for v in eligible.values()) / len(eligible)
                    check(f'board:{sign}', moves['board'] * sign >= self.c['board_pct'] - 1e-9 and breadth >= self.c['breadth_ratio'],
                          dict(kind='板块快速涨跌', name='游戏板块', direction=direction,
                               detail=f'近5分钟{moves["board"]:+.2f}%，同向比例{breadth:.0%}'))
            for code, ratio in volume.items():
                check(f'stock:{code}:{sign}', moves[code] * sign >= self.c['stock_pct'] - 1e-9 and ratio >= self.c['volume_ratio'],
                      dict(kind='个股放量涨跌', name=f'{fresh[code]["name"]}({code})', direction=direction,
                           detail=f'近5分钟{moves[code]:+.2f}%，成交额{ratio:.2f}倍'))
        leaders = sorted(eligible, key=lambda c: abs(eligible[c]), reverse=True)[:3]
        quality = dict(status='healthy' if fresh_coverage >= self.c['coverage_ratio'] and 'board' in fresh else 'degraded',
                       fresh_coverage=fresh_coverage, window_coverage=coverage,
                       board_move=moves.get('board'),
                       leaders=[f'{fresh[c]["name"]}({c}) {eligible[c]:+.2f}%' for c in leaders],
                       quote_time=dt.datetime.fromtimestamp(min(q['ts'] for q in fresh.values()), TZ).isoformat() if fresh else None)
        self.state['last_quality'] = quality
        return candidates, quality


def publish(config, title, message):
    headers = {'Content-Type': 'application/json'}
    if config.get('ntfy_token'):
        headers['Authorization'] = 'Bearer ' + config['ntfy_token']
    payload = dict(topic=config['ntfy_topic'], title=title, message=message, priority=4)
    response = json.loads(request(config['ntfy_server'], json.dumps(payload, ensure_ascii=False).encode(), headers))
    if not response.get('id') or response.get('event') != 'message':
        raise RuntimeError('ntfy未确认接收')
    return response['id']


class Outbox:
    """One worker owns the durable queue; retries never block quote collection."""
    def __init__(self, config, directory):
        self.c, self.directory = config, directory
        self.path = directory / 'outbox.json'
        self.jobs = read(self.path, [])
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.future = None
        self.active = None

    def add(self, title, message, now):
        self.jobs.append(dict(id=secrets.token_hex(8), title=title, message=message,
                             created=now, due=now, attempt=0))
        save(self.path, self.jobs)

    def pump(self, now):
        if self.future and self.future.done():
            job = self.active
            try:
                message_id = self.future.result()
                logging.info('ntfy accepted event=%s message=%s (phone unconfirmed)', job['id'], message_id)
                self.jobs.remove(job)
            except Exception as exc:
                job['attempt'] += 1
                logging.warning('push failed event=%s attempt=%s type=%s', job['id'], job['attempt'], type(exc).__name__)
                if job['attempt'] >= 3:
                    self.jobs.remove(job)
                else:
                    job['due'] = now + (5 if job['attempt'] == 1 else 15)
            self.future = self.active = None
            save(self.path, self.jobs)
        if self.future:
            return
        expired = [j for j in self.jobs if now - j['created'] > 120]
        for job in expired:
            logging.warning('expired unsent event=%s', job['id'])
            self.jobs.remove(job)
        if expired:
            save(self.path, self.jobs)
        ready = next((j for j in self.jobs if j['due'] <= now), None)
        if ready:
            self.active = ready
            self.future = self.pool.submit(publish, self.c, ready['title'], ready['message'])


def health(state, degraded, now, label='游戏监控'):
    if degraded:
        if state.get('fault_since') is None:
            state['fault_since'] = now
        if now - state['fault_since'] >= 180 and not state.get('fault_notified'):
            state['fault_notified'] = True
            return label + '数据故障', '行情或名单数据连续异常3分钟，缺失数据对应规则已暂停，请查看Mac服务状态。'
    else:
        recovery = state.get('fault_notified')
        state['fault_since'] = None
        state['fault_notified'] = False
        if recovery:
            return label + '已恢复', '行情服务恢复，完整窗口积累后继续判定异动。'
    return None


def modes(config):
    push = bool(config.get('enabled') and config.get('phone_verified'))
    return bool(push or config.get('observe_only')), push


def record_notice(directory, box, push, title, body, now, signals=None, scope=None):
    """Observation mode records locally and never queues a delayed phone alert."""
    path = directory / 'events.json'
    events = read(path, [])
    event = dict(time=now, title=title, message=body, phone_queued=push)
    if signals is not None:
        event['signals'] = signals
    if scope is not None:
        event['scope'] = scope
    events.append(event)
    save(path, events[-2000:])
    logging.info('notice title=%s phone_queued=%s', title, push)
    if push:
        box.add(title, body, now)


def record_dragon_notices(directory, box, monitor, signals):
    """Local events only; pending IDs make an interrupted write replay-safe."""
    if not signals:
        return
    from dragon_fund_flow import DEFINITION, SCOPE
    existing = read(directory / 'events.json', [])
    seen = {signal.get('event_id') for event in existing for signal in event.get('signals', [])
            if signal.get('scope') == SCOPE}
    acknowledged = []
    for signal in signals:
        event_id = signal['event_id']
        if event_id not in seen:
            title = signal['kind'] + '｜' + signal['name'] + '(' + signal['code'] + ')'
            body = (signal['detail'] + '\n资金时间：' + signal['source_asof']
                    + '\n来源：' + signal['source_url'] + '\n' + DEFINITION)
            record_notice(directory, box, False, title, body, signal['observed_ts'],
                          signals=[signal], scope=SCOPE)
            seen.add(event_id)
        acknowledged.append(event_id)
    monitor.acknowledge(acknowledged)


def run(config_path):
    directory = config_path.parent
    handler = RotatingFileHandler(directory / 'monitor.log', maxBytes=2_000_000, backupCount=3)
    logging.basicConfig(level=logging.INFO, handlers=[handler], format='%(asctime)s %(levelname)s %(message)s')
    lock = (directory / 'service.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    state_path = directory / 'state.json'
    config = dict(DEFAULTS, **read(config_path, {}))
    all_sectors = config.get('scope') == 'all_sectors'
    label = '全板块监控' if all_sectors else '游戏监控'
    if all_sectors:
        from sector_monitor import SectorEngine, SectorFeed, format_alerts
        engine_type, feed_type = SectorEngine, SectorFeed
    else:
        engine_type, feed_type = Engine, Feed
    engine = engine_type(config, read(state_path, {}))
    universe = read(directory / 'universe.json', {})
    if universe.get('scope', 'game') != config.get('scope', 'game'):
        universe = {}
    feed, executor = feed_type(), concurrent.futures.ThreadPoolExecutor(max_workers=1)
    box = Outbox(engine.c, directory)
    from dragon_fund_flow import DragonFlowMonitor
    dragon_flow = DragonFlowMonitor(directory)
    dragon_quality = None
    future, next_poll, awake = None, 0.0, None
    next_status = 0.0
    was_push = False
    try:
        while True:
            now = dt.datetime.now(TZ)
            c = dict(DEFAULTS, **read(config_path, {}))
            engine.c = box.c = c
            enabled, push_enabled = modes(c)
            if push_enabled and not was_push:
                # Re-evaluate current fresh conditions when phone delivery becomes available.
                engine.state['latches'] = {}
            was_push = push_enabled
            sid = session(now)
            trading_day, calendar_reason = is_trading_day(now.date())
            if enabled and sid and awake is None:
                awake = subprocess.Popen(['/usr/bin/caffeinate', '-i', '-w', str(os.getpid())])
            elif (not enabled or not sid) and awake is not None:
                awake.terminate()
                awake.wait()
                awake = None
            status = '未启用：等待手机验收' if not enabled else ('监控中' if sid else calendar_reason if not trading_day else '休市时段')
            if enabled and not push_enabled:
                status = '本机观察已开启；手机提醒未启用｜' + status
            if push_enabled:
                box.pump(now.timestamp())
            # A separate future prevents slow individual fund-flow requests from
            # delaying sector discovery, quotes, evaluation, or their notices.
            try:
                dragon_signals, dragon_quality = dragon_flow.pump(now, enabled)
                record_dragon_notices(directory, box, dragon_flow, dragon_signals)
            except Exception as exc:
                logging.warning('dragon flow failed type=%s', type(exc).__name__)
                dragon_quality = dict(status='unknown', reason='资金监控暂不可用；板块监控继续')
            if future and future.done():
                try:
                    u, quotes, errors = future.result()
                    if u != universe:
                        universe = u
                        save(directory / 'universe.json', u)
                    if enabled and sid:
                        events, quality = engine.evaluate(now, quotes, universe['members'])
                        degraded = bool(errors) or quality['status'] == 'degraded'
                        quality['errors'] = errors
                        if errors:
                            quality['status'] = 'degraded'
                        notice = health(engine.state, degraded, now.timestamp(), label)
                        if notice:
                            record_notice(directory, box, push_enabled, *notice, now.timestamp())
                        if events and all_sectors:
                            bodies = format_alerts(events, quality)
                            for index, body in enumerate(bodies, 1):
                                title = '全板块异动｜' + now.strftime('%H:%M:%S')
                                if len(bodies) > 1:
                                    title += f'｜{index}/{len(bodies)}'
                                record_notice(directory, box, push_enabled, title, body, now.timestamp(),
                                              signals=events if index == 1 else [])
                            logging.info('alert rules=%s', [e['key'] for e in events])
                        elif events:
                            board_move = quality['board_move']
                            body = '\n'.join(f'{e["kind"]}｜{e["name"]}｜{e["detail"]}' for e in events)
                            body += '\n板块近5分钟：' + (f'{board_move:+.2f}%' if board_move is not None else '窗口未就绪')
                            body += '\n主要异动：' + '；'.join(quality['leaders'])
                            body += '\n行情时间：' + str(quality['quote_time']) + '\n来源：东方财富、腾讯财经'
                            record_notice(directory, box, push_enabled, '游戏板块异动｜' + now.strftime('%H:%M:%S'), body[:1100], now.timestamp())
                            logging.info('alert rules=%s', [e['key'] for e in events])
                        save(state_path, engine.state)
                except Exception as exc:
                    logging.warning('collection failed type=%s', type(exc).__name__)
                    if enabled and sid:
                        engine.state['last_quality'] = dict(status='degraded', scope=c.get('scope'),
                            errors=['本轮行情采集失败：' + type(exc).__name__], quote_time=None)
                        notice = health(engine.state, True, now.timestamp(), label)
                        if notice:
                            record_notice(directory, box, push_enabled, *notice, now.timestamp())
                        save(state_path, engine.state)
                future = None
            # Refresh membership before open; never use a previous day's list for live scans.
            premarket = trading_day and dt.time(9, 15) <= now.time().replace(tzinfo=None) < dt.time(9, 30)
            need_universe = universe.get('date') != str(now.date())
            if enabled and (sid or (premarket and need_universe)) and future is None and time.monotonic() >= next_poll:
                day = now.date()
                current = universe
                def collect(day=day, current=current, sid=sid):
                    u = feed.universe(day) if current.get('date') != str(day) else current
                    quotes, errors = feed.snapshot(u) if sid else ({}, [])
                    if all_sectors and any('名单变化' in e for e in errors):
                        # Retry discovery next cycle, keeping valid existing indexes usable now.
                        u = dict(u, date=None)
                    return u, quotes, errors
                future = executor.submit(collect)
                next_poll = time.monotonic() + c['poll_seconds']
            if not sid:
                engine.state['fault_since'] = None
                engine.state['fault_notified'] = False
            if enabled and sid and engine.state.get('fault_since') is not None:
                status = '数据异常：相关规则暂停' + ('；手机提醒未启用' if not push_enabled else '')
            if time.monotonic() >= next_status:
                save(directory / 'status.json', dict(status=status, heartbeat=now.isoformat(),
                    enabled=bool(enabled), phone_alerts_enabled=push_enabled, membership_date=universe.get('date'),
                    scope=c.get('scope', 'game'), sector_count=len(universe.get('members', {})) if all_sectors else 1,
                    category_counts=universe.get('counts'), poll_seconds=c['poll_seconds'],
                    quality=engine.state.get('last_quality'), pending_pushes=len(box.jobs),
                    dragon_fund_flow={key: dragon_quality.get(key) for key in
                        ('status', 'reason', 'tracked_count', 'fresh_count')} if dragon_quality else None))
                next_status = time.monotonic() + 5
            time.sleep(1)
    finally:
        if awake:
            awake.terminate()
        executor.shutdown(wait=False)
        dragon_flow.close()
        box.pool.shutdown(wait=False)


def install(config_path):
    directory = config_path.parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    if not config_path.exists():
        c = dict(DEFAULTS, ntfy_topic='game-' + secrets.token_hex(20))
        save(config_path, c)
    agent = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
    agent.parent.mkdir(parents=True, exist_ok=True)
    definition = dict(Label=LABEL, ProgramArguments=[sys.executable, str(Path(__file__).resolve()),
        '--config', str(config_path), 'run'], RunAtLoad=True, KeepAlive=True, ThrottleInterval=15,
        WorkingDirectory=str(ROOT), StandardOutPath=str(directory / 'launch.log'),
        StandardErrorPath=str(directory / 'launch-error.log'))
    agent.write_bytes(plistlib.dumps(definition))
    os.chmod(agent, 0o600)
    domain = f'gui/{os.getuid()}'
    subprocess.run(['launchctl', 'bootout', domain, str(agent)], capture_output=True)
    subprocess.run(['launchctl', 'bootstrap', domain, str(agent)], check=True)
    print('后台服务已安装；正式监控等待手机验收。配置：' + str(config_path))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=CONFIG)
    parser.add_argument('command', choices=['install', 'run', 'status', 'probe', 'test-push', 'enable', 'observe', 'disable', 'subscription'])
    parser.add_argument('--phone-verified', action='store_true', help='仅在用户确认Wi-Fi、移动网络及锁屏振动验收后使用')
    args = parser.parse_args()
    if args.command == 'install':
        return install(args.config)
    c = read(args.config, dict(DEFAULTS))
    if args.command == 'run':
        return run(args.config)
    if args.command == 'status':
        print(json.dumps(read(args.config.parent / 'status.json', {'status': '未安装'}), ensure_ascii=False, indent=2))
    elif args.command == 'subscription':
        print(c['ntfy_server'] + '/' + c['ntfy_topic'])
    elif args.command == 'probe':
        if c.get('scope') == 'all_sectors':
            from sector_monitor import SectorFeed
            feed = SectorFeed()
        else:
            feed = Feed()
        u = feed.universe(dt.datetime.now(TZ).date())
        quotes, errors = feed.snapshot(u)
        age = {k: round(time.time() - v['ts']) for k, v in quotes.items()}
        print(json.dumps(dict(universe=u, quote_count=len(quotes), age_seconds=age, errors=errors), ensure_ascii=False, indent=2))
    elif args.command == 'test-push':
        if not c['ntfy_topic']:
            parser.error('请先install生成主题')
        stamp = dt.datetime.now(TZ).isoformat(timespec='seconds')
        mid = publish(c, '游戏监控·振动测试', f'发送时间：{stamp}\n请确认收到通知并发生振动。本消息为测试，正式监控尚未因此启用。')
        save(args.config.parent / 'last-test.json', dict(sent_at=stamp, server_message_id=mid, phone_verified=False))
        print('ntfy服务已接收测试；手机接收与振动仍待确认。')
    elif args.command == 'enable':
        if not args.phone_verified:
            parser.error('需要手机实测确认后使用 --phone-verified')
        if not (args.config.parent / 'last-test.json').exists():
            parser.error('尚未成功发送测试消息')
        c.update(enabled=True, phone_verified=True, observe_only=False, verified_at=dt.datetime.now(TZ).isoformat())
        save(args.config, c)
        print('正式监控已启用。')
    elif args.command == 'observe':
        c['observe_only'] = True
        save(args.config, c)
        print('本机行情观察与异动记录已开启；手机提醒仍需实机验收。')
    elif args.command == 'disable':
        c['enabled'] = False
        c['observe_only'] = False
        save(args.config, c)
        print('监控已关闭。')


if __name__ == '__main__':
    main()
