"""Topic expiry, isolation and publication; artificial data, no network."""
import copy
import datetime as dt
import os
import re
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

    def comparison(self):
        from research_topics import STOCK_TEXT
        data = copy.deepcopy(self.data)
        data.update(comparison_scope='人工局部比较池', comparison_method='人工观察顺序，不授予资格')
        stock = {key: '人工说明' for key in STOCK_TEXT}
        stock.update(code='600001', name='人工样本', rank=1, close=10.0,
                     price_as_of=data['as_of'], sources=[
                         {'label': '人工来源', 'url': 'https://example.org/history',
                          'published_at': '2026-01-08', 'observation_period': '人工周期',
                          'retrieved_at': self.now.isoformat()}])
        data['stocks'] = [stock]
        return data

    def test_stock_comparison_preserves_scope_dates_and_counter_evidence(self):
        data = self.comparison()
        data['stocks'][0]['counter'] = '<img src=x onerror=alert(1)>'
        publish_topic(data, now=self.now)
        page = render_topics(now=self.now + dt.timedelta(days=1))
        for text in ('人工局部比较池', '人工观察顺序', '最强反证', '下一步确认', '失效条件', '观察窗口已结束'):
            self.assertIn(text, page)
        self.assertNotIn('<img src=x', page)
        self.assertIn('id="topic-sample-600001"', page)

    def test_duplicate_stock_and_rank_cannot_inflate_comparison(self):
        data = self.comparison()
        data['stocks'].append(copy.deepcopy(data['stocks'][0]))
        with self.assertRaises(ValueError): publish_topic(data, now=self.now)
        data['stocks'][1]['code'] = '600002'
        with self.assertRaises(ValueError): publish_topic(data, now=self.now)
        data['stocks'][1]['rank'] = 4
        with self.assertRaises(ValueError): publish_topic(data, now=self.now)

    def test_prices_cannot_cross_cutoff_or_grant_eligibility(self):
        for update in ({'price_as_of': '2026-01-09T15:00:00+08:00'}, {'close': float('nan')},
                       {'eligible': True}, {'sources': []}):
            with self.subTest(update=update):
                data = self.comparison(); data['stocks'][0].update(update)
                with self.assertRaises(ValueError): publish_topic(data, now=self.now)

    def test_related_link_keeps_expiry_and_strategy_independence(self):
        from research_topics import render_topic_links
        data = self.comparison(); data['related_module'] = 'prelaunch'
        publish_topic(data, now=self.now)
        page = render_topic_links('prelaunch', now=self.now + dt.timedelta(days=1))
        self.assertIn('href="#topic-sample"', page)
        self.assertIn('历史研究', page)
        self.assertIn('核心资格分别核验', page)
        self.assertEqual(render_topic_links('dragon', now=self.now), '')

    def tiered_comparison(self):
        data = self.comparison()
        for number in range(2, 6):
            stock = copy.deepcopy(data['stocks'][0])
            stock.update(code=f'60000{number}', name=f'人工样本{number}',
                         rank=number if number <= 3 else None)
            data['stocks'].append(stock)
        data['context_notes'] = ['人工集中度说明，不修改配置']
        data['related_modules'] = ['hot', 'dragon', 'yichujifa', 'prelaunch']

        def item(number, rank, position='unknown'):
            return {'code': f'60000{number}', 'rank': rank, 'action': '人工处理方向',
                    'reason': '人工比较理由', 'conditions': '人工待验证条件',
                    'position_status': position}

        data['decision_tiers'] = [
            {'id': 'exit', 'label': '退出与排除', 'summary': '人工退出说明',
             'items': [item(3, 2, 'held'), item(4, 1, 'not_held')]},
            {'id': 'observe', 'label': '观察', 'summary': '人工观察说明', 'items': [item(2, 1)]},
            {'id': 'conditional', 'label': '明日条件操作', 'summary': '人工条件说明',
             'items': [item(1, 5), item(5, 1)]},
        ]
        return data

    def test_tiers_sort_independently_without_changing_original_ranks(self):
        data = self.tiered_comparison()
        publish_topic(data, now=self.now)
        page = render_topics(now=self.now)
        self.assertLess(page.index('data-tier-id="conditional"'), page.index('data-tier-id="observe"'))
        self.assertLess(page.index('data-tier-id="observe"'), page.index('data-tier-id="exit"'))
        conditional = page.split('data-tier-id="conditional"', 1)[1].split('data-tier-id="observe"', 1)[0]
        self.assertLess(conditional.index('600005'), conditional.index('600001'))
        self.assertIn('原第1位', conditional)
        self.assertIn('<td>5</td>', conditional)
        self.assertEqual(data['stocks'][0]['rank'], 1)
        for text in ('持仓退出管理', '移出候选池（未持仓）', '明日盘中确认尚未发生',
                     '条件未满足时不能称为可买', '不授予交易资格', '不覆盖热点原始前三名次'):
            self.assertIn(text, page)

    def test_decision_stocks_cannot_be_unresolved_or_repeat_across_tiers(self):
        for code in ('999999', '600002'):
            with self.subTest(code=code):
                data = self.tiered_comparison()
                data['decision_tiers'][0]['items'][0]['code'] = code
                with self.assertRaises(ValueError): publish_topic(data, now=self.now)
        data = self.tiered_comparison()
        data['decision_tiers'][2]['items'][1]['code'] = '600001'
        with self.assertRaises(ValueError): publish_topic(data, now=self.now)

    def test_decision_priorities_are_positive_unique_integers(self):
        for rank in (True, 0, -1, '1', 1.5, 5):
            with self.subTest(rank=rank):
                data = self.tiered_comparison()
                data['decision_tiers'][2]['items'][1]['rank'] = rank
                with self.assertRaises(ValueError): publish_topic(data, now=self.now)

    def test_three_distinct_tiers_allow_empty_without_replacement(self):
        data = self.tiered_comparison()
        for tier in data['decision_tiers']:
            tier['items'] = []
        publish_topic(data, now=self.now)
        self.assertEqual(render_topics(now=self.now).count('本梯队暂空，不补位。'), 3)
        for tiers in (data['decision_tiers'][:2], data['decision_tiers'] + [data['decision_tiers'][0]],
                      [data['decision_tiers'][0]] * 3):
            invalid = copy.deepcopy(data); invalid['decision_tiers'] = tiers
            with self.assertRaises(ValueError): publish_topic(invalid, now=self.now)

    def test_exit_must_distinguish_holdings_from_candidate_removal(self):
        for position in ('unknown', 'assumed', None):
            data = self.tiered_comparison()
            data['decision_tiers'][0]['items'][0]['position_status'] = position
            with self.assertRaises(ValueError): publish_topic(data, now=self.now)
        data = self.tiered_comparison()
        del data['decision_tiers'][0]['items'][0]['position_status']
        with self.assertRaises(ValueError): publish_topic(data, now=self.now)

    def test_decision_notes_are_escaped_and_cannot_grant_eligibility(self):
        data = self.tiered_comparison()
        attack = '<img src=x onerror=alert(1)>'
        data['context_notes'] = [attack]
        tier = data['decision_tiers'][2]
        tier.update(label=attack, summary=attack)
        tier['items'][0].update(action=attack, reason=attack, conditions=attack)
        publish_topic(data, now=self.now)
        page = render_topics(now=self.now)
        self.assertNotIn(attack, page)
        self.assertIn('&lt;img src=x onerror=alert(1)&gt;', page)
        tier['items'][0]['eligible'] = True
        with self.assertRaises(ValueError): publish_topic(data, now=self.now)
        for context in ('plain text', [1], [None]):
            invalid = self.tiered_comparison(); invalid['context_notes'] = context
            with self.assertRaises(ValueError): publish_topic(invalid, now=self.now)

    def test_all_four_research_entries_link_topic_without_adding_pages(self):
        from investment_dashboard import render_dashboard
        from research_topics import render_topic_links
        data = self.tiered_comparison()
        data['related_module'] = 'prelaunch'
        publish_topic(data, now=self.now)
        page = render_dashboard(self.now.date())
        self.assertEqual(re.findall(r'data-page="([a-z]+)"', page),
                         ['hot', 'dragon', 'yichujifa', 'prelaunch', 'strategy', 'reports'])
        self.assertNotIn('@@', page)
        for module in data['related_modules']:
            surface = page.split(f'<section id="page-{module}"', 1)[1].split('<section id="page-', 1)[0]
            self.assertIn('href="#topic-sample"', surface)
            self.assertEqual(render_topic_links(module, now=self.now).count('href="#topic-sample"'), 1)
        later = render_topics(now=self.now + dt.timedelta(days=1))
        self.assertIn('观察窗口已结束', later)
        self.assertIn(data['valid_until'], later)

    def test_invalid_related_modules_do_not_silently_expand_scope(self):
        for related in ('hot', ['hot', 'hot'], ['strategy'], [None]):
            data = self.comparison(); data['related_modules'] = related
            with self.assertRaises(ValueError): publish_topic(data, now=self.now)


if __name__ == '__main__': unittest.main()
