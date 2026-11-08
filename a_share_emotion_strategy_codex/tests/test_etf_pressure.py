"""Deterministic offline candle/flow separation, using invented instruments."""
import copy
import datetime as dt
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import etf_pressure as pressure
from research_store import read_json, research_path

NOW = dt.datetime(2026, 8, 13, 12, tzinfo=pressure.TZ)
DAY = '2026-08-12'


def documents(last=None):
    dates, day = [], dt.date.fromisoformat(DAY)
    while len(dates) < 22:
        if day.weekday() < 5:
            dates.append(day.isoformat())
        day -= dt.timedelta(days=1)
    dates.reverse()
    bars = [[date, 100, 100, 101, 99, 1000] for date in dates]
    bars[-1] = [DAY, 100, 96, 100.5, 95.5, 1300] if last is None else [DAY, *last]
    monitor = {'schema_version': 1, 'as_of_cn': DAY, 'generated_at': NOW.isoformat(),
               'cn': [{'code': '159999', 'name': '人工代理ETF', 'as_of': DAY, 'close': bars[-1][2]}],
               'account': {'balance': 'SYNTHETIC_PRIVATE_BALANCE'}}
    evidence = {'retrieved_at': NOW.isoformat(), 'sources': [
        {'source': 'https://quotes.example.test/history',
         'payload': {'data': {'sz159999': {'qfqday': bars, 'qt': {'token': 'SYNTHETIC_SECRET'}}}}}]}
    return monitor, evidence


def flow_fixture():
    value = pressure.empty_flow()
    value.update(status='reference_only', retrieved_at=NOW.isoformat(), note='原站时点未知，保持原排序。')
    for side, sign, offset in (('inflows', 1, 0), ('outflows', -1, 100)):
        value[side] = [{'code': '881' + f'{offset+i:03d}', 'name': '人工行业' + str(i), 'net_yuan': sign*(100000-i),
                        'rank': i, 'change_pct': None} for i in range(1, 11)]
    return value


class EtfPressureTests(unittest.TestCase):
    def compute(self, last=None):
        return pressure.compute_report(*documents(last), now=NOW)

    def test_true_bear_separates_gap_and_body_and_excludes_current_volume_from_mean(self):
        row = self.compute()['candles'][0]
        self.assertTrue(row['is_large_bear'])
        self.assertEqual(row['body_pct'], -4)
        self.assertEqual(row['change_pct'], -4)
        self.assertEqual(row['body_range_pct'], 80)
        self.assertEqual(row['close_position_pct'], 10)
        self.assertEqual(row['volume_ratio20'], 1.3)
        report = self.compute(); self.assertEqual(report['coverage']['large_bear_with_volume'], 1)
        self.assertEqual(report['flow']['status'], 'unavailable')
        self.assertIsNone(report['flow']['as_of'])
        self.assertFalse(report['flow']['is_main_metric'])

    def test_gap_down_green_candle_is_not_large_bear(self):
        row = self.compute([95, 96, 97, 94, 2000])['candles'][0]
        self.assertEqual(row['change_pct'], -4)
        self.assertGreater(row['body_pct'], 0)
        self.assertFalse(row['is_large_bear'])

    def test_exact_threshold_and_each_failure(self):
        self.assertTrue(self.compute([100, 98, 100.5, 97.5, 1000])['candles'][0]['is_large_bear'])
        for fields in ([100, 98.1, 100, 97.8, 1000], [100, 97, 105, 96, 1000],
                       [100, 97, 100.1, 90, 1000], [100, 100, 100, 100, 1000]):
            self.assertFalse(self.compute(fields)['candles'][0]['is_large_bear'])
        flat = self.compute([100, 100, 100, 100, 1000])['candles'][0]
        self.assertIsNone(flat['body_range_pct'])
        self.assertIsNone(flat['close_position_pct'])

    def test_missing_volume_is_unknown_volume_not_a_veto(self):
        for bad in (None, -1, float('nan'), True):
            row = self.compute([100, 96, 100.5, 95.5, bad])['candles'][0]
            self.assertIsNone(row['volume_ratio20'])
            self.assertTrue(row['is_large_bear'])
        monitor, evidence = documents()
        evidence['sources'][0]['payload']['data']['sz159999']['qfqday'] = evidence['sources'][0]['payload']['data']['sz159999']['qfqday'][-1:]
        row = pressure.compute_report(monitor, evidence, NOW)['candles'][0]
        self.assertIsNone(row['change_pct']); self.assertIsNone(row['volume_ratio20'])
        self.assertTrue(row['is_large_bear'])
        monitor,evidence=documents()
        evidence['sources'][0]['payload']['data']['sz159999']['qfqday'][-1].pop()
        row=pressure.compute_report(monitor,evidence,NOW)['candles'][0]
        self.assertIsNone(row['volume_ratio20']);self.assertTrue(row['is_large_bear'])

    def test_future_intraday_date_mismatch_and_corrupt_ohlc_fail_closed(self):
        monitor, evidence = documents()
        scenarios = []
        bad = copy.deepcopy(evidence); bad['sources'][0]['payload']['data']['sz159999']['qfqday'].append(['2026-08-14',100,96,101,95,1000]); scenarios.append((monitor,bad,NOW))
        bad = copy.deepcopy(monitor); bad['cn'][0]['as_of'] = '2026-08-11'; scenarios.append((bad,evidence,NOW))
        bad = copy.deepcopy(evidence); bad['sources'][0]['payload']['data']['sz159999']['qfqday'][-1][3] = 94; scenarios.append((monitor,bad,NOW))
        scenarios.append((monitor,evidence,dt.datetime(2026,8,12,14,tzinfo=pressure.TZ)))
        bad=copy.deepcopy(evidence);bad['sources'][0]['payload']['data']['sz159999']['qfqday'][0][0]=[];scenarios.append((monitor,bad,NOW))
        for m, e, now in scenarios:
            row = pressure.compute_report(m,e,now)['candles'][0]
            self.assertIsNone(row['is_large_bear']); self.assertTrue(row['missing'])

    def test_adjustment_missing_and_duplicate_evidence_are_unknown(self):
        monitor, evidence = documents(); data = evidence['sources'][0]['payload']['data']['sz159999']
        data['day'] = data.pop('qfqday')
        self.assertIsNone(pressure.compute_report(monitor,evidence,NOW)['candles'][0]['is_large_bear'])
        monitor,evidence = documents(); evidence['sources'].append(copy.deepcopy(evidence['sources'][0]))
        self.assertIsNone(pressure.compute_report(monitor,evidence,NOW)['candles'][0]['is_large_bear'])

    def test_eastmoney_allowed_only_as_fqt1_price_evidence(self):
        monitor,evidence = documents(); raw = evidence['sources'][0]['payload']['data']['sz159999']['qfqday']
        em = {'format':'eastmoney_qfq','code':'159999','source':'https://push2his.eastmoney.com/api/qt/stock/kline/get?fqt=1',
              'payload':{'data':{'code':'159999','klines':[','.join(map(str,r)) for r in raw]}}}
        evidence['sources']=[em]
        report = pressure.compute_report(monitor,evidence,NOW)
        self.assertTrue(report['candles'][0]['is_large_bear']); self.assertEqual(report['flow']['provider'],'同花顺')
        em['source']=em['source'].replace('fqt=1','fqt=0')
        self.assertIsNone(pressure.compute_report(monitor,evidence,NOW)['candles'][0]['is_large_bear'])

    def test_publish_is_private_allowlisted_and_keeps_monitor_untouched(self):
        report = self.compute(); report['flow'] = flow_fixture()
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'AUTOSTRATEGY_PRIVATE_ROOT':root}):
            result = pressure.publish_report(report)
            self.assertEqual(read_json(research_path('etf_pressure/current.json')),result)
            self.assertFalse(research_path('etf_monitor/current.json').exists())
            encoded=json.dumps(result)
            self.assertNotIn('SYNTHETIC_PRIVATE',encoded);self.assertNotIn('SYNTHETIC_SECRET',encoded)
            self.assertNotIn('payload',encoded)
            self.assertEqual(len(list(research_path('etf_pressure/archive').rglob('*.json'))),1)

    def test_funding_never_upgrades_to_main_or_dated_signal_or_other_provider(self):
        for field,value in (('provider','东方财富'),('is_main_metric',True),('as_of',DAY),
                            ('universe_complete',True),('status','available')):
            report=self.compute();report['flow']=flow_fixture();report['flow'][field]=value
            with self.assertRaises(ValueError): pressure._validate_report(report)
        report=self.compute();report['flow']=flow_fixture();report['flow']['outflows'].pop()
        pressure._validate_report(report)
        report=self.compute();report['flow']=flow_fixture();report['flow']['inflows'][0]['rank']=2
        with self.assertRaises(ValueError): pressure._validate_report(report)

    def test_noncanonical_nested_payloads_and_account_notes_rejected(self):
        report=self.compute();report['coverage']['payload']={'raw':'not_public'}
        with self.assertRaises(ValueError):pressure._validate_report(report)
        report=self.compute();report['flow']=flow_fixture()
        report['flow']['note']='普通资金净额不识别真实账户。'
        with self.assertRaises(ValueError):pressure._validate_report(report)

    def test_pool_expansion_and_unique_sample_expected_not_fixed_to_twelve(self):
        monitor,evidence=documents()
        monitor['cn']=[dict(monitor['cn'][0],code=f'159{i:03d}') for i in range(21)]
        monitor['cn'].append(dict(monitor['cn'][0]))
        report=pressure.compute_report(monitor,evidence,NOW)
        self.assertEqual(report['coverage']['expected'],21)
        self.assertEqual(report['coverage']['received'],21)
        self.assertEqual(report['universe_label'],'21只ETF代理，不是全行业')
        pressure._validate_report(report)
        monitor['cn']*=50
        with self.assertRaises(ValueError):pressure.compute_report(monitor,evidence,NOW)

    def test_partial_funding_preserves_original_rank_and_reports_coverage(self):
        report=self.compute();report['flow']=flow_fixture()
        report['flow']['inflows']=report['flow']['inflows'][2:5]
        report['flow']['outflows']=report['flow']['outflows'][:2]
        with tempfile.TemporaryDirectory() as root,patch.dict(os.environ,{'AUTOSTRATEGY_PRIVATE_ROOT':root}):
            stored=pressure.publish_report(report)
        self.assertEqual([x['rank'] for x in stored['flow']['inflows']],[3,4,5])
        self.assertEqual(stored['coverage']['flow_inflows'],3)
        self.assertEqual(stored['coverage']['flow_outflows'],2)
        self.assertIn('流入3/10、流出2/10',stored['flow']['note'])

    def test_funding_rejects_cross_group_duplicates_zero_outflow_and_nonindustry_codes(self):
        for bad in ('duplicate','zero','stock','source'):
            report=self.compute();report['flow']=flow_fixture()
            if bad=='duplicate':report['flow']['outflows'][0]['code']=report['flow']['inflows'][0]['code']
            if bad=='zero':report['flow']['outflows'][0]['net_yuan']=0
            if bad=='stock':report['flow']['outflows'][0]['code']='600999'
            if bad=='source':report['flow']['source']='https://data.10jqka.com.cn/funds/hyzjl/arbitrary'
            with self.assertRaises(ValueError):pressure._validate_report(report)


if __name__ == '__main__':
    unittest.main()
