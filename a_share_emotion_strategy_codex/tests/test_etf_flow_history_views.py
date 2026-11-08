"""Offline historical flow DOM contracts using invented sectors and quantities."""
import copy
import datetime as dt
from html.parser import HTMLParser
import os
import tempfile
import unittest
from unittest.mock import patch

import etf_flow_history_views as views
from etf_pressure_views import TZ
from research_store import atomic_json, research_path


NOW = dt.datetime(2026, 10, 10, 9, tzinfo=TZ)


def fixture():
    days, date = [], dt.date(2026, 10, 9)
    while len(days) < 20:
        if date.weekday() < 5:
            days.append(date.isoformat())
        date -= dt.timedelta(days=1)
    dates = list(reversed(days))
    def ranking(direction, total):
        return [{'code': 'BK9001' if direction == 'inflows' else 'BK9002',
                 'name': '人工行业流入甲' if direction == 'inflows' else '人工行业流出乙',
                 'rank': 1, 'main_net_yuan': total,
                 'large_net_yuan': total * .25, 'superlarge_net_yuan': total * .75}]
    records = [{'date': day, 'status': 'complete', 'expected_sectors': 4, 'verified_sectors': 4,
                'inflows': ranking('inflows', 124000000), 'outflows': ranking('outflows', -97000000)}
               for day in dates]
    return {'schema_version': 1, 'provider': 'eastmoney', 'metric': '东财模型主力净额',
            'unit': 'CNY_yuan', 'generated_at': NOW.isoformat(),
            'retrieved_at': '2026-10-10T08:55:00+08:00', 'cutoff': dates[-1],
            'window_start': dates[0], 'window_end': dates[-1], 'trading_dates20': dates,
            'catalog': {'count': 4, 'complete': True, 'classification': '东方财富行业',
                        'source': 'https://example.com/catalog', 'as_of': '2026-10-10',
                        'scope_note': '人工构造冻结目录，不是真实历史市场池'},
            'coverage': {'expected_sectors': 4, 'verified_sectors': 4, 'complete_days': 20, 'expected_days': 20},
            'days': records,
            'summary': {'complete_window': True, 'latest_date': dates[-1], 'latest_status': 'complete',
                        'cumulative': {'status': 'complete', 'expected_sectors': 4, 'verified_sectors': 4,
                                       'inflows': ranking('inflows', 321000000),
                                       'outflows': ranking('outflows', -278000000), 'zero_count': 2}},
            'gaps': [], 'sources': [{'label': '人工历史接口', 'url': 'https://example.com/history',
                                    'as_of': dates[-1], 'retrieved_at': '2026-10-10T08:55:00+08:00',
                                    'basis': '人工构造，统一人民币元'}]}


class Tags(HTMLParser):
    def __init__(self, value):
        super().__init__(); self.tags = []; self.feed(value)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class EtfFlowHistoryViewTests(unittest.TestCase):
    def page(self, report=None, now=NOW):
        return views.render(fixture() if report is None else report, now=now)

    def test_twenty_date_panels_latest_selected_and_stable_dom(self):
        page = self.page(); tags = Tags(page).tags
        root = [attrs for tag, attrs in tags if attrs.get('id') == 'etf-em-history'][0]
        self.assertIn('data-flow-history', root)
        self.assertEqual(root['data-flow-selected-date'], '2026-10-09')
        select = [attrs for tag, attrs in tags if tag == 'select' and 'data-flow-date' in attrs]
        self.assertEqual(len(select), 1)
        self.assertEqual(select[0]['aria-controls'], 'etf-flow-days')
        panels = [attrs for _, attrs in tags if 'data-flow-day' in attrs]
        self.assertEqual(len(panels), 20)
        self.assertEqual(panels[0]['data-flow-day'], '2026-10-09')
        self.assertNotIn('hidden', panels[0])
        self.assertEqual(sum('hidden' in panel for panel in panels), 19)
        self.assertIn('id="etf-flow-2026-10-09-inflows-BK9001"', page)
        self.assertIn('id="etf-flow-history-sources"', page)
        self.assertIn('id="etf-flow-cumulative-inflows-BK9001"', page)
        self.assertIn('<strong>20 / 20</strong>', page)
        self.assertNotIn(' open', page)

    def test_distinct_provider_model_scope_and_backfill_disclosures(self):
        page = self.page()
        for text in ('东财模型主力', '大单 / 超大单', '不单独构成买卖信号',
                     '历史回补与重新计算', '不倒填当时实时发布',
                     '当前行业目录被冻结后回溯', '分类会变化，成分可能重叠', '金额统一按人民币元保存'):
            self.assertIn(text, page)
        self.assertIn('+1.24 亿', page)
        self.assertIn('-9,700.00 万', page)
        self.assertIn('大单净额', page)
        self.assertIn('超大单净额', page)
        self.assertIn('+3.21 亿', page)  # supplied cumulative result, not a renderer sum
        self.assertNotIn('24.80 亿', page)

    def test_removed_provider_note_does_not_restore_deleted_module(self):
        report = fixture()
        report['gaps'] = ['本榜与同花顺普通资金净额是不同来源、分类与模型，独立显示，不混排或补缺。',
                          '部分历史成分尚未核验。']
        page = self.page(report)
        self.assertNotIn('同花顺', page)
        self.assertIn('部分历史成分尚未核验', page)

    def test_partial_or_incomplete_catalog_never_exposes_old_formal_rankings(self):
        report = fixture()
        report['days'][-1].update(status='partial', verified_sectors=3)
        report['days'][-1]['inflows'][0]['name'] = '不应展示的局部旧榜'
        page = self.page(report)
        self.assertIn('<strong>19 / 20</strong>', page)
        self.assertIn('该日覆盖或榜单字段未完整核验', page)
        self.assertNotIn('不应展示的局部旧榜', page)
        self.assertNotIn('id="etf-flow-cumulative"', page)
        self.assertNotIn('+3.21 亿', page)
        report = fixture(); report['catalog']['complete'] = False
        page = self.page(report)
        self.assertIn('<strong>0 / 20</strong>', page)
        self.assertNotIn('人工行业流入甲', page)
        self.assertIn('当前冻结行业目录不完整', page)

    def test_missing_day_is_an_unavailable_panel_not_zero_or_adjacent_data(self):
        report = fixture(); report['days'].pop()
        page = self.page(report)
        self.assertIn('<strong>19 / 20</strong>', page)
        self.assertIn('该声明交易日没有有效日记录', page)
        self.assertIn('不以0、相邻日期或旧榜填补', page)
        self.assertEqual(len([a for _, a in Tags(page).tags if 'data-flow-day' in a]), 20)

    def test_future_unclosed_or_future_generated_records_stop_formal_tables(self):
        report = fixture(); report['generated_at'] = '2026-10-11T09:00:00+08:00'
        page = self.page(report)
        self.assertIn('正式榜暂停展示', page)
        self.assertNotIn('人工行业流入甲', page)
        report = fixture()
        now = dt.datetime(2026, 10, 9, 14, tzinfo=TZ)
        report['generated_at'] = '2026-10-09T13:50:00+08:00'
        report['retrieved_at'] = '2026-10-09T13:45:00+08:00'
        report['catalog']['as_of'] = '2026-10-09'
        page = self.page(report, now)
        self.assertIn('该日期尚未完成收盘', page)
        self.assertNotIn('人工行业流入甲', page)

    def test_old_selected_days_remain_valid_history_and_only_latest_cutoff_warns(self):
        page = self.page(now=dt.datetime(2026, 10, 14, 9, tzinfo=TZ))
        self.assertIn('最新截止日距今已超过4个自然日', page)
        self.assertIn('主动回看的历史日期仍可作为历史研究', page)
        self.assertIn('<strong>20 / 20</strong>', page)
        self.assertIn('人工行业流入甲', page)
        self.assertIn('id="etf-flow-cumulative"', page)

    def test_duplicate_dates_or_bad_rankings_do_not_create_formal_tables(self):
        report = fixture(); report['days'].append(copy.deepcopy(report['days'][-1]))
        page = self.page(report)
        self.assertIn('<strong>19 / 20</strong>', page)
        self.assertIn('重复版本', page)
        report = fixture(); report['days'][-1]['inflows'][0]['rank'] = 2
        self.assertIn('<strong>19 / 20</strong>', self.page(report))
        report = fixture(); report['days'][-1]['outflows'][0]['main_net_yuan'] = 1
        self.assertIn('<strong>19 / 20</strong>', self.page(report))
        report = fixture(); report['days'][-1]['inflows'][0]['main_net_yuan'] = float('nan')
        self.assertIn('<strong>19 / 20</strong>', self.page(report))

    def test_missing_split_or_percentage_is_not_invented(self):
        report = fixture()
        for day in report['days']:
            day['inflows'][0].update(large_net_yuan=None, superlarge_net_yuan=float('inf'))
        page = self.page(report)
        self.assertIn('<dt>大单净额</dt><dd>—</dd>', page)
        self.assertIn('<dt>超大单净额</dt><dd>—</dd>', page)
        self.assertNotIn('Infinity', page)
        self.assertNotIn('nan', page)
        self.assertNotIn('50.00%', page)
        self.assertIn('未提供的占比、大单明细不推算', page)

    def test_invalid_or_wrong_provider_schema_and_default_private_path(self):
        for report in ({}, [], {'schema_version': True}, dict(fixture(), provider='ths'),
                       dict(fixture(), unit='CNY_10000')):
            self.assertIn('历史资金数据不可用', self.page(report))
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'AUTOSTRATEGY_PRIVATE_ROOT': root}):
            self.assertIn('等待可核验快照', views.render(now=NOW))
            atomic_json(research_path('etf_flow_history/current.json'), fixture())
            self.assertIn('人工行业流入甲', views.render(now=NOW))

    def test_html_private_unknown_fields_and_unsafe_source_links_are_not_exposed(self):
        report = fixture(); report['account'] = 'PRIVATE-ACCOUNT'
        for day in report['days']:
            day['inflows'][0]['name'] = '<img src=x onerror=alert(1)>'
        report['catalog']['scope_note'] = '账户总资产123万元'
        report['catalog']['source'] = 'http://example.com/insecure'
        report['gaps'] = ['/Users/artificial/private/raw.json', 'password=PRIVATE-PASSWORD']
        report['sources'] = [{'label': 'bad', 'url': 'https://example.com/?token=PRIVATE-TOKEN',
                              'basis': 'file:///tmp/private'},
                             {'label': 'bad', 'url': 'https://127.0.0.1/local'}]
        page = self.page(report)
        for text in ('PRIVATE-ACCOUNT', 'PRIVATE-PASSWORD', 'PRIVATE-TOKEN', '123万元',
                     '/Users/artificial', 'file://', 'http://example.com', 'https://127.0.0.1', '<img'):
            self.assertNotIn(text, page)
        self.assertIn('&lt;img', page)
        tags = Tags(page).tags
        self.assertFalse(any(key.startswith('on') for _, attrs in tags for key in attrs))
        self.assertFalse(any(tag == 'script' for tag, _ in tags))
        self.assertTrue(all(cls.startswith('etfm') for _, attrs in tags for cls in attrs.get('class', '').split()))


if __name__ == '__main__':
    unittest.main()
