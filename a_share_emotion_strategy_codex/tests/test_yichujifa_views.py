"""Artificial per-branch UI evidence fixtures; no private stock data is read."""
import copy
import unittest

import yichujifa_views as views


def fixture():
    row = {'code': '600001', 'name': '虚构观察甲', 'branch': 'hold_breakout', 'sector': '人工行业',
           'native_shape': {'anchor_date': '2026-09-07', 'anchor_low': 8.25, 'anchor_mid': 9.15,
                            'anchor_high': 10.05, 'post_days': 3, 'median_volume_ratio': .7,
                            'status': 'holding', 'midpoint_dips': ['2026-09-08'], 'recovery_closes': 2},
           'announcement': {'reviewed_through': '2026-09-10T16:00:00+08:00', 'alerts': ['人工公告风险摘要'],
                            'sources': ['https://example.com/close-filing']}}
    data = {'phase': 'intraday', 'generated_at': '2026-09-11T10:00:00+08:00',
            'live_review': {'mode': 'live', 'time': '2026-09-11T09:58:05+08:00', 'reasons': []},
            'native_report': {'pool_members': [{'c': '600002', 'n': '虚构同业乙', 'hybk': '人工行业', 'lbc': 1},
                                              {'c': '600003', 'n': '无关行业丙', 'hybk': '其他行业', 'lbc': 1}]},
            'sector_evidence': [{'name': '人工行业', 'index': {'return_pct': 1.2, 'ma5': 111.1, 'verified': True},
                                 'cores': [{'code': '600002', 'name': '虚构固定核心', 'selected_on': '2026-09-09',
                                            'return_2d_pct': -1.1, 'close': 10, 'ma5': 11, 'verified': True}]}],
            'snapshots': []}
    for number, minute in enumerate((56, 57, 58)):
        at = f'2026-09-11T09:{minute}:00+08:00'
        volume = {'time': at, 'baseline_dates': ['2026-09-04', '2026-09-07', '2026-09-08', '2026-09-09', '2026-09-10'],
                  'baseline_volumes': [100, 120, 130, 140, 150], 'volume': 180 + number,
                  'ratio': (180 + number) / 130, 'verified': True, 'source': 'https://example.com/minute-volume'}
        quote = {'time': at, 'price': 9.95 + number * .1, 'previous_close': 9.9, 'vwap': 9.95,
                 'at_limit': number > 0, 'turnover_today': 1.5, 'day_low': 9.85, 'verified': True,
                 'comparable_volume': volume}
        data['snapshots'].append({'time': at, 'quotes': {'600001': quote, '600002': dict(quote, price=12.34)},
                                  'emotion': {'code': '883410', 'time': at, 'price': 115.2, 'previous_close': 114.8, 'verified': True},
                                  'reviews': {'600001': {'reviewed_through': at, 'disclosures_checked_on': '2026-09-11',
                                                       'sources': ['https://example.com/live-filing']}}})
    return row, data


class YichujifaViewsTests(unittest.TestCase):
    def test_native_anchor_prices_visible_without_chart(self):
        row, data = fixture(); row['bars'] = []
        output = views.render_native_evidence(row, data)
        for text in ('8.25', '9.15', '10.05', '首板最低价 L', '首板实体中点 M', '首板最高价 H'):
            self.assertIn(text, output)
        self.assertNotIn('<svg', output)

    def test_downgrade_recovery_and_announcement_are_actual_fields(self):
        output = views.render_native_evidence(*fixture())
        for text in ('2026-09-08', '连续收盘恢复计数', '人工公告风险摘要', 'https://example.com/close-filing'):
            self.assertIn(text, output)

    def test_fixed_industry_core_and_index_are_displayed(self):
        output = views.render_native_evidence(*fixture())
        for text in ('虚构固定核心', '2026-09-09', '111.1', '行业指数五日涨幅'):
            self.assertIn(text, output)

    def test_three_frames_preserve_distinct_source_time_and_values(self):
        output = views.render_live_evidence(*fixture())
        for text in ('事件前基准', '首次触发观察', '再次确认观察', '09:56:00', '09:57:00', '09:58:00', '个股自身源时间', '60 秒'):
            self.assertIn(text, output)

    def test_same_peer_and_883410_have_own_times(self):
        output = views.render_live_evidence(*fixture())
        for text in ('883410', '115.2', '虚构同业乙', '12.34', '首次VWAP', '再次VWAP'):
            self.assertIn(text, output)
        self.assertNotIn('无关行业丙', output)

    def test_hold_branch_displays_all_five_minute_baselines(self):
        output = views.render_live_evidence(*fixture())
        for text in ('2026-09-04', '2026-09-07', '2026-09-08', '2026-09-09', '2026-09-10',
                     '分钟量自身时间', 'https://example.com/minute-volume', '1.2倍'):
            self.assertIn(text, output)

    def test_missing_live_does_not_fabricate_empty_success(self):
        row, data = fixture(); data.update(live_review=None, snapshots=None)
        output = views.render_live_evidence(row, data)
        self.assertIn('尚未取得完整盘中复核', output)
        self.assertIn('至少间隔60秒', output)
        self.assertIn('未填入虚构行情', output)
        self.assertNotIn('09:58:00', output)

    def test_replay_is_explicitly_historical(self):
        row, data = fixture(); data['live_review'].update(mode='replay', replay_only=True)
        output = views.render_live_evidence(row, data)
        self.assertIn('历史重放：不是当前参与信号', output)
        self.assertNotIn('candidate-state positive', output)

    def test_missing_own_source_time_is_not_filled_with_sample_time(self):
        row, data = fixture(); del data['snapshots'][-1]['quotes']['600001']['time']
        output = views.render_live_evidence(row, data)
        self.assertIn('后两次个股自身行情间隔：未提供 秒', output)

    def test_future_frame_or_minute_volume_not_displayed_as_observed(self):
        row, data = fixture(); data['snapshots'][-1]['time'] = '2099-01-01T09:58:00+08:00'
        data['snapshots'][-1]['quotes']['600001']['price'] = 77777
        data['snapshots'][1]['quotes']['600001']['comparable_volume'].update(time='2099-01-01T09:57:00+08:00', volume=88888)
        output = views.render_live_evidence(row, data)
        self.assertNotIn('77777', output)
        self.assertNotIn('88888', output)

    def test_chain_branch_does_not_import_hold_volume_threshold(self):
        row, data = fixture(); row.update(branch='one_to_two', native_shape={})
        output = views.render_live_evidence(row, data)
        self.assertIn('新的换手封板或回封', output)
        self.assertIn('不混用于此分支', output)
        self.assertNotIn('此前交易日', output)

    def test_only_safe_allowlisted_fields_are_rendered(self):
        row, data = fixture(); row['announcement']['alerts'] = ['<script>bad()</script>', '/Users/private/secrets.json']
        row['announcement']['sources'] += ['javascript:alert(1)', 'https://user:pass@example.com/private']
        data['snapshots'][0]['account'] = 'ACCOUNT_SECRET_MARKER'
        output = views.render_evidence(row, data)
        self.assertIn('&lt;script&gt;', output)
        for text in ('<script>', '/Users/', 'javascript:', 'https://user:pass', 'ACCOUNT_SECRET_MARKER'):
            self.assertNotIn(text, output)

    def test_live_announcement_review_is_separate_from_close_review(self):
        output = views.render_evidence(*fixture())
        self.assertIn('2026-09-10T16:00:00', output)
        self.assertIn('当日公告检查日期', output)
        self.assertIn('https://example.com/live-filing', output)


if __name__ == '__main__': unittest.main()
