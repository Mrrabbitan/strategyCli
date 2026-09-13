"""Render a saved dragon-cycle research snapshot; never create trading signals."""

from __future__ import annotations

import datetime as dt
import html
import json
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
from research_store import research_path
SNAPSHOT_PATH = research_path("dragon/current.json")


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _link(source: dict) -> str:
    label = _escape(source.get("label", source.get("title", "来源")))
    url = str(source.get("url", ""))
    if urlsplit(url).scheme not in {"https", "http"}:
        return label
    return f'<a href="{_escape(url)}" target="_blank" rel="noopener noreferrer">{label}</a>'


def render_dragon_watchlist_section(
    path: Path | None = None, *, as_of: dt.date | None = None,
) -> str:
    """Display dated observations only, including after the snapshot has expired."""
    path = path if path is not None else research_path("dragon/current.json")
    if not path.exists():
        return '<section id="dragon-cycle-watchlist" class="notice">龙空龙当前观察名单缺失；等待有效研究，不沿用历史股票或其他专题名单。</section>'
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        target = dt.date.fromisoformat(data["target_date"])
        prepared = dt.date.fromisoformat(data["analysis_date"])
        today = as_of or dt.datetime.now(ZoneInfo("Asia/Shanghai")).date()
        if today < prepared:
            return ""
        if today >= dt.date(2026, 9, 8):
            from emotion_monitor import load_emotion_config
            from hot_sector_leaders import gate_reason
            context = data.get("hot_sector_focus")
            if not context or context.get("as_of") != data.get("quote_date") or any(
                gate_reason(str(row.get("code", "")), context, load_emotion_config())
                for row in data.get("selected", [])
            ):
                return '<section id="dragon-cycle-watchlist"><b>龙空龙 · 热点板块前三</b><p>旧研究快照未通过新版热点排名核验，停止展示候选；等待按新模型重算。</p></section>'
        items = data["selected"]
        if not isinstance(items, list) or len(items) > 5:
            raise ValueError("Invalid observation list")
        if data.get("actionable") is not False or any(
            x.get("actionable") is not False
            or not x.get("historical_screen_pass")
            or x["friday_volume_ratio"] < 1
            or x["expansion_count"] >= 2
            for x in items
        ):
            raise ValueError("Snapshot violates research-only admission rules")

        status = "观察窗口已结束，需重新筛选" if today > target else "目标交易日待验证；尚未满足建仓条件"
        count_note = "是否触发第二次放量取决于本轮已确认次数，不因查询或回封清零。"
        rows, audits = [], []
        for item in items:
            count = item["expansion_count"]
            trigger = item["monday_volume_trigger_shares"]
            rows.append(
                f'<tr><td><b>{_escape(item["name"])}</b><small>{_escape(item["code"])} · {item["boards"]}连板</small></td>'
                f'<td>{item["close"]:.2f}元<small>数据截止日收盘，不是建仓价</small></td>'
                f'<td>{item["friday_volume_ratio"]:.4f}倍<small>累计放量 {count} 次</small></td>'
                f'<td>{item["break_count"]}次开板<small>最后回封 {_escape(item["last_reseal"])}</small></td>'
                f'<td>{item["turnover_pct"]:.2f}%<small>数据截止日全天，非竞价换手</small></td>'
                f'<td>{trigger / 1e6:.4f}万手<small>{_escape(item["monday_trigger_result"])}</small></td>'
                f'<td class="dragon-reason">{_escape(item["rationale"])}<small>{_escape(item["risk_note"])}</small></td></tr>'
            )
            ledger = "".join(
                f'<tr><td>{_escape(r["date"])}</td><td>{r["volume_shares"]:,}</td>'
                f'<td>{r["previous_volume_shares"]:,}</td><td>{r["volume_ratio"]:.6f}</td>'
                f'<td>{"是" if r["expanded"] else "否"}</td><td>{r["expansion_count"]}</td></tr>'
                for r in item["history"]
            )
            lhb = "；".join(
                f'{_escape(r["period"])}净额 {r["net_cny"] / 1e8:+.4f}亿元'
                for r in item.get("lhb", [])
            ) or "本次汇总未查到记录；不等于没有资金交易"
            notices = " · ".join(_link(r) for r in item.get("announcement_review", []))
            audits.append(
                f'<details><summary>{_escape(item["name"])}：逐日量能与证据</summary>'
                f'<p>目标交易日放量判断：累计成交量达到 {trigger:,} 股。{_escape(item["monday_trigger_result"])}。</p>'
                f'<div class="table-scroll"><table><thead><tr><th>日期</th><th>成交股数</th><th>前日股数</th>'
                f'<th>倍数</th><th>≥1.2倍</th><th>累计次数</th></tr></thead><tbody>{ledger}</tbody></table></div>'
                f'<p>对应研究期间龙虎榜：{lhb}。不同期间不相加，不等同于具体游资身份。</p>'
                f'<p>{" · ".join(_link(s) for s in item["sources"])}</p>'
                f'<p>公告标题核查（正文及交易日状态待复核）：{notices or "未发现上述标题类别；不代表无风险"}</p></details>'
            )
        exclusions = "".join(
            f'<li>{_escape(x["name"])}（{_escape(x["code"])}）：'
            f'昨日量{x["friday_volume_ratio"]:.4f}倍，放量{x["expansion_count"]}次；'
            f'{_escape("；".join(x["exclusion_reasons"]))}。</li>'
            for x in data["excluded"]
        )
        alternates = "".join(
            f'<li>{_escape(x["name"])}：{_escape(x["not_selected_reason"])}。</li>'
            for x in data["alternates"]
        )
        checks = "".join(f'<li>{_escape(s)}</li>' for s in data["entry_checks"])
        limitations = "".join(f'<li>{_escape(s)}</li>' for s in data["data_quality"]["limitations"])
        market = data["market"]
        coverage = data["coverage"]
        return f'''<!-- dragon-cycle:start -->
<section id="dragon-cycle-watchlist" class="dragon" data-target-date="{target.isoformat()}">
<style>
.dragon{{margin-top:20px;padding:24px;border:1px solid #d5dfe9;border-radius:18px;background:#fff;color:#172033}}
.dragon h2{{font-size:23px;margin:8px 0 12px}}.dragon h3{{font-size:16px;margin:20px 0 10px}}
.dragon p,.dragon li{{font-size:14px;line-height:1.75}}.dragon .dragon-label{{color:#18365a;font-weight:700;font-size:12px}}
.dragon .dragon-status{{padding:12px 16px;background:#fff3d6;color:#805400;border-radius:10px;font-weight:700}}
.dragon table{{width:100%;border-collapse:collapse;font-size:13px}}.dragon td,.dragon th{{padding:12px;border-bottom:1px solid #e6e8ef;text-align:left}}
.dragon .dragon-reason{{min-width:260px;white-space:normal;line-height:1.6}}.dragon small{{display:block;color:#687085;font-size:12px;white-space:normal;margin-top:5px;line-height:1.5}}
.dragon details{{border-top:1px solid #e6e8ef;padding:12px 0}}.dragon summary{{cursor:pointer;font-weight:650}}
.dragon a{{color:#1b5d94}}.dragon .table-scroll{{overflow-x:auto}}.dragon .dragon-meta{{color:#687085;font-size:12px}}
@media(max-width:800px){{.dragon{{padding:16px}}.dragon h2{{font-size:20px}}}}
</style>
<span class="dragon-label">龙空龙 · 独立量价研究 · 目标交易日 {target.isoformat()}</span>
<h2>目标交易日观察名单 · {len(items)}/5</h2>
<p class="dragon-status" data-dragon-status>{status} · 已确认可建仓：0只</p>
<p>{_escape(market["summary"])}</p>
<p class="dragon-meta">本区分析日期 {_escape(data["analysis_date"])}；行情截至 {_escape(data["quote_date"])}收盘。
{_escape(data["snapshot_kind"])}。本区为注明日期的策略研究；独立资金异动见时点报告，不自动交易。</p>
<p>覆盖数据截止日涨停池{coverage["source_limit_pool"]}只，其中主板连板{coverage["mainboard_consecutive_candidates"]}只；
{coverage["historical_screen_pass"]}只通过昨日不缩量及放量次数筛选，从中选择以下{len(items)}只待验证观察。
表格顺序不是当日竞价强弱排名，不是保证收益的买入清单。</p>
<div class="table-scroll"><table><thead><tr><th>股票</th><th>历史价格</th><th>昨日量能</th><th>历史回封</th><th>全天换手</th><th>当日累计量门槛</th><th>理由与风险</th></tr></thead>
<tbody>{''.join(rows) or '<tr><td colspan="7">暂无合格观察候选，不补位。</td></tr>'}</tbody></table></div>
<p><b>{count_note}</b>
表中万手数仅为显示值，精确股数见下方证据。第二次放量之后不能因为回封而恢复资格。</p>
<h3>当日核验顺序</h3><ol>{checks}</ol>
<p>目标为一至三个涨停，不是必须等到的卖出条件；盈利退出后等待新节点。未提供持仓成本和买入日期，本区不判断个人盈亏或可卖数量。</p>
<details><summary>市场证据与数据局限</summary>
<p>本次数据截止日主板涨停{market["sealed_mainboard"]}只、触板未封{market["failed_mainboard"]}只；
按“未封÷全部触板”计算炸板比例{market["failed_ratio"]:.2%}。{_escape(market["breadth"])} {_escape(market["promotion"])}</p>
<p>{_escape(data["data_quality"]["historical_selection"])}</p><ul>{limitations}</ul>
<p>{' · '.join(_link(s) for s in data['sources'])}</p></details>
{''.join(audits)}
<details><summary>为什么没选其他连板股：{len(data["excluded"])}只排除、{len(data["alternates"])}只排序靠后</summary><ul>{exclusions}{alternates}</ul></details>
<p class="dragon-meta">事实：已核验的历史量价；推断：观察优先级；策略假设：1.2倍与第二次放量退出。
本名单仅针对{target.isoformat()}，过期必须重新筛选，不能沿用为下一交易日建仓信号。</p>
</section>
<script>(function(){{const root=document.getElementById('dragon-cycle-watchlist');
const day=new Intl.DateTimeFormat('sv-SE',{{timeZone:'Asia/Shanghai'}}).format(new Date());
if(root&&day>root.dataset.targetDate)root.querySelector('[data-dragon-status]').textContent='观察窗口已结束，需重新筛选 · 旧名单无建仓资格';
}})();</script>
<!-- dragon-cycle:end -->'''
    except (OSError, ValueError, KeyError, TypeError):
        return '<section id="dragon-cycle-watchlist" class="near-strip near-error"><b>龙空龙观察数据不可用</b><span>停止展示名单；未产生建仓信号。</span></section>'
