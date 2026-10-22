"""Deterministic, public-data estimates of main-board price deviation risk.

This module does not decide whether an exchange has recognised an abnormal move.
It deliberately keeps unreset rolling research estimates separate from verified
reset dates. ``normal_reset`` / ``severe_reset`` mean the first session INCLUDED
after the relevant exchange publication, not the publication's date.

The caller must provide ordinary Shanghai/Shenzhen main-board A shares only,
their correct exchange A-share index, and an independently verified calendar.
The index date sequence supplies the expected sessions here; missing stock bars
are never filled or silently skipped. Prices are unadjusted. Suspected corporate
actions remain an explicit limitation, not an invented adjustment factor.
"""

from __future__ import annotations

from datetime import date
import math
from typing import Any, Dict, Iterable, Mapping, Optional


RULE_VERSION = "mainboard-deviation-2026-07-06-v1"
EFFECTIVE_FROM = "2026-07-06"
THRESHOLDS = {3: 20.0, 10: 100.0, 30: 200.0}
SAME_DIRECTION_ABNORMAL_EVENTS_10D = 4
SOURCES = [
    {
        "label": "上交所交易规则（2026年修订），5.4.1—5.4.6",
        "url": "https://www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/c/c_20260424_10816492.shtml",
    },
    {
        "label": "深交所交易规则（2026年修订），5.4.1、5.4.3—5.4.6",
        "url": "https://docs.static.szse.cn/www/lawrules/rule/trade/current/W020260424690713155663.pdf",
    },
    {
        "label": "上证A股指数000002官方编制方案",
        "url": "https://www.sse.com.cn/market/sseindex/indexlist/indexdetails/indexmethods/c1/000002_000002hbook_CN.pdf",
    },
    {
        "label": "深证A指399107官方编制方案",
        "url": "https://www.szse.cn/marketServices/message/index/project/P020250711381739218475.pdf",
    },
]
BENCHMARKS = {
    "sh": {"code": "000002", "symbol": "sh000002", "name": "上证A股指数"},
    "sz": {"code": "399107", "symbol": "sz399107", "name": "深证A指"},
}
FORMULA = "100 * (stock_close / stock_base_close - index_close / index_base_close)"
WINDOW_POLICY = "研究保守最大窗：截止日向前1至N个连续交易日，另列恰好N日；不冒称交易所正式认定"
SCENARIO_ASSUMPTION = "仅情景：下一交易日基准指数涨跌0%、无除权停牌、已核验重置不变；不是行情预测或允许涨幅"


def _date(value: Any) -> str:
    if isinstance(value, date):
        return value.isoformat()[:10]
    text = str(value or "")[:10]
    date.fromisoformat(text)
    return text


def _number(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _rows(bars: Iterable[Mapping[str, Any]], cutoff: str):
    result: Dict[str, Dict[str, Any]] = {}
    issues = []
    future = 0
    for raw in bars:
        try:
            day = _date(raw.get("date", raw.get("day")))
        except (ValueError, TypeError, AttributeError):
            issues.append("invalid_bar_date")
            continue
        if day > cutoff:
            future += 1
            continue
        close = _number(raw.get("close"))
        if close is None:
            issues.append("invalid_close:" + day)
            continue
        if day in result:
            # Identical duplicates also invalidate provenance/uniqueness.
            issues.append("duplicate_date:" + day)
            continue
        row = dict(raw)
        row["date"] = day
        row["close"] = close
        result[day] = row
    return result, issues, future


def _reset(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, Mapping):
        value = value.get("effective_from", value.get("date", value.get("start")))
    return _date(value)


def _suspended(row: Mapping[str, Any]) -> bool:
    return (
        row.get("suspended") is True
        or row.get("is_suspended") is True
        or str(row.get("trading_status", "")).lower() in {"suspended", "停牌"}
    )


def _unlimited(row: Mapping[str, Any]) -> bool:
    return row.get("no_price_limit") is True or row.get("unlimited") is True


def _corporate_action_issues(rows: Mapping[str, Mapping[str, Any]], days):
    issues = []
    prev_coverage = 0
    for before, current in zip(days, days[1:]):
        prior = rows[before]
        row = rows[current]
        supplied = row.get("prev_close", row.get("pre_close", row.get("preclose")))
        previous = _number(supplied)
        if previous is not None:
            prev_coverage += 1
            # A native reference differing by more than half one cent is not the
            # preceding raw close. It can be an ex-right reference or bad data.
            if abs(previous - prior["close"]) > 0.00500001:
                issues.append("reference_close_changed:" + current)
        factor_before = _number(prior.get("adjust_factor", prior.get("adj_factor")))
        factor_current = _number(row.get("adjust_factor", row.get("adj_factor")))
        if factor_before is not None and factor_current is not None:
            if not math.isclose(factor_before, factor_current, rel_tol=1e-8, abs_tol=1e-10):
                issues.append("adjustment_factor_changed:" + current)
        else:
            adjusted_before = _number(prior.get("qfq_close", prior.get("adj_close")))
            adjusted_current = _number(row.get("qfq_close", row.get("adj_close")))
            if adjusted_before is not None and adjusted_current is not None:
                # Coarse vendor adjusted closes can themselves cause tiny ratio
                # changes. Compare feasible factors allowing half a cent of
                # rounding on EACH adjusted close, rather than treating price
                # precision as a corporate action. Disjoint intervals still
                # mean the unadjusted calculation is unsafe.
                low_before = (adjusted_before - 0.00500001) / prior["close"]
                high_before = (adjusted_before + 0.00500001) / prior["close"]
                low_current = (adjusted_current - 0.00500001) / row["close"]
                high_current = (adjusted_current + 0.00500001) / row["close"]
                if max(low_before, low_current) > min(high_before, high_current) + 1e-10:
                    issues.append("adjusted_ratio_changed:" + current)
    if prev_coverage != max(len(days) - 1, 0):
        issues.append("corporate_actions_not_fully_verified")
    return sorted(set(issues))


def _candidate(stock, benchmark, days, length: int):
    start = days[-length]
    base_day = days[-length - 1]
    end = days[-1]
    stock_ratio = stock[end]["close"] / stock[base_day]["close"]
    index_ratio = benchmark[end]["close"] / benchmark[base_day]["close"]
    return {
        "deviation_pp": 100.0 * (stock_ratio - index_ratio),
        "start": start,
        "base_date": base_day,
        "end": end,
        "length": length,
        "stock_return_pct": 100.0 * (stock_ratio - 1.0),
        "index_return_pct": 100.0 * (index_ratio - 1.0),
    }


def calculate(stock_bars, index_bars, cutoff: str, *, normal_reset=None,
              severe_reset=None, reset_verified=False,
              normal_reset_verified=None, severe_reset_verified=None) -> Dict[str, Any]:
    """Return signed 3/10/30-session research deviation and threshold distances.

    ``reset_verified=True`` is an attestation by the caller that relevant
    exchange publications were comprehensively checked, including absence of a
    reset when its corresponding argument is None. ``normal_reset_verified``
    and ``severe_reset_verified`` can attest the two independent histories
    separately; when None each falls back to ``reset_verified``. Unverified
    reset arguments are retained but NOT applied to rolling calculations.

    There must be 31 complete matching observations ending at ``cutoff``. With
    shorter history an otherwise complete short window can still be shown, but
    the report remains partial and cannot claim full 30-session coverage.
    Missing sessions inside a window invalidate that window instead of joining
    observations across the gap. Values always remain public-data estimates,
    never exchange-recognised remaining "permitted gains".
    """
    cutoff_day = _date(cutoff)
    normal_verified = (reset_verified if normal_reset_verified is None else normal_reset_verified) is True
    severe_verified = (reset_verified if severe_reset_verified is None else severe_reset_verified) is True
    report: Dict[str, Any] = {
        "rule_version": RULE_VERSION,
        "effective_from": EFFECTIVE_FROM,
        "sources": SOURCES,
        "formula": FORMULA,
        "window_policy": WINDOW_POLICY,
        "cutoff": cutoff_day,
        "reference_only": True,
        "reset_verified": normal_verified and severe_verified,
        "normal_reset_verified": normal_verified,
        "severe_reset_verified": severe_verified,
        "normal_reset": None,
        "severe_reset": None,
        "required_bars": 31,
        "issues": [],
        "metrics": {},
    }
    try:
        report["normal_reset"] = _reset(normal_reset)
        report["severe_reset"] = _reset(severe_reset)
    except (ValueError, TypeError):
        report["issues"].append("invalid_reset_date")
    if cutoff_day < EFFECTIVE_FROM:
        report["issues"].append("rule_not_effective_at_cutoff")
    stock, stock_issues, stock_future = _rows(stock_bars, cutoff_day)
    benchmark, index_issues, index_future = _rows(index_bars, cutoff_day)
    report["excluded_future_bars"] = {"stock": stock_future, "index": index_future}
    report["issues"].extend("stock:" + issue for issue in stock_issues)
    report["issues"].extend("index:" + issue for issue in index_issues)
    days = sorted(benchmark)
    report["stock_bar_count"] = len(stock)
    report["index_bar_count"] = len(benchmark)
    if not days or days[-1] != cutoff_day or cutoff_day not in stock:
        report["issues"].append("source_not_at_cutoff")
    fatal = bool(stock_issues or index_issues or any(
        issue in report["issues"] for issue in
        ("invalid_reset_date", "rule_not_effective_at_cutoff", "source_not_at_cutoff")
    ))
    expected = days[-31:]
    missing = [day for day in expected if day not in stock]
    report["missing_stock_dates"] = missing
    report["complete_history"] = len(expected) == 31 and not missing and not fatal
    if not report["complete_history"]:
        report["issues"].append("incomplete_31_observations")
    if not (normal_verified and severe_verified):
        report["issues"].append("exchange_reset_history_not_verified")
    if not normal_verified:
        report["issues"].append("normal_exchange_reset_history_not_verified")
    if not severe_verified:
        report["issues"].append("severe_exchange_reset_history_not_verified")
    matched = [day for day in expected if day in stock]
    if len(matched) == len(expected):
        report["issues"].extend(_corporate_action_issues(stock, expected))

    for window, threshold in THRESHOLDS.items():
        issue_list = []
        window_reset_verified = normal_verified if window == 3 else severe_verified
        result: Dict[str, Any] = {
            "window": window,
            "threshold": threshold,
            "direction": "up",
            "reference_only": True,
            "reset_verified": window_reset_verified,
            "deviation_pp": None,
            "max_positive_deviation": None,
            "max_start": None,
            "max_end": None,
            "length": None,
            "headroom_pp": None,
            "exactN": None,
            "next_session_scenario": None,
            "issues": issue_list,
        }
        report["metrics"][str(window)] = result
        if fatal:
            result["status"] = "unavailable"
            issue_list.append("invalid_or_stale_source")
            continue
        # A complete N-session window requires N+1 closes. Refuse to produce a
        # misleading maximum if any of those sessions is absent or suspended.
        relevant = days[-window - 1:]
        if len(relevant) != window + 1:
            result["status"] = "unavailable"
            issue_list.append("insufficient_history")
            continue
        if any(day not in stock for day in relevant):
            result["status"] = "unavailable"
            issue_list.append("missing_session_inside_window")
            continue
        if relevant[1] < EFFECTIVE_FROM:
            result["status"] = "unavailable"
            issue_list.append("window_crosses_rule_effective_date")
            continue
        if any(_suspended(stock[day]) for day in relevant[1:]):
            result["status"] = "unavailable"
            issue_list.append("suspension_session_rule_not_verified")
            continue
        if any(_unlimited(stock[day]) for day in relevant[1:]):
            result["status"] = "unavailable"
            issue_list.append("unlimited_price_session_excluded")
            continue
        action_issues = _corporate_action_issues(stock, relevant)
        issue_list.extend(action_issues)
        if any("changed:" in issue for issue in action_issues):
            # A reference close/factor change is evidence that raw close ratios
            # cannot represent the required adjusted interval. Do not publish
            # a precise but wrong headroom or next-session price in that case.
            result["status"] = "unavailable"
            result["corporate_action_uncertain"] = True
            issue_list.append("corporate_action_adjustment_not_verified")
            continue
        reset = report["normal_reset" if window == 3 else "severe_reset"]
        applied_reset = reset if window_reset_verified else None
        result["reset_applied"] = applied_reset
        rolling_candidates = [
            _candidate(stock, benchmark, relevant, length)
            for length in range(1, window + 1)
        ]
        rolling_maximum = max(rolling_candidates, key=lambda item: (item["deviation_pp"], -item["length"]))
        result["rolling_reference"] = {
            "maximum": rolling_maximum,
            "exactN": rolling_candidates[-1],
            "headroom_pp": threshold - rolling_maximum["deviation_pp"],
            "reset_applied": None,
            "reference_only": True,
        }
        candidates = [item for item in rolling_candidates if applied_reset is None or item["start"] >= applied_reset]
        result["exactN"] = next((item for item in candidates if item["length"] == window), None)
        if not candidates:
            result["status"] = "reset_pending_next_session"
            issue_list.append("reset_effective_after_cutoff")
            continue
        # Stable tie: prefer the shortest interval, keeping the declared actual
        # start/base dates reproducible rather than using an unspecified choice.
        maximum = max(candidates, key=lambda item: (item["deviation_pp"], -item["length"]))
        headroom = threshold - maximum["deviation_pp"]
        if abs(headroom) < 1e-10:
            headroom = 0.0
        result.update({
            "status": "reference",
            "deviation_pp": maximum["deviation_pp"],
            "max_positive_deviation": maximum["deviation_pp"],
            "max_start": maximum["start"],
            "max_base_date": maximum["base_date"],
            "max_end": maximum["end"],
            "length": maximum["length"],
            "headroom_pp": headroom,
            "threshold_reached_estimate": headroom <= 0.0,
            "stock_return_pct": maximum["stock_return_pct"],
            "index_return_pct": maximum["index_return_pct"],
        })
        result["corporate_action_uncertain"] = any("changed:" in issue for issue in issue_list)
        result["headroom_kind"] = "unadjusted_price_reference" if result["corporate_action_uncertain"] else (
            "verified_reset_public_data_estimate" if window_reset_verified else "unreset_rolling_reference"
        )
        if not window_reset_verified:
            issue_list.append("exchange_reset_history_not_verified")
        # The next session rolls the look-back: for length N its base is the
        # current (N-1)-session base, NOT the current N-session base. Solve each
        # possible interval separately, then retain the earliest threshold.
        scenarios = []
        current = relevant[-1]
        for length in range(1, window + 1):
            base = relevant[-length]
            start = "next_session" if length == 1 else relevant[-length + 1]
            if applied_reset is not None:
                if start != "next_session" and start < applied_reset:
                    continue
                # A future reset date is not invented as a trading date here.
                # The caller must resolve the actual next exchange session.
                if applied_reset > cutoff_day:
                    continue
            index_ratio = benchmark[current]["close"] / benchmark[base]["close"]
            price = stock[base]["close"] * (threshold / 100.0 + index_ratio)
            day_pct = 100.0 * (price / stock[current]["close"] - 1.0)
            scenarios.append({
                "index_day_pct": 0.0,
                "day_pct": day_pct,
                "threshold_price_unrounded": price,
                "start": start,
                "base_date": base,
                "end": "next_session",
                "length": length,
                "assumption": SCENARIO_ASSUMPTION,
                "is_forecast": False,
                "is_price_limit": False,
            })
        if scenarios:
            result["next_session_scenario"] = min(scenarios, key=lambda item: (item["day_pct"], item["length"]))
        else:
            issue_list.append("next_session_reset_date_needs_calendar")
        result["issues"] = sorted(set(issue_list))
    available = sum(item["deviation_pp"] is not None for item in report["metrics"].values())
    report["status"] = "complete" if available == 3 and report["complete_history"] else "partial" if available else "unavailable"
    report["issues"] = sorted(set(report["issues"]))
    report["calculation_kind"] = "reconciled_public_data_estimate" if (
        report["complete_history"] and available == 3 and normal_verified and severe_verified
        and not any("changed:" in issue or "not_fully_verified" in issue for issue in report["issues"])
    ) else "rolling_research_estimate"
    return report
