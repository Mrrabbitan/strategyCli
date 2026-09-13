from __future__ import annotations

import copy
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from emotion_monitor import build_emotion_report, load_emotion_config
from hot_sector_leaders import build_hot_sector_context, filter_ranked_rows, gate_reason
from near_limit_monitor import build_emotion_leader_report, build_preopen_near_limit_report
from dragon_watchlist import render_dragon_watchlist_section


class HotSectorTests(unittest.TestCase):
    def setUp(self):
        self.config = load_emotion_config()
        self.day = "2026-09-08"
        self.pool = [self.row(f"60000{i}", 7 - i) for i in range(1, 6)]

    def row(self, code, boards, theme="算力"):
        return {"c": code, "n": "虚构测试" + code, "hybk": theme, "lbc": boards,
                "amount": 200_000_000, "fund": 30_000_000, "zbc": 1,
                "hs": 8.0, "listing_age_verified": True,
                "zttj": {"ct": boards, "days": boards}}

    def context(self, pool=None, near=None, verified=True):
        return build_hot_sector_context(self.pool if pool is None else pool, near or [],
            as_of=self.day, source_verified=verified, config=self.config)

    def quote(self, code):
        return {"code": code, "price": 11.0, "prev_close": 10.0, "open": 10.3,
                "high": 11.0, "low": 10.2, "pct": 10.0, "turnover": 8.0,
                "amount_cny": 200_000_000, "volume_lots": 190_000,
                "buy_lots": 120_000, "sell_lots": 70_000, "timestamp": "20260908103000"}

    def test_cold_sector_high_board_is_excluded(self):
        context = self.context(self.pool + [self.row("600099", 12, "冷门")])
        selected = filter_ranked_rows(self.pool + [self.row("600099", 12, "冷门")], context, self.config)
        self.assertEqual([row["c"] for row in selected], ["600001", "600002", "600003"])
        self.assertEqual(context["sectors"][0]["limit_up_count"], 5)
        self.assertNotIn("600099", context["members"])

    def test_active_lower_board_beats_weak_high_board(self):
        weak = dict(self.row("600001", 10), amount=20_000_000, fund=100_000, hs=0.3, zbc=5)
        strong = dict(self.row("600002", 2), amount=800_000_000, fund=240_000_000, hs=10, zbc=0)
        context = self.context([weak, strong])
        self.assertEqual(context["members"]["600002"]["sector_member_rank"], 1)
        self.assertLess(context["members"]["600001"]["strength_score"], context["members"]["600002"]["strength_score"])

    def test_low_price_and_pretty_shape_do_not_add_strength(self):
        original = self.context()
        changed = [dict(row, price=1, low_position=True, smooth_shape=True) for row in self.pool]
        self.assertEqual(original, self.context(changed))

    def test_missing_near_high_is_not_perfect_resilience(self):
        context = self.context(near=[{"c":"600099", "n":"虚构临板", "hybk":"算力",
            "prior_boards":0, "amount":200_000_000, "hs":8}])
        self.assertIsNone(context["members"]["600099"]["retreat_pct"])
        self.assertEqual(context["members"]["600099"]["strength_components"]["resilience"], 0)

    def test_first_board_is_research_only_not_a_trade(self):
        pool = [self.row("600001", 1), self.row("600002", 1)]
        quotes = {row["c"]:self.quote(row["c"]) for row in pool}
        report = build_emotion_leader_report(pool, quotes, quotes, {"算力":2}, {}, hot_sector_context=self.context(pool))
        self.assertEqual(len(report["emotion_leaders"]["items"]), 2)
        self.assertTrue(all(row["formal_eligible"] is False for row in report["emotion_leaders"]["items"]))

    def test_hard_risk_and_missing_quote_do_not_promote_fourth(self):
        quotes = {row["c"]: self.quote(row["c"]) for row in self.pool}
        del quotes["600002"]
        report = build_emotion_leader_report(self.pool, quotes, quotes, {"算力": 5},
            {"600001": ["虚构测试股东减持计划公告"]}, hot_sector_context=self.context())
        items = report["emotion_leaders"]["items"]
        self.assertEqual([row["code"] for row in items], ["600003"])
        self.assertEqual(items[0]["sector_member_rank"], 3)
        self.assertFalse(items[0]["formal_eligible"])
        self.assertFalse(report["emotion_leader_data_quality"]["actionable"])

    def test_filtered_subset_cannot_recalculate_rank(self):
        subset = self.pool[3:]
        quotes = {row["c"]: self.quote(row["c"]) for row in subset}
        report = build_emotion_leader_report(subset, quotes, quotes, {"算力": 5}, {},
                                             hot_sector_context=self.context())
        self.assertEqual(report["emotion_leaders"]["items"], [])

    def test_each_hot_sector_has_its_own_three_places(self):
        pool = self.pool + [self.row(f"60001{i}", 7 - i, "液冷") for i in range(1, 6)]
        selected = filter_ranked_rows(pool, self.context(pool), self.config)
        self.assertEqual(len(selected), 6)
        for theme in ("算力", "液冷"):
            self.assertEqual([row["sector_member_rank"] for row in selected if row["hybk"] == theme], [1, 2, 3])

    def test_order_stable_and_codes_not_double_counted(self):
        self.assertEqual(self.context(), self.context(list(reversed(self.pool)) + [self.pool[0]]))
        self.assertEqual(len(filter_ranked_rows(self.pool + self.pool, self.context(), self.config)), 3)

    def test_old_context_does_not_admit_current_day_leader(self):
        context = self.context()
        context["as_of"] = "2026-09-07"
        quotes = {row["c"]: self.quote(row["c"]) for row in self.pool}
        report = build_emotion_leader_report(self.pool, quotes, quotes, {"算力": 5}, {}, hot_sector_context=context)
        self.assertFalse(report["emotion_leaders"]["items"])

    def test_near_candidate_does_not_receive_an_unconfirmed_board(self):
        near = {"c": "600009", "n": "虚构临板", "hybk": "算力", "prior_boards": 0,
                "prospective_board": 99, "amount": 300_000_000}
        context = self.context(near=[near])
        self.assertEqual(context["members"]["600009"]["current_boards"], 0)
        self.assertTrue(gate_reason("600009", context, self.config))

    def test_missing_source_or_industry_stops_new_list(self):
        missing = self.pool + [dict(self.row("600099", 10), hybk="")]
        for context in (None, self.context(verified=False), self.context(missing)):
            self.assertEqual(filter_ranked_rows(self.pool, context, self.config), [])

    def test_preopen_rejects_legacy_stale_and_wrong_version(self):
        source = {"next_session_watchlist": [{"code": "600001", "name": "虚构"}],
                  "hot_sector_focus": self.context()}
        for day, version in (("2026-09-07", None), (self.day, "legacy")):
            snapshot = copy.deepcopy(source)
            if version:
                snapshot["hot_sector_focus"]["version"] = version
            report = build_preopen_near_limit_report({"emotion_near_limit": snapshot}, expected_as_of=day)
            self.assertFalse(report["emotion_near_limit"]["items"])
        legacy = build_preopen_near_limit_report({"emotion_near_limit": {"next_session_watchlist": [{"code": "600001"}]}}, expected_as_of=self.day)
        self.assertFalse(legacy["emotion_near_limit"]["items"])

    def test_non_hot_holding_exit_warning_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(json.dumps({"entries": [{"code": "600099", "name": "虚构旧仓",
                "theme": "冷门", "source": "auto", "status": "active", "selected_at": self.day,
                "expires_on": "2026-09-10", "catalyst_evidence": [], "last_metrics": {}}]}))
            weak = dict(self.quote("600099"), price=9.0, pct=-10.0, high=10.0,
                        buy_lots=10_000, sell_lots=180_000)
            report = build_emotion_report(dt.date.fromisoformat(self.day), "10_30", "高",
                [], {"600099": weak}, {}, {}, {}, [], {}, persist_state=False, state_path=path,
                hot_sector_context=self.context())
            self.assertFalse(report["emotion_dynamic_targets"]["items"])
            self.assertEqual(report["emotion_recommendations"][0]["action"], "减仓/退出")
            self.assertEqual(report["emotion_alerts"][0]["severity"], "red")

    def test_old_dragon_snapshot_is_not_presented_as_new_model(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old-watchlist.json"
            path.write_text(json.dumps({"analysis_date":"2026-09-07", "target_date":"2026-09-08",
                                        "quote_date":"2026-09-07", "selected":[]}))
            rendered = render_dragon_watchlist_section(path, as_of=dt.date(2026, 9, 8))
        self.assertIn("旧研究快照", rendered)
        self.assertNotIn("周一观察名单", rendered)


if __name__ == "__main__":
    unittest.main()
