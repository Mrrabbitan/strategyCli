"""Read-only Eastmoney historical sector-flow views.

The supplied current catalogue is a frozen research universe, not a reconstruction
of every historical date's industry universe. No collection or ranking occurs here.
"""
from __future__ import annotations

import datetime as dt
import re

from etf_pressure_views import _amount, _clock, _date, _e, _finite, _link, _now, _num
from research_store import read_json, research_path


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _ranking(value, direction):
    """Validate the already-ranked supplier output, never sort or replenish it."""
    if not isinstance(value, list) or len(value) > 10:
        return None
    seen, previous, rows = set(), None, []
    for index, row in enumerate(value):
        if not isinstance(row, dict):
            return None
        code, net, rank = row.get('code'), row.get('main_net_yuan'), row.get('rank')
        if (not isinstance(code, str) or not re.fullmatch(r'BK\d+', code) or code in seen
                or not isinstance(row.get('name'), str) or not row['name']
                or not isinstance(rank, int) or isinstance(rank, bool) or rank != index + 1
                or not _finite(net) or (net <= 0 if direction == 'inflows' else net >= 0)
                or (previous is not None and (net > previous if direction == 'inflows' else net < previous))):
            return None
        seen.add(code); previous = net; rows.append(row)
    return rows


def _complete_day(day, catalog, clock_valid, now):
    date = _date(day.get('date')) if isinstance(day, dict) else None
    if not date or not clock_valid or now < dt.datetime.combine(date, dt.time(15), now.tzinfo):
        return False
    expected, verified, size = (_count(day.get('expected_sectors')), _count(day.get('verified_sectors')),
                                 _count(catalog.get('count')))
    if (day.get('status') != 'complete' or catalog.get('complete') is not True
            or not size or expected != size or verified != expected):
        return False
    inflows, outflows = _ranking(day.get('inflows'), 'inflows'), _ranking(day.get('outflows'), 'outflows')
    return (inflows is not None and outflows is not None
            and not ({row['code'] for row in inflows} & {row['code'] for row in outflows}))


def _table(rows, direction, edition, *, limit=10):
    if not rows:
        sign = '正' if direction == 'inflows' else '负'
        return f'<p class="etfm-empty">已核验的冻结池内没有{sign}主力净额行业，不补其他方向或零值。</p>'
    body = []
    for row in rows[:limit]:
        body.append(f'<tr><td>{row["rank"]}</td><td class="etfm-identity">'
                    f'<details id="etf-flow-{edition}-{direction}-{row["code"]}" class="etfm-detail">'
                    f'<summary><b>{_e(row["name"])}</b><span class="etfm-code">{row["code"]}</span></summary>'
                    '<div class="etfm-detail-body"><dl class="etfm-metrics"><div><dt>大单净额</dt>'
                    f'<dd>{_amount(row.get("large_net_yuan"))}</dd></div><div><dt>超大单净额</dt>'
                    f'<dd>{_amount(row.get("superlarge_net_yuan"))}</dd></div></dl>'
                    '<p>供应商按订单大小分类，不能据此识别真实机构或游资账户。'
                    '<a href="#etf-flow-history-sources">查看历史接口与单位来源</a></p></div></details></td>'
                    f'<td class="{"etfm-up" if direction == "inflows" else "etfm-down"}">{_amount(row["main_net_yuan"])}</td></tr>')
    return ('<div class="etfm-table-wrap"><table class="etfm-table"><thead><tr>'
            '<th scope="col">名次</th><th scope="col">东财行业</th><th scope="col">模型主力净额</th>'
            '</tr></thead><tbody>' + ''.join(body) + '</tbody></table></div>')


def _day_panel(date, day, catalog, clock_valid, now, selected):
    complete = _complete_day(day, catalog, clock_valid, now)
    expected = _count(day.get('expected_sectors')) if isinstance(day, dict) else None
    verified = _count(day.get('verified_sectors')) if isinstance(day, dict) else None
    if complete:
        state = '完整冻结池 · 正式前十'
        content = ('<div class="etfm-duo"><div class="etfm-panel"><h3>东财模型主力净流入前十</h3>'
                   + _table(day['inflows'], 'inflows', date) + '</div>'
                   '<div class="etfm-panel"><h3>东财模型主力净流出前十</h3>'
                   + _table(day['outflows'], 'outflows', date) + '</div></div>')
    else:
        state = '资料不足 · 无正式榜'
        if not day:
            reason = '该声明交易日没有有效日记录；不以0、相邻日期或旧榜填补。'
        elif _date(date) > now.date() or now < dt.datetime.combine(_date(date), dt.time(15), now.tzinfo):
            reason = '该日期尚未完成收盘，不展示正式历史榜。'
        elif not clock_valid:
            reason = '目录或报告时钟不一致、缺失或未来；本轮不展示正式历史榜。'
        elif catalog.get('complete') is not True:
            reason = '当前冻结行业目录不完整，不能把局部记录称为全池前十。'
        else:
            reason = '该日覆盖或榜单字段未完整核验，不能形成正式全池前十；未知不按0处理。'
        content = ('<div class="etfm-duo"><div class="etfm-panel"><h3>东财模型主力净流入前十</h3>'
                   f'<p class="etfm-empty">{reason}</p></div><div class="etfm-panel"><h3>东财模型主力净流出前十</h3>'
                   f'<p class="etfm-empty">{reason}</p></div></div>')
    hidden = '' if selected else ' hidden'
    return (f'<section id="etf-flow-day-{date}" data-flow-day="{date}"{hidden}>'
            '<header class="etfm-section-heading"><div>'
            f'<h3>{date} · {_e(state)}</h3><p class="etfm-note">行业核验 {_num(verified, 0)} / {_num(expected, 0)}；'
            '固定当前目录回溯，非当时全市场目录。</p></div></header>' + content + '</section>')


def _cumulative(report, catalog, complete_window, clock_valid, now):
    summary = report.get('summary')
    summary = summary if isinstance(summary, dict) else {}
    cumulative = summary.get('cumulative')
    cumulative = cumulative if isinstance(cumulative, dict) else {}
    proxy = dict(cumulative, date=report.get('window_end'))
    valid = (complete_window and summary.get('complete_window') is True
             and _complete_day(proxy, catalog, clock_valid, now))
    if not valid:
        return '<p class="etfm-note">20日累计榜：尚未完成完整窗口及冻结全池核验，不自行补数或累计局部行业。</p>'
    return ('<details id="etf-flow-cumulative" class="etfm-sources"><summary>20日累计 · 完整冻结池前三</summary>'
            '<p class="etfm-note">仅展示后端已核验的20交易日累计结果，不能将正净额理解为持有资金增加或机构持仓。</p>'
            '<div class="etfm-duo"><div class="etfm-panel"><h3>累计净流入前三</h3>'
            + _table(cumulative['inflows'], 'inflows', 'cumulative', limit=3) + '</div>'
            '<div class="etfm-panel"><h3>累计净流出前三</h3>'
            + _table(cumulative['outflows'], 'outflows', 'cumulative', limit=3) + '</div></div></details>')


def render(report=None, now=None):
    """Render an offline snapshot only. Date selection is bound by the parent UI."""
    now = _now(now)
    report = read_json(research_path('etf_flow_history/current.json'), {}) if report is None else report
    base = '<section id="etf-em-history" class="etfm etfm-section" data-flow-history'
    if (not isinstance(report, dict) or report.get('schema_version') != 1
            or isinstance(report.get('schema_version'), bool) or report.get('provider') != 'eastmoney'
            or report.get('metric') != '东财模型主力净额' or report.get('unit') != 'CNY_yuan'):
        return base + ('><h2>东财模型主力 · 20日历史榜</h2><p class="etfm-empty">'
                       '历史资金数据不可用，等待可核验快照；缺失不表示资金为0。</p></section>')
    catalog = report.get('catalog')
    catalog = catalog if isinstance(catalog, dict) else {}
    cutoff, start, end = (_date(report.get(key)) for key in ('cutoff', 'window_start', 'window_end'))
    generated, retrieved = _clock(report.get('generated_at')), _clock(report.get('retrieved_at'))
    catalog_day = _date(catalog.get('as_of'))
    clock_valid = (cutoff is not None and start is not None and end is not None and start <= end == cutoff
                   and generated is not None and retrieved is not None and retrieved <= generated <= now
                   and now >= dt.datetime.combine(cutoff, dt.time(15), now.tzinfo)
                   and retrieved >= dt.datetime.combine(cutoff, dt.time(15), now.tzinfo)
                   and catalog_day is not None and catalog_day <= now.date())
    declared = report.get('trading_dates20')
    declared = declared if isinstance(declared, list) else []
    dates = sorted({value for value in declared if _date(value) is not None}, reverse=True)
    window_consistent = (len(dates) == 20 and len(declared) == 20 and start is not None and end is not None
                         and dates[0] == end.isoformat() and dates[-1] == start.isoformat())
    days, duplicate = {}, set()
    for day in report.get('days', []) if isinstance(report.get('days'), list) else []:
        if not isinstance(day, dict) or _date(day.get('date')) is None:
            continue
        date = day['date']
        if date in days:
            duplicate.add(date)
        else:
            days[date] = day
    for date in duplicate:
        days.pop(date, None)
    complete_dates = [date for date in dates if _complete_day(days.get(date), catalog, clock_valid, now)]
    complete_window = window_consistent and len(complete_dates) == 20
    selected = dates[0] if dates else ''
    options = ''.join(f'<option value="{date}">{date}</option>' for date in dates)
    selector = (f'<div class="etfm-toolbar"><label>回看交易日<select data-flow-date aria-controls="etf-flow-days">{options}</select></label>'
                '<p class="etfm-result-count">选择历史日期仅切换已保存记录</p></div>') if dates else ''
    panels = ''.join(_day_panel(date, days.get(date), catalog, clock_valid, now, date == selected) for date in dates)
    if not dates:
        panels = '<p class="etfm-empty">没有声明有效交易日，不推算或自动填补20日记录。</p>'
    warnings = []
    if not clock_valid:
        warnings.append('报告、目录或收盘时钟尚未一致核验，正式榜暂停展示。')
    if not window_consistent:
        warnings.append('声明的20日窗口缺失、重复或与起止日期不一致；不生成正式累计榜。')
    if cutoff and (now.date()-cutoff).days > 4:
        warnings.append('最新截止日距今已超过4个自然日，请补新数据；主动回看的历史日期仍可作为历史研究。')
    if duplicate:
        warnings.append('部分交易日存在重复版本，该日不选择其中一份冒充已核验。')
    warning_html = '<p class="etfm-warning">' + ' '.join(warnings) + '</p>' if warnings else ''
    sources = []
    for source in report.get('sources', []) if isinstance(report.get('sources'), list) else []:
        if isinstance(source, dict):
            sources.append(f'<li>{_link(source.get("url"), source.get("label", "来源 ↗"))}'
                           f'<span>资料截至 {_e(source.get("as_of"))} · 取得 {_e(source.get("retrieved_at"))}</span>'
                           f'<p>{_e(source.get("basis"), "口径尚待补齐")}</p></li>')
    gaps = [gap for gap in report.get('gaps', []) if isinstance(gap, str)
            and not gap.startswith('本榜与同花顺普通资金净额')][:8] if isinstance(report.get('gaps'), list) else []
    gap_html = '<ul>' + ''.join(f'<li>{_e(gap)}</li>' for gap in gaps) + '</ul>' if gaps else ''
    count = _count(catalog.get('count'))
    coverage = report.get('coverage')
    coverage = coverage if isinstance(coverage, dict) else {}
    verified_sectors = _count(coverage.get('verified_sectors'))
    return (base + f' data-flow-selected-date="{selected}">'
            '<header class="etfm-section-heading"><div><span class="etfm-kicker">EASTMONEY / HISTORICAL MAIN-FLOW MODEL</span>'
            '<h2>东财模型主力 · 20日历史榜</h2></div><span class="etfm-badge">历史回补 · 非实时</span></header>'
            '<p class="etfm-note">主力是供应商对大单 / 超大单的分类，不代表真实机构账户。'
            '历史资金排名用于研究板块变化，不单独构成买卖信号。</p>'
            f'<div class="etfm-summary"><article class="etfm-summary-focus"><span>冻结回溯窗口</span><strong>{_e(report.get("window_start"))} — {_e(report.get("window_end"))}</strong>'
            '<p>历史回补与重新计算，不倒填当时实时发布或当时已经知道的目录。</p></article>'
            f'<article><span>已核验日覆盖</span><strong>{len(complete_dates)} / 20</strong><p>缺日不当0，局部池不授正式榜。</p></article>'
            f'<article><span>行业核验 / 当前冻结目录</span><strong>{_num(verified_sectors, 0)} / {_num(count, 0)}</strong>'
            f'<p>{"目录完整" if catalog.get("complete") is True else "目录尚不完整"} · 东方财富行业分类</p></article></div>'
            + warning_html + selector + f'<div id="etf-flow-days">{panels}</div>'
            + _cumulative(report, catalog, complete_window, clock_valid, now) +
            '<details id="etf-flow-history-sources" class="etfm-sources"><summary>来源、金额单位与历史目录边界</summary>'
            '<p>金额统一按人民币元保存，页面按元 / 万 / 亿换算；未提供的占比、大单明细不推算。'
            '每个完整日只在已核验的全部冻结行业中展示正净额降序前十、负净额升序前十，不拿零值或其他行业补位。</p>'
            '<p>当前行业目录被冻结后回溯，不等于每个历史日期当时全市场的行业池；分类会变化，成分可能重叠。'
            '历史日期由你主动回看，老日期不是失效记录；最新截止日的更新提示与历史有效性分开。</p>'
            f'<p>截止 {_e(report.get("cutoff"))} · 数据取得 {_e(report.get("retrieved_at"))} · 重算 {_e(report.get("generated_at"))}。'
            f'目录资料 {_e(catalog.get("as_of"))} · {_link(catalog.get("source"), "目录来源 ↗")}。</p>'
            f'<ul>{"".join(sources) or "<li>尚未提供可展示来源，需补核验。</li>"}</ul>{gap_html}</details></section>')
