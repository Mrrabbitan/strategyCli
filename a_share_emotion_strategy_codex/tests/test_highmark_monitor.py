"""Artificial data only: dates, publication isolation and truthful delivery."""
import copy
import datetime as dt
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import highmark_monitor as monitor
from research_store import atomic_json, read_json


class HighmarkMonitorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'AUTOSTRATEGY_PRIVATE_ROOT': self.tmp.name})
        self.env.start()
        self.now = dt.datetime(2026, 10, 22, 16, 0, tzinfo=monitor.TZ)
        days = monitor.trading_sessions(self.now.date(), 40)
        bars = [{'date': day, 'close': 10 + i / 100, 'volume': 100,
                 'prev_close': 10 + (i - 1) / 100 if i else 10}
                for i, day in enumerate(days)]
        index = [{'date': day, 'close': 100} for day in days]
        source = 'https://example.com/artificial-pool'
        self.evidence = {'pool': {'status': 'complete', 'items': [
            {'code': '002001', 'name': '样例股份', 'boards': 2, 'source': source}]},
            'stocks': {'002001': {'status': 'complete', 'history': bars,
                                  'quote': {'price': bars[-1]['close'], 'name': '样例股份'}}},
            'benchmarks': {'sz399107': {'history': index}}, 'retained': [],
            'missing': [], 'sources': [{'label': '虚构', 'url': source}]}

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def report(self, evidence=None, phase='close'):
        now = self.now if phase == 'close' else self.now.replace(day=23, hour=9)
        return monitor.build_report(evidence or self.evidence, now=now, phase=phase,
                                    trade_date='2026-10-22')

    def test_three_windows_and_reference_only(self):
        report = self.report()
        self.assertEqual(report['coverage']['scanned_count'], 1)
        self.assertEqual(set(report['rows'][0]['metrics']), {'3', '10', '30'})
        self.assertTrue(report['rows'][0]['reference_only'])
        self.assertEqual(report['rows'][0]['last_in_pool'], '2026-10-22')

    def test_feed_conflict_does_not_calculate(self):
        data = copy.deepcopy(self.evidence)
        data['stocks']['002001']['status'] = 'unavailable'
        data['stocks']['002001']['missing'] = ['报价冲突']
        row = self.report(data)['rows'][0]
        self.assertEqual(row['metrics'], {})
        self.assertTrue(row['excluded'])

    def test_missing_index_session_does_not_join_dates(self):
        data = copy.deepcopy(self.evidence)
        data['benchmarks']['sz399107']['history'].pop(-5)
        self.assertEqual(self.report(data)['coverage']['scanned_count'], 0)

    def test_index_partial_cannot_count_as_fully_calculated(self):
        data = copy.deepcopy(self.evidence)
        data['benchmarks']['sz399107'].update(status='partial', quote=None)
        self.assertEqual(self.report(data)['coverage']['scanned_count'], 0)

    def test_index_quote_conflict(self):
        data = copy.deepcopy(self.evidence)
        data['benchmarks']['sz399107']['quote'] = {'price': 99}
        self.assertEqual(self.report(data)['rows'][0]['metrics'], {})

    def test_historical_zero_volume_is_not_an_ordinary_session(self):
        data = copy.deepcopy(self.evidence)
        data['stocks']['002001']['history'][-2]['volume_hands'] = 0
        row = self.report(data)['rows'][0]
        self.assertIsNone(row['metrics']['3']['value'])
        self.assertIsNone(row['metrics']['30']['value'])

    def test_no_future_price_changes_result(self):
        original = self.report()['rows'][0]['metrics']
        data = copy.deepcopy(self.evidence)
        data['stocks']['002001']['history'].append({'date': '2026-10-23', 'close': 10000})
        calculated = self.report(data)['rows'][0]['metrics']
        self.assertEqual(original, calculated)

    def test_missing_stock_date_does_not_fill_zero(self):
        data = copy.deepcopy(self.evidence)
        data['stocks']['002001']['history'].pop(-3)
        self.assertEqual(self.report(data)['rows'][0]['metrics'], {})

    def test_normal_and_severe_reset_separate(self):
        reset = {'002001': {'normal_reset': '2026-10-21', 'verified': True,
                            'exhaustive_review': True, 'coverage_start': '2026-08-01',
                            'coverage_end': '2026-10-22',
                            'source_url': 'https://example.com/artificial',
                            'verified_at': self.now.isoformat(), 'exchange_published_date': '2026-10-20'}}
        report = monitor.build_report(self.evidence, now=self.now, phase='close', trade_date='2026-10-22', resets=reset)
        row = report['rows'][0]
        self.assertEqual(row['metrics']['3']['reset_applied'], '2026-10-21')
        self.assertIsNone(row['metrics']['10']['reset_applied'])

    def test_provider_events_align_estimates_without_claiming_official(self):
        data = copy.deepcopy(self.evidence)
        data['events'] = {'status': 'complete', 'provider_complete': True,
                          'official_complete': False, 'coverage_start': '2026-08-01',
                          'coverage_end': '2026-10-22', 'normal': {'002001': [
                              {'published_date': '2026-10-20'}]}, 'severe': {}}
        report = self.report(data)
        self.assertEqual(report['rows'][0]['metrics']['3']['reset_applied'], '2026-10-21')
        self.assertIsNone(report['rows'][0]['metrics']['10']['reset_applied'])
        self.assertFalse(report['reset_alignment']['official_complete'])
        self.assertTrue(report['rows'][0]['reference_only'])

    def test_partial_events_do_not_assert_absence_or_apply_reset(self):
        data = copy.deepcopy(self.evidence)
        data['events'] = {'status': 'partial', 'provider_complete': False,
                          'coverage_start': '2026-08-01', 'coverage_end': '2026-10-22',
                          'normal': {'002001': [{'published_date': '2026-10-20'}]}}
        self.assertIsNone(self.report(data)['rows'][0]['metrics']['3']['reset_applied'])

    def test_today_event_resets_next_session_not_today_closing_value(self):
        data = copy.deepcopy(self.evidence)
        data['events'] = {'status': 'complete', 'provider_complete': True,
                          'coverage_start': '2026-08-01', 'coverage_end': '2026-10-22',
                          'normal': {'002001': [{'published_date': '2026-10-22'}]}, 'severe': {}}
        metric = self.report(data)['rows'][0]['metrics']['3']
        self.assertIsNone(metric['reset_applied'])
        self.assertIsNotNone(metric['value'])
        self.assertEqual(metric['pending_reset'], '2026-10-23')
        self.assertEqual(metric['next_day_flat_index_pct'], 20)

    def test_two_phases_send_independently_and_rerun_is_quiet(self):
        first = monitor.publish(self.report())
        self.assertTrue(monitor.decision(first)['notify'])
        monitor.mark_sent(first['edition'])
        rerun = self.report()
        rerun['generated_at'] = (self.now + dt.timedelta(minutes=1)).isoformat()
        rerun = monitor.publish(rerun)
        self.assertFalse(monitor.decision(rerun)['notify'])
        preopen = monitor.publish(self.report(phase='preopen'))
        self.assertTrue(monitor.decision(preopen)['notify'])

    def test_version_check_prevents_concurrent_receipt(self):
        first = monitor.publish(self.report())
        second = self.report()
        second['generated_at'] = (self.now + dt.timedelta(minutes=1)).isoformat()
        monitor.publish(second)
        with self.assertRaises(ValueError):
            monitor.mark_sent(first['edition'])

    def test_page_failure_and_recovery_not_suppressed(self):
        first = self.report()
        first['build_status'] = 'success'
        first = monitor.publish(first)
        monitor.mark_sent(first['edition'])
        failed = copy.deepcopy(first)
        failed['build_status'] = 'failed'
        self.assertTrue(monitor.decision(failed)['notify'])
        failed = monitor.publish(failed)
        monitor.mark_sent(failed['edition'])
        recovered = copy.deepcopy(failed)
        recovered['build_status'] = 'success'
        self.assertTrue(monitor.decision(recovered)['notify'])

    def test_empty_is_distinct_from_failed_and_first_fault_only(self):
        empty = self.report({'pool': {'status': 'complete', 'items': []}})
        empty = monitor.publish(empty)
        self.assertEqual(monitor.decision(empty)['reason'], '已核验范围为空')
        fault = self.report({'pool': {'status': 'unavailable', 'items': []}})
        fault = monitor.publish(fault)
        self.assertEqual(monitor.decision(fault)['reason'], '首次取数故障')
        monitor.mark_sent(fault['edition'])
        other = copy.deepcopy(fault)
        other['generated_at'] = (self.now + dt.timedelta(days=1)).isoformat()
        other['date'] = '2026-10-23'
        other = monitor.publish(other)
        self.assertFalse(monitor.decision(other)['notify'])

    def test_recent_members_retained_without_changing_today_boards(self):
        original = monitor.publish(self.report())
        retained = monitor.retained_items('2026-10-22')
        self.assertEqual(retained[0]['code'], '002001')
        data = copy.deepcopy(self.evidence)
        data['pool']['items'] = []
        data['retained'] = retained
        row = self.report(data)['rows'][0]
        self.assertEqual(row['boards'], 0)
        self.assertEqual(row['origin'], '近期高标继续跟踪')

    def test_weekend_and_expired_preopen_do_not_collect(self):
        def fail(*args, **kwargs):
            raise AssertionError('must not collect')
        weekend = self.now.replace(day=24)
        self.assertFalse(monitor.run(phase='close', now=weekend, collector=fail)['notify'])
        late = self.now.replace(hour=10)
        self.assertFalse(monitor.run(phase='preopen', now=late, collector=fail)['notify'])
        self.assertFalse(monitor.decision()['notify'])

    def test_skip_cannot_resend_an_old_pending_report(self):
        monitor.publish(self.report())
        self.assertTrue(monitor.decision()['notify'])
        monitor.run(phase='close', now=self.now.replace(day=24), build_page=False)
        self.assertFalse(monitor.decision()['notify'])

    def test_unknown_calendar_publishes_failure_not_stale_market(self):
        with patch.object(monitor, 'is_trading_day', return_value=(False, '交易所休市日历不可用，安全跳过')):
            result = monitor.run(phase='close', now=self.now, build_page=False)
        self.assertTrue(result['notify'])
        report = read_json(monitor.folder() / 'current.json')
        self.assertIsNone(report['as_of'])
        self.assertIsNone(report['trade_date'])
        self.assertEqual(report['status'], 'unavailable')
        self.assertTrue(monitor.decision()['notify'])

    def test_publish_rejects_market_future(self):
        report = self.report()
        report['as_of'] = '2026-10-23T15:00:00+08:00'
        with self.assertRaises(ValueError):
            monitor.publish(report)


if __name__ == '__main__':
    unittest.main()
