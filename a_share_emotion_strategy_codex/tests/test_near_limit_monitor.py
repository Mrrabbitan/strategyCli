from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from near_limit_monitor import (  # noqa: E402
    build_emotion_leader_report,
    build_preopen_near_limit_report,
    build_near_limit_report,
    limit_up_price,
    prefilter_emotion_leaders,
    prefilter_near_limit,
)

from focus_fixtures import focus_context


class NearLimitMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.day = dt.date(2026, 8, 25)
        self.market = {
            "600001": {
                "name": "测试股份", "price": 10.70, "prev_close": 10.00, "open": 10.20,
                "high": 10.85, "low": 10.10, "pct": 7.0, "turnover": 8.0,
                "amount_cny": 200_000_000, "ticktime": "10:30:00",
            },
        }
        self.quote = {
            "name": "测试股份", "code": "600001", "price": 10.70, "prev_close": 10.00,
            "open": 10.20, "high": 10.85, "low": 10.10, "pct": 7.0,
            "amount_cny": 200_000_000, "volume_lots": 190_000,
            "buy_lots": 120_000, "sell_lots": 70_000, "turnover": 8.0,
            "timestamp": "20260825103000",
        }
        self.near_context = focus_context([], near=[{"c": "600001", "n": "测试股份", "hybk": "算力", "prior_boards": 0, "amount": 200_000_000}], peers=True)
        self.evidence = {
            "code": "600001", "title": "测试股份签署算力项目协议", "published_at": "2026-08-25",
            "url": "https://www.sse.com.cn/disclosure/example", "event_type": "项目合同",
            "theme": "算力", "source_tier": "official", "source_type": "exchange",
        }

    def test_limit_price_rounds_to_cent(self) -> None:
        self.assertEqual(limit_up_price(10.05), 11.06)

    def test_main_board_three_percent_and_prospective_board_filter(self) -> None:
        market = dict(self.market)
        market["300001"] = dict(self.market["600001"], name="创业板测试")
        market["600002"] = dict(self.market["600001"], name="*ST测试")
        market["600003"] = dict(self.market["600001"], price=10.60)
        rows, sealed = prefilter_near_limit(market, {"600001": {"lbc": 1}, "600003": {"lbc": 2}})
        self.assertEqual([row["c"] for row in rows], ["600001"])
        self.assertEqual(rows[0]["prospective_board"], 2)
        self.assertEqual(rows[0]["signal_type"], "首次临板")
        self.assertFalse(sealed)

    def test_reseal_is_grouped_separately(self) -> None:
        market = {"600001": dict(self.market["600001"], high=11.0, price=10.80)}
        rows, _ = prefilter_near_limit(market, {})
        self.assertEqual(rows[0]["signal_type"], "炸板待回封")

    def test_only_verified_candidates_enter_top_five_and_vacancies_remain(self) -> None:
        raw, sealed = prefilter_near_limit(self.market, {})
        raw[0].update({"hybk": "算力", "listing_age_verified": True, "theme_up3_count": 0, "theme_breadth_ratio": 0.0})
        with tempfile.TemporaryDirectory() as tmp:
            result = build_near_limit_report(
                report_date=self.day,
                slot="10_30",
                risk_level="中",
                candidates=raw,
                sealed_candidates=sealed,
                quotes={"600001": self.quote},
                sina_quotes={"600001": dict(self.quote)},
                current_pool_map={},
                theme_counts={"算力": 2},
                evidence_map={"600001": [self.evidence]},
                evidence_errors=[],
                announcement_risks={},
                market_snapshot=self.market,
                universe_quality={"universe_count": 5500, "expected_count": 5500, "coverage_ratio": 1.0, "errors": []},
                persist_state=True,
                state_path=Path(tmp) / "near.json",
                hot_sector_context=self.near_context,
            )
            self.assertEqual(result["emotion_near_limit"]["displayed_count"], 1)
            self.assertEqual(result["emotion_near_limit"]["vacant_count"], 4)
            self.assertEqual(result["emotion_near_limit"]["items"][0]["prospective_board"], 1)
            self.assertTrue((Path(tmp) / "near.json").exists())

    def test_missing_catalyst_never_backfills(self) -> None:
        raw, sealed = prefilter_near_limit(self.market, {})
        raw[0].update({"hybk": "算力", "listing_age_verified": True})
        with tempfile.TemporaryDirectory() as tmp:
            result = build_near_limit_report(
                self.day, "10_30", "中", raw, sealed, {"600001": self.quote},
                {"600001": dict(self.quote)}, {}, {"算力": 2}, {}, [], {}, self.market,
                {"universe_count": 5500, "expected_count": 5500, "coverage_ratio": 1.0, "errors": []},
                False, Path(tmp) / "near.json",
                hot_sector_context=self.near_context,
            )
            self.assertFalse(result["emotion_near_limit"]["items"])
            self.assertEqual(result["emotion_near_limit"]["vacant_count"], 5)

    def test_final_quote_at_limit_moves_to_outcomes_and_does_not_fill_top_five(self) -> None:
        raw, sealed = prefilter_near_limit(self.market, {})
        raw[0].update({"hybk": "算力", "listing_age_verified": True})
        limit_quote = dict(self.quote, price=11.0, high=11.0, pct=10.0)
        result = build_near_limit_report(
            self.day, "10_30", "中", raw, sealed, {"600001": limit_quote},
            {"600001": dict(limit_quote)}, {}, {"算力": 2}, {"600001": [self.evidence]}, [], {},
            {"600001": dict(self.market["600001"], price=11.0, high=11.0)},
            {"universe_count": 5500, "expected_count": 5500, "coverage_ratio": 1.0, "errors": []}, False,
            hot_sector_context=self.near_context,
        )
        self.assertFalse(result["emotion_near_limit"]["items"])
        self.assertEqual(result["emotion_near_limit"]["sealed"][0]["status"], "复核时已封板")
        self.assertEqual(result["emotion_near_limit_outcomes"][0]["code"], "600001")

    def test_medium_high_market_never_returns_trial_action(self) -> None:
        raw, sealed = prefilter_near_limit(self.market, {})
        raw[0].update({"hybk": "算力", "listing_age_verified": True})
        result = build_near_limit_report(
            self.day, "10_30", "中高", raw, sealed, {"600001": self.quote},
            {"600001": dict(self.quote)}, {}, {"算力": 2}, {"600001": [self.evidence]}, [], {}, self.market,
            {"universe_count": 5500, "expected_count": 5500, "coverage_ratio": 1.0, "errors": []}, False,
            hot_sector_context=self.near_context,
        )
        self.assertEqual(result["emotion_near_limit"]["items"][0]["recommendation"]["action"], "等待承接")

    def test_preopen_uses_only_previous_close_watchlist(self) -> None:
        previous = {
            "emotion_near_limit": {
                "items": [{"code": "600999", "signal_type": "首次临板"}],
                "next_session_watchlist": [{"code": "600001", "name": "测试股份", "signal_type": "首次临板"}],
            }
        }
        previous["emotion_near_limit"]["hot_sector_focus"] = self.near_context
        result = build_preopen_near_limit_report(previous, expected_as_of="2026-08-25")
        self.assertEqual(result["emotion_near_limit"]["items"][0]["code"], "600001")
        self.assertEqual(result["emotion_near_limit"]["items"][0]["signal_type"], "盘前观察")
        self.assertFalse(result["emotion_near_limit_data_quality"]["actionable"])

    def test_preopen_without_previous_close_is_not_actionable(self) -> None:
        result = build_preopen_near_limit_report(None)
        self.assertFalse(result["emotion_near_limit_data_quality"]["actionable"])
        self.assertEqual(result["emotion_near_limit"]["vacant_count"], 5)

    def test_low_universe_coverage_disables_candidates(self) -> None:
        raw, sealed = prefilter_near_limit(self.market, {})
        result = build_near_limit_report(
            self.day, "10_30", "中", raw, sealed, {}, {}, {}, {}, {}, [], {}, self.market,
            {"universe_count": 5000, "expected_count": 5500, "coverage_ratio": 0.90, "errors": []}, False,
            hot_sector_context=self.near_context,
        )
        self.assertFalse(result["emotion_near_limit_data_quality"]["actionable"])
        self.assertFalse(result["emotion_near_limit"]["items"])

    def test_leader_research_accepts_launched_first_board(self) -> None:
        pool = [
            {"c": "600001", "n": "二连板", "lbc": 2, "zttj": {"days": 2, "ct": 2}},
            {"c": "600002", "n": "近期多板", "lbc": 1, "zttj": {"days": 8, "ct": 3}},
            {"c": "600003", "n": "普通首板", "lbc": 1, "zttj": {"days": 1, "ct": 1}},
        ]
        self.assertEqual(
            [row["c"] for row in prefilter_emotion_leaders(pool)], ["600001", "600002", "600003"]
        )

    def test_leader_ranking_caps_three_per_sector_and_never_formally_admits(self) -> None:
        candidates = []
        quotes = {}
        for index in range(1, 7):
            code = f"60000{index}"
            candidates.append({
                "c": code, "n": f"龙头{index}", "hybk": "算力", "lbc": 7 - index,
                "zbc": index % 2, "hs": 6.0 + index, "amount": 200_000_000,
                "fund": 40_000_000, "zttj": {"days": 7 - index, "ct": 7 - index},
                "listing_age_verified": True,
            })
            quotes[code] = dict(self.quote, code=code, name=f"龙头{index}", price=11.0, high=11.0, pct=10.0)
        result = build_emotion_leader_report(
            candidates, quotes, quotes, {"算力": 3}, {},
            hot_sector_context=focus_context(candidates),
        )
        items = result["emotion_leaders"]["items"]
        self.assertEqual(len(items), 3)
        self.assertEqual([x["sector_member_rank"] for x in items], [1, 2, 3])
        self.assertEqual([row["current_boards"] for row in items], [5, 6, 4])
        self.assertEqual([row["strength_score"] for row in items], sorted(
            (row["strength_score"] for row in items), reverse=True))
        self.assertFalse(items[0]["formal_eligible"])
        self.assertEqual(set(items[0]["score_components"]), {
            "current_boards", "recent_frequency", "seal_quality", "liquidity", "theme_linkage",
        })

    def test_leader_quote_and_hard_risk_failures_leave_vacancies(self) -> None:
        candidates = [
            {
                "c": "600001", "n": "缺行情", "hybk": "算力", "lbc": 2, "zbc": 0,
                "amount": 200_000_000, "fund": 40_000_000, "zttj": {"days": 2, "ct": 2},
                "listing_age_verified": True,
            },
            {
                "c": "600002", "n": "风险票", "hybk": "算力", "lbc": 3, "zbc": 0,
                "amount": 300_000_000, "fund": 50_000_000, "zttj": {"days": 3, "ct": 3},
                "listing_age_verified": True,
            },
        ]
        risk_quote = dict(self.quote, code="600002", name="风险票", price=11.0, high=11.0, pct=10.0)
        result = build_emotion_leader_report(
            candidates, {"600002": risk_quote}, {"600002": dict(risk_quote)}, {"算力": 2},
            {"600002": ["风险票关于股东减持计划的公告"]},
            hot_sector_context=focus_context(candidates, as_of="2026-08-25"),
        )
        self.assertFalse(result["emotion_leaders"]["items"])
        self.assertEqual(result["emotion_leaders"]["vacant_count"], 5)
        self.assertEqual(len(result["emotion_leaders"]["rejected"]), 2)


if __name__ == "__main__":
    unittest.main()
