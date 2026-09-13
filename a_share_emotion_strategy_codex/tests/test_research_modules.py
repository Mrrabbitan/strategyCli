"""Independent module publication contracts, with artificial local snapshots."""
import copy
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import research_modules as rm


NOW = dt.datetime.now(rm.TZ).replace(microsecond=0)


def snapshot(module='yichujifa', **changes):
    value = {'schema_version': 1, 'module_id': module, 'status': 'complete',
             'as_of': (NOW - dt.timedelta(hours=1)).isoformat(), 'generated_at': NOW.isoformat(),
             'valid_until': (NOW + dt.timedelta(hours=2)).isoformat(), 'phase': 'prepare',
             'summary': '人工研究结果', 'coverage': {'verified': 1}, 'missing': [],
             'sources': [{'label': '人工来源', 'url': 'https://example.com/research'}],
             'rules': {'version': 'test', 'source_hash': 'test-hash'},
             'candidates': [{'code': '600001', 'name': '虚构观察甲', 'group': '一进二', 'eligible': False,
                             'conditions': [], 'bars': [], 'metrics': {}, 'levels': [], 'reasons': []}]}
    value.update(changes)
    return value


class ResearchModulesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'AUTOSTRATEGY_PRIVATE_ROOT': self.tmp.name})
        self.env.start(); self.addCleanup(self.env.stop)

    def test_first_partial_is_research_not_not_run(self):
        value = snapshot(status='partial', missing=['人工缺口'])
        result = rm.publish('yichujifa', value, attempted_at=NOW)
        self.assertTrue(result['changed'])
        loaded = rm.load_module('yichujifa', NOW)
        self.assertEqual(loaded['state'], 'partial')
        self.assertEqual(loaded['data']['missing'], ['人工缺口'])

    def test_new_empty_and_never_run_are_distinct(self):
        self.assertEqual(rm.load_module('yichujifa', NOW)['state'], 'not_run')
        rm.publish('yichujifa', snapshot(status='empty', candidates=[]), attempted_at=NOW)
        self.assertEqual(rm.load_module('yichujifa', NOW)['state'], 'empty')

    def test_generation_clocks_do_not_create_content_changes(self):
        first = snapshot(); rm.publish('yichujifa', first, attempted_at=NOW)
        before = rm.module_path('yichujifa').read_bytes()
        second = copy.deepcopy(first); second['generated_at'] = (NOW + dt.timedelta(seconds=1)).isoformat()
        self.assertEqual(rm.content_hash(first), rm.content_hash(second))
        self.assertFalse(rm.publish('yichujifa', second, attempted_at=NOW + dt.timedelta(seconds=1))['changed'])
        self.assertEqual(rm.module_path('yichujifa').read_bytes(), before)

    def test_nested_evidence_time_remains_a_material_change(self):
        first = snapshot(); second = copy.deepcopy(first)
        first['candidates'][0]['quote_time'] = (NOW - dt.timedelta(seconds=20)).isoformat()
        second['candidates'][0]['quote_time'] = (NOW - dt.timedelta(seconds=10)).isoformat()
        self.assertNotEqual(rm.content_hash(first), rm.content_hash(second))

    def test_failure_preserves_history_but_blocks_current_eligibility(self):
        original = snapshot(); rm.publish('yichujifa', original, attempted_at=NOW)
        raw = rm.module_path('yichujifa').read_bytes()
        rm.record_failure('yichujifa', '人工连接故障', now=NOW + dt.timedelta(seconds=1))
        loaded = rm.load_module('yichujifa', NOW + dt.timedelta(seconds=2))
        self.assertEqual(rm.module_path('yichujifa').read_bytes(), raw)
        self.assertEqual(loaded['data']['summary'], original['summary'])
        self.assertEqual(loaded['state'], 'unavailable')

    def test_initial_failure_is_not_never_run(self):
        rm.record_failure('yichujifa', '人工连接故障', now=NOW)
        self.assertEqual(rm.load_module('yichujifa', NOW)['state'], 'unavailable')

    def test_repeated_fault_is_idempotent_and_recovery_removes_fault(self):
        self.assertTrue(rm.record_failure('yichujifa', '人工故障', now=NOW)['changed'])
        self.assertFalse(rm.record_failure('yichujifa', '人工故障', now=NOW + dt.timedelta(seconds=1))['changed'])
        self.assertTrue(rm.publish('yichujifa', snapshot(), attempted_at=NOW + dt.timedelta(seconds=2))['changed'])
        self.assertEqual(rm.load_module('yichujifa', NOW + dt.timedelta(seconds=3))['state'], 'complete')

    def test_older_source_cannot_overwrite_later_source(self):
        rm.publish('yichujifa', snapshot(summary='新行情'), attempted_at=NOW)
        older = snapshot(summary='旧行情', as_of=(NOW - dt.timedelta(hours=2)).isoformat())
        result = rm.publish('yichujifa', older, attempted_at=NOW + dt.timedelta(seconds=2))
        self.assertEqual(result['status'], 'superseded')
        self.assertEqual(rm.load_module('yichujifa', NOW)['data']['summary'], '新行情')

    def test_slow_older_run_cannot_overwrite_same_close_newer_run(self):
        newer = snapshot(summary='后启动的新研究', run_started_at=(NOW - dt.timedelta(minutes=1)).isoformat())
        rm.publish('yichujifa', newer, attempted_at=NOW)
        old = snapshot(summary='先启动但晚完成的旧研究', run_started_at=(NOW - dt.timedelta(minutes=3)).isoformat())
        result = rm.publish('yichujifa', old, attempted_at=NOW + dt.timedelta(seconds=10))
        self.assertEqual(result['status'], 'superseded')
        self.assertEqual(rm.load_module('yichujifa', NOW)['data']['summary'], '后启动的新研究')

    def test_old_attempt_does_not_override_new_attempt(self):
        rm.publish('yichujifa', snapshot(), attempted_at=NOW)
        result = rm.record_failure('yichujifa', '迟到故障', now=NOW - dt.timedelta(seconds=1))
        self.assertEqual(result['status'], 'superseded')
        self.assertEqual(rm.load_module('yichujifa', NOW)['state'], 'complete')

    def test_future_source_rejected_without_erasing_current(self):
        rm.publish('yichujifa', snapshot(), attempted_at=NOW)
        future = snapshot(as_of=(NOW + dt.timedelta(seconds=1)).isoformat())
        with self.assertRaises(ValueError): rm.publish('yichujifa', future, attempted_at=NOW)
        self.assertEqual(rm.load_module('yichujifa', NOW)['state'], 'complete')

    def test_future_and_expired_stored_sources_fail_closed(self):
        value = snapshot(as_of=(NOW + dt.timedelta(hours=1)).isoformat())
        rm.module_path('yichujifa').parent.mkdir(parents=True)
        rm.module_path('yichujifa').write_text(json.dumps(value))
        self.assertEqual(rm.load_module('yichujifa', NOW)['state'], 'unavailable')
        value.update(as_of=(NOW - dt.timedelta(hours=3)).isoformat(), valid_until=(NOW - dt.timedelta(seconds=1)).isoformat())
        rm.module_path('yichujifa').write_text(json.dumps(value))
        self.assertEqual(rm.load_module('yichujifa', NOW)['state'], 'expired')

    def test_modules_publish_independently_under_concurrency(self):
        errors = []
        def run(module):
            try: rm.publish(module, snapshot(module), attempted_at=NOW)
            except Exception as exc: errors.append(exc)
        threads = [threading.Thread(target=run, args=(module,)) for module in rm.FOLDERS]
        for thread in threads: thread.start()
        for thread in threads: thread.join(3)
        self.assertFalse(errors)
        for module in rm.FOLDERS:
            self.assertEqual(rm.load_module(module, NOW)['data']['module_id'], module)

    def test_invalid_module_nonfinite_and_schema_rejected(self):
        with self.assertRaises(ValueError): rm.publish('dragon', snapshot(), attempted_at=NOW)
        with self.assertRaises(ValueError): rm.publish('yichujifa', snapshot(coverage={'x': float('nan')}), attempted_at=NOW)
        with self.assertRaises(ValueError): rm.publish('yichujifa', snapshot(schema_version=2), attempted_at=NOW)

    def test_each_changed_revision_is_privately_archived(self):
        first = snapshot(); rm.publish('yichujifa', first, attempted_at=NOW)
        second = snapshot(summary='人工变化'); rm.publish('yichujifa', second, attempted_at=NOW + dt.timedelta(seconds=1))
        history = rm.module_path('yichujifa').parent / 'history'
        self.assertTrue((history / (rm.content_hash(first) + '.json')).is_file())
        self.assertTrue((history / (rm.content_hash(second) + '.json')).is_file())

    def test_missing_live_lifetime_cannot_pass_from_imported_boolean(self):
        value = snapshot(); value['candidates'][0]['eligible'] = True
        rm.publish('yichujifa', value, attempted_at=NOW)
        row = rm.load_module('yichujifa', NOW)['data']['candidates'][0]
        self.assertFalse(row['eligible'])
        self.assertTrue(row['historical_eligible'])

    def test_live_claim_needs_intraday_own_time_and_ninety_second_limit(self):
        now = dt.datetime(2026, 9, 14, 9, 58, tzinfo=rm.TZ)
        source = now - dt.timedelta(seconds=30)
        row = {'eligible': True, 'live_as_of': source.isoformat(),
               'live_valid_until': (source + dt.timedelta(seconds=90)).isoformat()}
        data = snapshot(phase='intraday')
        self.assertTrue(rm.eligible_now('yichujifa', row, data, now))
        for field, bad in [('live_as_of', (now + dt.timedelta(seconds=1)).isoformat()),
                           ('live_valid_until', (source + dt.timedelta(seconds=91)).isoformat())]:
            changed = dict(row, **{field: bad})
            self.assertFalse(rm.eligible_now('yichujifa', changed, data, now))
        self.assertFalse(rm.eligible_now('yichujifa', row, dict(data, phase='replay'), now))
        self.assertFalse(rm.eligible_now('yichujifa', row, data, now.replace(hour=12)))

    def test_prelaunch_core_research_is_not_given_an_intraday_expiry_rule(self):
        # Core research follows its frozen five-session window, not a 90-second
        # quote lifetime and not a newer module-wide publication window.
        frozen_until = NOW + dt.timedelta(days=2)
        data = snapshot('prelaunch', valid_until=(NOW + dt.timedelta(days=5)).isoformat())
        row = {'eligible': True, 'valid_until': frozen_until.isoformat()}
        self.assertTrue(rm.eligible_now('prelaunch', row, data, NOW))
        self.assertTrue(rm.eligible_now('prelaunch', row, data, NOW + dt.timedelta(minutes=10)))
        self.assertFalse(rm.eligible_now('prelaunch', {'eligible': True}, data, NOW))
        self.assertFalse(rm.eligible_now('prelaunch', row, data, frozen_until + dt.timedelta(seconds=1)))

    def test_hot_virtual_auction_before_925_cannot_pass(self):
        now = dt.datetime(2026, 9, 14, 9, 30, tzinfo=rm.TZ)
        row = {'eligible': True, 'auction': {'verified': True, 'qualified': True,
               'source_asof': '2026-09-14T09:20:00+08:00'}}
        self.assertFalse(rm.eligible_now('hot', row, snapshot('hot'), now))


if __name__ == '__main__': unittest.main()
