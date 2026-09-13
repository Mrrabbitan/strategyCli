#!/usr/bin/env python3
"""Research-only technology leader ranking for the 19:00 review."""

from __future__ import annotations

import html
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from emotion_monitor import _hard_risk_titles, load_emotion_config


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


def technology_direction(theme: str, config: Optional[Dict[str, Any]] = None) -> str:
    config = config or load_emotion_config()
    normalized = str(theme or "").strip()
    for direction, keywords in config["technology_leaders"]["directions"].items():
        if any(str(keyword) in normalized for keyword in keywords):
            return str(direction)
    return ""


def prefilter_technology_leaders(
    pool: List[Dict[str, Any]], config: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Select repeated-limit technology names across A-share boards."""
    config = config or load_emotion_config()
    cfg = config["technology_leaders"]
    rows: List[Dict[str, Any]] = []
    for item in pool:
        code = str(item.get("c", ""))
        name = str(item.get("n", "")).strip()
        theme = str(item.get("hybk", "")).strip()
        recent = item.get("zttj") or {}
        boards = _i(item.get("lbc"))
        recent_count = _i(recent.get("ct"))
        direction = technology_direction(theme, config)
        if not direction or not any(code.startswith(prefix) for prefix in cfg["code_prefixes"]):
            continue
        upper_name = name.upper()
        if "ST" in upper_name or "退" in name or upper_name.startswith(("N", "C")):
            continue
        if boards < _i(cfg["min_current_boards"]) and recent_count < _i(cfg["min_recent_limit_count"]):
            continue
        rows.append({**item, "technology_direction": direction})
    return rows


def _score(
    candidate: Dict[str, Any], quote: Dict[str, Any], direction_count: int,
    risk_level: str, breadth: Dict[str, Any], indices: Dict[str, Dict[str, Any]],
    config: Dict[str, Any],
) -> Tuple[float, Dict[str, float]]:
    cfg = config["technology_leaders"]
    weights = cfg["weights"]
    recent = candidate.get("zttj") or {}
    boards = _i(candidate.get("lbc"))
    recent_count = _i(recent.get("ct"))
    recent_days = max(_i(recent.get("days")), 1)
    breaks = _i(candidate.get("zbc"))
    amount = _f(quote.get("amount_cny"), _f(candidate.get("amount")))
    turnover = _f(quote.get("turnover"), _f(candidate.get("hs")))
    seal_ratio = _f(candidate.get("fund")) / max(amount, 1.0)
    open_gap = (_f(quote.get("open")) / max(_f(quote.get("prev_close")), 0.0001) - 1.0) * 100

    if 4.0 <= turnover <= 20.0:
        turnover_quality = 1.0
    elif turnover < 4.0:
        turnover_quality = _clamp(turnover / 4.0)
    else:
        turnover_quality = _clamp(1.0 - (turnover - 20.0) / 35.0)
    breadth_total = max(_i(breadth.get("total")), 1)
    breadth_ratio = _i(breadth.get("up")) / breadth_total
    index_mean = sum(_f(row.get("pct")) for row in indices.values()) / max(len(indices), 1)
    risk_factor = {"低": 1.0, "中低": 0.9, "中": 0.75, "中高": 0.5, "高": 0.25}.get(risk_level, 0.6)
    market_factor = 0.45 * _clamp((breadth_ratio - 0.35) / 0.35) + 0.35 * _clamp((index_mean + 1.0) / 3.0) + 0.20 * risk_factor

    components = {
        "height": _clamp(boards / 6.0) * _f(weights["height"]),
        "repeatability": (
            0.55 * _clamp(recent_count / 6.0) + 0.45 * _clamp(recent_count / recent_days)
        ) * _f(weights["repeatability"]),
        "seal_quality": (
            0.55 * _clamp(seal_ratio / 0.25) + 0.30 * (1.0 - _clamp(breaks / 8.0))
            + 0.15 * (1.0 - _clamp(max(open_gap - 5.0, 0.0) / 5.0))
        ) * _f(weights["seal_quality"]),
        "liquidity": (
            0.55 * _clamp(amount / 1_500_000_000.0) + 0.45 * turnover_quality
        ) * _f(weights["liquidity"]),
        "direction_linkage": _clamp(direction_count / 5.0) * _f(weights["direction_linkage"]),
        "market_fit": market_factor * _f(weights["market_fit"]),
    }
    return round(sum(components.values()), 2), {
        key: round(value, 2) for key, value in components.items()
    }


def build_technology_leader_report(
    candidates: List[Dict[str, Any]], quotes: Dict[str, Dict[str, Any]],
    sina_quotes: Dict[str, Dict[str, Any]], announcement_risks: Dict[str, List[str]],
    risk_level: str, breadth: Dict[str, Any], indices: Dict[str, Dict[str, Any]],
    metadata_errors: Optional[List[str]] = None,
    hot_sector_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    from hot_sector_leaders import filter_ranked_rows, gate_reason
    config = load_emotion_config()
    quote_days = {str(row.get("timestamp", ""))[:8] for row in quotes.values() if row.get("timestamp")}
    if hot_sector_context and (len(quote_days) != 1 or
        hot_sector_context.get("as_of", "").replace("-", "") not in quote_days):
        hot_sector_context = None
    cfg = config["technology_leaders"]
    direction_counts = Counter(str(row.get("technology_direction", "")) for row in candidates)
    eligible: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []

    for candidate in candidates:
        code = str(candidate.get("c", ""))
        name = str(candidate.get("n", ""))
        theme = str(candidate.get("hybk", ""))
        direction = str(candidate.get("technology_direction") or technology_direction(theme, config))
        quote = quotes.get(code)
        sina = sina_quotes.get(code)
        reasons: List[str] = []
        focus_reason = gate_reason(code, hot_sector_context, config)
        if focus_reason:
            reasons.append(focus_reason)
        if not candidate.get("listing_age_verified"):
            reasons.append("无法验证上市已满5个交易日")
        if not quote or not sina:
            reasons.append("双源行情缺失")
        else:
            gap = abs(_f(quote.get("price")) / max(_f(sina.get("price")), 0.0001) - 1.0) * 100
            if gap > _f(cfg["quote_price_tolerance_pct"]):
                reasons.append(f"双源价格偏差{gap:.2f}%")
        risk_titles = announcement_risks.get(code, [])
        if any(str(title).startswith("公告抓取失败") for title in risk_titles):
            reasons.append("官方公告风险校验失败")
        hard_titles = _hard_risk_titles(risk_titles, config)
        if hard_titles:
            reasons.append("命中官方硬风险公告：" + "；".join(hard_titles[:2]))
        if reasons:
            rejected.append({"code": code, "name": name, "direction": direction, "reasons": reasons})
            continue

        score, components = _score(
            candidate, quote, direction_counts[direction], risk_level, breadth, indices, config
        )
        recent = candidate.get("zttj") or {}
        boards = _i(candidate.get("lbc"))
        recent_count = _i(recent.get("ct"))
        recent_days = max(_i(recent.get("days")), 1)
        breaks = _i(candidate.get("zbc"))
        amount = _f(quote.get("amount_cny"), _f(candidate.get("amount")))
        turnover = _f(quote.get("turnover"), _f(candidate.get("hs")))
        seal_ratio = _f(candidate.get("fund")) / max(amount, 1.0)
        cosmic = (
            score >= _f(cfg["universe_score_threshold"])
            and boards >= _i(cfg["universe_min_boards"])
            and recent_count >= _i(cfg["universe_min_recent_limit_count"])
            and direction_counts[direction] >= _i(cfg["universe_min_direction_limit_count"])
        )
        grade = "哈药式宇宙龙头候选" if cosmic else ("方向龙头候选" if score >= 65 else "强势跟踪")
        chase_risk = breaks >= 5 or turnover > 25.0 or seal_ratio < 0.03
        eligible.append({
            "rank": 0, "direction_rank": 0, "code": code, "name": name,
            "theme": theme, "direction": direction, "grade": grade,
            "cosmic_candidate": cosmic, "score": score, "score_components": components,
            "current_boards": boards, "recent_days": recent_days,
            "recent_limit_count": recent_count, "pct": round(_f(quote.get("pct")), 3),
            "turnover_pct": round(turnover, 3), "amount_cny": amount,
            "seal_amount_ratio": round(seal_ratio, 4), "breaks": breaks,
            "direction_limit_count": direction_counts[direction],
            "risk_flag": "追高风险" if chase_risk else "分歧观察",
            "reasons": [
                f"当前{boards}连板，近{recent_days}日涨停{recent_count}次",
                f"封单/成交额{seal_ratio:.1%}、开板{breaks}次、换手{turnover:.2f}%",
                f"科技方向“{direction}”重复涨停候选{direction_counts[direction]}只",
                f"市场风险{risk_level}，市场适配得分{components['market_fit']:.2f}/{cfg['weights']['market_fit']}",
            ],
            "recommendation": {
                "action": "核心观察" if cosmic else "研究观察",
                "text": "只等次日分歧承接与再次转强，不在涨停价追；方向退潮或开板失去承接即失效",
            },
            "invalidation_conditions": ["开板后无法回封", "科技方向联动跌破门槛", "连板高度终止", "出现官方硬风险"],
            "research_only": True, "formal_eligible": False,
            "quote_timestamp": str(quote.get("timestamp", "")),
        })

    eligible.sort(key=lambda row: (
        -int(bool(row["cosmic_candidate"])), -_f(row["score"]), -_i(row["current_boards"]),
        -_i(row["recent_limit_count"]), _i(row["breaks"]), -_f(row["amount_cny"]), row["code"],
    ))
    eligible = filter_ranked_rows(eligible, hot_sector_context, config)
    selected: List[Dict[str, Any]] = []
    per_direction: Counter[str] = Counter()
    for row in eligible:
        direction = str(row["direction"])
        if per_direction[direction] >= _i(cfg["max_per_direction"]):
            continue
        selected.append(row)
        per_direction[direction] += 1
        if len(selected) >= _i(cfg["max_results"]):
            break
    direction_ranks: Counter[str] = Counter()
    for rank, row in enumerate(selected, 1):
        row["rank"] = rank
        direction_ranks[row["direction"]] += 1
        row["direction_rank"] = direction_ranks[row["direction"]]

    directions: List[Dict[str, Any]] = []
    for direction, count in direction_counts.most_common():
        rows = [row for row in eligible if row["direction"] == direction]
        if not rows:
            continue
        directions.append({
            "direction": direction, "candidate_count": count,
            "leader": rows[0]["name"], "leader_code": rows[0]["code"],
            "leader_score": rows[0]["score"], "cosmic_candidate": rows[0]["cosmic_candidate"],
        })
    errors = list(metadata_errors or [])
    quote_failures = sum(1 for row in rejected if any("行情" in reason for reason in row["reasons"]))
    actionable = not candidates or bool(eligible) or quote_failures < len(candidates)
    status = "complete" if not errors and not rejected else "degraded"
    return {
        "technology_cosmic_leaders": {
            "hot_sector_focus": hot_sector_context,
            "label": "科技方向宇宙龙头候选",
            "benchmark": "哈药式仅指高辨识度、高度、重复涨停和方向带动能力的量化结构，不代表复制其走势",
            "items": selected, "directions": directions,
            "eligible_count": len(eligible), "displayed_count": len(selected),
            "cosmic_candidate_count": sum(1 for row in selected if row["cosmic_candidate"]),
            "rejected": rejected,
            "methodology": "当前涨停池中的科技行业；双源行情与官方硬风险校验；综合高度、重复性、封板、流动性、方向联动和市场环境；每日动态重排",
            "research_only": True, "formal_eligible": False,
        },
        "technology_leader_data_quality": {
            "status": status, "actionable": actionable, "errors": errors,
            "warnings": (["部分候选因双源行情或官方风险校验被剔除"] if rejected else []),
            "synthetic_data_used": False,
        },
    }


def build_degraded_technology_leader_report(error: str) -> Dict[str, Any]:
    return {
        "technology_cosmic_leaders": {
            "label": "科技方向宇宙龙头候选", "benchmark": "数据不足",
            "items": [], "directions": [], "eligible_count": 0, "displayed_count": 0,
            "cosmic_candidate_count": 0, "rejected": [],
            "methodology": "数据不足，不沿用旧榜", "research_only": True, "formal_eligible": False,
        },
        "technology_leader_data_quality": {
            "status": "error", "actionable": False, "errors": [error], "warnings": [],
            "synthetic_data_used": False,
        },
    }


def render_technology_leader_section(report: Dict[str, Any]) -> str:
    data = report["technology_cosmic_leaders"]
    quality = report["technology_leader_data_quality"]
    direction_text = "；".join(
        f"{row['direction']}：{row['leader']} {row['leader_score']:.2f}"
        + ("（宇宙龙头候选）" if row["cosmic_candidate"] else "")
        for row in data["directions"]
    ) or "暂无满足重复涨停门槛的科技方向"
    rows = []
    for item in data["items"]:
        components = item["score_components"]
        rows.append(
            "<tr>"
            f"<td>{item['rank']}</td><td><b>{html.escape(item['name'])}</b><small>{item['code']}</small></td>"
            f"<td>{html.escape(item['direction'])}<small>{html.escape(item['theme'])} · 板内原始第{item.get('sector_member_rank', '—')}名</small></td>"
            f"<td><b>{item['score']:.2f}</b><small>{html.escape(item['grade'])}</small></td>"
            f"<td>{item['current_boards']}连板 / 近{item['recent_days']}日{item['recent_limit_count']}次"
            f"<small>封成比{item['seal_amount_ratio']:.1%} · 开板{item['breaks']}次 · 换手{item['turnover_pct']:.2f}%</small></td>"
            f"<td>{html.escape(item['risk_flag'])}<small>{html.escape(item['recommendation']['text'])}</small></td>"
            f"<td><small>高度{components['height']:.1f} / 重复{components['repeatability']:.1f} / "
            f"封板{components['seal_quality']:.1f} / 流动性{components['liquidity']:.1f} / "
            f"方向{components['direction_linkage']:.1f} / 市场{components['market_fit']:.1f}</small></td>"
            "</tr>"
        )
    body = "".join(rows) or "<tr><td colspan='7'>暂无合格科技龙头候选，保留空缺。</td></tr>"
    errors = "；".join(quality.get("errors") or []) or "双源行情、官方硬风险和量价结构校验完成"
    return (
        "<section><h2>科技方向宇宙龙头候选</h2>"
        f"<div class='card'><b>方向雷达：</b>{html.escape(direction_text)}<br>"
        f"<small>{html.escape(data['benchmark'])}。只供研究，不构成交易资格。</small></div>"
        "<table><thead><tr><th>#</th><th>标的</th><th>科技方向</th><th>综合分</th>"
        "<th>强度结构</th><th>风险与建议</th><th>分项</th></tr></thead>"
        f"<tbody>{body}</tbody></table>"
        f"<p class='emotion-note'>{html.escape(data['methodology'])}。数据：{html.escape(quality['status'])}；{html.escape(errors)}</p>"
        "</section>"
    )
