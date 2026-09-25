"""Fictional same-session morphology review; no feeds, clocks or private data."""
import copy
import datetime as dt
import unittest

from sector_radar_focus import REQUIRED, SOURCE_HASH, build_prelaunch_focus
from sector_radar_views import prelaunch_focus_section


SIGNAL = '2026-09-24'
ASOF = SIGNAL + 'T15:00:00+08:00'


def fixture(count=4):
    days = [(dt.date(2026, 6, 1) + dt.timedelta(days=i)).isoformat() for i in range(116)
            if (dt.date(2026, 6, 1) + dt.timedelta(days=i)).weekday() < 5]
    report = {'signal_date': SIGNAL, 'as_of': ASOF, 'candidates': [], 'sectors': [
        {'id': 'fiction-a', 'label': '虚构板块甲', 'member_codes': [], 'top_codes': ['600009'],
         'coverage': {'complete': True, 'membership_verified': True}, 'missing': []},
        {'id': 'fiction-empty', 'label': '虚构空板块', 'member_codes': [], 'top_codes': [],
         'coverage': {'complete': True, 'membership_verified': True}, 'missing': []}]}
    native = {'signal_date': SIGNAL, 'as_of': ASOF, 'status': 'partial',
              'rules': {'source_hash': SOURCE_HASH}, 'input_fingerprint': 'fictional-input',
              'watch': [], 'core': [], 'started': [], 'invalid': [], 'missing': []}
    for i in range(count):
        code = str(600001 + i)
        conditions = [{'label': label, 'passed': True, 'detail': '人工验证'} for label in REQUIRED]
        conditions += [{'label': '未加速且最近5日无实际收盘涨停', 'passed': None, 'detail': '历史涨停待核验'},
                       {'label': '最近压力及扣费净空间≥2', 'passed': None, 'detail': '费用未知'}]
        native['watch'].append({'code': code, 'name': '虚构企业' + str(i), 'status': '待验证', 'eligible': False,
                                'bars': [{'date': d, 'close': 10} for d in days], 'conditions': conditions,
                                'metrics': {'5日涨幅%': 3, '20日涨幅%': 6, '中位量比': 1.4, '试盘日期': '2026-09-18'},
                                'levels': [{'label': '参考收盘', 'value': 10}, {'label': '冻结支撑', 'value': 9},
                                           {'label': '冻结上沿', 'value': 13}], 'sources': []})
        report['candidates'].append({'code': code, 'name': '虚构企业' + str(i), 'state': 'pending',
                                      'groups': ['fiction-a'], 'announcement_verified': False,
                                      'strategy_checks': {'prelaunch': {'status': 'pending', 'observation_passed': False}},
                                      'ranks': {'fiction-a': {'rank': None, 'score': 10 + i}},
                                      'metrics': {'median_amount_5d': 1000000}})
        report['sectors'][0]['member_codes'].append(code)
    return report, native


class RadarFocusTests(unittest.TestCase):
    def test_does_not_mutate_original_qualifications_or_rank_and_keeps_three_slots(self):
        report, native = fixture()
        before = copy.deepcopy((report, native))
        focus = build_prelaunch_focus(report, native)
        self.assertEqual((report, native), before)
        self.assertEqual([r['code'] for r in focus['sectors'][0]['entries']], ['600004', '600003', '600002'])
        self.assertEqual(focus['sectors'][0]['omitted_count'], 1)
        self.assertTrue(all(r['tier'] == 'pending' and not r['actionable'] for r in focus['sectors'][0]['entries']))
        self.assertEqual(focus['sectors'][1]['entries'], [])
        self.assertIn('历史涨停待核验', str(focus))
        self.assertIn('费用', str(focus))

    def test_partial_sector_or_partial_scores_use_code_order_not_partial_rank(self):
        for mode in ('coverage', 'score'):
            report, native = fixture()
            if mode == 'coverage':
                report['sectors'][0]['coverage']['membership_verified'] = False
            else:
                report['candidates'][1]['ranks'] = {}
            group = build_prelaunch_focus(report, native)['sectors'][0]
            self.assertEqual([r['code'] for r in group['entries']], ['600001', '600002', '600003'])
            self.assertIn('不是完整板块排名', group['ordering'])

    def test_old_or_different_rule_report_is_rejected(self):
        for field in ('date', 'rule', 'unavailable'):
            report, native = fixture()
            if field == 'date': native['signal_date'] = '2026-09-23'
            elif field == 'rule': native['rules']['source_hash'] = 'other'
            else: native['status'] = 'unavailable'
            result = build_prelaunch_focus(report, native)
            self.assertEqual(result['status'], 'unavailable')
            self.assertFalse(result['sectors'])

    def test_known_failures_unknown_history_and_terminal_state_not_promoted(self):
        for mode in ('failed', 'history', 'trend', 'expired', 'excluded', 'duplicate', 'future'):
            report, native = fixture(1)
            row = native['watch'][0]
            if mode == 'failed': row['conditions'].append({'label': '固定行业相对强度与广度', 'passed': False})
            elif mode in ('history', 'trend'): row['conditions'][1 if mode == 'history' else 2]['passed'] = None
            elif mode == 'expired': row['status'] = '观察到期'
            elif mode == 'excluded': report['candidates'][0]['state'] = 'excluded'
            elif mode == 'duplicate': row['bars'].append(copy.deepcopy(row['bars'][-1]))
            else: row['bars'].append({'date': '2026-09-28', 'close': 100})
            self.assertEqual(build_prelaunch_focus(report, native)['sectors'][0]['entries'], [], mode)

    def test_started_state_never_recovers_when_returns_cool(self):
        report, native = fixture(1)
        row = native['watch'].pop()
        row['status'] = '已启动'; row['metrics']['5日涨幅%'] = -3
        native['started'].append(row)
        result = build_prelaunch_focus(report, native)['sectors'][0]
        self.assertEqual(result['entries'], [])
        self.assertEqual(result['started'][0]['code'], row['code'])

    def test_old_invalid_row_cannot_overwrite_current_record(self):
        report, native = fixture(1)
        old = copy.deepcopy(native['watch'][0])
        old.update(status='失效')
        old['bars'] = old['bars'][:-1]
        native['invalid'].append(old)
        result = build_prelaunch_focus(report, native)['sectors'][0]
        self.assertEqual(len(result['entries']), 1)
        self.assertEqual(result['entries'][0]['native_status'], '待验证')

    def test_known_gross_space_upper_bound_blocks_missing_fee_upgrade(self):
        report, native = fixture(1)
        native['watch'][0]['levels'][-1]['value'] = 10.03
        result = build_prelaunch_focus(report, native)['sectors'][0]
        self.assertEqual(result['entries'], [])
        self.assertEqual(result['rejected_count'], 1)
        near = result['near_misses'][0]
        self.assertAlmostEqual(near['gross_rr_upper_bound'], .03)
        self.assertIn('小于原规则净空间≥2', near['failure_reasons'][0])
        self.assertIn('空间上限不足', result['rejection_reasons'][0]['reason'])

    def test_exact_two_gross_bound_still_pending_not_core_without_costs(self):
        report, native = fixture(1)
        native['watch'][0]['levels'][-1]['value'] = 12
        result = build_prelaunch_focus(report, native)['sectors'][0]['entries'][0]
        self.assertEqual(result['tier'], 'pending')
        self.assertFalse(result['native_core'])

    def test_trial_without_two_absorption_days_is_counterexample_never_priority(self):
        report, native = fixture(1)
        row = native['watch'][0]
        next(c for c in row['conditions'] if c['label'] == '试盘后至少两日缩量承接')['passed'] = False
        row['levels'][-1]['value'] = 11
        result = build_prelaunch_focus(report, native)['sectors'][0]
        self.assertEqual(result['entries'], [])
        self.assertEqual(result['near_misses'][0]['code'], row['code'])
        self.assertIn('试盘后至少两日缩量承接', result['near_misses'][0]['failure_reasons'])
        self.assertIn('小于原规则净空间≥2', str(result['near_misses'][0]['failure_reasons']))

    def test_original_portfolio_cap_cannot_be_bypassed_by_pending_review(self):
        report, native = fixture(1)
        native['watch'][0]['reasons'] = ['符合个股数值资格，受Top10行业/催化组合上限限制']
        result = build_prelaunch_focus(report, native)['sectors'][0]
        self.assertEqual(result['entries'], [])
        self.assertIn('组合上限', str(result['rejection_reasons']))

    def test_core_first_only_with_native_and_radar_evidence_complete(self):
        report, native = fixture()
        row = native['watch'][0]
        for condition in row['conditions']: condition['passed'] = True
        row.update(eligible=True, status='蓄势')
        row['metrics']['净空间比'] = 2.8
        radar = report['candidates'][0]
        radar.update(state='watch', announcement_verified=True)
        radar['strategy_checks']['prelaunch'].update(status='passed', observation_passed=True,
                                                    as_of=ASOF, rule_hash=SOURCE_HASH)
        result = build_prelaunch_focus(report, native)['sectors'][0]
        self.assertEqual(result['entries'][0]['code'], '600001')
        self.assertEqual(result['entries'][0]['tier'], 'core')
        report['sectors'][0]['coverage']['membership_verified'] = False
        self.assertTrue(all(r['tier'] == 'pending' for r in build_prelaunch_focus(report, native)['sectors'][0]['entries']))

    def test_readonly_render_escapes_markup_shows_counterevidence_and_historical_state(self):
        report, native = fixture(2)
        native['watch'][0]['name'] = '<script>bad()</script>'
        native['watch'][0]['levels'][-1]['value'] = 10.03
        report['prelaunch_focus'] = build_prelaunch_focus(report, native)
        text = prelaunch_focus_section(report, False)
        self.assertIn('低位形态优先看', text)
        self.assertIn('未入选 · 条件未满足', text)
        self.assertIn('&lt;script&gt;', text)
        self.assertNotIn('<script>', text)
        self.assertNotIn('虚构空板块', text)
        historical = prelaunch_focus_section(report, True)
        self.assertIn('历史复核 · 不取得当前资格', historical)
        self.assertNotIn('>待启动形态 · 待验证', historical)


if __name__ == '__main__':
    unittest.main()
