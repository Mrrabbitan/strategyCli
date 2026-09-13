from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from technology_leader import (  # noqa: E402
    build_technology_leader_report,
    prefilter_technology_leaders,
    render_technology_leader_section,
    technology_direction,
)

from focus_fixtures import focus_context


class TechnologyLeaderTests(unittest.TestCase):
    def _candidate(self, code: str, name: str, theme: str, boards: int = 4) -> dict:
        return {
            "c": code, "n": name, "hybk": theme, "lbc": boards, "zbc": 0,
            "hs": 10.0, "amount": 1_000_000_000, "fund": 300_000_000,
            "zttj": {"days": boards, "ct": boards}, "listing_age_verified": True,
        }

    def _quote(self, code: str, name: str) -> dict:
        return {
            "code": code, "name": name, "price": 11.0, "prev_close": 10.0,
            "open": 10.3, "high": 11.0, "low": 10.2, "pct": 10.0,
            "turnover": 10.0, "amount_cny": 1_000_000_000,
            "timestamp": "20260827150000",
        }

    def test_direction_mapping_and_prefilter_are_dynamic(self) -> None:
        self.assertEqual(technology_direction("通信设备"), "通信与算力基础设施")
        pool = [
            self._candidate("003040", "通信龙头", "通信设备"),
            self._candidate("600001", "普通制造", "通用设备"),
            self._candidate("688001", "芯片首板", "半导体", boards=1),
        ]
        rows = prefilter_technology_leaders(pool)
        self.assertEqual([row["c"] for row in rows], ["003040"])
        self.assertEqual(rows[0]["technology_direction"], "通信与算力基础设施")

    def test_cosmic_label_requires_strength_and_direction_linkage(self) -> None:
        candidates = [
            self._candidate("003040", "科技甲", "通信设备", boards=6),
            self._candidate("600001", "科技乙", "通信设备", boards=3),
        ]
        for row in candidates:
            row["technology_direction"] = "通信与算力基础设施"
        quotes = {row["c"]: self._quote(row["c"], row["n"]) for row in candidates}
        report = build_technology_leader_report(
            candidates, quotes, quotes, {}, "中低",
            {"up": 3500, "down": 1500, "flat": 100, "total": 5100},
            {"000001": {"pct": 1.2}, "399001": {"pct": 1.8}, "399006": {"pct": 2.0}},
            hot_sector_context=focus_context(candidates, as_of="2026-08-27"),
        )
        first = report["technology_cosmic_leaders"]["items"][0]
        self.assertEqual(first["name"], "科技甲")
        self.assertTrue(first["cosmic_candidate"])
        self.assertEqual(first["grade"], "哈药式宇宙龙头候选")
        self.assertEqual(set(first["score_components"]), {
            "height", "repeatability", "seal_quality", "liquidity",
            "direction_linkage", "market_fit",
        })

    def test_hard_risk_is_rejected_and_never_backfilled(self) -> None:
        candidate = self._candidate("600001", "风险科技", "半导体", boards=5)
        candidate["technology_direction"] = "半导体芯片"
        quote = self._quote("600001", "风险科技")
        report = build_technology_leader_report(
            [candidate], {"600001": quote}, {"600001": quote},
            {"600001": ["风险科技关于股东减持计划的公告"]}, "中",
            {"up": 2500, "down": 2500, "flat": 100, "total": 5100},
            {"000001": {"pct": 0.0}},
            hot_sector_context=focus_context([candidate], as_of="2026-08-27"),
        )
        self.assertFalse(report["technology_cosmic_leaders"]["items"])
        self.assertEqual(len(report["technology_cosmic_leaders"]["rejected"]), 1)

    def test_render_contains_research_boundary(self) -> None:
        report = build_technology_leader_report(
            [], {}, {}, {}, "中", {"up": 0, "total": 0}, {},
            hot_sector_context=focus_context([], as_of="2026-08-27"),
        )
        rendered = render_technology_leader_section(report)
        self.assertIn("科技方向宇宙龙头候选", rendered)
        self.assertIn("不构成交易资格", rendered)


if __name__ == "__main__":
    unittest.main()
