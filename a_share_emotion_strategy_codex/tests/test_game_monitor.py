import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from game_monitor import DEFAULTS, Engine, Feed, Outbox, TZ, health, session, save, read, publish, modes, record_notice

START = dt.datetime(2026, 9, 7, 9, 30, tzinfo=TZ)
MEMBERS = {'002001': 'A', '002002': 'B', '002003': 'C'}


def quote(t, p=100, amount=0):
    return dict(name='test', ts=t.timestamp(), price=p, amount=amount)


def build(sign=1, stock=3, board=1, multiplier=2):
    engine = Engine(dict(DEFAULTS))
    events = []
    for sec in range(0, 1201, 30):
        now = START + dt.timedelta(seconds=sec)
        move = 0 if sec < 930 else (sec - 900) / 300
        amount = min(sec, 900) * 100 + max(0, sec - 900) * 100 * multiplier
        quotes = {c: quote(now, 100 + sign * stock * move, amount) for c in MEMBERS}
        quotes['board'] = quote(now, 100 + sign * board * move)
        current, quality = engine.evaluate(now, quotes, MEMBERS)
        events += current
    return engine, events, quotes


class Rules(unittest.TestCase):
    def test_observation_does_not_publish_or_queue(self):
        self.assertEqual(modes(DEFAULTS), (False, False))
        self.assertEqual(modes(dict(DEFAULTS, enabled=True)), (False, False))
        self.assertEqual(modes(dict(DEFAULTS, observe_only=True)), (True, False))
        self.assertEqual(modes(dict(DEFAULTS, enabled=True, phone_verified=True)), (True, True))
        with tempfile.TemporaryDirectory() as directory:
            from unittest.mock import Mock
            box = Mock()
            record_notice(Path(directory), box, False, 'test', 'body', 0)
            box.add.assert_not_called()
            self.assertFalse(read(Path(directory) / 'events.json', [])[0]['phone_queued'])
            record_notice(Path(directory), box, True, 'test2', 'body2', 1)
            box.add.assert_called_once_with('test2', 'body2', 1)

    def test_up_down_and_boundaries(self):
        for sign in (1, -1):
            _, events, _ = build(sign)
            self.assertEqual(len(events), 5)
            self.assertEqual({e['kind'] for e in events}, {'集体异动', '板块快速涨跌', '个股放量涨跌'})
            self.assertTrue(all(e['direction'] == ('上涨' if sign == 1 else '下跌') for e in events))

    def test_below_price_and_volume(self):
        _, events, _ = build(stock=2.99, board=0.99)
        self.assertEqual([e['kind'] for e in events], ['集体异动'])
        _, events, _ = build(multiplier=1.99)
        self.assertNotIn('个股放量涨跌', [e['kind'] for e in events])

    def test_warmup_and_stale(self):
        engine = Engine(dict(DEFAULTS))
        now = START + dt.timedelta(minutes=1)
        events, q = engine.evaluate(now, {c: quote(now) for c in MEMBERS}, MEMBERS)
        self.assertEqual(events, [])
        self.assertEqual(q['window_coverage'], 0)
        events, q = engine.evaluate(now + dt.timedelta(seconds=91), {c: quote(now) for c in MEMBERS}, MEMBERS)
        self.assertEqual(events, [])
        self.assertEqual(q['fresh_coverage'], 0)

    def test_missing_coverage_keeps_individual(self):
        engine, _, quotes = build()
        engine.state['latches'] = {}
        now = START + dt.timedelta(minutes=20)
        quotes.pop('002003')
        events, q = engine.evaluate(now, quotes, MEMBERS)
        self.assertEqual(q['status'], 'degraded')
        self.assertEqual([e['kind'] for e in events], ['个股放量涨跌'] * 2)

    def test_restart_dedup(self):
        engine, _, quotes = build()
        restored = Engine(dict(DEFAULTS), json.loads(json.dumps(engine.state)))
        events, _ = restored.evaluate(START + dt.timedelta(minutes=20), quotes, MEMBERS)
        self.assertEqual(events, [])

    def test_reverse_independent(self):
        engine, _, quotes = build()
        for sec in range(1230, 1531, 30):
            now = START + dt.timedelta(seconds=sec)
            quotes = {c: quote(now, 103 - 7 * (sec - 1200) / 330, 150000 + (sec - 1200) * 500) for c in MEMBERS}
            quotes['board'] = quote(now, 101 - 3 * (sec - 1200) / 330)
            events, _ = engine.evaluate(now, quotes, MEMBERS)
            if any(e['direction'] == '下跌' for e in events):
                return
        self.fail('opposite direction suppressed')

    def test_gap_prevents_window(self):
        engine, _, quotes = build()
        now = START + dt.timedelta(minutes=22)
        quotes = {c: quote(now, 120, 500000) for c in MEMBERS}
        events, q = engine.evaluate(now, quotes, MEMBERS)
        self.assertEqual(events, [])
        self.assertEqual(q['window_coverage'], 0)

    def test_lunch_resets(self):
        engine, _, _ = build()
        now = START.replace(hour=13, minute=0)
        events, q = engine.evaluate(now, {c: quote(now, 150) for c in MEMBERS}, MEMBERS)
        self.assertEqual(events, [])
        self.assertEqual(q['window_coverage'], 0)

    def test_amount_reset_prevents_volume(self):
        engine, _, quotes = build()
        now = START + dt.timedelta(minutes=20, seconds=30)
        quotes = {c: quote(now, 120, 1) for c in MEMBERS}
        events, _ = engine.evaluate(now, quotes, MEMBERS)
        self.assertEqual(events, [])

    def test_calendar(self):
        self.assertIsNone(session(START.replace(day=6)))
        self.assertIsNone(session(START.replace(month=10, day=1)))
        self.assertIsNone(session(START.replace(hour=12)))
        self.assertIsNone(session(START.replace(hour=15)))
        self.assertIsNotNone(session(START))

    def test_rearm_after_clear_and_cooldown(self):
        engine, _, quotes = build()
        now = START + dt.timedelta(minutes=20)
        # A prior alert older than cooldown must still not repeat while active.
        for latch in engine.state['latches'].values():
            latch['last'] = now.timestamp() - 601
        self.assertEqual(engine.evaluate(now, quotes, MEMBERS)[0], [])
        for latch in engine.state['latches'].values():
            latch['active'] = False
        self.assertEqual(len(engine.evaluate(now, quotes, MEMBERS)[0]), 5)
        for latch in engine.state['latches'].values():
            latch['active'] = False
        self.assertEqual(engine.evaluate(now, quotes, MEMBERS)[0], [])

    def test_fault_and_recovery(self):
        state = {}
        self.assertIsNone(health(state, True, 0))
        self.assertIsNone(health(state, True, 179))
        self.assertIsNotNone(health(state, True, 180))
        self.assertIsNone(health(state, True, 240))
        self.assertIsNotNone(health(state, False, 250))
        self.assertIsNone(health(state, False, 260))

    def test_queue_retry_and_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            box = Outbox(DEFAULTS, Path(directory))
            try:
                box.add('title', 'body', 0)
                with patch('game_monitor.publish', side_effect=RuntimeError):
                    box.pump(0)
                    try: box.future.result(timeout=2)
                    except RuntimeError: pass
                    box.pump(1)
                    self.assertEqual(box.jobs[0]['due'], 6)
                    box.pump(6)
                    try: box.future.result(timeout=2)
                    except RuntimeError: pass
                    box.pump(7)
                    self.assertEqual(box.jobs[0]['due'], 22)
                    box.pump(22)
                    try: box.future.result(timeout=2)
                    except RuntimeError: pass
                    box.pump(23)
                    self.assertEqual(box.jobs, [])
                box.add('title', 'body', 0)
                box.pump(121)
                self.assertEqual(box.jobs, [])
            finally:
                box.pool.shutdown()

    def test_queue_persistence_and_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            save(path / 'outbox.json', [dict(id='test', title='t', message='m', created=0, due=0, attempt=0)])
            box = Outbox(DEFAULTS, path)
            try:
                with patch('game_monitor.publish', return_value='accepted'):
                    box.pump(1)
                    box.future.result(timeout=2)
                    box.pump(2)
                self.assertEqual(read(path / 'outbox.json', None), [])
            finally:
                box.pool.shutdown()

    def test_board_missing_keeps_cluster_and_stocks(self):
        engine, _, quotes = build()
        engine.state['latches'] = {}
        quotes.pop('board')
        events, quality = engine.evaluate(START + dt.timedelta(minutes=20), quotes, MEMBERS)
        self.assertEqual(quality['status'], 'degraded')
        self.assertEqual(len(events), 4)
        self.assertNotIn('板块快速涨跌', [e['kind'] for e in events])

    def test_board_requires_breadth(self):
        engine, _, quotes = build()
        engine.state['latches'] = {}
        for code in ('002001', '002002'):
            quotes[code] = dict(quotes[code], price=99)
        events, _ = engine.evaluate(START + dt.timedelta(minutes=20), quotes, MEMBERS)
        self.assertNotIn('板块快速涨跌', [e['kind'] for e in events])

    def test_future_and_nan_rejected(self):
        engine = Engine(dict(DEFAULTS))
        quotes = {'002001': quote(START + dt.timedelta(seconds=1)),
                  '002002': quote(START, p=float('nan')),
                  '002003': quote(START, p=0)}
        events, quality = engine.evaluate(START, quotes, MEMBERS)
        self.assertEqual(events, [])
        self.assertEqual(quality['fresh_coverage'], 0)

    def test_ntfy_payload_and_ack(self):
        config = dict(DEFAULTS, ntfy_topic='test', ntfy_token='secret')
        with patch('game_monitor.request', return_value=b'{"id":"abc","event":"message"}') as mock:
            self.assertEqual(publish(config, '异动', '消息'), 'abc')
            url, body, headers = mock.call_args.args
            self.assertEqual(json.loads(body)['priority'], 4)
            self.assertEqual(json.loads(body)['topic'], 'test')
            self.assertEqual(headers['Authorization'], 'Bearer secret')
        with patch('game_monitor.request', return_value=b'{"error":"no"}'):
            with self.assertRaises(RuntimeError):
                publish(config, 'title', 'message')

    def test_listing_pagination(self):
        feed = Feed()
        with patch.object(feed, 'eastmoney', side_effect=[({'total': 2, 'diff': [{'f12': 'a'}]}, 'x'), ({'total': 2, 'diff': [{'f12': 'b'}]}, 'x')]):
            self.assertEqual(len(feed.listing('s')[0]), 2)


if __name__ == '__main__':
    unittest.main()
