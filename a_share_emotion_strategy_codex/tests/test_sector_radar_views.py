"""Compact radar UI tests with fictional saved research only."""
import copy
import datetime as dt
import os
import tempfile
import unittest
from unittest.mock import patch

import sector_radar_views as views
from investment_dashboard import render_dashboard
from sector_radar_daily import build_daily_model
from test_sector_radar_daily import fixture, add_observed

NOW = dt.datetime(2026, 9, 25, 16, 0, tzinfo=views.TZ)


class SectorRadarViewsTests(unittest.TestCase):
    def page(self, data=None, state='partial', days=None):
        report = fixture() if data is None else data
        if days is None:
            days = [build_daily_model(report, historical=state in ('expired', 'unavailable'))] if report else []
        with patch.object(views, 'load_daily_reports', return_value={
                'days': days, 'state': state, 'latest_attempt': {}}) as loaded:
            text = views.render_page(NOW)
        loaded.assert_called_once_with(now=NOW)
        return text

    def test_priority_review_names_visible_even_when_not_admitted(self):
        text = self.page()
        for value in ('虚构甲', '虚构乙', '未入选 · 待补证', '未入选 · 条件未满足', '最近压力下的空间不足'):
            self.assertIn(value, text)
        self.assertIn('虚构板块甲 / 虚构板块乙', text)
        self.assertEqual(text.count('data-radar-stock="600901"'), 1)
        self.assertIn('id="radar-list-2026-09-24-review" data-radar-list="review"', text)
        self.assertIn('最近压力 12.10', text)

    def test_complete_universe_matrix_and_export_removed_not_merely_hidden(self):
        report = fixture()
        report['candidates'].append({'code': '600999', 'name': '仅在完整池不应上屏', 'state': 'pending',
                                     'strategy_checks': {'dragon': {'status': 'failed', 'reasons': ['PRIVATE-MATRIX']}}})
        text = self.page(report)
        for old in ('全部股票与五策略矩阵', 'data-radar-table', 'data-radar-export', 'radar-check-grid',
                    '仅在完整池不应上屏', 'PRIVATE-MATRIX', 'radar-stock-600999', 'data-research-search="sector-radar"'):
            self.assertNotIn(old, text)
        self.assertIn('id="radar-universe"', text)  # Safe destination for old deep links.
        self.assertIn('id="radar-prelaunch-focus"', text)

    def test_dates_and_categories_are_separate_accessible_tabs(self):
        days = [build_daily_model(fixture()), build_daily_model(fixture('2026-09-22'), historical=True)]
        text = self.page(days=days)
        self.assertEqual(text.count('data-radar-date='), 2)
        self.assertEqual(text.count('data-radar-list-tab='), 6)
        self.assertIn('data-radar-day="2026-09-22"', text)
        self.assertNotIn('data-radar-date="2026-09-23"', text)
        self.assertIn('历史列表 · 不授予当前观察资格', text)
        self.assertIn('aria-controls="radar-list-2026-09-24-started"', text)
        self.assertIn('role="tabpanel"', text)
        self.assertIn('data-radar-until="2026-09-28T15:00:00+08:00"', text)

    def test_formal_observation_requires_native_flag_and_saved_top(self):
        report = fixture(); add_observed(report)
        self.assertIn('收盘观察 · 非买点', self.page(report))
        report['candidates'][-1]['strategy_checks']['prelaunch']['observation_passed'] = False
        text = self.page(report)
        self.assertNotIn('data-radar-stock="600904"', text)
        self.assertIn('暂无完整核验通过的正式观察股', text)

    def test_native_core_outside_original_top_is_not_lost_or_called_top_three(self):
        report = fixture(); add_observed(report)
        core = report['prelaunch_focus']['sectors'][0]['entries'][0]
        core.update(tier='core', native_core=True)
        report['candidates'][0].update(state='watch', strategy_checks=copy.deepcopy(report['candidates'][-1]['strategy_checks']))
        text = self.page(report)
        self.assertIn('潜伏核心 · 非板块前三排名', text)
        self.assertIn('data-radar-stock="600901"', text)
        self.assertIn('data-radar-stock="600904"', text)

    def test_never_run_failure_valid_empty_are_distinct(self):
        self.assertIn('尚未执行', self.page({}, 'not_run'))
        self.assertIn('本轮研究受阻', self.page({}, 'unavailable'))
        report = fixture(); report.update(status='empty', candidates=[], prelaunch_focus={})
        self.assertIn('筛选完成，正式观察为空', self.page(report, 'empty'))
        failed = self.page(state='unavailable')
        self.assertIn('仅保留原日期列表', failed)
        self.assertIn('历史列表 · 不授予当前观察资格', failed)

    def test_escaped_markup_safe_sources_and_no_private_fields(self):
        report = fixture()
        row = report['prelaunch_focus']['sectors'][0]['entries'][0]
        row['name'] = '<script>alert(1)</script>'
        row['missing'] = ['/Users/private/account.json']
        row['sources'] += [{'url': 'javascript:alert(1)'}, {'url': 'file:///tmp/secret'},
                           {'url': 'https://example.com/path?token=SECRET-TOKEN'}]
        report['raw'] = {'password': 'PRIVATE-RAW'}
        text = self.page(report)
        for bad in ('<script>', 'PRIVATE-RAW', 'SECRET-POSITION', '/Users/private', 'javascript:alert', 'file:///tmp', 'SECRET-TOKEN'):
            self.assertNotIn(bad, text)
        self.assertIn('&lt;script&gt;', text)
        self.assertIn('href="https://example.com/evidence"', text)

    def test_details_collapsed_without_repeating_sector_empty_cards(self):
        text = self.page()
        self.assertNotIn(' open>', text)
        self.assertEqual(text.count('class="radar-daily-row"'), 3)
        self.assertNotIn('class="radar-sector"', text)
        self.assertNotIn('本次缺失证据与执行状态', text)

    def test_known_long_barrier_shortened_but_original_retained(self):
        report = fixture()
        reason = '已知冻结上沿下的毛空间上限仅 1.100，已小于原规则净空间≥2；更近压力及非负费用只会缩小空间'
        report['prelaunch_focus']['sectors'][0]['near_misses'][0]['failure_reasons'] = [reason]
        text = self.page(report)
        self.assertIn('毛空间上限 1.100＜2，空间不足', text)
        self.assertIn(reason, text)

    def test_no_real_lists_in_public_fixtures_and_nine_pages_remain(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'AUTOSTRATEGY_PRIVATE_ROOT': temp}):
            text = render_dashboard(dt.date(2026, 9, 23))
        for name in ('hot', 'dragon', 'yichujifa', 'prelaunch', 'late-day', 'three-step', 'sector-radar', 'strategy', 'reports'):
            self.assertIn(f'id="page-{name}"', text)
        self.assertEqual(text.count('class="nav-item'), 9)
        for old in ('strategy-late-day', 'strategy-three-step', 'dragon-cycle-watchlist', 'hot-sector-board'):
            self.assertIn(f'id="{old}"', text)
        self.assertNotIn('@@', text)


if __name__ == '__main__':
    unittest.main()
