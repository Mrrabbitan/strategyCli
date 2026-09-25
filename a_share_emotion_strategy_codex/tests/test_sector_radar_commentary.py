"""Artificial OHLC examples; these checks are not investment backtests."""
import copy
import unittest

from sector_radar_commentary import describe_stock


DAY = '2026-09-24'


def bar(day, close=10.0, volume=1000, **extra):
    result = {'date': day, 'open': 10., 'high': 11., 'low': 9.,
              'close': close, 'volume_shares': volume}
    result.update(extra)
    return result


def row(price=10.5, support=9.0, upper=11.0, pressure=None):
    levels = [{'label': '冻结支撑', 'value': support}, {'label': '冻结上沿', 'value': upper}]
    if pressure is not None:
        levels.append({'label': '最近压力', 'value': pressure})
    return {'price': price, 'levels': levels, 'admitted': False, 'status': '优先复核'}


class CommentaryTests(unittest.TestCase):
    def setUp(self):
        self.bars = [bar('2026-09-23'), bar(DAY, 10.5, 2000)]

    def analyse(self, entry=None, bars=None):
        return describe_stock(row() if entry is None else entry,
                              self.bars if bars is None else bars, DAY)

    def test_rising_price_and_volume_are_literal_not_flow_identity(self):
        result = self.analyse()
        self.assertIn('放量上涨', result['verdict'])
        self.assertIn('+5.00%', result['price_volume'])
        self.assertIn('2.00倍', result['price_volume'])
        for phrase in ('主力', '吸筹', '资金净流入', '获利概率', '买入'):
            self.assertNotIn(phrase, str(result))
        self.assertEqual(result['metrics']['volume_ratio'], 2.)

    def test_falling_price_with_less_volume(self):
        result = self.analyse(row(9.5), [bar('2026-09-23'), bar(DAY, 9.5, 500)])
        self.assertIn('缩量回落', result['verdict'])
        self.assertIn('-5.00%', result['price_volume'])

    def test_below_frozen_support_is_first_even_if_overshoot(self):
        result = self.analyse(row(9., support=9.5, upper=10.5),
                              [bar('2026-09-23'), bar(DAY, 9., 2000)])
        self.assertIn('收盘已低于冻结支撑9.5', result['verdict'])
        self.assertIn('不下移', result['risk'])

    def test_raw_high_above_upper_close_below_upper_is_not_breakout(self):
        result = self.analyse(row(10.5, upper=10.8))
        self.assertEqual(result['verdict'], '盘中超过原上沿10.8，收盘未站稳。')

    def test_equal_high_to_upper_is_not_exceeded(self):
        result = self.analyse(row(10.5, upper=11.))
        self.assertNotIn('盘中超过', result['verdict'])

    def test_pressure_is_independent_of_frozen_upper(self):
        result = self.analyse(row(10.5, upper=12., pressure=10.6))
        self.assertIn('最近压力10.6', result['watch'])
        self.assertIn('上至最近压力10.6（0.95%）', result['risk'])
        self.assertIn('冻结支撑9（14.29%）', result['risk'])
        self.assertEqual(result['metrics']['frozen_upper'], 12.)
        self.assertNotIn('目标收益', result['risk'])

    def test_upper_alone_is_never_named_nearest_pressure(self):
        result = self.analyse()
        self.assertIn('原上沿11', result['watch'])
        self.assertNotIn('最近压力', result['watch'])

    def test_source_conflict_duplicates_prevents_numeric_conclusion(self):
        conflict = bar(DAY, 10.6, 2000)
        result = self.analyse(bars=self.bars + [conflict])
        self.assertEqual(result['metrics'], {})
        self.assertIn('同日行情记录冲突', result['limitation'])

    def test_identical_duplicate_has_no_impact(self):
        self.assertEqual(self.analyse(), self.analyse(bars=self.bars + [copy.deepcopy(self.bars[-1])]))

    def test_negative_volume_never_makes_ratio(self):
        self.bars[-1]['volume_shares'] = -1000
        result = self.analyse()
        self.assertIsNone(result['metrics']['volume_ratio'])
        self.assertIn('成交股数无效', result['limitation'])
        self.assertNotIn('放量', result['verdict'])

    def test_zero_prior_volume_never_makes_ratio(self):
        self.bars[0]['volume_shares'] = 0
        result = self.analyse()
        self.assertIsNone(result['metrics']['volume_ratio'])
        self.assertIn('含零值', result['limitation'])

    def test_plain_volume_field_does_not_guess_shares_or_lots(self):
        del self.bars[-1]['volume_shares']
        self.bars[-1]['volume'] = 10000
        result = self.analyse()
        self.assertIsNone(result['metrics']['volume_ratio'])
        self.assertIn('成交股数缺失', result['limitation'])

    def test_future_bars_have_no_effect(self):
        self.assertEqual(self.analyse(), self.analyse(bars=self.bars + [bar('2026-09-25', 999., -9)]))

    def test_stale_or_missing_bars_never_relabelled(self):
        for bars in ([], self.bars[:1], [self.bars[-1]]):
            with self.subTest(bars=bars):
                result = self.analyse(bars=bars)
                self.assertEqual(result['metrics'], {})
                self.assertTrue(result['limitation'])

    def test_close_outside_ohlc_range_rejected(self):
        self.bars[-1]['close'] = 12.
        result = self.analyse(row(12.))
        self.assertEqual(result['metrics'], {})
        self.assertIn('高低价', result['limitation'])

    def test_invalid_price_and_nan_rejected(self):
        for price in (False, -1, float('nan')):
            with self.subTest(price=price):
                self.bars[-1]['close'] = price
                result = self.analyse(row(price))
                self.assertEqual(result['metrics'], {})

    def test_row_close_conflict_not_rounded_away(self):
        self.assertEqual(self.analyse(row(10.5001))['metrics'], {})
        self.assertTrue(self.analyse(row(10.5000000001))['metrics'])

    def test_equal_ohlc_no_division_by_zero(self):
        result = self.analyse(row(10.), [bar('2026-09-23'), bar(DAY, 10., 1000, open=10., high=10., low=10.)])
        self.assertIn('收盘持平', result['verdict'])
        self.assertIn('同价', result['price_volume'])

    def test_conflicting_levels_are_not_arbitrarily_selected(self):
        entry = row()
        entry['levels'].append({'label': '冻结支撑', 'value': 8.})
        result = self.analyse(entry)
        self.assertIsNone(result['metrics']['support'])
        self.assertIn('关键价位记录冲突', result['limitation'])

    def test_no_frozen_level_uses_only_labelled_historical_reference(self):
        result = self.analyse({'price': 10.5})
        self.assertIn('仅作参照', result['watch'])
        self.assertIn('未给出有效冻结支撑', result['risk'])
        self.assertIsNone(result['metrics']['support'])

    def test_no_mutation_of_input_or_eligibility(self):
        entry, original_bars = row(), copy.deepcopy(self.bars)
        original_entry = copy.deepcopy(entry)
        result = self.analyse(entry)
        self.assertEqual(entry, original_entry)
        self.assertEqual(self.bars, original_bars)
        self.assertNotIn('admitted', result)

    def test_nearer_recent_high_not_skipped_for_distant_frozen_upper(self):
        result = self.analyse(row(10.5, upper=12.))
        self.assertIn('先看近期高点11，再看原上沿12', result['watch'])
        self.assertNotIn('最近压力', result['watch'])

    def test_nearer_upper_before_higher_recent_high(self):
        result = self.analyse(row(10.5, upper=10.8))
        self.assertIn('原上沿10.8能否站稳，再看近期高点11', result['watch'])

    def test_price_volume_includes_actual_high(self):
        self.assertIn('日内最高11', self.analyse()['price_volume'])

    def test_known_failed_structure_is_not_erased_by_price_analysis(self):
        entry = row()
        entry['failure_reasons'] = ['试盘后至少两日缩量承接', '已知冻结上沿下的毛空间上限不足']
        result = self.analyse(entry)
        self.assertIn('缩量承接条件未满足', result['risk'])
        self.assertIn('原空间条件未满足', result['risk'])


if __name__ == '__main__':
    unittest.main()
