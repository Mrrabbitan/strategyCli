"""Exchange trading calendar shared by research and the local monitor."""
from __future__ import annotations
import datetime as dt
from typing import Any, Dict, Optional, Tuple
from research_store import load_config

class MarketCalendarError(RuntimeError):
    pass

def is_trading_day(day: dt.date, config: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    try:
        calendar = config if config and "closures" in config else load_config("market_calendar.json")
        year = str(day.year)
        if year not in calendar["closures"]:
            return False, f"缺少{year}年交易所休市日历，安全跳过"
        if day.weekday() >= 5 or day.isoformat() in calendar["closures"][year]:
            return False, "周末或交易所公告休市日"
        return True, "交易日历通过"
    except (OSError, ValueError, KeyError, TypeError):
        return False, "交易所休市日历不可用，安全跳过"

def previous_trading_day(day: dt.date) -> dt.date:
    cursor = day - dt.timedelta(days=1)
    for _ in range(20):
        ok, _ = is_trading_day(cursor)
        if ok:
            return cursor
        cursor -= dt.timedelta(days=1)
    raise MarketCalendarError(f"cannot resolve previous trading day for {day}")
