"""Read-only feed contracts using entirely synthetic local monitor records."""
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import monitor_feed

TZ = monitor_feed.TZ
NOW = dt.datetime(2026, 9, 14, 10, 0, 30, tzinfo=TZ)
SOURCE = NOW - dt.timedelta(seconds=30)


def sector(**changes):
    value = dict(key='sector:BK0001:60:1', code='BK0001', name='虚构行业', kind='板块快速涨跌',
                 quote_time=SOURCE.isoformat(), direction='拉升', seconds=60, move_pct=0.8,
                 scope='all_sectors', categories=['行业'])
    value.update(changes)
    return value


def fund(**changes):
    value = dict(type='dragon_fund_outflow', key='dragon-flow:2026-09-14:600001:1:dragon_fund_outflow',
                 event_id='synthetic-fund-event', code='600001', name='虚构观察甲', episode=1,
                 source_asof=SOURCE.isoformat(), main_net_cny=-1000, main_net_pct=None,
                 provider='eastmoney', scope='dragon_watchlist', kind='龙空龙资金异动')
    value.update(changes)
    return value


def batch(signals=None, when=None, title='测试记录'):
    value = dict(time=(when or NOW - dt.timedelta(seconds=10)).timestamp(), title=title,
                 message='不应以自由文本重建信号', phone_queued=False)
    if signals is not None:
        value['signals'] = signals
    return value


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.root_patch = patch('monitor_feed.monitor_root', return_value=self.directory)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.calendar_patch = patch('market_calendar.is_trading_day', return_value=(True, '测试交易日历'))
        self.calendar_patch.start()
        self.addCleanup(self.calendar_patch.stop)

    def write(self, filename, data):
        (self.directory / filename).write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')

    def status(self, **changes):
        value = dict(heartbeat=NOW.isoformat(), enabled=True, scope='all_sectors',
                     membership_date=NOW.date().isoformat(), sector_count=2,
                     category_counts={'行业': 1, '概念': 1},
                     quality=dict(status='healthy', fresh_count=2, expected_count=2,
                                  fresh_coverage=1, window_coverage=1, quote_time=SOURCE.isoformat()))
        value.update(changes)
        self.write('status.json', value)
        return monitor_feed.read_status(now=NOW)

    def test_sector_and_fund_source_timestamps_are_normalized(self):
        self.write('events.json', [batch([sector(), fund()])])
        events = monitor_feed.read_events(cutoff=NOW)['events']
        self.assertEqual({r['type'] for r in events}, {'sector_movement', 'dragon_fund_outflow'})
        for row in events:
            self.assertEqual(row['quote_time'], SOURCE.isoformat())
            self.assertEqual(row['observed_at'], (NOW - dt.timedelta(seconds=10)).isoformat())
        flow = next(r for r in events if r['type'] == 'dragon_fund_outflow')
        self.assertEqual(flow['source_asof'], SOURCE.isoformat())
        self.assertEqual(flow['scope'], 'dragon_watchlist')
        self.assertEqual(flow['main_net_cny'], -1000)

    def test_repeated_batches_are_deduplicated_but_distinct_episodes_remain(self):
        first = fund()
        recovery = fund(type='dragon_fund_recovery', key='recovery:1', main_net_cny=0)
        second = fund(key='negative:2', episode=2)
        self.write('events.json', [batch([first, sector(), recovery, second]),
                                   batch([first, sector()], when=NOW)])
        events = monitor_feed.read_events(cutoff=NOW)['events']
        self.assertEqual(len(events), 4)
        self.assertEqual(len({r['id'] for r in events}), 4)
        self.assertEqual(sum(r['type'] == 'dragon_fund_outflow' for r in events), 2)

    def test_different_sector_windows_are_not_collapsed(self):
        self.write('events.json', [batch([sector(), sector(key='sector:BK0001:300:1', seconds=300)])])
        self.assertEqual(len(monitor_feed.read_events(cutoff=NOW)['events']), 2)

    def test_future_source_future_observation_and_source_after_observation_are_rejected(self):
        self.write('events.json', [
            batch([sector(quote_time=(NOW + dt.timedelta(seconds=1)).isoformat())]),
            batch([fund()], when=NOW + dt.timedelta(seconds=1)),
            batch([fund(source_asof=NOW.isoformat())], when=NOW - dt.timedelta(seconds=1)),
            batch([sector(quote_time='not-a-date')]),
            batch([dict(type='dragon_fund_outflow', code='600001')]),
        ])
        self.assertEqual(monitor_feed.read_events(cutoff=NOW)['events'], [])

    def test_source_day_controls_historical_filter(self):
        yesterday = SOURCE - dt.timedelta(days=1)
        self.write('events.json', [batch([sector(), fund(source_asof=yesterday.isoformat())])])
        today = monitor_feed.read_events(cutoff=NOW)
        old = monitor_feed.read_events(day=yesterday.date(), cutoff=NOW)
        self.assertEqual([r['type'] for r in today['events']], ['sector_movement'])
        self.assertEqual([r['type'] for r in old['events']], ['dragon_fund_outflow'])

    def test_legacy_game_text_is_not_reclassified_as_sector_signal(self):
        self.write('events.json', [batch(title='游戏板块异动｜10:00'),
                                   batch([dict(key='stock:600001:1', name='旧游戏个股', quote_time=SOURCE.isoformat())]),
                                   batch(title='游戏监控数据故障'), batch(title='游戏监控已恢复')])
        events = monitor_feed.read_events(cutoff=NOW)['events']
        self.assertEqual(len(events), 2)
        self.assertEqual({r['type'] for r in events}, {'health'})

    def test_fields_are_allowlisted_and_private_strings_suppressed(self):
        self.write('events.json', [dict(batch([fund(ntfy_token='SYNTHETIC_TOKEN', config={'password': 'SYNTHETIC_PASSWORD'},
                    name='/Users/<synthetic>/private-person', source='Bearer SYNTHETIC_BEARER')]),
                    ntfy_topic='SYNTHETIC_TOPIC', message='SYNTHETIC_MESSAGE_SECRET')])
        text = json.dumps(monitor_feed.read_events(cutoff=NOW), ensure_ascii=False)
        for secret in ('SYNTHETIC_TOKEN', 'SYNTHETIC_PASSWORD', 'SYNTHETIC_BEARER',
                       'SYNTHETIC_TOPIC', 'SYNTHETIC_MESSAGE_SECRET', '/Users/'):
            self.assertNotIn(secret, text)

    def test_unreadable_or_wrong_shape_event_store_is_unavailable(self):
        self.assertEqual(monitor_feed.read_events(cutoff=NOW)['status'], 'unavailable')
        (self.directory / 'events.json').write_text('{broken')
        self.assertEqual(monitor_feed.read_events(cutoff=NOW)['status'], 'unavailable')
        self.write('events.json', {})
        self.assertEqual(monitor_feed.read_events(cutoff=NOW)['status'], 'unavailable')
        self.write('events.json', [])
        self.assertEqual(monitor_feed.read_events(cutoff=NOW)['status'], 'available')

    def test_status_healthy_warming_degraded_and_disabled(self):
        self.assertEqual(self.status()['state'], 'healthy')
        self.assertEqual(self.status(quality={'status': 'healthy', 'window_coverage': 0.5})['state'], 'warming')
        self.assertEqual(self.status(quality={'status': 'degraded', 'window_coverage': 1})['state'], 'degraded')
        self.assertEqual(self.status(scope='game')['state'], 'degraded')
        self.assertEqual(self.status(membership_date='2026-09-11')['state'], 'degraded')
        self.assertEqual(self.status(enabled=False)['state'], 'disabled')

    def test_status_offline_on_missing_stale_future_or_malformed_heartbeat(self):
        self.assertEqual(monitor_feed.read_status(now=NOW)['state'], 'offline')
        for heartbeat in (None, 'bad-date', (NOW - dt.timedelta(seconds=91)).isoformat(),
                          (NOW + dt.timedelta(seconds=1)).isoformat()):
            self.assertEqual(self.status(heartbeat=heartbeat)['state'], 'offline')

    def test_closed_calendar_and_lunch_are_not_live_normality(self):
        self.status()
        lunch = NOW.replace(hour=12)
        self.write('status.json', dict(heartbeat=lunch.isoformat(), enabled=True))
        self.assertEqual(monitor_feed.read_status(now=lunch)['state'], 'closed')
        with patch('market_calendar.is_trading_day', return_value=(False, '测试休市日')):
            self.assertEqual(self.status()['state'], 'closed')

    def test_stale_fund_status_cannot_remain_healthy(self):
        for heartbeat in ((NOW - dt.timedelta(seconds=91)).isoformat(),
                          (NOW + dt.timedelta(seconds=1)).isoformat(), None):
            self.write('dragon-flow-status.json', dict(status='healthy', heartbeat=heartbeat,
                stocks=[dict(code='600001', name='虚构观察甲', status='fresh', main_net_cny=-1000,
                             source_asof=SOURCE.isoformat())]))
            flow = self.status()['flow']
            self.assertNotEqual(flow['status'], 'healthy')
            self.assertTrue(all(r.get('status') != 'fresh' for r in flow.get('stocks', [])))

    def test_flow_and_service_private_fields_are_not_exposed(self):
        self.write('dragon-flow-status.json', dict(status='degraded', heartbeat=NOW.isoformat(),
            scope='dragon_watchlist', ntfy_token='SYNTHETIC_FLOW_SECRET', internal_path='/Users/<synthetic-private>',
            membership=dict(status='expired', reason='测试过期', valid_until='2026-09-11', config='HIDDEN_CONFIG'),
            stocks=[dict(code='600001', name='虚构观察甲', status='unavailable', main_net_cny=None,
                         token='SYNTHETIC_STOCK_SECRET')]))
        result = self.status(ntfy_token='SYNTHETIC_SERVICE_SECRET', config={'topic': 'SYNTHETIC_NESTED_SECRET'})
        text = json.dumps(result, ensure_ascii=False)
        self.assertEqual(result['flow']['membership']['status'], 'expired')
        for secret in ('SYNTHETIC_FLOW_SECRET', 'SYNTHETIC_STOCK_SECRET', 'SYNTHETIC_SERVICE_SECRET',
                       'SYNTHETIC_NESTED_SECRET', 'HIDDEN_CONFIG', '/Users/<synthetic-private>'):
            self.assertNotIn(secret, text)


if __name__ == '__main__':
    unittest.main()
