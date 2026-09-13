#!/usr/bin/env python3
"""Rule-based A-share emotion watchlist and alert engine.

The module intentionally uses public quote/limit-pool proxies.  It never labels
those proxies as actual brokerage or institutional account flows and it cannot
place orders.
"""

from __future__ import annotations

import datetime as dt
import html
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
from research_store import config_path, private_path
CONFIG_PATH = config_path("emotion_config.json")
STATE_PATH = private_path("data/emotion/watchlist.json")
CALENDAR_PATH = config_path("market_calendar.json")


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


def load_emotion_config(path: Path = CONFIG_PATH) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_watchlist(path: Path = STATE_PATH) -> Dict[str, Any]:
    if not path.exists():
        return {"version": 2, "updated_at": "", "entries": [], "last_dynamic_targets": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.setdefault("version", 2)
    payload.setdefault("updated_at", "")
    payload.setdefault("entries", [])
    payload.setdefault("last_dynamic_targets", [])
    return payload


def save_watchlist(state: Dict[str, Any], path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def load_catalyst_evidence(path: Optional[Path]) -> Tuple[Dict[str, List[Dict[str, Any]]], List[str]]:
    if not path:
        return {}, ["未提供催化证据文件；自动候选只预筛、不入正式观察池"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, [f"催化证据文件读取失败：{exc}"]
    records = payload.get("catalysts", payload if isinstance(payload, list) else [])
    if not isinstance(records, list):
        return {}, ["催化证据字段 catalysts 必须为数组"]
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        code = str(record.get("code", ""))
        if len(code) == 6 and code.isdigit():
            grouped.setdefault(code, []).append(record)
    return grouped, []


def _closures() -> set[str]:
    payload = json.loads(CALENDAR_PATH.read_text(encoding="utf-8"))
    result: set[str] = set()
    for dates in payload.get("closures", {}).values():
        result.update(str(value) for value in dates)
    return result


def _is_trading_day(day: dt.date, closures: set[str]) -> bool:
    return day.weekday() < 5 and day.isoformat() not in closures


def add_trading_days(day: dt.date, count: int) -> dt.date:
    closures = _closures()
    current = day
    remaining = count
    while remaining > 0:
        current += dt.timedelta(days=1)
        if _is_trading_day(current, closures):
            remaining -= 1
    return current


def active_auto_codes(report_date: dt.date, state: Optional[Dict[str, Any]] = None) -> List[str]:
    state = state or load_watchlist()
    result = []
    for entry in state.get("entries", []):
        if entry.get("source") != "auto" or entry.get("status") != "active":
            continue
        expires_on = str(entry.get("expires_on", ""))
        if expires_on and report_date.isoformat() > expires_on:
            continue
        result.append(str(entry.get("code", "")))
    return [code for code in result if len(code) == 6]


def _domain(url: str) -> str:
    return urlparse(url).netloc.lower().split(":", 1)[0]


def _domain_matches(domain: str, suffixes: Iterable[str]) -> bool:
    return any(domain == suffix or domain.endswith("." + suffix) for suffix in suffixes)


def validate_catalyst(
    code: str,
    theme: str,
    records: List[Dict[str, Any]],
    report_date: dt.date,
    config: Dict[str, Any],
) -> Tuple[bool, List[Dict[str, Any]], List[str]]:
    catalyst_cfg = config["catalyst"]
    valid: List[Dict[str, Any]] = []
    reasons: List[str] = []
    cutoff = report_date - dt.timedelta(days=_i(catalyst_cfg["max_age_calendar_days"], 7))
    for record in records:
        title = str(record.get("title", "")).strip()
        url = str(record.get("url", "")).strip()
        published = str(record.get("published_at", ""))[:10]
        event_type = str(record.get("event_type", "")).strip()
        matched_theme = str(record.get("theme", "")).strip()
        domain = _domain(url)
        try:
            published_date = dt.date.fromisoformat(published)
        except ValueError:
            reasons.append("催化缺少有效发布时间")
            continue
        if not title or not url or not event_type or not matched_theme:
            reasons.append("催化证据缺少标题、URL、事件类型或题材")
            continue
        if published_date < cutoff or published_date > report_date:
            reasons.append(f"催化证据已过期或来自未来：{published}")
            continue
        if _domain_matches(domain, catalyst_cfg["rejected_domains"]):
            reasons.append(f"不接受社媒/论坛来源：{domain}")
            continue
        if theme and matched_theme != theme:
            reasons.append(f"催化题材不匹配：{matched_theme} != {theme}")
            continue
        source_tier = str(record.get("source_tier", "mainstream")).lower()
        source_type = str(record.get("source_type", "")).lower()
        official = source_tier == "official" and (
            _domain_matches(domain, catalyst_cfg["official_domains"])
            or (source_type == "company" and domain and not _domain_matches(domain, catalyst_cfg["rejected_domains"]))
        )
        enriched = dict(record)
        enriched.update({"domain": domain, "official": official, "verified_for_code": code})
        valid.append(enriched)
    official_valid = [row for row in valid if row["official"]]
    mainstream_domains = {row["domain"] for row in valid if not row["official"] and row["domain"]}
    verified = bool(official_valid) or len(mainstream_domains) >= 2
    if not verified:
        reasons.append("真实催化需一个官方来源，或两个独立主流财经来源")
    return verified, valid if verified else [], reasons


def quote_metrics(quote: Dict[str, Any], pool: Dict[str, Any], theme_limit_count: int) -> Dict[str, Any]:
    price = _f(quote.get("price"))
    previous = _f(quote.get("prev_close"))
    opened = _f(quote.get("open"))
    high = _f(quote.get("high"))
    low = _f(quote.get("low"))
    amount = _f(quote.get("amount_cny"))
    volume_shares = _f(quote.get("volume_lots")) * 100
    buy_lots = _f(quote.get("buy_lots"))
    sell_lots = _f(quote.get("sell_lots"))
    active_total = buy_lots + sell_lots
    vwap = amount / volume_shares if volume_shares > 0 else 0.0
    pct = _f(quote.get("pct"))
    return {
        "price": price,
        "pct": pct,
        "open_gap_pct": ((opened / previous - 1) * 100) if previous else 0.0,
        "vwap": vwap,
        "below_vwap_pct": ((vwap - price) / vwap * 100) if vwap else 0.0,
        "retreat_from_high_pct": ((high - price) / high * 100) if high else 0.0,
        "active_sell_ratio": (sell_lots / active_total) if active_total else 0.0,
        "turnover_pct": _f(quote.get("turnover")),
        "amount_cny": amount,
        "boards": _i(pool.get("lbc")),
        "breaks": _i(pool.get("zbc")),
        "seal_amount_ratio": _f(pool.get("fund")) / max(_f(pool.get("amount")), 1.0),
        "theme_limit_count": theme_limit_count,
        "at_limit_up": pct >= 9.5,
        "one_word": bool(price and opened == high == low and pct >= 9.5),
        "quote_timestamp": str(quote.get("timestamp", "")),
    }


def preliminary_candidate(
    item: Dict[str, Any], quote: Dict[str, Any], theme_count: int, config: Dict[str, Any]
) -> Tuple[bool, List[str], Dict[str, Any]]:
    filters = config["candidate_filters"]
    pool = item
    metrics = quote_metrics(quote, pool, theme_count)
    reasons: List[str] = []
    name = str(item.get("n", ""))
    if "ST" in name.upper():
        reasons.append("ST标的")
    boards = _i(item.get("lbc"))
    if not (_i(filters["min_boards"]) <= boards <= _i(filters["max_boards"])):
        reasons.append("板位不在1—2板")
    turnover = _f(item.get("hs"))
    if not (_f(filters["min_turnover_pct"]) <= turnover <= _f(filters["max_turnover_pct"])):
        reasons.append("换手率不在2%—18%")
    if _f(item.get("amount")) < _f(filters["min_amount_cny"]):
        reasons.append("成交额低于1亿元")
    if metrics["one_word"]:
        reasons.append("一字加速不可成交")
    if theme_count < _i(filters["min_theme_limit_count"]):
        reasons.append("板块联动不足")
    if metrics["breaks"] > _i(filters["max_initial_breaks"]):
        reasons.append("炸板次数过多")
    if metrics["seal_amount_ratio"] < _f(filters["min_seal_amount_ratio"]):
        reasons.append("封单成交比不足")
    if (
        metrics["active_sell_ratio"] > _f(filters["max_initial_active_sell_ratio"])
        and metrics["seal_amount_ratio"] < _f(filters["min_seal_amount_ratio"]) * 1.6
    ):
        reasons.append("主动卖出与弱封单共振")
    return not reasons, reasons, metrics


def _hard_risk_titles(titles: Iterable[str], config: Dict[str, Any]) -> List[str]:
    words = config["catalyst"]["hard_risk_words"]
    return [str(title) for title in titles if any(word in str(title) for word in words)]


def _evaluate_alert(
    entry: Dict[str, Any], metrics: Dict[str, Any], previous: Dict[str, Any], risk_titles: List[str],
    risk_level: str, new_catalyst: bool, config: Dict[str, Any], data_ok: bool,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    alerts = config["alerts"]
    negative: List[Dict[str, Any]] = []
    positive: List[Dict[str, Any]] = []
    hard = False
    if not data_ok:
        recommendation = {"action": "数据不足", "text": "数据不足，不执行仓位变化"}
        return {
            "code": entry["code"], "name": entry["name"], "severity": "none", "direction": "数据不足",
            "triggers": [], "recommendation": recommendation,
        }, recommendation
    if metrics["open_gap_pct"] > _f(alerts["high_open_hard_pct"]):
        negative.append({"type": "high_open", "label": f"高开{metrics['open_gap_pct']:.2f}%>7%", "hard": True})
        hard = True
    if metrics["pct"] <= _f(alerts["limit_down_pct"]):
        negative.append({"type": "limit_down", "label": "触及跌停/接近跌停", "hard": True})
        hard = True
    hard_titles = _hard_risk_titles(risk_titles, config)
    if hard_titles:
        negative.append({"type": "official_risk", "label": "官方硬风险：" + "；".join(hard_titles[:2]), "hard": True})
        hard = True
    retreat = metrics["retreat_from_high_pct"]
    if retreat >= _f(alerts["high_retreat_red_pct"]):
        negative.append({"type": "high_retreat", "label": f"较日内高点回落{retreat:.2f}%", "hard": True})
        hard = True
    elif retreat >= _f(alerts["high_retreat_orange_pct"]):
        negative.append({"type": "high_retreat", "label": f"较日内高点回落{retreat:.2f}%", "hard": False})
    if metrics["below_vwap_pct"] >= _f(alerts["below_vwap_pct"]) and metrics["active_sell_ratio"] >= _f(alerts["active_sell_ratio"]):
        negative.append({
            "type": "fund_outflow_proxy",
            "label": f"低于VWAP {metrics['below_vwap_pct']:.2f}%且主动卖出占比{metrics['active_sell_ratio']:.1%}",
            "hard": True,
        })
        hard = True
    break_increase = metrics["breaks"] - _i(previous.get("breaks"))
    if "breaks" in previous and break_increase >= _i(alerts["break_increase_red"]) and not metrics["at_limit_up"]:
        negative.append({"type": "break_increase", "label": f"炸板次数增加{break_increase}且未回封", "hard": True})
        hard = True
    previous_seal = _f(previous.get("seal_amount_ratio"))
    if previous_seal > 0 and metrics["seal_amount_ratio"] > 0:
        seal_drop = (previous_seal - metrics["seal_amount_ratio"]) / previous_seal
        if seal_drop >= _f(alerts["seal_ratio_drop_red"]):
            negative.append({"type": "seal_drop", "label": f"封单成交比较上次下降{seal_drop:.1%}", "hard": True})
            hard = True
    theme_linked = metrics["theme_limit_count"] >= _i(config["candidate_filters"]["min_theme_limit_count"])
    if not theme_linked:
        negative.append({"type": "theme_weak", "label": "板块联动跌破门槛", "hard": False})
    if new_catalyst:
        positive.append({"type": "catalyst", "label": "出现已验证的新催化"})
    if theme_linked:
        positive.append({"type": "theme_linked", "label": f"同题材涨停{metrics['theme_limit_count']}只"})
    tape_strong = (
        metrics["pct"] >= _f(alerts["strong_pct"])
        and metrics["vwap"] > 0
        and metrics["below_vwap_pct"] <= 0
        and metrics["retreat_from_high_pct"] <= _f(alerts["strong_retreat_max_pct"])
    )
    if tape_strong:
        positive.append({"type": "tape_strong", "label": "价格强于VWAP且接近日内高位"})

    if hard or len(negative) >= 2:
        severity, direction, triggers = "red", "追高风险" if all(row["type"] == "high_open" for row in negative) else "转弱", negative
    elif len(positive) >= 2 and not negative:
        severity, direction, triggers = "red", "转强", positive
    elif negative:
        severity, direction, triggers = "orange", "转弱观察", negative
    elif positive and new_catalyst:
        severity, direction, triggers = "orange", "催化待确认", positive
    else:
        severity, direction, triggers = "none", "平稳", []

    if severity == "red" and direction in {"转弱", "硬风险"}:
        recommendation = {"action": "减仓/退出", "text": "已持有：减仓/退出；未持有：剔除/不介入"}
    elif severity == "red" and direction == "追高风险":
        recommendation = {"action": "禁止介入", "text": "高开超过7%，禁止追高；已有仓按承接和原计划处理"}
    elif severity == "red" and direction == "转强":
        allow_trial = (
            risk_level != "高" and metrics["open_gap_pct"] <= 5.0 and not metrics["at_limit_up"]
            and metrics["below_vwap_pct"] <= 0 and not hard
            and (new_catalyst or bool(entry.get("catalyst_evidence")))
        )
        if allow_trial:
            recommendation = {
                "action": "允许小仓试错",
                "text": f"仅允许小仓试错：单票≤{config['candidate_max_capital_cny'] / 10000:.1f}万元，最多{config['max_new_positions']}只，合计≤{config['max_new_total_capital_cny'] / 10000:.1f}万元",
            }
        else:
            recommendation = {"action": "等待承接", "text": "转强但不满足试错条件，等待分歧、承接、再转强；涨停价不追"}
    elif severity == "orange":
        recommendation = {"action": "等待承接", "text": "单项异动尚未共振，等待下一时点确认"}
    else:
        recommendation = {"action": "持有观察", "text": "未出现需要调整的共振信号，继续观察"}
    return {
        "code": entry["code"], "name": entry["name"], "severity": severity, "direction": direction,
        "triggers": triggers, "recommendation": recommendation,
    }, recommendation


def build_emotion_report(
    report_date: dt.date,
    slot: str,
    risk_level: str,
    candidates: List[Dict[str, Any]],
    quotes: Dict[str, Dict[str, Any]],
    pool_map: Dict[str, Dict[str, Any]],
    theme_counts: Dict[str, int],
    evidence_map: Dict[str, List[Dict[str, Any]]],
    evidence_errors: List[str],
    announcement_risks: Dict[str, List[str]],
    persist_state: bool = True,
    state_path: Path = STATE_PATH,
    hot_sector_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    from hot_sector_leaders import filter_ranked_rows, gate_reason, rank_fields
    config = load_emotion_config()
    if slot != "09_00" and hot_sector_context and hot_sector_context.get("as_of") != report_date.isoformat():
        hot_sector_context = None
    state = load_watchlist(state_path)
    today = report_date.isoformat()
    for entry in state["entries"]:
        if entry.get("status") == "active" and today > str(entry.get("expires_on", today)):
            entry["status"] = "expired"
            entry["invalidated_reason"] = "达到5个交易日留存上限"

    active_auto = [row for row in state["entries"] if row.get("source") == "auto" and row.get("status") == "active"]
    active_codes = {str(row.get("code")) for row in active_auto}
    admissions_today = sum(1 for row in state["entries"] if row.get("source") == "auto" and row.get("selected_at") == today)
    selection = {"prequalified": [], "admitted": [], "rejected": []}
    for item in candidates:
        code = str(item.get("c", ""))
        name = str(item.get("n", ""))
        theme = str(item.get("hybk", "其他"))
        quote = quotes.get(code, {})
        preliminary_ok, reasons, metrics = preliminary_candidate(item, quote, _i(theme_counts.get(theme)), config)
        focus_reason = gate_reason(code, hot_sector_context, config)
        if focus_reason:
            reasons.append(focus_reason)
        if preliminary_ok:
            selection["prequalified"].append({"code": code, "name": name, "theme": theme})
        verified, evidence, catalyst_reasons = validate_catalyst(code, theme, evidence_map.get(code, []), report_date, config)
        hard_titles = _hard_risk_titles(announcement_risks.get(code, []), config)
        reasons.extend(catalyst_reasons if not verified else [])
        if hard_titles:
            reasons.append("命中官方硬风险公告：" + "；".join(hard_titles[:2]))
        if code in active_codes:
            continue
        if reasons:
            selection["rejected"].append({"code": code, "name": name, "theme": theme, "reasons": reasons})
            continue
        if admissions_today >= _i(config["daily_admission_limit"]):
            selection["rejected"].append({"code": code, "name": name, "theme": theme, "reasons": ["当日新增上限5只"]})
            continue
        if len(active_auto) >= _i(config["max_auto_watchlist"]):
            selection["rejected"].append({"code": code, "name": name, "theme": theme, "reasons": ["自动观察池已达25只"]})
            continue
        entry = {
            "code": code, "name": name, "theme": theme, "source": "auto", "status": "active",
            "selected_at": today, "expires_on": add_trading_days(report_date, _i(config["retention_trading_days"]) - 1).isoformat(),
            "catalyst_evidence": evidence, "last_metrics": metrics, "alert_history": [],
        }
        state["entries"].append(entry)
        active_auto.append(entry)
        active_codes.add(code)
        admissions_today += 1
        selection["admitted"].append({"code": code, "name": name, "theme": theme, "expires_on": entry["expires_on"], "evidence": evidence})

    rows: List[Dict[str, Any]] = []
    alerts_out: List[Dict[str, Any]] = []
    recommendations: List[Dict[str, Any]] = []
    current_entries = [row for row in state["entries"] if row.get("source") == "auto" and row.get("status") == "active"]
    candidate_scores = {str(item.get("c", "")): _f(item.get("score"), 0.0) for item in candidates}
    missing_quotes: List[str] = []
    for entry in current_entries:
        code = str(entry["code"])
        quote = quotes.get(code)
        if not quote:
            missing_quotes.append(code)
            metrics: Dict[str, Any] = {}
            data_ok = False
        else:
            pool = pool_map.get(code, {})
            theme = str(entry.get("theme") or pool.get("hybk") or "未确认")
            metrics = quote_metrics(quote, pool, _i(theme_counts.get(theme)))
            data_ok = bool(metrics["quote_timestamp"] and metrics["vwap"] > 0)
            if not data_ok:
                missing_quotes.append(code)
        verified, verified_evidence, _ = validate_catalyst(
            code, str(entry.get("theme", "")), evidence_map.get(code, []), report_date, config
        )
        previous = dict(entry.get("last_metrics") or {})
        alert, recommendation = _evaluate_alert(
            entry, metrics, previous, announcement_risks.get(code, []), risk_level,
            new_catalyst=verified, config=config, data_ok=data_ok,
        )
        qualification_reasons: List[str] = []
        pool_item = pool_map.get(code)
        if pool_item and quote:
            basic_ok, qualification_reasons, _ = preliminary_candidate(
                pool_item, quote, _i(theme_counts.get(str(pool_item.get("hybk", "其他")))), config
            )
        else:
            basic_ok = False
            qualification_reasons.append("当前不在1—2板联动候选池")
        historical_evidence = entry.get("catalyst_evidence") or verified_evidence
        catalyst_ok = bool(historical_evidence)
        focus_reason = gate_reason(code, hot_sector_context, config)
        if focus_reason:
            basic_ok = False
            qualification_reasons.append(focus_reason)
            if recommendation.get("action") not in {"减仓/退出", "禁止介入"}:
                recommendation = {"action": "仅风险跟踪", "text": focus_reason + "；不新增，已有风险提醒保留"}
        if not catalyst_ok:
            qualification_reasons.append("未验证真实催化")
        row = {
            **rank_fields(code, hot_sector_context),
            "code": code, "name": entry["name"], "theme": entry.get("theme", "未确认"), "source": entry["source"],
            "selected_at": entry.get("selected_at", ""), "expires_on": entry.get("expires_on", ""),
            "selection_qualified": basic_ok and catalyst_ok, "qualification_reasons": qualification_reasons,
            "catalyst_evidence": historical_evidence, "metrics": metrics, "severity": alert["severity"],
            "direction": alert["direction"], "recommendation": recommendation,
            "dynamic_score": candidate_scores.get(code, 0.0),
        }
        rows.append(row)
        recommendations.append({"code": code, "name": entry["name"], **recommendation})
        if alert["severity"] in {"red", "orange"}:
            alerts_out.append(alert)
        if entry["source"] == "auto" and data_ok:
            entry["last_metrics"] = metrics
            if alert["severity"] != "none":
                entry.setdefault("alert_history", []).append({
                    "date": today, "slot": slot, "severity": alert["severity"], "direction": alert["direction"],
                    "triggers": alert["triggers"],
                })
                entry["alert_history"] = entry["alert_history"][-20:]
            invalidating_types = {trigger["type"] for trigger in alert["triggers"]}
            if alert["severity"] == "red" and alert["direction"] == "转弱" and invalidating_types.intersection({
                "limit_down", "official_risk", "fund_outflow_proxy", "high_retreat"
            }):
                entry["status"] = "invalidated"
                entry["invalidated_reason"] = "负向红色共振触发失效"

    dynamic_limit = _i(config.get("dynamic_target_limit"), 5)
    dynamic_items = [
        row for row in rows
        if row["selection_qualified"]
        and row["recommendation"]["action"] not in {"减仓/退出", "禁止介入"}
    ]
    dynamic_items.sort(
        key=lambda row: (
            _f(row.get("dynamic_score")),
            _f((row.get("metrics") or {}).get("theme_limit_count")),
            _f((row.get("metrics") or {}).get("pct")),
        ),
        reverse=True,
    )
    dynamic_items = filter_ranked_rows(dynamic_items, hot_sector_context, config)[:dynamic_limit]
    previous_dynamic = [str(code) for code in state.get("last_dynamic_targets", [])]
    current_dynamic = [str(row["code"]) for row in dynamic_items]
    state["last_dynamic_targets"] = current_dynamic
    state["version"] = 2
    state["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
    if persist_state:
        save_watchlist(state, state_path)
    red_count = sum(1 for row in alerts_out if row["severity"] == "red")
    orange_count = sum(1 for row in alerts_out if row["severity"] == "orange")
    quality_errors = list(evidence_errors)
    focus_missing = config.get("hot_sector_focus", {}).get("enabled") and (
        not hot_sector_context or hot_sector_context.get("status") != "complete"
    )
    if focus_missing:
        quality_errors.append("热点前三排名缺失或未通过校验")
    if missing_quotes:
        quality_errors.append("观察池行情缺失：" + "、".join(missing_quotes))
    unverified = sum(1 for row in selection["rejected"] if any("催化" in reason or "来源" in reason for reason in row["reasons"]))
    quality_status = "error" if missing_quotes else ("degraded" if quality_errors or unverified else "complete")
    return {
        "emotion_watchlist": rows,
        "emotion_selection": selection,
        "emotion_alerts": sorted(alerts_out, key=lambda row: (row["severity"] == "red", row["direction"] == "转弱"), reverse=True),
        "emotion_recommendations": recommendations,
        "emotion_dynamic_targets": {
            "hot_sector_focus": hot_sector_context,
            "mode": "dynamic",
            "items": dynamic_items,
            "displayed_count": len(dynamic_items),
            "vacant_count": max(dynamic_limit - len(dynamic_items), 0),
            "replacements": {
                "added": [code for code in current_dynamic if code not in previous_dynamic],
                "dropped": [code for code in previous_dynamic if code not in current_dynamic],
            },
            "methodology": "每个时点按1—2板、真实催化、板块联动、双源行情、资金代理和风险条件重排；无固定标的，更优合格候选可接替",
        },
        "emotion_data_quality": {
            "status": quality_status, "errors": quality_errors, "missing_quote_codes": missing_quotes,
            "unverified_candidate_count": unverified, "actionable": not missing_quotes and not focus_missing, "synthetic_data_used": False,
            "fund_flow_disclaimer": "主动买卖、VWAP、封单和炸板仅为公开行情代理，不代表真实机构账户资金流",
        },
        "emotion_summary": {"slot": slot, "red_count": red_count, "orange_count": orange_count, "tracked_count": len(rows)},
    }


def render_emotion_section(report: Dict[str, Any]) -> str:
    summary = report["emotion_summary"]
    quality = report["emotion_data_quality"]
    alert_items = []
    for alert in report["emotion_alerts"]:
        klass = "emotion-red" if alert["severity"] == "red" else "emotion-orange"
        icon = "🔴" if alert["severity"] == "red" else "🟠"
        trigger_text = "；".join(html.escape(str(row["label"])) for row in alert["triggers"])
        alert_items.append(
            f"<li class='{klass}'><b>{icon} {html.escape(alert['name'])}（{alert['code']}）·{html.escape(alert['direction'])}</b>"
            f"<span>{trigger_text}</span><em>{html.escape(alert['recommendation']['text'])}</em></li>"
        )
    if not alert_items:
        alert_items.append("<li class='emotion-clear'><b>无红色异动</b><span>本时点未出现需要调整的共振信号。</span></li>")
    rows = []
    for item in report["emotion_watchlist"]:
        level = "🔴" if item["severity"] == "red" else ("🟠" if item["severity"] == "orange" else "—")
        source = "动态入池"
        qualified = "通过" if item["selection_qualified"] else "未通过"
        metrics = item.get("metrics") or {}
        tape = "数据不足" if not metrics else (
            f"涨跌{metrics['pct']:+.2f}%；VWAP{metrics['vwap']:.2f}；高点回落{metrics['retreat_from_high_pct']:.2f}%；"
            f"主动卖出{metrics['active_sell_ratio']:.1%}；同题材涨停{metrics['theme_limit_count']}只"
        )
        evidence = item.get("catalyst_evidence") or []
        evidence_html = "未验证"
        if evidence:
            first = evidence[0]
            evidence_html = f"<a href='{html.escape(str(first.get('url', '')), quote=True)}' target='_blank' rel='noreferrer'>{html.escape(str(first.get('title', '已验证催化')))}</a>"
        rows.append(
            "<tr>" + "".join([
                f"<td>{level}</td>", f"<td><b>{html.escape(item['name'])}</b><small>{item['code']} · {source}</small></td>",
                f"<td>{html.escape(str(item['theme']))}<small>筛选资格：{qualified}</small></td>", f"<td>{tape}</td>",
                f"<td>{evidence_html}</td>", f"<td><b>{html.escape(item['recommendation']['action'])}</b><small>{html.escape(item['recommendation']['text'])}</small></td>",
            ]) + "</tr>"
        )
    return f"""
<style>.emotion-monitor{{margin:20px 0}}.emotion-head{{display:flex;justify-content:space-between;gap:12px;align-items:center}}.emotion-counts{{display:flex;gap:8px;flex-wrap:wrap}}.emotion-counts span{{padding:6px 10px;border-radius:999px;background:#f1f3f7}}.emotion-counts .red{{background:#fee2e2;color:#a20f0f;font-weight:800}}.emotion-counts .orange{{background:#ffedd5;color:#9a4b00;font-weight:800}}.emotion-alerts{{list-style:none;padding:0;margin:12px 0;display:grid;gap:8px}}.emotion-alerts li{{border-radius:10px;padding:12px 14px;display:grid;gap:5px}}.emotion-alerts span,.emotion-alerts em{{font-size:13px;font-style:normal}}.emotion-red{{background:#fff0f0;border:1px solid #ef4444;color:#8f1010}}.emotion-orange{{background:#fff7ed;border:1px solid #f59e0b;color:#8a4900}}.emotion-clear{{background:#f3f6fa;border:1px solid #d8dee8;color:#536174}}.emotion-note{{font-size:12px;color:#667085}}@media(max-width:800px){{.emotion-head{{align-items:flex-start;flex-direction:column}}}}</style>
<section class="emotion-monitor"><div class="emotion-head"><div><h2>情绪票异动监控</h2><div class="emotion-note">全部标的动态筛选；公开量价仅为资金代理。</div></div><div class="emotion-counts"><span class="red">红色 {summary['red_count']}</span><span class="orange">橙色 {summary['orange_count']}</span><span>跟踪 {summary['tracked_count']}</span><span>数据 {html.escape(quality['status'])}</span></div></div><ul class="emotion-alerts">{''.join(alert_items)}</ul><div class="table-scroll"><table><thead><tr><th>告警</th><th>标的</th><th>题材/资格</th><th>量价与联动</th><th>真实催化</th><th>操作建议</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div></section>"""


def render_dynamic_targets_section(report: Dict[str, Any]) -> str:
    from hot_sector_leaders import render_focus_note
    dynamic = report["emotion_dynamic_targets"]
    rows = []
    for rank, item in enumerate(dynamic["items"], 1):
        metrics = item.get("metrics") or {}
        rows.append(
            "<tr>" + "".join([
                f"<td>{rank}</td>",
                f"<td><b>{html.escape(item['name'])}</b><small>{item['code']}</small></td>",
                f"<td>{html.escape(str(item.get('theme', '')))} · 原始第{item.get('sector_member_rank', '—')}名</td>",
                f"<td>{_f(metrics.get('pct')):+.2f}%<small>同题材涨停{_i(metrics.get('theme_limit_count'))}只</small></td>",
                f"<td>{html.escape(item['recommendation']['text'])}</td>",
            ]) + "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='5'>本时点暂无同时通过真实催化、板块联动、双源行情与资金代理的动态标的；不以固定名称或未核验候选补位。</td></tr>")
    replacements = dynamic.get("replacements") or {}
    change_text = (
        f"新增：{'、'.join(replacements.get('added') or []) or '无'}；"
        f"退出：{'、'.join(replacements.get('dropped') or []) or '无'}"
    )
    return (
        "<section><h2>热点板块前三 · 动态策略候选</h2>"
        f"<div class='card'>每个时点自动重排，无固定标的；{html.escape(change_text)}。</div>"
        "<table><thead><tr><th>排名</th><th>标的</th><th>题材</th><th>量价/联动</th><th>条件式建议</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>{render_focus_note(dynamic.get('hot_sector_focus'))}</section>"
    )


def build_degraded_emotion_report(error: str, slot: str) -> Dict[str, Any]:
    return {
        "emotion_watchlist": [],
        "emotion_selection": {"prequalified": [], "admitted": [], "rejected": []},
        "emotion_alerts": [],
        "emotion_recommendations": [],
        "emotion_dynamic_targets": {
            "mode": "dynamic", "items": [], "displayed_count": 0, "vacant_count": 5,
            "replacements": {"added": [], "dropped": []},
            "methodology": "数据不足，不沿用固定标的或旧榜",
        },
        "emotion_data_quality": {
            "status": "error", "errors": [error], "missing_quote_codes": [],
            "unverified_candidate_count": 0, "actionable": False, "synthetic_data_used": False,
            "fund_flow_disclaimer": "数据不足，不执行仓位变化",
        },
        "emotion_summary": {"slot": slot, "red_count": 0, "orange_count": 0, "tracked_count": 0},
    }


def render_latest_alert_strip(report: Dict[str, Any]) -> str:
    summary = report["emotion_summary"]
    quality = report["emotion_data_quality"]
    red = [row for row in report["emotion_alerts"] if row["severity"] == "red"]
    if not quality.get("actionable"):
        focus = "数据不足，不执行仓位变化"
        klass, title = "has-red", "情绪监控数据不足"
    elif red:
        focus = "；".join(f"{row['name']}·{row['direction']}·{row['recommendation']['action']}" for row in red[:3])
        klass, title = "has-red", f"🔴 {summary['red_count']}个红色异动"
    else:
        focus = f"橙色{summary['orange_count']}个，跟踪{summary['tracked_count']}只"
        klass, title = "clear", "无红色异动"
    return (
        f"<section class='emotion-strip {klass}'><div><small>情绪监控 · {html.escape(summary['slot'].replace('_', ':'))}</small>"
        f"<b>{html.escape(title)}</b></div><span>{html.escape(focus)}</span></section>"
    )
