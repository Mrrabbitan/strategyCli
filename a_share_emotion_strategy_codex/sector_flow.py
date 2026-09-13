"""Sector-level capital-flow collection and report rendering.

The module keeps each source separate.  In particular, turnover and price
changes are never substituted for a net fund-flow figure.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from market_data import fetch_sector_etf_market
from research_store import private_path, research_path


ROOT = Path(__file__).resolve().parent
EASTMONEY_SECTOR_FLOW_URL = "https://push2.eastmoney.com/api/qt/clist/get"
EASTMONEY_FUND_PROFILE = "https://fundf10.eastmoney.com/jbgk_{code}.html"
HKEX_NORTHBOUND_URL = "https://www.hkex.com.hk/Market-Data/Securities-Prices/Equities/China-Connect-Market/" \
    "Shanghai-Connect?sc_lang=en"
ETF_SECTOR_MAP = {
    "bank": "银行", "consumer": "消费", "semiconductor": "半导体", "medical": "医药",
}


class SectorFlowError(RuntimeError):
    pass


def _request(url: str, params: Optional[Dict[str, Any]] = None, timeout: int = 15) -> str:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
    error: Optional[Exception] = None
    for attempt in range(3):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout).read().decode("utf-8", "replace")
        except Exception as exc:
            error = exc
            if attempt < 2:
                time.sleep(0.6 * (attempt + 1))
    raise SectorFlowError(f"request failed: {url}: {error}")


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _history_dir() -> Path:
    return private_path("cache/sector_flow")


def _main_snapshot_path(day: dt.date) -> Path:
    return _history_dir() / f"main-{day.isoformat()}.json"


def fetch_main_sector_flow() -> Dict[str, Any]:
    """Fetch Eastmoney's industry-board main net-flow table.

    f62 is the provider's documented/main-board net inflow field.  We retain
    its provider label rather than recasting it as institutional holdings.
    """
    params = {
        "pn": 1, "pz": 500, "po": 1, "np": 1, "fltt": 2, "invt": 2,
        "fid": "f62", "fs": "m:90+t:2+f:!50",
        "fields": "f12,f14,f2,f3,f6,f62,f184",
    }
    try:
        payload = json.loads(_request(EASTMONEY_SECTOR_FLOW_URL, params))
        rows = ((payload.get("data") or {}).get("diff") or [])
        if not rows:
            raise SectorFlowError("provider returned no industry rows")
        now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        return {
            "source": "东方财富行业板块主力资金",
            "source_url": EASTMONEY_SECTOR_FLOW_URL,
            "available": True,
            "updated_at": now,
            "limitations": "主力净流为数据提供商口径，不代表全部机构资金或真实账户持仓。",
            "rows": [{
                "sector": str(row.get("f14") or "未知行业"),
                "code": str(row.get("f12") or ""),
                "main_net_inflow_cny": _float(row.get("f62")),
                "main_net_inflow_ratio_pct": _float(row.get("f184")),
                "pct": _float(row.get("f3")),
                "amount_cny": _float(row.get("f6")),
            } for row in rows],
            "error": "",
        }
    except Exception as exc:
        return {
            "source": "东方财富行业板块主力资金", "source_url": EASTMONEY_SECTOR_FLOW_URL,
            "available": False, "updated_at": "", "limitations": "数据缺失时不以成交额或涨跌幅替代净流入。",
            "rows": [], "error": str(exc),
        }


def _save_main_snapshot(day: dt.date, report: Dict[str, Any]) -> None:
    if not report["available"]:
        return
    path = _main_snapshot_path(day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"date": day.isoformat(), "rows": report["rows"]}, ensure_ascii=False), encoding="utf-8")


def _main_five_day(day: dt.date, current: Dict[str, Any], persist_state: bool) -> Tuple[Dict[str, float], List[str]]:
    if persist_state:
        _save_main_snapshot(day, current)
    samples: List[Tuple[str, List[Dict[str, Any]]]] = []
    for path in sorted(_history_dir().glob("main-*.json"), reverse=True):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            stamp = str(item.get("date", ""))
            if stamp <= day.isoformat() and isinstance(item.get("rows"), list):
                samples.append((stamp, item["rows"]))
            if len(samples) == 5:
                break
        except (OSError, json.JSONDecodeError):
            continue
    totals: Dict[str, float] = {}
    for _, rows in samples:
        for row in rows:
            sector = str(row.get("sector", "未知行业"))
            totals[sector] = totals.get(sector, 0.0) + _float(row.get("main_net_inflow_cny"))
    return totals, sorted(stamp for stamp, _ in samples)


def _parse_fund_shares(code: str) -> Tuple[float, str]:
    source = EASTMONEY_FUND_PROFILE.format(code=code)
    text = _request(source)
    plain = re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", text))
    match = re.search(r"份额规模.*?([0-9.]+)亿份", plain)
    if not match:
        raise SectorFlowError(f"{code} ETF share scale missing")
    return float(match.group(1)) * 1e8, source


def _etf_snapshot_path(day: dt.date) -> Path:
    return _history_dir() / f"etf-{day.isoformat()}.json"


def _load_etf_snapshots(day: dt.date) -> List[Dict[str, Any]]:
    rows = []
    for path in sorted(_history_dir().glob("etf-*.json"), reverse=True):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            if str(row.get("date", "")) <= day.isoformat():
                rows.append(row)
            if len(rows) == 5:
                break
        except (OSError, json.JSONDecodeError):
            continue
    return rows


def build_etf_flow(day: dt.date, etf_market: Optional[Dict[str, Any]], persist_state: bool) -> Dict[str, Any]:
    market = fetch_sector_etf_market(day) if etf_market is None else etf_market
    universe = market.get("universe") or {}
    quotes = {str(code): row.get("quote") or {} for code, row in universe.items()}
    rows, errors = [], list((market.get("data_quality") or {}).get("errors") or [])
    for code, item in universe.items():
        sector = ETF_SECTOR_MAP.get(str(item.get("group", "")))
        if not sector:
            continue
        try:
            shares, source = _parse_fund_shares(str(code))
            price = _float(quotes.get(str(code), {}).get("price"))
            if price <= 0:
                raise SectorFlowError(f"{code} quote missing")
            rows.append({"code": str(code), "sector": sector, "shares": shares, "price": price, "source_url": source})
        except Exception as exc:
            errors.append(str(exc))
    snapshot = {"date": day.isoformat(), "rows": rows}
    if persist_state and rows:
        path = _etf_snapshot_path(day)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    history = _load_etf_snapshots(day)
    prior = history[1] if len(history) > 1 else None
    five_day_prior = history[-1] if len(history) >= 5 else None

    def aggregate(reference: Optional[Dict[str, Any]]) -> Dict[str, float]:
        if not reference:
            return {}
        previous = {str(row["code"]): row for row in reference.get("rows", [])}
        totals: Dict[str, float] = {}
        for row in rows:
            old = previous.get(row["code"])
            if old:
                delta = (row["shares"] - _float(old.get("shares"))) * row["price"]
                totals[row["sector"]] = totals.get(row["sector"], 0.0) + delta
        return totals

    return {
        "source": "天天基金ETF份额规模", "source_url": "https://fundf10.eastmoney.com/",
        "available": bool(rows) and prior is not None,
        "quote_as_of": market.get("as_of"),
        "updated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds") if rows else "",
        "limitations": "按ETF份额变化×现价估算申赎资金；份额规模为数据源披露频率，非盘中逐笔资金流。",
        "one_day_cny": aggregate(prior), "five_day_cny": aggregate(five_day_prior),
        "observed_dates": [str(item.get("date")) for item in history], "errors": errors,
    }


def build_northbound_flow(day: dt.date) -> Dict[str, Any]:
    """Read an auditable HKEX-derived sector snapshot when one is supplied.

    HKEX does not provide a stable public intraday industry-flow API.  The
    importer intentionally requires the original official URL and date rather
    than silently substituting a commercial estimate.
    """
    path = research_path("sector_flow/northbound_holdings.json")
    base = {
        "source": "港交所互联互通持仓", "source_url": HKEX_NORTHBOUND_URL,
        "available": False, "updated_at": "", "as_of": "", "one_day_cny": {}, "five_day_cny": {},
        "limitations": "港交所持仓为日频/延迟数据，不是盘中北向净买入；缺少可审计官方文件时不显示估算值。", "error": "",
    }
    if not path.exists():
        base["error"] = "未导入港交所官方行业持仓文件"
        return base
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        as_of = dt.date.fromisoformat(str(data["as_of"]))
        if as_of > day or not str(data.get("official_url", "")).startswith("https://"):
            raise SectorFlowError("官方文件日期或URL无效")
        for field in ("one_day_cny", "five_day_cny"):
            if not isinstance(data.get(field), dict):
                raise SectorFlowError(f"{field} missing")
        return {**base, "available": True, "updated_at": str(data.get("updated_at") or as_of.isoformat()),
                "as_of": as_of.isoformat(), "source_url": str(data["official_url"]),
                "one_day_cny": data["one_day_cny"], "five_day_cny": data["five_day_cny"]}
    except Exception as exc:
        base["error"] = str(exc)
        return base


def build_sector_flow(day: dt.date, slot: str, etf_market: Optional[Dict[str, Any]] = None, *, persist_state: bool = True) -> Dict[str, Any]:
    main = fetch_main_sector_flow()
    main_five, dates = _main_five_day(day, main, persist_state)
    etf = build_etf_flow(day, etf_market, persist_state)
    northbound = build_northbound_flow(day)
    by_sector: Dict[str, Dict[str, Any]] = {}
    for row in main["rows"]:
        by_sector[row["sector"]] = {**row, "main_five_day_cny": main_five.get(row["sector"]),
                                    "etf_one_day_cny": etf["one_day_cny"].get(row["sector"]),
                                    "etf_five_day_cny": etf["five_day_cny"].get(row["sector"]),
                                    "northbound_one_day_cny": northbound["one_day_cny"].get(row["sector"]),
                                    "northbound_five_day_cny": northbound["five_day_cny"].get(row["sector"])}
    ordered = sorted(by_sector.values(), key=lambda row: row["main_net_inflow_cny"], reverse=True)
    sustained_in = [row["sector"] for row in ordered if (row.get("main_five_day_cny") or 0) > 0 and row["main_net_inflow_cny"] > 0][:3]
    sustained_out = [row["sector"] for row in reversed(ordered) if (row.get("main_five_day_cny") or 0) < 0 and row["main_net_inflow_cny"] < 0][:3]
    return {
        "slot": slot, "as_of": day.isoformat(), "sector_rows": ordered,
        "sources": {"main": main, "etf": etf, "northbound": northbound},
        "windows": {"main_five_day_observed_dates": dates, "etf_observed_dates": etf["observed_dates"]},
        "summary": {"sustained_inflows": sustained_in, "sustained_outflows": sustained_out},
    }


def _money(value: Any) -> str:
    if value is None:
        return "—"
    return f"{_float(value) / 1e8:+.2f}亿"


def render_sector_flow_section(report: Dict[str, Any]) -> str:
    sources = report["sources"]
    rows = report["sector_rows"][:12]
    main = sources["main"]
    source_text = "；".join(
        f"{html.escape(item['source'])}：{'可用' if item['available'] else '不可用'}"
        for item in sources.values()
    )
    links = []
    for key, item in sources.items():
        url = html.escape(str(item.get("source_url", "")), quote=True)
        links.append(f'<a href="{url}" target="_blank" rel="noreferrer">{html.escape(item["source"])}</a>')
    etf_date = sources["etf"].get("quote_as_of")
    etf_date_note = (
        f"ETF估算基准价截至 {html.escape(str(etf_date))}，不代表份额披露日期或盘中资金成交时间。"
        if etf_date else "ETF估算基准价日期待核验。"
    )
    svg_rows = []
    for index, row in enumerate(rows):
        y = 50 + index * 36
        amount = _float(row["main_net_inflow_cny"])
        color = "#d84b35" if amount >= 0 else "#14705b"
        width = min(16, 2 + abs(amount) / 1e8 / 2)
        direction = "流入" if amount >= 0 else "流出"
        svg_rows.append(
            f'<path d="M150 {y} C270 {y}, 310 {y}, 430 {y}" fill="none" stroke="{color}" stroke-width="{width:.1f}" opacity=".72"/>'
            f'<text x="444" y="{y + 4}" font-size="12">{html.escape(row["sector"])} {direction} {_money(amount)}</text>'
        )
    table_rows = "".join(
        "<tr>" + "".join([
            f"<td>{rank}</td><td><b>{html.escape(row['sector'])}</b></td>",
            f"<td class={'inflow' if row['main_net_inflow_cny'] >= 0 else 'outflow'}>{_money(row['main_net_inflow_cny'])}</td>",
            f"<td>{_money(row.get('main_five_day_cny'))}</td><td>{_money(row.get('etf_one_day_cny'))}</td>",
            f"<td>{_money(row.get('northbound_one_day_cny'))}</td><td>{row['pct']:+.2f}%</td><td>{_money(row['amount_cny'])}</td>",
        ]) + "</tr>"
        for rank, row in enumerate(rows, 1)
    ) or '<tr><td colspan="8">行业主力资金源不可用；未以成交额替代净流数据。</td></tr>'
    return f'''<style>
.sector-flow{{margin:22px 0}}.sector-flow .flow-note,.sector-flow .flow-summary,.sector-flow .flow-source{{font-size:12px;color:#667085;line-height:1.65}}.sector-flow .flow-chart{{border:1px solid #e5e7eb;background:#fff;margin:12px 0;overflow:auto}}.sector-flow svg{{display:block;min-width:680px;width:100%;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;color:#1d2433}}.sector-flow .inflow{{color:#b33525;font-weight:700}}.sector-flow .outflow{{color:#14705b;font-weight:700}}.sector-flow .flow-source a{{color:#315d94}}
</style><section class="sector-flow"><h2>资金流向</h2>
<div class="flow-note">盘中主口径：行业主力净流；近5日仅累计已获取交易日快照。{source_text}</div>
<div class="flow-chart" role="img" aria-label="行业主力资金流向图"><svg viewBox="0 0 760 {max(130, 72 + len(rows) * 36)}" preserveAspectRatio="xMinYMin meet"><text x="14" y="28" font-size="13" font-weight="700">行业主力资金</text><text x="150" y="28" font-size="12" fill="#687085">红=净流入　绿=净流出</text>{''.join(svg_rows)}</svg></div>
<div class="table-scroll"><table><thead><tr><th>#</th><th>行业</th><th>主力当日</th><th>主力5日</th><th>ETF份额当日</th><th>北向持仓当日</th><th>涨跌</th><th>成交额</th></tr></thead><tbody>{table_rows}</tbody></table></div>
<p class="flow-summary">持续流入：{html.escape('、'.join(report['summary']['sustained_inflows']) or '暂无足够5日样本')}；持续流出：{html.escape('、'.join(report['summary']['sustained_outflows']) or '暂无足够5日样本')}。ETF份额及北向持仓为低频/延迟口径，未显示时即为数据不可用。{etf_date_note}</p>
<p class="flow-source">来源：{'；'.join(links)}。主力资金不等同机构账户；ETF为份额变化估算；北向为官方持仓变化，均不构成交易指令。</p></section>'''
