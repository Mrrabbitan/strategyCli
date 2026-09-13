import concurrent.futures
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dragon_fund_flow import (DragonFlowEngine, DragonFlowFeed, DragonFlowMonitor,
                              ENDPOINT, TZ, load_members, monitoring_members, parse_flow)
from game_monitor import record_dragon_notices

NOW = dt.datetime(2026, 9, 14, 9, 31, tzinfo=TZ)
MEMBERS = {'600001': dict(code='600001', name='虚构观察甲', origins=['observed'])}


def document(expires='2026-09-15', observed=None, pins=None):
    return {'monitoring': {'valid_until': expires,
            'observed': observed if observed is not None else [{'code': '600001', 'name': '虚构观察甲'}],
            'pins': pins or []}}


def snapshot(now=NOW, value=-100):
    return dict(code='600001', provider='eastmoney', source_ts=now.timestamp(),
                source_asof=now.isoformat(), fetched_at=now.isoformat(),
                period='day_to_source_asof', main_net_cny=value,
                main_net_pct=None, source_url=ENDPOINT)


def payload(rows=None):
    return dict(rc=0, data=dict(code='600001', market=1, name='虚构观察甲',
        tradePeriods=dict(periods=[dict(b=202609140930, e=202609141130),
                                  dict(b=202609141300, e=202609141500)]),
        klines=rows or ['2026-09-14 09:30,-50,20,30,-70,20',
                       '2026-09-14 09:31,-100,40,60,-130,30']))


class MembershipTests(unittest.TestCase):
    def test_only_current_observed_and_confirmed_unexpired_pins(self):
        d = document(pins=[{'code': '002001', 'name': '虚构跟踪乙', 'confirmed': True, 'valid_until': '2026-09-14'},
                           {'code': '002002', 'name': '未确认', 'confirmed': False, 'valid_until': '2026-09-15'},
                           {'code': '002003', 'name': '已过期', 'confirmed': True, 'valid_until': '2026-09-13'}])
        d['stocks'] = [{'code': '600999', 'name': '仅研究未跟踪'}]
        members, info = monitoring_members(d, NOW.date())
        self.assertEqual(set(members), {'600001', '002001'})
        self.assertEqual(info['status'], 'valid')

    def test_expired_is_not_normal_empty(self):
        self.assertEqual(monitoring_members(document('2026-09-13'), NOW.date())[1]['status'], 'expired')
        members, info = monitoring_members(document(observed=[]), NOW.date())
        self.assertEqual(members, {})
        self.assertEqual(info['status'], 'valid')

    def test_valid_pins_survive_expired_observations(self):
        members, info = monitoring_members(document('2026-09-13', pins=[
            {'code': '002001', 'name': '虚构跟踪乙', 'confirmed': True, 'valid_until': '2026-09-15'}]), NOW.date())
        self.assertEqual(set(members), {'002001'})
        self.assertEqual(info['status'], 'partial')

    def test_invalid_rows_and_missing_dates_fail_closed(self):
        d = document(None, pins=[{'code': '002001', 'name': '无有效期', 'confirmed': True}])
        self.assertEqual(monitoring_members(d, NOW.date())[0], {})
        d = document(observed=[{'code': '300001', 'name': '非主板'}, {'code': '600001', 'name': ''},
                               {'code': 600001, 'name': '非字符串'}])
        members, info = monitoring_members(d, NOW.date())
        self.assertFalse(members)
        self.assertEqual(info['status'], 'invalid')

    def test_duplicate_members_do_not_double_monitor(self):
        d = document(pins=[{'code': '600001', 'name': '虚构观察甲', 'confirmed': True, 'valid_until': '2026-09-15'}])
        members, _ = monitoring_members(d, NOW.date())
        self.assertEqual(len(members), 1)
        self.assertEqual(members['600001']['origins'], ['observed', 'pin'])

    def test_missing_and_malformed_files(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / 'current.json'
            self.assertEqual(load_members(NOW, p)[1]['status'], 'missing')
            p.write_text('{bad')
            self.assertEqual(load_members(NOW, p)[1]['status'], 'invalid')


class FeedTests(unittest.TestCase):
    def test_latest_cumulative_not_sum_and_source_time(self):
        quote = parse_flow(payload(), '600001', NOW + dt.timedelta(seconds=15))
        self.assertEqual(quote['main_net_cny'], -100)
        self.assertNotEqual(quote['main_net_cny'], -150)
        self.assertEqual(quote['source_ts'], NOW.timestamp())
        self.assertIsNone(quote['main_net_pct'])
        self.assertEqual(quote['fetched_at'], (NOW + dt.timedelta(seconds=15)).isoformat())

    def test_reject_code_market_schema_and_arithmetic_mismatch(self):
        for change in (lambda p: p.update(rc=1), lambda p: p['data'].update(code='600002'),
                       lambda p: p['data'].update(market=0), lambda p: p['data'].update(klines=[]),
                       lambda p: p['data'].update(tradePeriods={}),
                       lambda p: p['data']['tradePeriods']['periods'][0].update(b=202609130930)):
            p = payload()
            change(p)
            with self.assertRaises(ValueError):
                parse_flow(p, '600001', NOW)
        for row in ('2026-09-14 09:31,1,0,0,2,3', '2026-09-14 09:31,nan,0,0,0,0',
                    '09:31,-1,0,0,-1,0', '2026-09-14 09:31,--,0,0,-1,0'):
            with self.assertRaises(ValueError):
                parse_flow(payload([row]), '600001', NOW)

    def test_reject_conflicting_duplicate_mixed_date_and_reverse_order(self):
        good = payload()['data']['klines']
        for rows in (list(reversed(good)), good + ['2026-09-14 09:31,1,0,0,1,0'],
                     ['2026-09-13 09:30,-1,0,0,-1,0'] + good):
            with self.assertRaises(ValueError):
                parse_flow(payload(rows), '600001', NOW)

    def test_endpoint_is_single_stock_and_redirect_preserved(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps(payload()).encode()
        response.geturl.return_value = 'https://push2delay.eastmoney.com/api/qt/stock/fflow/kline/get'
        with patch('dragon_fund_flow.urllib.request.urlopen', return_value=response) as call:
            quote = DragonFlowFeed().one(MEMBERS['600001'])
        url = call.call_args.args[0].full_url
        self.assertIn('secid=1.600001', url)
        self.assertIn('klt=1', url)
        self.assertNotIn('clist', url)
        self.assertTrue(quote['delayed_transport'])

    def test_one_stock_failure_does_not_remove_valid_other(self):
        feed = DragonFlowFeed()
        members = dict(MEMBERS, **{'002001': dict(code='002001', name='虚构乙', origins=['pin'])})
        def one(member):
            if member['code'] == '002001':
                raise TimeoutError('synthetic timeout')
            return snapshot()
        with patch.object(feed, 'one', side_effect=one):
            quotes, errors = feed.snapshot(members)
        self.assertEqual(set(quotes), {'600001'})
        self.assertEqual(set(errors), {'002001'})


class EpisodeTests(unittest.TestCase):
    def test_first_negative_persistent_negative_recovery_and_next_episode(self):
        engine = DragonFlowEngine()
        types = []
        for minute, value in enumerate((-0.01, -200, -50, 0, 10, -1)):
            now = NOW + dt.timedelta(minutes=minute)
            events, _ = engine.evaluate(now, {'600001': snapshot(now, value)}, MEMBERS)
            types.extend(e['type'] for e in events)
        self.assertEqual(types, ['dragon_fund_outflow', 'dragon_fund_recovery', 'dragon_fund_outflow'])
        self.assertEqual(engine.state['stocks']['600001']['episode'], 2)
        self.assertTrue(all(e['scope'] == 'dragon_watchlist' for e in engine.state['pending_signals']))

    def test_first_nonnegative_has_no_recovery_event(self):
        self.assertEqual(DragonFlowEngine().evaluate(NOW, {'600001': snapshot(value=0)}, MEMBERS)[0], [])

    def test_stale_future_wrong_session_and_previous_day_do_not_trigger(self):
        for now, quote_time in ((NOW, NOW - dt.timedelta(seconds=91)), (NOW, NOW + dt.timedelta(seconds=1)),
                                (NOW, NOW - dt.timedelta(days=1)),
                                (NOW.replace(hour=13, minute=0), NOW.replace(hour=11, minute=29))):
            events, rows = DragonFlowEngine().evaluate(now, {'600001': snapshot(quote_time)}, MEMBERS)
            self.assertEqual(events, [])
            self.assertEqual(rows[0]['status'], 'unavailable')

    def test_exact_90_seconds_is_accepted(self):
        self.assertEqual(len(DragonFlowEngine().evaluate(NOW + dt.timedelta(seconds=90),
                         {'600001': snapshot()}, MEMBERS)[0]), 1)

    def test_restart_missing_and_stale_cannot_rearm(self):
        engine = DragonFlowEngine()
        engine.evaluate(NOW, {'600001': snapshot()}, MEMBERS)
        restored = DragonFlowEngine(json.loads(json.dumps(engine.state)))
        later = NOW + dt.timedelta(minutes=5)
        self.assertEqual(restored.evaluate(later, {}, MEMBERS)[0], [])
        restored.evaluate(later, {'600001': snapshot(NOW, 1)}, MEMBERS)
        self.assertEqual(restored.evaluate(later, {'600001': snapshot(later, -1)}, MEMBERS)[0], [])

    def test_lunch_preserves_episode_cross_day_starts_new_episode(self):
        engine = DragonFlowEngine()
        engine.evaluate(NOW, {'600001': snapshot()}, MEMBERS)
        self.assertEqual(engine.evaluate(NOW.replace(hour=12), {}, MEMBERS)[0], [])
        afternoon = NOW.replace(hour=13)
        self.assertEqual(engine.evaluate(afternoon, {'600001': snapshot(afternoon)}, MEMBERS)[0], [])
        tomorrow = NOW + dt.timedelta(days=1)
        events, _ = engine.evaluate(tomorrow, {'600001': snapshot(tomorrow)}, MEMBERS)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['episode'], 1)
        self.assertIn('2026-09-15', events[0]['event_id'])

    def test_removed_readded_same_day_does_not_realert(self):
        engine = DragonFlowEngine()
        engine.evaluate(NOW, {'600001': snapshot()}, MEMBERS)
        engine.evaluate(NOW + dt.timedelta(minutes=1), {}, {})
        later = NOW + dt.timedelta(minutes=2)
        self.assertEqual(engine.evaluate(later, {'600001': snapshot(later)}, MEMBERS)[0], [])

    def test_new_day_nonnegative_does_not_claim_previous_day_recovery(self):
        engine = DragonFlowEngine()
        engine.evaluate(NOW, {'600001': snapshot()}, MEMBERS)
        tomorrow = NOW + dt.timedelta(days=1)
        self.assertEqual(engine.evaluate(tomorrow, {'600001': snapshot(tomorrow, 10)}, MEMBERS)[0], [])
        self.assertFalse(engine.state['stocks']['600001']['negative_active'])

    def test_same_timestamp_revision_and_backwards_time_cannot_rearm(self):
        engine = DragonFlowEngine()
        engine.evaluate(NOW, {'600001': snapshot()}, MEMBERS)
        for stamp in (NOW, NOW - dt.timedelta(seconds=30)):
            events, rows = engine.evaluate(NOW, {'600001': snapshot(stamp, 1)}, MEMBERS)
            self.assertFalse(events)
            self.assertEqual(rows[0]['status'], 'unavailable')
        later = NOW + dt.timedelta(minutes=1)
        self.assertEqual(engine.evaluate(later, {'600001': snapshot(later)}, MEMBERS)[0], [])

    def test_off_scope_bad_provider_and_nonfinite_are_not_observations(self):
        q = snapshot()
        for field, value in (('provider', 'ths'), ('main_net_cny', float('nan')), ('period', 'minute_delta')):
            bad = dict(q, **{field: value})
            self.assertEqual(DragonFlowEngine().evaluate(NOW, {'600001': bad}, MEMBERS)[0], [])
        self.assertEqual(DragonFlowEngine().evaluate(NOW, {'600999': q}, MEMBERS)[0], [])


class ManualExecutor:
    def __init__(self):
        self.jobs = []
    def submit(self, *args):
        future = concurrent.futures.Future()
        self.jobs.append((future, args))
        return future
    def shutdown(self, wait=False):
        pass


class IntegrationTests(unittest.TestCase):
    def make_monitor(self, directory, loader=None):
        self.clock = 0
        self.executor = ManualExecutor()
        return DragonFlowMonitor(directory, loader=loader or (lambda now: monitoring_members(document(), now.date())),
                                 executor=self.executor, monotonic=lambda: self.clock)

    def test_independent_future_does_not_wait_or_overlap(self):
        with tempfile.TemporaryDirectory() as d:
            monitor = self.make_monitor(d)
            for sec in (0, 30, 60, 120):
                self.clock = sec
                signals, status = monitor.pump(NOW + dt.timedelta(seconds=sec))
                self.assertEqual(signals, [])
                self.assertTrue(status['inflight'])
            self.assertEqual(len(self.executor.jobs), 1)
            self.executor.jobs[0][0].set_result(({'600001': snapshot(NOW)}, {}))
            signals, status = monitor.pump(NOW + dt.timedelta(seconds=121))
            self.assertFalse(signals)
            self.assertEqual(status['fresh_count'], 0)
            self.assertEqual(len(self.executor.jobs), 2)

    def test_30_second_poll_and_members_removed_while_inflight(self):
        with tempfile.TemporaryDirectory() as d:
            active = [True]
            monitor = self.make_monitor(d, lambda now: monitoring_members(document(observed=None if active[0] else []), now.date()))
            monitor.pump(NOW)
            active[0] = False
            self.executor.jobs[0][0].set_result(({'600001': snapshot()}, {}))
            self.clock = 1
            events, report = monitor.pump(NOW + dt.timedelta(seconds=1))
            self.assertFalse(events)
            self.assertEqual(report['status'], 'empty')
            active[0] = True
            self.clock = 29
            monitor.pump(NOW + dt.timedelta(seconds=29))
            self.assertEqual(len(self.executor.jobs), 1)
            self.clock = 30
            monitor.pump(NOW + dt.timedelta(seconds=30))
            self.assertEqual(len(self.executor.jobs), 2)

    def test_pending_event_survives_restart_and_partial_delivery_without_phone(self):
        with tempfile.TemporaryDirectory() as d:
            directory = Path(d)
            monitor = self.make_monitor(d)
            monitor.pump(NOW)
            self.executor.jobs[0][0].set_result(({'600001': snapshot()}, {}))
            signals, _ = monitor.pump(NOW)
            self.assertEqual(len(json.loads(monitor.state_path.read_text())['pending_signals']), 1)
            box = Mock()
            with patch.object(monitor, 'acknowledge', side_effect=OSError('synthetic crash')):
                with self.assertRaises(OSError):
                    record_dragon_notices(directory, box, monitor, signals)
            restored = self.make_monitor(d)
            pending, _ = restored.pump(NOW)
            record_dragon_notices(directory, box, restored, pending)
            records = json.loads((directory / 'events.json').read_text())
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]['scope'], 'dragon_watchlist')
            self.assertFalse(records[0]['phone_queued'])
            self.assertFalse(restored.engine.state['pending_signals'])
            box.add.assert_not_called()
            self.assertEqual((monitor.state_path.stat().st_mode & 0o777), 0o600)

    def test_expired_membership_corrupt_state_and_disabled_do_not_scan(self):
        with tempfile.TemporaryDirectory() as d:
            monitor = self.make_monitor(d, lambda now: monitoring_members(document('2026-09-13'), now.date()))
            self.assertEqual(monitor.pump(NOW)[1]['status'], 'unknown')
            self.assertEqual(len(self.executor.jobs), 0)
            Path(d, 'dragon-flow-state.json').write_text('{bad')
            monitor = self.make_monitor(d)
            self.assertEqual(monitor.pump(NOW)[1]['status'], 'unknown')
            self.assertEqual(len(self.executor.jobs), 0)
        with tempfile.TemporaryDirectory() as d:
            monitor = self.make_monitor(d)
            self.assertEqual(monitor.pump(NOW, enabled=False)[1]['status'], 'disabled')
            self.assertEqual(len(self.executor.jobs), 0)

    def test_fresh_status_expires_without_successful_next_poll(self):
        with tempfile.TemporaryDirectory() as d:
            monitor = self.make_monitor(d)
            monitor.pump(NOW)
            self.executor.jobs[0][0].set_result(({'600001': snapshot()}, {}))
            self.assertEqual(monitor.pump(NOW)[1]['status'], 'healthy')
            self.clock = 91
            report = monitor.pump(NOW + dt.timedelta(seconds=91))[1]
            self.assertEqual(report['status'], 'degraded')
            self.assertIsNone(report['stocks'][0]['main_net_cny'])
            self.assertEqual(report['stocks'][0]['last_valid_main_net_cny'], -100)

    def test_structurally_corrupt_persisted_state_is_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, 'dragon-flow-state.json').write_text(json.dumps(dict(
                version=1, provider='eastmoney', stocks={'600001': 'corrupt'}, pending_signals=[])))
            monitor = self.make_monitor(d)
            self.assertEqual(monitor.pump(NOW)[1]['status'], 'unknown')
            self.assertFalse(self.executor.jobs)


if __name__ == '__main__':
    unittest.main()
