"""Artificial, offline examples; no real securities or network calls."""

import copy
from datetime import date, timedelta
import unittest

from highmark_rules import calculate, EFFECTIVE_FROM, RULE_VERSION


def sample(count=40, growth=0.0, index_growth=0.0):
    start = date(2026, 8, 3)
    days = []
    cursor = start
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    stock, index = [], []
    price, points = 10.0, 1000.0
    for day in days:
        old_price, old_points = price, points
        price *= 1.0 + growth
        points *= 1.0 + index_growth
        stock.append({"date": day, "close": price, "prev_close": old_price})
        index.append({"date": day, "close": points, "prev_close": old_points})
    return stock, index, days[-1]


class HighmarkRulesTest(unittest.TestCase):
    def test_compound_interval_not_sum_of_daily_moves(self):
        stock, index, cutoff = sample(growth=0.10)
        result = calculate(stock, index, cutoff)
        metric = result["metrics"]["3"]
        self.assertAlmostEqual(metric["deviation_pp"], 33.1)
        self.assertAlmostEqual(metric["headroom_pp"], -13.1)
        self.assertEqual(metric["length"], 3)
        self.assertTrue(result["reference_only"])
        self.assertEqual(result["rule_version"], RULE_VERSION)

    def test_benchmark_is_compounded_over_identical_interval(self):
        stock, index, cutoff = sample(growth=0.02, index_growth=0.01)
        metric = calculate(stock, index, cutoff)["metrics"]["3"]
        self.assertAlmostEqual(metric["deviation_pp"], 100 * (1.02 ** 3 - 1.01 ** 3))
        self.assertNotAlmostEqual(metric["deviation_pp"], 3.0)

    def test_shorter_window_can_be_riskier_than_exact_n(self):
        stock, index, cutoff = sample()
        stock[-3]["close"] = 8.0
        stock[-2]["close"] = 8.8
        stock[-2]["prev_close"] = 8.0
        stock[-1]["close"] = 9.68
        stock[-1]["prev_close"] = 8.8
        # The 3-session window is negative, but the last two sessions +21%.
        metric = calculate(stock, index, cutoff)["metrics"]["3"]
        self.assertEqual(metric["length"], 2)
        self.assertAlmostEqual(metric["deviation_pp"], 21.0)
        self.assertAlmostEqual(metric["exactN"]["deviation_pp"], -3.2)

    def test_next_day_rolls_oldest_session_out_before_solving(self):
        stock, index, cutoff = sample()
        stock[-3]["close"] = 20.0
        stock[-2]["close"] = 20.0
        stock[-2]["prev_close"] = 20.0
        stock[-1]["close"] = 20.0
        stock[-1]["prev_close"] = 20.0
        metric = calculate(stock, index, cutoff)["metrics"]["3"]
        scenario = metric["next_session_scenario"]
        self.assertAlmostEqual(scenario["day_pct"], 20.0)
        self.assertFalse(scenario["is_forecast"])
        self.assertFalse(scenario["is_price_limit"])
        self.assertNotEqual(scenario["base_date"], metric["max_base_date"])

    def test_next_scenario_is_price_solution_not_headroom_points(self):
        stock, index, cutoff = sample(growth=0.05)
        metric = calculate(stock, index, cutoff)["metrics"]["3"]
        scenario = metric["next_session_scenario"]
        self.assertAlmostEqual(scenario["day_pct"], 100 * (1.2 / 1.05 ** 2 - 1))
        self.assertNotAlmostEqual(scenario["day_pct"], metric["headroom_pp"])

    def test_normal_reset_does_not_erase_severe_window(self):
        stock, index, cutoff = sample(growth=0.03)
        reset = stock[-1]["date"]
        result = calculate(stock, index, cutoff, normal_reset=reset, reset_verified=True)
        self.assertEqual(result["metrics"]["3"]["length"], 1)
        self.assertEqual(result["metrics"]["10"]["length"], 10)
        self.assertEqual(result["metrics"]["30"]["length"], 30)
        self.assertEqual(result["metrics"]["3"]["rolling_reference"]["maximum"]["length"], 3)

    def test_severe_reset_separate_from_normal(self):
        stock, index, cutoff = sample(growth=0.03)
        reset = stock[-1]["date"]
        result = calculate(stock, index, cutoff, severe_reset=reset, reset_verified=True)
        self.assertEqual(result["metrics"]["3"]["length"], 3)
        self.assertEqual(result["metrics"]["10"]["length"], 1)
        self.assertEqual(result["metrics"]["30"]["length"], 1)

    def test_normal_verified_does_not_attest_severe_reset_absence(self):
        stock, index, cutoff = sample(growth=0.03)
        result = calculate(stock, index, cutoff, normal_reset=cutoff,
                           severe_reset=cutoff, normal_reset_verified=True,
                           severe_reset_verified=False)
        self.assertEqual(result["metrics"]["3"]["length"], 1)
        self.assertEqual(result["metrics"]["10"]["length"], 10)
        self.assertEqual(result["metrics"]["30"]["length"], 30)
        self.assertTrue(result["metrics"]["3"]["reset_verified"])
        self.assertFalse(result["metrics"]["10"]["reset_verified"])
        self.assertFalse(result["reset_verified"])
        self.assertNotIn("exchange_reset_history_not_verified", result["metrics"]["3"]["issues"])
        self.assertIn("exchange_reset_history_not_verified", result["metrics"]["10"]["issues"])

    def test_independent_flags_override_combined_backwards_compatible_flag(self):
        stock, index, cutoff = sample(growth=0.03)
        result = calculate(stock, index, cutoff, normal_reset=cutoff,
                           severe_reset=cutoff, reset_verified=True,
                           normal_reset_verified=False)
        self.assertEqual(result["metrics"]["3"]["length"], 3)
        self.assertEqual(result["metrics"]["10"]["length"], 1)
        self.assertFalse(result["normal_reset_verified"])
        self.assertTrue(result["severe_reset_verified"])

    def test_unverified_reset_is_not_silently_applied(self):
        stock, index, cutoff = sample(growth=0.03)
        result = calculate(stock, index, cutoff, normal_reset=cutoff)
        self.assertEqual(result["metrics"]["3"]["length"], 3)
        self.assertIsNone(result["metrics"]["3"]["reset_applied"])
        self.assertIn("exchange_reset_history_not_verified", result["issues"])

    def test_missing_date_does_not_join_sessions_over_gap(self):
        stock, index, cutoff = sample(growth=0.03)
        del stock[-5]
        result = calculate(stock, index, cutoff)
        self.assertIsNotNone(result["metrics"]["3"]["deviation_pp"])
        self.assertIsNone(result["metrics"]["10"]["deviation_pp"])
        self.assertIsNone(result["metrics"]["30"]["deviation_pp"])
        self.assertFalse(result["complete_history"])
        self.assertEqual(result["status"], "partial")

    def test_suspension_and_unlimited_sessions_are_not_assumed_ordinary(self):
        stock, index, cutoff = sample()
        stock[-2]["suspended"] = True
        self.assertIsNone(calculate(stock, index, cutoff)["metrics"]["3"]["deviation_pp"])
        stock[-2].pop("suspended")
        stock[-2]["no_price_limit"] = True
        self.assertIsNone(calculate(stock, index, cutoff)["metrics"]["3"]["deviation_pp"])

    def test_future_bars_cannot_change_historical_value(self):
        stock, index, cutoff = sample(growth=0.01)
        baseline = calculate(stock, index, cutoff)
        future_date = (date.fromisoformat(cutoff) + timedelta(days=1)).isoformat()
        stock.append({"date": future_date, "close": 1000000})
        index.append({"date": future_date, "close": 0.01})
        result = calculate(stock, index, cutoff)
        self.assertEqual(result["metrics"], baseline["metrics"])
        self.assertEqual(result["excluded_future_bars"], {"stock": 1, "index": 1})

    def test_stale_or_duplicate_source_cannot_pass(self):
        stock, index, cutoff = sample()
        stale = stock[:-1]
        self.assertEqual(calculate(stale, index, cutoff)["status"], "unavailable")
        stock.append(copy.deepcopy(stock[-1]))
        self.assertEqual(calculate(stock, index, cutoff)["status"], "unavailable")

    def test_corporate_action_reference_change_is_not_ignored(self):
        stock, index, cutoff = sample()
        stock[-2]["prev_close"] = 9.8
        result = calculate(stock, index, cutoff, reset_verified=True)
        self.assertTrue(result["reference_only"])
        self.assertEqual(result["calculation_kind"], "rolling_research_estimate")
        self.assertTrue(any(issue.startswith("reference_close_changed:") for issue in result["issues"]))
        self.assertIsNone(result["metrics"]["3"]["deviation_pp"])
        self.assertIsNone(result["metrics"]["3"]["headroom_pp"])
        self.assertIsNone(result["metrics"]["3"]["next_session_scenario"])

    def test_corporate_action_blocks_only_windows_containing_it(self):
        stock, index, cutoff = sample(growth=0.01)
        stock[-6]["prev_close"] *= 0.8
        result = calculate(stock, index, cutoff)
        self.assertIsNotNone(result["metrics"]["3"]["deviation_pp"])
        self.assertIsNone(result["metrics"]["10"]["deviation_pp"])
        self.assertIsNone(result["metrics"]["30"]["deviation_pp"])
        self.assertEqual(result["status"], "partial")

    def test_adjusted_ratio_change_is_explicit_uncertainty(self):
        stock, index, cutoff = sample()
        for row in stock:
            row["qfq_close"] = row["close"]
        stock[-1]["qfq_close"] *= 0.9
        issues = calculate(stock, index, cutoff)["metrics"]["3"]["issues"]
        self.assertTrue(any(issue.startswith("adjusted_ratio_changed:") for issue in issues))

    def test_adjusted_close_rounding_is_not_misread_as_a_corporate_action(self):
        stock, index, cutoff = sample(growth=0.013)
        for row in stock:
            row["qfq_close"] = round(row["close"] * 0.92345, 2)
        result = calculate(stock, index, cutoff)
        self.assertIsNotNone(result["metrics"]["30"]["deviation_pp"])
        self.assertFalse(any("adjusted_ratio_changed:" in issue for issue in result["issues"]))

    def test_less_than_31_observations_is_not_complete(self):
        stock, index, cutoff = sample(count=20)
        result = calculate(stock, index, cutoff)
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["complete_history"])
        self.assertIsNone(result["metrics"]["30"]["deviation_pp"])

    def test_old_rule_period_is_not_backfilled_with_2026_formula(self):
        stock = [{"date": "2026-07-03", "close": 10}]
        index = [{"date": "2026-07-03", "close": 1000}]
        result = calculate(stock, index, "2026-07-03")
        self.assertIn("rule_not_effective_at_cutoff", result["issues"])
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(EFFECTIVE_FROM, "2026-07-06")

    def test_window_straddling_effective_date_is_unavailable(self):
        stock, index, cutoff = sample()
        # Shift the artificial history to June/July, ending just after the rule
        # changed. The new formula must not be retroactively applied to June.
        for left, right in zip(stock, index):
            day = (date.fromisoformat(left["date"]) - timedelta(days=60)).isoformat()
            left["date"] = right["date"] = day
        result = calculate(stock, index, stock[-1]["date"])
        self.assertIn("window_crosses_rule_effective_date", result["metrics"]["30"]["issues"])

    def test_threshold_equality_is_inclusive_without_binary_float_noise(self):
        stock, index, cutoff = sample()
        stock[-1]["close"] = 12.0
        result = calculate(stock, index, cutoff)["metrics"]["3"]
        self.assertEqual(result["headroom_pp"], 0.0)
        self.assertTrue(result["threshold_reached_estimate"])

    def test_verified_complete_inputs_remain_estimates_not_official(self):
        stock, index, cutoff = sample(growth=0.01)
        result = calculate(stock, index, cutoff, reset_verified=True)
        self.assertEqual(result["calculation_kind"], "reconciled_public_data_estimate")
        self.assertTrue(result["reference_only"])
        self.assertEqual(result["status"], "complete")


if __name__ == "__main__":
    unittest.main()
