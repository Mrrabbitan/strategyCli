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
            'flow': {'status': 'reference_only', 'provider': '同花顺',
                     'metric': '普通资金净额（未标主力）', 'as_of': None,
                     'retrieved_at': NOW.isoformat(),
                     'inflows': [{'code': '881901', 'name': '人工行业甲', 'net_yuan': 123000000,
                                  'rank': 1, 'change_pct': 1.25}],
                     'outflows': [{'code': '881902', 'name': '人工行业乙', 'net_yuan': -87000000,
                                   'rank': 1, 'change_pct': -2.5}],
                     'source': 'https://data.10jqka.com.cn/funds/hyzjl/',
                     'note': '资金自身时点未提供，仅参考',
                     'coverage_kind': 'publisher_top10_each', 'universe_complete': False,
                     'is_main_metric': False},
            'coverage': {'candles': 1}, 'gaps': ['未取得全行业成分广度']}


class Tags(HTMLParser):
    def __init__(self, value):
        super().__init__(); self.tags = []; self.feed(value)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class EtfPressureViewTests(unittest.TestCase):
    def page(self, report=None, now=NOW):
        return views.render(fixture() if report is None else report, now=now)

    def test_two_reference_lists_precede_candles_with_separate_clocks_and_metric(self):
        page = self.page()
        self.assertLess(page.index('净流入参考前十'), page.index('大阴线 · ETF代理复核'))
        self.assertIn('净流出参考前十', page)
        self.assertIn('主力口径未核验 · 当日资金时间尚缺', page)
        self.assertIn('普通资金净额（未标主力）', page)
        self.assertIn('资金自身日期 / 时刻：未知', page)
        self.assertIn('采集时间：2026-10-10T09:00:00+08:00', page)
        self.assertIn('原站各前十，非全行业', page)
        self.assertIn('+1.23 亿', page)
        self.assertIn('-8,700.00 万', page)
        self.assertIn('不是全行业扫描', page)
        self.assertIn('资金与价格四象限怎么读', page)
        self.assertIn('四象限暂不合并计算', page)
        self.assertIn('大阴线不能直接命名为资金撤退', page)

    def test_failed_or_bad_metric_flow_uses_no_old_rows_or_other_source(self):
        for mutate in (lambda flow: flow.update(status='unavailable'),
                       lambda flow: flow.update(is_main_metric=True),
                       lambda flow: flow.update(universe_complete=True),
                       lambda flow: flow.update(provider='其他来源'),
                       lambda flow: flow.update(metric='主力净额'),
                       lambda flow: flow.update(as_of=DAY),
                       lambda flow: flow.update(source='https://example.com/funds'),
                       lambda flow: flow.update(retrieved_at='2026-10-11T09:00:00+08:00')):
            report = fixture(); mutate(report['flow']); page = self.page(report)
            self.assertIn('本轮资金榜不可用', page)
            self.assertNotIn('<b>人工行业甲</b>', page)
            self.assertNotIn('<b>人工行业乙</b>', page)
            self.assertNotIn('+1.23 亿', page)
            self.assertEqual(page.count('本轮该榜数据不可用'), 2)
            self.assertIn('旧榜不冒充本轮成功', page)

    def test_flat_valid_candle_is_known_not_large_bear(self):
        report = fixture(); row = report['candles'][0]
        row.update(open=100, close=100, high=100, low=100, change_pct=0, is_large_bear=False)
        page = self.page(report)
        self.assertIn('已核验1个ETF代理样本未见符合', page)
        self.assertIn('未达大阴线', page)
        self.assertNotIn('待核验 · 形态字段不足或冲突', page)

    def test_publisher_order_and_first_ten_slots_are_not_sorted_or_replenished(self):
        report = fixture()
        rows = [{'code': f'8819{i:02}', 'name': f'人工位置{i}', 'rank': i,
                 'net_yuan': i * 10000, 'change_pct': 0} for i in range(1, 12)]
        rows[0], rows[1] = rows[1], rows[0]
        rows[2]['net_yuan'] = float('nan')
        report['flow']['inflows'] = rows
        page = self.page(report)
        self.assertLess(page.index('<b>人工位置2</b>'), page.index('<b>人工位置1</b>'))
        self.assertNotIn('<b>人工位置3</b>', page)
        self.assertNotIn('<b>人工位置11</b>', page)
        self.assertIn('不取第十一名补位', page)

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
        self.assertIn('本轮资金榜不可用', page)
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'AUTOSTRATEGY_PRIVATE_ROOT': root}):
            self.assertIn('等待可核验', views.render(now=NOW))
            atomic_json(research_path('etf_pressure/current.json'), fixture())
            self.assertIn('人工行业ETF甲', views.render(now=NOW))

    def test_unknown_fields_private_notes_unsafe_links_and_html_are_not_exposed(self):
        report = fixture(); report['payload'] = {'account': 'PRIVATE-PAYLOAD'}
        report['flow']['note'] = '账户总资产123万元'
        report['flow']['source'] = 'https://example.com/?token=PRIVATE-TOKEN'
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
