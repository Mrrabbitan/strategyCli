#!/usr/bin/env python3
"""Research-only thematic watchlists with auditable activation gates."""

from __future__ import annotations

import datetime as dt
import html
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from emotion_monitor import load_emotion_config, validate_catalyst
from market_data import fetch_history
from research_store import research_path


ROOT = Path(__file__).resolve().parent
WATCHLIST_PATH = research_path("theme_watchlists.json")


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_theme_watchlists(path: Path = WATCHLIST_PATH) -> Dict[str, Any]:
    if not path.exists():
        return {"version": 1, "research_only": True, "watchlists": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.setdefault("research_only", True)
    payload.setdefault("auto_trade_eligible", False)
    payload.setdefault("watchlists", [])
    return payload


def theme_watchlist_codes(payload: Optional[Dict[str, Any]] = None) -> List[str]:
    payload = payload or load_theme_watchlists()
    codes: List[str] = []
    for watchlist in payload.get("watchlists", []):
        for item in watchlist.get("items", []):
            code = str(item.get("code", ""))
            if len(code) == 6 and code.isdigit() and code not in codes:
                codes.append(code)
    return codes


def _rolling_above_ma20(rows: List[Dict[str, Any]], count: int) -> bool:
    if len(rows) < 20 + count - 1:
        return False
    for offset in range(count):
        end = len(rows) - offset
        closes = [_f(row.get("close")) for row in rows[end - 20:end]]
        if closes[-1] <= sum(closes) / 20:
            return False
    return True


def _quote_valid(tencent: Dict[str, Any], sina: Dict[str, Any]) -> tuple[bool, str]:
    first, second = _f(tencent.get("price")), _f(sina.get("price"))
    if first <= 0 or second <= 0:
        return False, "双源行情缺失"
    difference_pct = abs(first / second - 1) * 100
    if difference_pct > 0.5:
        return False, f"双源价格差{difference_pct:.2f}%"
    return True, "双源行情通过"


def build_theme_watchlist_report(
    as_of: dt.date,
    tencent_quotes: Dict[str, Dict[str, Any]],
    sina_quotes: Dict[str, Dict[str, Any]],
    evidence_map: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    histories: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload = payload or load_theme_watchlists()
    evidence_map = evidence_map or {}
    histories = dict(histories or {})
    codes = theme_watchlist_codes(payload)
    errors: List[str] = []

    missing = [code for code in codes if code not in histories]
    if missing:
        with ThreadPoolExecutor(max_workers=6) as executor:
            jobs = {executor.submit(fetch_history, code, 180): code for code in missing}
            for future in as_completed(jobs):
                code = jobs[future]
                try:
                    histories[code] = future.result()
                except Exception as exc:
                    errors.append(f"{code}日线：{exc}")

    emotion_config = load_emotion_config()
    reports: List[Dict[str, Any]] = []
    for watchlist in payload.get("watchlists", []):
        start = dt.date.fromisoformat(str(watchlist["start_date"]))
        end = dt.date.fromisoformat(str(watchlist["end_date"]))
        policy = dict(watchlist.get("activation_policy") or {})
        stage = "预备观察" if as_of < start else ("激活窗口" if as_of <= end else "窗口结束")
        items: List[Dict[str, Any]] = []
        for configured in watchlist.get("items", []):
            code = str(configured.get("code", ""))
            quote = tencent_quotes.get(code, {})
            quote_stamp = str(quote.get("timestamp", ""))
            include_as_of_close = (
                quote_stamp[:8] == as_of.strftime("%Y%m%d") and quote_stamp[8:14] >= "150000"
            )
            rows = [
                row for row in histories.get(code, [])
                if str(row.get("date", "")) < as_of.isoformat()
                or (include_as_of_close and str(row.get("date", "")) == as_of.isoformat())
            ]
            quote_ok, quote_reason = _quote_valid(quote, sina_quotes.get(code, {}))
            consecutive = int(policy.get("min_consecutive_closes_above_ma20", 2))
            above_ma20 = _rolling_above_ma20(rows, consecutive)
            closes = [_f(row.get("close")) for row in rows[-20:]]
            ma20 = sum(closes) / len(closes) if len(closes) == 20 else 0.0
            # Tencent's daily K-line volume uses shares for 688 STAR Market
            # equities but lots for the other equities in this watchlist.
            # fetch_history assumes lots, so undo that factor for 688 codes.
            turnover_scale = 0.01 if code.startswith("688") else 1.0
            avg_amount = (
                sum(_f(row.get("turnover_cny_est")) * turnover_scale for row in rows[-20:]) / 20
                if len(rows) >= 20 else 0.0
            )
            volume_ratio = _f(quote.get("amount_cny")) / avg_amount if avg_amount else 0.0
            distance_ma20 = (_f(quote.get("price")) / ma20 - 1) * 100 if ma20 else 0.0
            not_chasing = (
                _f(quote.get("pct")) <= _f(policy.get("max_open_or_daily_gain_pct", 7.0))
                and distance_ma20 <= _f(policy.get("max_distance_above_ma20_pct", 8.0))
            )
            catalyst_ok, catalysts, catalyst_reasons = validate_catalyst(
                code, str(configured.get("group", "")), evidence_map.get(code, []), as_of, emotion_config
            )
            technical_qualified = bool(
                quote_ok and above_ma20
                and volume_ratio >= _f(policy.get("min_volume_ratio_20d", 1.3))
                and not_chasing
            )
            items.append({
                **configured,
                "price": round(_f(quote.get("price")), 3),
                "pct": round(_f(quote.get("pct")), 3),
                "quote_timestamp": quote_stamp,
                "quote_valid": quote_ok,
                "quote_validation": quote_reason,
                "ma20": round(ma20, 3) if ma20 else None,
                "distance_above_ma20_pct": round(distance_ma20, 3) if ma20 else None,
                "volume_ratio_20d": round(volume_ratio, 3) if avg_amount else None,
                "consecutive_above_ma20": above_ma20,
                "not_chasing": not_chasing,
                "technical_qualified": technical_qualified,
                "verified_catalyst": catalyst_ok,
                "catalysts": catalysts,
                "catalyst_rejections": catalyst_reasons,
            })

        qualified_by_group: Dict[str, int] = {}
        for item in items:
            if item["technical_qualified"]:
                group = str(item.get("group", ""))
                qualified_by_group[group] = qualified_by_group.get(group, 0) + 1
        min_group = int(policy.get("min_group_qualified_count", 3))
        for item in items:
            group_confirmed = qualified_by_group.get(str(item.get("group", "")), 0) >= min_group
            activation_ready = bool(
                stage == "激活窗口" and item["technical_qualified"]
                and group_confirmed and item["verified_catalyst"]
            )
            item["group_confirmed"] = group_confirmed
            item["activation_ready"] = activation_ready
            if stage == "预备观察":
                item["status"] = "预备观察"
            elif stage == "窗口结束":
                item["status"] = "窗口结束，重新筛选"
            elif activation_ready:
                item["status"] = "条件触发，仍需进入动态目标复核"
            elif item["technical_qualified"] and group_confirmed:
                item["status"] = "量价联动通过，等待合格催化"
            elif item["technical_qualified"]:
                item["status"] = "个股量价通过，等待板块联动"
            else:
                item["status"] = "等待量价条件"
        reports.append({
            "id": watchlist.get("id"), "label": watchlist.get("label"),
            "start_date": start.isoformat(), "end_date": end.isoformat(), "stage": stage,
            "summary": watchlist.get("summary", ""),
            "decision_rules": list(watchlist.get("decision_rules") or []),
            "activation_policy": policy, "qualified_by_group": qualified_by_group,
            "items": items,
        })

    return {
        "as_of": as_of.isoformat(),
        "research_only": True,
        "auto_trade_eligible": False,
        "dynamic_target_qualification_required": True,
        "watchlists": reports,
        "data_quality": {
            "status": "ok" if not errors else "degraded",
            "actionable": False,
            "errors": errors,
            "synthetic_data_used": False,
            "scope": "主题研究观察，不影响情绪策略、临板Top 5或ETF关键数据质量",
        },
    }


def render_theme_watchlist_section(report: Dict[str, Any]) -> str:
    if not report.get("watchlists"):
        return ""
    blocks: List[str] = []
    for watchlist in report["watchlists"]:
        rows: List[str] = []
        for item in watchlist.get("items", []):
            price = f"{_f(item.get('price')):.2f}" if _f(item.get("price")) else "—"
            ma20 = f"{_f(item.get('ma20')):.2f}" if item.get("ma20") else "—"
            ratio = f"{_f(item.get('volume_ratio_20d')):.2f}x" if item.get("volume_ratio_20d") is not None else "—"
            disclosure = str(item.get("official_disclosure_url", ""))
            name = html.escape(str(item.get("name", "")))
            if disclosure:
                name = f'<a href="{html.escape(disclosure, quote=True)}">{name}</a>'
            risks = "、".join(str(value) for value in item.get("primary_risks", []) if value)
            rationale = html.escape(str(item.get("rationale", "")))
            if risks:
                rationale += "<br><small>主要风险：" + html.escape(risks) + "</small>"
            rows.append(
                "<tr>"
                f"<td>{name}<br><small>{html.escape(str(item.get('code', '')))} · {html.escape(str(item.get('tier', '')))}</small></td>"
                f"<td>{html.escape(str(item.get('group', '')))}</td>"
                f"<td>{price}<br><small>MA20 {ma20} · 量比 {ratio}</small></td>"
                f"<td>{html.escape(str(item.get('status', '')))}</td>"
                f"<td>{rationale}</td>"
                "</tr>"
            )
        summary = html.escape(str(watchlist.get("summary", "")))
        summary_html = f"<p>{summary}</p>" if summary else ""
        decision_rules = [str(value) for value in watchlist.get("decision_rules", []) if value]
        rules_html = ""
        if decision_rules:
            rules_html = "<ul>" + "".join(
                f"<li>{html.escape(value)}</li>" for value in decision_rules
            ) + "</ul>"
        blocks.append(
            "<section><h2>主题研究观察："
            + html.escape(str(watchlist.get("label", "")))
            + "</h2><p><b>窗口："
            + html.escape(str(watchlist.get("start_date", "")))
            + "—" + html.escape(str(watchlist.get("end_date", "")))
            + "；当前阶段：" + html.escape(str(watchlist.get("stage", "")))
            + "。</b> 这是研究观察池，不是固定标的，也不具备自动买入资格。"
            + "只有连续站上MA20、成交额达到20日均值1.3倍、同组至少3只联动、不过度追高，"
            + "且催化满足一个官方来源或两个独立主流财经来源，才进入动态目标复核。</p>"
            + summary_html + rules_html
            + "<table><thead><tr><th>标的</th><th>分组</th><th>量价</th><th>状态</th><th>入池理由</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table></section>"
        )
    return "".join(blocks)
