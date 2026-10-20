"""Synthetic historical-THS display only; no network or real private data."""
import copy
import datetime as dt
import os
import tempfile
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

from research_modules import TZ
from research_topics import publish_topic, render_topics, validate


class HistoricalFundViewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {'AUTOSTRATEGY_PRIVATE_ROOT': temporary.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.now = dt.datetime(2026, 10, 20, 11, 0, 5, tzinfo=TZ)
        self.data = {
            'schema_version': 1, 'topic_id': 'sector-fund-pulse',
            'title': '人工资金故障示例', 'summary': '本轮取数失败，未发布新榜。',
            'status': 'unavailable', 'as_of_kind': 'capture',
            'as_of': '2026-10-20T11:00:00+08:00',
            'generated_at': self.now.isoformat(), 'valid_until': self.now.isoformat(),
            'directions': [], 'changes': ['人工接口超时'], 'missing': ['本轮没有新资金数据'],
            'sources': [],
            'historical_fund_reference': {
                'provider': 'ths', 'source_policy': 'ths-only',
                'coverage_kind': 'publisher_top10', 'fund_source_time': None,
                'sampled_at': '2026-10-20T10:30:00+08:00',
                'source_url': 'https://data.10jqka.com.cn/funds/hyzjl/',
                'rows': [dict(rank=i+1, code=f'8819{i:02d}', name=f'人工行业{i}',
                              net_cny=(10-i)*1e8, change_pct=None if i == 0 else 1.25)
                         for i in range(10)],
            },
        }

    def reject(self, data):
        with self.assertRaises(ValueError):
            validate(data, self.now)

    def changed(self, **updates):
        data = copy.deepcopy(self.data)
        data['historical_fund_reference'].update(updates)
        return data

    def test_history_is_collapsed_separate_and_not_current_directions(self):
        self.assertEqual(validate(self.data, self.now)['directions'], [])
        publish_topic(self.data, now=self.now)
        markup = render_topics(now=self.now, include=('sector-fund-pulse',))
        soup = BeautifulSoup(markup, 'html.parser')
        history = soup.select_one('details.historical-fund-reference')
        self.assertIsNotNone(history)
        self.assertFalse(history.has_attr('open'))
        self.assertEqual(history.summary.get_text(), '上次成功参考（历史，非本轮）')
        self.assertEqual(len(history.select('tbody tr')), 10)
        self.assertIn('旧采集时间：2026-10-20T10:30:00+08:00', markup)
        self.assertIn('资金日期／时刻未知', markup)
        self.assertIn('不是本轮榜单', markup)
        self.assertIn('本轮资料不可用，未发布新结果', markup)
        self.assertEqual(self.data['directions'], [])
        self.assertNotIn('<th>方向</th>', markup)
        self.assertNotIn('当前排名', markup)

    def test_native_order_units_missing_auxiliary_and_html_escaping(self):
        data = copy.deepcopy(self.data)
        data['historical_fund_reference']['rows'][0]['name'] = '<img src=x onerror=alert(1)>'
        publish_topic(data, now=self.now)
        markup = render_topics(now=self.now)
        soup = BeautifulSoup(markup, 'html.parser')
        history = soup.select_one('details.historical-fund-reference')
        self.assertIsNone(history.find('img'))
        self.assertIn('&lt;img src=x onerror=alert(1)&gt;', markup)
        rows = history.select('tbody tr')
        self.assertEqual([row.find('td').get_text() for row in rows], [str(i) for i in range(1, 11)])
        self.assertIn('+10.00亿元', rows[0].get_text())
        self.assertEqual(rows[0].find_all('td')[-1].get_text(), '缺失')
        self.assertEqual(rows[1].find_all('td')[-1].get_text(), '+1.25%')

    def test_existing_topics_without_history_keep_old_contract(self):
        data = copy.deepcopy(self.data)
        data.pop('historical_fund_reference')
        data.update(topic_id='legacy-fund-note', status='partial')
        validate(data, self.now)
        publish_topic(data, now=self.now)
        markup = render_topics(now=self.now)
        self.assertIn('topic-legacy-fund-note', markup)
        self.assertNotIn('historical-fund-reference', markup)

    def test_only_unavailable_sector_fund_topic_can_attach_history(self):
        for updates in ({'topic_id': 'other-topic'}, {'status': 'partial'}, {'status': 'complete'}):
            with self.subTest(updates=updates):
                data = copy.deepcopy(self.data)
                data.update(updates)
                self.reject(data)

    def test_provider_policy_and_coverage_cannot_restore_old_sources(self):
        for updates in ({'provider': 'sina'}, {'provider': 'eastmoney'},
                        {'source_policy': 'all-fallback'}, {'coverage_kind': 'universe'}):
            with self.subTest(updates=updates):
                self.reject(self.changed(**updates))

    def test_only_known_exact_ths_public_sources_are_allowed(self):
        legacy = ('https://data.10jqka.com.cn/funds/hyzjl/field/tradezdf/order/desc/'
                  'page/1/ajax/1/free/1/')
        validate(self.changed(source_url=legacy), self.now)
        for url in ('https://example.invalid/funds/hyzjl/',
                    'https://data.10jqka.com.cn.evil.invalid/funds/hyzjl/',
                    'https://data.10jqka.com.cn/funds/hyzjl/?current=true',
                    'http://data.10jqka.com.cn/funds/hyzjl/', 'javascript:alert(1)'):
            with self.subTest(url=url):
                self.reject(self.changed(source_url=url))

    def test_unknown_source_time_is_explicit_not_replaced_with_capture(self):
        self.reject(self.changed(fund_source_time='2026-10-20T10:30:00+08:00'))
        data = copy.deepcopy(self.data)
        data['historical_fund_reference'].pop('fund_source_time')
        self.reject(data)

    def test_future_equal_or_timezone_free_capture_is_rejected(self):
        for sampled_at in ('2026-10-20T11:00:00+08:00', '2026-10-20T11:00:05+08:00',
                           '2026-10-20T11:01:00+08:00', '2026-10-20T10:30:00',
                           '2026-10-20', 'not-a-time', None):
            with self.subTest(sampled_at=sampled_at):
                self.reject(self.changed(sampled_at=sampled_at))

    def test_timezone_conversion_still_proves_capture_is_older(self):
        validate(self.changed(sampled_at='2026-10-20T02:30:00Z'), self.now)
        self.reject(self.changed(sampled_at='2026-10-20T03:01:00Z'))

    def test_history_requires_complete_ten_unique_industry_codes(self):
        for mutation in ('short', 'extra', 'duplicate', 'not-industry', 'non-string'):
            with self.subTest(mutation=mutation):
                data = copy.deepcopy(self.data)
                rows = data['historical_fund_reference']['rows']
                if mutation == 'short':
                    rows.pop()
                elif mutation == 'extra':
                    rows.append(dict(rows[-1]))
                elif mutation == 'duplicate':
                    rows[-1]['code'] = rows[0]['code']
                elif mutation == 'not-industry':
                    rows[0]['code'] = '600001'
                else:
                    rows[0]['code'] = 881900
                self.reject(data)

    def test_original_rank_order_cannot_be_repaired_or_faked(self):
        for rank in (0, 2, 1.0, True, None):
            with self.subTest(rank=rank):
                data = copy.deepcopy(self.data)
                data['historical_fund_reference']['rows'][0]['rank'] = rank
                self.reject(data)
        data = copy.deepcopy(self.data)
        rows = data['historical_fund_reference']['rows']
        rows[0], rows[1] = rows[1], rows[0]
        self.reject(data)

    def test_nonfinite_boolean_negative_or_missing_amounts_are_rejected(self):
        for amount in (float('nan'), float('inf'), 10**1000, -1, True, None, '100000000'):
            with self.subTest(amount=amount):
                data = copy.deepcopy(self.data)
                data['historical_fund_reference']['rows'][0]['net_cny'] = amount
                self.reject(data)
        for change in (float('nan'), float('-inf'), True, '1.2'):
            with self.subTest(change=change):
                data = copy.deepcopy(self.data)
                data['historical_fund_reference']['rows'][0]['change_pct'] = change
                self.reject(data)

    def test_history_cannot_coexist_with_current_direction_rows(self):
        data = copy.deepcopy(self.data)
        data['directions'] = [{key: '人工当前方向' for key in
            ('name', 'industry_state', 'market_state', 'evidence', 'counter', 'next')}]
        self.reject(data)

    def test_history_cannot_carry_any_current_eligibility_flags(self):
        for level in ('topic', 'history', 'row'):
            for flag in ('eligible', 'current_eligible', 'ranking_complete', 'reference_complete'):
                with self.subTest(level=level, flag=flag):
                    data = copy.deepcopy(self.data)
                    target = (data if level == 'topic' else data['historical_fund_reference']
                              if level == 'history' else data['historical_fund_reference']['rows'][0])
                    target[flag] = True
                    self.reject(data)

    def test_invalid_history_is_isolated_and_does_not_show_false_table(self):
        from research_store import atomic_json, research_path
        data = self.changed(provider='sina')
        atomic_json(research_path('topics/sector-fund-pulse/current.json'), data)
        markup = render_topics(now=self.now)
        self.assertIn('独立专题资料不可用', markup)
        self.assertNotIn('上次成功参考', markup)
        self.assertNotIn('人工行业0', markup)


if __name__ == '__main__':
    unittest.main()
