"""Synthetic records only: no network, real stock lists, or notifications."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from highmark_events import collect_events, reparse_archive, COLUMNS, PAGE_SIZE, MAX_RESPONSE_BYTES


NORMAL = "非S证券连续三个交易日内收盘价格涨幅偏离值累计达到20%的证券"
NORMAL_DOWN = "非S证券连续三个交易日内收盘价格跌幅偏离值累计达到20%的证券"
SEVERE = "有价格涨跌幅限制的连续10个交易日内收盘价格涨幅偏离值累计达到100%的证券"
DAILY = "日涨幅偏离值达到7%的前5只证券"


def row(code="600001", day="2026-10-20", reason=NORMAL):
    return {"SECURITY_CODE": code, "SECUCODE": code + ".SH",
            "SECURITY_NAME_ABBR": "人工样例", "TRADE_DATE": day + " 00:00:00",
            "EXPLANATION": reason}


def payload(rows, count=None, pages=None):
    count = len(rows) if count is None else count
    pages = (count + PAGE_SIZE - 1) // PAGE_SIZE if pages is None else pages
    return {"success": True, "result": {"count": count, "pages": pages, "data": rows}}


class Response:
    def __init__(self, value, status=200):
        self.status_code = status
        self.content = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()

    def iter_content(self, chunk_size):
        for start in range(0, len(self.content), chunk_size):
            yield self.content[start:start + chunk_size]

    def close(self):
        pass


class HighmarkEventsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"AUTOSTRATEGY_PRIVATE_ROOT": self.tmp.name})
        self.env.start()
        self.raw = Path(self.tmp.name) / "raw"

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def collect(self, responses):
        with patch("highmark_events.requests.get", side_effect=responses) as get:
            result = collect_events("2026-10-01", "2026-10-22", self.raw)
        return result, get

    def test_complete_one_page_keeps_provider_and_official_distinct(self):
        p = payload([row()])
        result, get = self.collect([Response(p), Response(p)])
        self.assertTrue(result["provider_complete"])
        self.assertFalse(result["official_complete"])
        self.assertTrue(result["reference_only"])
        self.assertEqual(result["normal"]["600001"][0]["published_date"], "2026-10-20")
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args.kwargs["timeout"], 6)
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        self.assertTrue((self.raw / "page-001.json").exists())

    def test_full_pagination_uses_raw_total_not_event_count(self):
        rows = [row(code="600%03d" % i, reason=DAILY) for i in range(501)]
        rows[-1] = row("600501", reason=NORMAL)
        first, last = payload(rows[:500], 501), payload(rows[500:], 501)
        result, get = self.collect([Response(first), Response(last), Response(first)])
        self.assertTrue(result["provider_complete"])
        self.assertEqual(result["raw_row_count"], 501)
        self.assertEqual(result["unique_raw_count"], 501)
        self.assertEqual(result["event_count"], 1)
        self.assertEqual(result["page_row_counts"], [500, 1])
        self.assertEqual(get.call_count, 3)

    def test_distinct_reasons_retained_raw_then_events_merge_by_kind(self):
        p = payload([row(reason=SEVERE), row(reason="连续10个交易日内4次出现同正向异常波动的证券"),
                     row(reason=NORMAL), row(reason=DAILY)])
        result, _ = self.collect([Response(p), Response(p)])
        self.assertEqual(result["raw_row_count"], 4)
        self.assertEqual(result["unique_raw_count"], 4)
        self.assertEqual(len(result["severe"]["600001"]), 1)
        self.assertEqual(len(result["severe"]["600001"][0]["reasons"]), 2)
        self.assertEqual(result["event_count"], 2)

    def test_failed_page_never_becomes_successful_empty_or_complete(self):
        first = payload([row(code="600%03d" % i, reason=DAILY) for i in range(500)], 501)
        result, get = self.collect([Response(first), TimeoutError("synthetic")])
        self.assertFalse(result["provider_complete"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(get.call_count, 2)
        self.assertIn("TimeoutError", result["missing"])
        self.assertTrue((self.raw / "page-002.failure.json").exists())

    def test_duplicate_primary_key_invalidates_coverage(self):
        p = payload([row(), row()])
        result, _ = self.collect([Response(p)])
        self.assertIn("duplicate_raw_primary_key", result["missing"])
        self.assertFalse(result["provider_complete"])

    def test_final_metadata_change_is_not_silently_accepted(self):
        first, changed = payload([row()]), payload([row(), row("600002")])
        result, _ = self.collect([Response(first), Response(changed)])
        self.assertFalse(result["provider_complete"])
        self.assertIn("final_metadata_changed", result["missing"])

    def test_same_total_with_changed_first_page_is_inconsistent(self):
        result, _ = self.collect([Response(payload([row()])), Response(payload([row("600002")]))])
        self.assertIn("first_page_changed_during_collection", result["missing"])

    def test_date_conflict_and_identity_conflict_are_not_reset_evidence(self):
        p = payload([row(day="2026-11-01")])
        result, _ = self.collect([Response(p)])
        self.assertIn("row_outside_requested_dates", result["missing"])
        item = row()
        item["SECUCODE"] = "600999.SH"
        result, _ = self.collect([Response(payload([item]))])
        self.assertIn("security_identity_conflict", result["missing"])

    def test_unrecognised_abnormal_reason_keeps_gap(self):
        p = payload([row(reason="本所认定属于严重异常波动的其他证券")])
        result, _ = self.collect([Response(p), Response(p)])
        self.assertTrue(result["pagination_complete"])
        self.assertFalse(result["provider_complete"])
        self.assertIn("unrecognised_disclosure_reason", result["missing"])
        self.assertEqual(result["normal"], {})
        self.assertEqual(result["severe"], {})

    def test_single_day_and_turnover_do_not_trigger_normal_reset(self):
        p = payload([row(reason=DAILY), row("600002", reason="日换手率达到20%的前5只证券"),
                     row("600003", reason="日振幅值达到15%的前5只证券")])
        result, _ = self.collect([Response(p), Response(p)])
        self.assertTrue(result["provider_complete"])
        self.assertEqual(result["normal"], {})
        self.assertEqual(result["severe"], {})

    def test_complete_three_day_turnover_is_ordinary_reset_not_price_event(self):
        reason = "连续三个交易日内日均换手率与前五个交易日的日均换手率的比值达到30倍且累计换手率达到20%的证券"
        p = payload([row(reason=reason)])
        result, _ = self.collect([Response(p), Response(p)])
        self.assertTrue(result["provider_complete"])
        event = result["normal"]["600001"][0]
        self.assertEqual(event["reason_types"], ["turnover"])
        self.assertFalse(event["counts_toward_10d_price_events"])

    def test_truncated_three_day_turnover_leaves_stock_specific_gap(self):
        p = payload([row(reason="连续三个交易日内日均换手率与前五个交易日的日均换手率的比值达到30倍的证券")])
        result, _ = self.collect([Response(p), Response(p)])
        self.assertFalse(result["provider_complete"])
        self.assertEqual(result["unresolved_codes"], ["600001"])
        self.assertEqual(result["normal"], {})

    def test_price_and_turnover_same_day_merge_but_preserve_price_count(self):
        reason = "连续三个交易日内日均换手率与前五个交易日的日均换手率的比值达到30倍且累计换手率达到20%的证券"
        p = payload([row(reason=reason), row(reason=NORMAL)])
        result, _ = self.collect([Response(p), Response(p)])
        event = result["normal"]["600001"][0]
        self.assertEqual(set(event["reason_types"]), {"turnover", "price"})
        self.assertTrue(event["counts_toward_10d_price_events"])

    def test_negative_and_thirty_day_thresholds_are_known_severe_events(self):
        p = payload([row(reason=NORMAL_DOWN),
                     row("600002", reason="连续30个交易日内收盘价格涨幅偏离值累计达到200%的证券"),
                     row("600003", reason="连续10个交易日内收盘价格跌幅偏离值累计达到50%的证券")])
        result, _ = self.collect([Response(p), Response(p)])
        self.assertTrue(result["provider_complete"])
        self.assertEqual(set(result["severe"]), {"600002", "600003"})

    def test_native_severe_period_alias_retains_positive_event(self):
        p = payload([row(reason="严重异常期间日收盘价格涨幅偏离值累计达到100%的证券"),
                     row("600002", reason="严重异常期间4次出现正向异常波动的证券")])
        result, _ = self.collect([Response(p), Response(p)])
        self.assertTrue(result["provider_complete"])
        self.assertEqual(set(result["severe"]), {"600001", "600002"})
        self.assertFalse(result["official_complete"])

    def test_native_daily_amplitude_alias_does_not_become_reset(self):
        p = payload([row(reason="日振幅值达到15%的前5只证券")])
        result, _ = self.collect([Response(p), Response(p)])
        self.assertTrue(result["provider_complete"])
        self.assertEqual(result["normal"], {})
        self.assertEqual(result["severe"], {})

    def test_upward_fifty_and_downward_hundred_are_not_regulatory_aliases(self):
        p = payload([row(reason="连续10个交易日内收盘价格涨幅偏离值累计达到50%的证券"),
                     row("600002", reason="连续10个交易日内收盘价格跌幅偏离值累计达到100%的证券")])
        result, _ = self.collect([Response(p), Response(p)])
        self.assertFalse(result["provider_complete"])
        self.assertEqual(result["severe"], {})
        self.assertEqual(result["unresolved_codes"], ["600001", "600002"])

    def test_generic_signed_severe_threshold_requires_matching_direction(self):
        p = payload([row(reason="连续10个交易日内收盘价格涨跌幅偏离值累计达到+100%的证券"),
                     row("600002", reason="连续30个交易日内收盘价格涨跌幅偏离值累计达到-70%的证券")])
        result, _ = self.collect([Response(p), Response(p)])
        self.assertTrue(result["provider_complete"])
        self.assertEqual(set(result["severe"]), {"600001", "600002"})

    def test_other_board_rows_do_not_create_mainboard_reset(self):
        item = row("300001", reason="连续三个交易日内涨幅偏离值累计达到30%的证券")
        item["SECUCODE"] = "300001.SZ"
        p = payload([item])
        result, _ = self.collect([Response(p), Response(p)])
        self.assertTrue(result["provider_complete"])
        self.assertEqual(result["normal"], {})

    def test_no_records_requires_explicit_consistent_zero_metadata(self):
        p = payload([])
        result, _ = self.collect([Response(p), Response(p)])
        self.assertTrue(result["provider_complete"])
        self.assertFalse(result["official_complete"])
        result, _ = self.collect([Response({"success": True, "result": None})])
        self.assertFalse(result["provider_complete"])
        self.assertEqual(result["status"], "unavailable")

    def test_count_pages_and_row_count_must_agree(self):
        result, _ = self.collect([Response(payload([row()], count=501, pages=1))])
        self.assertIn("count_pages_conflict", result["missing"])
        result, _ = self.collect([Response(payload([row()], count=2, pages=1))])
        self.assertIn("page_row_count_mismatch", result["missing"])

    def test_transport_errors_and_invalid_json_are_retained_once(self):
        result, get = self.collect([Response(b"synthetic access denied", status=403)])
        self.assertEqual(get.call_count, 1)
        self.assertIn("http_403", result["missing"])
        result, get = self.collect([Response(b"not json")])
        self.assertEqual(get.call_count, 1)
        self.assertIn("JSONDecodeError", result["missing"])

    def test_request_has_no_posterior_performance_or_success_rate_fields(self):
        p = payload([])
        _, get = self.collect([Response(p), Response(p)])
        url = get.call_args.args[0]
        self.assertNotIn("D1_CLOSE", url)
        self.assertNotIn("EXPLAIN%2C", url)
        self.assertNotIn(",EXPLAIN,", COLUMNS)
        self.assertIn("EXPLANATION", COLUMNS)

    def test_raw_archive_cannot_be_written_into_public_source(self):
        with self.assertRaises(ValueError):
            collect_events("2026-10-01", "2026-10-22", Path(self.tmp.name).parent / "not-private")

    def test_future_end_date_is_rejected_before_request(self):
        with patch("highmark_events.requests.get") as get:
            with self.assertRaises(ValueError):
                collect_events("2099-01-01", "2099-02-01", self.raw)
            get.assert_not_called()

    def test_streamed_body_limit_retains_only_a_bounded_failure_prefix(self):
        result, get = self.collect([Response(b"x" * (MAX_RESPONSE_BYTES + 100))])
        self.assertFalse(result["provider_complete"])
        self.assertIn("response_too_large", result["missing"])
        self.assertTrue(get.call_args.kwargs["stream"])
        saved = json.loads((self.raw / "page-001.failure.json").read_text())
        import base64
        self.assertEqual(len(base64.b64decode(saved["raw_body_base64"])), MAX_RESPONSE_BYTES)
        self.assertFalse(saved["raw_body_complete"])

    def test_offline_reparse_preserves_retrieval_fingerprints_without_network(self):
        p = payload([row()])
        original, _ = self.collect([Response(p), Response(p)])
        source_bytes = (self.raw / "collection.json").read_bytes()
        with patch("highmark_events.requests.get", side_effect=AssertionError("no network")) as get:
            replayed = reparse_archive(self.raw, self.raw / "reparsed")
            get.assert_not_called()
        self.assertTrue(replayed["provider_complete"])
        self.assertEqual(replayed["network_requests"], 0)
        self.assertEqual(replayed["sources"][0]["retrieved_at"], original["sources"][0]["retrieved_at"])
        self.assertEqual(replayed["sources"][0]["raw_sha256"], original["sources"][0]["raw_sha256"])
        self.assertEqual((self.raw / "collection.json").read_bytes(), source_bytes)

    def test_offline_reparse_rejects_tampered_or_missing_archive_pages(self):
        p = payload([row()])
        self.collect([Response(p), Response(p)])
        raw_path = self.raw / "page-001.json"
        saved = json.loads(raw_path.read_text())
        saved["raw_sha256"] = "0" * 64
        raw_path.write_text(json.dumps(saved))
        with patch("highmark_events.requests.get", side_effect=AssertionError("no network")):
            result = reparse_archive(self.raw, self.raw / "reparsed")
        self.assertFalse(result["provider_complete"])
        self.assertIn("archive_fingerprint_mismatch", result["missing"])


if __name__ == "__main__":
    unittest.main()
