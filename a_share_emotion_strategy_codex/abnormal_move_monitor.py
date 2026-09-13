"""Full-market 10-trading-day abnormal-rise monitor.

The module is research-only.  It uses adjusted daily bars plus independently
validated public quotes; it never connects to a broker or emits orders.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import math
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple


ROOT = Path(__file__).resolve().parent
from research_store import private_path
DEFAULT_CACHE = private_path("data/cache/abnormal_10d/history.json")
TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
EASTMONEY_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EASTMONEY_BJ_URL = "https://push2.eastmoney.com/api/qt/clist/get"
EASTMONEY_PROFILE_URL = "https://push2.eastmoney.com/api/qt/stock/get"
SINA_BJ_COUNT_URL = "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeStockCount"
SINA_BJ_LIST_URL = "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
SINA_KLINE_URL = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"


class AbnormalMoveDataError(RuntimeError):
    pass


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def is_common_a_share(code: str) -> bool:
    """Return whether *code* is an active-market ordinary A-share code shape."""
    return bool(re.fullmatch(
        r"(?:60[0135]\d{3}|68[89]\d{3}|00[0123]\d{3}|30[01]\d{3}|920\d{3})", code
    ))


def market_name(code: str) -> str:
    if code.startswith("920"):
        return "北交所"
    if code.startswith(("68",)):
        return "科创板"
    if code.startswith(("30",)):
        return "创业板"
    return "沪深主板"


def _symbol(code: str) -> str:
    if code.startswith("920"):
        return "bj" + code
    return ("sh" if code.startswith(("6",)) else "sz") + code


def _secid(code: str) -> str:
    return ("1." if code.startswith("6") else "0.") + code


def _request_json(url: str, params: Optional[Dict[str, Any]] = None, timeout: int = 12) -> Dict[str, Any]:
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://quote.eastmoney.com/",
    })
    last: Optional[Exception] = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            last = exc
            if attempt == 0:
                time.sleep(0.35)
    raise AbnormalMoveDataError(f"request failed: {url}: {last}")


def fetch_tencent_history(code: str, count: int = 18) -> List[Dict[str, Any]]:
    symbol = _symbol(code)
    payload = _request_json(TENCENT_KLINE_URL, {"param": f"{symbol},day,,,{count},qfq"}, timeout=6)
    node = ((payload.get("data") or {}).get(symbol) or {})
    raw_rows = node.get("qfqday") or node.get("day") or []
    rows: List[Dict[str, Any]] = []
    for row in raw_rows:
        if len(row) < 6:
            continue
        rows.append({
            "date": str(row[0]), "open": _f(row[1]), "close": _f(row[2]),
            "high": _f(row[3]), "low": _f(row[4]), "volume": _f(row[5]),
        })
    return rows[-15:]


def fetch_primary_history(code: str, count: int = 18) -> List[Dict[str, Any]]:
    # Tencent currently exposes only the latest bar for many 920-series symbols.
    return fetch_sina_history(code, count) if code.startswith("920") else fetch_tencent_history(code, count)


def fetch_eastmoney_history(code: str, count: int = 18) -> List[Dict[str, Any]]:
    payload = _request_json(EASTMONEY_KLINE_URL, {
        "secid": _secid(code), "klt": 101, "fqt": 1, "lmt": count,
        "end": "20500101", "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
    })
    klines = ((payload.get("data") or {}).get("klines") or [])
    rows: List[Dict[str, Any]] = []
    for line in klines:
        parts = str(line).split(",")
        if len(parts) < 6:
            continue
        rows.append({
            "date": parts[0], "open": _f(parts[1]), "close": _f(parts[2]),
            "high": _f(parts[3]), "low": _f(parts[4]), "volume": _f(parts[5]),
        })
    return rows[-15:]


def fetch_sina_history(code: str, count: int = 18) -> List[Dict[str, Any]]:
    payload = _request_json(SINA_KLINE_URL, {
        "symbol": _symbol(code), "scale": 240, "ma": "no", "datalen": count,
    })
    if not isinstance(payload, list):
        raise AbnormalMoveDataError(f"Sina history invalid for {code}")
    rows = []
    for row in payload:
        rows.append({
            "date": str(row.get("day") or ""), "open": _f(row.get("open")),
            "close": _f(row.get("close")), "high": _f(row.get("high")),
            "low": _f(row.get("low")), "volume": _f(row.get("volume")),
        })
    return rows[-15:]


def fetch_verification_history(code: str, count: int = 18) -> List[Dict[str, Any]]:
    if code.startswith("920"):
        return fetch_eastmoney_history(code, count)
    try:
        return fetch_sina_history(code, count)
    except Exception:
        return fetch_eastmoney_history(code, count)


def fetch_beijing_snapshot() -> Tuple[Dict[str, Dict[str, Any]], int]:
    """Fetch Beijing Exchange ordinary A-shares without changing other market scans."""
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"}
    try:
        count_url = SINA_BJ_COUNT_URL + "?" + urllib.parse.urlencode({"node": "hs_bjs"})
        with urllib.request.urlopen(urllib.request.Request(count_url, headers=headers), timeout=10) as response:
            count_raw = response.read().decode("utf-8", errors="replace")
        match = re.search(r"\d+", count_raw)
        if not match:
            raise AbnormalMoveDataError("Sina Beijing count missing")
        expected = int(match.group())
        rows: List[Dict[str, Any]] = []
        for page in range(1, math.ceil(expected / 80) + 1):
            params = {
                "page": page, "num": 80, "sort": "symbol", "asc": 1,
                "node": "hs_bjs", "symbol": "", "_s_r_a": "page",
            }
            url = SINA_BJ_LIST_URL + "?" + urllib.parse.urlencode(params)
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=12) as response:
                rows.extend(json.loads(response.read().decode("utf-8")))
        result = {}
        for row in rows:
            code = str(row.get("code") or "")
            if not code.startswith("920"):
                continue
            result[code] = {
                "name": str(row.get("name") or ""), "price": _f(row.get("trade")),
                "pct": _f(row.get("changepercent")), "volume": _f(row.get("volume")),
                "amount_cny": _f(row.get("amount")), "turnover": _f(row.get("turnoverratio")),
                "high": _f(row.get("high")), "low": _f(row.get("low")),
                "open": _f(row.get("open")), "prev_close": _f(row.get("settlement")),
            }
        if not result:
            raise AbnormalMoveDataError("Sina Beijing list empty")
        return result, expected
    except Exception:
        params = {
            "pn": 1, "pz": 1000, "po": 1, "np": 1, "fltt": 2, "invt": 2,
            "fid": "f12", "fs": "m:0+t:81+s:2048",
            "fields": "f2,f3,f5,f6,f8,f12,f14,f15,f16,f17,f18",
        }
        payload = _request_json(EASTMONEY_BJ_URL, params)
        data = payload.get("data") or {}
        result = {}
        for row in data.get("diff") or []:
            code = str(row.get("f12") or "")
            if not code.startswith("920"):
                continue
            result[code] = {
                "name": str(row.get("f14") or ""), "price": _f(row.get("f2")),
                "pct": _f(row.get("f3")), "volume": _f(row.get("f5")),
                "amount_cny": _f(row.get("f6")), "turnover": _f(row.get("f8")),
                "high": _f(row.get("f15")), "low": _f(row.get("f16")),
                "open": _f(row.get("f17")), "prev_close": _f(row.get("f18")),
            }
        if not result:
            raise AbnormalMoveDataError("Beijing market sources returned no ordinary A-shares")
        return result, int(_f(data.get("total"), len(result)))


def fetch_eastmoney_industry(code: str) -> str:
    payload = _request_json(EASTMONEY_PROFILE_URL, {
        "secid": _secid(code), "fields": "f57,f58,f127",
    })
    return str((payload.get("data") or {}).get("f127") or "").strip()


def fetch_tencent_quotes(codes: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    symbols = [_symbol(code) for code in codes]
    if not symbols:
        return {}
    request = urllib.request.Request(
        TENCENT_QUOTE_URL + ",".join(symbols),
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://stock.qq.com/"},
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        raw = response.read().decode("gb18030", errors="replace")
    quotes: Dict[str, Dict[str, Any]] = {}
    for line in raw.splitlines():
        match = re.search(r'="(.*)";', line)
        if not match:
            continue
        parts = match.group(1).split("~")
        if len(parts) < 39:
            continue
        quotes[str(parts[2])] = {
            "price": _f(parts[3]), "timestamp": str(parts[30]),
            "high": _f(parts[33]), "low": _f(parts[34]), "open": _f(parts[5]),
        }
    return quotes


def load_history_cache(path: Path = DEFAULT_CACHE) -> Dict[str, Any]:
    if not path.exists():
        return {"version": 1, "expected_count": 0, "histories": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload.get("histories"), dict):
            raise ValueError("histories missing")
        return payload
    except (OSError, ValueError, json.JSONDecodeError):
        return {"version": 1, "expected_count": 0, "histories": {}}


def save_history_cache(cache: Dict[str, Any], path: Path = DEFAULT_CACHE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(cache, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)


def _completed_rows(rows: List[Dict[str, Any]], cutoff: dt.date) -> List[Dict[str, Any]]:
    return [row for row in rows if str(row.get("date", "")) <= cutoff.isoformat() and _f(row.get("close")) > 0]


def _score_band(score: Optional[float]) -> str:
    if score is None:
        return "数据不足"
    return "高" if score >= 70 else ("中" if score >= 50 else "低")


def _weighted_score(components: List[Tuple[float, Optional[float]]]) -> Optional[float]:
    available = [(weight, value) for weight, value in components if value is not None]
    if not available:
        return None
    return round(sum(weight * _clamp(float(value)) / 100 for weight, value in available) / sum(w for w, _ in available) * 100, 2)


def score_candidate(
    current: Dict[str, Any], prior: List[Dict[str, Any]], gain_pct: float,
    industry_limit_count: Optional[int], risk_titles: Optional[List[str]],
    score_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    score_config = score_config or {}
    continuation_weights = score_config.get("continuation_weights") or {
        "trend_structure": 30, "momentum_5d": 20, "volume_confirmation": 20,
        "close_location": 15, "industry_linkage": 15,
    }
    risk_weights = score_config.get("pullback_risk_weights") or {
        "overextension": 30, "volatility_upper_shadow": 25,
        "volume_price_divergence": 20, "weak_close": 15, "announcement_risk": 10,
    }
    closes = [_f(row.get("close")) for row in prior]
    latest = closes[-1]
    ma5 = sum(closes[-5:]) / 5
    ma10 = sum(closes[-10:]) / 10
    trend = 100.0 if current["price"] >= ma5 >= ma10 else (65.0 if current["price"] >= ma5 else 25.0)
    momentum5 = (current["price"] / closes[-5] - 1) * 100 if closes[-5] else 0.0
    momentum = _clamp((momentum5 + 5) * 3.2)
    recent_volumes = [_f(row.get("volume")) for row in prior[-8:]]
    baseline_volume = sum(recent_volumes[:5]) / max(len(recent_volumes[:5]), 1)
    current_volume = _f(current.get("volume"), recent_volumes[-1] if recent_volumes else 0)
    volume_ratio = current_volume / baseline_volume if baseline_volume else 0.0
    volume_confirmation = _clamp(45 + (volume_ratio - 1) * 45) if baseline_volume else None
    day_range = max(_f(current.get("high")) - _f(current.get("low")), 0.0)
    close_location = (
        _clamp((current["price"] - _f(current.get("low"))) / day_range * 100)
        if day_range > 0 else None
    )
    linkage = _clamp((industry_limit_count or 0) * 25) if industry_limit_count is not None else None
    continuation = _weighted_score([
        (_f(continuation_weights.get("trend_structure")), trend),
        (_f(continuation_weights.get("momentum_5d")), momentum),
        (_f(continuation_weights.get("volume_confirmation")), volume_confirmation),
        (_f(continuation_weights.get("close_location")), close_location),
        (_f(continuation_weights.get("industry_linkage")), linkage),
    ])

    overextension = _clamp(50 + (gain_pct - 80) * 1.5)
    true_ranges = []
    upper_shadows = []
    for row in prior[-10:]:
        close = _f(row.get("close"))
        high, low, opened = _f(row.get("high")), _f(row.get("low")), _f(row.get("open"))
        if close > 0:
            true_ranges.append((high - low) / close * 100)
            upper_shadows.append(max(high - max(opened, close), 0) / close * 100)
    volatility = _clamp((sum(true_ranges) / max(len(true_ranges), 1)) * 12 + max(upper_shadows or [0]) * 10)
    divergence = _clamp((volume_ratio - 1) * 45 + max(-momentum5, 0) * 8) if baseline_volume else None
    weak_close = (100 - close_location) if close_location is not None else None
    announcement_risk = 100.0 if risk_titles else (0.0 if risk_titles == [] else None)
    pullback = _weighted_score([
        (_f(risk_weights.get("overextension")), overextension),
        (_f(risk_weights.get("volatility_upper_shadow")), volatility),
        (_f(risk_weights.get("volume_price_divergence")), divergence),
        (_f(risk_weights.get("weak_close")), weak_close),
        (_f(risk_weights.get("announcement_risk")), announcement_risk),
    ])
    if continuation is None or pullback is None:
        outlook = "数据不足"
    elif continuation >= 70 and pullback < 50:
        outlook = "强势延续观察"
    elif continuation >= 70:
        outlook = "高波动延续"
    elif pullback >= 70:
        outlook = "回撤风险较高"
    else:
        outlook = "分歧等待"
    positives = []
    if trend >= 100:
        positives.append("价格保持MA5/MA10多头结构")
    if volume_confirmation is not None and volume_confirmation >= 70:
        positives.append("量能承接较强")
    if linkage is not None and linkage >= 50:
        positives.append("行业涨停联动")
    risks = []
    if gain_pct >= 100:
        risks.append("10日涨幅已达翻倍异动")
    if volatility >= 70:
        risks.append("波动或上影偏高")
    if announcement_risk is not None and announcement_risk >= 100:
        risks.append("近期公告命中硬风险词")
    return {
        "continuation_score": continuation, "continuation_band": _score_band(continuation),
        "pullback_risk_score": pullback, "pullback_risk_band": _score_band(pullback),
        "outlook": outlook, "positive_factors": positives or ["暂无突出加分项"],
        "risk_factors": risks or ["未发现规则内突出风险项"],
        "score_complete": all(value is not None for value in (volume_confirmation, close_location, linkage, announcement_risk)),
    }


def _empty_report(error: str, scope: str = "全部在市A股普通股票") -> Dict[str, Any]:
    return {
        "abnormal_10d_monitor": {
            "label": "10日翻倍异动监控", "scope": scope, "as_of": "", "trigger_count": 0,
            "warning_count": 0, "items": [], "unverified_items": [],
            "methodology": "10个交易日复权累计涨幅；未来5日为规则化研究评分，不是确定性预测",
        },
        "abnormal_10d_data_quality": {
            "status": "error", "actionable": False, "errors": [error], "warnings": [],
            "universe_count": 0, "expected_count": 0, "universe_coverage_ratio": 0.0,
            "history_coverage_ratio": 0.0, "dual_source_verified_count": 0,
            "missing_codes": [], "synthetic_data_used": False,
        },
    }


def build_abnormal_10d_report(
    report_date: dt.date,
    slot: str,
    market_snapshot: Dict[str, Dict[str, Any]],
    expected_count: int,
    config: Dict[str, Any],
    theme_counts: Optional[Dict[str, int]] = None,
    industry_by_code: Optional[Dict[str, str]] = None,
    risk_title_fetcher: Optional[Callable[[str], List[str]]] = None,
    history_fetcher: Callable[[str, int], List[Dict[str, Any]]] = fetch_primary_history,
    verification_history_fetcher: Callable[[str, int], List[Dict[str, Any]]] = fetch_verification_history,
    quote_fetcher: Callable[[Iterable[str]], Dict[str, Dict[str, Any]]] = fetch_tencent_quotes,
    industry_fetcher: Optional[Callable[[str], str]] = fetch_eastmoney_industry,
    cache_path: Path = DEFAULT_CACHE,
    persist_cache: bool = True,
    as_of_date: Optional[dt.date] = None,
) -> Dict[str, Any]:
    cfg = config["abnormal_10d"]
    cache_days = int(cfg.get("history_cache_days", 15))
    cache = load_history_cache(cache_path)
    histories: Dict[str, Any] = cache["histories"]
    cutoff = (as_of_date or report_date - dt.timedelta(days=1)) if slot == "09_00" else report_date - dt.timedelta(days=1)
    universe = {code: row for code, row in market_snapshot.items() if is_common_a_share(code)}
    if slot == "09_00" and not universe:
        universe = {}
        for code, cached in histories.items():
            if not is_common_a_share(code):
                continue
            completed = _completed_rows(cached.get("rows", []), cutoff)
            latest = completed[-1] if completed else {}
            universe[code] = {
                "name": cached.get("name", ""), "price": _f(latest.get("close")),
                "open": _f(latest.get("open")), "high": _f(latest.get("high")),
                "low": _f(latest.get("low")), "volume": _f(latest.get("volume")),
            }
        expected_count = int(cache.get("expected_count") or expected_count)
    if not universe:
        return _empty_report("全市场普通A股行情为空")

    missing_history = []
    for code in universe:
        rows = _completed_rows((histories.get(code) or {}).get("rows", []), cutoff)
        required = 11 if slot == "09_00" else 10
        if len(rows) < required:
            missing_history.append(code)
    workers = int(cfg.get("history_workers", 20))
    fetched_errors: Dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(history_fetcher, code, 18): code for code in missing_history}
        completed_fetches = 0
        for future in as_completed(futures):
            code = futures[future]
            completed_fetches += 1
            try:
                rows = future.result()
                histories[code] = {
                    "name": universe[code].get("name", ""), "market": market_name(code),
                    "rows": rows[-cache_days:],
                }
            except Exception as exc:
                fetched_errors[code] = str(exc)
            if completed_fetches >= 100 and len(fetched_errors) / completed_fetches >= 0.90:
                for pending, pending_code in futures.items():
                    if not pending.done() and pending.cancel():
                        fetched_errors[pending_code] = "历史行情源批量失败，已触发熔断"
                break

    preliminary = []
    history_ok = 0
    insufficient: List[Dict[str, Any]] = []
    for code, current in universe.items():
        rows = _completed_rows((histories.get(code) or {}).get("rows", []), cutoff)
        required = 11 if slot == "09_00" else 10
        if len(rows) < required:
            insufficient.append({
                "code": code, "name": current.get("name", ""),
                "is_st": "ST" in str(current.get("name", "")).upper(),
                "reason": "不足11个可核验交易日",
            })
            continue
        history_ok += 1
        current_price = _f(rows[-1].get("close")) if slot == "09_00" else _f(current.get("price"))
        anchor = _f(rows[-11].get("close")) if slot == "09_00" else _f(rows[-10].get("close"))
        if current_price <= 0 or anchor <= 0:
            insufficient.append({"code": code, "name": current.get("name", ""), "reason": "当前价或历史锚点无效"})
            continue
        gain = (current_price / anchor - 1) * 100
        if gain + 1e-9 >= _f(cfg.get("warning_threshold_pct"), 80):
            preliminary.append((code, current, rows, current_price, anchor, gain))

    candidate_codes = [row[0] for row in preliminary]
    try:
        second_quotes = quote_fetcher(candidate_codes)
    except Exception:
        second_quotes = {}
    risk_titles: Dict[str, Optional[List[str]]] = {code: None for code in candidate_codes}
    if risk_title_fetcher:
        with ThreadPoolExecutor(max_workers=min(8, max(len(candidate_codes), 1))) as executor:
            futures = {executor.submit(risk_title_fetcher, code): code for code in candidate_codes}
            for future in as_completed(futures):
                code = futures[future]
                try:
                    risk_titles[code] = future.result()
                except Exception:
                    risk_titles[code] = None
    resolved_industries = dict(industry_by_code or {})
    missing_industries = [code for code in candidate_codes if not resolved_industries.get(code)]
    if industry_fetcher and missing_industries:
        with ThreadPoolExecutor(max_workers=min(8, len(missing_industries))) as executor:
            futures = {executor.submit(industry_fetcher, code): code for code in missing_industries}
            for future in as_completed(futures):
                code = futures[future]
                try:
                    resolved_industries[code] = future.result()
                except Exception:
                    resolved_industries[code] = ""

    verified_items = []
    unverified_items = list(insufficient)
    tolerance = _f(cfg.get("quote_price_tolerance_pct"), 0.3)
    gain_tolerance = _f(cfg.get("gain_tolerance_pct_points"), 0.8)
    trigger = _f(cfg.get("trigger_threshold_pct"), 100)
    theme_counts = theme_counts or {}
    for code, current, rows, current_price, anchor, gain in preliminary:
        quote = second_quotes.get(code) or {}
        second_price = _f(quote.get("price"))
        try:
            verification_rows = _completed_rows(verification_history_fetcher(code, 18), cutoff)
        except Exception:
            verification_rows = []
        required = 11 if slot == "09_00" else 10
        if len(verification_rows) < required or (slot != "09_00" and second_price <= 0):
            unverified_items.append({"code": code, "name": current.get("name", ""), "reason": "第二行情源或复权锚点缺失"})
            continue
        second_anchor = _f(verification_rows[-11].get("close")) if slot == "09_00" else _f(verification_rows[-10].get("close"))
        second_current = _f(verification_rows[-1].get("close")) if slot == "09_00" else second_price
        second_gain = (second_current / second_anchor - 1) * 100 if second_anchor else -999
        price_gap = abs(second_current / current_price - 1) * 100 if current_price else 999
        if price_gap > tolerance or abs(second_gain - gain) > gain_tolerance:
            unverified_items.append({
                "code": code, "name": current.get("name", ""),
                "reason": f"双源差异超限：价格{price_gap:.2f}%/涨幅{abs(second_gain-gain):.2f}个百分点",
            })
            continue
        industry = resolved_industries.get(code, "")
        score = score_candidate(
            {**current, "price": current_price}, rows,
            gain, theme_counts.get(industry) if industry else None, risk_titles.get(code), cfg,
        )
        verified_items.append({
            "code": code, "name": str(current.get("name") or histories.get(code, {}).get("name") or ""),
            "market": market_name(code), "is_st": "ST" in str(current.get("name", "")).upper(),
            "alert_level": "翻倍异动" if gain >= trigger else "临界预警",
            "gain_10d_pct": round(gain, 3), "distance_to_100_pct_points": round(100 - gain, 3),
            "current_price": round(current_price, 3), "anchor_price": round(anchor, 3),
            "anchor_date": str((rows[-11] if slot == "09_00" else rows[-10]).get("date", "")),
            "data_timestamp": (
                f"{rows[-1].get('date', '')} 收盘" if slot == "09_00"
                else str(quote.get("timestamp") or report_date.isoformat())
            ),
            "industry": industry or "行业数据不足", "hard_risk_titles": risk_titles.get(code) or [],
            **score,
        })
    verified_items.sort(key=lambda row: (row["alert_level"] == "翻倍异动", row["gain_10d_pct"]), reverse=True)

    universe_count = len(universe)
    universe_coverage = universe_count / max(expected_count, 1)
    history_coverage = history_ok / max(universe_count, 1)
    errors = []
    if universe_coverage < _f(cfg.get("minimum_universe_coverage_ratio"), 0.98):
        errors.append(f"全市场覆盖率{universe_coverage:.2%}低于98%")
    if history_coverage < _f(cfg.get("minimum_history_coverage_ratio"), 0.98):
        errors.append(f"历史日线覆盖率{history_coverage:.2%}低于98%")
    if candidate_codes and len(verified_items) != len(candidate_codes):
        errors.append("达到预警阈值的标的未全部通过双源复核")
    actionable = not errors
    if not actionable:
        # Do not publish partial directional scores as a complete full-market result.
        verified_items = []
    if slot == "19_00":
        for code, current in universe.items():
            price = _f(current.get("price"))
            if price <= 0:
                continue
            existing = list((histories.get(code) or {}).get("rows", []))
            today = {
                "date": report_date.isoformat(), "open": _f(current.get("open"), price),
                "close": price, "high": _f(current.get("high"), price),
                "low": _f(current.get("low"), price), "volume": _f(current.get("volume")),
            }
            existing = [row for row in existing if str(row.get("date")) != report_date.isoformat()]
            existing.append(today)
            histories[code] = {
                "name": current.get("name", histories.get(code, {}).get("name", "")),
                "market": market_name(code), "rows": existing[-cache_days:],
            }
    cache.update({
        "version": 1, "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "expected_count": expected_count, "histories": histories,
    })
    if persist_cache:
        save_history_cache(cache, cache_path)
    return {
        "abnormal_10d_monitor": {
            "label": "10日翻倍异动监控", "scope": "全部在市A股普通股票",
            "as_of": report_date.isoformat(),
            "trigger_count": sum(1 for row in verified_items if row["alert_level"] == "翻倍异动"),
            "warning_count": sum(1 for row in verified_items if row["alert_level"] == "临界预警"),
            "items": verified_items, "unverified_items": unverified_items,
            "methodology": "10个交易日复权累计涨幅；未来5日双评分为规则化研究评估，不是确定性预测",
        },
        "abnormal_10d_data_quality": {
            "status": "complete" if actionable else "error", "actionable": actionable,
            "errors": errors, "warnings": ([f"{len(fetched_errors)}只历史日线抓取失败"] if fetched_errors else []),
            "universe_count": universe_count, "expected_count": expected_count,
            "universe_coverage_ratio": round(universe_coverage, 6),
            "history_coverage_ratio": round(history_coverage, 6),
            "dual_source_verified_count": len(verified_items),
            "missing_codes": sorted(fetched_errors), "synthetic_data_used": False,
        },
    }


def render_abnormal_10d_section(report: Dict[str, Any]) -> str:
    monitor = report["abnormal_10d_monitor"]
    quality = report["abnormal_10d_data_quality"]
    if not quality.get("actionable"):
        errors = "；".join(quality.get("errors") or ["未知错误"])
        return (
            "<section><h2>10日翻倍异动监控</h2><div class='card'>"
            f"<b>数据不足</b><small>{html.escape(errors)}。本模块不输出走势评分，其他策略模块按各自数据质量运行。</small>"
            "</div></section>"
        )
    items = monitor.get("items") or []
    if not items:
        body = "<div class='card'>当前无10日累计涨幅达到80%且通过双源校验的普通A股。</div>"
    else:
        rows = []
        for item in items:
            positives = "；".join(item.get("positive_factors") or [])
            risks = "；".join(item.get("risk_factors") or [])
            rows.append(
                "<tr>"
                f"<td><b>{html.escape(item['name'])}</b><small>{html.escape(item['code'])}·{html.escape(item['market'])}</small></td>"
                f"<td>{html.escape(item['alert_level'])}<small>10日 {item['gain_10d_pct']:+.2f}%｜距100% {item['distance_to_100_pct_points']:+.2f}pct</small></td>"
                f"<td>{item['current_price']:.3f}<small>{html.escape(item['anchor_date'])} 基准 {item['anchor_price']:.3f}</small></td>"
                f"<td>{item['continuation_score'] if item['continuation_score'] is not None else '—'}·{html.escape(item['continuation_band'])}</td>"
                f"<td>{item['pullback_risk_score'] if item['pullback_risk_score'] is not None else '—'}·{html.escape(item['pullback_risk_band'])}</td>"
                f"<td><b>{html.escape(item['outlook'])}</b><small>加分：{html.escape(positives)}<br>风险：{html.escape(risks)}</small></td>"
                f"<td>{html.escape(item['data_timestamp'])}</td>"
                "</tr>"
            )
        body = (
            "<div class='table-scroll' style='overflow:auto'><table><thead><tr><th>标的</th><th>级别/涨幅</th><th>当前/基准</th>"
            "<th>延续分</th><th>回撤风险分</th><th>未来5日规则评估</th><th>数据截止</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>"
        )
    return (
        "<section><h2>10日翻倍异动监控</h2>"
        f"<p class='emotion-note'>翻倍异动{monitor['trigger_count']}只，临界预警{monitor['warning_count']}只。"
        "评分用于未来5个交易日的风险研究，不构成确定性预测或交易指令。</p>"
        f"{body}</section>"
    )


def render_abnormal_10d_strip(report: Dict[str, Any]) -> str:
    monitor = report["abnormal_10d_monitor"]
    quality = report["abnormal_10d_data_quality"]
    if not quality.get("actionable"):
        return "<section class='near-strip near-error'><b>10日翻倍异动监控·数据不足</b><span>完整列表暂停，详见当前时点报告的数据质量。</span></section>"
    items = monitor.get("items") or []
    riskiest = max(items, key=lambda row: row.get("pullback_risk_score") or -1, default=None)
    detail = (
        f"最高回撤风险：{riskiest['name']}·{riskiest['pullback_risk_score']:.2f}分·{riskiest['outlook']}"
        if riskiest else "当前暂无达到80%预警阈值的合格标的"
    )
    return (
        "<section class='near-strip'><b>10日翻倍异动监控 · "
        f"触发{monitor['trigger_count']}只 / 预警{monitor['warning_count']}只</b><span>{html.escape(detail)}</span></section>"
    )
