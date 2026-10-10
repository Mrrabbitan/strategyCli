"""Artificial bars and explicit synthetic calendars only; no network or accounts."""

import copy
import importlib.util
import json
import subprocess
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / 'docs/playbooks/a-share-catch-up-dragon/scripts/analyze.py'
SPEC = importlib.util.spec_from_file_location('catch_up_dragon_analyze', SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
analyze = MODULE.analyze


def sample(count=150, slope=0.0):
    # Explicit synthetic sessions: these dates make no real exchange-calendar claim.
    dates = [(date(2024, 1, 1) + timedelta(days=index)).isoformat() for index in range(count)]
    bars = []
    for index, day in enumerate(dates):
        close = 10 + slope * index
        bars.append({'date': day, 'open': close, 'high': close + 0.1,
                     'low': close - 0.1, 'close': close, 'volume': 1000.0})
    return {'signal_date': dates[-1], 'adjustment': 'qfq', 'source': 'synthetic-test',
            'volume_unit': 'shares', 'trading_days': dates,
            't0': dates[20] if count > 20 else dates[0], 'bars': bars}


class CatchUpDragonTests(unittest.TestCase):
    def test_constant_price_ema_and_macd(self):
        result = analyze(sample())
        self.assertEqual(result['status'], 'verified')
        for field in ('ema12', 'ema26'):
            self.assertAlmostEqual(result['macd'][field], 10)
        for field in ('dif', 'dea', 'histogram'):
            self.assertAlmostEqual(result['macd'][field], 0)
        self.assertEqual(result['macd']['momentum_label'], 'unsupported')
        self.assertFalse(result['macd']['enhancement_evidence'])
        self.assertEqual(result['metrics']['return_5d']['value_pct'], 0)
        self.assertEqual(result['metrics']['ma20']['value'], 10)

    def test_first_close_seed_and_histogram_multiplier(self):
        payload = sample(2, 1)
        macd = analyze(payload)['macd']
        expected12 = 10 + 2 / 13
        expected26 = 10 + 2 / 27
        dif = expected12 - expected26
        self.assertAlmostEqual(macd['ema12'], expected12)
        self.assertAlmostEqual(macd['ema26'], expected26)
        self.assertAlmostEqual(macd['dif'], dif)
        self.assertAlmostEqual(macd['dea'], dif * .2)
        self.assertAlmostEqual(macd['histogram'], 2 * dif * .8)

    def test_trend_returns_and_warmup(self):
        payload = sample(150, .02)
        result = analyze(payload)
        bars = payload['bars']
        self.assertEqual(result['macd']['momentum_label'], 'supportive')
        self.assertGreater(result['macd']['dif'], 0)
        self.assertAlmostEqual(result['metrics']['return_5d']['value_pct'],
                               (bars[-1]['close'] / bars[-6]['close'] - 1) * 100)
        self.assertAlmostEqual(result['metrics']['return_20d']['value_pct'],
                               (bars[-1]['close'] / bars[-21]['close'] - 1) * 100)
        self.assertAlmostEqual(result['metrics']['ma20']['value'],
                               sum(bar['close'] for bar in bars[-20:]) / 20)

    def test_short_history_is_diagnostic_never_verified(self):
        for count in (1, 26, 129):
            result = analyze(sample(count, .02))
            self.assertEqual(result['status'], 'insufficient_history')
            self.assertEqual(result['macd']['status'], 'insufficient_history')
            self.assertEqual(result['macd']['momentum_label'], 'insufficient_history')
            self.assertIn('dif', result['macd'])
            self.assertFalse(result['macd']['enhancement_evidence'])
        self.assertEqual(analyze(sample(130))['macd']['status'], 'verified')

    def test_three_increasing_histograms_are_separate_enhancement(self):
        payload = sample()
        for index, bar in enumerate(payload['bars']):
            close = 10 + .0001 * index ** 3
            bar.update(open=close, high=close + .1, low=close - .1, close=close)
        macd = analyze(payload)['macd']
        self.assertEqual(macd['momentum_label'], 'supportive')
        self.assertTrue(macd['three_histograms_strictly_increasing'])
        self.assertTrue(macd['enhancement_evidence'])

    def test_negative_dif_does_not_support_momentum(self):
        macd = analyze(sample(150, -.02))['macd']
        self.assertLess(macd['dif'], 0)
        self.assertEqual(macd['momentum_label'], 'unsupported')
        self.assertFalse(macd['enhancement_evidence'])

    def test_future_rows_do_not_change_historical_indicators(self):
        payload = sample(140, .01)
        baseline = analyze(payload)
        augmented = copy.deepcopy(payload)
        future = (date.fromisoformat(payload['signal_date']) + timedelta(days=1)).isoformat()
        augmented['trading_days'].append(future)
        augmented['bars'].append({'date': future, 'open': 500, 'high': 600,
                                  'low': 400, 'close': 550, 'volume': 999999})
        augmented['notes'] = 'Fingerprint must include arbitrary caller metadata.'
        result = analyze(augmented)
        self.assertEqual(result['metrics'], baseline['metrics'])
        self.assertEqual(result['macd'], baseline['macd'])
        self.assertEqual(result['t0_comparison'], baseline['t0_comparison'])
        self.assertEqual(result['truncated_future']['bars'], [future])
        self.assertNotEqual(result['input_fingerprint'], baseline['input_fingerprint'])

    def test_duplicate_and_unsorted_bar_dates_rejected(self):
        for mode in ('duplicate', 'unsorted'):
            payload = sample()
            if mode == 'duplicate':
                payload['bars'][50]['date'] = payload['bars'][49]['date']
            else:
                payload['bars'][49], payload['bars'][50] = payload['bars'][50], payload['bars'][49]
            with self.assertRaisesRegex(ValueError, 'unique and strictly ascending'):
                analyze(payload)

    def test_duplicate_calendar_and_unknown_bar_dates_rejected(self):
        payload = sample()
        payload['trading_days'][50] = payload['trading_days'][49]
        with self.assertRaisesRegex(ValueError, 'unique and strictly ascending'):
            analyze(payload)
        payload = sample()
        payload['trading_days'].pop(50)
        with self.assertRaisesRegex(ValueError, 'absent from trading_days'):
            analyze(payload)

    def test_missing_target_and_internal_date_rejected(self):
        for index in (-1, 120):
            payload = sample()
            day = payload['bars'].pop(index)['date']
            with self.assertRaisesRegex(ValueError, day):
                analyze(payload)

    def test_explicit_suspension_does_not_extend_windows(self):
        payload = sample()
        removed = payload['bars'].pop(-3)
        index = payload['trading_days'].index(removed['date'])
        payload['trading_days'][index] = {'date': removed['date'], 'status': 'suspended'}
        result = analyze(payload)
        self.assertEqual(result['suspended_dates'], [removed['date']])
        for metric in ('return_5d', 'return_20d', 'ma20', 'volume_ratio_previous5', 'breakout'):
            self.assertEqual(result['metrics'][metric]['status'], 'insufficient_window')
        self.assertIsNone(result['metrics']['ma20']['value'])
        self.assertIsNone(result['metrics']['breakout']['confirmation_line_10d'])
        self.assertEqual(result['macd']['completed_bars'], 149)

    def test_bars_on_suspended_day_rejected(self):
        payload = sample()
        day = payload['trading_days'][100]
        payload['trading_days'][100] = {'date': day, 'status': 'suspended'}
        with self.assertRaisesRegex(ValueError, 'suspended dates'):
            analyze(payload)

    def test_nan_infinity_zero_price_and_negative_volume_rejected(self):
        for field, value in (('close', float('nan')), ('volume', float('inf')),
                             ('open', 0), ('low', -1), ('volume', -1)):
            payload = sample()
            payload['bars'][70][field] = value
            with self.assertRaises(ValueError):
                analyze(payload)

    def test_inconsistent_ohlc_and_incomplete_daily_bar_rejected(self):
        payload = sample()
        payload['bars'][-1]['high'] = 9
        with self.assertRaisesRegex(ValueError, 'OHLC'):
            analyze(payload)
        payload = sample()
        payload['bars'][-1]['completed'] = False
        with self.assertRaisesRegex(ValueError, 'completed'):
            analyze(payload)

    def test_units_normalized_and_unknown_rejected(self):
        payload = sample()
        shares = analyze(payload)
        payload['volume_unit'] = 'lots_100'
        for bar in payload['bars']:
            bar['volume'] /= 100
        lots = analyze(payload)
        self.assertEqual(lots['volume_unit'], 'shares')
        self.assertEqual(lots['metrics'], shares['metrics'])
        for unit in ('lots', '', None):
            payload['volume_unit'] = unit
            with self.assertRaisesRegex(ValueError, 'volume_unit'):
                analyze(payload)

    def test_volume_baseline_excludes_signal_date(self):
        payload = sample()
        payload['bars'][-1]['volume'] = 5000
        ratio = analyze(payload)['metrics']['volume_ratio_previous5']
        self.assertEqual(ratio['previous_mean_volume_shares'], 1000)
        self.assertEqual(ratio['value'], 5)
        payload['bars'][-6:-1] = [dict(bar, volume=0) for bar in payload['bars'][-6:-1]]
        ratio = analyze(payload)['metrics']['volume_ratio_previous5']
        self.assertEqual(ratio['status'], 'undefined_zero_baseline')
        self.assertIsNone(ratio['value'])

    def test_signal_day_high_never_enters_frozen_confirmation_line(self):
        payload = sample()
        payload['bars'][-1].update(open=10.5, high=100, low=10.2, close=10.4)
        breakout = analyze(payload)['metrics']['breakout']
        self.assertEqual(breakout['confirmation_line_10d'], 10.1)
        self.assertTrue(breakout['close_above_confirmation_line'])
        self.assertEqual(breakout['end_date'], payload['bars'][-2]['date'])

    def test_short_windows_do_not_backfill(self):
        result = analyze(sample(5))
        self.assertEqual(result['metrics']['return_5d']['status'], 'insufficient_window')
        self.assertIsNone(result['metrics']['return_5d']['value_pct'])
        self.assertIsNone(result['metrics']['breakout']['confirmation_line_10d'])

    def test_t0_common_dates_only_and_exact_gap(self):
        payload = sample(150, .01)
        payload['leader_bars'] = [dict(bar, open=bar['close'] * 2, high=bar['high'] * 2,
                                       low=bar['low'] * 2, close=bar['close'] * 2)
                                  for bar in payload['bars']]
        comparison = analyze(payload)['t0_comparison']
        self.assertEqual(comparison['status'], 'comparable')
        self.assertEqual(comparison['leader_minus_candidate_pct'], 0)
        payload['leader_bars'] = payload['leader_bars'][30:]
        comparison = analyze(payload)['t0_comparison']
        self.assertEqual(comparison['status'], 'incomparable_dates')
        self.assertIsNone(comparison['leader_return_pct'])
        self.assertIsNone(comparison['leader_minus_candidate_pct'])

    def test_leader_missing_signal_date_never_uses_previous_close(self):
        payload = sample()
        payload['leader_bars'] = copy.deepcopy(payload['bars'][:-1])
        comparison = analyze(payload)['t0_comparison']
        self.assertEqual(comparison['leader_missing_dates'], [payload['signal_date']])
        self.assertIsNone(comparison['leader_minus_candidate_pct'])

    def test_adjustment_and_source_contract(self):
        for field, value in (('adjustment', 'hfq'), ('source', ''), ('source', None)):
            payload = sample()
            payload[field] = value
            with self.assertRaises(ValueError):
                analyze(payload)
        payload = sample()
        payload['leader_bars'] = copy.deepcopy(payload['bars'])
        payload['leader_adjustment'] = 'hfq'
        with self.assertRaisesRegex(ValueError, 'leader_adjustment'):
            analyze(payload)

    def test_input_fingerprint_covers_all_input(self):
        payload = sample()
        baseline = analyze(payload)['input_fingerprint']
        payload['metadata'] = {'a': 1}
        changed = analyze(payload)['input_fingerprint']
        self.assertNotEqual(baseline, changed)
        reordered = dict(reversed(list(payload.items())))
        self.assertEqual(changed, analyze(reordered)['input_fingerprint'])

    def test_no_eligibility_win_rate_or_limit_inference(self):
        serialized = json.dumps(analyze(sample())).lower()
        for forbidden in ('buy_eligible', 'win_rate', 'limit_up', 'position_size'):
            self.assertNotIn(forbidden, serialized)

    def test_cli_stdin_and_nonfinite_rejection(self):
        result = subprocess.run([sys.executable, str(SCRIPT), '-'],
                                input=json.dumps(sample(130)), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'verified')
        result = subprocess.run([sys.executable, str(SCRIPT), '-'],
                                input='{"close": NaN}', text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)['status'], 'rejected')


if __name__ == '__main__':
    unittest.main()
