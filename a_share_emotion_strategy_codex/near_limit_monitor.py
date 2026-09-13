#!/usr/bin/env python3
"""Deterministic main-board near-limit scanner and audit renderer.

"Near limit" is a research label, not a prediction.  The module consumes
public market snapshots assembled by ``run_report.py`` and never places orders.
"""

from __future__ import annotations

import datetime as dt
import html
import json
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from emotion_monitor import _hard_risk_titles, load_emotion_config, quote_metrics, validate_catalyst
from hot_sector_leaders import VERSION, filter_ranked_rows, gate_reason, render_focus_note


ROOT = Path(__file__).resolve().parent
from research_store import private_path
STATE_PATH = private_path("data/emotion/near_limit_state.json")


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def limit_up_price(previous_close: float) -> float:
    """Return the ordinary main-board +10% limit rounded to the one-cent tick."""
    if previous_close <= 0:
        return 0.0
    value = Decimal(str(previous_close)) * Decimal("1.10")
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def is_main_board_common(code: str, name: str, prefixes: Iterable[str]) -> bool:
    upper_name = name.strip().upper()
    if not any(code.startswith(prefix) for prefix in prefixes):
        return False
    if "ST" in upper_name or "退" in name or upper_name.startswith(("N", "C")):
        return False
    return len(code) == 6 and code.isdigit()


def prefilter_near_limit(
    market: Dict[str, Dict[str, Any]],
    previous_pool_map: Dict[str, Dict[str, Any]],
    config: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Scan the entire market snapshot and return unsealed candidates + sealed outcomes."""
    config = config or load_emotion_config()
    near_cfg = config["near_limit"]
    filters = config["candidate_filters"]
    candidates: List[Dict[str, Any]] = []
    sealed: List[Dict[str, Any]] = []
    for code, row in market.items():
        name = str(row.get("name", ""))
        if not is_main_board_common(code, name, near_cfg["main_board_prefixes"]):
            continue
        price = _f(row.get("price"))
        previous = _f(row.get("prev_close"))
        opened = _f(row.get("open"))
        high = _f(row.get("high"))
        low = _f(row.get("low"))
        turnover = _f(row.get("turnover"))
        amount = _f(row.get("amount_cny"))
        limit_price = limit_up_price(previous)
        if min(price, previous, opened, high, low, limit_price) <= 0:
            continue
        prior_boards = _i(previous_pool_map.get(code, {}).get("lbc"))
        prospective_board = prior_boards + 1
        if prospective_board not in {1, 2}:
            continue
        if not (_f(filters["min_turnover_pct"]) <= turnover <= _f(filters["max_turnover_pct"])):
            continue
        if amount < _f(filters["min_amount_cny"]):
            continue
        one_word = abs(opened - limit_price) < 0.005 and abs(high - low) < 0.005
        if one_word:
            continue
        base = {
            "c": code,
            "n": name,
            "price": price,
            "prev_close": previous,
            "open": opened,
            "high": high,
            "low": low,
            "pct": _f(row.get("pct")),
            "hs": turnover,
            "amount": amount,
            "limit_price": limit_price,
            "prior_boards": prior_boards,
            "prospective_board": prospective_board,
            "market_tick_time": str(row.get("ticktime", "")),
        }
        if price >= limit_price - 0.005:
            sealed.append({**base, "signal_type": "已封板"})
            continue
        distance = (limit_price / price - 1.0) * 100
        if not (0 < distance <= _f(near_cfg["max_distance_to_limit_pct"])):
            continue
        touched = high >= limit_price - 0.005
        candidates.append({
            **base,
            "distance_to_limit_pct": distance,
            "signal_type": "炸板待回封" if touched else "首次临板",
        })
    candidates.sort(key=lambda row: (row["distance_to_limit_pct"], -row["amount"], row["c"]))
    sealed.sort(key=lambda row: (-row["prospective_board"], -row["amount"], row["c"]))
    return candidates, sealed


def load_near_limit_state(path: Path = STATE_PATH) -> Dict[str, Any]:
    if not path.exists():
        return {"version": 1, "report_date": "", "last_slot": "", "latest_items": [], "history": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.setdefault("version", 1)
    payload.setdefault("report_date", "")
    payload.setdefault("last_slot", "")
    payload.setdefault("latest_items", [])
    payload.setdefault("history", [])
    return payload


def save_near_limit_state(state: Dict[str, Any], path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _score_candidate(
    candidate: Dict[str, Any], metrics: Dict[str, Any], evidence: List[Dict[str, Any]],
    theme_limit_count: int, theme_up3_count: int, theme_breadth_ratio: float,
    report_date: dt.date, config: Dict[str, Any],
) -> Tuple[float, Dict[str, float]]:
    weights = config["near_limit"]["score_weights"]
    max_distance = _f(config["near_limit"]["max_distance_to_limit_pct"], 3.0)
    proximity = _clamp((max_distance - _f(candidate["distance_to_limit_pct"])) / max_distance)
    vwap_strength = _clamp((2.0 - max(_f(metrics["below_vwap_pct"]), 0.0)) / 2.0)
    sell_strength = _clamp((0.75 - _f(metrics["active_sell_ratio"])) / 0.40)
    retreat_strength = _clamp((8.0 - _f(metrics["retreat_from_high_pct"])) / 8.0)
    flow = 0.4 * vwap_strength + 0.4 * sell_strength + 0.2 * retreat_strength
    limit_link = _clamp(theme_limit_count / 3.0)
    breadth_link = _clamp(theme_up3_count / 5.0) * _clamp(theme_breadth_ratio / 0.8)
    theme = max(limit_link, breadth_link)
    official = any(bool(row.get("official")) for row in evidence)
    newest = max((str(row.get("published_at", ""))[:10] for row in evidence), default="")
    try:
        age = max(0, (report_date - dt.date.fromisoformat(newest)).days)
    except ValueError:
        age = 7
    catalyst = 0.7 * (1.0 if official else 0.85) + 0.3 * _clamp((7.0 - age) / 7.0)
    if candidate["signal_type"] == "首次临板":
        board_quality = 1.0
    else:
        board_quality = _clamp((8.0 - 2.0 * _i(metrics.get("breaks"))) / 10.0)
    if _i(candidate["prospective_board"]) == 2:
        board_quality = max(0.0, board_quality - 0.1)
    components = {
        "proximity": proximity * _f(weights["proximity"]),
        "flow": flow * _f(weights["flow"]),
        "theme": theme * _f(weights["theme"]),
        "catalyst": catalyst * _f(weights["catalyst"]),
        "board_quality": board_quality * _f(weights["board_quality"]),
    }
    return round(sum(components.values()), 2), {key: round(value, 2) for key, value in components.items()}


def _recommendation(risk_level: str, metrics: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, str]:
    if bool(metrics.get("at_limit_up")):
        return {"action": "禁止介入", "text": "复核时已在涨停价，不追板；仅记录本时点封板成果"}
    if _f(metrics["open_gap_pct"]) > _f(config["alerts"]["high_open_hard_pct"]):
        return {"action": "禁止介入", "text": "高开超过7%，禁止追高；已有仓按承接与原计划处理"}
    fund_weak = (
        _f(metrics["below_vwap_pct"]) >= _f(config["alerts"]["below_vwap_pct"])
        and _f(metrics["active_sell_ratio"]) >= _f(config["alerts"]["active_sell_ratio"])
    )
    if fund_weak:
        return {"action": "禁止介入", "text": "资金代理转弱；已持有按承接处理，未持有不介入"}
    if risk_level in {"高", "中高"}:
        return {"action": "等待承接", "text": "市场高风险，仅观察临板强度，不追涨"}
    if _f(metrics["open_gap_pct"]) <= 5.0 and _f(metrics["below_vwap_pct"]) <= 0:
        return {
            "action": "允许小仓试错",
            "text": f"仅允许小仓试错：单票≤{config['candidate_max_capital_cny'] / 10000:.1f}万元；涨停价不追",
        }
    return {"action": "等待承接", "text": "等待分歧、承接、再转强；临板不等于买点"}


def prefilter_emotion_leaders(
    pool: List[Dict[str, Any]], config: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Return current limit-pool names with repeated limit-up evidence."""
    config = config or load_emotion_config()
    leader_cfg = config["leaders"]
    prefixes = config["near_limit"]["main_board_prefixes"]
    rows: List[Dict[str, Any]] = []
    for item in pool:
        code = str(item.get("c", ""))
        name = str(item.get("n", ""))
        streak = _i(item.get("lbc"))
        recent = item.get("zttj") or {}
        recent_count = _i(recent.get("ct"))
        if not is_main_board_common(code, name, prefixes):
            continue
        after_launch = config.get("hot_sector_focus", {}).get("research_after_launch", False)
        if after_launch and streak < 1:
            continue
        if not after_launch and streak < _i(leader_cfg["min_current_boards"], 2) and recent_count < _i(
            leader_cfg["min_recent_limit_count"], 2
        ):
            continue
        rows.append(dict(item))
    return rows


def _leader_score(
    candidate: Dict[str, Any], quote: Dict[str, Any], theme_limit_count: int, config: Dict[str, Any],
) -> Tuple[float, Dict[str, float]]:
    weights = config["leaders"]["weights"]
    recent = candidate.get("zttj") or {}
    boards = _i(candidate.get("lbc"))
    recent_count = _i(recent.get("ct"))
    recent_days = max(_i(recent.get("days")), 1)
    breaks = _i(candidate.get("zbc"))
    amount = _f(quote.get("amount_cny"), _f(candidate.get("amount")))
    turnover = _f(quote.get("turnover"), _f(candidate.get("hs")))
    seal_ratio = _f(candidate.get("fund")) / max(amount, 1.0)
    if 3.0 <= turnover <= 18.0:
        turnover_quality = 1.0
    elif turnover < 3.0:
        turnover_quality = _clamp(turnover / 3.0)
    else:
        turnover_quality = _clamp(1.0 - (turnover - 18.0) / 30.0)
    components = {
        "current_boards": _clamp(boards / 5.0) * _f(weights["current_boards"]),
        "recent_frequency": (
            0.6 * _clamp(recent_count / 5.0) + 0.4 * _clamp(recent_count / recent_days)
        ) * _f(weights["recent_frequency"]),
        "seal_quality": (
            0.5 * _clamp(seal_ratio / 0.30) + 0.5 * (1.0 - _clamp(breaks / 5.0))
        ) * _f(weights["seal_quality"]),
        "liquidity": (
            (8.0 / 15.0) * _clamp(amount / 1_000_000_000.0) + (7.0 / 15.0) * turnover_quality
        ) * _f(weights["liquidity"]),
        "theme_linkage": _clamp(theme_limit_count / 4.0) * _f(weights["theme_linkage"]),
    }
    return round(sum(components.values()), 2), {key: round(value, 2) for key, value in components.items()}


def build_emotion_leader_report(
    candidates: List[Dict[str, Any]],
    quotes: Dict[str, Dict[str, Any]],
    sina_quotes: Dict[str, Dict[str, Any]],
    theme_counts: Dict[str, int],
    announcement_risks: Dict[str, List[str]],
    metadata_errors: Optional[List[str]] = None,
    hot_sector_context: Optional[Dict[str, Any]] = None,
    expected_as_of: Optional[str] = None,
) -> Dict[str, Any]:
    """Rank research-only emotion leaders after quote, metadata and risk validation."""
    config = load_emotion_config()
    quote_days = {str(row.get("timestamp", ""))[:8] for row in quotes.values() if row.get("timestamp")}
    expected_day = expected_as_of.replace("-", "") if expected_as_of else next(iter(quote_days), "")
    if hot_sector_context and (
        not expected_day or (not expected_as_of and len(quote_days) > 1)
        or hot_sector_context.get("as_of", "").replace("-", "") != expected_day
    ):
        hot_sector_context = None
    leader_cfg = config["leaders"]
    eligible: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    for candidate in candidates:
        code = str(candidate.get("c", ""))
        name = str(candidate.get("n", ""))
        theme = str(candidate.get("hybk", ""))
        reasons: List[str] = []
        focus_reason = gate_reason(code, hot_sector_context, config)
        if focus_reason:
            reasons.append(focus_reason)
        quote = quotes.get(code)
        sina = sina_quotes.get(code)
        if not candidate.get("listing_age_verified"):
            reasons.append("无法验证上市已满5个交易日")
        if not theme:
            reasons.append("题材/行业归属缺失")
        if not quote or not sina:
            reasons.append("双源行情缺失")
        else:
            price_gap = abs(_f(quote.get("price")) / max(_f(sina.get("price")), 0.0001) - 1.0) * 100
            if price_gap > _f(leader_cfg["quote_price_tolerance_pct"], 0.3):
                reasons.append(f"双源价格偏差{price_gap:.2f}%")
            theoretical_limit = limit_up_price(_f(quote.get("prev_close")))
            if _f(quote.get("pct")) < 9.5 or _f(quote.get("price")) < theoretical_limit - 0.005:
                reasons.append("复核时已不在涨停价")
        risk_titles = announcement_risks.get(code, [])
        if any(str(title).startswith("公告抓取失败") for title in risk_titles):
            reasons.append("官方公告风险校验失败")
        hard_titles = _hard_risk_titles(risk_titles, config)
        if hard_titles:
            reasons.append("命中官方硬风险公告：" + "；".join(hard_titles[:2]))
        if reasons:
            rejected.append({"code": code, "name": name, "theme": theme, "reasons": list(dict.fromkeys(reasons))})
            continue
        theme_limit_count = _i(theme_counts.get(theme))
        score, components = _leader_score(candidate, quote, theme_limit_count, config)
        recent = candidate.get("zttj") or {}
        boards = _i(candidate.get("lbc"))
        recent_count = _i(recent.get("ct"))
        recent_days = max(_i(recent.get("days")), 1)
        breaks = _i(candidate.get("zbc"))
        amount = _f(quote.get("amount_cny"), _f(candidate.get("amount")))
        seal_amount = _f(candidate.get("fund"))
        seal_ratio = seal_amount / max(amount, 1.0)
        turnover = _f(quote.get("turnover"), _f(candidate.get("hs")))
        eligible.append({
            "rank": 0,
            "code": code,
            "name": name,
            "theme": theme,
            "status": "主线强度研究候选",
            "current_boards": boards,
            "recent_days": recent_days,
            "recent_limit_count": recent_count,
            "pct": round(_f(quote.get("pct")), 3),
            "turnover_pct": round(turnover, 3),
            "amount_cny": amount,
            "seal_amount_cny": seal_amount,
            "seal_amount_ratio": round(seal_ratio, 4),
            "breaks": breaks,
            "theme_limit_count": theme_limit_count,
            "score": score,
            "score_components": components,
            "reasons": [
                f"当前{boards}连板，近{recent_days}日涨停{recent_count}次",
                f"封单约占成交额{seal_ratio:.1%}，盘中开板{breaks}次",
                f"换手{turnover:.2f}%、成交额{amount / 100_000_000:.2f}亿元",
                f"同题材涨停{theme_limit_count}只",
            ],
            "recommendation": {"action": "研究观察", "text": "量价规则识别，不在涨停价追；等待分歧、承接与再次转强"},
            "research_only": True,
            "formal_eligible": False,
            "invalidation_conditions": ["开板后无法回封", "连板高度终止", "题材联动转弱", "出现官方硬风险"],
            "quote_timestamp": str(quote.get("timestamp", "")),
        })
    eligible.sort(key=lambda row: (
        -_f(row["score"]), -_i(row["current_boards"]), -_i(row["recent_limit_count"]),
        _i(row["breaks"]), -_f(row["seal_amount_ratio"]), -_f(row["amount_cny"]), row["code"],
    ))
    maximum = _i(leader_cfg["max_results"], 5)
    top = filter_ranked_rows(eligible, hot_sector_context, config)[:maximum]
    for rank, row in enumerate(top, 1):
        row["rank"] = rank
    errors = list(metadata_errors or [])
    if config.get("hot_sector_focus", {}).get("enabled") and (
        not hot_sector_context or hot_sector_context.get("status") != "complete"
    ):
        errors.append("热点前三排名缺失或未通过校验")
    status = "complete" if len(top) == maximum and not errors else "degraded"
    return {
        "emotion_leaders": {
            "label": "热点板块前三 · 情绪龙头候选",
            "hot_sector_focus": hot_sector_context,
            "items": top,
            "eligible_count": len(eligible),
            "displayed_count": len(top),
            "vacant_count": max(0, maximum - len(top)),
            "rejected": rejected,
            "methodology": "热点行业强势池内前三，先按综合强度排名再核验；已启动首板可观察，板数不是唯一门槛；旧连板分仅辅助解释，不覆盖原始排名；不构成交易资格",
        },
        "emotion_leader_data_quality": {
            "status": status,
            "actionable": False,
            "errors": errors,
            "warnings": [],
            "unverified_candidate_count": len(rejected),
            "synthetic_data_used": False,
        },
    }


def build_near_limit_report(
    report_date: dt.date,
    slot: str,
    risk_level: str,
    candidates: List[Dict[str, Any]],
    sealed_candidates: List[Dict[str, Any]],
    quotes: Dict[str, Dict[str, Any]],
    sina_quotes: Dict[str, Dict[str, Any]],
    current_pool_map: Dict[str, Dict[str, Any]],
    theme_counts: Dict[str, int],
    evidence_map: Dict[str, List[Dict[str, Any]]],
    evidence_errors: List[str],
    announcement_risks: Dict[str, List[str]],
    market_snapshot: Dict[str, Dict[str, Any]],
    universe_quality: Dict[str, Any],
    persist_state: bool = True,
    state_path: Path = STATE_PATH,
    hot_sector_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    config = load_emotion_config()
    minimum_coverage = _f(config["near_limit"]["minimum_universe_coverage_ratio"], 0.98)
    coverage = _f(universe_quality.get("coverage_ratio"))
    system_errors = list(universe_quality.get("errors") or [])
    if config.get("hot_sector_focus", {}).get("enabled") and (
        not hot_sector_context or hot_sector_context.get("status") != "complete"
        or hot_sector_context.get("as_of") != report_date.isoformat()
    ):
        system_errors.append("热点板块前三排名缺失、过期或未通过校验")
    if coverage < minimum_coverage:
        system_errors.append(f"全市场覆盖率{coverage:.1%}低于{minimum_coverage:.0%}")
    rejected: List[Dict[str, Any]] = []
    eligible: List[Dict[str, Any]] = []
    validated_sealed: List[Dict[str, Any]] = []
    for candidate in candidates:
        code = str(candidate["c"])
        name = str(candidate["n"])
        theme = str(candidate.get("hybk", ""))
        reasons: List[str] = []
        focus_reason = gate_reason(code, hot_sector_context, config)
        if focus_reason:
            reasons.append(focus_reason)
        if not candidate.get("listing_age_verified"):
            reasons.append("无法验证上市已满5个交易日")
        if not theme:
            reasons.append("题材/行业归属缺失")
        quote = quotes.get(code)
        sina = sina_quotes.get(code)
        if not quote or not sina:
            reasons.append("双源行情缺失")
            metrics: Dict[str, Any] = {}
        else:
            price_gap = abs(_f(quote.get("price")) / max(_f(sina.get("price")), 0.0001) - 1.0) * 100
            if price_gap > _f(config["near_limit"]["quote_price_tolerance_pct"]):
                reasons.append(f"双源价格偏差{price_gap:.2f}%")
            metrics = quote_metrics(quote, current_pool_map.get(code, {}), _i(theme_counts.get(theme)))
            metrics["at_limit_up"] = _f(quote.get("price")) >= _f(candidate["limit_price"]) - 0.005
        verified, evidence, catalyst_reasons = validate_catalyst(
            code, theme, evidence_map.get(code, []), report_date, config
        )
        if not verified:
            reasons.extend(catalyst_reasons)
        hard_titles = _hard_risk_titles(announcement_risks.get(code, []), config)
        if hard_titles:
            reasons.append("命中官方硬风险公告：" + "；".join(hard_titles[:2]))
        limit_count = _i(theme_counts.get(theme))
        up3_count = _i(candidate.get("theme_up3_count"))
        breadth_ratio = _f(candidate.get("theme_breadth_ratio"))
        theme_linked = limit_count >= _i(config["candidate_filters"]["min_theme_limit_count"]) or (
            up3_count >= 3 and breadth_ratio >= 0.60
        )
        if not theme_linked:
            reasons.append("板块联动不足")
        if metrics:
            fund_weak = (
                _f(metrics["below_vwap_pct"]) >= _f(config["alerts"]["below_vwap_pct"])
                and _f(metrics["active_sell_ratio"]) >= _f(config["alerts"]["active_sell_ratio"])
            )
            if fund_weak or _f(metrics["retreat_from_high_pct"]) >= _f(config["alerts"]["high_retreat_red_pct"]):
                reasons.append("资金出逃/冲高回落代理触发红色阈值")
            if _f(metrics["active_sell_ratio"]) > _f(config["candidate_filters"]["max_initial_active_sell_ratio"]):
                reasons.append("主动卖出比例过高")
        if reasons:
            rejected.append({"code": code, "name": name, "theme": theme, "reasons": list(dict.fromkeys(reasons))})
            continue
        if bool(metrics.get("at_limit_up")):
            validated_sealed.append({
                "code": code,
                "name": name,
                "status": "复核时已封板",
                "prospective_board": _i(candidate["prospective_board"]),
                "price": _f(quote["price"]),
                "limit_price": _f(candidate["limit_price"]),
                "observed_at_slot": slot,
                "is_resealed": candidate.get("signal_type") == "炸板待回封",
                "close_sealed": slot == "19_00",
            })
            continue
        score, components = _score_candidate(
            candidate, metrics, evidence, limit_count, up3_count, breadth_ratio, report_date, config
        )
        eligible.append({
            "rank": 0,
            "code": code,
            "name": name,
            "theme": theme,
            "signal_type": candidate["signal_type"],
            "prospective_board": _i(candidate["prospective_board"]),
            "price": _f(quote["price"]),
            "limit_price": _f(candidate["limit_price"]),
            "distance_to_limit_pct": round((_f(candidate["limit_price"]) / _f(quote["price"]) - 1) * 100, 3),
            "pct": _f(quote["pct"]),
            "turnover_pct": _f(quote["turnover"]),
            "amount_cny": _f(quote["amount_cny"]),
            "vwap": _f(metrics["vwap"]),
            "active_sell_ratio": _f(metrics["active_sell_ratio"]),
            "retreat_from_high_pct": _f(metrics["retreat_from_high_pct"]),
            "theme_limit_count": limit_count,
            "theme_up3_count": up3_count,
            "theme_breadth_ratio": breadth_ratio,
            "score": score,
            "score_components": components,
            "catalyst_evidence": evidence,
            "recommendation": _recommendation(risk_level, metrics, config),
            "invalidation_conditions": ["距离涨停扩大到3%以上", "跌破VWAP并出现主动卖出共振", "板块联动失效", "出现官方硬风险"],
            "quote_timestamp": str(quote.get("timestamp", "")),
        })
    eligible.sort(key=lambda row: (
        -_f(row["score"]), _f(row["distance_to_limit_pct"]), _f(row["active_sell_ratio"]),
        -_f(row["amount_cny"]), row["code"],
    ))
    top = filter_ranked_rows(eligible, hot_sector_context, config)[:_i(config["near_limit"]["max_results"], 5)] if not system_errors else []
    for rank, row in enumerate(top, 1):
        row["rank"] = rank

    state = load_near_limit_state(state_path)
    previous_items = state.get("latest_items", []) if state.get("report_date") == report_date.isoformat() else []
    top_codes = {row["code"] for row in top}
    outcomes: List[Dict[str, Any]] = []
    for previous in previous_items:
        code = str(previous.get("code", ""))
        current = market_snapshot.get(code, {})
        price = _f(current.get("price"))
        limit_price = _f(previous.get("limit_price"))
        high = _f(current.get("high"))
        touched = bool(high and limit_price and high >= limit_price - 0.005)
        if price and limit_price and price >= limit_price - 0.005:
            status = "触板并封板"
        elif touched and code in top_codes:
            status = "炸板待回封"
        elif touched:
            status = "触板后开板并出榜"
        elif code in top_codes:
            status = "继续在榜"
        else:
            status = "退出临板榜"
        outcomes.append({
            "code": code,
            "name": previous.get("name", ""),
            "status": status,
            "price": price,
            "observed_at_slot": slot,
            "is_resealed": previous.get("signal_type") == "炸板待回封" or touched,
            "close_sealed": slot == "19_00" and status == "触板并封板",
            "prospective_board": _i(previous.get("prospective_board")),
            "limit_price": limit_price,
        })
    previous_codes = {str(row.get("code")) for row in previous_items}
    for row in top:
        if row["code"] not in previous_codes:
            outcomes.append({
                "code": row["code"], "name": row["name"], "status": "新入榜", "price": row["price"],
                "observed_at_slot": slot, "is_resealed": row["signal_type"] == "炸板待回封",
                "close_sealed": False, "prospective_board": row["prospective_board"],
                "limit_price": row["limit_price"],
            })

    # The outcomes area records only names that were on a preceding dynamic
    # shortlist, plus names that sealed between the broad scan and final
    # evidence/quote validation.  Unrelated already-sealed stocks are not
    # presented as scanner hits.
    sealed_rows = [
        row for row in outcomes if row.get("status") == "触板并封板"
    ] + validated_sealed
    for row in validated_sealed:
        if row["code"] not in previous_codes:
            outcomes.append(dict(row))
    next_session = top.copy() if slot == "19_00" else []
    if persist_state and not system_errors:
        state.update({
            "report_date": report_date.isoformat(), "last_slot": slot, "latest_items": top,
            "history": (state.get("history", []) + [{"date": report_date.isoformat(), "slot": slot, "items": top, "outcomes": outcomes}])[-60:],
        })
        save_near_limit_state(state, state_path)

    unresolved = sum(1 for row in rejected if any("催化" in reason or "来源" in reason for reason in row["reasons"]))
    status = "error" if system_errors else ("degraded" if evidence_errors or unresolved or len(top) < 5 else "complete")
    return {
        "emotion_near_limit": {
            "label": "热点板块前三 · 临板研究候选",
            "hot_sector_focus": hot_sector_context,
            "slot": slot,
            "items": top,
            "eligible_count": len(eligible),
            "displayed_count": len(top),
            "vacant_count": max(0, 5 - len(top)),
            "rejected": rejected,
            "sealed": sealed_rows,
            "next_session_watchlist": next_session,
            "methodology": "沪深主板普通A股全市场扫描；距理论涨停≤3%；预计首板/二板；真实催化、板块联动和量价资金代理全部通过",
        },
        "emotion_near_limit_outcomes": outcomes,
        "emotion_near_limit_data_quality": {
            "status": status,
            "actionable": not system_errors,
            "errors": system_errors + list(evidence_errors),
            "warnings": list(universe_quality.get("warnings") or []),
            "universe_count": _i(universe_quality.get("universe_count")),
            "expected_count": _i(universe_quality.get("expected_count")),
            "coverage_ratio": coverage,
            "unverified_candidate_count": unresolved,
            "synthetic_data_used": False,
        },
    }


def build_preopen_near_limit_report(previous_snapshot: Optional[Dict[str, Any]], *, expected_as_of: Optional[str] = None) -> Dict[str, Any]:
    source = ((previous_snapshot or {}).get("emotion_near_limit") or {})
    context = source.get("hot_sector_focus")
    config = load_emotion_config()
    focus_missing = config.get("hot_sector_focus", {}).get("enabled") and (
        not context or context.get("status") != "complete" or not expected_as_of
        or context.get("as_of") != expected_as_of
        or context.get("policy") != config.get("hot_sector_focus")
        or context.get("version") != VERSION
    )
    items = [] if focus_missing else filter_ranked_rows(list(source.get("next_session_watchlist") or []), context, config)[:5]
    for rank, row in enumerate(items, 1):
        row["rank"] = rank
        row["signal_type"] = "盘前观察"
    missing = not bool(previous_snapshot) or bool(focus_missing)
    return {
        "emotion_near_limit": {
            "hot_sector_focus": context,
            "label": "盘前观察池（沿用上一交易日19:00审计结果）",
            "slot": "09_00", "items": items, "eligible_count": len(items), "displayed_count": len(items),
            "vacant_count": max(0, 5 - len(items)), "rejected": [], "sealed": [],
            "next_session_watchlist": items,
            "methodology": "盘前不冒充盘中临板信号；仅展示上一交易日19:00生成的次日观察池",
        },
        "emotion_near_limit_outcomes": [],
        "emotion_near_limit_data_quality": {
            "status": "error" if missing else ("complete" if len(items) == 5 else "degraded"),
            "actionable": False, "errors": ["缺少上一交易日19:00热点前三审计结果或版本已过期"] if missing else [],
            "warnings": [],
            "universe_count": 0, "expected_count": 0, "coverage_ratio": 0.0,
            "unverified_candidate_count": 0, "synthetic_data_used": False,
        },
    }


def build_degraded_near_limit_report(error: str, slot: str) -> Dict[str, Any]:
    return {
        "emotion_near_limit": {
            "label": "临板候选（概率筛选）", "slot": slot, "items": [], "eligible_count": 0,
            "displayed_count": 0, "vacant_count": 5, "rejected": [], "sealed": [],
            "next_session_watchlist": [], "methodology": "数据不足，不执行仓位变化",
        },
        "emotion_near_limit_outcomes": [],
        "emotion_near_limit_data_quality": {
            "status": "error", "actionable": False, "errors": [error], "universe_count": 0,
            "warnings": [],
            "expected_count": 0, "coverage_ratio": 0.0, "unverified_candidate_count": 0,
            "synthetic_data_used": False,
        },
    }


def render_near_limit_section(report: Dict[str, Any]) -> str:
    data = report["emotion_near_limit"]
    quality = report["emotion_near_limit_data_quality"]
    leaders = report.get("emotion_leaders") or {"items": [], "displayed_count": 0, "vacant_count": 5}
    leader_quality = report.get("emotion_leader_data_quality") or {"status": "error", "errors": ["龙头榜未生成"]}
    rows: List[str] = []
    for item in data["items"]:
        evidence_links = "；".join(
            f"<a href='{html.escape(str(row.get('url', '')))}' target='_blank' rel='noreferrer'>{html.escape(str(row.get('title', '证据')))}</a>"
            for row in item.get("catalyst_evidence", [])[:2]
        ) or "无"
        rows.append(
            "<tr>" + "".join([
                f"<td>{item['rank']}</td>",
                f"<td><b>{html.escape(item['name'])}</b><small>{item['code']} · 板内原始第{item.get('sector_member_rank', '—')}名</small></td>",
                f"<td><span class='near-kind'>{html.escape(item['signal_type'])}</span><small>预计{item['prospective_board']}板</small></td>",
                f"<td><b>{item['distance_to_limit_pct']:.2f}%</b><small>{item['price']:.2f} → {item['limit_price']:.2f}</small></td>",
                f"<td>{item.get('strength_score', item['score']):.2f}<small>主线强度 · 换手{item['turnover_pct']:.2f}% · 主动卖出{item['active_sell_ratio']:.1%}</small></td>",
                f"<td>{html.escape(item['theme'])}<small>同题材涨停{item['theme_limit_count']}只</small></td>",
                f"<td>{evidence_links}</td>",
                f"<td><b>{html.escape(item['recommendation']['action'])}</b><small>{html.escape(item['recommendation']['text'])}</small></td>",
            ]) + "</tr>"
        )
    for _ in range(data["vacant_count"]):
        rows.append("<tr class='near-vacant'><td>—</td><td colspan='7'>暂无合格候选；不降低催化、联动或资金门槛补位。</td></tr>")
    leader_rows: List[str] = []
    for item in leaders.get("items", []):
        reasons = "；".join(html.escape(str(reason)) for reason in item.get("reasons", []))
        invalidations = "；".join(html.escape(str(reason)) for reason in item.get("invalidation_conditions", []))
        components = item.get("score_components", {})
        component_text = " · ".join(
            f"{label}{_f(components.get(key)):.1f}"
            for key, label in (
                ("current_boards", "连板"), ("recent_frequency", "频率"), ("seal_quality", "封板"),
                ("liquidity", "流动性"), ("theme_linkage", "联动"),
            )
        )
        leader_rows.append(
            "<tr>" + "".join([
                f"<td>{item['rank']}</td>",
                f"<td><b>{html.escape(item['name'])}</b><small>{item['code']} · {html.escape(item['theme'])} · 板内原始第{item.get('sector_member_rank', '—')}名</small></td>",
                f"<td><span class='near-kind'>{html.escape(item['status'])}</span><small>{item['current_boards']}连板 · 近{item['recent_days']}日{item['recent_limit_count']}次涨停</small></td>",
                f"<td><b>{item.get('strength_score', item['score']):.2f}</b><small>主线强度；旧结构分 {item['score']:.2f} · {html.escape(component_text)}</small></td>",
                f"<td>{reasons}</td>",
                f"<td><b>{html.escape(item['recommendation']['action'])}</b><small>{html.escape(item['recommendation']['text'])}；失效：{invalidations}</small></td>",
            ]) + "</tr>"
        )
    for _ in range(_i(leaders.get("vacant_count"))):
        leader_rows.append("<tr class='near-vacant'><td>—</td><td colspan='5'>暂无通过双源行情、上市时长与官方风险校验的多次涨停龙头；不以普通首板补位。</td></tr>")
    leader_quality_text = "；".join(leader_quality.get("errors", [])) or "双源行情、上市时长与官方风险校验完成"
    leader_html = (
        "<div class='near-research'><div class='near-head'><div><h3>热点板块前三 · 情绪龙头候选</h3>"
        "<p>启动后综合强度排名；板位只作辅助，首板也可研究；不等于真实主力身份或买入信号。</p></div>"
        f"<span class='pill'>数据 {html.escape(str(leader_quality.get('status', 'error')))} · 入选 {leaders.get('displayed_count', 0)}/5</span></div>"
        "<div class='table-scroll'><table><thead><tr><th>#</th><th>标的</th><th>连板/频率</th><th>综合分</th><th>入选理由</th><th>约束</th></tr></thead>"
        f"<tbody>{''.join(leader_rows)}</tbody></table></div>"
        f"<p class='emotion-note'>{html.escape(str(leaders.get('methodology', '')))}。数据说明：{html.escape(leader_quality_text)}</p></div>"
    )
    quality_text = "；".join(quality.get("errors", [])) or "全市场与证据校验完成"
    return f"""
<style>.near-limit{{margin:20px 0}}.near-head{{display:flex;justify-content:space-between;gap:12px;align-items:end}}.near-head p,.near-research p{{margin:4px 0;color:#667085;font-size:12px}}.near-kind{{display:inline-block;border-radius:999px;background:#fff0cd;color:#8a5a00;padding:4px 8px;font-weight:700}}.near-vacant td{{color:#7a8496;text-align:center;background:#fafbfc}}.near-research{{margin-top:18px;padding-top:14px;border-top:1px solid #e6e8ef}}.near-research h3{{margin:0 0 4px}}@media(max-width:800px){{.near-head{{align-items:flex-start;flex-direction:column}}}}</style>
<section class='near-limit'><div class='near-head'><div><h2>热点板块前三 · 临板候选</h2><p>{html.escape(data['label'])}；临板不等于必然涨停或买入信号。</p></div><span class='pill'>数据 {html.escape(quality['status'])} · 入选 {data['displayed_count']}/5</span></div>
<div class='table-scroll'><table><thead><tr><th>#</th><th>标的</th><th>状态</th><th>距涨停</th><th>评分/资金</th><th>题材联动</th><th>真实催化</th><th>建议</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
{render_focus_note(leaders.get('hot_sector_focus') or data.get('hot_sector_focus'))}{leader_html}<p class='emotion-note'>{html.escape(data['methodology'])}。数据说明：{html.escape(quality_text)}</p></section>"""


def render_near_limit_strip(report: Dict[str, Any]) -> str:
    data = report["emotion_near_limit"]
    quality = report["emotion_near_limit_data_quality"]
    if not quality["actionable"]:
        return "<section class='near-strip near-error'><b>热点前三：数据不足</b><span>不执行仓位变化</span></section>"
    summary = "；".join(
        f"{row['name']}·{row.get('theme', '')}第{row.get('sector_member_rank', '—')}名·{row['signal_type']}·距板{row['distance_to_limit_pct']:.2f}%·{row['recommendation']['action']}"
        for row in data["items"]
    ) or "暂无合格候选"
    leaders = report.get("emotion_leaders") or {"items": [], "displayed_count": 0}
    leader_summary = "；".join(
        f"{row['name']}·{row.get('theme', '')}第{row.get('sector_member_rank', '—')}名·{row['current_boards']}连板/近{row['recent_days']}日{row['recent_limit_count']}板·{row.get('strength_score', row['score']):.2f}"
        for row in leaders.get("items", [])
    )
    if leader_summary:
        summary += "｜程序识别情绪龙头：" + leader_summary
    return f"<section class='near-strip'><b>热点板块前三 · 临板候选 · {data['displayed_count']}/5</b><span>{html.escape(summary)}</span></section>"
