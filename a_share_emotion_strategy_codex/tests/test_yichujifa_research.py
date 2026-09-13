"""Artificial native reports only; no actual watchlists or market requests."""
import copy
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yichujifa_research as module


NOW = dt.datetime(2026, 9, 14, 9, 58, 10, tzinfo=module.TZ)


def fixture():
    row = {'code': '600001', 'name': '虚构研究甲', 'branch': 'one_to_two', 'sector': '人工行业',
           'rank': 1, 'rank_verified': True, 'prequalified': True, 'status': 'watch', 'reasons': [],
           'score': 80, 'stage': '一进二', 'float_cap_cny': 1_000_000_000, 'last_close': 11,
           'five_day': {'verified': True, 'return_pct': 3, 'ma5': 10.5, 'trend': '改善',
                        'rows': [{'date': '2026-09-11', 'open': 10, 'high': 11, 'low': 9.8,
                                  'close': 11, 'volume': 23400, 'amount': 240000}]},
           'announcement': {'reviewed_through': '2026-09-11T16:00:00+08:00',
                            'sources': ['https://example.com/filing']},
           'sources': [{'url': 'https://example.com/prices'}], 'shape': None}
    return {'schema_version': 1, 'version': 'test', 'as_of': '2026-09-11',
            'generated_at': '2026-09-11T16:10:00+08:00', 'mode': 'close_watchlist',
            'phase': {'verified': True, 'day_number': 2, 'day1': '2026-09-10'},
            'coverage': {'requested_stocks': 1, 'dual_history_verified': 1,
                         'pool_days': {'2026-09-11': {'metadata_verified': True}}},
            'sectors': [{'name': '人工行业', 'verified': True}], 'errors': [], 'candidates': [row],
            'sources': [{'url': 'https://example.com/source'}], 'scope': '虚构验收样本'}


def live_fixture():
    frames = []
    for time in ('09:56:00', '09:57:00', '09:58:00'):
        stamp = '2026-09-14T' + time + '+08:00'
        frames.append({'time': stamp, 'quotes': {'600001': {'time': stamp, 'verified': True,
                                                          'tradable': True, 'normal_limit_rule': True}}})
    live = {'mode': 'live', 'time': '2026-09-14T09:58:05+08:00', 'reasons': [],
            'results': [{'code': '600001', 'name': '虚构研究甲', 'branch': 'one_to_two',
                         'status': '条件已满足', 'reasons': []}], 'automatic_order': False}
    return live, frames


class YichujifaResearchTests(unittest.TestCase):
    def import_report(self, report=None, **kwargs):
        return module.import_native(report if report is not None else fixture(), as_of=NOW, **kwargs)

    def test_close_prequalification_is_not_current_permission(self):
        result = self.import_report()
        self.assertEqual(result['module_id'], 'yichujifa')
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['coverage']['prequalified_count'], 1)
        self.assertFalse(result['candidates'][0]['eligible'])
        self.assertEqual(result['as_of'], '2026-09-11T15:00:00+08:00')

    def test_three_branches_preserve_same_stock_and_rank(self):
        report = fixture()
        row = copy.deepcopy(report['candidates'][0]); row['branch'] = 'two_to_three'; row['rank'] = 4
        report['candidates'].append(row)
        row = copy.deepcopy(row); row['branch'] = 'hold_breakout'; row['rank'] = None
        report['candidates'].append(row)
        result = self.import_report(report)
        self.assertEqual(len(result['candidates']), 3)
        self.assertEqual(len({r['key'] for r in result['candidates']}), 3)
        self.assertEqual(result['candidates'][1]['rank'], 4)
        self.assertIsNone(result['candidates'][2]['rank'])

    def test_no_replacement_by_fourth(self):
        report = fixture(); report['candidates'][0]['rank'] = 4
        live, frames = live_fixture()
        self.assertFalse(self.import_report(report, live=live, snapshots=frames)['candidates'][0]['eligible'])

    def test_missing_is_not_zero_and_units_remain_shares_yuan(self):
        report = fixture(); row = report['candidates'][0]
        row['shape'] = {'anchor_date': '2026-09-07', 'anchor_low': 9.8, 'anchor_mid': 10.5,
                        'anchor_high': 11, 'post_days': 4, 'median_volume_ratio': None}
        row['five_day']['rows'][0]['amount'] = None
        candidate = self.import_report(report)['candidates'][0]
        self.assertEqual(candidate['bars'][0]['volume_shares'], 23400)
        self.assertIsNone(candidate['bars'][0]['amount_cny'])
        self.assertIsNone(candidate['metrics']['整理量 / 首板量'])
        self.assertEqual([r['value'] for r in candidate['levels']], [9.8, 10.5, 11])

    def test_coverage_conflict_outside_errors_still_partial(self):
        report = fixture(); report['coverage']['pool_days']['2026-09-11']['metadata_verified'] = False
        result = self.import_report(report)
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(any('完整性' in x for x in result['missing']))

    def test_sector_unknown_and_candidate_missing_remain_partial(self):
        report = fixture(); report['sectors'][0]['verified'] = False
        report['candidates'][0].update(prequalified=False, reasons=['公告原文未核验'])
        result = self.import_report(report)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['candidates'][0]['status'], '待验证观察')

    def test_live_requires_explicit_evidence_and_uses_own_quote_time(self):
        live, frames = live_fixture()
        no_evidence = self.import_report(live=live)
        self.assertFalse(no_evidence['candidates'][0]['eligible'])
        result = self.import_report(live=live, snapshots=frames)
        self.assertTrue(result['candidates'][0]['eligible'])
        self.assertEqual(result['live_as_of'], '2026-09-14T09:58:00+08:00')
        self.assertEqual(result['live_valid_until'], '2026-09-14T09:59:30+08:00')

    def test_expired_live_only_historical_pass(self):
        live, frames = live_fixture()
        result = module.import_native(fixture(), live=live, snapshots=frames, as_of=NOW + dt.timedelta(minutes=3))
        self.assertFalse(result['candidates'][0]['eligible'])
        self.assertIn('历史', result['candidates'][0]['status'])

    def test_latest_quote_cannot_retime_old_quote(self):
        live, frames = live_fixture()
        frames[-1]['quotes']['600001']['time'] = '2026-09-14T09:57:00+08:00'
        self.assertFalse(self.import_report(live=live, snapshots=frames)['candidates'][0]['eligible'])

    def test_replay_never_current(self):
        live, frames = live_fixture(); live.update(mode='replay', replay_only=True)
        result = self.import_report(live=live, snapshots=frames)
        self.assertFalse(result['candidates'][0]['eligible'])
        self.assertEqual(result['phase'], 'replay')

    def test_global_live_early_return_keeps_watchlist(self):
        result = self.import_report(live={'mode': 'live', 'time': NOW.isoformat(), 'reasons': ['当前市场资料缺失']})
        self.assertEqual(len(result['candidates']), 1)
        self.assertIn('当前市场资料缺失', result['missing'])
        self.assertIn('当前市场资料缺失', result['candidates'][0]['reasons'])

    def test_before_950_and_lunch_cannot_be_current(self):
        live, frames = live_fixture()
        for now in (NOW.replace(hour=9, minute=30), NOW.replace(hour=12, minute=10)):
            self.assertFalse(module.import_native(fixture(), live=live, snapshots=frames, as_of=now)['candidates'][0]['eligible'])

    def test_prior_close_stale_and_future_report_fail_closed(self):
        stale = module.import_native(fixture(), as_of=NOW + dt.timedelta(days=1))
        self.assertEqual(stale['status'], 'partial')
        self.assertTrue(any('过期' in x for x in stale['missing']))
        report = fixture(); report['as_of'] = '2026-09-15'
        self.assertEqual(self.import_report(report)['status'], 'unavailable')

    def test_rejected_and_started_statuses_preserved(self):
        for original, expected in [('rejected', '不做'), ('tracking', '已启动跟踪'), ('downgraded', '降级观察')]:
            report = fixture(); report['candidates'][0].update(status=original, prequalified=False)
            self.assertEqual(self.import_report(report)['candidates'][0]['status'], expected)

    def test_empty_is_valid_only_with_sufficient_evidence(self):
        report = fixture(); report['candidates'] = []
        self.assertEqual(self.import_report(report)['status'], 'empty')
        report['coverage'] = {}
        self.assertEqual(self.import_report(report)['status'], 'partial')

    def test_duplicate_key_is_not_counted_twice(self):
        report = fixture(); report['candidates'].append(copy.deepcopy(report['candidates'][0]))
        result = self.import_report(report)
        self.assertEqual(len(result['candidates']), 1)
        self.assertEqual(result['status'], 'partial')

    def test_private_paths_not_rendered_and_unsafe_links_removed(self):
        report = fixture(); report['errors'] = ['读取失败 /Users/artificial/private/input.json']
        report['sources'] = [{'url': 'file:///Users/artificial/secret.json'}, {'url': 'javascript:alert(1)'}]
        result = self.import_report(report)
        self.assertNotIn('/Users/', json.dumps(result, ensure_ascii=False))
        self.assertEqual(result['sources'], [])

    def test_private_file_input_rejects_outside_and_symlink(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as other:
            good = Path(folder) / 'good.json'; good.write_text('{}')
            outside = Path(other) / 'other.json'; outside.write_text('{}')
            link = Path(folder) / 'link.json'; link.symlink_to(outside)
            with patch.dict(os.environ, {'AUTOSTRATEGY_YICHUJIFA_RUNTIME': folder}):
                self.assertEqual(module._read_private(good), {})
                for path in (outside, link):
                    with self.assertRaises(ValueError): module._read_private(path)

    def test_research_dictionary_path_and_history_do_not_execute_scan(self):
        with patch.object(module.subprocess, 'run') as run:
            result = module.research(NOW, input_data=fixture())
            self.assertEqual(result['status'], 'complete')
            self.assertEqual(module.research(NOW, input_data='arbitrary.json')['status'], 'unavailable')
            self.assertEqual(module.research(NOW - dt.timedelta(days=100))['status'], 'unavailable')
            run.assert_not_called()

    def test_live_wrapper_handles_explicit_result_and_snapshots(self):
        live, frames = live_fixture()
        result = module.research(NOW, input_data={'report': fixture()}, live_data={'live': live, 'snapshots': frames})
        self.assertTrue(result['candidates'][0]['eligible'])

    def test_malformed_optional_payload_fails_closed(self):
        self.assertEqual(self.import_report(live=[] )['status'], 'complete')
        self.assertEqual(self.import_report(live=['invalid'])['status'], 'unavailable')
        report = fixture(); report['coverage']['pool_days'] = ['invalid']
        self.assertEqual(self.import_report(report)['status'], 'unavailable')

    def test_inconsistent_prequalification_and_unknown_permission_fail_closed(self):
        live, frames = live_fixture()
        report = fixture(); report['candidates'][0]['reasons'] = ['公告原文未核验']
        self.assertFalse(self.import_report(report, live=live, snapshots=frames)['candidates'][0]['eligible'])
        frames[-1]['quotes']['600001']['tradable'] = None
        self.assertFalse(self.import_report(live=live, snapshots=frames)['candidates'][0]['eligible'])

    def test_nonfinite_native_values_do_not_break_json(self):
        report = fixture(); report['candidates'][0]['score'] = float('nan')
        result = self.import_report(report)
        json.dumps(result, allow_nan=False)
        self.assertIsNone(result['candidates'][0]['metrics']['板内评分'])

    def test_market_checks_and_sector_reviews_use_native_evidence(self):
        report = fixture(); report['sectors'][0].update(flags={'trend_weak': None, 'core_losses': False},
                                                       counts=[2, 2, 1, 1, 2], reopen_condition='人工恢复条件')
        result = self.import_report(report)
        self.assertEqual(len(result['market_checks']), 4)
        self.assertIn('行业趋势转弱：待验证', result['sector_reviews'][0]['reasons'])
        self.assertEqual(result['sector_reviews'][0]['recovery'], ['人工恢复条件'])

    def test_evidence_date_conflicts_and_closed_day_are_not_accepted(self):
        self.assertEqual(self.import_report(evidence={'as_of': '2026-09-10'})['status'], 'unavailable')
        report = fixture(); report['as_of'] = '2026-09-12'
        self.assertEqual(self.import_report(report)['status'], 'unavailable')

    def test_global_failure_blocks_inconsistent_row_success(self):
        live, frames = live_fixture(); live['reasons'] = ['市场修复资料缺失']
        self.assertFalse(self.import_report(live=live, snapshots=frames)['candidates'][0]['eligible'])


if __name__ == '__main__':
    unittest.main()
