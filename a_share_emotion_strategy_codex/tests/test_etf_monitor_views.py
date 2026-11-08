"""Offline renderer contracts using invented ETF names and measurements."""
import copy
import datetime as dt
from html.parser import HTMLParser
import os
import tempfile
import unittest
from unittest.mock import patch

import etf_monitor_views as views
from research_store import atomic_json, research_path


def fixture():
    row = {'code': '599901', 'name': '测试行业ETF甲', 'as_of': '2026-08-12', 'close': 1.25,
           'metrics': {'pct1': 0, 'pct5': 2.15, 'pct20': -1.25, 'pct60': 5.0, 'ma5': 1.23,
                       'ma20': 1.20, 'ma60': 1.10, 'ma20_5ago': 1.19, 'vol20': 15.5,
                       'drawdown60': -8.5, 'rs20': None},
           'bucket': 'turn', 'state': '人工示例：短均线抬升，仍需后续确认',
           'bars': [{'date': '2026-08-10', 'close': 1.2}, {'date': '2026-08-11', 'close': 1.19},
                    {'date': '2026-08-12', 'close': 1.25}],
           'conditions': ['后续收盘仍位于观察均线上方'], 'invalid_condition': '跌破观察均线',
           'missing': ['基准收益尚未取得'], 'source': 'https://example.com/prices'}
    second = copy.deepcopy(row); second.update(code='599902', name='测试行业ETF乙', bucket='repair')
    us = copy.deepcopy(row); us.update(code='TEST', name='测试海外ETF'); us['metrics']['rs20'] = 1.75
    return {'schema_version': 1, 'as_of_cn': '2026-08-12', 'as_of_us': '2026-08-12',
            'generated_at': '2026-08-13T08:00:00+08:00', 'published_at': '2026-08-13T08:01:00+08:00',
            'status': 'partial', 'backfill': True, 'cn': [row, second], 'us': [us],
            'coverage': {'cn': 2, 'us': 1, 'cn_scope': '人工固定样本'},
            'focus': {'priority_code': '599901', 'alternatives': ['599902'], 'basis': '测试观察依据'},
            'heat': [{'industry': '测试行业甲', 'count': 2, 'amount_yuan': 120000000,
                      'stocks': [{'code': '609901', 'name': '测试公司甲'}], 'trend': None,
                      'source': 'https://example.com/pool'}],
            'events': [], 'gaps': ['部分行业没有对应ETF'],
            'sources': [{'label': '测试价格来源', 'url': 'https://example.com/prices', 'as_of': '2026-08-12',
                         'retrieved_at': '2026-08-13T08:00:00+08:00', 'basis': '人工构造，非真实行情'}]}


class Tags(HTMLParser):
    def __init__(self, text):
        super().__init__(); self.tags = []; self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class EtfMonitorViewTests(unittest.TestCase):
    def page(self, report=None, day=13):
        return views.render(fixture() if report is None else report, now=dt.datetime(2026, 8, day, 9, tzinfo=views.TZ))

    def test_sections_table_contract_and_metric_basis(self):
        page = self.page(); tags = Tags(page).tags
        for title in ('国内ETF走势', '初步反转观察', '修复观察', '涨停行业前八', '美股强弱对照', '政策与事件', '数据覆盖与方法'):
            self.assertIn(title, page)
        rows = [attrs for tag, attrs in tags if tag == 'tr' and 'data-etf-row' in attrs]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['data-etf-code'], '599901')
        self.assertEqual(rows[0]['data-etf-pct20'], '-1.25')
        self.assertEqual(rows[0]['data-etf-order'], '0')
        self.assertIn('id="etfm-detail-599901"', page)
        self.assertIn('data-etf-asof="2026-08-12"', page)
        self.assertIn('data-etf-validity', page)
        self.assertIn('data-etf-empty class="etfm-empty" hidden', page)
        self.assertIn('<polyline', page)
        self.assertIn('美股上涨不等于A股次日上涨', page)
        self.assertIn('1.20 亿', page)
        self.assertIn('历史补录快照', page)
        self.assertNotIn(' open', page)

    def test_missing_snapshot_and_bad_structure_fail_closed(self):
        for report in ({}, [], {'schema_version': True}, dict(fixture(), cn='broken'), dict(fixture(), coverage=[])):
            with self.subTest(report=type(report)):
                page = self.page(report)
                self.assertIn('数据不可用', page)
                self.assertNotIn('data-etf-row', page)
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'AUTOSTRATEGY_PRIVATE_ROOT': root}):
            self.assertIn('等待可核验', views.render())
            atomic_json(research_path('etf_monitor/current.json'), fixture())
            self.assertIn('测试行业ETF甲', views.render(now=dt.datetime(2026, 8, 13, 9, tzinfo=views.TZ)))

    def test_future_and_expired_snapshots(self):
        future = fixture(); future['as_of_cn'] = '2026-08-14'
        self.assertIn('数据不可用', self.page(future))
        future = fixture(); future['published_at'] = '2026-08-14T09:00:00+08:00'
        self.assertIn('未来时间', self.page(future))
        self.assertNotIn('历史参考 · 需刷新', self.page(day=16))
        expired = self.page(day=17)
        self.assertIn('历史参考 · 需刷新', expired)
        self.assertIn('以下全部仅供历史参考', expired)
        self.assertIn('历史 · 初步反转', expired)

    def test_invalid_rows_do_not_break_other_rows_or_use_future_bars(self):
        report = fixture(); report['cn'][0]['bars'].append({'date': '2026-08-14', 'close': 9})
        report['cn'].append(copy.deepcopy(report['cn'][1]))
        page = self.page(report)
        self.assertIn('2 条记录', page)
        self.assertEqual(page.count('<tr data-etf-row'), 1)
        self.assertNotIn('etfm-detail-599901', page)
        self.assertNotIn('2026-08-14', page)

    def test_no_private_unknown_fields_or_unsafe_links(self):
        report = fixture(); report['account'] = {'cash': 'PRIVATE-ACCOUNT'}
        row = report['cn'][0]; row['name'] = '<img src=x onerror=alert(1)>'
        row['conditions'] = ['/Users/private/account.json', 'password=PRIVATE-PASSWORD']
        row['raw_path'] = '/private/secret'; row['source'] = 'javascript:alert(1)'
        report['sources'] += [{'url': url, 'label': 'bad'} for url in (
            'file:///tmp/private', 'https://user:credential@example.com/a',
            'https://example.com/?token=PRIVATE-TOKEN', 'https://127.0.0.1/local',
            'https://example.com/?api_key=PRIVATE-KEY', 'https://example.com/\nattack')]
        page = self.page(report)
        for bad in ('<img', 'PRIVATE-ACCOUNT', 'PRIVATE-PASSWORD', 'PRIVATE-TOKEN', 'PRIVATE-KEY',
                    '/Users/private', '/private/secret', 'javascript:', 'file://', 'user:credential', 'https://127.0.0.1'):
            self.assertNotIn(bad, page)
        self.assertIn('&lt;img', page)
        tags = Tags(page).tags
        self.assertFalse(any(k.lower().startswith('on') for _, attrs in tags for k in attrs))
        self.assertTrue(all(cls.startswith('etfm') for _, attrs in tags for cls in attrs.get('class', '').split()))
        self.assertIn('rel="noopener noreferrer"', page)

    def test_nan_infinite_bool_and_missing_are_not_zero(self):
        report = fixture(); m = report['cn'][0]['metrics']
        m.update(pct5=float('nan'), pct20=True, vol20=float('inf'), ma20=None)
        page = self.page(report)
        row = [a for t, a in Tags(page).tags if t == 'tr' and a.get('data-etf-code') == '599901'][0]
        for key in ('pct5', 'pct20', 'vol20'):
            self.assertEqual(row['data-etf-' + key], '')
        self.assertNotIn('nan', page); self.assertNotIn('Infinity', page)
        self.assertIn('走势待补', page)
        self.assertIn('本次未提供已核验政策', page)

    def test_heat_order_is_count_then_only_limit_up_turnover(self):
        report = fixture(); report['heat'] += [
            {'industry': '测试行业乙', 'count': 2, 'amount_yuan': 200000000},
            {'industry': '测试行业丙', 'count': 1, 'amount_yuan': 900000000}]
        page = self.page(report)
        self.assertLess(page.index('<b>测试行业乙</b>'), page.index('<b>测试行业甲</b>'))
        self.assertLess(page.index('<b>测试行业甲</b>'), page.index('<b>测试行业丙</b>'))
        self.assertEqual(page.count('<small>家数并列</small>'), 2)
        self.assertIn('并非整个行业成交或净流入', page)

    def test_intraday_empty_observation_and_future_fail_closed(self):
        self.assertIn('尚未取得两帧新行情', self.page())
        report = fixture(); report['intraday'] = {'status': 'observation_only', 'observed_at': '2026-08-13T08:59:30+08:00',
            'rows': [{'code': '599901', 'name': '测试盘中ETF', 'price': 1.24, 'pct': 0.8, 'vwap': 1.23,
                      'volume_ratio': 987654, 'as_of': '2026-08-13T08:59:29+08:00', 'conditions': ['等待新增行情']} ]}
        page = self.page(report)
        self.assertIn('测试盘中ETF', page)
        self.assertIn('盘中观察 · 未确认', page)
        self.assertNotIn('987654', page)
        self.assertIn('来源不明的量比不能替代', page)
        self.assertEqual(page.count('<tr data-etf-row'), 2)
        self.assertIn('data-etf-quote-at="2026-08-13T08:59:29+08:00"', page)
        self.assertIn('历史盘中观察 · 时效不足', views.render(report, now=dt.datetime(2026, 8, 13, 9, 2, tzinfo=views.TZ)))
        report['intraday']['observed_at'] = '2026-08-13T09:55:00+08:00'
        self.assertNotIn('测试盘中ETF', self.page(report))
        self.assertIn('本次复核不可用', self.page(report))

    def test_unhashable_optional_values_remain_unavailable(self):
        report = fixture(); report['cn'][0]['bucket'] = []
        report['heat'][0]['trend'] = {'ma20_direction': []}
        page = self.page(report)
        self.assertIn('数据不足', page)
        self.assertIn('走势待补', page)

    def test_evidence_ids_survive_heat_rank_changes(self):
        report = fixture(); first = self.page(report)
        ids = [a['id'] for t, a in Tags(first).tags if t == 'details' and a.get('id', '').startswith('etfm-heat-')]
        report['heat'].insert(0, {'industry': '另一人工行业', 'count': 9, 'amount_yuan': 1})
        second = self.page(report)
        self.assertIn('id="' + ids[0] + '"', second)
        self.assertIn('id="etfm-us-TEST"', second)
        self.assertIn('id="etfm-sources"', second)
        self.assertNotIn('</span> 只ETF', second)


if __name__ == '__main__':
    unittest.main()
