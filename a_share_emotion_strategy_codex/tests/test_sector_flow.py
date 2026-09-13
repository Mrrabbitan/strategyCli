import datetime as dt
import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sector_flow


def main_source(rows, available=True):
    return {
        "source": "主力", "source_url": "https://example.test/main", "available": available,
        "updated_at": "2026-08-25T10:30:00+08:00", "limitations": "test", "rows": rows, "error": "" if available else "down",
    }


class SectorFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = contextlib.ExitStack()
        self.patches.enter_context(patch.object(sector_flow, "private_path", side_effect=lambda p: self.root / p))
        self.patches.enter_context(patch.object(sector_flow, "research_path", side_effect=lambda p: self.root / "research" / p))

    def tearDown(self):
        self.patches.close()
        self.temp.cleanup()

    def test_complete_sources_sorts_and_accumulates_five_days(self):
        rows = [
            {"sector": "银行", "code": "BK1", "main_net_inflow_cny": 2e8, "main_net_inflow_ratio_pct": 2, "pct": 1, "amount_cny": 8e8},
            {"sector": "半导体", "code": "BK2", "main_net_inflow_cny": -1e8, "main_net_inflow_ratio_pct": -1, "pct": -1, "amount_cny": 6e8},
        ]
        day = dt.date(2026, 8, 25)
        cache = self.root / "cache" / "sector_flow"
        cache.mkdir(parents=True)
        for offset in range(1, 5):
            past = day - dt.timedelta(days=offset)
            sector_flow._main_snapshot_path(past).write_text(
                '{"date":"' + past.isoformat() + '","rows":' + __import__("json").dumps(rows) + '}', encoding="utf-8"
            )
        etf = {"source": "ETF", "source_url": "https://example.test/etf", "available": True, "updated_at": "now", "limitations": "test", "one_day_cny": {"银行": 1e7}, "five_day_cny": {"银行": 4e7}, "observed_dates": ["a", "b"], "errors": []}
        north = {"source": "北向", "source_url": "https://example.test/north", "available": True, "updated_at": "now", "as_of": "2026-08-24", "limitations": "test", "one_day_cny": {"银行": 2e7}, "five_day_cny": {"银行": 5e7}, "error": ""}
        with patch.object(sector_flow, "fetch_main_sector_flow", return_value=main_source(rows)), patch.object(sector_flow, "build_etf_flow", return_value=etf), patch.object(sector_flow, "build_northbound_flow", return_value=north):
            report = sector_flow.build_sector_flow(day, "10_30", {}, persist_state=True)
        self.assertEqual(report["sector_rows"][0]["sector"], "银行")
        self.assertEqual(report["sector_rows"][0]["main_five_day_cny"], 1e9)
        self.assertEqual(report["sector_rows"][0]["etf_one_day_cny"], 1e7)
        self.assertIn("资金流向", sector_flow.render_sector_flow_section(report))

    def test_single_source_failure_is_explicit_and_other_rows_remain(self):
        rows = [{"sector": "银行", "code": "BK1", "main_net_inflow_cny": 1e8, "main_net_inflow_ratio_pct": 1, "pct": 0.3, "amount_cny": 2e8}]
        unavailable_etf = {"source": "ETF", "source_url": "https://example.test/etf", "available": False, "updated_at": "", "limitations": "test", "one_day_cny": {}, "five_day_cny": {}, "observed_dates": [], "errors": ["down"]}
        north = {"source": "北向", "source_url": "https://example.test/north", "available": True, "updated_at": "now", "as_of": "2026-08-24", "limitations": "test", "one_day_cny": {}, "five_day_cny": {}, "error": ""}
        with patch.object(sector_flow, "fetch_main_sector_flow", return_value=main_source(rows)), patch.object(sector_flow, "build_etf_flow", return_value=unavailable_etf), patch.object(sector_flow, "build_northbound_flow", return_value=north):
            report = sector_flow.build_sector_flow(dt.date(2026, 8, 25), "10_30", {}, persist_state=False)
        self.assertFalse(report["sources"]["etf"]["available"])
        self.assertEqual(len(report["sector_rows"]), 1)
        self.assertIn("ETF：不可用", sector_flow.render_sector_flow_section(report))

    def test_all_sources_missing_never_creates_proxy_flow(self):
        unavailable = main_source([], available=False)
        empty_etf = {"source": "ETF", "source_url": "https://example.test/etf", "available": False, "updated_at": "", "limitations": "test", "one_day_cny": {}, "five_day_cny": {}, "observed_dates": [], "errors": ["down"]}
        empty_north = {"source": "北向", "source_url": "https://example.test/north", "available": False, "updated_at": "", "as_of": "", "limitations": "test", "one_day_cny": {}, "five_day_cny": {}, "error": "down"}
        with patch.object(sector_flow, "fetch_main_sector_flow", return_value=unavailable), patch.object(sector_flow, "build_etf_flow", return_value=empty_etf), patch.object(sector_flow, "build_northbound_flow", return_value=empty_north):
            report = sector_flow.build_sector_flow(dt.date(2026, 8, 25), "10_30", {}, persist_state=False)
        rendered = sector_flow.render_sector_flow_section(report)
        self.assertEqual(report["sector_rows"], [])
        self.assertIn("未以成交额替代净流数据", rendered)
        self.assertNotIn("+0.00亿", rendered)

    def test_public_etf_market_input_never_requires_account_report(self):
        day = dt.date(2026, 9, 11)
        market = {"universe": {"510001": {
            "code": "510001", "name": "虚构行业基金", "group": "bank", "quote": {"price": 2.0}
        }}, "data_quality": {"errors": []}}
        snapshots = [
            {"date": "2026-09-11", "rows": [{"code": "510001", "shares": 1200}]},
            {"date": "2026-09-10", "rows": [{"code": "510001", "shares": 1000}]},
        ]
        with patch.object(sector_flow, "_parse_fund_shares", return_value=(1200, "https://example.test/fund")), \
                patch.object(sector_flow, "_load_etf_snapshots", return_value=snapshots):
            result = sector_flow.build_etf_flow(day, market, persist_state=False)
        self.assertTrue(result["available"])
        self.assertEqual(result["one_day_cny"], {"银行": 400.0})
        self.assertFalse(sector_flow._history_dir().exists())

    def test_etf_quote_failure_is_reported_without_substitute_prices(self):
        market = {"universe": {}, "data_quality": {"errors": ["public ETF quote unavailable"]}}
        with patch.object(sector_flow, "fetch_sector_etf_market", return_value=market), \
                patch.object(sector_flow, "_parse_fund_shares") as shares:
            result = sector_flow.build_etf_flow(dt.date(2026, 9, 11), None, persist_state=False)
        self.assertFalse(result["available"])
        self.assertEqual(result["one_day_cny"], {})
        self.assertIn("public ETF quote unavailable", result["errors"])
        shares.assert_not_called()


if __name__ == "__main__":
    unittest.main()
