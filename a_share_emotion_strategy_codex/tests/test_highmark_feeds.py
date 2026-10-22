"""Offline, invented evidence for the high-mark feed contracts."""
from __future__ import annotations

import datetime as dt
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from highmark_feeds import (EvidenceError, PublicEvidence, _retained_items,
                           adjustment_diagnostic, collect_evidence,
                           normalize_history, normalize_pool, normalize_quotes)


DAY = "2026-01-30"
SOURCE = {"url": "https://example.invalid/native", "fetched_at": "2026-01-30T16:00:00+08:00"}


def bars(count=70, end=DAY, price=10):
    end = dt.date.fromisoformat(end)
    return [[(end - dt.timedelta(days=count - 1 - i)).isoformat(), str(price),
             str(price), str(price + 1), str(price - 1), "100"] for i in range(count)]


def history_payload(symbol="sz000001", rows=None, key="day"):
    return {"data": {symbol: {key: rows if rows is not None else bars()}}}


def pool_payload(rows=None):
    rows = rows if rows is not None else [{"c": "000001", "n": "人工甲", "lbc": 3, "p": 10000, "zf": 10}]
    return {"data": {"qdate": DAY.replace("-", ""), "tc": len(rows), "pool": rows}}


def quote(symbol="sz000001", name="人工甲", stamp="20260130150001", price="10", prev="10", pct="0"):
    f = [""] * 40
    for index, value in {1: name, 2: symbol[2:], 3: price, 4: prev, 30: stamp,
                         32: pct, 37: "1000", 38: "3"}.items():
        f[index] = value
    return 'v_' + symbol + '="' + "~".join(f) + '";'


class HighmarkFeedContracts(unittest.TestCase):
    def test_pool_filters_all_non_mainboard_and_st_without_filling(self):
        rows = [{"c": code, "n": name, "lbc": 2, "p": 10000, "zf": 10}
                for code, name in (("000001", "人工甲"), ("600001", "ST人工"),
                                   ("688001", "科创人工"), ("300001", "创业人工"),
                                   ("920001", "北交人工"), ("200001", "B股人工"))]
        result = normalize_pool(pool_payload(rows), DAY, SOURCE)
        self.assertEqual([r["code"] for r in result["items"]], ["000001"])
        self.assertEqual(result["source_count"], 6)
        self.assertEqual(len(result["excluded"]), 5)
        self.assertFalse(result["all_market_trend_leaders_complete"])

    def test_pool_first_board_not_dynamic_highmark(self):
        payload = pool_payload()
        payload["data"]["pool"][0]["lbc"] = 1
        self.assertEqual(normalize_pool(payload, DAY, SOURCE)["items"], [])

    def test_pool_current_native_zdp_field_is_used(self):
        payload = pool_payload()
        row = payload["data"]["pool"][0]
        row["zdp"] = row.pop("zf")
        self.assertEqual(normalize_pool(payload, DAY, SOURCE)["items"][0]["pct"], 10)

    def test_pool_wrong_date_duplicate_or_count_is_failure(self):
        for mode in ("date", "duplicate", "count"):
            payload = pool_payload()
            if mode == "date":
                payload["data"]["qdate"] = "20260129"
            elif mode == "count":
                payload["data"]["tc"] = 2
            else:
                payload["data"]["pool"] *= 2
                payload["data"]["tc"] = 2
            with self.subTest(mode=mode), self.assertRaises(EvidenceError):
                normalize_pool(payload, DAY, SOURCE)

    def test_bars_keep_native_prices_and_do_not_invent_reference_close(self):
        value = normalize_history(history_payload(), "sz000001", DAY)
        self.assertEqual(value["adjustment"], "none")
        self.assertEqual(value["history"][-1]["close"], 10)
        self.assertNotIn("prev_close", value["history"][-1])
        self.assertFalse(value["native_reference_close_available"])

    def test_history_requires_65_ordered_unique_bars_and_exact_day(self):
        for raw in (bars(64), bars(end="2026-01-29"), list(reversed(bars())), bars() + bars()[-1:]):
            with self.subTest(length=len(raw)), self.assertRaises(EvidenceError):
                normalize_history(history_payload(rows=raw), "sz000001", DAY)

    def test_future_daily_rows_are_not_used(self):
        raw = bars() + [["2026-01-31", "999", "999", "1000", "998", "100"]]
        value = normalize_history(history_payload(rows=raw), "sz000001", DAY)
        self.assertEqual(value["future_rows_excluded"], 1)
        self.assertEqual(value["history"][-1]["date"], DAY)

    def test_adjustment_factor_diagnostic_does_not_modify_bars(self):
        native = [{"date": "2026-01-01", "close": 10}, {"date": "2026-01-02", "close": 5}]
        adjusted = [{"date": "2026-01-01", "close": 5}, {"date": "2026-01-02", "close": 5}]
        check = adjustment_diagnostic(native, adjusted)
        self.assertTrue(check["corporate_action_detected"])
        self.assertEqual(check["possible_adjustment_dates"], ["2026-01-02"])
        self.assertEqual(native[0]["close"], 10)
        self.assertIsNone(adjustment_diagnostic(native, adjusted[:1])["corporate_action_detected"])

    def test_quote_uses_own_source_time_and_native_reference(self):
        output, missing = normalize_quotes(quote(), ["sz000001"], DAY, SOURCE)
        self.assertEqual(missing, [])
        self.assertEqual(output["sz000001"]["prev_close"], 10)
        self.assertEqual(output["sz000001"]["source_time"], "2026-01-30T15:00:01+08:00")

    def test_quote_stale_premarket_or_inconsistent_is_not_verified(self):
        for text in (quote(stamp="20260129160000"), quote(stamp="20260130093000"), quote(pct="10")):
            value, missing = normalize_quotes(text, ["sz000001"], DAY, SOURCE)
            self.assertFalse(value)
            self.assertTrue(missing)

    def test_duplicate_quote_remains_conflict_even_if_first_is_invalid(self):
        with self.assertRaises(EvidenceError):
            normalize_quotes(quote(pct="10") + quote(), ["sz000001"], DAY, SOURCE)

    def test_retained_must_have_explicit_source_and_mainboard_identity(self):
        accepted, missing = _retained_items([
            {"code": "000001", "name": "人工甲", "source": SOURCE},
            {"code": "600001", "name": "人工乙"},
            {"code": "300001", "name": "人工丙", "source": SOURCE}])
        self.assertEqual(set(accepted), {"000001"})
        self.assertEqual(len(missing), 2)

    def test_http_denial_body_preserved_no_second_request(self):
        error = urllib.error.HTTPError("https://example.invalid/api", 403, "Forbidden", {}, io.BytesIO(b"denied"))
        with tempfile.TemporaryDirectory() as temp, patch("urllib.request.build_opener") as factory:
            factory.return_value.open.side_effect = error
            feed = PublicEvidence(Path(temp))
            for label in ("first", "second"):
                with self.assertRaises(EvidenceError):
                    feed.read("https://example.invalid/api", label)
            self.assertEqual(factory.return_value.open.call_count, 1)
            self.assertEqual((Path(temp) / "first.raw").read_bytes(), b"denied")
            self.assertEqual(json.loads((Path(temp) / "first.json").read_text())["http_status"], 403)

    def test_failed_pool_stays_failed_retained_source_not_substitute_pool(self):
        def read_json(self, url, label):
            if label == "limit-up-pool":
                raise EvidenceError("人工连接失败")
            symbol = "sh000002" if "sh000002" in url else "sz399107" if "sz399107" in url else "sz000001"
            return history_payload(symbol, key="qfqday" if label.endswith("qfq") else "day"), SOURCE
        text = quote() + quote("sh000002", "人工指数甲") + quote("sz399107", "人工指数乙")
        with tempfile.TemporaryDirectory() as temp, patch.object(PublicEvidence, "json", read_json), patch.object(PublicEvidence, "read", return_value=(text, SOURCE)):
            result = collect_evidence(DAY, Path(temp), [{"code": "000001", "name": "人工甲", "source": SOURCE}])
        self.assertEqual(result["pool"]["status"], "unavailable")
        self.assertFalse(result["pool"]["pool_complete"])
        self.assertIn("000001", result["stocks"])
        self.assertEqual(result["stocks"]["000001"]["membership"], "retained_prior_highmark")
        self.assertFalse(result["stocks"]["000001"]["security_status_verified"])
        self.assertEqual(len(result["stocks"]["000001"]["adjusted_history"]), 70)

    def test_changed_st_quote_excluded_and_crosscheck_downgrades_pool(self):
        def read_json(self, url, label):
            if label == "limit-up-pool":
                return pool_payload(), SOURCE
            symbol = "sh000002" if "sh000002" in url else "sz399107" if "sz399107" in url else "sz000001"
            return history_payload(symbol, key="qfqday" if label.endswith("qfq") else "day"), SOURCE
        text = quote(name="*ST人工") + quote("sh000002", "人工指数甲") + quote("sz399107", "人工指数乙")
        with tempfile.TemporaryDirectory() as temp, patch.object(PublicEvidence, "json", read_json), patch.object(PublicEvidence, "read", return_value=(text, SOURCE)):
            result = collect_evidence(DAY, Path(temp), [])
        self.assertEqual(result["pool"]["status"], "partial")
        self.assertFalse(result["pool"]["pool_complete"])
        self.assertEqual(result["stocks"]["000001"]["status"], "unavailable")
        self.assertIn("ST", result["exclusions"][-1]["reason"])

    def test_zero_volume_and_benchmark_close_conflict_are_not_verified(self):
        def read_json(self, url, label):
            if label == "limit-up-pool":
                return pool_payload(), SOURCE
            symbol = "sh000002" if "sh000002" in url else "sz399107" if "sz399107" in url else "sz000001"
            raw = bars()
            if symbol == "sz000001":
                raw[-1][-1] = "0"
            return history_payload(symbol, raw, key="qfqday" if label.endswith("qfq") else "day"), SOURCE
        text = quote() + quote("sh000002", "人工指数甲", price="11", pct="10") + quote("sz399107", "人工指数乙")
        with tempfile.TemporaryDirectory() as temp, patch.object(PublicEvidence, "json", read_json), patch.object(PublicEvidence, "read", return_value=(text, SOURCE)):
            result = collect_evidence(DAY, Path(temp), [])
        self.assertEqual(result["stocks"]["000001"]["status"], "unavailable")
        self.assertIn("无成交量", result["exclusions"][-1]["reason"])
        self.assertEqual(result["benchmarks"]["sh000002"]["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
