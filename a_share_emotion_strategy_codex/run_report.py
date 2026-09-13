#!/usr/bin/env python3
"""Generate one of the five A-share emotion-strategy HTML reports.

The runner is deliberately research-only: it fetches public data, validates the
two quote feeds, applies the configured research rules, and renders HTML. It has
no brokerage integration and cannot place orders.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import math
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from market_calendar import (
    is_trading_day as calendar_is_trading_day,
    previous_trading_day,
)
from market_data import fetch_sector_etf_market
from research_store import private_path, load_config, atomic_json
from emotion_monitor import (
    active_auto_codes,
    build_degraded_emotion_report,
    build_emotion_report,
    load_catalyst_evidence,
    render_dynamic_targets_section,
    render_emotion_section,
)
from near_limit_monitor import (
    build_emotion_leader_report,
    build_degraded_near_limit_report,
    build_near_limit_report,
    build_preopen_near_limit_report,
    prefilter_emotion_leaders,
    prefilter_near_limit,
    render_near_limit_section,
)
from sector_flow import build_sector_flow, render_sector_flow_section
from hot_sector_leaders import build_hot_sector_context, filter_ranked_rows
from theme_watchlist import (
    build_theme_watchlist_report,
    load_theme_watchlists,
    render_theme_watchlist_section,
    theme_watchlist_codes,
)
from technology_leader import (
    build_degraded_technology_leader_report,
    build_technology_leader_report,
    prefilter_technology_leaders,
    render_technology_leader_section,
)
from abnormal_move_monitor import (
    build_abnormal_10d_report,
    fetch_beijing_snapshot,
    render_abnormal_10d_section,
)


ROOT = Path(__file__).resolve().parent
TZ = ZoneInfo("Asia/Shanghai")
SLOTS = {
    "09_00": "09_00_preopen.html",
    "10_30": "10_30_morning.html",
    "13_30": "13_30_afternoon.html",
    "14_30": "14_30_preclose.html",
    "19_00": "19_00_review.html",
}
TENCENT_URL = "https://qt.gtimg.cn/q="
SINA_QUOTE_URL = "https://hq.sinajs.cn/list="
SINA_LIST_URL = "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
SINA_COUNT_URL = "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeStockCount"
EASTMONEY_POOL_URL = "https://push2ex.eastmoney.com/getTopicZTPool"
EASTMONEY_LHB_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
EASTMONEY_ANN_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
EASTMONEY_STOCK_PROFILE_URL = "https://push2.eastmoney.com/api/qt/stock/get"
TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
CFFEX_URL = "http://www.cffex.com.cn/sj/ccpm/{ym}/{day}/{var}_1.csv"
SSE_MARGIN_URL = "https://query.sse.com.cn/marketdata/tradedata/queryMargin.do"
SZSE_MARGIN_URL = "https://www.szse.cn/api/report/ShowReport/data"
ETF_PROXIES = {
    "510300": "沪深300ETF",
    "510050": "上证50ETF",
    "510500": "中证500ETF",
    "159915": "创业板ETF",
    "512100": "中证1000ETF",
}
RISK_WORDS = ("减持", "立案", "处罚", "监管", "问询", "风险提示", "异常波动", "亏损", "退市")


class DataError(RuntimeError):
    pass


def request_bytes(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = 15,
    extra_headers: Optional[Dict[str, str]] = None,
) -> bytes:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer": "https://quote.eastmoney.com/",
    }
    if extra_headers:
        headers.update(extra_headers)
    last: Optional[Exception] = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as res:
                return res.read()
        except Exception as exc:  # network errors vary by platform
            last = exc
            if attempt < 2:
                time.sleep(0.7 * (attempt + 1))
    raise DataError(f"request failed after retries: {url}: {last}")


def request_json(url: str, params: Optional[Dict[str, Any]] = None, timeout: int = 15) -> Dict[str, Any]:
    raw = request_bytes(url, params=params, timeout=timeout)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise DataError(f"invalid JSON from {url}: {exc}") from exc


def market_symbol(code: str) -> str:
    if code == "000001":
        return "sh000001"
    return ("sh" if code.startswith(("5", "6", "9")) else "sz") + code


def secid(code: str) -> str:
    return ("1." if code.startswith(("5", "6", "9")) else "0.") + code


def fnum(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def inum(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def fetch_tencent_quotes(codes: Iterable[str]) -> Tuple[Dict[str, Dict[str, Any]], str]:
    symbols = [market_symbol(code) for code in codes]
    raw = request_bytes(TENCENT_URL + ",".join(symbols)).decode("gb18030", errors="replace")
    quotes: Dict[str, Dict[str, Any]] = {}
    newest = ""
    for line in raw.splitlines():
        match = re.search(r'="(.*)";', line)
        if not match:
            continue
        parts = match.group(1).split("~")
        if len(parts) < 39:
            continue
        code = parts[2]
        stamp = parts[30]
        newest = max(newest, stamp)
        quotes[code] = {
            "name": parts[1],
            "code": code,
            "price": fnum(parts[3]),
            "prev_close": fnum(parts[4]),
            "open": fnum(parts[5]),
            "volume_lots": inum(parts[6]),
            "buy_lots": inum(parts[7]),
            "sell_lots": inum(parts[8]),
            "timestamp": stamp,
            "change": fnum(parts[31]),
            "pct": fnum(parts[32]),
            "high": fnum(parts[33]),
            "low": fnum(parts[34]),
            "amount_cny": fnum(parts[37]) * 10000,
            "turnover": fnum(parts[38]),
            "source": TENCENT_URL + market_symbol(code),
        }
    if not quotes:
        raise DataError("Tencent quote feed returned no parseable records")
    return quotes, newest


def fetch_sina_quotes(codes: Iterable[str]) -> Tuple[Dict[str, Dict[str, Any]], str]:
    symbols = [market_symbol(code) for code in codes]
    raw = request_bytes(
        SINA_QUOTE_URL + ",".join(symbols),
        extra_headers={"Referer": "https://finance.sina.com.cn/"},
    ).decode("gb18030", errors="replace")
    quotes: Dict[str, Dict[str, Any]] = {}
    newest = ""
    for line in raw.splitlines():
        symbol_match = re.match(r"var hq_str_(\w+)=", line)
        value_match = re.search(r'="(.*)";', line)
        if not symbol_match or not value_match:
            continue
        symbol = symbol_match.group(1)
        parts = value_match.group(1).split(",")
        if len(parts) < 32:
            continue
        code = symbol[-6:]
        previous = fnum(parts[2])
        price = fnum(parts[3])
        stamp = parts[30].replace("-", "") + parts[31].replace(":", "")
        newest = max(newest, stamp)
        quotes[code] = {
            "name": parts[0],
            "code": code,
            "price": price,
            "prev_close": previous,
            "open": fnum(parts[1]),
            "pct": ((price / previous - 1) * 100) if previous else 0.0,
            "high": fnum(parts[4]),
            "low": fnum(parts[5]),
            "volume_shares": inum(parts[8]),
            "amount_cny": fnum(parts[9]),
            "timestamp": stamp,
            "source": SINA_QUOTE_URL + symbol,
        }
    if not quotes:
        raise DataError("Sina quote feed returned no parseable records")
    return quotes, newest


def fetch_market_list() -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    cache_path = private_path("data/cache/market_list.json")

    def fresh_cache() -> Optional[Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]]:
        if not cache_path.exists():
            return None
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            fetched_at = dt.datetime.fromisoformat(str(cached.get("fetched_at", "")))
            age_seconds = (dt.datetime.now() - fetched_at).total_seconds()
            if fetched_at.date() != dt.date.today() or not (0 <= age_seconds <= 600):
                return None
            breadth = dict(cached["breadth"])
            breadth["cache_used"] = True
            breadth["cache_age_seconds"] = round(age_seconds, 1)
            return dict(cached["market"]), breadth
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return None

    try:
        raw_count = request_bytes(
            SINA_COUNT_URL, {"node": "hs_a"}, extra_headers={"Referer": "https://finance.sina.com.cn/"}
        ).decode("utf-8", errors="replace")
    except DataError:
        cached = fresh_cache()
        if cached:
            return cached
        raise
    match = re.search(r"\d+", raw_count)
    if not match:
        raise DataError("Sina market count returned no number")
    expected_count = int(match.group())
    pages = math.ceil(expected_count / 80)
    base_params = {"num": 80, "sort": "symbol", "asc": 1, "node": "hs_a", "symbol": "", "_s_r_a": "page"}
    page_rows: Dict[int, List[Dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {}
        for page in range(1, pages + 1):
            params = dict(base_params)
            params["page"] = page
            futures[executor.submit(
                request_bytes, SINA_LIST_URL, params, 15, {"Referer": "https://finance.sina.com.cn/"}
            )] = page
        for future in as_completed(futures):
            page = futures[future]
            try:
                page_rows[page] = list(json.loads(future.result().decode("utf-8")))
            except Exception:
                page_rows[page] = []
    rows: List[Dict[str, Any]] = []
    for page in range(1, pages + 1):
        rows.extend(page_rows.get(page, []))
    market: Dict[str, Dict[str, Any]] = {}
    up = down = flat = 0
    for row in rows:
        code = str(row.get("code", ""))
        pct = fnum(row.get("changepercent"), math.nan)
        if not code or math.isnan(pct):
            continue
        if pct > 0:
            up += 1
        elif pct < 0:
            down += 1
        else:
            flat += 1
        market[code] = {
            "name": row.get("name", ""),
            "price": fnum(row.get("trade")),
            "pct": pct,
            "amount_cny": fnum(row.get("amount")),
            "volume": fnum(row.get("volume")),
            "turnover": fnum(row.get("turnoverratio")),
            "high": fnum(row.get("high")),
            "low": fnum(row.get("low")),
            "open": fnum(row.get("open")),
            "prev_close": fnum(row.get("settlement")),
            "ticktime": str(row.get("ticktime", "")),
        }
    if not market:
        raise DataError("Sina market list returned no records")
    missing_pages = [page for page in range(1, pages + 1) if not page_rows.get(page)]
    if len(market) / max(expected_count, 1) < 0.98:
        cached = fresh_cache()
        if cached:
            return cached
    breadth = {
        "up": up, "down": down, "flat": flat, "total": up + down + flat,
        "expected_count": expected_count,
        "coverage_ratio": len(market) / max(expected_count, 1),
        "missing_pages": missing_pages,
        "cache_used": False,
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache_path.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "fetched_at": dt.datetime.now().isoformat(timespec="seconds"), "market": market, "breadth": breadth,
    }, ensure_ascii=False), encoding="utf-8")
    temporary.replace(cache_path)
    return market, breadth


def fetch_near_limit_metadata(
    code: str, report_date: dt.date, cached_profile: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Fetch an auditable industry label and verify at least six daily bars."""
    cached_profile = cached_profile or {}
    industry = str(cached_profile.get("industry") or "").strip()
    listing_date = str(cached_profile.get("listing_date") or "")
    profile_fetched = False
    if not industry:
        profile = request_json(EASTMONEY_STOCK_PROFILE_URL, {
            "secid": secid(code),
            "fields": "f26,f57,f58,f127,f128,f129",
        }).get("data") or {}
        industry = str(profile.get("f127") or "").strip()
        listing_date = str(profile.get("f26") or "")
        profile_fetched = bool(industry)
    symbol = market_symbol(code)
    kline = request_json(TENCENT_KLINE_URL, {"param": f"{symbol},day,,,10,qfq"})
    node = ((kline.get("data") or {}).get(symbol) or {})
    history = node.get("qfqday") or node.get("day") or []
    completed = [row for row in history if row and str(row[0]) < report_date.isoformat()]
    prior_streak = 0
    for index in range(len(completed) - 1, 0, -1):
        prior_close = fnum(completed[index - 1][2])
        close = fnum(completed[index][2])
        if prior_close > 0 and close / prior_close >= 1.095:
            prior_streak += 1
        else:
            break
    return {
        "industry": industry,
        "listing_date": listing_date,
        "profile_fetched": profile_fetched,
        "history_count": len(history),
        "listing_age_verified": len(history) >= 6,
        "prior_consecutive_limits": prior_streak,
    }


def enrich_near_limit_candidates(
    candidates: List[Dict[str, Any]], previous_pool_map: Dict[str, Dict[str, Any]], report_date: dt.date,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    enriched: List[Dict[str, Any]] = []
    errors: List[str] = []
    metadata: Dict[str, Dict[str, Any]] = {}
    cache_path = private_path("data/cache/security_profiles.json")
    try:
        profile_cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        profile_cache = {}
    cutoff = report_date - dt.timedelta(days=90)
    valid_cache: Dict[str, Dict[str, Any]] = {}
    for code, row in profile_cache.items():
        try:
            if dt.date.fromisoformat(str(row.get("fetched_at", ""))) >= cutoff and row.get("industry"):
                valid_cache[code] = row
        except ValueError:
            continue
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(
                fetch_near_limit_metadata, str(item["c"]), report_date, valid_cache.get(str(item["c"]))
            ): str(item["c"])
            for item in candidates
        }
        for future in as_completed(futures):
            code = futures[future]
            try:
                metadata[code] = future.result()
            except Exception as exc:
                metadata[code] = {
                    "industry": "", "history_count": 0, "listing_age_verified": False,
                    "prior_consecutive_limits": 0,
                }
                errors.append(f"{code}题材/上市日校验失败：{exc}")
    cache_changed = False
    for code, info in metadata.items():
        if info.get("industry") and (info.get("profile_fetched") or code not in valid_cache):
            profile_cache[code] = {
                "industry": info["industry"], "listing_date": info.get("listing_date", ""),
                "fetched_at": report_date.isoformat(), "source": EASTMONEY_STOCK_PROFILE_URL,
            }
            cache_changed = True
    if cache_changed:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(profile_cache, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(cache_path)
    for item in candidates:
        code = str(item["c"])
        info = metadata.get(code, {})
        prior_theme = str(previous_pool_map.get(code, {}).get("hybk") or "").strip()
        prior_boards = (
            inum(previous_pool_map[code].get("lbc"))
            if code in previous_pool_map else inum(info.get("prior_consecutive_limits"))
        )
        prospective_board = prior_boards + 1
        if prospective_board not in {1, 2}:
            continue
        enriched.append({
            **item,
            "prior_boards": prior_boards,
            "prospective_board": prospective_board,
            "hybk": prior_theme or str(info.get("industry") or "").strip(),
            "listing_date": str(info.get("listing_date") or ""),
            "listing_history_count": inum(info.get("history_count")),
            "listing_age_verified": bool(info.get("listing_age_verified")),
            "theme_up3_count": 0,
            "theme_breadth_ratio": 0.0,
        })
    return enriched, errors


def enrich_leader_candidates(
    candidates: List[Dict[str, Any]], report_date: dt.date,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Verify leader listing age without applying the 1-2 board near-limit filter."""
    metadata: Dict[str, Dict[str, Any]] = {}
    errors: List[str] = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(
                fetch_near_limit_metadata,
                str(item["c"]),
                report_date,
                {"industry": str(item.get("hybk") or ""), "listing_date": ""},
            ): str(item["c"])
            for item in candidates
        }
        for future in as_completed(futures):
            code = futures[future]
            try:
                metadata[code] = future.result()
            except Exception as exc:
                metadata[code] = {"history_count": 0, "listing_age_verified": False}
                errors.append(f"{code}龙头上市时长校验失败：{exc}")
    enriched: List[Dict[str, Any]] = []
    for item in candidates:
        code = str(item["c"])
        info = metadata.get(code, {})
        enriched.append({
            **item,
            "listing_history_count": inum(info.get("history_count")),
            "listing_age_verified": bool(info.get("listing_age_verified")),
        })
    return enriched, errors


def fetch_limit_pool(date: dt.date) -> Tuple[List[Dict[str, Any]], str]:
    params = {
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "dpt": "wz.ztzt",
        "Pageindex": 0,
        "pagesize": 200,
        "sort": "fbt:asc",
        "date": date.strftime("%Y%m%d"),
    }
    payload = request_json(EASTMONEY_POOL_URL, params)
    data = payload.get("data") or {}
    return data.get("pool") or [], str(data.get("qdate") or "")


def fetch_lhb(date: dt.date) -> Dict[str, Dict[str, Any]]:
    params = {
        "reportName": "RPT_DAILYBILLBOARD_DETAILSNEW",
        "columns": "ALL",
        "filter": f"(TRADE_DATE='{date.isoformat()}')",
        "pageNumber": 1,
        "pageSize": 500,
    }
    payload = request_json(EASTMONEY_LHB_URL, params)
    rows = (((payload.get("result") or {}).get("data")) or [])
    result: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        code = str(row.get("SECURITY_CODE", ""))
        current = result.get(code)
        if not current or abs(fnum(row.get("BILLBOARD_NET_AMT"))) > abs(fnum(current.get("BILLBOARD_NET_AMT"))):
            result[code] = row
    return result


def fetch_announcements(code: str) -> List[Dict[str, Any]]:
    params = {
        "sr": -1,
        "page_size": 10,
        "page_index": 1,
        "ann_type": "A",
        "client_source": "web",
        "stock_list": code,
    }
    payload = request_json(EASTMONEY_ANN_URL, params)
    return ((payload.get("data") or {}).get("list") or [])


def recent_risk_titles(items: List[Dict[str, Any]], asof: dt.date, days: int = 7) -> List[str]:
    result: List[str] = []
    cutoff = asof - dt.timedelta(days=days)
    for item in items:
        stamp = str(item.get("display_time") or item.get("notice_date") or "")[:10]
        try:
            item_date = dt.date.fromisoformat(stamp)
        except ValueError:
            item_date = asof
        title = str(item.get("title") or "")
        if item_date >= cutoff and any(word in title for word in RISK_WORDS):
            result.append(title)
    return result[:3]


def fetch_cffex(date: dt.date) -> Dict[str, Dict[str, Any]]:
    summaries: Dict[str, Dict[str, Any]] = {}
    for var in ("IF", "IH", "IC", "IM"):
        url = CFFEX_URL.format(ym=date.strftime("%Y%m"), day=date.strftime("%d"), var=var)
        raw = request_bytes(url).decode("gb18030", errors="replace")
        members: Dict[str, Dict[str, int]] = defaultdict(lambda: {"long": 0, "long_chg": 0, "short": 0, "short_chg": 0})
        contracts = set()
        for row in csv.reader(raw.splitlines()):
            if len(row) < 12 or not row[0].isdigit() or not row[1].startswith(var):
                continue
            contracts.add(row[1])
            long_name, short_name = row[6].strip(), row[9].strip()
            members[long_name]["long"] += inum(row[7])
            members[long_name]["long_chg"] += inum(row[8])
            members[short_name]["short"] += inum(row[10])
            members[short_name]["short_chg"] += inum(row[11])
        if not contracts:
            continue
        ranked = []
        for name, values in members.items():
            values = dict(values)
            values["name"] = name
            values["net"] = values["long"] - values["short"]
            values["net_chg"] = values["long_chg"] - values["short_chg"]
            ranked.append(values)
        citic = next((x for x in ranked if x["name"].startswith("中信期货")), None)
        summaries[var] = {
            "contracts": sorted(contracts),
            "citic": citic,
            "top_net_long": sorted(ranked, key=lambda x: x["net"], reverse=True)[:3],
            "top_net_short": sorted(ranked, key=lambda x: x["net"])[:3],
            "source": url,
        }
    return summaries


def fetch_margin(date: dt.date) -> Dict[str, Any]:
    start = date - dt.timedelta(days=10)
    sse_params = {
        "isPagination": "true",
        "beginDate": start.strftime("%Y%m%d"),
        "endDate": date.strftime("%Y%m%d"),
        "tabType": "",
        "stockCode": "",
        "pageHelp.pageSize": 5000,
        "pageHelp.pageNo": 1,
        "pageHelp.beginPage": 1,
        "pageHelp.cacheSize": 1,
        "pageHelp.endPage": 5,
    }
    sse_payload = json.loads(request_bytes(
        SSE_MARGIN_URL,
        sse_params,
        extra_headers={"Referer": "https://www.sse.com.cn/"},
    ).decode("utf-8"))
    sse_rows = sse_payload.get("result") or []
    if not sse_rows:
        raise DataError("SSE margin feed has no recent published row")
    sse_row = max(sse_rows, key=lambda row: str(row.get("opDate", "")))
    published = str(sse_row.get("opDate"))
    szse_params = {
        "SHOWTYPE": "JSON",
        "CATALOGID": "1837_xxpl",
        "txtDate": f"{published[:4]}-{published[4:6]}-{published[6:]}",
        "tab1PAGENO": 1,
        "random": "0.7425245522795993",
    }
    szse_payload = json.loads(request_bytes(
        SZSE_MARGIN_URL,
        szse_params,
        extra_headers={"Referer": "https://www.szse.cn/disclosure/margin/object/index.html"},
    ).decode("utf-8"))
    szse_rows = (szse_payload[0].get("data") if szse_payload else []) or []
    if not szse_rows:
        raise DataError(f"SZSE margin feed has no row for {published}")
    szse_row = szse_rows[0]

    def sz_number(key: str) -> float:
        return fnum(str(szse_row.get(key, "0")).replace(",", "")) * 1e8

    sse_balance = fnum(sse_row.get("rzrqjyzl"))
    szse_balance = sz_number("jrrzrjye")
    return {
        "date": f"{published[:4]}-{published[4:6]}-{published[6:]}",
        "sse": {
            "financing_balance": fnum(sse_row.get("rzye")),
            "financing_buy": fnum(sse_row.get("rzmre")),
            "securities_balance_value": fnum(sse_row.get("rqylje")),
            "total_balance": sse_balance,
        },
        "szse": {
            "financing_balance": sz_number("jrrzye"),
            "financing_buy": sz_number("jrrzmr"),
            "securities_balance_value": sz_number("jrrjye"),
            "total_balance": szse_balance,
        },
        "combined_total_balance": sse_balance + szse_balance,
        "sources": {"sse": SSE_MARGIN_URL, "szse": SZSE_MARGIN_URL},
    }


def choose_slot(now: dt.datetime) -> str:
    minutes = now.hour * 60 + now.minute
    boundaries = [(9 * 60 + 45, "09_00"), (11 * 60 + 15, "10_30"),
                  (14 * 60 + 15, "13_30"), (16 * 60, "14_30")]
    for boundary, slot in boundaries:
        if minutes < boundary:
            return slot
    return "19_00"


def quote_time_fresh_for_slot(stamp: str, slot: str) -> bool:
    if slot == "09_00":
        return True
    if len(stamp) < 12 or not stamp[:12].isdigit():
        return False
    minute = int(stamp[8:10]) * 60 + int(stamp[10:12])
    minimums = {
        "10_30": 10 * 60 + 20,
        "13_30": 13 * 60 + 20,
        "14_30": 14 * 60 + 20,
        "19_00": 15 * 60,
    }
    return minute >= minimums[slot]


def money(value: float) -> str:
    if abs(value) >= 1e8:
        return f"{value / 1e8:.2f}亿元"
    value_wan = value / 1e4
    digits = 1 if abs(value_wan) < 10 else 0
    return f"{value_wan:.{digits}f}万元"


def signed_money(value: float) -> str:
    return ("+" if value > 0 else "") + money(value)


def link(label: str, url: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noreferrer">{html.escape(label)}</a>'


def td(value: Any) -> str:
    return f"<td>{value}</td>"


def load_editorial(path: Optional[Path]) -> Dict[str, Any]:
    if not path:
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def report_monitor_context(report_date: dt.date, slot: str, now: dt.datetime) -> Dict[str, Any]:
    """Freeze events at the requested report cutoff; never attach today's status to a historical day."""
    from monitor_feed import read_events, read_status

    observed = now.replace(tzinfo=TZ) if now.tzinfo is None else now.astimezone(TZ)
    hours, minutes = (int(part) for part in slot.split("_"))
    requested = dt.datetime.combine(report_date, dt.time(hours, minutes), tzinfo=TZ)
    cutoff = min(observed, requested)
    return {
        "monitor_cutoff": cutoff.isoformat(timespec="seconds"),
        "monitor_cutoff_policy": "不晚于实际生成时间及所选报告时点；服务状态另列自身as_of",
        "monitor_events": read_events(report_date, cutoff=cutoff),
        "monitor_status": read_status(now=observed) if report_date == observed.date() else None,
    }


def rebuild_dashboard(report_date: dt.date) -> None:
    from build_investment_site import build
    build(report_date)


def save_report_snapshot(path: Path, snapshot: Dict[str, Any], report_date: dt.date, *, refresh: bool) -> None:
    """Publish JSON atomically before the common builder reads the report index."""
    atomic_json(path, snapshot)
    if refresh:
        rebuild_dashboard(report_date)


def load_previous_audited_snapshot(report_date: dt.date) -> Optional[Dict[str, Any]]:
    previous = previous_trading_day(report_date)
    report_dir = private_path("reports") / previous.isoformat()
    for filename in ("19-00.json", "14-30.json", "13-30.json", "10-30.json", "09-00.json"):
        path = report_dir / filename
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("trade_day_verified") and payload.get("quote_validation_passed"):
            return payload
    return None


def load_previous_close_snapshot(report_date: dt.date) -> Optional[Dict[str, Any]]:
    previous = previous_trading_day(report_date)
    path = private_path("reports") / previous.isoformat() / "19-00.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not payload.get("trade_day_verified") or not payload.get("quote_validation_passed"):
        return None
    return payload


def pool_matches_previous_close(pool: List[Dict[str, Any]], live_quotes: Dict[str, Dict[str, Any]]) -> bool:
    checks = []
    for item in pool:
        code = str(item.get("c", ""))
        quote = live_quotes.get(code)
        if not quote or not quote.get("prev_close"):
            continue
        checks.append(abs(fnum(item.get("p")) / 1000 - fnum(quote.get("prev_close"))) <= 0.011)
        if len(checks) >= 20:
            break
    return len(checks) >= 3 and sum(checks) / len(checks) >= 0.8


def opportunity_score(item: Dict[str, Any], theme_counts: Counter) -> float:
    turnover = fnum(item.get("hs"))
    seal_ratio = fnum(item.get("fund")) / max(fnum(item.get("amount")), 1)
    return (
        theme_counts[str(item.get("hybk", ""))] * 2.8
        + (2.2 if inum(item.get("lbc")) == 1 else 1.0)
        + max(0.0, 2.0 - abs(turnover - 8.0) / 5.0)
        + min(seal_ratio * 8.0, 2.5)
        - inum(item.get("zbc")) * 0.25
    )


def select_opportunities(pool: List[Dict[str, Any]], excluded_codes: set, asof: dt.date) -> Tuple[List[Dict[str, Any]], Dict[str, List[str]]]:
    theme_counts = Counter(str(item.get("hybk", "其他")) for item in pool)
    candidates = []
    for item in pool:
        name = str(item.get("n", ""))
        if (
            str(item.get("c", "")) in excluded_codes
            or "ST" in name.upper()
            or inum(item.get("lbc")) > 2
            or not (2.0 <= fnum(item.get("hs")) <= 18.0)
            or fnum(item.get("amount")) < 1e8
        ):
            continue
        enriched = dict(item)
        enriched["score"] = opportunity_score(item, theme_counts)
        candidates.append(enriched)
    candidates.sort(key=lambda x: (x["score"], fnum(x.get("amount"))), reverse=True)

    shortlist = candidates[:25]
    fetched_risks: Dict[str, List[str]] = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch_announcements, str(item["c"])): str(item["c"]) for item in shortlist}
        for future in as_completed(futures):
            code = futures[future]
            try:
                fetched_risks[code] = recent_risk_titles(future.result(), asof)
            except DataError as exc:
                fetched_risks[code] = [f"公告抓取失败：{exc}"]

    selected: List[Dict[str, Any]] = []
    risk_map: Dict[str, List[str]] = {}
    picked_theme: Counter = Counter()
    for item in shortlist:
        code, theme = str(item["c"]), str(item.get("hybk", "其他"))
        if picked_theme[theme] >= 2:
            continue
        risks = fetched_risks.get(code, ["公告抓取失败：无返回"])
        risk_map[code] = risks
        if risks and not risks[0].startswith("公告抓取失败"):
            continue
        selected.append(item)
        picked_theme[theme] += 1
        if len(selected) == 5:
            break
    return selected, risk_map


def quote_validation(codes: Iterable[str], tencent: Dict[str, Dict[str, Any]], second_source: Dict[str, Dict[str, Any]]) -> Tuple[bool, List[str]]:
    lines = []
    all_ok = True
    for code in codes:
        tq, eq = tencent.get(code), second_source.get(code)
        if not tq or not eq:
            all_ok = False
            lines.append(f"{code} 缺少一类行情源")
            continue
        price_tolerance = max(0.011, max(abs(tq["price"]), abs(eq["price"])) * 0.003)
        price_ok = abs(tq["price"] - eq["price"]) <= price_tolerance
        pct_ok = abs(tq["pct"] - eq["pct"]) <= 0.35
        ok = price_ok and pct_ok
        all_ok = all_ok and ok
        lines.append(
            f"{html.escape(tq['name'])}({code}) 腾讯/新浪收盘价 {tq['price']:.2f}/{eq['price']:.2f}，"
            f"涨跌幅 {tq['pct']:+.2f}%/{eq['pct']:+.2f}%：{'一致' if ok else '需复核'}"
        )
    return all_ok, lines


def risk_assessment(indices: Dict[str, Dict[str, Any]], breadth: Dict[str, int]) -> Tuple[str, str, str]:
    total = max(breadth["total"], 1)
    down_ratio = breadth["down"] / total
    gem = indices.get("399006", {}).get("pct", 0.0)
    sz = indices.get("399001", {}).get("pct", 0.0)
    if down_ratio >= 0.68 or gem <= -2.5 or sz <= -2.0:
        return (
            "高",
            "风险暴露依用户确认的私有预算；缺预算仅观察，不生成仓位金额",
            "防守优先：次日只观察分歧后的承接，不追一字板或高开加速；若高低位同步炸板则暂停新开仓。",
        )
    if down_ratio >= 0.55 or gem <= -1.2:
        return ("中高", "依用户确认的私有预算评估；缺预算仅观察", "小仓试错，必须等板块联动和承接确认。")
    return ("中", "首日额度依用户确认的私有预算；缺预算仅观察", "按触发条件与已确认预算评估，现金保留遵循私有风险约束。")


def render_cffex(cffex: Dict[str, Dict[str, Any]]) -> str:
    if not cffex:
        return "中金所持仓抓取失败或本时点不要求；不得据此推断机构方向。"
    lines = []
    for var in ("IF", "IH", "IC", "IM"):
        info = cffex.get(var)
        if not info:
            lines.append(f"{var}：缺失")
            continue
        citic = info.get("citic")
        if citic:
            longs = "、".join(f"{x['name'].replace('(代客)', '')}{x['net']:+,}" for x in info["top_net_long"][:2])
            shorts = "、".join(f"{x['name'].replace('(代客)', '')}{x['net']:+,}" for x in info["top_net_short"][:2])
            lines.append(
                f"{link(var + '官方CSV', info['source'])}：中信期货在进入前20榜单的各合约合计"
                f"多{citic['long']:,}、空{citic['short']:,}、净{citic['net']:+,}手，净变化{citic['net_chg']:+,}手；"
                f"可见净多前列：{html.escape(longs)}；可见净空前列：{html.escape(shorts)}"
            )
        else:
            lines.append(f"{link(var + '官方CSV', info['source'])}：中信期货未出现在可见前20持仓栏，不能视为零持仓")
    return "<br>".join(lines) + "<br><small>口径：仅汇总中金所各合约公布的前20会员席位；不是机构自营仓，也不能直接等同市场方向。</small>"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate A-share emotion strategy HTML report")
    parser.add_argument("--slot", choices=["auto", *SLOTS], default="auto")
    parser.add_argument("--date", help="YYYY-MM-DD; defaults to Asia/Shanghai local date")
    parser.add_argument("--editorial", type=Path, help="Optional JSON with market_summary and source_links")
    parser.add_argument("--emotion-evidence", type=Path, help="Structured catalyst evidence JSON produced by the emotion-monitor skill")
    parser.add_argument("--candidate-only", action="store_true", help="Print the current pre-catalyst shortlist and exit without writing reports or state")
    parser.add_argument("--allow-stale", action="store_true", help="Render even when feed trade date differs from requested date")
    args = parser.parse_args()

    now = dt.datetime.now(TZ)
    report_date = dt.date.fromisoformat(args.date) if args.date else now.date()
    slot = choose_slot(now) if args.slot == "auto" else args.slot
    emotion_config = load_config("emotion_config.json")
    editorial = load_editorial(args.editorial)
    evidence_map, evidence_errors = load_catalyst_evidence(args.emotion_evidence)

    calendar_ok, calendar_reason = calendar_is_trading_day(report_date)
    if not calendar_ok and not args.allow_stale:
        print(json.dumps({
            "status": "skipped",
            "report_date": report_date.isoformat(),
            "slot": slot,
            "reason": calendar_reason,
        }, ensure_ascii=False, indent=2))
        return 0

    market_data_date = previous_trading_day(report_date) if slot == "09_00" else report_date

    watchlist_codes = active_auto_codes(report_date)
    index_codes = ["000001", "399001", "399006"]
    base_codes = list(dict.fromkeys(watchlist_codes + index_codes))
    quotes, quote_stamp = fetch_tencent_quotes(base_codes)
    live_preopen_quotes = dict(quotes)
    preopen_snapshot: Optional[Dict[str, Any]] = None
    if slot == "09_00":
        preopen_snapshot = load_previous_audited_snapshot(report_date)
        if preopen_snapshot:
            previous_quotes = {
                **(preopen_snapshot.get("dynamic_quotes") or {}),
                # One-time compatibility with reports generated before the
                # dynamic-target migration.
                **(preopen_snapshot.get("fixed_quotes") or {}),
                **(preopen_snapshot.get("indices") or {}),
            }
            for code in base_codes:
                if code in previous_quotes:
                    quotes[code] = previous_quotes[code]
            previous_stamps = [str(row.get("timestamp", "")) for row in previous_quotes.values() if isinstance(row, dict)]
            quote_stamp = max(previous_stamps, default=quote_stamp)
    pool, pool_date = fetch_limit_pool(market_data_date)
    quote_date = quote_stamp[:8]
    expected = report_date.strftime("%Y%m%d")
    market_expected = market_data_date.strftime("%Y%m%d")
    if slot == "09_00":
        pool_previous_verified = pool_date == market_expected or (
            pool_date == expected and pool_matches_previous_close(pool, live_preopen_quotes)
        )
        is_trade_day = calendar_ok and quote_date in {expected, market_expected} and pool_previous_verified
    else:
        is_trade_day = (
            calendar_ok and quote_date == expected and pool_date == expected
            and quote_time_fresh_for_slot(quote_stamp, slot)
        )
    if not is_trade_day and not args.allow_stale:
        raise DataError(
            f"requested {expected} ({slot}), but Tencent={quote_date or 'missing'} and Eastmoney pool={pool_date or 'missing'}; "
            "likely a non-trading day or stale feed (use --allow-stale only for diagnostics)"
        )

    pool_map = {str(item["c"]): item for item in pool}
    theme_counts = Counter(str(item.get("hybk", "其他")) for item in pool)
    lhb = fetch_lhb(market_data_date)
    ann_risks: Dict[str, List[str]] = {}
    risk_check_codes = list(dict.fromkeys(watchlist_codes))
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fetch_announcements, code): code for code in risk_check_codes}
        for future in as_completed(futures):
            code = futures[future]
            try:
                ann_risks[code] = recent_risk_titles(future.result(), report_date)
            except DataError as exc:
                ann_risks[code] = [f"公告抓取失败：{exc}"]

    opportunities, opportunity_risks = select_opportunities(pool, set(), report_date)
    leader_candidates, leader_metadata_errors = enrich_leader_candidates(
        prefilter_emotion_leaders(pool), report_date
    )
    technology_candidates: List[Dict[str, Any]] = []
    technology_metadata_errors: List[str] = []
    if slot == "19_00":
        technology_candidates, technology_metadata_errors = enrich_leader_candidates(
            prefilter_technology_leaders(pool), report_date
        )
    leader_risks: Dict[str, List[str]] = {}
    technology_risks: Dict[str, List[str]] = {}
    leader_risk_codes = [str(item.get("c", "")) for item in leader_candidates if item.get("c")]
    technology_risk_codes = [str(item.get("c", "")) for item in technology_candidates if item.get("c")]
    combined_leader_risk_codes = list(dict.fromkeys(leader_risk_codes + technology_risk_codes))
    if combined_leader_risk_codes:
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_announcements, code): code for code in combined_leader_risk_codes}
            for future in as_completed(futures):
                code = futures[future]
                try:
                    titles = recent_risk_titles(future.result(), report_date)
                except Exception as exc:
                    titles = [f"公告抓取失败：{exc}"]
                if code in leader_risk_codes:
                    leader_risks[code] = titles
                if code in technology_risk_codes:
                    technology_risks[code] = titles
    market_error = ""
    market_snapshot: Dict[str, Dict[str, Any]] = {}
    near_candidates: List[Dict[str, Any]] = []
    sealed_candidates: List[Dict[str, Any]] = []
    near_metadata_errors: List[str] = []
    near_risks: Dict[str, List[str]] = {}
    universe_quality: Dict[str, Any] = {
        "universe_count": 0, "expected_count": 0, "coverage_ratio": 0.0, "errors": [], "warnings": [],
    }
    previous_close_snapshot = load_previous_close_snapshot(report_date) if slot == "09_00" else None
    if slot == "09_00":
        if editorial.get("breadth"):
            breadth = {key: inum(editorial["breadth"].get(key)) for key in ("up", "down", "flat", "total")}
        elif preopen_snapshot:
            breadth = dict(preopen_snapshot.get("breadth") or {"up": 0, "down": 0, "flat": 0, "total": 0})
        else:
            breadth = {"up": 0, "down": 0, "flat": 0, "total": 0}
        near_research_queue = list((((previous_close_snapshot or {}).get("emotion_near_limit") or {}).get("next_session_watchlist") or []))
    else:
        try:
            market_snapshot, market_breadth = fetch_market_list()
            breadth = (
                {key: inum(editorial["breadth"].get(key)) for key in ("up", "down", "flat", "total")}
                if editorial.get("breadth") else market_breadth
            )
            universe_quality = {
                "universe_count": len(market_snapshot),
                "expected_count": inum(market_breadth.get("expected_count")),
                "coverage_ratio": fnum(market_breadth.get("coverage_ratio")),
                "errors": [],
                "warnings": ([f"全市场缺失分页：{market_breadth.get('missing_pages')}"] if market_breadth.get("missing_pages") else []),
            }
            previous_pool, previous_pool_date = fetch_limit_pool(previous_trading_day(report_date))
            expected_previous = previous_trading_day(report_date).strftime("%Y%m%d")
            if previous_pool_date != expected_previous:
                universe_quality["warnings"].append(
                    f"历史涨停池返回{previous_pool_date or 'missing'}而非{expected_previous}；板位改用最近日K线复算"
                )
                previous_pool_map = {}
            else:
                previous_pool_map = {str(item.get("c", "")): item for item in previous_pool}
            raw_near, sealed_candidates = prefilter_near_limit(market_snapshot, previous_pool_map)
            for sealed in sealed_candidates:
                current_board = inum(pool_map.get(str(sealed.get("c", "")), {}).get("lbc"))
                if current_board:
                    sealed["prospective_board"] = current_board
            near_candidates, near_metadata_errors = enrich_near_limit_candidates(raw_near, previous_pool_map, report_date)
        except Exception as exc:
            market_error = str(exc)
            breadth = {"up": 0, "down": 0, "flat": 0, "total": 0}
            universe_quality["errors"].append(market_error)
        near_research_queue = [
            row for row in near_candidates if row.get("listing_age_verified") and row.get("hybk")
        ]

    near_risk_codes = [str(item.get("c", "")) for item in near_research_queue if item.get("c")]
    if near_risk_codes:
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_announcements, code): code for code in near_risk_codes}
            for future in as_completed(futures):
                code = futures[future]
                try:
                    near_risks[code] = recent_risk_titles(future.result(), report_date)
                except Exception as exc:
                    near_risks[code] = [f"公告抓取失败：{exc}"]

    hot_sector_context = build_hot_sector_context(
        pool, near_candidates if slot != "09_00" else [],
        as_of=market_data_date.isoformat(),
        source_verified=is_trade_day and (slot == "09_00" or (
            fnum(universe_quality.get("coverage_ratio")) >= emotion_config["near_limit"]["minimum_universe_coverage_ratio"]
            and not near_metadata_errors and not universe_quality["errors"]
        )),
        config=emotion_config,
    )
    # Compare the full strong-stock pool before strategy-specific exclusions.
    # Filter research queues too, so evidence collection does not reintroduce laggards.
    opportunities = filter_ranked_rows(opportunities, hot_sector_context, emotion_config)
    leader_candidates = filter_ranked_rows(leader_candidates, hot_sector_context, emotion_config)
    technology_candidates = filter_ranked_rows(technology_candidates, hot_sector_context, emotion_config)
    if slot != "09_00":
        near_research_queue = filter_ranked_rows(near_research_queue, hot_sector_context, emotion_config)
    else:
        near_research_queue = build_preopen_near_limit_report(
            previous_close_snapshot, expected_as_of=market_data_date.isoformat(),
        )["emotion_near_limit"]["items"]

    if args.candidate_only:
        print(json.dumps({
            "status": "candidate_only",
            "hot_sector_focus": hot_sector_context,
            "report_date": report_date.isoformat(),
            "slot": slot,
            "market_data_date": market_data_date.isoformat(),
            "candidates": opportunities,
            "leader_research_queue": leader_candidates,
            "technology_leader_research_queue": technology_candidates,
            "near_limit_research_queue": near_research_queue,
            "near_limit_data_quality": {
                **universe_quality,
                "metadata_errors": near_metadata_errors,
                "research_queue_count": len(near_research_queue),
                "scope": "沪深主板普通A股；排除科创板、创业板、北交所、ST/退市及上市前5个交易日",
            },
            "evidence_contract": {
                "root": "catalysts[]",
                "required_fields": ["code", "title", "published_at", "url", "event_type", "theme", "source_tier"],
                "source_tier": ["official", "mainstream"],
                "verification": "1个官方来源，或2个独立主流财经来源；社媒/论坛不接受",
            },
        }, ensure_ascii=False, indent=2))
        return 0
    theme_watchlist_config = load_theme_watchlists()
    research_theme_codes = theme_watchlist_codes(theme_watchlist_config)
    opportunity_codes = [str(item["c"]) for item in opportunities]
    leader_codes = [str(item["c"]) for item in leader_candidates]
    technology_codes = [str(item["c"]) for item in technology_candidates]
    near_codes = [str(item["c"]) for item in near_research_queue] if slot != "09_00" else []
    validation_codes = list(dict.fromkeys(watchlist_codes + opportunity_codes + near_codes))
    check_codes = list(dict.fromkeys(
        validation_codes + leader_codes + technology_codes + index_codes + research_theme_codes
    ))
    if slot == "09_00":
        extra_codes = list(dict.fromkeys(
            opportunity_codes + leader_codes + technology_codes + near_codes + research_theme_codes
        ))
        extra_quotes, opportunity_stamp = fetch_tencent_quotes(extra_codes) if extra_codes else ({}, "")
        all_tencent = {**quotes, **extra_quotes}
        opportunity_quotes = {code: all_tencent[code] for code in opportunity_codes if code in all_tencent}
        leader_quotes = {code: all_tencent[code] for code in leader_codes if code in all_tencent}
        technology_quotes = {code: all_tencent[code] for code in technology_codes if code in all_tencent}
        near_quotes = {code: all_tencent[code] for code in near_codes if code in all_tencent}
        near_stamp = opportunity_stamp
        sina_quotes, sina_stamp = fetch_sina_quotes(check_codes)
    else:
        with ThreadPoolExecutor(max_workers=2) as executor:
            tencent_future = executor.submit(fetch_tencent_quotes, check_codes)
            sina_future = executor.submit(fetch_sina_quotes, check_codes)
            refreshed_tencent, refreshed_stamp = tencent_future.result()
            sina_quotes, sina_stamp = sina_future.result()
        all_tencent = refreshed_tencent
        quote_stamp = refreshed_stamp
        quotes = {code: refreshed_tencent[code] for code in base_codes if code in refreshed_tencent}
        opportunity_quotes = {code: refreshed_tencent[code] for code in opportunity_codes if code in refreshed_tencent}
        leader_quotes = {code: refreshed_tencent[code] for code in leader_codes if code in refreshed_tencent}
        technology_quotes = {code: refreshed_tencent[code] for code in technology_codes if code in refreshed_tencent}
        near_quotes = {code: refreshed_tencent[code] for code in near_codes if code in refreshed_tencent}
        opportunity_stamp = refreshed_stamp
        near_stamp = refreshed_stamp
    validation_ok, validation_lines = quote_validation(validation_codes, all_tencent, sina_quotes)
    if slot == "09_00" and preopen_snapshot:
        validation_ok = bool(preopen_snapshot.get("quote_validation_passed"))
        validation_lines = [
            f"盘前复用上一交易日 {market_data_date.isoformat()} 已审计双源收盘快照；"
            "当日竞价行情不用于冒充完整成交数据。"
        ]
    indices = {code: quotes[code] for code in index_codes}
    risk_level, exposure, core_action = risk_assessment(indices, breadth)
    combined_ann_risks = {**ann_risks, **opportunity_risks, **near_risks, **technology_risks}
    try:
        emotion_report = build_emotion_report(
            report_date=report_date,
            slot=slot,
            risk_level=risk_level,
            candidates=opportunities,
            quotes=all_tencent,
            pool_map=pool_map,
            theme_counts=dict(theme_counts),
            evidence_map=evidence_map,
            evidence_errors=evidence_errors,
            announcement_risks=combined_ann_risks,
            persist_state=not args.allow_stale,
            hot_sector_context=hot_sector_context,
        )
    except Exception as exc:
        emotion_report = build_degraded_emotion_report(str(exc), slot)
    try:
        if slot == "09_00":
            near_limit_report = build_preopen_near_limit_report(previous_close_snapshot, expected_as_of=market_data_date.isoformat())
        elif universe_quality["errors"]:
            near_limit_report = build_degraded_near_limit_report("；".join(universe_quality["errors"]), slot)
        else:
            near_limit_report = build_near_limit_report(
                report_date=report_date,
                slot=slot,
                risk_level=risk_level,
                candidates=near_research_queue,
                sealed_candidates=sealed_candidates,
                quotes=near_quotes,
                sina_quotes={code: sina_quotes.get(code, {}) for code in near_codes},
                current_pool_map=pool_map,
                theme_counts=dict(theme_counts),
                evidence_map=evidence_map,
                evidence_errors=evidence_errors + near_metadata_errors,
                announcement_risks=near_risks,
                market_snapshot=market_snapshot,
                universe_quality=universe_quality,
                persist_state=not args.allow_stale,
                hot_sector_context=hot_sector_context,
            )
    except Exception as exc:
        near_limit_report = build_degraded_near_limit_report(str(exc), slot)
    try:
        near_limit_report.update(build_emotion_leader_report(
            candidates=leader_candidates,
            quotes=leader_quotes,
            sina_quotes={code: sina_quotes.get(code, {}) for code in leader_codes},
            theme_counts=dict(theme_counts),
            announcement_risks=leader_risks,
            metadata_errors=leader_metadata_errors,
            hot_sector_context=hot_sector_context,
            expected_as_of=market_data_date.isoformat(),
        ))
    except Exception as exc:
        near_limit_report.update({
            "emotion_leaders": {
                "label": "程序识别情绪龙头 Top 5", "items": [], "eligible_count": 0,
                "displayed_count": 0, "vacant_count": 5, "rejected": [],
                "methodology": "数据不足，不以普通首板补位",
            },
            "emotion_leader_data_quality": {
                "status": "error", "actionable": False, "errors": [str(exc)], "warnings": [],
                "unverified_candidate_count": len(leader_candidates), "synthetic_data_used": False,
            },
        })
    if slot == "19_00":
        try:
            technology_report = build_technology_leader_report(
                candidates=technology_candidates,
                quotes=technology_quotes,
                sina_quotes={code: sina_quotes.get(code, {}) for code in technology_codes},
                announcement_risks=technology_risks,
                risk_level=risk_level,
                breadth=breadth,
                indices=indices,
                metadata_errors=technology_metadata_errors,
                hot_sector_context=hot_sector_context,
            )
        except Exception as exc:
            technology_report = build_degraded_technology_leader_report(str(exc))
    else:
        technology_report = build_degraded_technology_leader_report("仅在19:00复盘启用")
    abnormal_market = dict(market_snapshot)
    abnormal_expected_count = inum(universe_quality.get("expected_count"), len(abnormal_market))
    abnormal_source_error = ""
    if slot != "09_00":
        try:
            beijing_market, beijing_expected = fetch_beijing_snapshot()
            abnormal_market.update(beijing_market)
            abnormal_expected_count += beijing_expected
        except Exception as exc:
            abnormal_source_error = f"北交所全市场行情失败：{exc}"
    try:
        profile_path = private_path("data/cache/security_profiles.json")
        profile_cache = json.loads(profile_path.read_text(encoding="utf-8")) if profile_path.exists() else {}
        industries = {code: str(row.get("industry") or "") for code, row in profile_cache.items()}
        abnormal_report = build_abnormal_10d_report(
            report_date=report_date,
            slot=slot,
            market_snapshot=abnormal_market,
            expected_count=abnormal_expected_count,
            config=emotion_config,
            theme_counts=dict(theme_counts),
            industry_by_code=industries,
            risk_title_fetcher=lambda code: recent_risk_titles(fetch_announcements(code), report_date),
            persist_cache=not args.allow_stale,
            as_of_date=market_data_date if slot == "09_00" else report_date,
        )
        if abnormal_source_error:
            quality = abnormal_report["abnormal_10d_data_quality"]
            quality["status"] = "error"
            quality["actionable"] = False
            quality["errors"] = [*quality.get("errors", []), abnormal_source_error]
            abnormal_report["abnormal_10d_monitor"]["items"] = []
            abnormal_report["abnormal_10d_monitor"]["trigger_count"] = 0
            abnormal_report["abnormal_10d_monitor"]["warning_count"] = 0
    except Exception as exc:
        abnormal_report = {
            "abnormal_10d_monitor": {
                "label": "10日翻倍异动监控", "scope": "全部在市A股普通股票", "as_of": "",
                "trigger_count": 0, "warning_count": 0, "items": [], "unverified_items": [],
                "methodology": "10个交易日复权累计涨幅；未来5日为规则化研究评分，不是确定性预测",
            },
            "abnormal_10d_data_quality": {
                "status": "error", "actionable": False, "errors": [str(exc)], "warnings": [],
                "universe_count": 0, "expected_count": 0, "universe_coverage_ratio": 0.0,
                "history_coverage_ratio": 0.0, "dual_source_verified_count": 0,
                "missing_codes": [], "synthetic_data_used": False,
            },
        }
    try:
        theme_watchlist_report = build_theme_watchlist_report(
            as_of=report_date,
            tencent_quotes=all_tencent,
            sina_quotes=sina_quotes,
            evidence_map=evidence_map,
            payload=theme_watchlist_config,
        )
    except Exception as exc:
        theme_watchlist_report = {
            "as_of": report_date.isoformat(), "research_only": True,
            "auto_trade_eligible": False, "dynamic_target_qualification_required": True,
            "watchlists": [],
            "data_quality": {
                "status": "error", "actionable": False, "errors": [str(exc)],
                "synthetic_data_used": False,
                "scope": "主题研究观察失败，不影响情绪策略、临板Top 5或其他市场数据质量",
            },
        }
    etf_market = fetch_sector_etf_market(market_data_date)
    sector_flow = build_sector_flow(report_date, slot, etf_market, persist_state=not args.allow_stale)
    cffex: Dict[str, Dict[str, Any]] = {}
    cffex_error = ""
    margin: Dict[str, Any] = {}
    margin_error = ""
    etf_quotes: Dict[str, Dict[str, Any]] = {}
    etf_validation_ok = False
    etf_validation_lines: List[str] = []
    if slot == "19_00":
        try:
            cffex = fetch_cffex(report_date)
        except DataError as exc:
            cffex_error = str(exc)
        try:
            margin = fetch_margin(report_date)
        except DataError as exc:
            margin_error = str(exc)
        try:
            etf_tencent, _ = fetch_tencent_quotes(ETF_PROXIES)
            etf_sina, _ = fetch_sina_quotes(ETF_PROXIES)
            etf_validation_ok, etf_validation_lines = quote_validation(ETF_PROXIES, etf_tencent, etf_sina)
            etf_quotes = etf_tencent
        except DataError as exc:
            etf_validation_lines = [f"ETF代理行情抓取失败：{exc}"]

    summary = editorial.get("market_summary") or (
        f"上证{indices['000001']['pct']:+.2f}%、深证{indices['399001']['pct']:+.2f}%、"
        f"创业板{indices['399006']['pct']:+.2f}%；样本市场上涨{breadth['up']}、下跌{breadth['down']}、"
        f"平盘{breadth['flat']}。涨停池{len(pool)}只，较强行业包括："
        + "、".join(f"{name}{count}只" for name, count in theme_counts.most_common(5))
        + "。"
    )
    source_links = editorial.get("source_links") or []
    sources_html = "；".join(link(str(item.get("label", "外部来源")), str(item.get("url", ""))) for item in source_links if item.get("url"))
    market_context = html.escape(summary) + (f"<br>编辑性复核：{sources_html}" if sources_html else "")
    if slot == "19_00":
        market_context += "<br><b>中金所席位：</b>" + (render_cffex(cffex) if cffex else html.escape(cffex_error or "未取得"))
        if margin:
            market_context += (
                f"<br><b>两融（最新已发布 {html.escape(margin['date'])}）：</b>"
                f"沪深合计余额{money(margin['combined_total_balance'])}；"
                f"沪市融资买入{money(margin['sse']['financing_buy'])}，深市融资买入{money(margin['szse']['financing_buy'])}。"
                f"来源：{link('上交所', margin['sources']['sse'])}、{link('深交所', margin['sources']['szse'])}。"
            )
        else:
            market_context += f"<br><b>两融：</b>数据缺口（{html.escape(margin_error or '未取得')}），不据此推断杠杆资金方向。"
        if etf_quotes:
            etf_text = "、".join(
                f"{html.escape(ETF_PROXIES[code])}{quote['pct']:+.2f}%（成交{money(quote['amount_cny'])}）"
                for code, quote in etf_quotes.items()
            )
            market_context += f"<br><b>ETF代理：</b>{etf_text}。"
        else:
            market_context += "<br><b>ETF代理：</b>数据缺口，不据此推断资金风格。"

    verify_items = [
        f"交易日校验：{html.escape(calendar_reason)}；腾讯时间戳 {html.escape(quote_stamp)}；"
        f"东财涨停池交易日 {html.escape(pool_date)}；盘前基准日 {market_data_date.isoformat()}；"
        f"结论={'通过' if is_trade_day else '未通过/诊断模式'}。",
        f"双源行情校验：{'全部通过' if validation_ok else '存在差异'}。" + "<br>".join(validation_lines),
        f"龙虎榜：{link('东方财富数据中心', EASTMONEY_LHB_URL)}，当日去重后{len(lhb)}只；只表示上榜席位，不代表全市场资金。",
        f"公告：{link('东方财富公告接口', EASTMONEY_ANN_URL)}，对动态观察池和候选做近7日标题关键词初筛；标题筛查不能替代公告原文。",
        f"涨停池：{link('东方财富涨停池', EASTMONEY_POOL_URL)}；动态观察池与候选由{link('腾讯行情', TENCENT_URL)}和{link('新浪行情', SINA_QUOTE_URL)}交叉复核。",
    ]
    if market_error:
        verify_items.append(f"全市场广度抓取降级：{html.escape(market_error)}；风险级别仅使用指数条件，未伪造广度数据。")
    abnormal_quality = abnormal_report["abnormal_10d_data_quality"]
    verify_items.append(
        f"10日翻倍异动监控：{'通过' if abnormal_quality.get('actionable') else '数据不足'}；"
        f"普通A股覆盖{abnormal_quality.get('universe_coverage_ratio', 0):.2%}，"
        f"历史日线覆盖{abnormal_quality.get('history_coverage_ratio', 0):.2%}，"
        f"双源复核{abnormal_quality.get('dual_source_verified_count', 0)}只；未使用模拟数据。"
    )
    if opportunity_risks:
        verify_items.append("候选公告筛查已执行；命中风险词的候选已从本轮自动名单剔除。")
    if slot == "19_00":
        verify_items.append(
            f"ETF代理双源行情校验：{'通过' if etf_validation_ok else '未通过'}。" + "<br>".join(etf_validation_lines)
        )
        verify_items.append(
            "两融使用交易所最新已发布日期；若晚于收盘但尚未发布当日值，会明确显示上一可得交易日，不用旧值冒充当日值。"
        )
    verification = "<ol><li>" + "</li><li>".join(verify_items) + "</li></ol>"

    template = (ROOT / "templates" / SLOTS[slot]).read_text(encoding="utf-8")
    rendered = template
    replacements = {
        "{{DATE}}": report_date.isoformat(),
        "{{DATA_TIME}}": max(quote_stamp, opportunity_stamp, near_stamp) or now.strftime("%Y%m%d%H%M%S"),
        "{{RISK_LEVEL}}": risk_level,
        "{{EXPOSURE}}": exposure,
        "{{CORE_ACTION}}": core_action,
        "{{DYNAMIC_TARGET_SECTION}}": (
            render_abnormal_10d_section(abnormal_report)
            + render_near_limit_section(near_limit_report)
            + render_emotion_section(emotion_report)
            + render_theme_watchlist_section(theme_watchlist_report)
            + render_dynamic_targets_section(emotion_report)
        ),
        "{{TECHNOLOGY_LEADER_SECTION}}": (
            render_technology_leader_section(technology_report) if slot == "19_00" else ""
        ),
        "{{MARKET_CONTEXT}}": market_context,
        "{{VERIFICATION}}": verification,
        "{{EMOTION_MONITOR}}": render_near_limit_section(near_limit_report) + render_emotion_section(emotion_report),
    }
    for key, value in replacements.items():
        rendered = rendered.replace(key, str(value))
    rendered = rendered.replace(
        "<section><h2>市场与外部环境</h2>",
        render_sector_flow_section(sector_flow) + "<section><h2>市场与外部环境</h2>",
        1,
    )
    unresolved = re.findall(r"{{[A-Z0-9_]+}}", rendered)
    if unresolved:
        raise DataError(f"unresolved template placeholders: {sorted(set(unresolved))}")

    output_dir = private_path("reports") / report_date.isoformat()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{slot.replace('_', '-')}.html"
    output_path.write_text(rendered, encoding="utf-8")
    generated_at = dt.datetime.now(TZ)
    snapshot = {
        "hot_sector_focus": hot_sector_context,
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "report_date": report_date.isoformat(),
        "slot": slot,
        "trade_day_verified": is_trade_day,
        "quote_timestamp": quote_stamp,
        "risk_level": risk_level,
        "breadth": breadth,
        "indices": indices,
        "dynamic_quotes": {code: quotes[code] for code in watchlist_codes if code in quotes},
        "dynamic_limit_pool": {code: pool_map.get(code) for code in watchlist_codes},
        "strategy_target_mode": "dynamic",
        "opportunities": opportunities,
        "emotion_watchlist": emotion_report["emotion_watchlist"],
        "emotion_selection": emotion_report["emotion_selection"],
        "emotion_alerts": emotion_report["emotion_alerts"],
        "emotion_recommendations": emotion_report["emotion_recommendations"],
        "emotion_dynamic_targets": emotion_report["emotion_dynamic_targets"],
        "emotion_data_quality": emotion_report["emotion_data_quality"],
        "emotion_near_limit": near_limit_report["emotion_near_limit"],
        "emotion_near_limit_outcomes": near_limit_report["emotion_near_limit_outcomes"],
        "emotion_near_limit_data_quality": near_limit_report["emotion_near_limit_data_quality"],
        "emotion_leaders": near_limit_report["emotion_leaders"],
        "emotion_leader_data_quality": near_limit_report["emotion_leader_data_quality"],
        "technology_cosmic_leaders": technology_report["technology_cosmic_leaders"],
        "technology_leader_data_quality": technology_report["technology_leader_data_quality"],
        "theme_research_watchlist": theme_watchlist_report,
        "abnormal_10d_monitor": abnormal_report["abnormal_10d_monitor"],
        "abnormal_10d_data_quality": abnormal_report["abnormal_10d_data_quality"],
        "quote_validation_passed": validation_ok,
        "cffex": cffex,
        "margin": margin,
        "etf_quotes": etf_quotes,
        "etf_quote_validation_passed": etf_validation_ok,
        "sector_flow": sector_flow,
        "data_quality": {
            "status": "verified" if is_trade_day and validation_ok else "degraded",
            "trade_day_verified": is_trade_day,
            "quote_validation_passed": validation_ok,
            "synthetic_data_used": False,
        },
        "sources": {
            "tencent": TENCENT_URL,
            "eastmoney_pool": EASTMONEY_POOL_URL,
            "sina_quotes": SINA_QUOTE_URL,
            "sina_market": SINA_LIST_URL,
            "eastmoney_lhb": EASTMONEY_LHB_URL,
            "eastmoney_announcements": EASTMONEY_ANN_URL,
            "eastmoney_stock_profile": EASTMONEY_STOCK_PROFILE_URL,
            "tencent_adjusted_kline": "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
            "sina_daily_kline": "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData",
            "eastmoney_adjusted_kline": "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            "eastmoney_beijing_market": "https://push2.eastmoney.com/api/qt/clist/get",
            "sina_beijing_market": "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData",
            "sse_margin": SSE_MARGIN_URL,
            "szse_margin": SZSE_MARGIN_URL,
            "eastmoney_sector_flow": "https://push2.eastmoney.com/api/qt/clist/get",
            "cninfo_disclosures": "https://www.cninfo.com.cn/",
            "eastmoney_financial_analysis": "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/Index",
            "baidu_valuation_history": "https://gushitong.baidu.com/",
        },
    }
    snapshot.update(report_monitor_context(report_date, slot, generated_at))
    save_report_snapshot(output_dir / f"{slot.replace('_', '-')}.json", snapshot,
                         report_date, refresh=not args.allow_stale)
    latest = private_path("reports/latest.html")
    print(json.dumps({
        "status": "ok",
        "report": str(output_path),
        "latest": str(latest),
        "latest_updated": not args.allow_stale,
        "snapshot": str(output_dir / f"{slot.replace('_', '-')}.json"),
        "trade_day_verified": is_trade_day,
        "quote_validation_passed": validation_ok,
        "risk_level": risk_level,
        "market_data_quality": snapshot["data_quality"]["status"],
        "opportunities": [f"{item['n']}({item['c']})" for item in opportunities],
        "dynamic_targets": [
            f"{item['name']}({item['code']})·{item['recommendation']['action']}"
            for item in emotion_report["emotion_dynamic_targets"]["items"]
        ],
        "emotion_red_alerts": emotion_report["emotion_summary"]["red_count"],
        "emotion_orange_alerts": emotion_report["emotion_summary"]["orange_count"],
        "emotion_data_quality": emotion_report["emotion_data_quality"]["status"],
        "near_limit_candidates": [
            f"{item['name']}({item['code']})·{item['signal_type']}·距板{item['distance_to_limit_pct']:.2f}%·{item['recommendation']['action']}"
            for item in near_limit_report["emotion_near_limit"]["items"]
        ],
        "near_limit_data_quality": near_limit_report["emotion_near_limit_data_quality"]["status"],
        "abnormal_10d": [
            f"{item['name']}({item['code']})·{item['alert_level']}·10日{item['gain_10d_pct']:+.2f}%"
            for item in abnormal_report["abnormal_10d_monitor"]["items"]
        ],
        "abnormal_10d_data_quality": abnormal_report["abnormal_10d_data_quality"]["status"],
        "emotion_leaders": [
            f"{item['name']}({item['code']})·{item['current_boards']}连板·近{item['recent_days']}日{item['recent_limit_count']}次·{item['score']:.2f}"
            for item in near_limit_report["emotion_leaders"]["items"]
        ],
        "emotion_leader_data_quality": near_limit_report["emotion_leader_data_quality"]["status"],
        "technology_leaders": [
            f"{item['name']}({item['code']})·{item['direction']}·{item['grade']}·{item['score']:.2f}"
            for item in technology_report["technology_cosmic_leaders"]["items"]
        ],
        "technology_leader_data_quality": technology_report["technology_leader_data_quality"]["status"],
        "theme_watchlist_data_quality": theme_watchlist_report["data_quality"]["status"],
        "theme_watchlist": [
            f"{item['name']}({item['code']})·{item['status']}"
            for watchlist in theme_watchlist_report["watchlists"]
            for item in watchlist["items"]
        ],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DataError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
