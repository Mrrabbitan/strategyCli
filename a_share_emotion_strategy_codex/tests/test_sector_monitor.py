import datetime as dt
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from game_monitor import DEFAULTS, TZ
from sector_monitor import SectorEngine, SectorFeed, format_alerts

START = dt.datetime(2026, 9, 14, 9, 30, tzinfo=TZ)
MEMBERS = {'BK001': '银行', 'BK002': '算力', 'BK003': '游戏'}


def quote(now, price=100, amount=100, name='银行'):
    return dict(ts=now.timestamp(), price=price, amount=amount, name=name,
                day_pct=-1.2, source='test', categories=['行业'])


def replay(sign=1, move=1):
    engine = SectorEngine(dict(DEFAULTS))
    events = []
    for sec in range(0, 301, 30):
        now = START + dt.timedelta(seconds=sec)
        current, quality = engine.evaluate(now, {
            code: quote(now, 100 + sign * move * sec / 300, amount=100 + sec, name=name)
            for code, name in MEMBERS.items()}, MEMBERS)
        events.extend(current)
    return engine, events, quality


class SectorRules(unittest.TestCase):
    def test_all_sectors_both_directions_and_exact_threshold(self):
        for sign, direction in ((1, '拉升'), (-1, '下跌')):
            engine, events, quality = replay(sign)
            self.assertEqual(len(events), 3)
            self.assertEqual({e['code'] for e in events}, set(MEMBERS))
            self.assertTrue(all(e['direction'] == direction and e['seconds'] == 300 for e in events))
            self.assertEqual(quality['fresh_count'], 3)
            self.assertEqual(quality['window_coverage'], 1)
        self.assertEqual(replay(move=0.999)[1], [])

    def test_one_minute_without_five_minute_warmup(self):
        engine = SectorEngine(dict(DEFAULTS))
        for sec in (0, 30, 60):
            now = START + dt.timedelta(seconds=sec)
            events, quality = engine.evaluate(now, {'BK001': quote(now, 100 + sec / 120)}, MEMBERS)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['seconds'], 60)
        self.assertEqual(quality['window_coverage'], 0)

    def test_missing_sector_does_not_suppress_valid_signal(self):
        engine, _, _ = replay()
        engine.state['latches'] = {}
        now = START + dt.timedelta(minutes=5)
        events, quality = engine.evaluate(now, {'BK001': quote(now, 101, 400)}, MEMBERS)
        self.assertEqual(len(events), 1)
        self.assertEqual(quality['status'], 'degraded')
        self.assertEqual(quality['missing_codes'], ['BK002', 'BK003'])

    def test_stale_future_nan_zero_and_unknown(self):
        engine = SectorEngine(dict(DEFAULTS))
        for q in (quote(START - dt.timedelta(seconds=91)), quote(START + dt.timedelta(seconds=1)),
                  quote(START, float('nan')), quote(START, 0), quote(START, amount=-1)):
            events, quality = engine.evaluate(START, {'BK001': q, 'UNKNOWN': quote(START)}, MEMBERS)
            self.assertEqual(events, [])
            self.assertEqual(quality['fresh_count'], 0)

    def test_restart_dedup_cooldown_and_reversal(self):
        engine, _, _ = replay()
        engine = SectorEngine(dict(DEFAULTS), json.loads(json.dumps(engine.state)))
        now = START + dt.timedelta(minutes=5)
        quotes = {code: quote(now, 101, 400) for code in MEMBERS}
        self.assertEqual(engine.evaluate(now, quotes, MEMBERS)[0], [])
        for latch in engine.state['latches'].values():
            latch['last'] = now.timestamp() - 601
        self.assertEqual(engine.evaluate(now, quotes, MEMBERS)[0], [])
        for latch in engine.state['latches'].values():
            latch['active'] = False
        self.assertEqual(len(engine.evaluate(now, quotes, MEMBERS)[0]), 3)
        for latch in engine.state['latches'].values():
            latch['active'] = False
        self.assertEqual(engine.evaluate(now, quotes, MEMBERS)[0], [])
        seen = []
        for sec in (330, 360, 390):
            now = START + dt.timedelta(seconds=sec)
            current, _ = engine.evaluate(now, {c: quote(now, 101 - (sec - 300) / 60, 100 + sec) for c in MEMBERS}, MEMBERS)
            seen.extend(current)
        self.assertTrue(any(e['direction'] == '下跌' for e in seen))

    def test_lunch_weekend_and_large_gap(self):
        engine, _, _ = replay()
        self.assertEqual(engine.evaluate(START.replace(day=12), {}, MEMBERS)[1]['status'], 'closed')
        for now in (START.replace(hour=13, minute=0), START.replace(day=15)):
            events, quality = engine.evaluate(now, {'BK001': quote(now, 150)}, MEMBERS)
            self.assertEqual(events, [])
            self.assertEqual(quality['one_minute_coverage'], 0)
        engine, _, _ = replay()
        now = START + dt.timedelta(minutes=7)
        events, quality = engine.evaluate(now, {'BK001': quote(now, 150, 600)}, MEMBERS)
        self.assertEqual(events, [])
        self.assertEqual(quality['window_coverage'], 0)

    def test_same_timestamp_revision_and_amount_reset(self):
        for price, amount in ((150, 400), (150, 1)):
            engine, _, _ = replay()
            now = START + dt.timedelta(minutes=5)
            self.assertEqual(engine.evaluate(now, {'BK001': quote(now, price, amount)}, MEMBERS)[0], [])

    def test_old_game_history_not_reused(self):
        engine = SectorEngine(dict(DEFAULTS), {'session': 'old', 'history': {'board': []}, 'last_quality': {'status': 'healthy'}})
        self.assertEqual(engine.state['history'], {})
        self.assertNotIn('last_quality', engine.state)

    def test_message_batches_retain_every_sector(self):
        _, events, quality = replay()
        all_events = [dict(events[0], code=f'BK{i:04}', name=f'行业{i}') for i in range(100)]
        all_events += [dict(all_events[0], seconds=60, detail='近1分钟+0.50%')]
        bodies = format_alerts(all_events, quality)
        self.assertGreater(len(bodies), 1)
        self.assertTrue(all(len(b) <= 1000 for b in bodies))
        body = '\n'.join(bodies)
        self.assertEqual(body.count('(BK0000)'), 1)
        for i in range(100):
            self.assertIn(f'(BK{i:04})', body)
        self.assertIn('近1分钟+0.50%', body)


def row(code, name='板块'):
    return dict(f12=code, f14=name, f2=100, f6=1000, f3=1, f124=START.timestamp(), source='test')


class SectorData(unittest.TestCase):
    def test_pagination_and_duplicates(self):
        feed = SectorFeed()
        rows = [row(f'BK{i:04}') for i in range(101)]
        def page(kind, page):
            return {'total': 101, 'diff': rows[(page-1)*100:page*100]}, 'test'
        with patch.object(feed, 'page', side_effect=page):
            self.assertEqual(len(feed.category(2)), 101)
        rows[-1] = rows[0]
        with patch.object(feed, 'page', side_effect=page):
            with self.assertRaises(RuntimeError):
                feed.category(2)

    def test_incomplete_list_rejected(self):
        feed = SectorFeed()
        with patch.object(feed, 'page', return_value=({'total': 100, 'diff': [row('BK001')]}, 'test')):
            with self.assertRaises(RuntimeError):
                feed.category(2)

    def test_category_union_and_partial_feed(self):
        feed = SectorFeed()
        data = {2: {'BK001': row('BK001')}, 3: {'BK001': row('BK001'), 'BK002': row('BK002')}}
        with patch.object(feed, 'scan', return_value=(data, [])):
            universe = feed.universe(START.date())
            self.assertEqual(len(universe['members']), 2)
            self.assertEqual(universe['categories']['BK001'], ['行业', '概念'])
            quotes, errors = feed.snapshot(universe)
            self.assertEqual(set(quotes), {'BK001', 'BK002'})
            self.assertEqual(errors, [])
        with patch.object(feed, 'scan', return_value=({2: data[2]}, ['概念不可用'])):
            quotes, errors = feed.snapshot(universe)
            self.assertEqual(set(quotes), {'BK001'})
            self.assertEqual(errors, ['概念不可用'])
            with self.assertRaises(RuntimeError):
                feed.universe(START.date())

    def test_missing_quote_timestamp_and_universe_drift(self):
        feed = SectorFeed()
        data = {2: {'BK001': row('BK001')}, 3: {'BK002': row('BK002')}}
        with patch.object(feed, 'scan', return_value=(data, [])):
            universe = feed.universe(START.date())
        del data[2]['BK001']['f124']
        data[3]['BK003'] = row('BK003')
        with patch.object(feed, 'scan', return_value=(data, [])):
            quotes, errors = feed.snapshot(universe)
            self.assertEqual(set(quotes), {'BK002'})
            self.assertTrue(any('名单变化' in e for e in errors))


if __name__ == '__main__':
    unittest.main()
