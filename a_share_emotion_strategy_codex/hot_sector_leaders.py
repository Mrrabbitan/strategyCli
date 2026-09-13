"""Shared, point-in-time hot-sector/leader gate. No trading or return forecasts."""

from __future__ import annotations

import datetime as dt
import math
import html
from collections import defaultdict
from typing import Any


VERSION = "hot-sector-top3-v2"
SCOPE = "沪深主板当期涨停池＋已扫描临板池；行业口径，非全部板块成分股排名"


def _number(value: Any, default: float = 0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (ValueError, TypeError):
        return default


def build_hot_sector_context(
    pool: list[dict], near_candidates: list[dict], *, as_of: str,
    source_verified: bool, config: dict,
) -> dict:
    """Rank BEFORE catalyst, liquidity, listing-age and trade-signal exclusions.

    A blocked leader keeps its original place, so fourth place never becomes
    third just because the first name fails a later validation.
    """
    policy = config["hot_sector_focus"]
    result = {
        "version": VERSION, "as_of": as_of, "scope": SCOPE,
        "status": "complete", "errors": [], "sectors": [], "members": {},
        "policy": dict(policy), "source_verified": source_verified,
        "methodology": "热点按涨停家数、最高连板、涨停成交额排序；板内按成交参与、封板质量、承接代理、重复强势及有限板位分综合排序；先排名再查资格，不补第四名；评分不是胜率或真实主力资金",
        "theme_continuity_status": "仅当期热点，多日主线持续性未核验",
        "research_only": True,
    }
    try:
        dt.date.fromisoformat(as_of)
    except (TypeError, ValueError):
        result["errors"].append("行情日期缺失或无效")
    if not source_verified:
        result["errors"].append("当期股票池日期/覆盖未通过核验")
    prefixes = config["near_limit"]["main_board_prefixes"]
    grouped: dict[str, list[dict]] = defaultdict(list)
    seen: set[str] = set()
    # Pool wins over a near-limit observation of the same code.
    for sealed, inputs in ((True, pool), (False, near_candidates)):
        for item in inputs:
            code, name = str(item.get("c", "")), str(item.get("n", ""))
            if not (len(code) == 6 and code.isdigit() and code.startswith(tuple(prefixes))):
                continue
            if "ST" in name.upper() or "退" in name or name.upper().startswith(("N", "C")):
                continue
            if code in seen:
                continue
            seen.add(code)
            theme = str(item.get("hybk") or "").strip()
            if theme in {"", "其他", "未知", "未确认"}:
                result["errors"].append(f"{code}行业归属缺失，不能证明前三排名")
                continue
            boards = int(_number(item.get("lbc") if sealed else item.get("prior_boards")))
            amount = _number(item.get("amount"))
            if amount <= 0 or boards < (1 if sealed else 0):
                result["errors"].append(f"{code}连板/成交数据缺失")
                continue
            recent = item.get("zttj") or {}
            grouped[theme].append({
                "code": code, "name": name, "theme": theme, "sealed": sealed,
                "current_boards": boards,
                "recent_limit_count": int(_number(recent.get("ct"), boards)),
                "seal_amount_ratio": _number(item.get("fund")) / amount if sealed else 0,
                "breaks": int(_number(item.get("zbc"), 999 if sealed else 0)),
                "amount_cny": amount,
                "turnover_pct": _number(item.get("hs")),
                "retreat_pct": (0 if sealed else max(0, (_number(item.get("high")) / _number(item.get("price")) - 1) * 100)) if sealed or min(_number(item.get("high")), _number(item.get("price"))) > 0 else None,
            })
    if result["errors"]:
        result["status"] = "error"
        return result
    sectors = []
    for theme, members in grouped.items():
        sealed_members = [row for row in members if row["sealed"]]
        count = len(sealed_members)
        if count < int(policy["min_limit_up_count"]):
            continue
        sectors.append({
            "theme": theme, "limit_up_count": count,
            "max_boards": max(row["current_boards"] for row in sealed_members),
            "limit_amount_cny": sum(row["amount_cny"] for row in sealed_members),
            "comparison_count": len(members),
        })
    sectors.sort(key=lambda row: (-row["limit_up_count"], -row["max_boards"], -row["limit_amount_cny"], row["theme"]))
    # No invented limit on the number of hot sectors. Each sector has 3 slots.
    for sector_rank, sector in enumerate(sectors, 1):
        sector["sector_rank"] = sector_rank
        members = grouped[sector["theme"]]
        largest_amount = max(row["amount_cny"] for row in members)
        weights = policy["strength_weights"]
        for row in members:
            turnover = row["turnover_pct"]
            turnover_quality = min(turnover / 3, 1) if turnover < 3 else max(0, 1 - max(0, turnover - 18) / 30)
            components = {
                "participation": (0.5 * row["amount_cny"] / largest_amount + 0.5 * turnover_quality) * weights["participation"],
                "seal_quality": min(row["seal_amount_ratio"] / 0.30, 1) * weights["seal_quality"],
                "resilience": (max(0, 1 - row["breaks"] / 5) if row["sealed"] else (max(0, 1 - row["retreat_pct"] / 8) if row["retreat_pct"] is not None else 0)) * weights["resilience"],
                "repeat_strength": min(row["recent_limit_count"] / 5, 1) * weights["repeat_strength"],
                "board_position": min(row["current_boards"] / 5, 1) * weights["board_position"],
            }
            row["strength_score"] = round(sum(components.values()), 4)
            row["strength_components"] = {key: round(value, 4) for key, value in components.items()}
            row["capital_continuity_status"] = "未核验持续净流入；成交、封单、重复涨停仅为代理"
        members.sort(key=lambda row: (
            -row["strength_score"], -row["amount_cny"], row["breaks"], row["code"],
        ))
        sector["top_codes"] = []
        for rank, row in enumerate(members, 1):
            row.update({"sector_rank": sector_rank, "sector_member_rank": rank,
                        "hot_sector_eligible": rank <= int(policy["max_per_sector"])})
            result["members"][row["code"]] = row
            if row["hot_sector_eligible"]:
                sector["top_codes"].append(row["code"])
        result["sectors"].append(sector)
    return result


def gate_reason(code: str, context: dict | None, config: dict) -> str:
    if not config.get("hot_sector_focus", {}).get("enabled", False):
        return ""
    if (not context or context.get("version") != VERSION or context.get("status") != "complete"
            or context.get("policy") != config.get("hot_sector_focus") or not context.get("source_verified")):
        return "热点板块前三排名数据不足"
    member = context.get("members", {}).get(code)
    if not member:
        return "不在当期热点板块强势股比较池"
    if not member.get("hot_sector_eligible"):
        return f"板块内第{member['sector_member_rank']}名，超出前三；不因其他股票被剔除而补位"
    return ""


def rank_fields(code: str, context: dict | None) -> dict:
    row = (context or {}).get("members", {}).get(code, {})
    return {key: row[key] for key in ("sector_rank", "sector_member_rank", "strength_score", "strength_components", "capital_continuity_status") if key in row} | {
        "leader_model_version": (context or {}).get("version"),
        "leader_rank_as_of": (context or {}).get("as_of"),
    }


def filter_ranked_rows(rows: list[dict], context: dict | None, config: dict) -> list[dict]:
    selected, seen = [], set()
    for row in rows:
        code = str(row.get("code", row.get("c", "")))
        if code in seen or gate_reason(code, context, config):
            continue
        seen.add(code)
        selected.append(dict(row, **rank_fields(code, context)))
    if config.get("hot_sector_focus", {}).get("enabled", False):
        selected.sort(key=lambda row: (row.get("sector_rank", 999), row.get("sector_member_rank", 999)))
    return selected


def render_focus_note(context: dict | None) -> str:
    if not context or context.get("status") != "complete":
        return "<p>热点前三排名待核验；不以旧榜或后排补位。</p>"
    sectors = "；".join(f"{row['theme']}：{row['limit_up_count']}只涨停" for row in context["sectors"])
    return (f"<p>行情日期 {html.escape(context['as_of'])}；{html.escape(context['scope'])}。"
            f"热点依据：{html.escape(sectors or '无板块达到门槛')}。"
            "板内先排前三再核验资格；保留原名次，不补第四名。各榜总展示上限继续适用。</p>")
