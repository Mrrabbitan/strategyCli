from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from abnormal_move_monitor import (  # noqa: E402
    build_abnormal_10d_report,
    is_common_a_share,
    market_name,
    render_abnormal_10d_section,
    render_abnormal_10d_strip,
    score_candidate,
)


def history(code: str, closes=None):
    closes = closes or [10.0] * 10
    start = dt.date(2026, 8, 14)
    return {
        "name": code,
        "market": market_name(code),
        "rows": [
            {
                "date": (start + dt.timedelta(days=index)).isoformat(),
                "open": close * 0.98, "close": close,
                "high": close * 1.02, "low": close * 0.97,
                "volume": 1000 + index * 20,
            }
            for index, close in enumerate(closes)
        ],
    }


class AbnormalMoveMonitorTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "abnormal_10d": {
                "warning_threshold_pct": 80.0,
                "trigger_threshold_pct": 100.0,
                "minimum_universe_coverage_ratio": 0.98,
                "minimum_history_coverage_ratio": 0.98,
                "quote_price_tolerance_pct": 0.3,
                "gain_tolerance_pct_points": 0.8,
                "history_workers": 2,
            }
        }

    def write_cache(self, path, histories, expected=None):
        path.write_text(json.dumps({
            "version": 1, "expected_count": expected or len(histories), "histories": histories,
        }), encoding="utf-8")

    def build(self, market, histories, expected=None, quote_prices=None, verification=None, slot="10_30"):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        cache_path = Path(temporary.name) / "history.json"
        self.write_cache(cache_path, histories, expected)
        quote_prices = quote_prices or {code: row["price"] for code, row in market.items()}
        verification = verification or {code: histories[code]["rows"] for code in histories}
        return build_abnormal_10d_report(
            report_date=dt.date(2026, 8, 28), slot=slot,
            market_snapshot=market, expected_count=expected or len(market), config=self.config,
            theme_counts={"测试行业": 3}, industry_by_code={code: "测试行业" for code in market},
            risk_title_fetcher=lambda code: [],
            history_fetcher=lambda code, count: histories.get(code, {}).get("rows", []),
            verification_history_fetcher=lambda code, count: verification.get(code, []),
            quote_fetcher=lambda codes: {
                code: {"price": quote_prices[code], "timestamp": "20260828103000"}
                for code in codes if code in quote_prices
            },
            industry_fetcher=None, cache_path=cache_path, persist_cache=False,
        )

    def test_common_share_scope_includes_all_boards(self):
        for code in ("600000", "000001", "300001", "688001", "920002"):
            self.assertTrue(is_common_a_share(code), code)
        for code in ("510300", "200001", "900901", "123001"):
            self.assertFalse(is_common_a_share(code), code)

    def test_threshold_boundaries_and_trigger_first_sort(self):
        gains = {
            "600001": 79.99, "600002": 80.0, "300001": 99.99,
            "688001": 100.0, "920002": 120.0,
        }
        histories = {code: history(code) for code in gains}
        market = {
            code: {
                "name": code, "price": 10 * (1 + gain / 100), "open": 10,
                "high": 10 * (1 + gain / 100) * 1.01,
                "low": 10 * (1 + gain / 100) * 0.98, "volume": 1800,
            }
            for code, gain in gains.items()
        }
        report = self.build(market, histories)
        monitor = report["abnormal_10d_monitor"]
        self.assertTrue(report["abnormal_10d_data_quality"]["actionable"])
        self.assertEqual(monitor["trigger_count"], 2)
        self.assertEqual(monitor["warning_count"], 2)
        self.assertEqual([row["code"] for row in monitor["items"]], ["920002", "688001", "300001", "600002"])
        for row in monitor["items"]:
            self.assertGreaterEqual(row["continuation_score"], 0)
            self.assertLessEqual(row["continuation_score"], 100)
            self.assertGreaterEqual(row["pullback_risk_score"], 0)
            self.assertLessEqual(row["pullback_risk_score"], 100)

    def test_preopen_uses_previous_close_and_eleventh_bar_anchor(self):
        code = "600001"
        closes = [10.0] + [12.0] * 9 + [18.0]
        histories = {code: history(code, closes)}
        report = self.build({}, histories, expected=1, quote_prices={code: 99.0}, slot="09_00")
        item = report["abnormal_10d_monitor"]["items"][0]
        self.assertAlmostEqual(item["gain_10d_pct"], 80.0, places=6)
        self.assertEqual(item["current_price"], 18.0)
        self.assertEqual(item["anchor_price"], 10.0)

    def test_dual_source_difference_blocks_partial_output(self):
        code = "600001"
        histories = {code: history(code)}
        market = {code: {"name": "测试", "price": 20.0, "open": 19, "high": 21, "low": 18, "volume": 1800}}
        report = self.build(market, histories, quote_prices={code: 19.0})
        self.assertFalse(report["abnormal_10d_data_quality"]["actionable"])
        self.assertFalse(report["abnormal_10d_monitor"]["items"])
        self.assertIn("双源差异超限", report["abnormal_10d_monitor"]["unverified_items"][0]["reason"])

    def test_history_coverage_failure_and_new_listing_are_explicit(self):
        code = "300001"
        histories = {code: history(code, [10.0] * 5)}
        market = {code: {"name": "ST测试", "price": 19.0}}
        report = self.build(market, histories)
        quality = report["abnormal_10d_data_quality"]
        self.assertFalse(quality["actionable"])
        self.assertEqual(quality["history_coverage_ratio"], 0.0)
        self.assertEqual(report["abnormal_10d_monitor"]["unverified_items"][0]["reason"], "不足11个可核验交易日")

    def test_missing_factor_is_renormalized_not_scored_as_zero(self):
        prior = history("600001")["rows"]
        score = score_candidate(
            {"price": 19.0, "open": 18.5, "high": 19.2, "low": 18.0, "volume": 1500},
            prior, 90.0, None, None,
        )
        self.assertFalse(score["score_complete"])
        self.assertIsNotNone(score["continuation_score"])
        self.assertIsNotNone(score["pullback_risk_score"])

    def test_rendering_has_full_table_and_dashboard_summary(self):
        code = "600001"
        histories = {code: history(code)}
        market = {code: {"name": "测试", "price": 19.0, "open": 18, "high": 19.2, "low": 17.8, "volume": 1800}}
        report = self.build(market, histories)
        detail = render_abnormal_10d_section(report)
        strip = render_abnormal_10d_strip(report)
        self.assertIn("未来5日规则评估", detail)
        self.assertIn("测试", detail)
        self.assertIn("触发0只 / 预警1只", strip)


if __name__ == "__main__":
    unittest.main()
