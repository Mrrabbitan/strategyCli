"""Fictional-calendar tests; no account or private fixtures."""
import datetime as dt
import unittest
from unittest.mock import patch

import market_calendar


class MarketCalendarTests(unittest.TestCase):
    def setUp(self):
        self.calendar = {"closures": {"2026": ["2026-10-01", "2026-10-02"]}}

    def test_weekend_holiday_and_previous_session(self):
        with patch.object(market_calendar, "load_config", return_value=self.calendar):
            self.assertTrue(market_calendar.is_trading_day(dt.date(2026, 9, 30))[0])
            self.assertFalse(market_calendar.is_trading_day(dt.date(2026, 10, 1))[0])
            self.assertFalse(market_calendar.is_trading_day(dt.date(2026, 10, 3))[0])
            self.assertEqual(market_calendar.previous_trading_day(dt.date(2026, 10, 5)), dt.date(2026, 9, 30))

    def test_missing_calendar_year_and_broken_source_fail_closed(self):
        with patch.object(market_calendar, "load_config", return_value=self.calendar):
            self.assertFalse(market_calendar.is_trading_day(dt.date(2027, 1, 4))[0])
            with self.assertRaises(market_calendar.MarketCalendarError):
                market_calendar.previous_trading_day(dt.date(2027, 2, 1))
        with patch.object(market_calendar, "load_config", side_effect=OSError("unavailable")):
            self.assertFalse(market_calendar.is_trading_day(dt.date(2026, 9, 30))[0])


if __name__ == "__main__":
    unittest.main()
