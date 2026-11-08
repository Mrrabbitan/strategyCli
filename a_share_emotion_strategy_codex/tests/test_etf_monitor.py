"""ETF monitor regressions with disposable private roots and artificial evidence."""
import copy
import datetime as dt
import json
import math
import os
from pathlib import Path
import statistics
import tempfile
import unittest
from unittest.mock import patch

import etf_monitor as monitor
from research_store import read_json, research_path


NOW = dt.datetime(2026, 11, 8, 12, tzinfo=monitor.TZ)
DAY = '2026-11-06'


def dates(count=100):
    result, day = [], dt.date.fromisoformat(DAY)
    while len(result) < count:
        if day.weekday() < 5:
            result.append(day.isoformat())
        day -= dt.timedelta(days=1)
    return list(reversed(result))


def documents(closes=None):
    closes = closes or [100 + i for i in range(100)]
    rows = [[day, str(price), str(price), str(price + 1), str(price - 1), '10000']
            for day, price in zip(dates(len(closes)), closes)]
    close = {'phase': 'close_backfill', 'signal_date': DAY, 'prepared_on': '2026-11-08',
             'computed_at': NOW.isoformat(), 'not_a_historical_intraday_signal': True,
             'etf_trends': {'as_of': DAY, 'results': [
                 {'code': 'sz159999', 'name': '人工消费ETF', 'as_of': DAY,
                  'close': closes[-1], 'state': '原字段不作为计算依据',
                  'source': 'https://quotes.example.test/cn'}]},
             'selection': {'priority_observation': '159999', 'hot_theme_observations': [],
                           'basis': '人工报告观察顺序，非产品买入排名'},
             'industry_ranking': {'signal_date': DAY, 'classification': 'eastmoney hybk unique', 'ranking': [
                 {'industry': '人工行业', 'count': 3, 'amount_yuan': 1230000,
                  'stocks': [{'code': '600999', 'name': '人工股票'}]}]},
             'industry_trend_reference': {'as_of': DAY, 'rows': [
                 {'name': '人工行业', 'pct5': 2.5, 'pct20': -3,
                  'ma20_direction': '下降', 'close_vs_ma20': '上方',
                  'source': 'https://index.example.test/history'}]},
             'evidence_dir': '/Users/artificial/private',
             'portfolio_snapshot': {'account_total_cny': 9999999}}
    evidence = {'retrieved_at': NOW.isoformat(), 'sources': [
        {'source': 'https://quotes.example.test/cn',
         'payload': {'data': {'sz159999': {'qfqday': rows,
                                       'qt': {'token': 'SYNTHETIC_SECRET'}}}}}]}
    us = {'as_of': DAY, 'retrieved_at_utc': NOW.isoformat(),
          'session_close_new_york': '2026-11-06T16:00:00-05:00',
          'session_close_beijing': '2026-11-07T05:00:00+08:00', 'etfs': []}
    for symbol, factor in (('SPY', 1), ('XLE', 2)):
        us['etfs'].append({'symbol': symbol, 'label_zh': '人工美股' + symbol,
                           'status': 'verified', 'as_of': DAY,
                           'source': {'url': f'https://us.example.test/{symbol}',
                                      'raw_path': '/Users/artificial/secret'},
                           'bars': [{'date': day, 'adjusted_close': 100 + i * factor,
                                     'close': (100 + i * factor) * 1.1,
                                     'low': (99 + i * factor) * 1.1}
                                    for i, day in enumerate(dates())]})
    return close, evidence, us


class EtfMonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        override = patch.dict(os.environ, {'AUTOSTRATEGY_PRIVATE_ROOT': str(self.base / 'private')})
        override.start(); self.addCleanup(override.stop)

    def write_import(self, close=None, evidence=None, us=None):
        default = documents()
        args = []
        for name, value in zip(('close', 'evidence', 'us'),
                               (close if close is not None else default[0],
                                evidence if evidence is not None else default[1],
                                us if us is not None else default[2])):
            path = self.base / (name + '.json')
            path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
            args.append(path)
        return monitor.import_snapshot(*args, now=NOW)

    def test_adjusted_metrics_calculated_with_correct_windows_and_units(self):
        report = self.write_import()
        row = report['cn'][0]; metrics = row['metrics']
        self.assertEqual(report['schema_version'], 1)
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(row['code'], '159999')
        self.assertAlmostEqual(metrics['pct1'], (199 / 198 - 1) * 100, places=5)
        self.assertAlmostEqual(metrics['pct5'], (199 / 194 - 1) * 100, places=5)
        self.assertAlmostEqual(metrics['pct20'], (199 / 179 - 1) * 100, places=5)
        self.assertAlmostEqual(metrics['pct60'], (199 / 139 - 1) * 100, places=5)
        self.assertEqual(metrics['ma5'], 197)
        self.assertEqual(metrics['ma20'], 189.5)
        self.assertEqual(metrics['ma60'], 169.5)
        self.assertEqual(metrics['ma20_5ago'], 184.5)
        logs = [math.log((i + 1) / i) for i in range(179, 199)]
        self.assertAlmostEqual(metrics['vol20'], statistics.stdev(logs) * math.sqrt(252) * 100, places=5)
        self.assertEqual(metrics['drawdown60'], 0)
        self.assertIsNone(metrics['rs20'])
        self.assertEqual(row['bucket'], 'trend')
        self.assertEqual(report['coverage']['cn'], 1)
        self.assertIn('非全市场', report['coverage']['cn_scope'])
        self.assertTrue(report['backfill'])
        self.assertTrue(any('不是当时' in gap for gap in report['gaps']))
        self.assertEqual(report['events'], [])
        self.assertNotIn('eligible', row)

    def test_drawdown_is_peak_to_trough_inside_last_sixty_closes(self):
        values = [100] * 5 + [120, 60] + [60] * 58
        close, evidence, us = documents(values)
        report = self.write_import(close, evidence, us)
        self.assertEqual(report['cn'][0]['metrics']['drawdown60'], -50)
        self.assertEqual(report['cn'][0]['metrics']['pct60'], -40)

    def test_us_returns_use_adjusted_close_and_same_calendar_spy_difference(self):
        report = self.write_import()
        spy, xle = report['us']
        self.assertEqual(xle['close'], 298)
        self.assertAlmostEqual(xle['metrics']['pct20'], (298 / 258 - 1) * 100, places=5)
        self.assertAlmostEqual(xle['metrics']['rs20'], xle['metrics']['pct20'] - spy['metrics']['pct20'], places=5)
        self.assertEqual(spy['metrics']['rs20'], 0)
        self.assertEqual(report['as_of_us'], DAY)
        self.assertEqual(report['coverage']['us'], 2)

    def test_missing_spy_or_mismatched_calendar_cannot_invent_relative_strength(self):
        close, evidence, us = documents()
        us['etfs'] = us['etfs'][1:]
        report = self.write_import(close, evidence, us)
        self.assertIsNone(report['us'][0]['metrics']['rs20'])
        us = documents()[2]
        us['etfs'][1]['bars'].pop(-10)
        report = self.write_import(close, evidence, us)
        self.assertIsNone(report['us'][1]['metrics']['rs20'])
        self.assertTrue(any('SPY' in gap for gap in report['us'][1]['missing']))

    def test_future_bars_are_excluded_and_date_mismatches_are_unknown(self):
        close, evidence, us = documents()
        evidence['sources'][0]['payload']['data']['sz159999']['qfqday'].append(
            ['2026-11-09', '1000', '1000', '1001', '999', '1'])
        report = self.write_import(close, evidence, us)
        row = report['cn'][0]
        self.assertEqual(row['bucket'], 'unknown')
        self.assertEqual(row['close'], 199)
        self.assertEqual(row['bars'][-1]['date'], DAY)
        self.assertIsNone(report['focus']['priority_code'])
        self.assertTrue(any('未来' in gap for gap in row['missing']))
        close, evidence, us = documents()
        close['etf_trends']['results'][0]['as_of'] = '2026-11-05'
        report = self.write_import(close, evidence, us)
        self.assertEqual(report['cn'][0]['bucket'], 'unknown')
        self.assertTrue(any('错配' in gap for gap in report['cn'][0]['missing']))

    def test_future_report_or_retrieval_time_never_grants_current_conclusion(self):
        close, evidence, us = documents()
        close['signal_date'] = '2026-11-09'
        evidence['retrieved_at'] = '2026-11-10T12:00:00+08:00'
        us['session_close_beijing'] = '2026-11-09T05:00:00+08:00'
        report = self.write_import(close, evidence, us)
        self.assertEqual(report['cn'][0]['bucket'], 'unknown')
        self.assertTrue(all(row['bucket'] == 'unknown' for row in report['us']))
        self.assertIsNone(report['focus']['priority_code'])

    def test_us_shanghai_close_cannot_belong_to_previous_new_york_day(self):
        close, evidence, us = documents()
        us['session_close_beijing'] = '2026-11-06T05:00:00+08:00'
        us['session_close_new_york'] = '2026-11-05T16:00:00-05:00'
        report = self.write_import(close, evidence, us)
        self.assertTrue(all(row['bucket'] == 'unknown' for row in report['us']))
        self.assertTrue(all(row['metrics']['rs20'] is None for row in report['us']))
        self.assertTrue(any('美东日期' in item for item in report['us'][0]['missing']))

    def test_retrieval_must_follow_completed_close_and_half_day_is_not_assumed_full_day(self):
        close, evidence, us = documents()
        evidence['retrieved_at'] = DAY + 'T09:00:00+08:00'
        us['retrieved_at_utc'] = '2026-11-06T12:00:00+00:00'
        report = self.write_import(close, evidence, us)
        self.assertEqual(report['cn'][0]['bucket'], 'unknown')
        self.assertTrue(all(row['bucket'] == 'unknown' for row in report['us']))
        close, evidence, us = documents()
        us['session_close_new_york'] = '2026-11-06T13:00:00-05:00'
        us['session_close_beijing'] = '2026-11-07T02:00:00+08:00'
        report = self.write_import(close, evidence, us)
        self.assertTrue(all(row['bucket'] == 'unknown' for row in report['us']))
        us['session_calendar'] = {'verified': True, 'date': DAY,
                                  'regular_session_end_new_york': '2026-11-06T13:00:00-05:00',
                                  'source': 'https://www.nyse.com/markets/hours-calendars'}
        report = self.write_import(close, evidence, us)
        # Synthetic verified calendar evidence supplies the explicit early close.
        self.assertTrue(all(row['bucket'] != 'unknown' for row in report['us']))
        us['etfs'][0]['validation'] = {'regular_market_quote_at_close': True,
                                      'regular_market_time_new_york': '2026-11-06T13:00:01-05:00'}
        report = self.write_import(close, evidence, us)
        self.assertNotEqual(report['us'][0]['bucket'], 'unknown')
        us['etfs'][0]['validation'] = {'regular_market_quote_at_close': True,
                                      'regular_market_time_new_york': '2026-11-06T16:00:00-05:00'}
        report = self.write_import(close, evidence, us)
        self.assertEqual(report['us'][0]['bucket'], 'unknown')

    def test_nine_am_new_york_is_not_a_completed_daily_session(self):
        close, evidence, us = documents()
        us['session_close_new_york'] = '2026-11-06T09:00:00-05:00'
        us['session_close_beijing'] = '2026-11-06T22:00:00+08:00'
        report = self.write_import(close, evidence, us)
        self.assertTrue(all(row['bucket'] == 'unknown' for row in report['us']))
        self.assertTrue(all(row['metrics']['rs20'] is None for row in report['us']))
        self.assertTrue(any('不把早盘' in item for item in report['gaps']))

    def test_intraday_is_whitelisted_finite_observation_only_with_expiry(self):
        close, evidence, us = documents()
        close['intraday'] = {
            'observed_at': NOW.isoformat(), 'note': '账户总资产123.45万元',
            'confirmation': True, 'rows': [
                {'code': 'sz159999', 'name': '人工ETF', 'price': 1.2, 'pct': float('nan'),
                 'vwap': '1.1', 'volume_ratio': float('inf'), 'as_of': NOW.isoformat(),
                 'conditions': ['持仓成本7.89万元', '已确认买入'], 'account_total_cny': 1234500},
                {'code': '159998', 'name': '持仓成本7.89万元', 'price': 1,
                 'as_of': (NOW-dt.timedelta(seconds=91)).isoformat()},
                {'code': '159997', 'name': '人工未来ETF',
                 'as_of': (NOW+dt.timedelta(seconds=1)).isoformat()}]}
        report = self.write_import(close, evidence, us)
        snapshot = report['intraday']; encoded = json.dumps(snapshot, ensure_ascii=False)
        self.assertEqual(snapshot['status'], 'observation_only')
        self.assertIn('时效不足', snapshot['note'])
        self.assertEqual(snapshot['rows'][0]['price'], 1.2)
        self.assertIsNone(snapshot['rows'][0]['pct'])
        self.assertIsNone(snapshot['rows'][0]['volume_ratio'])
        self.assertEqual(snapshot['rows'][0]['vwap'], 1.1)
        self.assertTrue(any('时效不足' in item for item in snapshot['rows'][1]['conditions']))
        self.assertTrue(any('未来' in item for item in snapshot['rows'][2]['conditions']))
        for text in ('123.45万元', '7.89万元', '1234500', 'confirmation', '已确认买入'):
            self.assertNotIn(text, encoded)
        self.assertEqual(self.write_import()['intraday']['status'], 'not_available')

    def test_intraday_same_chinese_date_and_retrieved_timestamp_are_required(self):
        close, evidence, us = documents()
        close['intraday'] = {'observed_at': (NOW+dt.timedelta(seconds=1)).isoformat(),
                             'rows': [{'code': '159999', 'as_of': NOW.isoformat()}]}
        report = self.write_import(close, evidence, us)
        self.assertIn('时效不足', report['intraday']['note'])
        close['intraday']['observed_at'] = (NOW-dt.timedelta(days=1)).isoformat()
        close['intraday']['rows'][0]['as_of'] = (NOW-dt.timedelta(days=1)).isoformat()
        report = self.write_import(close, evidence, us)
        self.assertIn('非当前上海日期', report['intraday']['note'])

    def test_insufficient_history_leaves_only_available_metrics_and_unknown_state(self):
        close, evidence, us = documents([100 + i for i in range(20)])
        report = self.write_import(close, evidence, us)
        row = report['cn'][0]
        self.assertIsNotNone(row['metrics']['pct5'])
        self.assertIsNotNone(row['metrics']['ma20'])
        for key in ('pct20', 'pct60', 'ma60', 'ma20_5ago', 'vol20', 'drawdown60', 'rs20'):
            self.assertIsNone(row['metrics'][key], key)
        self.assertEqual(row['bucket'], 'unknown')

    def test_missing_raw_evidence_does_not_trust_precomputed_summary(self):
        report = self.write_import(evidence={})
        row = report['cn'][0]
        self.assertEqual(row['bars'], [])
        self.assertIsNone(row['close'])
        self.assertTrue(all(value is None for value in row['metrics'].values()))
        self.assertEqual(row['bucket'], 'unknown')
        self.assertEqual(report['coverage']['cn'], 1)

    def test_eastmoney_qfq_can_supply_independent_evidence_without_renaming_tencent_day(self):
        close, evidence, us = documents()
        original = evidence['sources'][0]['payload']['data']['sz159999']
        original['day'] = original.pop('qfqday')
        record = {'format': 'eastmoney_qfq', 'code': '159999',
                  'source': 'https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=0.159999&fqt=1',
                  'retrieved_at': NOW.isoformat(),
                  'payload': {'data': {'code': '159999', 'klines': [','.join(line) for line in original['day']]}}}
        evidence['sources'].append(record)
        untouched = copy.deepcopy(evidence)
        report = self.write_import(close, evidence, us)
        self.assertEqual(evidence, untouched)
        self.assertEqual(report['cn'][0]['bucket'], 'trend')
        self.assertEqual(report['cn'][0]['metrics']['pct60'], round((199/139-1)*100, 6))
        self.assertIn('eastmoney.com', report['cn'][0]['source'])
        self.assertIn('fqt=1', report['sources'][0]['basis'])
        self.assertIn('未把腾讯day冒称', report['sources'][0]['basis'])
        record['source'] = record['source'].replace('fqt=1', 'fqt=0')
        report = self.write_import(close, evidence, us)
        self.assertEqual(report['cn'][0]['bucket'], 'unknown')
        self.assertTrue(any('fqt=1' in item for item in report['cn'][0]['missing']))

    def test_duplicate_dates_and_conflicting_close_fail_closed(self):
        close, evidence, us = documents()
        raw = evidence['sources'][0]['payload']['data']['sz159999']['qfqday']
        raw[-1][0] = raw[-2][0]
        report = self.write_import(close, evidence, us)
        self.assertEqual(report['cn'][0]['bars'], [])
        self.assertEqual(report['cn'][0]['bucket'], 'unknown')
        close, evidence, us = documents()
        close['etf_trends']['results'][0]['close'] = 200
        report = self.write_import(close, evidence, us)
        self.assertEqual(report['cn'][0]['bucket'], 'unknown')
        self.assertTrue(any('冲突' in gap for gap in report['cn'][0]['missing']))

    def test_hot_price_repair_is_not_relabelled_as_confirmed_reversal(self):
        values = [100] * 70 + [75] * 25 + [70, 78, 80, 82, 85]
        close, evidence, us = documents(values)
        close['etf_trends']['results'][0]['name'] = '人工传媒ETF'
        report = self.write_import(close, evidence, us)
        row = report['cn'][0]
        self.assertEqual(row['bucket'], 'repair')
        self.assertNotIn('趋势确认', row['state'])

    def test_heat_preserves_publisher_order_and_classification_difference(self):
        close, evidence, us = documents()
        close['industry_ranking']['ranking'].append({'industry': '人工第二行业', 'count': 1,
                                                   'amount_yuan': 1000, 'stocks': []})
        report = self.write_import(close, evidence, us)
        self.assertEqual([row['count'] for row in report['heat']], [3, 1])
        self.assertEqual(report['heat'][0]['trend']['pct20'], -3)
        self.assertIsNone(report['heat'][1]['trend'])
        self.assertEqual(report['heat'][0]['source'], 'https://push2ex.eastmoney.com/getTopicZTPool?date=20261106')
        self.assertTrue(any('不完全一致' in gap for gap in report['gaps']))
        close['industry_trend_reference']['rows'][0]['ma20_direction'] = 'down'
        report = self.write_import(close, evidence, us)
        self.assertEqual(report['heat'][0]['trend']['ma20_direction'], '下降')
        close['industry_ranking']['signal_date'] = '2026-11-05'
        report = self.write_import(close, evidence, us)
        self.assertEqual(report['heat'], [])
        self.assertTrue(any('榜日期' in gap for gap in report['gaps']))

    def test_public_canonical_fields_do_not_copy_payload_paths_accounts_or_secrets(self):
        close, evidence, us = documents()
        close['selection']['basis'] = '原文 /Users/artificial/private token=SYNTHETIC_TOKEN'
        close['etf_trends']['results'][0]['name'] = '/Users/artificial/name'
        evidence['sources'][0]['source'] = 'https://quotes.example.test/cn?token=SYNTHETIC_TOKEN'
        report = self.write_import(close, evidence, us)
        encoded = json.dumps(report, ensure_ascii=False)
        for prohibited in ('/Users/', 'SYNTHETIC_SECRET', 'SYNTHETIC_TOKEN', 'raw_path',
                           'payload', 'portfolio_snapshot', 'account_total_cny', '9999999'):
            self.assertNotIn(prohibited, encoded)
        self.assertEqual(report['cn'][0]['source'], 'https://quotes.example.test/cn')
        self.assertEqual(report['cn'][0]['name'], '私有内容已省略')

    def test_focus_basis_does_not_forward_chinese_account_information(self):
        close, evidence, us = documents()
        close['selection']['basis'] = '账户总资产123.45万元，持仓成本7.89万元，生活费2.46万元'
        report = self.write_import(close, evidence, us)
        encoded = json.dumps(report, ensure_ascii=False)
        for text in ('123.45万元', '7.89万元', '2.46万元', '账户总资产', '持仓成本'):
            self.assertNotIn(text, encoded)
        self.assertIn('不是买入指令', report['focus']['basis'])

    def test_publication_is_atomic_private_archived_idempotent_and_not_mutating(self):
        report = self.write_import()
        original = copy.deepcopy(report)
        with patch('etf_monitor._now', return_value=NOW):
            stored = monitor.publish_snapshot(report)
        self.assertEqual(report, original)
        self.assertEqual(read_json(research_path('etf_monitor/current.json')), stored)
        archive = list(research_path('etf_monitor/archive/' + DAY).glob('*.json'))
        self.assertEqual(len(archive), 1)
        self.assertEqual(read_json(archive[0]), stored)
        repeated = copy.deepcopy(report)
        repeated['generated_at'] = (NOW + dt.timedelta(minutes=1)).isoformat()
        self.assertEqual(monitor.publish_snapshot(repeated), stored)
        self.assertEqual(len(list(research_path('etf_monitor/archive/' + DAY).glob('*.json'))), 1)
        older = copy.deepcopy(report)
        older['generated_at'] = (NOW - dt.timedelta(minutes=1)).isoformat()
        older['gaps'].append('人工旧版本')
        with self.assertRaises(ValueError):
            monitor.publish_snapshot(older)
        self.assertEqual(read_json(research_path('etf_monitor/current.json')), stored)

    def test_publication_rejects_nans_unknown_fields_and_raw_private_content(self):
        report = self.write_import()
        for mutate in (lambda doc: doc.update(payload={'raw': 'x'}),
                       lambda doc: doc['cn'][0]['metrics'].update(pct5=float('nan')),
                       lambda doc: doc['gaps'].append('/Users/artificial/secret')):
            changed = copy.deepcopy(report); mutate(changed)
            with self.assertRaises(ValueError):
                monitor.publish_snapshot(changed)
        self.assertFalse(research_path('etf_monitor/current.json').exists())

    def test_optional_documents_empty_coverage_and_generic_read_errors(self):
        path = self.base / 'empty.json'; path.write_text('{}')
        report = monitor.import_snapshot(path, now=NOW)
        self.assertEqual(report['coverage']['cn'], 0)
        self.assertEqual(report['coverage']['us'], 0)
        self.assertEqual(report['cn'], [])
        self.assertEqual(report['focus']['alternatives'], [])
        with self.assertRaisesRegex(ValueError, '^Offline evidence document is unavailable$'):
            monitor.import_snapshot(self.base / 'missing.json', now=NOW)


if __name__ == '__main__':
    unittest.main()
