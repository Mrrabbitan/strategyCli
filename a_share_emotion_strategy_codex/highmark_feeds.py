"""Bounded public evidence for the daily main-board high-mark information monitor.

This collector does not run a selection strategy or infer regulator resets.  A
complete limit-up pool means the provider's native dated pool was verified; it
does not mean all market trend leaders were scanned.  Daily bars stay native and
unadjusted.  The separately retained qfq series is only an adjustment diagnostic,
never a replacement for native closes or an ex-right reference price.
"""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

from research_store import atomic_json


TZ = ZoneInfo("Asia/Shanghai")
TIMEOUT = 6
MAX_BODY = 2 * 1024 * 1024
HISTORY_COUNT = 100
MIN_HISTORY = 65
POOL_URL = "https://push2ex.eastmoney.com/getTopicZTPool"
KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
QUOTE_URL = "https://qt.gtimg.cn/q="
BENCHMARKS = {"sh000002": "上证A股指数", "sz399107": "深证A股指数"}


class EvidenceError(RuntimeError):
    """An upstream response failed its identity, date or completeness contract."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _number(value):
    if isinstance(value, bool):
        raise EvidenceError("布尔值不是数值")
    try:
        number = float(value)
    except (ValueError, TypeError):
        raise EvidenceError("原生数值缺失或无效")
    if not math.isfinite(number):
        raise EvidenceError("原生数值不是有限数")
    return number


def _main_board(code):
    return bool(re.fullmatch(r"(?:60[0135]\d{3}|00[0123]\d{3})", str(code)))


def _name_eligible(name):
    text = re.sub(r"\s+", "", str(name)).upper()
    return bool(text) and "ST" not in text and not text.startswith(("N", "C", "退")) and "退市" not in text


def _symbol(code):
    if not _main_board(code):
        raise EvidenceError("不属于沪深普通主板A股代码范围")
    return ("sh" if code.startswith("6") else "sz") + code


def _url(base, params):
    return base + "?" + urllib.parse.urlencode(params)


class PublicEvidence:
    """One six-second request per item; denial/challenge stops that source.

    No cookies, authentication rotation, challenge execution, redirects or stale
    cache substitution are used.  Raw success and HTTP failure bodies are stored
    byte-for-byte in the caller's private directory with SHA-256 metadata.
    """
    def __init__(self, raw_dir):
        self.raw_dir = Path(raw_dir)
        self.raw_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._blocked = set()
        self._lock = threading.Lock()

    def read(self, url, label, encoding="utf-8"):
        host = urllib.parse.urlparse(url).netloc
        with self._lock:
            if host in self._blocked:
                raise EvidenceError(host + "已出现访问拒绝或挑战，本轮不再请求")
        label = re.sub(r"[^A-Za-z0-9_.-]", "_", label)
        at = dt.datetime.now(TZ).isoformat(timespec="seconds")
        meta = {"url": url, "fetched_at": at, "attempt_count": 1,
                "timeout_seconds": TIMEOUT, "source_time": None}
        raw = b""
        try:
            request = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0", "Referer": "https://stock.qq.com/"})
            opener = urllib.request.build_opener(_NoRedirect)
            with opener.open(request, timeout=TIMEOUT) as response:
                meta["http_status"] = response.status
                raw = response.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                raise EvidenceError("正文超出2MiB上限")
            body = raw.decode(encoding, errors="strict")
            if re.search(r"captcha|access\s+denied|verify\s+you\s+are\s+human|访问验证|安全验证|访问过于频繁", body[:20000], re.I):
                with self._lock:
                    self._blocked.add(host)
                raise EvidenceError("上游访问挑战，不执行或绕过")
            meta["status"] = "success"
            return body, meta
        except urllib.error.HTTPError as exc:
            meta["http_status"] = exc.code
            try:
                raw = exc.read(MAX_BODY + 1)
            except OSError:
                pass
            if exc.code in (401, 403, 429) or 300 <= exc.code < 400:
                with self._lock:
                    self._blocked.add(host)
            meta.update(status="failed", error_type=type(exc).__name__,
                        error=f"HTTP {exc.code}")
            raise EvidenceError(f"{host}: HTTP {exc.code}")
        except Exception as exc:
            meta.update(status="failed", error_type=type(exc).__name__, error=str(exc))
            raise EvidenceError(f"{label}: {type(exc).__name__}: {exc}")
        finally:
            if raw:
                (self.raw_dir / (label + ".raw")).write_bytes(raw)
            meta["raw_sha256"] = hashlib.sha256(raw).hexdigest()
            meta["raw_bytes"] = len(raw)
            atomic_json(self.raw_dir / (label + ".json"), meta)

    def json(self, url, label):
        body, source = self.read(url, label)
        try:
            payload = json.loads(body)
        except (ValueError, TypeError) as exc:
            raise EvidenceError(f"{label}: JSON无效") from exc
        return payload, source


def normalize_pool(payload, trade_date, source):
    """The native pool's qdate/count/code checks mirror night_radar_feeds."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or str(data.get("qdate")) != trade_date.replace("-", ""):
        raise EvidenceError("涨停池原生日期与行情日不一致或缺失")
    rows, total = data.get("pool"), data.get("tc")
    if isinstance(total, bool) or not str(total).isdigit():
        raise EvidenceError("涨停池原生总数无效")
    if not isinstance(rows, list) or len(rows) != int(total):
        raise EvidenceError("涨停池条数与原生总数不一致")
    seen, output, excluded = set(), [], []
    for row in rows:
        if not isinstance(row, dict):
            raise EvidenceError("涨停池记录无效")
        code, name = str(row.get("c") or ""), str(row.get("n") or "").strip()
        if not re.fullmatch(r"\d{6}", code) or code in seen:
            raise EvidenceError("涨停池代码缺失或重复")
        seen.add(code)
        boards = row.get("lbc")
        if isinstance(boards, bool) or not str(boards).isdigit() or int(boards) < 1 or not name:
            raise EvidenceError("涨停池原生连板数或名称无效")
        close = _number(row.get("p")) / 1000
        if close <= 0:
            raise EvidenceError("涨停池原生收盘价无效")
        if not _main_board(code) or not _name_eligible(name):
            excluded.append({"code": code, "name": name,
                             "reason": "非沪深主板普通A股、ST、退市或无正常限制阶段名称"})
            continue
        if int(boards) < 2:
            continue
        native_pct = row.get("zdp") if row.get("zdp") is not None else row.get("zf")
        output.append({"code": code, "name": name, "boards": int(boards),
                       "close": close, "pct": _number(native_pct),
                       "source_date": trade_date, "source_url": source["url"],
                       "source": source, "membership": "current_limit_up_2plus"})
    return {"status": "complete", "items": sorted(output, key=lambda r: (-r["boards"], r["code"])),
            "source_count": int(total), "unique_source_count": len(seen),
            "source_date": trade_date, "pool_complete": True,
            "coverage_kind": "provider_dated_limit_up_pool", "all_market_trend_leaders_complete": False,
            "sources": [source], "excluded": excluded, "missing": []}


def normalize_history(payload, symbol, trade_date, adjustment="none", minimum=MIN_HISTORY):
    node = ((payload.get("data") or {}).get(symbol) or {}) if isinstance(payload, dict) else {}
    key = "qfqday" if adjustment == "qfq" else "day"
    raw = node.get(key)
    # A provider index has no corporate-action factor and may return native day.
    if adjustment == "qfq" and symbol in BENCHMARKS and raw is None:
        raw = node.get("day")
    if not isinstance(raw, list) or not raw:
        raise EvidenceError(symbol + "缺少指定复权模式日线")
    rows, dates, future_count = [], [], 0
    for item in raw:
        if not isinstance(item, list) or len(item) < 6:
            raise EvidenceError(symbol + "日线字段不完整")
        date = str(item[0])
        dt.date.fromisoformat(date)
        if date in dates or dates and date <= dates[-1]:
            raise EvidenceError(symbol + "日线日期重复或非递增")
        dates.append(date)
        if date > trade_date:
            future_count += 1
            continue
        values = {k: _number(item[i]) for i, k in enumerate(("open", "close", "high", "low", "volume_hands"), 1)}
        if any(values[k] <= 0 for k in ("open", "close", "high", "low")) or values["volume_hands"] < 0:
            raise EvidenceError(symbol + "日线价格或成交量无效")
        if values["low"] > min(values["open"], values["close"]) or values["high"] < max(values["open"], values["close"]):
            raise EvidenceError(symbol + "日线高低价关系无效")
        rows.append({"date": date, **values})
    if len(rows) < minimum:
        raise EvidenceError(f"{symbol}不足{minimum}根可核验日线（取得{len(rows)}）")
    if rows[-1]["date"] != trade_date:
        raise EvidenceError(symbol + "缺少指定行情日收盘，不能将旧日线标新")
    # Native daily closes are not the ex-right reference close.  Do not invent
    # prev_close or a native pct from adjacent unadjusted closes here.
    return {"status": "complete", "symbol": symbol, "history": rows,
            "adjustment": "provider_qfq_diagnostic_only" if adjustment == "qfq" else "none",
            "price_unit": "CNY" if symbol not in BENCHMARKS else "index_points",
            "volume_unit": "provider_native_hands", "source_date": trade_date,
            "future_rows_excluded": future_count, "native_pct_available": False,
            "native_reference_close_available": False, "missing": []}


def adjustment_diagnostic(native, adjusted):
    left = {r["date"]: r["close"] for r in native}
    right = {r["date"]: r["close"] for r in adjusted}
    if not left or set(left) != set(right):
        return {"status": "unavailable", "corporate_action_detected": None,
                "reason": "原生与前复权日线日期未完整一致"}
    dates = sorted(left)
    factors = [left[d] / right[d] for d in dates]
    changes = [dates[i] for i in range(1, len(dates))
               if abs(factors[i] / factors[i - 1] - 1) > .0005]
    return {"status": "complete", "corporate_action_detected": bool(changes),
            "possible_adjustment_dates": changes,
            "method": "同源未复权/前复权收盘比例相邻变化>0.05%诊断；不替代除权参考价",
            "qfq_not_used_for_native_returns": True}


def normalize_quotes(body, symbols, trade_date, source):
    output, missing = {}, []
    requested = set(symbols)
    seen_symbols = set()
    for match in re.finditer(r'v_([a-z]{2}\d{6})="([^"\r\n]*)"\s*;', body):
        symbol, text = match.groups()
        if symbol not in requested:
            continue
        if symbol in seen_symbols:
            raise EvidenceError(symbol + "独立报价重复")
        seen_symbols.add(symbol)
        f = text.split("~")
        try:
            if len(f) < 39 or f[2] != symbol[2:]:
                raise EvidenceError("报价代码或字段无效")
            stamp = dt.datetime.strptime(f[30], "%Y%m%d%H%M%S").replace(tzinfo=TZ)
            if stamp.date().isoformat() != trade_date or stamp.time() < dt.time(15):
                raise EvidenceError("报价源时点不属于指定行情日收盘后")
            price, prev_close, pct = _number(f[3]), _number(f[4]), _number(f[32])
            if price <= 0 or prev_close <= 0 or abs((price / prev_close - 1) * 100 - pct) > .021:
                raise EvidenceError("原生收盘、参考昨收与涨跌幅不一致")
            output[symbol] = {"code": f[2], "symbol": symbol, "name": f[1],
                              "price": price, "prev_close": prev_close, "pct": pct,
                              "source_time": stamp.isoformat(), "source_date": trade_date,
                              "source": source, "source_url": source["url"],
                              "amount_native_wan_cny": _number(f[37]),
                              "turnover_pct": _number(f[38]), "status": "complete"}
        except (ValueError, EvidenceError) as exc:
            missing.append(symbol + "独立报价无效: " + str(exc))
    for symbol in requested - set(output):
        if not any(m.startswith(symbol) for m in missing):
            missing.append(symbol + "独立报价缺失")
    return output, missing


def _retained_items(retained):
    rows, missing = {}, []
    for item in retained or []:
        if not isinstance(item, dict):
            missing.append("保留高标记录格式无效")
            continue
        code, name = str(item.get("code") or ""), str(item.get("name") or "").strip()
        source = item.get("source") or item.get("source_url")
        explicit = bool(source.strip()) if isinstance(source, str) else isinstance(source, dict) and bool(source.get("url"))
        if not _main_board(code) or not _name_eligible(name) or not explicit:
            missing.append((code or "未知代码") + "保留高标缺少普通主板身份、有效名称或明示来源")
            continue
        rows[code] = {**item, "code": code, "name": name,
                      "membership": "retained_prior_highmark", "membership_source": source}
    return rows, missing


def collect_evidence(trade_date: str, raw_dir: Path, retained: list[dict]) -> dict:
    """Collect a fresh dated 2+ ladder, explicitly retained names and benchmarks.

    Every network call is bounded to one six-second request.  Individual source
    failures do not become a successful empty pool, and no retained name fills a
    failed pool's coverage.  The caller remains responsible for exchange calendar,
    regulator announcements/resets, normal-limit status and the final calculation.
    """
    day = dt.date.fromisoformat(trade_date)
    if day.isoformat() != trade_date or day > dt.datetime.now(TZ).date():
        raise EvidenceError("行情日无效或在未来")
    feed = PublicEvidence(raw_dir)
    result = {"schema_version": 1, "trade_date": trade_date,
              "fetched_at": dt.datetime.now(TZ).isoformat(timespec="seconds"),
              "pool": {}, "stocks": {}, "quotes": {}, "benchmarks": {}, "exclusions": [],
              "disclosures": {"status": "unavailable", "items": [],
                              "missing": ["本采集器不核验停复牌/严重异动公告、监管周期重置及历史证券状态"]},
              "sources": [], "missing": []}
    pool_url = _url(POOL_URL, {"ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt",
                             "Pageindex": 0, "pagesize": 1000, "sort": "fbt:asc",
                             "date": trade_date.replace("-", "")})
    try:
        payload, source = feed.json(pool_url, "limit-up-pool")
        result["sources"].append(source)
        result["pool"] = normalize_pool(payload, trade_date, source)
        result["exclusions"].extend(result["pool"]["excluded"])
    except Exception as exc:
        result["pool"] = {"status": "unavailable", "items": [], "pool_complete": False,
                          "coverage_kind": "provider_dated_limit_up_pool", "all_market_trend_leaders_complete": False,
                          "sources": [], "missing": [str(exc)]}
        result["missing"].append("当日动态高标池不可用: " + str(exc))
    selected, retained_missing = _retained_items(retained)
    result["missing"].extend(retained_missing)
    for item in result["pool"]["items"]:
        selected[item["code"]] = item
    symbols = [_symbol(code) for code in sorted(selected)] + list(BENCHMARKS)

    def daily(symbol, adjustment):
        # This provider's qfq end boundary is exclusive, unlike its native day
        # response.  Request the next date then enforce <= trade_date locally;
        # never append today's bar to a historical adjusted series by inference.
        end_boundary = (day + dt.timedelta(days=1)).isoformat()
        url = _url(KLINE_URL, {"param": f"{symbol},day,,{end_boundary},{HISTORY_COUNT},{'qfq' if adjustment == 'qfq' else ''}"})
        payload, source = feed.json(url, symbol + "-" + adjustment)
        normalized = normalize_history(payload, symbol, trade_date, adjustment)
        normalized.update(source=source, source_url=url)
        return normalized

    jobs = {}
    histories = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        for symbol in symbols:
            jobs[executor.submit(daily, symbol, "none")] = (symbol, "none")
            if symbol not in BENCHMARKS:
                jobs[executor.submit(daily, symbol, "qfq")] = (symbol, "qfq")
        for future in concurrent.futures.as_completed(jobs):
            symbol, adjustment = jobs[future]
            try:
                value = future.result()
                histories[(symbol, adjustment)] = value
                result["sources"].append(value["source"])
            except Exception as exc:
                result["missing"].append(symbol + "/" + adjustment + ": " + str(exc))

    quotes_by_symbol = {}
    for offset in range(0, len(symbols), 50):
        batch = symbols[offset:offset + 50]
        if not batch:
            continue
        url = QUOTE_URL + ",".join(batch)
        try:
            body, source = feed.read(url, "quotes-" + str(offset // 50 + 1), "gb18030")
            result["sources"].append(source)
            values, missing = normalize_quotes(body, batch, trade_date, source)
            quotes_by_symbol.update(values)
            result["missing"].extend(missing)
        except Exception as exc:
            result["missing"].append("独立收盘报价失败: " + str(exc))

    for symbol in symbols:
        native = histories.get((symbol, "none"))
        if symbol in BENCHMARKS:
            result["benchmarks"][symbol] = native or {"status": "unavailable", "symbol": symbol, "history": []}
            result["benchmarks"][symbol]["name"] = BENCHMARKS[symbol]
            benchmark_quote = quotes_by_symbol.get(symbol)
            result["benchmarks"][symbol]["quote"] = benchmark_quote
            if native and not benchmark_quote:
                result["benchmarks"][symbol]["status"] = "partial"
                result["benchmarks"][symbol]["missing"].append("指数缺少独立收盘报价")
            elif native and abs(native["history"][-1]["close"] - benchmark_quote["price"]) > .0005:
                result["benchmarks"][symbol]["status"] = "unavailable"
                result["benchmarks"][symbol]["missing"].append("指数日线末收盘与独立报价冲突")
            continue
        code = symbol[2:]
        row = {**selected[code], **(native or {"status": "unavailable", "symbol": symbol, "history": []}),
               "security_status_verified": False,
               "security_status_note": "代码与原生名称仅作普通主板非ST筛查；未核验停复牌、上市期、退市整理与历史状态"}
        qfq = histories.get((symbol, "qfq"))
        row["adjustment_check"] = (adjustment_diagnostic(native["history"], qfq["history"])
                                   if native and qfq else {"status": "unavailable", "corporate_action_detected": None})
        row["adjusted_history"] = qfq["history"] if qfq else []
        if qfq:
            row["qfq_diagnostic"] = qfq
        quote = quotes_by_symbol.get(symbol)
        row["quote"] = quote
        if quote:
            result["quotes"][code] = quote
            if not _name_eligible(quote["name"]):
                row["status"] = "unavailable"
                row.setdefault("missing", []).append("当前原生报价名称进入ST、退市或无正常限制阶段")
            if native and abs(native["history"][-1]["close"] - quote["price"]) > .0005:
                row["status"] = "unavailable"
                row.setdefault("missing", []).append("日线末收盘与独立报价冲突")
            if row.get("membership") == "current_limit_up_2plus" and abs(row["close"] - quote["price"]) > .0005:
                row["status"] = "unavailable"
                row.setdefault("missing", []).append("动态涨停池收盘与独立报价冲突，可能为重标日期旧池")
            if native and native["history"][-1]["volume_hands"] <= 0:
                row["status"] = "unavailable"
                row.setdefault("missing", []).append("指定行情日原生日线无成交量，停牌/证券状态待核验")
        else:
            row["status"] = "partial" if native else "unavailable"
            row.setdefault("missing", []).append("缺少指定行情日独立收盘报价，不能确认末收盘")
        if row.get("status") == "unavailable":
            result["exclusions"].append({"code": code, "name": row["name"],
                                         "reason": "；".join(row.get("missing") or ["指定行情日历史日线不可用"])})
        result["stocks"][code] = row
    pool_items = [result["stocks"].get(row["code"]) for row in result["pool"]["items"]]
    failed_crosschecks = [row["code"] for row in pool_items if row and row.get("status") != "complete"]
    if failed_crosschecks and result["pool"]["status"] == "complete":
        result["pool"]["status"] = "partial"
        result["pool"]["pool_complete"] = False
        result["pool"]["missing"].append("高标收盘交叉核验不足: " + ",".join(failed_crosschecks))
    result["pool"]["crosscheck_count"] = sum(bool(row and row.get("status") == "complete") for row in pool_items)
    result["missing"].extend(result["disclosures"]["missing"])
    result["missing"] = sorted(set(result["missing"]))
    result["sources"] = sorted(result["sources"], key=lambda source: source["url"])
    atomic_json(Path(raw_dir) / "normalized-evidence.json", result)
    return result
