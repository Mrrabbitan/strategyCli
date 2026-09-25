"""Fictional private-store fixtures for the daily radar presentation only."""
import copy
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from research_store import atomic_json, research_path
from sector_radar_daily import TZ, build_daily_model, load_daily_reports


def fixture(day='2026-09-24', generated=None):
    source = day + 'T15:00:00+08:00'
    pending = {'code': '600901', 'name': '虚构甲', 'as_of': source, 'price': 10,
               'tier': 'pending', 'return_5d_pct': 2.5, 'return_20d_pct': 7,
               'probe_evidence': '试盘后缩量承接', 'support': 9, 'upper': 13,
               'missing': ['费用待验证'], 'confirmation': '补齐原生证据再复核',
               'sources': [{'label': '人工资料', 'url': 'https://example.com/evidence'}]}
    near = {'code': '600902', 'name': '虚构乙', 'as_of': source, 'price': 12,
            'failure_reasons': ['最近压力下的空间不足'], 'support': 11, 'pressure': 12.1,
            'confirmation': '不能绕过最近压力', 'missing': ['公告待核验']}
    started = {'code': '600903', 'name': '虚构丙', 'as_of': source, 'native_status': '已启动',
               'price': 15, 'return_5d_pct': 15, 'confirmation': '保持原退出纪律'}
    return {'module_id': 'sector-radar', 'schema_version': 1, 'status': 'partial',
            'signal_date': day, 'as_of': source,
            'generated_at': generated or day + 'T16:00:00+08:00',
            'valid_until': '2026-09-28T15:00:00+08:00', 'missing': ['人工缺口'],
            'sectors': [dict(id=sid, label=label, top_codes=[], member_codes=['600901', '600902', '600903'],
                             coverage={'complete': True, 'membership_verified': True})
                        for sid, label in [('a', '虚构板块甲'), ('b', '虚构板块乙')]],
            'candidates': [{'code': '600901', 'name': '虚构甲', 'state': 'pending', 'bars': [],
                            'ranks': {'a': {'score': 99}}, 'account': {'cost': 'SECRET-POSITION'}}],
            'prelaunch_focus': {'signal_date': day, 'status': 'partial',
                'sectors': [{'id': 'a', 'entries': [pending], 'near_misses': [near],
                             'started': [started], 'omitted_count': 0},
                            {'id': 'b', 'entries': [copy.deepcopy(pending)],
                             'near_misses': [copy.deepcopy(near)], 'started': [copy.deepcopy(started)]}]}}


def add_observed(report, code='600904'):
    check = {'status': 'passed', 'observation_passed': True, 'as_of': report['as_of'],
             'rule_version': 'fictional-v1', 'rule_hash': 'fictional-hash',
             'sources': [{'url': 'https://example.com/native'}], 'confirmation': ['次日重新确认'],
             'levels': [{'label': '冻结支撑', 'value': 8}], 'risks': ['跌破结构失效']}
    report['candidates'].append({'code': code, 'name': '虚构正式观察', 'state': 'watch',
                                 'announcement_verified': True, 'strategy_checks': {'prelaunch': check},
                                 'bars': [{'date': report['signal_date'], 'close': 10}],
                                 'metrics': {'return_5d': .025}})
    for sector in report['sectors']:
        sector['top_codes'] = [code]
        sector['member_codes'].append(code)


class DailyModelTests(unittest.TestCase):
    def test_saved_pending_and_failed_near_misses_are_visible_not_admitted(self):
        report = fixture()
        original = copy.deepcopy(report)
        result = build_daily_model(report)
        self.assertEqual(result['counts'], {'review': 2, 'observed': 0, 'started': 1})
        self.assertEqual(report, original)
        self.assertEqual(result['review'][0]['r5'], 2.5)
        self.assertEqual([g['id'] for g in result['review'][0]['groups']], ['a', 'b'])
        self.assertFalse(any(r['admitted'] or r['actionable'] for r in result['review']))
        failed = result['review'][1]
        self.assertIn('条件失败', failed['status'])
        self.assertEqual(failed['failure_reasons'], ['最近压力下的空间不足'])
        self.assertIn('不进入正式观察', failed['next_check'][0])
        self.assertEqual(result['started'][0]['status'], '已启动跟踪 · 不列低位候选')

    def test_no_focus_never_invents_review_from_scores_or_entire_universe(self):
        report = fixture()
        del report['prelaunch_focus']
        report['candidates'][0]['ranks']['a']['score'] = 100
        result = build_daily_model(report)
        self.assertEqual(result['review'], [])
        self.assertIn('不从全部股票', result['notes'][0])

    def test_near_miss_limits_are_disclosed_and_saved_rows_never_retrimmed(self):
        report = fixture()
        group = report['prelaunch_focus']['sectors'][0]
        group['omitted_count'] = 4
        for i in range(10):
            row = copy.deepcopy(group['near_misses'][0]); row['code'] = str(601100 + i)
            group['near_misses'].append(row)
        result = build_daily_model(report)
        self.assertEqual(len(result['review']), 12)
        self.assertTrue(any('未保存' in note for note in result['notes']))

    def test_observed_requires_original_pass_and_top_codes_not_score(self):
        report = fixture(); add_observed(report)
        result = build_daily_model(report)
        self.assertEqual([r['code'] for r in result['observed']], ['600904'])
        row = result['observed'][0]
        self.assertTrue(row['admitted']); self.assertFalse(row['actionable'])
        self.assertEqual(row['price'], 10); self.assertEqual(row['r5'], 2.5)
        self.assertEqual(len(row['groups']), 2)
        self.assertEqual(row['supporting_skills'], [{'id': 'prelaunch', 'label': '启动前潜伏'}])
        for case in ('pending', 'flag', 'state', 'date', 'coverage'):
            bad = copy.deepcopy(report); native = bad['candidates'][-1]['strategy_checks']['prelaunch']
            if case == 'pending': native['status'] = 'pending'
            elif case == 'flag': native['observation_passed'] = False
            elif case == 'state': bad['candidates'][-1]['state'] = 'pending'
            elif case == 'date': native['as_of'] = '2026-09-23T15:00:00+08:00'
            else:
                for s in bad['sectors']: s['coverage']['membership_verified'] = False
            self.assertEqual(build_daily_model(bad)['observed'], [], case)

    def test_finalized_observation_is_not_recomputed_from_metadata(self):
        report = fixture(); add_observed(report)
        row = report['candidates'][-1]
        row.pop('announcement_verified')
        row['strategy_checks']['prelaunch'] = {'status': 'passed', 'observation_passed': True}
        self.assertEqual([r['code'] for r in build_daily_model(report)['observed']], ['600904'])

    def test_forward_and_header_only_copy_finalized_whitelisted_fields(self):
        report = fixture()
        report['coverage'] = {'sectors_expected': 12, 'membership_verified': 12, 'sectors_complete': 12,
                              'unique_stocks': 50, 'forward_ready': True, 'secret': 'PRIVATE-COVERAGE'}
        report['forward_top'] = ['a']
        report['sectors'][0].update(forward_rank=1, confirmation=['等待量价确认'], risks=['强度转弱失效'])
        model = build_daily_model(report)
        self.assertEqual(model['valid_until'], report['valid_until'])
        self.assertEqual(model['forward'][0]['label'], '虚构板块甲')
        self.assertNotIn('secret', model['coverage'])
        for selected in ({'id': 'a'}, {'sector_id': 'a'}):
            report['forward_top'] = [selected]
            self.assertEqual(build_daily_model(report)['forward'][0]['id'], 'a')
        report['coverage']['forward_ready'] = False
        self.assertEqual(build_daily_model(report)['forward'], [])

    def test_price_and_twenty_day_return_use_sorted_unique_cutoff_bars(self):
        report = fixture(); add_observed(report)
        row = report['candidates'][-1]
        days = [(dt.date(2026, 9, 24) - dt.timedelta(days=i)).isoformat() for i in range(21)]
        row['bars'] = [{'date': day, 'close': 20 - i / 2} for i, day in enumerate(days)]
        row['bars'].append({'date': days[-1], 'close': 10})
        row['bars'].append({'date': '2026-09-28', 'close': 99})
        view = build_daily_model(report)['observed'][0]
        self.assertEqual(view['price'], 20)
        self.assertEqual(view['r20'], 100)

    def test_observed_not_duplicated_in_review_and_started_not_relabelled_low(self):
        report = fixture(); add_observed(report, '600901')
        result = build_daily_model(report)
        self.assertEqual([r['code'] for r in result['review']], ['600902'])
        report = fixture()
        started = report['prelaunch_focus']['sectors'][0]['started'][0]
        started['code'] = '600901'
        self.assertNotIn('600901', [r['code'] for r in build_daily_model(report)['review']])

    def test_native_core_not_in_sector_top_is_preserved_without_reordering_top(self):
        report = fixture(); add_observed(report, '600901')
        for sector in report['sectors']:
            sector['top_codes'] = []
        entry = report['prelaunch_focus']['sectors'][1]['entries'][0]
        entry.update(tier='core', native_core=True)
        original = copy.deepcopy(report)
        result = build_daily_model(report)
        self.assertEqual(report, original)
        self.assertEqual([r['code'] for r in result['observed']], ['600901'])
        core = result['observed'][0]
        self.assertEqual(core['observation_origin'], 'prelaunch_core')
        self.assertIn('非板块前三排名', core['status'])
        self.assertEqual(core['supporting_skills'], [{'id': 'prelaunch', 'label': '启动前潜伏'}])
        self.assertNotIn('600901', [r['code'] for r in result['review']])
        for sector in report['sectors']:
            sector['top_codes'] = ['600901']
        merged = build_daily_model(report)['observed']
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]['observation_origin'], 'sector_top')

    def test_core_requires_saved_confirmation_and_started_conflict_blocks_latent_core(self):
        for case in ('flag', 'tier', 'failed', 'native', 'started'):
            report = fixture(); add_observed(report, '600901')
            for sector in report['sectors']:
                sector['top_codes'] = []
            entry = report['prelaunch_focus']['sectors'][0]['entries'][0]
            entry.update(tier='core', native_core=True)
            if case == 'flag': entry['native_core'] = False
            elif case == 'tier': entry['tier'] = 'pending'
            elif case == 'failed': entry['failure_reasons'] = ['结构失效']
            elif case == 'native': report['candidates'][-1]['strategy_checks']['prelaunch']['status'] = 'pending'
            else: report['prelaunch_focus']['sectors'][1]['started'][0]['code'] = '600901'
            self.assertEqual(build_daily_model(report)['observed'], [], case)

    def test_old_focus_future_entry_and_unavailable_report_cannot_appear(self):
        report = fixture(); report['prelaunch_focus']['signal_date'] = '2026-09-23'
        self.assertEqual(build_daily_model(report)['review'], [])
        report = fixture()
        for group in report['prelaunch_focus']['sectors']:
            group['entries'][0]['as_of'] = '2026-09-28T15:00:00+08:00'
        self.assertEqual([r['code'] for r in build_daily_model(report)['review']], ['600902'])
        report['status'] = 'unavailable'
        self.assertEqual(build_daily_model(report)['review'], [])

    def test_allowlist_hides_matrix_private_fields_paths_and_unsafe_links(self):
        report = fixture(); add_observed(report)
        entry = report['prelaunch_focus']['sectors'][0]['entries'][0]
        entry['private_key'] = 'SECRET-KEY'
        entry['missing'].append('缓存 /Users/fictional/private/cache.json 未取得')
        entry['sources'] += [{'url': url} for url in [
            'file:///Users/fictional/secret', 'javascript:alert(1)', 'http://127.0.0.1/private',
            'https://user:secret@example.com/x', 'https://example.com/?api_key=secret',
            'https://localhost/private', 'https://192.168.1.1/private']]
        data = json.dumps(build_daily_model(report), ensure_ascii=False)
        for text in ('SECRET-POSITION', 'SECRET-KEY', 'strategy_checks', '/Users/',
                     'file://', 'javascript:', 'api_key', '127.0.0.1', '192.168.'):
            self.assertNotIn(text, data)
        self.assertIn('https://example.com/evidence', data)

    def test_historical_state_preserves_original_data_but_no_current_action(self):
        report = fixture(); add_observed(report)
        result = build_daily_model(report, historical=True)
        self.assertTrue(result['historical'])
        self.assertTrue(all(r['historical'] and not r['actionable'] for group in ('review', 'observed', 'started') for r in result[group]))


class DailyStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'AUTOSTRATEGY_PRIVATE_ROOT': self.temp.name})
        self.env.start()
        self.now = dt.datetime(2026, 9, 25, 16, tzinfo=TZ)

    def tearDown(self):
        self.env.stop(); self.temp.cleanup()

    def save(self, name, report):
        atomic_json(research_path('sector_radar/' + name), report)

    def test_same_day_dedup_uses_report_clock_not_filename_or_mtime(self):
        self.save('current.json', fixture(generated='2026-09-24T16:00:00+08:00'))
        latest = fixture(generated='2026-09-25T12:00:00+08:00')
        latest['prelaunch_focus']['sectors'][0]['entries'][0]['name'] = '较晚的人工复核'
        self.save('history/first-filename.json', latest)
        self.save('history/z-last-filename.json', fixture(generated='2026-09-24T17:00:00+08:00'))
        self.save('history/older-day.json', fixture('2026-09-23'))
        result = load_daily_reports(self.now)
        self.assertEqual([r['signal_date'] for r in result['days']], ['2026-09-24', '2026-09-23'])
        self.assertEqual(result['days'][0]['review'][0]['name'], '较晚的人工复核')
        self.assertFalse(result['days'][0]['historical']); self.assertTrue(result['days'][1]['historical'])

    def test_loader_completes_under_builder_update_lock(self):
        self.save('current.json', fixture())
        program = '''
import datetime as dt
from research_store import update_lock
from sector_radar_daily import load_daily_reports, TZ
with update_lock():
    result = load_daily_reports(dt.datetime(2026, 9, 25, 16, tzinfo=TZ))
    assert len(result['days']) == 1
'''
        result = subprocess.run([sys.executable, '-c', program],
                                cwd=str(Path(__file__).resolve().parents[1]),
                                env=os.environ.copy(), capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_failure_keeps_old_date_and_separate_warning(self):
        self.save('current.json', fixture())
        self.save('attempt.json', {'status': 'unavailable', 'attempted_at': self.now.isoformat(),
                                  'summary': '人工网络故障', 'missing': ['源数据不足'],
                                  'private_account': 'NOT-EXPOSED'})
        self.save('latest_attempt.json', {'module_id': 'sector-radar', 'status': 'unavailable',
                                        'generated_at': self.now.isoformat()})
        result = load_daily_reports(self.now)
        self.assertEqual(result['state'], 'unavailable')
        self.assertEqual(result['days'][0]['signal_date'], '2026-09-24')
        self.assertTrue(result['days'][0]['historical'])
        self.assertEqual(result['latest_attempt']['summary'], '人工网络故障')
        self.assertNotIn('NOT-EXPOSED', json.dumps(result))

    def test_future_report_future_bars_and_mismatched_day_are_rejected(self):
        self.save('current.json', fixture())
        self.save('history/future-report.json', fixture(generated='2026-09-26T16:00:00+08:00'))
        future = fixture('2026-09-28'); self.save('history/future-market.json', future)
        bars = fixture('2026-09-23')
        bars['candidates'][0]['bars'] = [{'date': '2026-09-28', 'close': 30}]
        self.save('history/future-bar.json', bars)
        mismatch = fixture('2026-09-22'); mismatch['as_of'] = '2026-09-23T15:00:00+08:00'
        self.save('history/wrong-date.json', mismatch)
        self.save('attempt.json', {'status': 'unavailable', 'attempted_at': '2026-09-28T16:00:00+08:00'})
        result = load_daily_reports(self.now)
        self.assertEqual(len(result['days']), 1)
        self.assertEqual(result['latest_attempt'], {})
        self.assertEqual(result['state'], 'partial')

    def test_twenty_day_cap_expiry_and_empty_store(self):
        self.assertEqual(load_daily_reports(self.now)['days'], [])
        for i in range(25):
            day = (dt.date(2026, 9, 24) - dt.timedelta(days=i)).isoformat()
            report = fixture(day); report['valid_until'] = '2026-09-24T15:00:00+08:00'
            self.save('history/%s.json' % i, report)
        result = load_daily_reports(self.now, limit=100)
        self.assertEqual(len(result['days']), 20)
        self.assertEqual(result['state'], 'expired')
        self.assertTrue(all(day['historical'] for day in result['days']))
        self.assertEqual(load_daily_reports(self.now, limit=0)['days'], [])

    def test_symlink_history_cannot_import_other_private_store(self):
        outside = research_path('other_module/private.json')
        atomic_json(outside, fixture())
        history = research_path('sector_radar/history'); history.mkdir(parents=True)
        (history / 'linked.json').symlink_to(outside)
        self.assertEqual(load_daily_reports(self.now)['days'], [])


if __name__ == '__main__':
    unittest.main()
