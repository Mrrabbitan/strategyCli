"""Offline native-feed contract tests with deliberately fictional records."""
from __future__ import annotations

import copy
import datetime as dt
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import night_radar_feeds as feeds


class Response(io.BytesIO):
    status_code = 200

    @property
    def content(self):
        return self.getvalue()

    def raise_for_status(self):
        pass


class NativeFeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.raw_dir = Path(self.temp.name)
        self.day = "2026-06-08"
        self.main = {
            "SECURITY_CODE": "600001", "SECURITY_NAME_ABBR": "虚构甲",
            "TRADE_DATE": self.day + " 00:00:00", "TRADE_ID": "101",
            "EXPLANATION": "日涨幅偏离值达到7%的前5只证券",
            "CHANGE_RATE": 10, "TURNOVERRATE": 9,
            "BILLBOARD_BUY_AMT": 100, "BILLBOARD_SELL_AMT": 40,
            "BILLBOARD_NET_AMT": 60, "TRADE_MARKET_CODE": "069001001001",
            "D1_CLOSE_ADJCHRATE": 99,
        }
        self.seat = {
            "SECURITY_CODE": "600001", "TRADE_DATE": self.day + " 00:00:00",
            "TRADE_ID": "101", "OPERATEDEPT_CODE": "0",
            "OPERATEDEPT_NAME": "机构专用", "BUY": 100, "SELL": 40, "NET": 60,
        }

    @staticmethod
    def payload(rows, count=None, pages=1):
        return {"success": True, "result": {
            "count": len(rows) if count is None else count,
            "pages": pages, "data": rows,
        }}

    def test_request_retries_once_and_saves_source_evidence(self):
        body = b'{"success":true,"data":{}}'
        with mock.patch.object(feeds.requests, "get",
                 side_effect=[OSError("offline"), Response(body)]) as req, \
             mock.patch.object(feeds.time, "sleep"):
            value = feeds.fetch_json("https://example.test/read", raw_dir=self.raw_dir,
                                     label="source")
        self.assertTrue(value["success"])
        self.assertEqual(req.call_count, 2)
        self.assertTrue(all(c.kwargs["timeout"] <= 12 for c in req.call_args_list))
        evidence = json.loads((self.raw_dir / "source.json").read_text())
        self.assertEqual(evidence["payload"], value)
        self.assertEqual(evidence["previous_attempts"][0]["error_type"], "OSError")
        self.assertEqual(len(evidence["raw_sha256"]), 64)

    def test_two_failed_attempts_are_not_empty_success(self):
        with mock.patch.object(feeds.requests, "get", side_effect=OSError("offline")) as req, \
             mock.patch.object(feeds.time, "sleep"):
            with self.assertRaises(feeds.FeedError):
                feeds.fetch_json("https://example.test/read", raw_dir=self.raw_dir, label="source")
        self.assertEqual(req.call_count, 2)
        self.assertTrue((self.raw_dir / "source.failure.json").exists())

    def test_expired_budget_does_not_make_a_request(self):
        with mock.patch.object(feeds.requests, "get") as req:
            with self.assertRaises(feeds.FeedError):
                feeds.fetch_json("https://example.test/read", raw_dir=self.raw_dir,
                                 label="source", deadline=feeds.time.monotonic()-1)
        req.assert_not_called()

    def test_pagination_keeps_all_pages(self):
        one, two = copy.deepcopy(self.main), {**self.main, "TRADE_ID": "102"}
        with mock.patch.object(feeds, "fetch_json", side_effect=[
                self.payload([one], 2, 2), self.payload([two], 2, 2)]) as fetch:
            rows, sources = feeds.fetch_paginated("fictional", self.day,
                    raw_dir=self.raw_dir, require_event_ids=True)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(sources), 2)
        self.assertEqual(parse_qs(urlsplit(fetch.call_args_list[1].args[0]).query)["pageNumber"], ["2"])

    def test_pagination_conflicts_fail(self):
        for second in (self.payload([{**self.main, "TRADE_ID": "102"}], 3, 2),
                       self.payload([], 2, 2), self.payload([self.main], 2, 2)):
            with self.subTest(second=second), mock.patch.object(feeds, "fetch_json", side_effect=[
                    self.payload([self.main], 2, 2), second]):
                with self.assertRaises(feeds.FeedError):
                    feeds.fetch_paginated("fictional", self.day, raw_dir=self.raw_dir,
                                          require_event_ids=True)

    def test_source_failure_or_date_conflict_is_not_empty(self):
        for payload in ({"success": False, "result": {"count": 0, "pages": 0, "data": []}},
                        self.payload([{**self.main, "TRADE_DATE": "2026-06-09"}]),
                        self.payload([self.main], 2, 1)):
            with self.subTest(payload=payload), mock.patch.object(feeds, "fetch_json", return_value=payload):
                with self.assertRaises(feeds.FeedError):
                    feeds.fetch_paginated("fictional", self.day, raw_dir=self.raw_dir)

    def test_verified_zero_total_is_valid_empty(self):
        with mock.patch.object(feeds, "fetch_json", return_value=self.payload([], 0, 0)):
            rows, sources = feeds.fetch_paginated("fictional", self.day, raw_dir=self.raw_dir)
        self.assertEqual(rows, [])
        self.assertEqual(len(sources), 1)

    def test_limit_pool_checks_date_total_and_unique_codes(self):
        good = {"data": {"qdate": 20260608, "tc": 1,
                         "pool": [{"c": "600001", "n": "虚构甲", "lbc": 2, "p": 11000}]}}
        with mock.patch.object(feeds, "fetch_json", return_value=good), \
             mock.patch.object(feeds, "_verify_pool_sample", return_value=[]):
            out = feeds.collect_limit_up(self.day, raw_dir=self.raw_dir)
        self.assertEqual(out["status"], "complete")
        self.assertEqual(out["rows"][0]["code"], "600001")
        self.assertEqual(out["rows"][0]["boards"], 2)
        for change in ({"qdate": 20260609}, {"tc": 2},
                       {"tc": 2, "pool": good["data"]["pool"] * 2}):
            bad = {"data": {**good["data"], **change}}
            with self.subTest(change=change), mock.patch.object(feeds, "fetch_json", return_value=bad):
                out = feeds.collect_limit_up(self.day, raw_dir=self.raw_dir)
            self.assertEqual(out["status"], "unavailable")
            self.assertEqual(out["rows"], [])

    def test_limit_pool_uses_allowlist_not_future_fields(self):
        payload = {"data": {"qdate": 20260608, "tc": 1, "pool": [
            {"c": "600001", "n": "虚构甲", "lbc": 2, "p": 11000, "D1_CLOSE_ADJCHRATE": 99}]}}
        with mock.patch.object(feeds, "fetch_json", return_value=payload), \
             mock.patch.object(feeds, "_verify_pool_sample", return_value=[]):
            out = feeds.collect_limit_up(self.day, raw_dir=self.raw_dir)
        self.assertNotIn("D1_CLOSE_ADJCHRATE", out["rows"][0])

    def test_limit_pool_missing_board_count_cannot_become_zero(self):
        payload = {"data": {"qdate": 20260608, "tc": 1, "pool": [
            {"c": "600001", "n": "虚构甲", "p": 11000}]}}
        with mock.patch.object(feeds, "fetch_json", return_value=payload):
            out = feeds.collect_limit_up(self.day, raw_dir=self.raw_dir)
        self.assertEqual(out["status"], "unavailable")

    def test_pool_sample_rejects_halted_old_bar_despite_current_qdate(self):
        pool = [{"c": "600001", "lbc": 2, "p": 11000}]
        payload = {"data": {"sh600001": {"day": [["2026-06-05", "10", "11"]]}}}
        with mock.patch.object(feeds, "fetch_json", return_value=payload):
            with self.assertRaises(feeds.FeedError):
                feeds._verify_pool_sample(pool, self.day, self.raw_dir, feeds.time.monotonic()+60)

    def test_pool_sample_rejects_conflicting_same_date_price(self):
        pool = [{"c": "600001", "lbc": 2, "p": 11000}]
        payload = {"data": {"sh600001": {"day": [[self.day, "10", "10.5"]]}}}
        with mock.patch.object(feeds, "fetch_json", return_value=payload):
            with self.assertRaises(feeds.FeedError):
                feeds._verify_pool_sample(pool, self.day, self.raw_dir, feeds.time.monotonic()+60)

    def test_pool_sample_accepts_exact_date_unadjusted_close(self):
        pool = [{"c": "600001", "lbc": 2, "p": 11000}]
        payload = {"data": {"sh600001": {"day": [[self.day, "10", "11"]]}}}
        with mock.patch.object(feeds, "fetch_json", return_value=payload):
            refs = feeds._verify_pool_sample(pool, self.day, self.raw_dir, feeds.time.monotonic()+60)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["source_date"], self.day)

    def test_pool_sample_does_not_accept_qfq_as_unadjusted(self):
        pool = [{"c": "600001", "lbc": 2, "p": 11000}]
        payload = {"data": {"sh600001": {"qfqday": [[self.day, "10", "11"]]}}}
        with mock.patch.object(feeds, "fetch_json", return_value=payload):
            with self.assertRaises(feeds.FeedError):
                feeds._verify_pool_sample(pool, self.day, self.raw_dir, feeds.time.monotonic()+60)

    def broker_loader(self, name, day, **kwargs):
        row = self.main if name == "RPT_DAILYBILLBOARD_DETAILSNEW" else self.seat
        return [copy.deepcopy(row)], [{"label": name, "url": "https://example.test/"+name}]

    def test_buy_sell_duplicates_remain_for_engine_multiset_union(self):
        with mock.patch.object(feeds, "fetch_paginated", side_effect=self.broker_loader):
            out = feeds.collect_broker_seats(self.day, raw_dir=self.raw_dir)
        self.assertEqual(out["status"], "complete")
        row = out["rows"][0]
        self.assertEqual(len(row["seats"]), 2)
        self.assertEqual({s["side"] for s in row["seats"]}, {"buy", "sell"})
        self.assertTrue(row["seats_complete"])
        self.assertEqual(row["period_start"], self.day)
        self.assertIsNone(row["source_time"])
        self.assertEqual(row["source_time_precision"], "date")
        self.assertNotIn("D1_CLOSE_ADJCHRATE", row)

    def test_cumulative_start_is_unknown_when_not_disclosed(self):
        self.main["EXPLANATION"] = "连续三个交易日内涨幅偏离值累计达到20%"
        with mock.patch.object(feeds, "fetch_paginated", side_effect=self.broker_loader):
            out = feeds.collect_broker_seats(self.day, raw_dir=self.raw_dir)
        self.assertEqual(out["rows"][0]["period"], "multi")
        self.assertIsNone(out["rows"][0]["period_start"])

    def test_missing_buy_feed_is_partial_with_no_complete_event(self):
        def loader(name, day, **kwargs):
            if name == "RPT_BILLBOARD_DAILYDETAILSBUY":
                raise feeds.FeedError("fictional failure")
            return self.broker_loader(name, day, **kwargs)
        with mock.patch.object(feeds, "fetch_paginated", side_effect=loader):
            out = feeds.collect_broker_seats(self.day, raw_dir=self.raw_dir)
        self.assertEqual(out["status"], "partial")
        self.assertFalse(out["rows"][0]["seats_complete"])

    def test_seat_arithmetic_or_event_mismatch_is_partial(self):
        for change in ({"NET": 61}, {"TRADE_ID": "missing"}, {"SECURITY_CODE": "600002"}):
            self.seat.update(change)
            with self.subTest(change=change), mock.patch.object(feeds, "fetch_paginated", side_effect=self.broker_loader):
                out = feeds.collect_broker_seats(self.day, raw_dir=self.raw_dir)
            self.assertEqual(out["status"], "partial")

    def test_institutions_keep_money_units_and_strip_future_returns(self):
        row = {**self.main, "BUY_AMT": 100, "SELL_AMT": 40, "NET_BUY_AMT": 60,
               "BUY_TIMES": 2, "SELL_TIMES": 1}
        with mock.patch.object(feeds, "fetch_paginated", return_value=([row], [{"url": "https://example.test"}])):
            out = feeds.collect_institutions(self.day, raw_dir=self.raw_dir)
        self.assertEqual(out["status"], "complete")
        self.assertEqual(out["rows"][0]["net_cny"], 60)
        self.assertEqual(out["rows"][0]["pct"], 10)
        self.assertEqual(out["rows"][0]["turnover_pct"], 9)
        self.assertNotIn("D1_CLOSE_ADJCHRATE", out["rows"][0])

    def test_invalid_institution_net_is_partial_not_zero(self):
        row = {**self.main, "BUY_AMT": 100, "SELL_AMT": 40, "NET_BUY_AMT": None}
        with mock.patch.object(feeds, "fetch_paginated", return_value=([row], [{"url": "https://example.test"}])):
            out = feeds.collect_institutions(self.day, raw_dir=self.raw_dir)
        self.assertEqual(out["status"], "partial")
        self.assertEqual(out["rows"], [])

    def test_collect_market_failure_does_not_block_disclosures(self):
        disclosures = types.ModuleType("night_radar_disclosures")
        disclosures.collect_disclosures = lambda *args: {
            k: feeds._section("complete") for k in ("announcements", "earnings", "repurchases")}
        with mock.patch.dict(sys.modules, {"night_radar_disclosures": disclosures}), \
             mock.patch.object(feeds, "_trade_date", return_value=self.day), \
             mock.patch.object(feeds, "collect_limit_up", side_effect=feeds.FeedError("offline")), \
             mock.patch.object(feeds, "collect_broker_seats", return_value=feeds._section("complete")), \
             mock.patch.object(feeds, "collect_institutions", return_value=feeds._section("complete")):
            out = feeds.collect(self.day+"T22:00:00+08:00", raw_dir=self.raw_dir)
        self.assertEqual(set(out["sections"]), set(feeds.SECTION_KEYS))
        self.assertEqual(out["sections"]["limit_up"]["status"], "unavailable")
        self.assertEqual(out["sections"]["earnings"]["status"], "complete")

    def test_unknown_calendar_never_fetches_market(self):
        disclosures = types.ModuleType("night_radar_disclosures")
        disclosures.collect_disclosures = lambda *args: {
            k: feeds._section("complete") for k in ("announcements", "earnings", "repurchases")}
        with mock.patch.dict(sys.modules, {"night_radar_disclosures": disclosures}), \
             mock.patch.object(feeds, "_trade_date", side_effect=feeds.FeedError("缺少日历")), \
             mock.patch.object(feeds, "collect_limit_up") as pool:
            out = feeds.collect(self.day+"T22:00:00+08:00", raw_dir=self.raw_dir)
        pool.assert_not_called()
        self.assertIsNone(out["trade_date"])
        self.assertEqual(out["sections"]["broker_seats"]["status"], "unavailable")

    def test_datetime_and_window_are_explicit(self):
        with self.assertRaises(ValueError):
            feeds.collect(self.day+"T22:00:00", raw_dir=self.raw_dir)
        with self.assertRaises(ValueError):
            feeds.collect(self.day+"T22:00:00+08:00",
                          window_start=self.day+"T23:00:00+08:00", raw_dir=self.raw_dir)


if __name__ == "__main__":
    unittest.main()
