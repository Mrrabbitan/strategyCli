"""Pressure views use invented instruments and isolated private snapshots only."""
import copy
import datetime as dt
from html.parser import HTMLParser
import os
import tempfile
import unittest
from unittest.mock import patch

import etf_pressure_views as views
from research_store import atomic_json, research_path


DAY = '2026-10-09'
NOW = dt.datetime(2026, 10, 10, 9, tzinfo=views.TZ)


def fixture():
    candle = {'code': '599901', 'name': '人工行业ETF甲', 'as_of': DAY,
              'open': 100, 'close': 96, 'high': 101, 'low': 95,
              'change_pct': -3.25, 'body_pct': -4, 'body_range_pct': 66.6667,
              'close_position_pct': 16.6667, 'volume_ratio20': 1.4,
              'is_large_bear': True, 'missing': [],
              'source': 'https://example.com/history', 'price_basis': '人工兼容前复权'}
    return {'schema_version': 1, 'as_of': DAY,
            'generated_at': DAY + 'T15:40:00+08:00',
            'universe_label': '人工固定ETF代理样本，非全市场',
            'candles': [candle],
            'coverage': {'candles': 1}, 'gaps': ['未取得全行业成分广度']}


class Tags(HTMLParser):
    def __init__(self, value):
        super().__init__(); self.tags = []; self.feed(value)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class EtfPressureViewTests(unittest.TestCase):
    def page(self, report=None, now=NOW):
        return views.render(fixture() if report is None else report, now=now)

    def test_only_price_pressure_and_independent_history_reference_are_displayed(self):
        page = self.page()
        self.assertIn('大阴线与量价压力', page)
        self.assertIn('id="etfm-pressure"', page)
        self.assertIn('id="etfp-bear"', page)
        self.assertIn('不是全行业扫描', page)
        self.assertIn('资金与价格四象限怎么读', page)
        self.assertIn('独立东财历史资金', page)
        self.assertIn('href="#etf-em-history"', page)
        self.assertIn('须对齐日期、行业分类与ETF成分范围', page)
        self.assertIn('大阴线不能直接命名为资金撤退', page)
        for text in ('同花顺', '普通净额', '0/10', '资金时点未知', '净流入参考前十',
                     '净流出参考前十', 'id="etfp-flow"', 'id="etfp-inflows"', 'id="etfp-outflows"'):
            self.assertNotIn(text, page)

    def test_every_legacy_flow_payload_is_ignored_without_rendering_or_reading_it(self):
        class UnreadableFlow(dict):
            def get(self, *_args):
                raise AssertionError('Removed flow payload must never be read')
        injected = {'status': 'reference_only', 'provider': '同花顺',
                    'metric': '普通资金净额（未标主力）', 'retrieved_at': NOW.isoformat(),
                    'inflows': [{'code': '881901', 'name': '不应展示的旧资金榜', 'rank': 1,
                                 'net_yuan': 123000000}],
                    'source': 'https://example.com/?token=PRIVATE-FLOW-TOKEN',
                    'note': '/Users/artificial/private/flow.json', 'account': 'PRIVATE-FLOW-ACCOUNT'}
        baseline = self.page()
        for value in (None, [], injected, dict(injected, status='unavailable'), UnreadableFlow(injected)):
            report = fixture(); report['flow'] = value
            page = self.page(report)
            self.assertEqual(page, baseline)
            for text in ('不应展示的旧资金榜', 'PRIVATE-FLOW-TOKEN', 'PRIVATE-FLOW-ACCOUNT',
                         '/Users/artificial', '+1.23 亿', '同花顺'):
                self.assertNotIn(text, page)

    def test_flat_valid_candle_is_known_not_large_bear(self):
        report = fixture(); row = report['candles'][0]
        row.update(open=100, close=100, high=100, low=100, change_pct=0, is_large_bear=False)
        page = self.page(report)
        self.assertIn('已核验1个ETF代理样本未见符合', page)
        self.assertIn('未达大阴线', page)
        self.assertNotIn('待核验 · 形态字段不足或冲突', page)

    def test_retired_flow_gaps_are_filtered_but_price_date_and_basis_gaps_remain(self):
        report = fixture()
        old = ['同花顺资金采集失败', '普通资金净额无法核验', '历史日榜0/20且流入0/10',
               '资金日期未与日K匹配，不把大阴线与资金净流出合并', 'THS timeout']
        price = ['仅检查固定ETF代理范围，非全行业扫描', '复权口径尚待核验',
                 '日线日期与报告错配，待重新取得', '分钟量比缺少同刻对比']
        report['gaps'] = old + price
        report['candles'][0]['missing'] = old + price
        page = self.page(report)
        for gap in old:
            self.assertNotIn(gap, page)
        for gap in price:
            self.assertIn(gap, page)

    def test_quotation_source_price_warnings_survive_removed_flow_module(self):
        report = fixture()
        price_warnings = ['同花顺日线日期缺失', '同花顺报价复权待核验',
                          '同花顺日线接口超时，价格序列覆盖不足', 'THS quote价格时点不可用']
        report['gaps'] = price_warnings + ['THS timeout', '同花顺资金采集失败', '历史资金日榜0/20']
        report['candles'][0]['missing'] = price_warnings
        page = self.page(report)
        for warning in price_warnings:
            self.assertIn(warning, page)
        for retired in ('THS timeout', '同花顺资金采集失败', '历史资金日榜0/20'):
            self.assertNotIn(retired, page)
        self.assertNotIn('id="etfp-flow"', page)

    def test_candle_columns_keep_close_return_and_body_separate(self):
        page = self.page()
        self.assertIn('已核验1个ETF代理样本，其中1只符合形态', page)
        self.assertIn('-3.25%', page)
        self.assertIn('-4.00%', page)
        self.assertIn('66.67%', page)
        self.assertIn('16.67%', page)
        self.assertIn('1.40 倍', page)
        self.assertIn('实体占高低振幅≥60%', page)
        self.assertIn('≤ −2%', page)
        self.assertIn('收盘位于日振幅下25%', page)
        self.assertIn('id="etfp-candle-599901"', page)
        self.assertIn('id="etfp-quadrants"', page)
        self.assertIn('id="etfp-gaps"', page)
        self.assertNotIn(' open', page)

    def test_no_large_bear_uses_three_weakest_close_returns_not_bodies(self):
        report = fixture(); report['candles'] = []
        for number, change, body in ((1, 2, -1.5), (2, -4, 2), (3, -2, 1), (4, -1, -0.5)):
            row = copy.deepcopy(fixture()['candles'][0])
            row.update(code=f'59990{number}', name=f'人工弱势{number}', open=100,
                       close=100+body, high=104, low=97, is_large_bear=False,
                       change_pct=change)
            report['candles'].append(row)
        page = self.page(report)
        self.assertIn('已核验4个ETF代理样本未见符合', page)
        self.assertLess(page.index('<b>人工弱势2</b>'), page.index('<b>人工弱势3</b>'))
        self.assertLess(page.index('<b>人工弱势3</b>'), page.index('<b>人工弱势4</b>'))
        self.assertNotIn('<b>人工弱势1</b>', page)
        self.assertIn('下跌日收阳并不等于大阴，也不自动证明止跌', page)
        self.assertIn('不能称全行业没有大阴线', page)

    def test_nonnegative_weakest_samples_are_not_called_obvious_decline(self):
        report = fixture(); row = report['candles'][0]
        row.update(open=100, close=101, high=102, low=99, change_pct=0.1, is_large_bear=False)
        page = self.page(report)
        self.assertIn('所列涨跌幅均非负，只是样本排序，不代表明显下跌', page)

    def test_incomplete_future_and_expired_candles_never_confirm_current_pattern(self):
        report = fixture()
        before_close = dt.datetime(2026, 10, 9, 14, 59, tzinfo=views.TZ)
        report['generated_at'] = DAY + 'T14:58:00+08:00'
        page = self.page(report, before_close)
        self.assertIn('尚未取得完整收盘 · 不确认当日大阴线', page)
        self.assertIn('历史 / 待核验 · 符合大阴线', page)
        report = fixture(); report['generated_at'] = '2026-10-11T15:40:00+08:00'
        self.assertIn('日期异常或未来时点', self.page(report))
        self.assertIn('历史参考 · 行情已超过4日', self.page(now=dt.datetime(2026, 10, 14, 9, tzinfo=views.TZ)))
        self.assertNotIn('历史参考 · 行情已超过4日', self.page(now=dt.datetime(2026, 10, 13, 9, tzinfo=views.TZ)))

    def test_mismatched_dates_duplicates_invalid_ohlc_and_flags_do_not_confirm(self):
        report = fixture(); row = report['candles'][0]
        report['candles'].append(copy.deepcopy(row))
        report['candles'].append(dict(row, code='599902', as_of='2026-10-08'))
        row['is_large_bear'] = False
        page = self.page(report)
        self.assertIn('形态字段不足或冲突', page)
        self.assertIn('2条记录', page)
        self.assertNotIn('已核验1个ETF代理样本，其中1只符合形态', page)
        report = fixture(); report['candles'][0].update(high=90, volume_ratio20=float('inf'))
        page = self.page(report)
        self.assertNotIn('Infinity', page)
        self.assertNotIn('nan', page)
        self.assertIn('形态字段不足或冲突', page)

    def test_bad_snapshot_empty_lists_and_default_private_location_fail_closed(self):
        for value in ({}, [], {'schema_version': True}, None):
            if value is not None:
                self.assertIn('本轮数据不可用', self.page(value))
        report = fixture(); report.update(candles=[], flow={})
        page = self.page(report)
        self.assertIn('缺少可展示的已核验ETF日线', page)
        self.assertNotIn('本轮资金榜不可用', page)
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'AUTOSTRATEGY_PRIVATE_ROOT': root}):
            self.assertIn('等待可核验', views.render(now=NOW))
            atomic_json(research_path('etf_pressure/current.json'), fixture())
            self.assertIn('人工行业ETF甲', views.render(now=NOW))

    def test_unknown_fields_private_notes_unsafe_links_and_html_are_not_exposed(self):
        report = fixture(); report['payload'] = {'account': 'PRIVATE-PAYLOAD'}
        report['flow'] = {'note': '账户总资产123万元', 'source': 'https://example.com/?token=PRIVATE-TOKEN'}
        row = report['candles'][0]; row['name'] = '<img src=x onerror=alert(1)>'
        row['source'] = 'http://example.com/insecure'
        row['price_basis'] = '/Users/artificial/private/account.json'
        row['missing'] = ['password=PRIVATE-SECRET', '账户总资产123万元']
        report['gaps'] += ['file:///tmp/private', '持仓成本123万元']
        page = self.page(report)
        for text in ('PRIVATE-PAYLOAD', 'PRIVATE-TOKEN', 'PRIVATE-SECRET', '/Users/artificial',
                     'file://', 'http://example.com', '<img', '123万元'):
            self.assertNotIn(text, page)
        self.assertIn('&lt;img', page)
        tags = Tags(page).tags
        self.assertFalse(any(key.startswith('on') for _, attrs in tags for key in attrs))
        self.assertFalse(any(tag == 'script' for tag, _ in tags))
        self.assertTrue(all(cls.startswith('etfm') for _, attrs in tags for cls in attrs.get('class', '').split()))

    def test_source_link_policy_only_accepts_safe_public_https(self):
        for url in ('file:///tmp/x', 'javascript:alert(1)', 'https://127.0.0.1/x',
                    'https://localhost/x', 'https://user:pw@example.com/x',
                    'https://example.com/?api_key=private', 'https://example.com/\nattack'):
            report = fixture(); report['candles'][0]['source'] = url
            self.assertNotIn('href="' + url, self.page(report))
        self.assertIn('rel="noopener noreferrer"', self.page())


if __name__ == '__main__':
    unittest.main()
