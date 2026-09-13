"""Industry and concept index alerts, hosted by the existing game-monitor service."""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import math
import re

from game_monitor import Feed, TZ, finite, session

KINDS = {2: '行业', 3: '概念'}


class SectorFeed(Feed):
    def page(self, kind, page):
        return self.eastmoney('clist/get', dict(pn=page, pz=100, np=1,
            fs=f'm:90+t:{kind}+f:!50', fields='f2,f3,f6,f12,f14,f124',
            fltt=2, fid='f12', po=0))

    def category(self, kind):
        first, source = self.page(kind, 1)
        total = int(first['total'])
        if not 0 < total <= 5000:
            raise RuntimeError('板块数量异常')
        pages = [(first, source)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            pages.extend(pool.map(lambda p: self.page(kind, p), range(2, math.ceil(total / 100) + 1)))
        rows = {}
        for data, host in pages:
            if int(data['total']) != total:
                raise RuntimeError('板块分页期间名单发生变化')
            batch = data.get('diff') or []
            if isinstance(batch, dict):
                batch = list(batch.values())
            for row in batch:
                code = str(row.get('f12', ''))
                if not re.fullmatch(r'BK\d+', code) or code in rows or not row.get('f14'):
                    raise RuntimeError('板块代码重复或无效')
                rows[code] = dict(row, source=host)
        if len(rows) != total:
            raise RuntimeError('板块分页覆盖不完整')
        return rows

    def scan(self):
        groups, errors = {}, []
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            jobs = {kind: pool.submit(self.category, kind) for kind in KINDS}
            for kind, future in jobs.items():
                try:
                    groups[kind] = future.result()
                except Exception as exc:
                    errors.append(KINDS[kind] + '板块行情不可用：' + type(exc).__name__)
        return groups, errors

    def universe(self, day):
        groups, errors = self.scan()
        if errors or len(groups) != len(KINDS):
            raise RuntimeError('行业及概念板块名单未完整取得')
        members, categories = {}, {}
        for kind, rows in groups.items():
            for code, row in rows.items():
                members[code] = row['f14']
                categories.setdefault(code, []).append(KINDS[kind])
        return dict(date=str(day), scope='all_sectors', members=members,
                    categories=categories, counts={KINDS[k]: len(v) for k, v in groups.items()},
                    source='东方财富行业及概念板块指数')

    def snapshot(self, universe):
        groups, errors = self.scan()
        quotes = {}
        for kind, rows in groups.items():
            expected = {c for c, kinds in universe['categories'].items() if KINDS[kind] in kinds}
            if set(rows) != expected:
                errors.append(KINDS[kind] + '名单变化，等待刷新；新增板块尚无完整窗口')
            for code, row in rows.items():
                if code not in universe['members']:
                    continue
                try:
                    q = dict(name=row['f14'], price=finite(row['f2']), amount=finite(row['f6']),
                             ts=finite(row['f124']), day_pct=finite(row['f3']),
                             categories=universe['categories'][code], source=row['source'])
                    if code not in quotes or q['ts'] > quotes[code]['ts']:
                        quotes[code] = q
                except (KeyError, ValueError, TypeError):
                    continue
        return quotes, errors


class SectorEngine:
    def __init__(self, config, state=None):
        self.c = config
        self.state = state or {}
        if self.state.get('scope') != 'all_sectors':
            self.state = {'scope': 'all_sectors'}
        self.state.setdefault('history', {})
        self.state.setdefault('latches', {})

    def evaluate(self, now, quotes, expected):
        sid, epoch = session(now), now.timestamp()
        if not sid:
            return [], {'status': 'closed'}
        if self.state.get('session') != sid:
            self.state.update(session=sid, history={}, latches={})
        hist, fresh = self.state['history'], {}
        for code, q in quotes.items():
            if code not in expected:
                continue
            try:
                if (not all(math.isfinite(q[k]) for k in ('price', 'amount', 'ts')) or
                        q['price'] <= 0 or q['amount'] < 0 or
                        not 0 <= epoch - q['ts'] <= self.c['stale_seconds'] or
                        session(dt.datetime.fromtimestamp(q['ts'], TZ)) != sid):
                    continue
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            rows = hist.setdefault(code, [])
            # Revised/out-of-order quotes must rebuild the window, never create a false jump.
            if rows and (q['ts'] < rows[-1]['ts'] or q['amount'] < rows[-1]['amount'] or
                         (q['ts'] == rows[-1]['ts'] and q['price'] != rows[-1]['price'])):
                rows.clear()
            if not rows or q['ts'] > rows[-1]['ts']:
                rows.append(q)
            hist[code] = [r for r in rows if r['ts'] >= epoch - 450]
            fresh[code] = q

        windows = ((60, self.c.get('sector_1m_pct', 0.5)),
                   (300, self.c.get('sector_5m_pct', 1.0)))
        ready = {seconds: set() for seconds, _ in windows}
        moves, events = {}, []
        for code, end in fresh.items():
            rows = hist[code]
            for seconds, threshold in windows:
                target = end['ts'] - seconds
                anchors = [r for r in rows if target - 40 <= r['ts'] <= target]
                if not anchors:
                    continue
                start = anchors[-1]
                selected = [r for r in rows if start['ts'] <= r['ts'] <= end['ts']]
                if any(b['ts'] - a['ts'] > self.c['stale_seconds'] for a, b in zip(selected, selected[1:])):
                    continue
                ready[seconds].add(code)
                move = (end['price'] / start['price'] - 1) * 100
                moves.setdefault(code, {})[str(seconds)] = move
                for sign, direction in ((1, '拉升'), (-1, '下跌')):
                    key = f'sector:{code}:{seconds}:{sign}'
                    latch = self.state['latches'].setdefault(key, {'active': False, 'last': -1e20})
                    condition = move * sign >= threshold - 1e-9
                    if condition and not latch['active'] and epoch - latch['last'] >= self.c['cooldown_seconds']:
                        events.append(dict(key=key, code=code, name=end['name'], kind='板块快速' + direction,
                            direction=direction, seconds=seconds, move_pct=move, day_pct=end.get('day_pct'),
                            amount_cny=end['amount'], quote_time=dt.datetime.fromtimestamp(end['ts'], TZ).isoformat(),
                            window_start=dt.datetime.fromtimestamp(start['ts'], TZ).isoformat(),
                            window_seconds=end['ts'] - start['ts'], categories=end.get('categories', []),
                            source=end.get('source'), detail=f'近{seconds // 60}分钟{move:+.2f}%'))
                        latch['last'] = epoch
                    latch['active'] = condition
        expected_count = len(expected)
        coverage = len(fresh) / expected_count if expected_count else 0
        quality = dict(status='healthy' if expected_count and len(fresh) == expected_count else 'degraded',
            scope='all_sectors', expected_count=expected_count, fresh_count=len(fresh),
            fresh_coverage=coverage, window_coverage=len(ready[300]) / expected_count if expected_count else 0,
            one_minute_coverage=len(ready[60]) / expected_count if expected_count else 0,
            missing_codes=sorted(set(expected) - set(fresh)),
            quote_time=dt.datetime.fromtimestamp(min(q['ts'] for q in fresh.values()), TZ).isoformat() if fresh else None)
        self.state['last_quality'] = quality
        return events, quality


def format_alerts(events, quality):
    """Combine one index's simultaneous windows; retain every distinct sector and direction."""
    groups = {}
    for event in events:
        groups.setdefault((event['code'], event['direction']), []).append(event)
    lines = []
    for group in sorted(groups.values(), key=lambda g: max(abs(e['move_pct']) for e in g), reverse=True):
        e = group[0]
        detail = '、'.join(x['detail'] for x in group)
        clock = e['quote_time'][11:19]
        daily = f'；今日{e["day_pct"]:+.2f}%' if e.get('day_pct') is not None else ''
        lines.append(f'{e["name"]}({e["code"]})｜{e["direction"]}｜{detail}{daily}｜行情{clock}')
    footer = (f'\n扫描覆盖：{quality["fresh_count"]}/{quality["expected_count"]}；来源：东方财富'
              '\n异动提醒不等于买卖信号。')
    batches, current = [], ''
    for line in lines:
        if current and len(current) + len(line) + len(footer) + 1 > 1000:
            batches.append(current + footer)
            current = ''
        current += ('\n' if current else '') + line
    if current:
        batches.append(current + footer)
    return batches
