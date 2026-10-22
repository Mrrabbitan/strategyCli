"""Artificial-only highmark presentation fixtures: no quotes or notifications."""
import copy
import datetime as dt
import os
import tempfile
import unittest
from unittest.mock import patch

import highmark_views as views
from research_store import atomic_json, research_path


def fixture():
    return {
        'date': '2026-08-12', 'phase': 'close', 'trade_date': '2026-08-12',
        'as_of': '2026-08-12T15:00:00+08:00',
        'generated_at': '2026-08-12T15:41:00+08:00', 'status': 'partial',
        'coverage': {'new_pool_count': 1, 'retained_count': 1, 'scanned_count': 2,
                     'total_count': 2, 'pool_complete': True},
        'rows': [{'code': '600901', 'name': '虚构甲', 'boards': 3,
                  'origin': 'current_pool', 'reference_only': True, 'missing': [],
                  'reset_note': '原公告区间尚未全量复核',
                  'metrics': {
                      '3': {'value': 19.25, 'threshold': 20, 'headroom_pp': .75,
                            'next_day_flat_index_pct': 4.25, 'start': '2026-08-10',
                            'end': '2026-08-12', 'days': 3, 'status': 'reference'},
                      '10': {'value': 92.125, 'threshold': 100, 'headroom_pp': 7.875,
                             'next_day_flat_index_pct': 3.5, 'start': '2026-07-30',
                             'end': '2026-08-12', 'days': 10, 'status': 'reference'},
                      '30': {'value': 205, 'threshold': 200, 'headroom_pp': -5,
                             'next_day_flat_index_pct': 6.5, 'start': '2026-07-02',
                             'end': '2026-08-12', 'days': 30, 'status': 'reference'}}}],
        'missing': ['人工测试缺口'], 'sources': [{'label': '人工规则来源',
                                                   'url': 'https://example.org/rule'}],
        'edition': 'artificial-edition', 'build_status': 'updated'}


class HighmarkViewsTests(unittest.TestCase):
    def test_three_metrics_distances_and_scenario_not_trade_allowance(self):
        report = fixture()
        page = views.render(report)
        markdown = views.render_markdown(report)
        for text in (page, markdown):
            for token in ('虚构甲', '600901', '+19.25%', '0.75 个百分点',
                          '+92.12%', '7.88 个百分点', '已超阈值 5.00 个百分点',
                          '约 +3.50% · 10日窗口', views.REFERENCE,
                          '阈值距离不是可上涨额度', '触线不代表必然停牌'):
                self.assertIn(token, text)
            self.assertNotIn('精确可涨到', text)
            self.assertNotIn('已通过官方', text)
        self.assertIn('id="topic-highmark-abnormal"', page)
        self.assertIn('2026-07-30—2026-08-12（10个交易日）', page)
        self.assertIn('<details id="highmark-evidence">', page)
        self.assertNotIn('<details id="highmark-evidence" open', page)
        self.assertIn('target="_blank" rel="noopener noreferrer"', page)

    def test_missing_values_are_dash_never_zero_or_valid_scenario(self):
        report = fixture()
        report['rows'][0]['metrics'] = {
            '3': {'value': 0, 'threshold': 20, 'headroom_pp': 20,
                  'next_day_flat_index_pct': 0, 'status': 'reference'},
            '10': {'value': None, 'next_day_flat_index_pct': 1},
            '30': {'value': float('nan'), 'next_day_flat_index_pct': 2}}
        self.assertIn('+0.00%', views.render(report))
        self.assertIn('约 +0.00% · 3日窗口', views.render_markdown(report))
        self.assertEqual(views._metric_cell(report['rows'][0], '10', 100), ('—', ''))
        for invalid in (True, float('inf'), float('nan'), 'broken'):
            report['rows'][0]['metrics']['3']['value'] = invalid
            self.assertNotIn('约 +0.00%', views.render_markdown(report))

    def test_status_failure_hides_unverified_metric_and_no_empty_success(self):
        report = fixture()
        report['rows'][0]['metrics']['10']['status'] = 'unavailable'
        self.assertNotIn('+92.12%', views.render(report))
        report.update(rows=[], status='unavailable')
        page, markdown = views.render(report), views.render_markdown(report)
        for text in (page, markdown):
            self.assertIn('未取得可展示的高标异动结果', text)
            self.assertIn('不表示市场没有高标或异动风险', text)
            self.assertNotIn('本轮计算完成', text)

    def test_calculator_aliases_and_derived_headroom_are_supported(self):
        report = fixture()
        report['rows'][0]['metrics']['3'] = {
            'deviation_pct': 18.5, 'max_start': '2026-08-11',
            'max_end': '2026-08-12', 'length': 2, 'status': 'complete'}
        page = views.render(report)
        self.assertIn('+18.50%', page)
        self.assertIn('距阈值 1.50 个百分点', page)
        self.assertIn('2026-08-11—2026-08-12（2个交易日）', page)

    def test_html_markup_markdown_and_source_links_are_safe(self):
        report = fixture()
        row = report['rows'][0]
        row.update(name='<img src=x onerror=alert(1)>|[*]',
                   reset_note='/Users/private/evidence.json',
                   missing=['Traceback token=PRIVATE-ERROR'])
        report['sources'] += [
            {'label': 'unsafe', 'url': 'javascript:alert(1)'},
            {'label': 'unsafe', 'url': 'file:///tmp/private'},
            {'label': 'unsafe', 'url': 'https://user:secret@example.org'},
            {'label': 'unsafe', 'url': 'https://example.org/?access_token=PRIVATE-TOKEN'},
            {'label': 'unsafe', 'url': 'https://example.org/?%74oken=PRIVATE-ENCODED'},
            {'label': 'unsafe', 'url': 'https://example.org/" onmouseover="bad'},
            {'label': 'safe<script>', 'url': 'https://example.org/public?a=1&b=2'}]
        report['raw'] = {'secret': 'PRIVATE-RAW'}
        page, markdown = views.render(report), views.render_markdown(report)
        for text in (page, markdown):
            for token in ('<img', '<script>', '/Users/private', 'PRIVATE-TOKEN',
                          'PRIVATE-ENCODED', 'PRIVATE-RAW', 'PRIVATE-ERROR',
                          'javascript:', 'file://', 'https://user:secret'):
                self.assertNotIn(token, text)
        self.assertIn('&lt;img', page)
        self.assertIn('\\|\\[\\*\\]', markdown)
        self.assertIn('本地证据已留档', page)
        self.assertIn('https://example.org/public?a=1&amp;b=2', page)

    def test_failed_build_and_coverage_do_not_claim_sync_or_full_market(self):
        report = fixture()
        report['coverage']['pool_complete'] = False
        report['build_status'] = 'failed'
        page, markdown = views.render(report), views.render_markdown(report)
        self.assertIn('本轮页面同步未完成', page)
        self.assertIn('页面同步失败', markdown)
        for text in (page, markdown):
            self.assertIn('当期涨停池覆盖不足', text)
            self.assertNotIn('全市场完整', text)

    def test_default_reads_only_private_snapshot_and_preserves_original_day(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'AUTOSTRATEGY_PRIVATE_ROOT': temp}):
            self.assertIn('尚未执行', views.render())
            atomic_json(research_path('highmark/current.json'), fixture())
            page = views.render(now=dt.datetime(2026, 8, 13, 9, tzinfo=views.TZ))
            self.assertIn('2026-08-12 · 盘后', page)
            self.assertNotIn('2026-08-13 · 盘后', page)

    def test_future_generated_snapshot_not_rendered_as_current(self):
        report = fixture()
        page = views.render(report, now=dt.datetime(2026, 8, 12, 15, tzinfo=views.TZ))
        self.assertIn('报告时点异常', page)
        self.assertNotIn('虚构甲', page)

    def test_primary_keeps_current_ladders_and_near_severe_threshold_only(self):
        report = fixture()
        current = report['rows'][0]
        retained_near = copy.deepcopy(current)
        retained_near.update(code='600902', name='虚构临界乙', boards=0)
        retained_near['metrics']['10']['headroom_pp'] = 30
        retained_near['metrics']['30']['headroom_pp'] = 31
        retained_far = copy.deepcopy(retained_near)
        retained_far.update(code='600903', name='虚构远阈丙')
        retained_far['metrics']['10']['headroom_pp'] = 30.001
        # A close 3-session threshold does not promote a distant retained name.
        retained_far['metrics']['3']['headroom_pp'] = -.5
        unavailable = copy.deepcopy(retained_near)
        unavailable.update(code='600904', name='虚构未算丁')
        unavailable['metrics']['10']['status'] = 'unavailable'
        without_boards = copy.deepcopy(retained_far)
        without_boards.update(code='600905', name='虚构省略板数戊')
        without_boards.pop('boards')
        report['rows'] = [current, retained_near, retained_far, unavailable, without_boards]
        primary, other = views._display_rows(report)
        self.assertEqual([r['code'] for r in primary], ['600901', '600902', '600905'])
        self.assertEqual([r['code'] for r in other], ['600903', '600904'])
        page, markdown = views.render(report), views.render_markdown(report)
        main, collapsed = page.split('<details id="highmark-other">', 1)
        for name in ('虚构甲', '虚构临界乙', '虚构省略板数戊'):
            self.assertIn(name, main)
            self.assertIn(name, markdown)
        for name in ('虚构远阈丙', '虚构未算丁'):
            self.assertNotIn(name, main)
            self.assertNotIn(name, markdown)
            self.assertIn(name, collapsed)
        self.assertIn('其余跟踪股 · 2只', collapsed)
        self.assertIn('其余 2 只跟踪股见页面展开', markdown)
        self.assertEqual(page.count('<th>3日偏离／距20</th>'), 2)
        self.assertNotIn('<details id="highmark-other" open', page)

    def test_markdown_retains_four_core_sources_page_preserves_all(self):
        report = fixture()
        report['sources'] = [
            {'label': '原始池', 'url': 'https://push2ex.eastmoney.com/getTopicZTPool?date=20260812'},
            {'label': '逐股日线1', 'url': 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=fiction1'},
            {'label': '逐股日线2', 'url': 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=fiction2'},
            {'label': '逐股报价', 'url': 'https://qt.gtimg.cn/q=sh600901'},
            {'label': '上交所交易规则', 'url': 'https://www.sse.com.cn/lawandrules/test-rule.shtml'},
            {'label': '深交所交易规则', 'url': 'https://docs.static.szse.cn/www/lawrules/rule/test.pdf'},
            {'label': '额外证据', 'url': 'https://example.org/additional'}]
        page, markdown = views.render(report), views.render_markdown(report)
        for label in ('收盘涨停池', '腾讯日线与收盘报价', '上交所规则', '深交所规则'):
            self.assertIn(label, markdown)
        self.assertEqual(markdown.count('](<https://'), 4)
        self.assertNotIn('fiction2', markdown)
        self.assertNotIn('额外证据', markdown)
        self.assertIn('fiction2', page)
        self.assertIn('额外证据', page)
        self.assertIn('完整计算 2/2 只', markdown)
        self.assertNotIn('核验 2/2 只', markdown)

    def test_all_distant_retained_rows_are_folded_not_misreported_failed(self):
        report = fixture()
        row = report['rows'][0]
        row['boards'] = 0
        for key in ('10', '30'):
            row['metrics'][key]['headroom_pp'] = 50
        markdown = views.render_markdown(report)
        self.assertNotIn('虚构甲', markdown)
        self.assertIn('当前主表没有达到展示门槛', markdown)
        self.assertNotIn('未取得可展示的高标异动结果', markdown)
        self.assertIn('虚构甲', views.render(report))


if __name__ == '__main__':
    unittest.main()
