"""Nightly report presentations tested only with invented companies and amounts."""
import datetime as dt
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import night_radar_views as views
from investment_dashboard import render_dashboard


def fixture(day='2026-08-12'):
    common = {'code': '600901', 'name': '虚构甲', 'source_url': 'https://example.com/notice',
              'source_time': day + 'T19:06:00+08:00'}
    rows = {
        'limit_up': dict(common, boards=3, risk='次日起停牌核查，复牌时间未定'),
        'announcements': dict(common, title='测试公告标题', summary='测试采购合同，非销售订单',
                              stance='risk', kind='正式公告', source_time_basis='供应商展示时间'),
        'earnings': dict(common, period_label='前三季度', profit_low_cny=123456789,
                         profit_high_cny=130000000, change_low_pct=60.1, change_high_pct=70.3,
                         forecast_type='预增', basis='归母净利润', estimated=True),
        'repurchases': dict(common, budget_low_cny=10000000, budget_high_cny=20000000,
                            price_cap=8, actual_cny=None, progress='董事会预案', purpose='注销'),
        'broker_seats': dict(common, net_cny=-12345678, pct=0, period='single',
                             top_seats=[{'name': '虚构营业部甲', 'net_cny': -22345678},
                                        {'name': '虚构营业部乙', 'net_cny': 10000000}]),
        'institutions': dict(common, net_cny=9012345, pct=-2.56, turnover_pct=9.2,
                             industry='虚构行业', period='single'),
    }
    return {'report_date': day, 'trade_date': day, 'cutoff': day + 'T22:00:00+08:00',
            'executed_at': day + 'T22:03:00+08:00', 'status': 'complete', 'mode': 'daily',
            'summary': '测试收盘事实与晚间披露',
            'sections': {key: {'status': 'complete', 'rows': [row], 'featured': [row],
                               'missing': [], 'sources': [{'label': '公开测试来源', 'url': 'https://example.com/source'}]}
                         for key, row in rows.items()}}


class NightRadarViewsTests(unittest.TestCase):
    def test_six_modules_source_links_and_cny_units(self):
        page = views.render_page([fixture()])
        self.assertEqual(page.count('class="night-card"'), 6)
        for _, title, _ in views.SECTIONS:
            self.assertIn(title, page)
        for expected in ('12,345.68～13,000', '-1,234.57', '901.23', '次日起停牌',
                         '采购合同，非销售订单', '供应商展示时间', '推算值', 'http'):
            self.assertIn(expected, page)
        self.assertNotIn('测试公告标题；测试采购合同', page)
        self.assertIn('target="_blank" rel="noopener noreferrer"', page)

    def test_unknown_actual_amount_is_not_zero_and_zero_is_preserved(self):
        report = fixture()
        row = report['sections']['repurchases']['featured'][0]
        self.assertEqual(views.row_cells('repurchases', row)[3], '—')
        self.assertEqual(views.row_cells('broker_seats', report['sections']['broker_seats']['featured'][0])[-1], '0')
        row['actual_cny'] = 0
        self.assertEqual(views.row_cells('repurchases', row)[3], '0')
        for invalid in (True, float('nan'), float('inf'), 'bad'):
            self.assertEqual(views.number(invalid), '—')

    def test_date_tabs_sorted_unique_and_deep_link_accessible(self):
        page = views.render_page([fixture('2026-08-11'), fixture(), fixture('2026-08-11')])
        self.assertEqual(page.count('data-night-date='), 2)
        self.assertLess(page.index('data-night-date="2026-08-12"'), page.index('data-night-date="2026-08-11"'))
        self.assertIn('id="topic-night-radar-20260812" role="tabpanel" aria-labelledby="night-tab-20260812"', page)
        self.assertIn('data-night-day="2026-08-11" tabindex="0" hidden', page)
        self.assertIn('aria-controls="topic-night-radar-20260812" aria-selected="true" tabindex="0"', page)
        broken = fixture('not-a-date')
        self.assertNotIn('not-a-date', views.render_page([broken]))

    def test_latest_attempt_of_same_date_wins_without_relabeling_old_day(self):
        newer = fixture(); newer.update(status='partial', summary='最新失败留在原日期', executed_at='2026-08-12T22:20:00+08:00')
        page = views.render_page([newer, fixture()])
        self.assertIn('最新失败留在原日期', page)
        self.assertNotIn('测试收盘事实与晚间披露', page)

    def test_full_rows_collapsed_multi_day_kept_separate_without_duplicate(self):
        report = fixture()
        section = report['sections']['broker_seats']
        multi = dict(section['rows'][0], name='虚构累计榜', period='multi', period_start=None,
                     period_end='2026-08-12', reason='累计偏离')
        extra = dict(section['rows'][0], name='虚构完整记录')
        section['rows'] += [multi, extra]
        section['multi_rows'] = [multi]
        page = views.render_page([report])
        self.assertIn('id="night-20260812-broker_seats-all"', page)
        self.assertIn('完整记录 · 2 条', page)
        self.assertIn('多日累计榜 · 1 条', page)
        self.assertEqual(page.count('虚构累计榜'), 1)
        self.assertIn('独立统计，不与单日榜相加', page)
        self.assertNotIn(' open', page)

    def test_failed_empty_and_not_run_are_distinct(self):
        self.assertIn('尚未执行', views.render_page([]))
        report = fixture()
        section = report['sections']['announcements']
        section.update(rows=[], featured=[], status='empty')
        self.assertIn('本期无符合本栏口径的记录', views.render_page([report]))
        section.update(status='unavailable', missing=['接口断开', '冗余故障详情'])
        page = views.render_page([report])
        self.assertIn('<p class="night-gap">接口连接失败；部分记录尚未完成核验。</p>', page)
        self.assertIn('暂无可核验的精选记录', page)
        self.assertIn('id="night-20260812-announcements-gaps"', page)
        self.assertIn('<li>冗余故障详情</li>', page)

    def test_public_gaps_summarize_http_errors_and_strip_event_ids(self):
        report = fixture()
        section = report['sections']['broker_seats']
        section.update(status='partial', missing=[
            'HTTPError 504: Traceback at https://example.com/private?token=SECRET-TOKEN timed out',
            '202608120001234567 买卖双榜不完整',
            '原始路径 /Users/private/evidence.json',
            '来源 qdate 与请求日期冲突'])
        page = views.render_page([report]); markdown = views.render_markdown(report)
        self.assertIn('接口超时，部分数据未取得；买卖席位榜不完整', page)
        self.assertIn('接口超时，部分数据未取得；买卖席位榜不完整', markdown)
        for private in ('HTTPError', 'Traceback', 'SECRET-TOKEN', '/Users/private', '202608120001234567'):
            self.assertNotIn(private, page)
            self.assertNotIn(private, markdown)
        self.assertIn('〔记录编号已省略〕 买卖双榜不完整', page)
        self.assertIn('本栏数据缺口', page)
        self.assertNotIn('〔记录编号已省略〕', markdown)

    def test_escaped_html_unsafe_links_and_private_payload_never_leak(self):
        report = fixture()
        row = report['sections']['announcements']['rows'][0]
        row.update(name='<img src=x onerror=alert(1)>', summary='/Users/private/evidence.json',
                   source_url='javascript:alert(1)', raw={'password': 'PRIVATE-RAW'})
        report['sections']['announcements']['sources'] += [
            {'url': 'file:///tmp/private'}, {'url': 'https://example.com/?access_token=PRIVATE-TOKEN'},
            {'url': 'https://user:private@example.com/'}, {'url': 'https://example.com/?%74oken=PRIVATE-ENCODED'}]
        report['raw'] = {'account': 'PRIVATE-ACCOUNT'}
        page = views.render_page([report]); markdown = views.render_markdown(report)
        for value in ('<img', '/Users/private', 'javascript:', 'file://', 'PRIVATE-TOKEN',
                      'PRIVATE-ENCODED', 'PRIVATE-RAW', 'PRIVATE-ACCOUNT'):
            self.assertNotIn(value, page)
            self.assertNotIn(value, markdown)
        self.assertIn('&lt;img', page)
        self.assertIn('本地证据已留档', page)

    def test_alignment_preserves_result_with_public_link(self):
        report = fixture(); report['mode'] = 'historical'
        report['alignment'] = [{'name': '虚构乙', 'code': '300901', 'module': '业绩',
                                'result': '范围差异', 'note': '仅用于对齐，不进入主板日报',
                                'source_url': 'https://example.com/alignment'}]
        page = views.render_page([report])
        self.assertIn('截图逐项对齐 · 1 条', page)
        self.assertIn('事后来源复核', page)
        self.assertIn('范围差异', page)
        self.assertIn('href="https://example.com/alignment"', page)
        self.assertIn('事后来源复核', views.render_markdown(report))

    def test_full_structured_rows_show_verification_note_and_late_arrival(self):
        report = fixture()
        section = report['sections']['announcements']
        extra = dict(section['rows'][0], name='未核验虚构丙', verification_note='原文未完成核验',
                     late_arrival=True, change='更正或更新')
        section['rows'].append(extra)
        page = views.render_page([report])
        for value in ('原文未完成核验', '补录', '更正或更新'):
            self.assertIn(value, page)
        self.assertNotIn('未核验虚构丙', views.render_markdown(report))

    def test_markdown_contains_only_featured_six_tables_and_safe_links(self):
        report = fixture()
        report['sections']['earnings']['rows'].append({'name': '仅完整展开可见', 'code': '600902'})
        markdown = views.render_markdown(report)
        self.assertEqual(sum(line.startswith('| ---') for line in markdown.splitlines()), 6)
        self.assertIn('[虚构甲 600901](<https://example.com/notice>)', markdown)
        self.assertIn('topic-night-radar-20260812', markdown)
        self.assertNotIn('仅完整展开可见', markdown)
        self.assertNotIn('###', markdown)

    def test_markdown_escapes_table_and_markup_and_preserves_ten_entries(self):
        report = fixture()
        report['sections']['limit_up']['featured'][0]['risk'] = '<script>|x\n[bad]*'
        markdown = views.render_markdown(report)
        self.assertIn('&lt;script&gt;\\|x \\[bad\\]\\*', markdown)
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'AUTOSTRATEGY_PRIVATE_ROOT': temp}), patch.object(views, 'load_reports', return_value=[report]), patch('research_topics.render_topics', side_effect=lambda **kw: '' if kw.get('include') else '<div id="test-existing-topics"></div>'):
            page = render_dashboard(dt.date(2026, 8, 12))
        self.assertEqual(page.count('class="nav-item'), 10)
        self.assertIn('id="topic-night-radar"', page)
        self.assertIn('id="page-reports"', page)
        self.assertNotIn('@@', page)
        self.assertLess(page.index('id="topic-night-radar"'), page.index('id="test-existing-topics"'))

    def test_reading_state_and_keyboard_handlers_are_connected(self):
        script = (Path(__file__).parents[1] / 'templates' / 'investment.js').read_text()
        for value in ('function selectNightDay', 'function setupNightTabs',
                      'selectNightDay(stored.night)', 'selectNightDay(nightDay.dataset.nightDay)',
                      'nightSelectedDate', 'buttons[next].focus()', 'setupNightTabs();'):
            self.assertIn(value, script)


if __name__ == '__main__':
    unittest.main()
