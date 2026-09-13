from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from emotion_monitor import (  # noqa: E402
    _evaluate_alert,
    add_trading_days,
    build_emotion_report,
    load_emotion_config,
    quote_metrics,
    validate_catalyst,
)

from focus_fixtures import focus_context


class EmotionMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_emotion_config()
        self.day = dt.date(2026, 8, 25)
        self.candidate = {
            "c": "600001", "n": "测试股份", "hybk": "算力", "lbc": 1, "hs": 8.0,
            "amount": 200_000_000, "fund": 30_000_000, "zbc": 0,
        }
        self.focus_context = focus_context([self.candidate], peers=True)
        self.quote = {
            "name": "测试股份", "code": "600001", "price": 11.0, "prev_close": 10.0,
            "open": 10.3, "high": 11.0, "low": 10.2, "pct": 10.0,
            "amount_cny": 200_000_000, "volume_lots": 190_000,
            "buy_lots": 120_000, "sell_lots": 70_000, "turnover": 8.0,
            "timestamp": "20260825103000",
        }
        self.official = {
            "code": "600001", "title": "测试股份签署算力项目协议", "published_at": "2026-08-25",
            "url": "https://www.sse.com.cn/disclosure/example", "event_type": "项目合同",
            "theme": "算力", "source_tier": "official",
        }

    def test_catalyst_requires_official_or_two_independent_mainstream_sources(self) -> None:
        ok, rows, _ = validate_catalyst("600001", "算力", [self.official], self.day, self.config)
        self.assertTrue(ok)
        self.assertTrue(rows[0]["official"])
        social = dict(self.official, url="https://guba.eastmoney.com/post/1", source_tier="mainstream")
        ok, _, _ = validate_catalyst("600001", "算力", [social], self.day, self.config)
        self.assertFalse(ok)
        first = dict(self.official, url="https://finance.example.com/a", source_tier="mainstream")
        second = dict(self.official, url="https://news.example.net/b", source_tier="mainstream")
        ok, rows, _ = validate_catalyst("600001", "算力", [first, second], self.day, self.config)
        self.assertTrue(ok)
        self.assertEqual({row["domain"] for row in rows}, {"finance.example.com", "news.example.net"})

    def test_verified_candidate_enters_watchlist_and_is_auditable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "watchlist.json"
            result = build_emotion_report(
                report_date=self.day,
                slot="10_30",
                risk_level="中",
                candidates=[self.candidate],
                quotes={"600001": self.quote},
                pool_map={"600001": self.candidate},
                theme_counts={"算力": 3},
                evidence_map={"600001": [self.official]},
                evidence_errors=[],
                announcement_risks={},
                persist_state=True,
                state_path=state_path,
                hot_sector_context=self.focus_context,
            )
            self.assertEqual(len(result["emotion_selection"]["admitted"]), 1)
            self.assertEqual(result["emotion_watchlist"][0]["source"], "auto")
            self.assertEqual(result["emotion_dynamic_targets"]["items"][0]["code"], "600001")
            self.assertEqual(result["emotion_dynamic_targets"]["mode"], "dynamic")
            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["entries"][0]["expires_on"], add_trading_days(self.day, 4).isoformat())
            self.assertEqual(persisted["entries"][0]["catalyst_evidence"][0]["title"], self.official["title"])

    def test_unverified_candidate_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = build_emotion_report(
                self.day, "10_30", "中", [self.candidate], {"600001": self.quote},
                {"600001": self.candidate}, {"算力": 3}, {}, ["missing evidence"], {},
                persist_state=False, state_path=Path(tmp) / "state.json",
                hot_sector_context=self.focus_context,
            )
            self.assertFalse(result["emotion_selection"]["admitted"])
            reasons = result["emotion_selection"]["rejected"][0]["reasons"]
            self.assertTrue(any("真实催化" in reason for reason in reasons))
            self.assertFalse(result["emotion_dynamic_targets"]["items"])

    def test_high_open_is_single_hard_red_and_never_a_buy(self) -> None:
        metrics = quote_metrics(dict(self.quote, open=10.8), self.candidate, 3)
        entry = {"code": "600001", "name": "测试股份"}
        alert, recommendation = _evaluate_alert(
            entry, metrics, {}, [], "中", False, self.config, True
        )
        self.assertEqual(alert["severity"], "red")
        self.assertEqual(alert["direction"], "追高风险")
        self.assertEqual(recommendation["action"], "禁止介入")

    def test_fund_outflow_proxy_is_red(self) -> None:
        weak_quote = dict(
            self.quote, price=9.5, high=10.0, amount_cny=200_000_000,
            volume_lots=200_000, buy_lots=20_000, sell_lots=80_000, pct=-5.0,
        )
        metrics = quote_metrics(weak_quote, {}, 0)
        entry = {"code": "600001", "name": "测试股份"}
        alert, recommendation = _evaluate_alert(entry, metrics, {}, [], "中", False, self.config, True)
        self.assertEqual(alert["severity"], "red")
        self.assertTrue(any(row["type"] == "fund_outflow_proxy" for row in alert["triggers"]))
        self.assertEqual(recommendation["action"], "减仓/退出")

    def test_positive_resonance_cannot_open_new_risk_when_market_high(self) -> None:
        metrics = quote_metrics(dict(self.quote, price=10.5, high=10.5, pct=5.0), {}, 3)
        entry = {"code": "600001", "name": "测试股份"}
        alert, recommendation = _evaluate_alert(entry, metrics, {}, [], "高", True, self.config, True)
        self.assertEqual(alert["severity"], "red")
        self.assertEqual(alert["direction"], "转强")
        self.assertEqual(recommendation["action"], "等待承接")

    def test_single_negative_indicator_is_orange(self) -> None:
        orange_quote = dict(self.quote, price=9.5, high=10.0, pct=-5.0, buy_lots=60_000, sell_lots=40_000)
        metrics = quote_metrics(orange_quote, {}, 3)
        metrics["below_vwap_pct"] = 0.0
        entry = {"code": "600001", "name": "测试股份"}
        alert, _ = _evaluate_alert(entry, metrics, {}, [], "中", False, self.config, True)
        self.assertEqual(alert["severity"], "orange")

    def test_initial_break_count_is_not_mistaken_for_an_increase(self) -> None:
        metrics = quote_metrics(dict(self.quote, pct=0.0), dict(self.candidate, zbc=4), 3)
        metrics["retreat_from_high_pct"] = 0.0
        entry = {"code": "600001", "name": "测试股份", "source": "manual", "catalyst_evidence": []}
        alert, _ = _evaluate_alert(entry, metrics, {}, [], "中", False, self.config, True)
        self.assertFalse(any(row["type"] == "break_increase" for row in alert["triggers"]))

    def test_manual_watch_without_catalyst_never_receives_trial_action(self) -> None:
        metrics = quote_metrics(dict(self.quote, price=10.6, high=10.6, pct=6.0), {}, 3)
        entry = {"code": "600001", "name": "测试股份", "source": "manual", "catalyst_evidence": []}
        alert, recommendation = _evaluate_alert(entry, metrics, {}, [], "中", False, self.config, True)
        self.assertEqual(alert["severity"], "red")
        self.assertEqual(recommendation["action"], "等待承接")

if __name__ == "__main__":
    unittest.main()
