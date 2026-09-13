"""Public market quotes and history; no account, holdings, or trading state."""
from __future__ import annotations
import datetime as dt
import json
import math
import re
import time
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Tuple
from research_store import load_config

TENCENT_QUOTE = "https://qt.gtimg.cn/q="
TENCENT_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"

class MarketDataError(RuntimeError):
    pass

def _get(url: str, timeout: int = 20, encoding: Optional[str] = None) -> Any:
    last: Optional[Exception] = None
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://fund.eastmoney.com/"})
            raw = urllib.request.urlopen(request, timeout=timeout).read()
            return raw.decode(encoding or "utf-8", errors="replace")
        except Exception as exc:
            last = exc
            if attempt < 2:
                time.sleep(0.6 * (attempt + 1))
    raise MarketDataError(f"request failed: {url}: {last}")


def _symbol(code: str) -> str:
    return ("sh" if code.startswith(("5", "6", "9")) else "sz") + code


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def fetch_quotes(codes: Iterable[str]) -> Tuple[Dict[str, Dict[str, Any]], str]:
    raw = _get(TENCENT_QUOTE + ",".join(_symbol(code) for code in codes), encoding="gb18030")
    quotes: Dict[str, Dict[str, Any]] = {}
    newest = ""
    for line in raw.splitlines():
        match = re.search(r'="(.*)";', line)
        if not match:
            continue
        parts = match.group(1).split("~")
        if len(parts) < 39:
            continue
        code, stamp = parts[2], parts[30]
        newest = max(newest, stamp)
        quotes[code] = {
            "code": code,
            "name": parts[1],
            "price": _float(parts[3]),
            "prev_close": _float(parts[4]),
            "timestamp": stamp,
            "pct": _float(parts[32]),
            "amount_cny": _float(parts[37]) * 10000,
        }
    if not quotes:
        raise MarketDataError("Tencent quote feed returned no records")
    return quotes, newest


def fetch_history(code: str, count: int = 180) -> List[Dict[str, Any]]:
    symbol = _symbol(code)
    url = f"{TENCENT_KLINE}?param={symbol},day,,,{count},qfq"
    payload = json.loads(_get(url))
    node = (payload.get("data") or {}).get(symbol) or {}
    rows = node.get("qfqday") or node.get("day") or []
    history = []
    for row in rows:
        if len(row) < 6:
            continue
        close, volume_lots = _float(row[2]), _float(row[5])
        history.append({
            "date": row[0],
            "open": _float(row[1]),
            "close": close,
            "high": _float(row[3]),
            "low": _float(row[4]),
            "volume_lots": volume_lots,
            "turnover_cny_est": close * volume_lots * 100,
        })
    if len(history) < 121:
        raise MarketDataError(f"{code} history has only {len(history)} rows")
    return history



def fetch_sector_etf_market(as_of: dt.date) -> Dict[str, Any]:
    """Fetch a public sector proxy universe independently of any portfolio.

    Only same-date valid prices enter the share-change estimator. The input
    universe contains public instrument metadata, never positions or budgets.
    """
    try:
        configuration = load_config("sector_market.json")
        if not isinstance(configuration, dict):
            raise MarketDataError("invalid public ETF configuration")
        configured = configuration.get("etf_universe", [])
        if not isinstance(configured, list):
            raise MarketDataError("public ETF universe must be a list")
        instruments = {}
        for item in configured:
            if not isinstance(item, dict):
                raise MarketDataError("invalid public ETF instrument")
            code = str(item.get("code", ""))
            if not re.fullmatch(r"(?:5|1)\d{5}", code) or code in instruments:
                raise MarketDataError("invalid or duplicate public ETF code")
            instruments[code] = {key: item[key] for key in ("code", "name", "group")}
        if not instruments:
            raise MarketDataError("public ETF universe is empty")
        quotes, _ = fetch_quotes(instruments)
        universe, errors = {}, []
        for code, item in instruments.items():
            quote = quotes.get(code) or {}
            price = quote.get("price", 0)
            if (str(quote.get("timestamp", ""))[:8] != as_of.strftime("%Y%m%d")
                    or not math.isfinite(price) or price <= 0):
                errors.append(f"{code} public ETF quote missing or date mismatch")
                continue
            universe[code] = dict(item, quote=quote)
        return {"as_of": as_of.isoformat(), "universe": universe,
                "data_quality": {"status": "verified" if not errors else "degraded", "errors": errors}}
    except (OSError, ValueError, KeyError, TypeError, MarketDataError) as exc:
        return {"as_of": as_of.isoformat(), "universe": {},
                "data_quality": {"status": "unavailable", "errors": [str(exc)]}}
