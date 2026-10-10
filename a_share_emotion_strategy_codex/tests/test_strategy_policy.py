"""Private research choices block execution, imports and live-looking history."""
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

import strategy_policy as policy
import refresh_research as refresh
import research_modules as modules
from research_store import private_path, research_path, atomic_json, read_json

PAUSED = ('prelaunch', 'yichujifa', 'late-day', 'three-step', 'event-timing')
NOW = dt.datetime(2026, 9, 11, 16, tzinfo=modules.TZ)


class StrategyPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {'AUTOSTRATEGY_PRIVATE_ROOT': self.temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)

    def configure(self):
        data = {'schema_version': 1, 'strategies': {
            name: {'state': 'paused', 'reason': '人工研究选择：暂停'} for name in PAUSED}}
        data['strategies'].update({name: {'state': 'active'} for name in ('dragon', 'macd', 'catch-up-dragon')})
        atomic_json(private_path('config/research_policy.json'), data)

    def test_absent_policy_preserves_defaults_and_private_policy_wins(self):
        with patch.object(policy, 'load_config', side_effect=FileNotFoundError):
            self.assertFalse(policy.is_paused('prelaunch'))
            self.assertIsNone(policy.policy_fingerprint())
        self.configure()
        for name in PAUSED:
            self.assertTrue(policy.is_paused(name))
        for name in ('dragon', 'macd', 'catch-up-dragon', 'hot', 'sector-radar'):
            self.assertFalse(policy.is_paused(name))
        self.assertEqual(policy.policy_status('prelaunch')['reason'], '人工研究选择：暂停')
        self.assertEqual(len(policy.policy_fingerprint()), 64)

    def test_corrupt_or_unknown_policy_stops_selection_but_keeps_context(self):
        file = private_path('config/research_policy.json')
        file.parent.mkdir(parents=True)
        for content in ('{broken', '{"schema_version":1,"strategies":{"dragon":{"state":"unknown"}}}'):
            file.write_text(content)
            self.assertTrue(policy.is_paused('dragon'))
            self.assertFalse(policy.is_paused('hot'))
            self.assertFalse(policy.is_paused('sector-radar'))
            with self.assertRaises(policy.StrategyPausedError):
                policy.require_enabled('dragon')

    def test_refresh_pause_does_not_execute_or_replace_any_history(self):
        self.configure()
        file = research_path('prelaunch/current.json')
        atomic_json(file, {'module_id': 'prelaunch', 'summary': '人工历史记录', 'frozen_records': [{'support': 10}]})
        before = file.read_bytes()
        with patch.object(refresh, 'run_module') as native, patch.object(refresh, 'build') as builder, patch.object(refresh, 'record_failure') as failure:
            result = refresh.refresh(['prelaunch', 'yichujifa', 'three-step'], as_of=NOW)
        self.assertTrue(all(row['status'] == 'paused' for row in result['modules']))
        native.assert_not_called(); builder.assert_not_called(); failure.assert_not_called()
        self.assertEqual(file.read_bytes(), before)
        self.assertFalse(research_path('prelaunch/attempt.json').exists())
        with self.assertRaises(policy.StrategyPausedError):
            refresh.run_module('prelaunch', NOW, 'close', input_data={'module_id': 'prelaunch', 'schema_version': 1})
        with self.assertRaises(policy.StrategyPausedError):
            modules.publish('prelaunch', {'module_id': 'prelaunch', 'status': 'complete'})
        self.assertFalse(modules.eligible_now('prelaunch', {'eligible': True, 'valid_until': '2026-09-12T15:00:00+08:00'}, {}, NOW))

    def test_all_preserves_active_hot_and_dragon_and_skips_paused(self):
        self.configure()
        fake = lambda module, *_args, **_kwargs: {'module_id': module, 'schema_version': 1, 'status': 'partial',
                'as_of': NOW.isoformat(), 'generated_at': NOW.isoformat(), 'valid_until': '2026-09-12T15:00:00+08:00',
                'summary': '人工行情背景', 'candidates': []}
        with patch.object(refresh, 'run_module', side_effect=fake) as native:
            result = refresh.refresh(refresh.DEFAULT_MODULES, as_of=NOW, rebuild=False)
        self.assertEqual([call.args[0] for call in native.call_args_list], ['hot', 'dragon'])
        states = {row['module']: row['status'] for row in result['modules']}
        self.assertEqual(states, {'hot': 'partial', 'dragon': 'partial', 'yichujifa': 'paused', 'prelaunch': 'paused'})

    def test_direct_native_entries_reject_before_collecting_or_loading_engine(self):
        self.configure()
        import prelaunch_research, yichujifa_research, three_step_research, late_day_research
        calls = [lambda: prelaunch_research.research(NOW), lambda: prelaunch_research.collect(NOW),
                 lambda: yichujifa_research.research(NOW), lambda: yichujifa_research.import_native({}),
                 lambda: three_step_research.research(NOW), lambda: late_day_research.run_research({}, now=NOW),
                 lambda: late_day_research.publish_report({})]
        with patch.object(prelaunch_research, 'PublicFeed') as feed, patch.object(yichujifa_research.subprocess, 'run') as sampler, patch.object(three_step_research, 'load_engine') as engine, patch.object(late_day_research, 'load_engine') as late_engine:
            for call in calls:
                with self.assertRaises(policy.StrategyPausedError):
                    call()
        feed.assert_not_called(); sampler.assert_not_called(); engine.assert_not_called(); late_engine.assert_not_called()

    def test_radar_pause_never_calls_runner_or_rule_engine_and_retains_dragon(self):
        self.configure()
        import sector_radar_skills as radar
        runners = {name: Mock(return_value={'status': 'partial', 'missing': [], 'candidates': [], 'rules': {'version': 'artificial', 'source_hash': 'artificial'}}) for name in radar.MODULES}
        calendar = {'verified': True, 'source': 'https://example.invalid/calendar', 'days': ['2026-09-11', '2026-09-14']}
        with patch.dict(radar.RUNNERS, runners), patch.object(radar, '_rule', return_value=('artificial', 'artificial')) as rule:
            result = radar.run_checks(NOW, '2026-09-11', [{'code': '600001', 'name': '人工样例'}], calendar)
        for name in ('prelaunch', 'yichujifa', 'late-day', 'three-step'):
            runners[name].assert_not_called()
            self.assertEqual(result['by_code']['600001'][name]['status'], 'paused')
            self.assertFalse(result['by_code']['600001'][name]['observation_passed'])
        runners['dragon'].assert_called_once()
        self.assertTrue(all(call.args[0] == 'dragon' for call in rule.call_args_list))
        self.assertNotIn('prelaunch_focus_input', result)
        self.assertEqual(result['strategy_policy_fingerprint'], policy.policy_fingerprint())

    def test_radar_supplied_pass_cannot_borrow_paused_strategy(self):
        self.configure()
        from sector_radar_research import checked_cells
        from sector_radar_daily import _supporting
        cell = {'status': 'passed', 'observation_passed': True, 'as_of': NOW.isoformat(),
                'rule_hash': 'artificial', 'rule_version': 'artificial', 'sources': ['https://example.invalid']}
        checked = checked_cells({'prelaunch': cell, 'dragon': cell}, '2026-09-11', NOW)
        self.assertEqual(checked['prelaunch']['status'], 'paused')
        self.assertFalse(checked['prelaunch']['observation_passed'])
        old = {'state': 'watch', 'strategy_checks': {'prelaunch': cell}}
        self.assertEqual(_supporting(old, '2026-09-11'), [])
        self.assertEqual(len(_supporting(old, '2026-09-11', include_paused=True)), 1)

    def test_explicit_paused_cli_returns_refusal_without_research_mutation(self):
        self.configure()
        with patch('sys.argv', ['refresh_research.py', '--module', 'prelaunch']), patch('builtins.print'), patch.object(refresh, 'run_module') as native:
            self.assertEqual(refresh.main(), 2)
        native.assert_not_called()
        self.assertFalse(research_path('prelaunch').exists())

    def test_radar_old_policy_dates_stay_historical_without_relabeling(self):
        self.configure()
        from sector_radar_daily import load_daily_reports
        cell = {'status': 'passed', 'observation_passed': True, 'as_of': NOW.isoformat()}
        report = {'module_id': 'sector-radar', 'schema_version': 1, 'status': 'partial',
                  'signal_date': '2026-09-11', 'as_of': '2026-09-11T15:00:00+08:00',
                  'generated_at': NOW.isoformat(), 'valid_until': '2026-09-14T15:00:00+08:00',
                  'sectors': [{'id': 'artificial', 'label': '人工板块', 'top_codes': ['600001'],
                               'coverage': {'complete': True, 'membership_verified': True}}],
                  'candidates': [{'code': '600001', 'name': '人工旧观察', 'state': 'watch',
                                  'strategy_checks': {'prelaunch': cell}, 'bars': []}]}
        file = research_path('sector_radar/current.json')
        atomic_json(file, report)
        before = file.read_bytes()
        old = load_daily_reports(NOW)['days'][0]
        self.assertTrue(old['historical'])
        self.assertEqual(len(old['observed']), 1)
        self.assertEqual(old['signal_date'], '2026-09-11')
        self.assertEqual(file.read_bytes(), before)
        report['strategy_policy_fingerprint'] = policy.policy_fingerprint()
        atomic_json(file, report)
        current = load_daily_reports(NOW)['days'][0]
        self.assertFalse(current['historical'])
        self.assertEqual(current['observed'], [])

    def test_radar_comparison_uses_only_active_independent_support(self):
        self.configure()
        from test_sector_radar_research import fixture, evaluate
        data, checks = fixture()
        report = evaluate(data, checks)
        self.assertTrue(report['coverage']['matrix_complete'])
        self.assertTrue(report['coverage']['observation_complete'])
        self.assertTrue(all(not sector['top_codes'] for sector in report['sectors']))
        for cells in checks['by_code'].values():
            cells['dragon'].update(status='passed', observation_passed=True)
        report = evaluate(data, checks)
        self.assertTrue(all(row['supporting_skills'] == ['dragon'] for row in report['candidates']))
        self.assertTrue(all(len(sector['top_codes']) == 3 for sector in report['sectors']))

    def test_builder_paused_pages_keep_anchors_and_only_folded_history(self):
        self.configure()
        from build_investment_site import build
        from research_modules import FOLDERS
        for name in ('prelaunch', 'yichujifa', 'three-step'):
            atomic_json(research_path(FOLDERS[name] + '/current.json'), {'module_id': name,
                'schema_version': 1, 'status': 'complete', 'as_of': NOW.isoformat(),
                'valid_until': '2099-09-11T15:00:00+08:00', 'generated_at': NOW.isoformat(),
                'summary': '人工旧资格', 'candidates': [{'code': '600001', 'name': '人工历史甲', 'eligible': True}]})
        page = (build(dt.date(2026, 9, 11)) / 'latest.html').read_text()
        for name in ('prelaunch', 'yichujifa', 'late-day', 'three-step'):
            self.assertIn('data-strategy-paused="' + name + '"', page)
            self.assertIn('id="page-' + name + '"', page)
        self.assertIn('id="prelaunch-selection"', page)
        self.assertIn('id="yichujifa-selection"', page)
        self.assertIn('id="topic-late-day"', page)
        self.assertIn('class="paused-history"', page)
        self.assertIn('人工历史甲', page)
        self.assertNotIn('class="candidate-state positive"', page)
        self.assertNotIn('人工旧资格', page)
        self.assertNotIn('data-module="prelaunch"', page)
        self.assertIn('id="page-hot"', page)
        self.assertIn('id="page-sector-radar"', page)

    def test_builder_does_not_invoke_paused_renderers_or_require_valid_old_metrics(self):
        self.configure()
        from build_investment_site import build
        import research_views
        original = research_views.render_module
        def render(module):
            if module in ('prelaunch', 'yichujifa'):
                raise AssertionError('Paused renderer was invoked')
            return original(module)
        atomic_json(research_path('three_step/current.json'), {'module_id': 'three-step',
            'as_of': NOW.isoformat(), 'valid_until': '2099-09-11T15:00:00+08:00', 'status': 'complete',
            'rows': [{'code': '600001', 'name': '人工旧记录', 'research_passed': True, 'metrics': 'broken'}]})
        with patch.object(research_views, 'render_module', side_effect=render), patch('three_step_views.render_page', side_effect=AssertionError('Paused renderer was invoked')), patch('late_day_research.render_page', side_effect=AssertionError('Paused renderer was invoked')):
            page = (build(dt.date(2026, 9, 11)) / 'latest.html').read_text()
        self.assertIn('人工旧记录', page)
        self.assertIn('data-strategy-paused="three-step"', page)


if __name__ == '__main__':
    unittest.main()
