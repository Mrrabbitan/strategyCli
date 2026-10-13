"""Read-only native feeds for the nightly information radar.

This module collects disclosed information, not selection-strategy results.
Amounts stay in CNY. Date-only market records never acquire a fabricated
publication clock. Original provider responses, including diagnostic failures,
remain in the private research store; normalized rows contain no future returns.
"""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import re
import time
from typing import Any, Optional
import urllib.parse
import urllib.request
import uuid
from zoneinfo import ZoneInfo

import requests

from market_calendar import is_trading_day, previous_trading_day
from research_store import atomic_json, research_path


TZ = ZoneInfo("Asia/Shanghai")
DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
LIMIT_POOL_URL = "https://push2ex.eastmoney.com/getTopicZTPool"
REQUEST_TIMEOUT = 12
MAX_SECTION_SECONDS = 60
MAX_PAGES = 30
SECTION_KEYS = (
    "limit_up", "announcements", "earnings", "repurchases",
    "broker_seats", "institutions",
)
_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://data.eastmoney.com/",
}


class FeedError(RuntimeError):
    """A request or its date/coverage contract was not verified."""


def _clock(value) -> dt.datetime:
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("cutoff must be an aware datetime or ISO timestamp")
    if parsed.tzinfo is None:
        raise ValueError("cutoff must contain its time zone")
    return parsed.astimezone(TZ)


def _trade_date(cutoff: dt.datetime) -> str:
    ok, reason = is_trading_day(cutoff.date())
    if "缺少" in reason or "不可用" in reason:
        raise FeedError(reason)
    if ok and cutoff.time().replace(tzinfo=None) >= dt.time(15):
        return cutoff.date().isoformat()
    return previous_trading_day(cutoff.date()).isoformat()


def _section(status="unavailable", rows=None, missing=None, sources=None, **extra):
    return {"status": status, "rows": rows or [], "missing": missing or [],
            "sources": sources or [], **extra}


def _number(value: Any, *, required=False) -> Optional[float]:
    if isinstance(value, bool):
        raise FeedError("invalid numeric boolean")
    if value is None or value == "" or value == "-":
        if required:
            raise FeedError("necessary numeric field is missing")
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        raise FeedError("invalid numeric field")
    if not math.isfinite(result):
        raise FeedError("non-finite numeric field")
    return result


def _url(base, params):
    return base + "?" + urllib.parse.urlencode(params)


def fetch_json(url: str, *, raw_dir: Path, label: str,
               deadline: Optional[float] = None) -> dict:
    """Two attempts maximum, 12 seconds each, with private source evidence."""
    safe_label = re.sub(r"[^A-Za-z0-9_.-]", "_", label)
    error = None
    attempts = []
    for attempt in range(2):
        remaining = REQUEST_TIMEOUT if deadline is None else deadline - time.monotonic()
        if remaining <= 0:
            error = FeedError("section collection time budget exceeded")
            break
        fetched = dt.datetime.now(TZ).isoformat(timespec="seconds")
        try:
            with requests.get(url, headers=_HEADERS,
                              timeout=min(REQUEST_TIMEOUT, remaining)) as res:
                res.raise_for_status()
                raw = res.content
                http_status = res.status_code
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise FeedError("response is not a JSON object")
            atomic_json(raw_dir / (safe_label + ".json"), {
                "url": url, "fetched_at": fetched, "http_status": http_status,
                "raw_sha256": hashlib.sha256(raw).hexdigest(), "payload": payload,
                "previous_attempts": attempts,
            })
            return payload
        except Exception as exc:
            error = exc
            attempts.append({"attempt": attempt + 1, "fetched_at": fetched,
                             "error_type": type(exc).__name__, "error": str(exc)})
            if attempt == 0 and (deadline is None or deadline - time.monotonic() > .4):
                time.sleep(.4)
    atomic_json(raw_dir / (safe_label + ".failure.json"), {
        "url": url, "attempts": attempts, "error": str(error),
    })
    raise FeedError(f"{safe_label}: {type(error).__name__}: {error}")


def _integer(value, field):
    if isinstance(value, bool):
        raise FeedError(f"invalid {field}")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise FeedError(f"missing {field}")
    if str(parsed) != str(value) or parsed < 0:
        raise FeedError(f"invalid {field}")
    return parsed


def _event_id(row):
    value = row.get("TRADE_ID")
    if value is None or not str(value).strip():
        raise FeedError("missing source event id")
    return str(value)


def fetch_paginated(report_name: str, trade_date: str, *, raw_dir: Path,
                    deadline: Optional[float] = None, require_event_ids=False):
    """Verify every page and a stable reported total; do not turn failures into empties."""
    params = {
        "reportName": report_name, "columns": "ALL",
        "filter": f"(TRADE_DATE='{trade_date}')", "pageNumber": 1,
        "pageSize": 500, "source": "WEB", "client": "WEB",
    }
    rows, sources, expected, page_total = [], [], None, None
    page_hashes = set()
    page = 1
    while True:
        params["pageNumber"] = page
        url = _url(DATACENTER_URL, params)
        payload = fetch_json(url, raw_dir=raw_dir,
                             label=f"{report_name}-p{page}", deadline=deadline)
        if payload.get("success") is not True:
            raise FeedError(f"{report_name}: source reported failure")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise FeedError(f"{report_name}: missing result envelope")
        total = _integer(result.get("count"), "total count")
        pages = _integer(result.get("pages"), "page count")
        data = result.get("data")
        if data is None and total == 0:
            data = []
        if not isinstance(data, list) or any(not isinstance(r, dict) for r in data):
            raise FeedError(f"{report_name}: invalid rows")
        if page == 1:
            expected, page_total = total, pages
            if pages > MAX_PAGES or (total > 0 and pages == 0):
                raise FeedError(f"{report_name}: invalid or excessive pagination")
        elif total != expected or pages != page_total:
            raise FeedError(f"{report_name}: totals changed between pages")
        if expected == 0 and (data or pages not in (0, 1)):
            raise FeedError(f"{report_name}: inconsistent empty result")
        if expected and not data:
            raise FeedError(f"{report_name}: missing page {page}")
        fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True,
                                     ensure_ascii=False).encode()).hexdigest()
        if data and fingerprint in page_hashes:
            raise FeedError(f"{report_name}: repeated pagination page")
        page_hashes.add(fingerprint)
        for row in data:
            if str(row.get("TRADE_DATE") or "")[:10] != trade_date:
                raise FeedError(f"{report_name}: source trade date conflict")
        rows.extend(data)
        sources.append({"label": report_name, "url": url,
                        "source_date": trade_date, "source_time": None,
                        "source_time_precision": "date"})
        if page >= max(page_total, 1):
            break
        page += 1
    if len(rows) != expected:
        raise FeedError(f"{report_name}: row count does not match total")
    if require_event_ids and len({_event_id(r) for r in rows}) != len(rows):
        raise FeedError(f"{report_name}: duplicate event ids")
    return rows, sources


def _code(row):
    code = str(row.get("SECURITY_CODE") or "")
    if not re.fullmatch(r"\d{6}", code):
        raise FeedError("invalid security code")
    return code


def _period(row, trade_date):
    reason = str(row.get("EXPLANATION") or "").strip()
    if not reason:
        raise FeedError("missing source event reason")
    multi = "连续" in reason or "累计" in reason
    start = None
    if multi:
        for field in ("START_DATE", "TRADE_START_DATE", "BEGIN_DATE", "abnormalStart"):
            candidate = str(row.get(field) or "")[:10]
            if candidate:
                if len(candidate) == 8 and candidate.isdigit():
                    candidate = f"{candidate[:4]}-{candidate[4:6]}-{candidate[6:]}"
                dt.date.fromisoformat(candidate)
                if candidate > trade_date:
                    raise FeedError("event begins after its end date")
                start = candidate
                break
    return {"period": "multi" if multi else "single",
            "period_start": start if multi else trade_date,
            "period_end": trade_date, "reason": reason}


def _source_metadata(url, trade_date):
    return {"source_url": url, "source_time": None, "source_date": trade_date,
            "source_time_precision": "date"}


def _verify_pool_sample(pool: list, trade_date: str, raw_dir: Path, deadline: float):
    """Cross-check at most five main-board rows against exact-date unadjusted bars.

    A provider may relabel yesterday's pool with today's qdate. In particular,
    a halted prior leader has no current-day bar and must fail this check. This
    sample is a stale-data check, not whole-market identity verification.
    """
    sample = sorted([row for row in pool if str(row.get("c", "")).startswith(("00", "60"))],
                    key=lambda row: (-int(row["lbc"]), str(row["c"])))[:5]

    def check(row):
        code = str(row["c"])
        symbol = ("sh" if code.startswith("6") else "sz") + code
        start = (dt.date.fromisoformat(trade_date) - dt.timedelta(days=35)).isoformat()
        url = _url("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get", {
            "param": symbol + ",day," + start + "," + trade_date + ",40,",
        })
        payload = fetch_json(url, raw_dir=raw_dir, label="pool-check-"+code,
                             deadline=deadline)
        raw = ((payload.get("data") or {}).get(symbol) or {}).get("day")
        if not isinstance(raw, list):
            raise FeedError(f"{code}未取得未复权日线，涨停池日期不能交叉核验")
        bars = [r for r in raw if isinstance(r, list) and r and str(r[0]) <= trade_date]
        if not bars or str(bars[-1][0]) != trade_date:
            raise FeedError(f"{code}缺少行情日未复权日K，池可能混入停牌或旧日股票")
        if len(bars[-1]) < 3:
            raise FeedError(f"{code}未复权日K收盘价缺失")
        close = _number(bars[-1][2], required=True)
        pool_close = _number(row.get("p"), required=True) / 1000
        if close <= 0 or abs(close - pool_close) > .0005:
            raise FeedError(f"{code}涨停池价格与行情日未复权收盘价冲突")
        return {"label": "腾讯未复权日K：涨停池高标日期和价格抽检", "url": url,
                "source_date": trade_date, "source_time": None,
                "source_time_precision": "date"}

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        return list(executor.map(check, sample))


def collect_limit_up(trade_date: str, *, raw_dir: Path) -> dict:
    url = _url(LIMIT_POOL_URL, {
        "ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt",
        "Pageindex": 0, "pagesize": 1000, "sort": "fbt:asc",
        "date": trade_date.replace("-", ""),
    })
    source = {"label": "东方财富原生收盘涨停池", "url": url,
              "source_date": trade_date, "source_time": None}
    deadline = time.monotonic() + MAX_SECTION_SECONDS
    try:
        payload = fetch_json(url, raw_dir=raw_dir, label="limit-up",
                             deadline=deadline)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise FeedError("涨停池缺少原生数据")
        if str(data.get("qdate")) != trade_date.replace("-", ""):
            raise FeedError("涨停池源qdate与行情日不一致")
        pool = data.get("pool")
        total = _integer(data.get("tc"), "涨停池总数")
        if not isinstance(pool, list) or any(not isinstance(r, dict) for r in pool):
            raise FeedError("涨停池格式无效")
        codes = [str(row.get("c") or "") for row in pool]
        if (len(pool) != total or len(set(codes)) != total
                or any(not re.fullmatch(r"\d{6}", c) for c in codes)):
            raise FeedError("涨停池总数或唯一证券代码校验失败")
        rows = []
        for row in pool:
            boards = _integer(row.get("lbc"), "连板数")
            if boards < 1 or not str(row.get("n") or "").strip():
                raise FeedError("涨停池缺少有效连板数或证券名称")
            close = _number(row.get("p"), required=True) / 1000
            if close <= 0:
                raise FeedError("涨停池原生价格无效")
            rows.append({
                "code": str(row["c"]), "name": str(row["n"]), "boards": boards,
                "close": close, "pct": _number(row.get("zf")),
                "turnover_pct": _number(row.get("hs")), "industry": row.get("hybk"),
                "amount_cny": _number(row.get("amount")),
                "market_code": row.get("m"),
                **_source_metadata(url, trade_date),
            })
        refs = _verify_pool_sample(pool, trade_date, raw_dir, deadline)
        return _section("complete", rows, sources=[source, *refs],
                        scope="供应商原始完整涨停池，证券资格由日报独立核验",
                        source_count=total, crosscheck_count=len(refs))
    except Exception as exc:
        return _section(missing=[str(exc)], sources=[source])


def collect_broker_seats(trade_date: str, *, raw_dir: Path) -> dict:
    deadline = time.monotonic() + MAX_SECTION_SECONDS
    names = ("RPT_DAILYBILLBOARD_DETAILSNEW", "RPT_BILLBOARD_DAILYDETAILSBUY",
             "RPT_BILLBOARD_DAILYDETAILSSELL")
    raw, sources, missing = {}, [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        jobs = {executor.submit(fetch_paginated, name, trade_date, raw_dir=raw_dir,
                                deadline=deadline, require_event_ids=(index == 0)): name
                for index, name in enumerate(names)}
        for future in concurrent.futures.as_completed(jobs):
            name = jobs[future]
            try:
                raw[name], refs = future.result()
                sources.extend(refs)
            except Exception as exc:
                missing.append(f"{name}: {exc}")
    if names[0] not in raw:
        return _section(missing=missing, sources=sources)
    events = {}
    main_source = next((source["url"] for source in sources if source.get("label") == names[0]),
                       DATACENTER_URL)
    for row in raw[names[0]]:
        try:
            event_id = _event_id(row)
            events[event_id] = {
                "code": _code(row), "name": str(row.get("SECURITY_NAME_ABBR") or ""),
                "event_id": event_id, **_period(row, trade_date), "seats": [],
                "pct": _number(row.get("CHANGE_RATE")),
                "turnover_pct": _number(row.get("TURNOVERRATE")),
                "market": row.get("TRADE_MARKET"),
                "market_code": row.get("TRADE_MARKET_CODE"),
                "security_type_code": row.get("SECURITY_TYPE_CODE"),
                "buy_cny": _number(row.get("BILLBOARD_BUY_AMT")),
                "sell_cny": _number(row.get("BILLBOARD_SELL_AMT")),
                "net_cny": _number(row.get("BILLBOARD_NET_AMT")),
                **_source_metadata(main_source, trade_date),
            }
        except Exception as exc:
            missing.append(f"主榜事件无效: {exc}")
    for name, side in zip(names[1:], ("buy", "sell")):
        for row in raw.get(name, []):
            try:
                event_id = _event_id(row)
                if event_id not in events:
                    raise FeedError("席位无对应主榜事件")
                if _code(row) != events[event_id]["code"]:
                    raise FeedError("席位证券与主榜事件冲突")
                buy = _number(row.get("BUY"), required=True)
                sell = _number(row.get("SELL"), required=True)
                net = _number(row.get("NET"), required=True)
                if buy < 0 or sell < 0 or abs((buy - sell) - net) > .02:
                    raise FeedError("席位买卖金额与净额不一致")
                events[event_id]["seats"].append({
                    "seat_id": str(row.get("OPERATEDEPT_CODE") or ""),
                    "name": str(row.get("OPERATEDEPT_NAME") or ""),
                    "buy_cny": buy, "sell_cny": sell, "net_cny": net, "side": side,
                })
            except Exception as exc:
                missing.append(f"席位记录无效: {exc}")
    both_available = all(name in raw for name in names)
    for event in events.values():
        sides = {s["side"] for s in event["seats"]}
        event["seats_complete"] = both_available and sides == {"buy", "sell"}
        if not event["seats_complete"]:
            missing.append(f"{event['code']}事件{event['event_id']}买卖双榜不完整")
    status = "partial" if missing else "complete"
    return _section(status, sorted(events.values(), key=lambda r: (r["code"], r["event_id"])),
                    sorted(set(missing)), sources,
                    scope="已披露龙虎榜买卖前五席位；单日与累计事件分别保留")


def collect_institutions(trade_date: str, *, raw_dir: Path) -> dict:
    try:
        rows, sources = fetch_paginated("RPT_ORGANIZATION_TRADE_DETAILS", trade_date,
                    raw_dir=raw_dir, deadline=time.monotonic() + MAX_SECTION_SECONDS)
    except Exception as exc:
        return _section(missing=[str(exc)])
    output, missing = [], []
    for row in rows:
        try:
            buy = _number(row.get("BUY_AMT"), required=True)
            sell = _number(row.get("SELL_AMT"), required=True)
            net = _number(row.get("NET_BUY_AMT"), required=True)
            if buy < 0 or sell < 0 or abs((buy - sell) - net) > .02:
                raise FeedError("机构买卖金额与净额不一致")
            output.append({
                "code": _code(row), "name": str(row.get("SECURITY_NAME_ABBR") or ""),
                "buy_cny": buy, "sell_cny": sell, "net_cny": net,
                "pct": _number(row.get("CHANGE_RATE")),
                "turnover_pct": _number(row.get("TURNOVERRATE")),
                "industry": row.get("INDUSTRY") or row.get("INDUSTRY_NAME"),
                "market": row.get("MARKET"), "market_code": row.get("TRADE_MARKET_CODE"),
                "buy_count": _number(row.get("BUY_TIMES")),
                "sell_count": _number(row.get("SELL_TIMES")),
                **_period(row, trade_date),
                **_source_metadata(sources[0]["url"], trade_date),
            })
        except Exception as exc:
            missing.append(f"机构记录无效: {exc}")
    return _section("partial" if missing else "complete", output,
                    sorted(set(missing)), sources,
                    scope="已披露机构专用席位统计；不识别机构账户或持仓")


def collect(cutoff, *, window_start=None, raw_dir=None) -> dict:
    """Collect six independent sections, preserving failures and source date precision."""
    cutoff = _clock(cutoff)
    window_start = (_clock(window_start) if window_start is not None
                    else cutoff - dt.timedelta(days=1))
    if window_start >= cutoff:
        raise ValueError("window_start must precede cutoff")
    if raw_dir is None:
        run_id = dt.datetime.now(TZ).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
        raw_dir = research_path(f"night_radar/raw/{run_id}")
    raw_dir = Path(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    result = {"schema_version": 1, "source_as_of": cutoff.isoformat(timespec="seconds"),
              "trade_date": None, "sections": {key: _section() for key in SECTION_KEYS}}
    try:
        trade_date = _trade_date(cutoff)
        result["trade_date"] = trade_date
    except Exception as exc:
        trade_date = None
        for key in ("limit_up", "broker_seats", "institutions"):
            result["sections"][key] = _section(missing=[str(exc)])

    jobs = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        if trade_date:
            for key, collector in (("limit_up", collect_limit_up),
                                   ("broker_seats", collect_broker_seats),
                                   ("institutions", collect_institutions)):
                jobs[executor.submit(collector, trade_date, raw_dir=raw_dir)] = key
        try:
            from night_radar_disclosures import collect_disclosures
            jobs[executor.submit(collect_disclosures, cutoff, window_start, raw_dir / "disclosures")] = "disclosures"
        except Exception as exc:
            for key in ("announcements", "earnings", "repurchases"):
                result["sections"][key] = _section(missing=[f"公告采集器不可用: {exc}"])
        for future in concurrent.futures.as_completed(jobs):
            key = jobs[future]
            try:
                value = future.result()
                if key == "disclosures":
                    for section_key in ("announcements", "earnings", "repurchases"):
                        result["sections"][section_key] = value[section_key]
                else:
                    result["sections"][key] = value
            except Exception as exc:
                keys = ("announcements", "earnings", "repurchases") if key == "disclosures" else (key,)
                for section_key in keys:
                    result["sections"][section_key] = _section(missing=[str(exc)])
    return result


def audit_sse_sample(trade_date: str, *, raw_dir: Path, code: str, ref_type="11") -> dict:
    """Persist one real SSE sample separately; never pretend it verifies the full pool."""
    date_compact = trade_date.replace("-", "")
    url = _url("https://query.sse.com.cn/marketdata/tradedata/queryTradeOpenInfo.do", {
        "Token": "QUERY", "tradeDate": date_compact, "refType": ref_type,
        "secCode": code, "orderB": "desc", "orderS": "desc",
    })
    req = urllib.request.Request(url, headers={**_HEADERS,
              "Referer": "https://www.sse.com.cn/disclosure/diclosure/public/"})
    payload, error = None, None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as res:
                payload = json.loads(res.read().decode("utf-8"))
            break
        except Exception as exc:
            error = str(exc)
            if attempt == 0:
                time.sleep(.4)
    audit = {"source_url": url, "fetched_at": dt.datetime.now(TZ).isoformat(timespec="seconds"),
             "trade_date": trade_date, "code": code, "scope": "一只证券、一个披露原因的官方抽检"}
    if payload is None:
        audit.update({"status": "unavailable", "error": error})
    else:
        atomic_json(Path(raw_dir) / "sse-sample.raw.json", payload)
        rows = (payload.get("pageHelp") or {}).get("data") or []
        if (not rows or any(str(r.get("tradeDate")) != date_compact
                           or str(r.get("secCode")) != code for r in rows)):
            audit.update({"status": "unavailable", "error": "官方样本日期或证券不一致"})
        else:
            buy = sum(float(r["branchTxAmt"]) for r in rows if r.get("bsType") == "B")
            sell = sum(float(r["branchTxAmt"]) for r in rows if r.get("bsType") == "S")
            audit.update({"status": "complete", "buy_cny": buy, "sell_cny": sell,
                          "net_cny": buy - sell, "row_count": len(rows),
                          "period_start": rows[0].get("abnormalStart"),
                          "period_end": rows[0].get("abnormalEnd")})
    atomic_json(Path(raw_dir) / "sse-sample.audit.json", audit)
    return audit
