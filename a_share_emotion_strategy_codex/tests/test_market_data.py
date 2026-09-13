"""Artificial market fixtures test units and provenance, not returns."""
import datetime as dt
import json
import unittest
from unittest.mock import patch

import market_data


class PublicMarketDataTests(unittest.TestCase):
    def history(self, code, count=121):
        start = dt.date(2025, 1, 1)
        rows = []
        day = start
        while len(rows) < count:
            if day.weekday() < 5:
                rows.append([day.isoformat(), "10", "11", "12", "9", "300"])
            day += dt.timedelta(days=1)
        symbol = market_data._symbol(code)
        return json.dumps({"data": {symbol: {"qfqday": rows}}})

    def test_historical_adapter_preserves_existing_units_and_fields(self):
        for code in ("600001", "688001"):
            with patch.object(market_data, "_get", return_value=self.history(code)):
                rows = market_data.fetch_history(code)
            self.assertEqual(len(rows), 121)
            self.assertEqual(rows[-1]["volume_lots"], 300)
            self.assertEqual(rows[-1]["turnover_cny_est"], 11 * 300 * 100)
            self.assertEqual([rows[-1][k] for k in ("open", "close", "high", "low")], [10, 11, 12, 9])

    def test_short_history_is_not_silently_accepted(self):
        with patch.object(market_data, "_get", return_value=self.history("600001", 120)):
            with self.assertRaises(market_data.MarketDataError):
                market_data.fetch_history("600001")

    def test_sector_proxy_quotes_are_account_free_and_date_checked(self):
        config = {"etf_universe": [
            {"code": "510001", "name": "虚构行业基金甲", "group": "bank"},
            {"code": "510002", "name": "虚构行业基金乙", "group": "consumer"},
        ]}
        quotes = {
            "510001": {"price": 2.0, "timestamp": "20260911150000"},
            "510002": {"price": 3.0, "timestamp": "20260910150000"},
        }
        with patch.object(market_data, "load_config", return_value=config) as loader, \
                patch.object(market_data, "fetch_quotes", return_value=(quotes, "20260911150000")):
            result = market_data.fetch_sector_etf_market(dt.date(2026, 9, 11))
        loader.assert_called_once_with("sector_market.json")
        self.assertEqual(set(result["universe"]), {"510001"})
        self.assertEqual(result["data_quality"]["status"], "degraded")
        self.assertNotIn("portfolio_snapshot", result)
        self.assertNotIn("etf_sleeve", result)

    def test_missing_quotes_and_nonfinite_prices_are_unavailable(self):
        config = {"etf_universe": [{"code": "510001", "name": "虚构行业基金", "group": "bank"}]}
        with patch.object(market_data, "load_config", return_value=config), \
                patch.object(market_data, "fetch_quotes", side_effect=market_data.MarketDataError("down")):
            result = market_data.fetch_sector_etf_market(dt.date(2026, 9, 11))
        self.assertEqual(result["universe"], {})
        self.assertEqual(result["data_quality"]["status"], "unavailable")
        with patch.object(market_data, "load_config", return_value=config), \
                patch.object(market_data, "fetch_quotes", return_value=({"510001": {"price": float("inf"), "timestamp": "20260911150000"}}, "")):
            self.assertEqual(market_data.fetch_sector_etf_market(dt.date(2026, 9, 11))["universe"], {})


if __name__ == "__main__":
    unittest.main()
