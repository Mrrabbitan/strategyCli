"""Allowlisted, read-only presentations of four independent research results."""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import math
import re
from urllib.parse import urlsplit

from research_modules import load_module, stamp, TZ, eligible_now
from yichujifa_views import render_evidence as yichujifa_evidence

LABELS = {
    'hot': ('热点竞价', '先看板块，再核竞价', 'hot-sector-board'),
    'dragon': ('龙空龙', '分歧承接 · 退出优先', 'dragon-cycle-watchlist'),
    'yichujifa': ('一触即发', '三个分支 · 新行情确认', 'yichujifa-selection'),
    'prelaunch': ('启动前潜伏', '尚未加速 · 结构空间', 'prelaunch-selection'),
}
STATE_LABELS = {'not_run': '尚未执行', 'complete': '筛选完成', 'partial': '证据待补',
                'empty': '无合格候选', 'expired': '历史研究', 'unavailable': '数据不可用'}
COVERAGE_LABELS = {'scope':'研究范围','source_pool_count':'原始涨停池','source_date_verified':'池日期核验',
    'source_count_verified':'池总数核验','ranked_sectors':'达到热点门槛行业','displayed_sectors':'展示行业',
    'observations':'研究席位','history_verified':'日线审计通过','auction_final_verified':'最终竞价核验通过',
    'auction_qualified':'竞价条件通过','historical_pass':'历史量价预筛通过','actionable':'参与条件通过',
    'fund_monitored':'独立资金监测','excluded':'新增资格否决','first_board_observations':'首板研究',
    'risk_observations':'风险预警观察','requested_stocks':'历史请求股票','dual_history_verified':'双源历史通过',
    'candidate_records':'分支研究记录','prequalified_count':'收盘预资格通过','eligible_count':'参与条件通过',
    'universe_count':'原始股票池','mainboard_count':'主板元数据','target_count':'初筛目标',
    'scanned_count':'实际复核','history_verified_count':'双源日线通过','core_count':'核心资格通过',
    'displayed_count':'展示详情','quote_verified_count':'同日流通市值核验','complete':'覆盖完整',
    'sampling':'抽样方法','sampling_method':'抽样方法','candidate_details_limit':'详情展示上限'}
COVERAGE_LABELS.update({'universe_expected':'全市场元数据目标','universe_received':'已取得元数据',
    'mainboard_metadata':'主板元数据','name_risk_proxy_excluded':'名称风险代理排除',
    'provisional_cap_above_limit':'临时市值预筛排除','history_target':'日线复核目标',
    'history_requested':'本次日线复核','history_dual_verified':'双源65日一致',
    'cap_dated_verified':'同日流通市值核验','processed':'已处理','core':'核心潜伏',
    'watch':'待验证观察','started':'已启动跟踪','excluded_or_invalid':'排除 / 失效 / 到期'})
COVERAGE_LABELS.update({'intraday_requested':'原观察项盘中请求','intraday_valid':'盘中90秒内有效报价'})


def plain(value):
    if value is None:
        return '未提供'
    if isinstance(value, bool):
        return '是' if value else '否'
    if isinstance(value, float):
        return f'{value:,.3f}'.rstrip('0').rstrip('.') if math.isfinite(value) else '未提供'
    if isinstance(value, (dict, list)):
        return '见逐项证据'
    translations = {'holding':'守位整理','pending':'待验证','partial':'资料不完整','unknown':'未知',
                    'started':'已启动','already_started':'已启动','invalid':'失效','complete':'完整','empty':'有效空池'}
    text = translations.get(str(value),str(value))
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?\+08:00',text):
        text = text[:10] + ' ' + text[11:19]
    if re.search(r'/(?:Users|home|private|var|tmp)/|file://|(?:password|token|secret)\s*[=:]', text, re.I):
        return '本地证据已留档；私有路径与配置不在页面展示'
    return text


def e(value):
    return html.escape(plain(value), quote=True)


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def links(items):
    rows = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        url = item.get('url') or item.get('source') or ''
        try:
            split = urlsplit(url)
            valid = split.scheme == 'https' and bool(split.netloc) and not split.username and not split.password
        except (ValueError, TypeError):
            valid = False
        if valid and plain(url) == url:
            rows.append(f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{e(item.get("label") or item.get("title") or split.hostname)} ↗</a>')
    return ' · '.join(dict.fromkeys(rows)) or '原始证据保存在本地；暂无可展示的来源链接。'


def lines(items):
    return ''.join('<li>' + e(item) + '</li>' for item in (items or []) if item is not None)


def evidence_chart(row, as_of):
    """OHLC/volume are plotted as observed; missing fields do not become zero bars."""
    cutoff = stamp(as_of)
    rows = []
    for bar in row.get('bars', []) if isinstance(row.get('bars'), list) else []:
        if not isinstance(bar, dict):
            continue
        day = str(bar.get('date', ''))[:10]
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day) or (cutoff and day > cutoff.date().isoformat()):
            continue
        if not all(finite(bar.get(k)) and bar[k] > 0 for k in ('open', 'high', 'low', 'close')):
            continue
        if bar['high'] < max(bar['open'], bar['close'], bar['low']) or bar['low'] > min(bar['open'], bar['close']):
            continue
        rows.append(bar)
    rows = rows[-40:]
    if not rows:
        return '<p class="chart-missing">没有同口径日线证据，暂不绘制价格与量能图。</p>'
    levels = [x for x in row.get('levels', []) if isinstance(x, dict) and finite(x.get('value')) and x['value'] > 0]
    low, high = min(b['low'] for b in rows), max(b['high'] for b in rows)
    levels = [x for x in levels if low * .75 <= x['value'] <= high * 1.25]
    lo = min([low] + [x['value'] for x in levels]); hi = max([high] + [x['value'] for x in levels])
    spread = max(hi - lo, hi * .02); lo -= spread * .08; hi += spread * .08
    y = lambda value: 22 + (hi - value) / (hi - lo) * 140
    step = 620 / max(len(rows), 1); body_width = max(2, min(12, step * .62))
    vols = [x.get('volume_shares') for x in rows if finite(x.get('volume_shares')) and x['volume_shares'] >= 0]
    vmax = max(vols or [1]) or 1
    plot = []
    for tick in (lo, (lo + hi) / 2, hi):
        yy = y(tick)
        plot.append(f'<line x1="56" x2="694" y1="{yy:.1f}" y2="{yy:.1f}" class="chart-grid"/><text x="50" y="{yy+4:.1f}" text-anchor="end">{tick:.2f}</text>')
    for i, bar in enumerate(rows):
        x = 65 + (i + .5) * step
        up = bar['close'] >= bar['open']; color = '#b14642' if up else '#247b71'
        top = min(y(bar['open']), y(bar['close'])); height = max(abs(y(bar['open']) - y(bar['close'])), 1.5)
        desc = f'{bar["date"]} 开 {bar["open"]} 高 {bar["high"]} 低 {bar["low"]} 收 {bar["close"]} 量 {plain(bar.get("volume_shares"))}股'
        plot.append(f'<g><title>{e(desc)}</title><line x1="{x:.1f}" x2="{x:.1f}" y1="{y(bar["high"]):.1f}" y2="{y(bar["low"]):.1f}" stroke="{color}"/><rect x="{x-body_width/2:.1f}" y="{top:.1f}" width="{body_width:.1f}" height="{height:.1f}" fill="{color}"/>')
        v = bar.get('volume_shares')
        if finite(v) and v >= 0:
            h = v / vmax * 42
            plot.append(f'<rect x="{x-body_width/2:.1f}" y="{226-h:.1f}" width="{body_width:.1f}" height="{h:.1f}" fill="{color}" opacity=".65"/>')
        plot.append('</g>')
    for level in levels[:5]:
        yy = y(level['value'])
        plot.append(f'<line x1="56" x2="694" y1="{yy:.1f}" y2="{yy:.1f}" class="chart-level"/><text x="695" y="{yy-3:.1f}" text-anchor="end" class="chart-level-label">{e(level.get("label"))} {level["value"]:.2f}</text>')
    plot.append(f'<text x="60" y="246">{e(rows[0]["date"])}</text><text x="692" y="246" text-anchor="end">{e(rows[-1]["date"])}</text><text x="50" y="203" text-anchor="end">量</text>')
    table = ''.join(f'<tr><td>{e(b.get("date"))}</td>'+''.join(f'<td>{e(b.get(k))}</td>' for k in ('open','high','low','close','volume_shares','amount_cny'))+'</tr>' for b in rows)
    return f'<figure class="evidence-chart"><svg viewBox="0 0 740 258" role="img" aria-label="{e(row.get("name"))}日线与成交量"><title>日线与成交量证据</title>{"".join(plot)}</svg><figcaption>价格：元 · 成交量：股 · 红涨绿跌；仅绘制已取得的日线，不代表当前买点。</figcaption></figure><details class="bar-data"><summary>查看图表原始数值与单位</summary><div class="table-scroll"><table><thead><tr><th>日期</th><th>开</th><th>高</th><th>低</th><th>收</th><th>成交量 / 股</th><th>成交额 / 元</th></tr></thead><tbody>{table}</tbody></table></div></details>'


def specific_evidence(module, row):
    blocks = []
    if module == 'hot':
        blocks.append('<h4>原始排名依据 · 与竞价结果分开</h4><ul>'+lines(row.get('selection_reasons'))+'</ul>')
        components = row.get('strength_components') or {}
        if isinstance(components,dict):
            blocks.append('<dl class="candidate-metrics">'+''.join('<div><dt>'+e(k)+'</dt><dd>'+e(v)+'</dd></div>' for k,v in components.items() if not isinstance(v,(dict,list)))+'</dl>')
    if module == 'dragon':
        volume = row.get('volume') or row.get('volume_audit') or {}
        ledger = row.get('volume_ledger') or volume
        days = ledger.get('days', []) if isinstance(ledger, dict) else ledger if isinstance(ledger, list) else []
        if days:
            table = ''
            for day in days:
                if not isinstance(day, dict):continue
                qty = day.get('volume_shares'); unit = '股'
                if qty is None: qty, unit = day.get('volume_lots'), '手'
                ratio = day.get('volume_ratio', day.get('ratio'))
                count = day.get('expansion_count', day.get('cumulative',day.get('cumulative_count')))
                note = day.get('time_note') or ('盘中累计量，未完成全天' if day.get('data_kind') == 'intraday_cumulative' else '完整收盘量，不倒推盘中触发')
                table += f'<tr><td>{e(day.get("date"))}</td><td>{e(qty)} {unit}</td><td>{e(ratio)}×</td><td>{e(count)}</td><td>{e(day.get("confirmed_as_of"))} · {e(note)}</td></tr>'
            blocks.append('<h4>本轮逐日放量 · 含首板，每日最多一次</h4><div class="table-scroll"><table><thead><tr><th>日期</th><th>成交量</th><th>前日倍数</th><th>累计次数</th><th>确认时点与口径</th></tr></thead><tbody>'+table+'</tbody></table></div>')
        timeline = row.get('refill_timeline') or row.get('reseal_timeline') or []
        if timeline:
            states = {'sealed':'封板','opened':'开板','resealed':'回封'}
            blocks.append('<h4>当日回封时间线</h4><ol>'+''.join('<li>'+e(x.get('source_asof') or x.get('time'))+' '+e(states.get(x.get('state'),x.get('event') or x.get('state')))+' '+links([{'url':x.get('source_url'),'label':'源记录'}])+'</li>' for x in timeline if isinstance(x,dict))+'</ol>')
        else:
            blocks.append('<p class="notice">当日封板—开板—回封时间线缺失；昨日回封不能代替当日回封。</p>')
    auction = row.get('auction')
    if module in ('hot', 'dragon'):
        if isinstance(auction,dict):
            labels = {'time':'最终成交时点','source_time':'源数据时间','source_asof':'源数据时间','price':'最终成交价 / 元','gap_pct':'高开幅度 / %','volume_shares':'成交量 / 股','amount_cny':'成交额 / 元','turnover_pct':'竞价换手 / %','reason':'核验说明'}
            rendered = ''.join(f'<div><dt>{label}</dt><dd>{e(auction.get(k))}</dd></div>' for k,label in labels.items() if k in auction)
            blocks.append('<h4>9:25最终竞价证据</h4><dl class="candidate-metrics">'+rendered+'</dl>')
        else:
            blocks.append('<p class="muted">最终竞价尚未取得有效证据；不以开盘价或全天换手代替。</p>')
    return ''.join(blocks)


def market_evidence(module, data):
    checks = [x for x in data.get('market_checks', []) if isinstance(x, dict)]
    sectors = [x for x in data.get('sector_reviews', []) if isinstance(x, dict)]
    if not checks and not sectors:
        return ''
    result = '<section class="research-market"><h2>市场与板块前提</h2><ul>'
    for check in checks:
        state = '通过' if check.get('passed') is True else '未通过' if check.get('passed') is False else '待验证'
        result += f'<li><b>{e(check.get("label"))} · {state}</b>：{e(check.get("detail"))}</li>'
    result += '</ul>'
    if sectors:
        result += f'<details id="{module}-sector-evidence"><summary>行业退潮、分化与恢复条件</summary>'
        for sector in sectors:
            result += f'<h3>{e(sector.get("name"))} · {e(sector.get("status"))}</h3><ul>{lines(sector.get("reasons"))}</ul><p>恢复条件</p><ul>{lines(sector.get("recovery"))}</ul>'
        result += '</details>'
    return result + '</section>'


def prelaunch_pools(data):
    result = '<section class="research-market" id="prelaunch-pools"><h2>四类研究清单</h2><p class="muted">核心资格、待验证、已启动与失效分别保存；下方详情仅展示研究队列，不是核心排名。</p>'
    for key, label in (('core','核心潜伏'),('watch','待验证观察'),('started','已启动跟踪'),('invalid','失效 / 排除 / 到期')):
        rows = [x for x in data.get(key,[]) if isinstance(x,dict)]
        table = ''
        for row in rows:
            metrics = row.get('metrics') or {}
            table += '<tr>'+''.join('<td>'+e(x)+'</td>' for x in (
                (row.get('name') or '')+' '+str(row.get('code') or ''), row.get('status'),
                metrics.get('净空间比'), row.get('valid_until'),
                (row.get('reasons') or ['见详情'])[0]))+'</tr>'
        result += f'<details class="native-bucket" id="prelaunch-pool-{key}"'+(' open' if key=='core' else '')+f'><summary>{label} · {len(rows)} 条</summary>'
        if rows:
            result += '<div class="table-scroll"><table><thead><tr><th>股票</th><th>研究状态</th><th>净空间比</th><th>原观察截止</th><th>主要依据或阻断项</th></tr></thead><tbody>'+table+'</tbody></table></div>'
        else:
            result += '<p class="muted">本类为空，不从其他类别补位。</p>'
        result += '</details>'
    return result + '</section>'


def intraday_context(context):
    """Display quote risk separately; a close qualification is not a live quote."""
    if not isinstance(context, dict):
        return ''
    at = stamp(context.get('source_asof'))
    now = dt.datetime.now(TZ)
    fresh = at is not None and at.date() == now.date() and 0 <= (now-at).total_seconds() <= 90
    label = '盘中价格风险背景 · 非新增资格' if fresh else '历史盘中背景 · 需重新取得报价' if at else '盘中背景待验证'
    expiry = at + dt.timedelta(seconds=90) if at else None
    attr = f' data-context-until="{html.escape(expiry.isoformat(),quote=True)}"' if fresh else ''
    source = context.get('source')
    sources = source if isinstance(source,list) else [source] if isinstance(source,dict) else [{'url':source,'label':'盘中报价来源'}]
    return f'<aside class="notice"><b{attr}>{e(label)}</b><p>参考价 {e(context.get("price"))} 元 · 报价源时间 {e(context.get("source_asof"))}</p><ul>{lines(context.get("notes") or context.get("errors"))}</ul><p>{links(sources)}</p><p>原收盘资格、冻结结构与五日观察期限不顺延。</p></aside>'


def candidate_card(module, row, data, index, expired=False):
    code, name = str(row.get('code','')), row.get('name') or '待核验证券'
    identity = '|'.join((module, str(row.get('group')), str(row.get('sector') or row.get('theme') or ''), code or str(index)))
    key = hashlib.sha256(identity.encode()).hexdigest()[:14]
    checks = [x for x in row.get('conditions',[]) if isinstance(x,dict)]
    pass_count = sum(x.get('passed') is True for x in checks)
    now = dt.datetime.now(TZ)
    live_expiry = stamp(row.get('live_valid_until') or data.get('live_valid_until'))
    if module == 'prelaunch':
        live_expiry = stamp(row.get('valid_until'))
    eligible = not expired and eligible_now(module, row, data, now)
    label = '核心研究资格通过' if module == 'prelaunch' and eligible else '最终竞价条件通过 · 非买点' if module == 'hot' and eligible else '该时点参与条件通过' if eligible else plain(row.get('status') or '待验证观察')
    if expired: label = '历史记录 · 不取得当前资格'
    elif row.get('eligible') is True and not eligible:
        label = '原观察期限已结束或未知 · 需重新筛选' if module == 'prelaunch' else '历史盘中复核 · 当前需重新确认'
    live_attr = f' data-live-until="{html.escape(live_expiry.isoformat(),quote=True)}" data-signal-kind="{module}"' if eligible and live_expiry else ''
    meta = row.get('metrics') if isinstance(row.get('metrics'),dict) else {}
    metrics = ''.join(f'<div><dt>{e(k)}</dt><dd>{e(v)}</dd></div>' for k,v in meta.items() if not isinstance(v,(dict,list)))
    checklist = ''.join(f'<tr><td><span class="check-{str(x.get("passed")).lower()}">{"通过" if x.get("passed") is True else "未通过" if x.get("passed") is False else "待验证"}</span></td><th scope="row">{e(x.get("label"))}</th><td>{e(x.get("detail"))}</td></tr>' for x in checks)
    bucket = 'qualified' if not expired and (eligible or row.get('prequalified') is True) else 'other'
    search = name + ' ' + code
    industry = row.get('sector') or row.get('theme')
    group_label = str(row.get('group') or '观察记录') + (' · ' + str(industry) if industry and industry != row.get('group') else '')
    return f'''<article class="research-candidate" data-candidate data-search="{e(search)}" data-bucket="{bucket}"><header><div><span class="eyebrow">{e(group_label)}</span><h3>{e(name)} <small>{e(code)}</small></h3></div><span class="candidate-state{' positive' if eligible else ''}"{live_attr}>{e(label)}</span></header>
<dl class="candidate-metrics">{metrics}</dl><p class="candidate-reason">{e((row.get('reasons') or ['需继续核验逐项条件'])[0])}</p>{intraday_context(row.get('intraday_context')) if module == 'prelaunch' else ''}
<details id="candidate-{key}"><summary>证据与条件 <span>{pass_count}/{len(checks)} 项已通过</span></summary><div class="table-scroll"><table class="condition-table"><thead><tr><th>结论</th><th>条件</th><th>事实与缺口</th></tr></thead><tbody>{checklist or '<tr><td colspan="3">尚无逐项核验证据</td></tr>'}</tbody></table></div>
{evidence_chart(row,data.get('as_of'))}{specific_evidence(module,row)}{yichujifa_evidence(row,data) if module == 'yichujifa' else ''}<ul>{lines(row.get('reasons'))}</ul>{('<p class="notice">失效 / 风险条件：'+e(row.get('invalidation') or row.get('risk_note'))+'</p>') if row.get('invalidation') or row.get('risk_note') else ''}<p class="source-links">{links(row.get('sources'))}</p></details></article>'''


def render_module(module):
    current = load_module(module)
    data, state = current['data'], current['state']
    title, eyebrow, anchor = LABELS[module]
    rules = data.get('rules') or {}
    attempt = current.get('attempt') or {}
    rows = [x for x in data.get('candidates',[]) if isinstance(x,dict)]
    coverage = data.get('coverage') if isinstance(data.get('coverage'),dict) else {}
    expired = state in ('not_run','expired','unavailable')
    now = dt.datetime.now(TZ)
    def currently_eligible(row):
        return eligible_now(module, row, data, now)
    count = sum(currently_eligible(x) for x in rows) if not expired else 0
    count_label = '核心研究' if module == 'prelaunch' else '竞价条件通过 / 非买点' if module == 'hot' else '参与条件通过'
    coverage_text = ' · '.join(e(COVERAGE_LABELS.get(k,k))+' '+e(v) for k,v in coverage.items() if not isinstance(v,(dict,list)))
    missing = list(data.get('missing') or [])
    if attempt.get('status') == 'unavailable': missing.insert(0, attempt.get('summary') or '最近更新失败')
    groups = []
    order = list(dict.fromkeys(str(x.get('group') or '观察记录') for x in rows))
    if module == 'yichujifa':
        branch_order = {'一进二':0,'二进三':1,'首板后守位':2}
        order.sort(key=lambda group:branch_order.get(group,3))
    for group in order:
        members = [x for x in rows if str(x.get('group') or '观察记录') == group]
        groups.append('<section class="research-group"><div class="research-group-title"><h2>'+e(group)+'</h2><span>'+str(len(members))+' 条研究记录</span></div>'+''.join(candidate_card(module,row,data,rows.index(row),expired) for row in members)+'</section>')
    phase = {'prepare':'盘前准备','close':'收盘研究','intraday':'盘中复核','replay':'历史重放'}.get(data.get('phase'),'尚无执行记录')
    expiry = stamp(data.get('valid_until'))
    source = links(data.get('sources'))
    rule_link = f'<a href="#strategy-{module}">查看规则与执行口径 ↗</a>' if module != 'hot' else '<a href="#page-dragon">核对龙空龙交易资格 ↗</a>'
    return f'''<div class="research-surface research-{module}" id="{anchor}" data-module="{module}" data-module-until="{html.escape(expiry.isoformat() if expiry else '',quote=True)}"><div class="research-heading"><div><span class="eyebrow">{eyebrow}</span><h1>{title}</h1><p class="lead">{e(data.get('summary') or '等待首次运行本模块的研究流程；规则可独立阅读。')}</p></div><div class="research-status"><b>{STATE_LABELS[state]}</b><span>{e(phase)}</span></div></div>
<div class="research-stats"><div><span>行情截至 · 上海时间</span><strong>{e(data.get('as_of'))}</strong></div><div><span>研究记录 / 非建仓数量</span><strong>{len(rows)} 条</strong></div><div><span>{count_label}</span><strong class="qualified-count">{count} 条</strong></div><div><span>最近执行 · 上海时间</span><strong>{e(attempt.get('attempted_at') or data.get('generated_at'))}</strong></div></div>
<div class="notice module-validity" role="status">{e(current['note'])}<p>观察有效至 {e(data.get('valid_until'))}。刷新网页只读取已发布结果，不会重新执行选股。</p>{('<p>盘中报价截至 '+e(data.get('intraday_as_of'))+'；收盘研究时点与原期限不变。</p><ul>'+lines((data.get('intraday_context') or {}).get('errors'))+'</ul>') if module == 'prelaunch' and data.get('phase') == 'intraday' else ''}</div><p class="coverage-line">{coverage_text or '覆盖范围尚未核验。'}</p>
{prelaunch_pools(data) if module == 'prelaunch' and data else ''}
{market_evidence(module,data)}<div class="research-toolbar"><label>查找股票 <input type="search" data-research-search="{module}" placeholder="名称或代码" autocomplete="off"></label><label>记录范围 <select data-research-filter="{module}"><option value="all">全部研究记录</option><option value="qualified">已通过预资格或核心资格</option><option value="other">待验证 / 不选 / 历史记录</option></select></label><span data-result-count="{module}">{len(rows)} 条记录</span></div>
{''.join(groups) or '<div class="research-empty"><b>'+('有效空池' if state=='empty' else '暂没有可展示的新版候选')+'</b><p>不以旧名单、无权限股票或其他策略候选补位。请查看本次覆盖与证据缺口。</p></div>'}
<p class="filter-empty" hidden>没有匹配的研究记录。筛选页面不改变策略排名或资格。</p><details class="research-limitations" id="missing-{module}" open><summary>本次缺失证据与限制</summary><ul>{lines(missing) or '<li>仍需逐只核验实时条件；研究完成不代表当前可以买入。</li>'}</ul></details>
<div class="research-provenance"><p>规则版本 {e(rules.get('version') or '待核验')} · 内容指纹 {e(str(rules.get('source_hash') or '未提供')[:24])}</p><p>{rule_link}</p><details id="sources-{module}"><summary>来源与原始证据</summary><p class="source-links">{source}</p></details>{('<p class="notice">'+e(data.get('risk_note'))+'</p>') if data.get('risk_note') else ''}<p class="muted">事实、条件推断与策略假设分开阅读。数据缺失不等于零，评分不等于胜率；本页不执行交易。</p></div></div>'''
