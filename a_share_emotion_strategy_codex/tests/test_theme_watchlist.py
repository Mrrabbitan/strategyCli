import datetime as dt
import copy
import unittest

from theme_watchlist import build_theme_watchlist_report


class ThemeWatchlistTests(unittest.TestCase):
    def setUp(self):
        self.payload = {
            "watchlists": [{
                "id": "test", "label": "test", "start_date": "2026-09-14", "end_date": "2026-09-30",
                "activation_policy": {
                    "min_consecutive_closes_above_ma20": 2, "min_volume_ratio_20d": 1.3,
                    "min_group_qualified_count": 1, "max_open_or_daily_gain_pct": 7,
                    "max_distance_above_ma20_pct": 8, "verified_catalyst_required": True,
                },
                "items": [{"code": "300373", "name": "虚构功率样本", "group": "功率半导体", "tier": "核心"}],
            }]
        }
        self.history = []
        day = dt.date(2026, 8, 1)
        for index in range(45):
            date = day + dt.timedelta(days=index)
            close = 10 + index * 0.05
            self.history.append({
                "date": date.isoformat(), "close": close, "open": close,
                "high": close * 1.01, "low": close * 0.99, "turnover_cny_est": 100_000_000,
            })
        self.quote = {"price": self.history[-1]["close"], "pct": 2, "amount_cny": 150_000_000, "timestamp": "20260915150000"}

    def test_pre_window_never_activates(self):
        result = build_theme_watchlist_report(
            dt.date(2026, 9, 1), {"300373": self.quote}, {"300373": self.quote},
            histories={"300373": self.history}, payload=self.payload,
        )
        item = result["watchlists"][0]["items"][0]
        self.assertEqual(item["status"], "预备观察")
        self.assertFalse(item["activation_ready"])
        self.assertFalse(result["auto_trade_eligible"])

    def test_active_window_still_requires_verified_catalyst(self):
        result = build_theme_watchlist_report(
            dt.date(2026, 9, 15), {"300373": self.quote}, {"300373": self.quote},
            histories={"300373": self.history}, payload=self.payload,
        )
        item = result["watchlists"][0]["items"][0]
        self.assertTrue(item["technical_qualified"])
        self.assertFalse(item["activation_ready"])
        self.assertEqual(item["status"], "量价联动通过，等待合格催化")

    def test_intraday_bar_is_not_treated_as_completed_close(self):
        quote = dict(self.quote, timestamp="20260915103000")
        result = build_theme_watchlist_report(
            dt.date(2026, 9, 15), {"300373": quote}, {"300373": quote},
            histories={"300373": self.history}, payload=self.payload,
        )
        item = result["watchlists"][0]["items"][0]
        self.assertTrue(item["consecutive_above_ma20"])
        self.assertEqual(item["quote_timestamp"], "20260915103000")

    def test_star_market_history_scale_remains_applied_once(self):
        payload = copy.deepcopy(self.payload)
        payload["watchlists"][0]["items"][0]["code"] = "688001"
        histories = [dict(row, turnover_cny_est=row["turnover_cny_est"] * 100) for row in self.history]
        result = build_theme_watchlist_report(
            dt.date(2026, 9, 15), {"688001": self.quote}, {"688001": self.quote},
            histories={"688001": histories}, payload=payload,
        )
        self.assertEqual(result["watchlists"][0]["items"][0]["volume_ratio_20d"], 1.5)


if __name__ == "__main__":
    unittest.main()
