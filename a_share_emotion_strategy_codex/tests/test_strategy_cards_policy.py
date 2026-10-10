"""Generic rule cards honor the private pause policy; no actual market input."""
import hashlib
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import investment_dashboard
from research_store import atomic_json, private_path


class RuleCardPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, AUTOSTRATEGY_PRIVATE_ROOT=self.tmp.name)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_paused_rules_do_not_read_live_qualification(self):
        ids = ('prelaunch', 'yichujifa', 'late-day', 'three-step', 'event-timing')
        atomic_json(private_path('config/research_policy.json'), {
            'schema_version': 1,
            'strategies': {sid: {'state': 'paused', 'reason': '人工暂停示例'} for sid in ids}})
        stub = {'as_of': None, 'generated_at': None, 'state': '尚未执行', 'missing': [], 'link': '#page-dragon'}
        with patch.object(investment_dashboard, 'research_status', return_value=stub) as status:
            page = investment_dashboard.render_playbooks()
        self.assertEqual([call.args[0] for call in status.call_args_list], ['dragon'])
        for sid in (*ids, 'dragon', 'macd', 'catch-up-dragon'):
            self.assertIn('id="strategy-' + sid + '"', page)
        self.assertEqual(page.count('人工暂停示例'), 5)
        self.assertLess(page.index('id="strategy-catch-up-dragon"'), page.index('id="strategy-prelaunch"'))
        self.assertIn('不参与当前选股', page)

    def test_skill_fingerprint_covers_executable_and_rules(self):
        folder = investment_dashboard.ROOT / 'docs/playbooks'
        registry = json.loads((folder / 'registry.json').read_text())
        card = next(x for x in registry['playbooks'] if x['id'] == 'catch-up-dragon')
        digest = hashlib.sha256()
        for name in card['source_hash_scope']:
            digest.update(name.encode())
            digest.update((folder / 'a-share-catch-up-dragon' / name).read_bytes())
        self.assertEqual(card['source_hash'], 'sha256:' + digest.hexdigest())

    def test_bad_policy_preserves_rules_but_not_current_selection(self):
        private_path('config').mkdir(parents=True)
        private_path('config/research_policy.json').write_text('broken')
        with patch.object(investment_dashboard, 'research_status', side_effect=AssertionError('should not execute')):
            page = investment_dashboard.render_playbooks()
        self.assertIn('研究选择配置不可用', page)
        self.assertIn('strategy-catch-up-dragon', page)

    def test_price_and_later_information_cutoff_are_distinct(self):
        from research_topics import validate, render_topics, publish_topic
        from research_modules import TZ
        now = dt.datetime(2026, 9, 12, 18, tzinfo=TZ)
        payload = {'schema_version': 1, 'topic_id': 'fictional-catchup',
                   'title': '人工专题', 'summary': '虚构，非实际行情',
                   'as_of': '2026-09-11T15:00:00+08:00',
                   'information_as_of': '2026-09-12T17:00:00+08:00',
                   'generated_at': now.isoformat(), 'valid_until': now.isoformat(),
                   'status': 'partial', 'changes': [], 'missing': [], 'directions': []}
        validate(payload, now)
        publish_topic(payload, now=now)
        self.assertIn('盘后资料不倒填行情判断', render_topics(now=now))
        payload['information_as_of'] = '2026-09-13T17:00:00+08:00'
        with self.assertRaises(ValueError):
            validate(payload, now)


if __name__ == '__main__':
    unittest.main()
