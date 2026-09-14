"""Topic expiry, isolation and publication; artificial data, no network."""
import copy
import datetime as dt
import os
import tempfile
import unittest
from unittest.mock import patch

from research_modules import TZ
from research_store import research_path
from research_topics import publish_topic, render_topics
from build_investment_site import build


class TopicTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {'AUTOSTRATEGY_PRIVATE_ROOT': tmp.name})
        env.start(); self.addCleanup(env.stop)
        self.now = dt.datetime(2026, 1, 8, 16, tzinfo=TZ)
        self.data = {'schema_version': 1, 'topic_id': 'sample', 'status': 'partial',
                     'as_of': '2026-01-08T15:00:00+08:00', 'generated_at': self.now.isoformat(),
                     'valid_until': '2026-01-09T09:25:00+08:00', 'title': '人工专题',
                     'summary': '<script>alert(1)</script>', 'changes': ['人工变化'],
                     'directions': [], 'missing': ['人工缺口'], 'sources': []}

    def test_expiry_uses_original_window_and_escapes_text(self):
        publish_topic(self.data, now=self.now)
        page = render_topics(now=self.now + dt.timedelta(days=1))
        self.assertIn('观察窗口已结束', page)
        self.assertNotIn('<script>alert', page)
        self.assertIn('静态专题不授予交易资格', page)

    def test_clock_only_repeat_does_not_replace_original(self):
        publish_topic(self.data, now=self.now)
        repeat = dict(self.data, generated_at=(self.now + dt.timedelta(minutes=1)).isoformat())
        self.assertFalse(publish_topic(repeat, now=self.now + dt.timedelta(minutes=1))['changed'])
        self.assertIn(self.now.isoformat(), research_path('topics/sample/current.json').read_text())

    def test_future_invalid_and_failed_topic_does_not_block_build(self):
        future = dict(self.data, as_of='2027-01-01T15:00:00+08:00')
        with self.assertRaises(ValueError): publish_topic(future, now=self.now)
        p = research_path('topics/broken/current.json'); p.parent.mkdir(parents=True); p.write_text('{bad')
        output = build(self.now.date())
        page = (output / 'latest.html').read_text()
        self.assertIn('独立专题资料不可用', page)
        self.assertIn('id="page-hot"', page)

    def test_changed_evidence_archives_and_late_publish_is_rejected(self):
        publish_topic(self.data, now=self.now)
        newer = dict(self.data, summary='新增人工证据', generated_at=(self.now + dt.timedelta(minutes=1)).isoformat())
        self.assertTrue(publish_topic(newer, now=self.now + dt.timedelta(minutes=1))['changed'])
        self.assertEqual(len(list(research_path('topics/sample/history').glob('*.json'))), 1)
        self.assertFalse(publish_topic(self.data, now=self.now + dt.timedelta(minutes=2))['changed'])

    def test_unsafe_source_link_is_not_rendered(self):
        data = copy.deepcopy(self.data)
        data['sources'] = [{'label': 'unsafe', 'url': 'javascript:alert(1)', 'published_at': '2026-01-08',
                            'observation_period': '人工观察期', 'retrieved_at': self.now.isoformat()}]
        publish_topic(data, now=self.now)
        self.assertNotIn('href="javascript:', render_topics(now=self.now))

    def test_missing_original_acquisition_time_is_not_fabricated(self):
        data = copy.deepcopy(self.data)
        data['sources'] = [{'label': 'sample', 'url': 'https://example.org/report',
                            'published_at': '2026-01-08', 'observation_period': '人工观察期', 'retrieved_at': None}]
        publish_topic(data, now=self.now)
        self.assertIn('原取得时间未记录', render_topics(now=self.now))

    def test_malformed_url_is_isolated_from_other_topics(self):
        from research_store import atomic_json
        publish_topic(self.data, now=self.now)
        broken = copy.deepcopy(self.data)
        broken['sources'] = [{'label': 'broken', 'url': 'https://[', 'published_at': 'unknown',
                              'observation_period': 'unknown', 'retrieved_at': None}]
        atomic_json(research_path('topics/broken/current.json'), broken)
        page = render_topics(now=self.now)
        self.assertIn('独立专题资料不可用', page)
        self.assertIn('人工专题', page)


if __name__ == '__main__': unittest.main()
