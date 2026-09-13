"""The refresh coordinator must isolate modules and never widen file access."""
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import refresh_research as refresh
import research_modules as rm


NOW = dt.datetime.now(rm.TZ).replace(microsecond=0) - dt.timedelta(seconds=2)


def artificial(module):
    return {'schema_version': 1, 'module_id': module, 'status': 'partial',
            'as_of': (NOW - dt.timedelta(hours=1)).isoformat(),
            'generated_at': NOW.isoformat(), 'valid_until': (NOW + dt.timedelta(days=1)).isoformat(),
            'summary': '人工研究记录', 'missing': ['人工证据缺口'], 'candidates': [],
            'coverage': {}, 'phase': 'prepare', 'rules': {}, 'sources': []}


class RefreshResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'AUTOSTRATEGY_PRIVATE_ROOT': self.tmp.name})
        self.env.start(); self.addCleanup(self.env.stop)

    def test_one_failed_module_does_not_block_others(self):
        def run(module, *args, **kwargs):
            if module == 'yichujifa': raise ValueError('private token=artificial /Users/artificial/hidden.json')
            return artificial(module)
        with patch.object(refresh, 'run_module', side_effect=run):
            result = refresh.refresh(['yichujifa', 'prelaunch'], as_of=NOW, rebuild=False)
        states = {x['module']: x['status'] for x in result['modules']}
        self.assertEqual(states, {'yichujifa': 'unavailable', 'prelaunch': 'partial'})
        self.assertNotIn('/Users/', json.dumps(result))
        self.assertNotIn('token=', json.dumps(rm.load_module('yichujifa', NOW)))
        self.assertEqual(rm.load_module('prelaunch', NOW)['state'], 'partial')

    def test_hot_failure_blocks_new_dragon_but_not_other_skills(self):
        calls = []
        def run(module, *args, **kwargs):
            calls.append(module)
            if module == 'hot': raise RuntimeError('artificial fault')
            return artificial(module)
        with patch.object(refresh, 'run_module', side_effect=run):
            result = refresh.refresh(['hot', 'dragon', 'yichujifa'], as_of=NOW, rebuild=False)
        self.assertNotIn('dragon', calls)
        self.assertIn('yichujifa', calls)
        states = {x['module']: x['status'] for x in result['modules']}
        self.assertEqual(states['dragon'], 'unavailable')
        self.assertEqual(states['yichujifa'], 'partial')

    def test_dragon_receives_this_run_hot_context(self):
        received = []
        def run(module, *args, **kwargs):
            if module == 'dragon': received.append(kwargs.get('hot'))
            return artificial(module)
        with patch.object(refresh, 'run_module', side_effect=run):
            refresh.refresh(['hot', 'dragon'], as_of=NOW, rebuild=False)
        self.assertEqual(received[0]['module_id'], 'hot')

    def test_repeated_identical_data_does_not_rebuild(self):
        with patch.object(refresh, 'run_module', side_effect=lambda module, *a, **kw: artificial(module)), patch.object(refresh, 'build') as build:
            refresh.refresh(['yichujifa'], as_of=NOW)
            refresh.refresh(['yichujifa'], as_of=NOW)
            self.assertEqual(build.call_count, 1)

    def test_modules_are_deduplicated_and_import_is_single_module(self):
        with patch.object(refresh, 'run_module', side_effect=lambda module, *a, **kw: artificial(module)) as run:
            result = refresh.refresh(['yichujifa', 'yichujifa'], as_of=NOW, rebuild=False)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(len(result['modules']), 1)
        with self.assertRaises(ValueError): refresh.refresh(['hot', 'dragon'], as_of=NOW, input_data=artificial('hot'), rebuild=False)

    def test_future_cutoff_unknown_module_and_unknown_phase_rejected(self):
        for kwargs in ({'as_of': dt.datetime.now(rm.TZ) + dt.timedelta(days=1)}, {'phase': 'fictional'}):
            with self.assertRaises(ValueError): refresh.refresh(['yichujifa'], rebuild=False, **kwargs)
        with self.assertRaises(ValueError): refresh.refresh(['other'], as_of=NOW, rebuild=False)

    def test_normalized_import_does_not_execute_native_network_scan(self):
        value = artificial('yichujifa')
        with patch('yichujifa_research.research') as run:
            self.assertEqual(refresh.run_module('yichujifa', NOW, 'prepare', input_data=value), value)
            run.assert_not_called()

    def test_normalized_future_source_becomes_failed_attempt(self):
        value = artificial('yichujifa'); value['as_of'] = (dt.datetime.now(rm.TZ) + dt.timedelta(days=1)).isoformat()
        result = refresh.refresh(['yichujifa'], as_of=NOW, input_data=value, rebuild=False)
        self.assertEqual(result['modules'][0]['status'], 'unavailable')

    def test_read_input_allows_only_private_research_roots(self):
        file = Path(self.tmp.name) / 'research/input.json'; file.parent.mkdir(); file.write_text('{}')
        self.assertEqual(refresh._read_input(file), {})
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other) / 'arbitrary.json'; outside.write_text('{}')
            with self.assertRaises(ValueError): refresh._read_input(outside)

    def test_private_input_symlink_and_parent_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other) / 'input.json'; outside.write_text('{}')
            link = Path(self.tmp.name) / 'link.json'; link.symlink_to(outside)
            parent = Path(self.tmp.name) / 'linked'; parent.symlink_to(other, target_is_directory=True)
            for path in (link, parent / 'input.json'):
                with self.assertRaises(ValueError): refresh._read_input(path)

    def test_input_requires_object_and_valid_version(self):
        file = Path(self.tmp.name) / 'input.json'; file.write_text('[]')
        with self.assertRaises(ValueError): refresh._read_input(file)
        value = artificial('yichujifa'); value['schema_version'] = 999
        with self.assertRaises(ValueError): refresh.run_module('yichujifa', NOW, 'prepare', input_data=value)

    def test_refresh_records_run_start_for_equal_source_late_results(self):
        with patch.object(refresh, 'run_module', side_effect=lambda module, *a, **kw: artificial(module)):
            refresh.refresh(['yichujifa'], as_of=NOW, rebuild=False)
        stored = rm.load_module('yichujifa', dt.datetime.now(rm.TZ))
        self.assertTrue(stored['data'].get('run_started_at') or stored['attempt'].get('run_started_at'))

    def test_build_failure_does_not_discard_other_module_outcomes(self):
        with patch.object(refresh, 'run_module', side_effect=lambda module, *a, **kw: artificial(module)), patch.object(refresh, 'build', side_effect=OSError('artificial build failure')):
            result = refresh.refresh(['yichujifa', 'prelaunch'], as_of=NOW, rebuild=True)
        self.assertEqual({x['module'] for x in result['modules']}, {'yichujifa', 'prelaunch'})

    def test_wrong_normalized_module_is_rejected_before_native_adapter(self):
        with patch('yichujifa_research.research') as run:
            with self.assertRaises(ValueError):
                refresh.run_module('yichujifa', NOW, 'prepare', input_data=artificial('dragon'))
            run.assert_not_called()


if __name__ == '__main__': unittest.main()
