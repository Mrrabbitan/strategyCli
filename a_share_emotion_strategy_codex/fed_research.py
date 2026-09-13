# -*- coding: utf-8 -*-
"""Dated, read-only research; never promotes stocks into a trading strategy."""
from __future__ import annotations

import datetime as dt
import html
import json
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
from research_store import research_path
SNAPSHOT = research_path('fed/current.json')
ASSET_PREFIX = 'assets/fed-research'


def e(value):
    return html.escape(str(value), quote=True)


def signed(value, suffix=''):
    if value is None:
        return '待验证'
    tone = 'fed-positive' if value > 0 else 'fed-negative' if value < 0 else ''
    return f'<span class="{tone}">{value:+.2f}{e(suffix)}</span>'


def links(sources):
    return ' · '.join(f'<a href="{e(s["url"])}" target="_blank" rel="noopener noreferrer">{e(s["label"])} ↗</a>'
                      for s in sources if urlsplit(s['url']).scheme in ('https', 'http'))


def render_fed_research(path: Path | None = None) -> str:
    path = path if path is not None else research_path("fed/current.json")
    if not path.exists():
        return ''
    try:
        d = json.loads(path.read_text(encoding='utf-8'))
        assert d['schema_version'] == 1
        assert dt.date.fromisoformat(d['asof']) <= dt.date.fromisoformat(d['research_date'])
        rows = d['stocks']
        assert 0 < len(rows) <= 100 and len({r['code'] for r in rows}) == len(rows)
        assert [r['rank'] for r in rows] == list(range(1, len(rows) + 1))
    except (OSError, ValueError, KeyError, TypeError, AssertionError):
        return '<section id="fed-watchlist-research"><p>议息专题快照不可用，停止展示观察排序；没有产生买入信号。</p></section>'

    prices = ''.join(f'<tr><th scope="row">{e(r["name"])}<small>{e(r["code"])}</small></th>'
                     f'<td>{r["close"]:.2f}</td><td>{signed(r["returns"]["5"], "%")}</td>'
                     f'<td>{signed(r["returns"]["20"], "%")}</td><td>{signed(r["returns"]["60"], "%")}</td>'
                     f'<td>{r["volume_ratio"]:.2f}倍</td><td>{r["turnover_pct"]:.2f}%</td>'
                     f'<td class="fed-description">{e(r["trend"])}</td></tr>' for r in rows)
    funds = ''.join(f'<tr><th scope="row">{e(r["name"])}</th><td>{signed(r["flow"]["1"]["net_yi"])}</td>'
                    f'<td>{signed(r["flow"]["1"]["net_share_pct"], "%")}</td><td>{signed(r["flow"]["5"]["net_yi"])}</td>'
                    f'<td>{signed(r["flow"]["20"]["net_yi"])}</td><td>{links([r["sources"][1]])}</td></tr>' for r in rows)
    cards = ''
    for r in rows:
        cards += f'''<details class="fed-stock" id="fed-stock-{e(r['code'])}">
<summary><span class="fed-rank">{r['rank']:02d}</span><span class="fed-stock-name"><b>{e(r['name'])}</b><small>{e(r['code'])} · {e(r['status'])}</small></span><span class="fed-price">{r['close']:.2f}<small>收盘 / 元</small></span></summary>
<div class="fed-stock-body"><p class="fed-label">判断与反证</p><p>{e(r['analysis'])}</p>
<dl><div><dt>压力观察 / 元</dt><dd>{e(r['pressure'])}</dd></div><div><dt>支撑观察 / 元</dt><dd>{e(r['support'])}</dd></div></dl>
<p>{e(r.get('fundamental_note',''))}</p><p><b>确认条件：</b>{e(r['confirmation'])}</p><p><b>失效或等待：</b>{e(r['invalidation'])}</p>
<p class="fed-muted">当日量 {r['volume_wanshou']:.2f}万手；较此前5日均量 {r['volume_previous5_mean']:.2f}倍；成交额 {r['amount_yi']:.2f}亿元。这里的历史均量仅用于描述，不增加龙空龙规则。</p>
<p class="fed-sources">{links(r['sources'])}</p></div></details>'''
    comparison = d.get('fundamental_comparison') or {}
    comparison_rows = ''.join(f'<tr><th>{e(x["name"])}</th><td>{x["revenue"]:.2f}</td><td>{x["recurring_profit"]:.2f}</td><td>{x["cfo"]:.2f}</td><td>{x["capex_cash"]:.2f}</td><td>{x["debt_asset_pct"]:.2f}%</td></tr>' for x in comparison.get('rows', []))
    comparison_html = f'<h3>盈利与现金流对照</h3><p>{e(comparison.get("period", ""))} · {e(comparison.get("units", ""))}</p><div class="fed-table-scroll"><table><thead><tr><th>股票</th><th>营收</th><th>扣非利润</th><th>经营现金流</th><th>资本开支现金</th><th>负债率</th></tr></thead><tbody>{comparison_rows}</tbody></table></div><p class="fed-muted">{e(comparison.get("note", ""))}</p>' if comparison_rows else ''
    counts = ''.join(f'<tr><th scope="row">{e(c["date"])}</th><td>{c["volume"]:.4f}</td><td>{c["previous"]:.4f}</td><td>{c["ratio"]:.2f}</td><td>{c["count"]}{"：退出预警，取消新增" if c["count"] >= 2 else ""}</td></tr>' for c in d['dragon_counts'])
    charts = ''.join(f'<figure><a href="{ASSET_PREFIX}/{e(c["file"])}" target="_blank" rel="noopener"><img src="{ASSET_PREFIX}/{e(c["file"])}" alt="{e(c["label"])}：截至{e(d["asof"])}的历史价格、量能或资金图" width="{int(c.get("width",2250))}" height="{int(c.get("height",2100))}" loading="lazy"></a><figcaption>{e(c["label"])} · 点击查看原图</figcaption></figure>' for c in d['charts'])
    top_cards = ''.join(f'<a href="#fed-stock-{e(c["code"])}"><small>{e(c["kicker"])}</small><b>{e(c["name"])}</b><span>{e(c["note"])}</span></a>' for c in d.get('top_cards', []))
    actions = ''.join(f'<article><h4>{e(a["stage"])}</h4><p>{e(a["action"])}</p></article>' for a in d['actions'])
    limitations = ''.join(f'<li>{e(x)}</li>' for x in d['limitations'])
    style = (ROOT / 'templates/fed_research.css').read_text(encoding='utf-8')
    return f'''<section id="fed-watchlist-research" class="fed-research" aria-labelledby="fed-title" data-asof="{e(d['asof'])}" data-research-date="{e(d['research_date'])}">
<style>{style}</style><div class="fed-heading"><div><span class="fed-label">议息专题 / 量价与资金复核</span><h2 id="fed-title">{e(d.get('title', '观察顺序与条件'))}</h2></div><span class="fed-date">研究 {e(d['research_date'])}<br>行情截至 {e(d['asof'])} 收盘</span></div>
<p class="fed-lead">{e(d['conclusion'])}</p><div class="fed-warning"><b>静态研究 · 非实时买入信号</b><p>未来1—3个月，均衡、允许等待。非交易日使用最近完整交易日复盘；刷新页面不会更新行情。每次建仓前须重新核验。</p></div>
<div class="fed-top-three">{top_cards}</div>
<p class="fed-muted">{e(d['ranking_note'])}</p><p>{e(d['revision'])}</p>
{comparison_html}<h3>价格、趋势与成交量</h3><p class="fed-muted">{e(d.get('price_note', '日量倍数为当日全天量除以前日全天量；不是软件盘中量比。'))}</p>
<div class="fed-table-scroll" tabindex="0" role="region" aria-label="股票价格与量能，可横向滚动"><table><caption>收盘价格单位：元；涨跌幅按交易日计算</caption><thead><tr><th>股票 / 代码</th><th>收盘</th><th>5日</th><th>20日</th><th>60日</th><th>日量倍数</th><th>当日换手</th><th>K线结构</th></tr></thead><tbody>{prices}</tbody></table></div>
<details class="fed-block" open><summary>同口径大单资金：看持续性与背离</summary><div class="fed-block-body"><p>{e(d['flow_definition'])}</p><p class="fed-muted">{e(d['flow_windows'])}</p><div class="fed-table-scroll" tabindex="0" role="region" aria-label="同花顺大单资金对照"><table><caption>净额单位：亿元；正为净流入、负为净流出</caption><thead><tr><th>股票</th><th>当日净额</th><th>当日净占比</th><th>5日净额</th><th>20日净额</th><th>来源</th></tr></thead><tbody>{funds}</tbody></table></div><p>{e(d.get('flow_commentary', '资金统计只作辅助风险背景，不代表账户身份。'))}</p></div></details>
<h3>观察顺序与逐股条件</h3><p class="fed-muted">点击股票展开依据、压力支撑、确认和失效条件。价位是历史观察区，不保证支撑或压力有效。</p><div class="fed-stock-list">{cards}</div>
<details class="fed-block"><summary>{e(d.get('chart_note', '查看研究图表'))}</summary><div class="fed-chart-grid">{charts}</div></details>
<h3>议息前后如何行动</h3><p>{e(d['market'])}</p><p>{e(d['macro'])}</p><div class="fed-actions">{actions}</div>
<div class="fed-exit"><h3>{e(d.get('dragon_title', '对应研究时点的龙空龙核验'))}</h3><p>{e(d['dragon_note'])}</p><h4>{e(d.get('count_title', '按日放量计数'))}</h4><div class="fed-table-scroll" tabindex="0" role="region" aria-label="研究时点逐日放量次数"><table><caption>成交量单位：万手；用户研究默认阈值1.2倍</caption><thead><tr><th>日期</th><th>当日量</th><th>前日量</th><th>倍数</th><th>累计次数 / 状态</th></tr></thead><tbody>{counts}</tbody></table></div><p>{e(d.get('count_note', '历史确认不倒推盘中成交；不同连板轮次不混计。'))}</p></div>
<details class="fed-block"><summary>数据范围、来源与缺失项</summary><div class="fed-block-body"><p>{e(d['scope'])}</p><ul>{limitations}</ul><p><b>事实：</b>已取得的价格、成交量、资金分类及公开公告。<b>推断：</b>趋势、观察顺序、支撑压力有效性。<b>策略假设：</b>1.2倍放量阈值与条件式行动，不是经过收益回测验证的模型。</p><p class="fed-sources">{links(d['sources'])}</p></div></details>
<p class="fed-muted">{e(d.get('scope_note', '专题排序独立于龙空龙资格，不自动新增监控标的。'))}</p></section>'''
