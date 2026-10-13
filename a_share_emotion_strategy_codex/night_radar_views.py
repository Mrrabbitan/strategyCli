"""Read-only, allowlisted HTML and chat tables for the nightly information radar."""
from __future__ import annotations

import datetime as dt
import html
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, unquote, urlsplit


SECTIONS = (
    ('limit_up', '收盘连板梯队', ('股票', '连板', '重要交易风险')),
    ('announcements', '公司公告', ('股票', '事件摘要', '性质', '披露时间')),
    ('earnings', '业绩盈喜', ('股票', '报告期', '预计归母净利润 / 万元', '同比 / %', '类型与口径')),
    ('repurchases', '股份回购', ('股票', '预算 / 万元', '价格上限 / 元', '已回购 / 万元', '用途与进度')),
    ('broker_seats', '游资动态 · 营业部席位', ('股票', '主要营业部及净额 / 万元', '披露营业部合计净额 / 万元', '涨跌幅 / %')),
    ('institutions', '机构抢筹 · 席位净买入', ('股票', '机构净买额 / 万元', '涨跌幅 / %', '换手率 / %', '行业')),
)
STATUS = {'complete': '核验完成', 'partial': '部分数据缺失',
          'unavailable': '数据不可用', 'empty': '本期无记录'}


def load_reports():
    # Delayed import keeps the presentation independently testable and read-only.
    from night_radar_store import load_reports as read_reports
    return read_reports()


def text(value):
    if value is None or isinstance(value, (dict, list)):
        return '—'
    value = str(value)
    if re.search(r'/(?:Users|home|private|var|tmp)/|file://|(?:password|token|secret)\s*[=:]', value, re.I):
        return '本地证据已留档'
    return value or '—'


def e(value):
    return html.escape(text(value), quote=True)


def records(value):
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def safe_url(value):
    if not isinstance(value, str) or text(value) != value or re.search(r'[\s<>"\\]', value):
        return ''
    try:
        parsed = urlsplit(value)
        sensitive = any(re.search(r'password|token|secret|api.?key|credential', key, re.I)
                        for key, _ in parse_qsl(parsed.query))
        return value if (parsed.scheme in ('http', 'https') and parsed.netloc and not parsed.username
                         and not parsed.password and not sensitive and text(unquote(value)) == unquote(value)) else ''
    except ValueError:
        return ''


def number(value, divisor=1):
    if value is None or isinstance(value, bool):
        return '—'
    try:
        result = Decimal(str(value)) / Decimal(divisor)
        if not result.is_finite():
            return '—'
        return f'{result:,.2f}'.rstrip('0').rstrip('.')
    except (InvalidOperation, ValueError, TypeError):
        return '—'


def interval(low, high, divisor=1):
    a, b = number(low, divisor), number(high, divisor)
    return a if a == b else f'{a}～{b}'


def stock(row):
    return ' '.join(text(row.get(key)) for key in ('name', 'code') if row.get(key) is not None) or '—'


def clock(value):
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(dt.timezone(dt.timedelta(hours=8)))
        return parsed.strftime('%Y-%m-%d %H:%M')
    except (ValueError, TypeError):
        return text(value)


def joined(*items):
    return '；'.join(dict.fromkeys(text(x) for x in items if x is not None and text(x) not in ('', '—'))) or '—'


def row_cells(key, row):
    cells = [stock(row)]
    if key == 'limit_up':
        cells += [number(row.get('boards')), joined(row.get('risk'))]
    elif key == 'announcements':
        stance = {'risk': '风险', 'opportunity': '机会', 'neutral': '中性', 'positive': '机会'}.get(row.get('stance'), row.get('stance'))
        cells += [joined(row.get('summary') or row.get('title'), row.get('change'), '补录' if row.get('late_arrival') else None),
                  joined(stance, row.get('kind')), joined(clock(row.get('source_time')), row.get('source_time_basis'))]
    elif key == 'earnings':
        cells += [text(row.get('period_label')),
                  interval(row.get('profit_low_cny'), row.get('profit_high_cny'), 10000),
                  interval(row.get('change_low_pct'), row.get('change_high_pct')),
                  joined(row.get('forecast_type'), row.get('basis'), '推算值' if row.get('estimated') else None)]
    elif key == 'repurchases':
        cells += [interval(row.get('budget_low_cny'), row.get('budget_high_cny'), 10000),
                  number(row.get('price_cap')), number(row.get('actual_cny'), 10000),
                  joined(row.get('progress'), row.get('purpose'))]
    elif key == 'broker_seats':
        seats = '；'.join(f'{text(s.get("name"))} {number(s.get("net_cny"), 10000)}'
                         for s in records(row.get('top_seats'))[:3])
        cells += [seats or '—', number(row.get('net_cny'), 10000), number(row.get('pct'))]
    elif key == 'institutions':
        cells += [number(row.get('net_cny'), 10000), number(row.get('pct')),
                  number(row.get('turnover_pct')), text(row.get('industry'))]
    if row.get('verification_note'):
        cells[-1] = joined(cells[-1], row['verification_note'])
    if key in ('earnings', 'repurchases'):
        cells[-1] = joined(cells[-1], row.get('change'), '补录' if row.get('late_arrival') else None)
    return cells


def table(key, title, headers, rows, *, multi=False):
    headings = list(headers) + (['统计期间'] if multi else [])
    body = []
    for row in rows:
        cells = row_cells(key, row)
        if multi:
            cells.append(joined(row.get('period_label'), row.get('period_start'), row.get('period_end'), row.get('reason')))
        rendered = []
        for i, value in enumerate(cells):
            content = e(value)
            if i == 0 and safe_url(row.get('source_url')):
                content = f'<a href="{e(row["source_url"])}" target="_blank" rel="noopener noreferrer">{content} ↗</a>'
            rendered.append(f'<td>{content}</td>')
        body.append('<tr>' + ''.join(rendered) + '</tr>')
    return (f'<div class="night-table-scroll" role="region" tabindex="0" aria-label="{e(title)}表格">'
            f'<table><caption class="night-sr-only">{e(title)}</caption><thead><tr>' +
            ''.join(f'<th scope="col">{e(h)}</th>' for h in headings) + '</tr></thead><tbody>' +
            ''.join(body) + '</tbody></table></div>')


def source_links(section):
    sources = records(section.get('sources'))
    items = []
    for item in sources:
        url = safe_url(item.get('url') or item.get('source_url'))
        if url:
            label = item.get('label') or item.get('title') or urlsplit(url).hostname
            items.append(f'<a href="{e(url)}" target="_blank" rel="noopener noreferrer">{e(label)} ↗</a>')
    return ' · '.join(dict.fromkeys(items))


def gap_category(value):
    """Translate collection diagnostics into a short reader-facing explanation."""
    value = str(value or '')
    for pattern, label in (
        (r'日期.*冲突|qdate|日线.*不一致|源日期', '源数据日期冲突'),
        (r'双榜|买卖榜|买榜|卖榜|buy.?sell|不完整.*席位|席位.*不完整', '买卖席位榜不完整'),
        (r'分页|page|总数|唯一.*代码', '部分分页或记录缺失'),
        (r'timeout|timed out|超时|时间预算', '接口超时，部分数据未取得'),
        (r'HTTP|URLError|Connection|接口断|断连|连接|网络|RemoteDisconnected|Traceback', '接口连接失败'),
        (r'行业', '部分行业归属缺失'),
        (r'互动|问答', '互动问答暂未完整覆盖'),
        (r'同比', '部分同比数据未完成原文核验'),
        (r'预算|回购.*金额', '部分回购金额未完成原文核验'),
        (r'原文|标题|预告|结构化', '部分信息尚未完成原文核验'),
        (r'时点|时间|截止|未来', '部分记录时间未通过核验'),
        (r'金额.*冲突|净额|算术|买卖总额', '部分席位金额核验不一致'),
        (r'日历|交易日', '交易日历尚未核验'),
    ):
        if re.search(pattern, value, re.I):
            return label
    return '部分记录尚未完成核验'


def gap_entries(section):
    values = section.get('missing')
    return [str(v) for v in values if isinstance(v, (str, int, float))] if isinstance(values, list) else []


def gap_summary(section):
    categories = list(dict.fromkeys(gap_category(value) for value in gap_entries(section)))
    return '；'.join(categories[:2]) or '本栏证据未完整取得'


def gap_detail(value):
    """Retain meaningful Chinese details, never HTTP stacks, IDs or private paths."""
    clean = text(value)
    if clean != str(value):
        return gap_category(value) + '；原始记录已在本地留档'
    if re.search(r'https?://|Traceback|HTTP|Connection|timeout|timed out|URLError|RemoteDisconnected|File \"', clean, re.I):
        return gap_category(clean) + '；原始接口诊断已在本地留档'
    clean = re.sub(r'[A-Za-z_-]*\d{9,}[A-Za-z_\d-]*', '〔记录编号已省略〕', clean)
    return clean.replace('\n', ' ').replace('\r', ' ')


def section_html(key, title, headers, section, token):
    rows, featured = records(section.get('rows')), records(section.get('featured'))
    if key in ('broker_seats', 'institutions'):
        rows = [r for r in rows if r.get('period') != 'multi']
    state = STATUS.get(section.get('status'), '尚未执行')
    result = f'<section class="night-card" aria-labelledby="night-{token}-{key}-title"><header><h3 id="night-{token}-{key}-title">{e(title)}</h3><span>{e(state)}</span></header>'
    if section.get('status') in ('partial', 'unavailable'):
        result += '<p class="night-gap">' + e(gap_summary(section)) + '。</p>'
    if featured:
        result += table(key, title, headers, featured)
    else:
        message = '本期无符合本栏口径的记录。' if section.get('status') in ('complete', 'empty') else '暂无可核验的精选记录。'
        result += f'<p class="night-empty">{message}</p>'
    if rows and rows != featured:
        result += f'<details id="night-{token}-{key}-all"><summary>完整记录 · {len(rows)} 条</summary>' + table(key, title + '完整记录', headers, rows) + '</details>'
    multi = records(section.get('multi_rows'))
    if multi:
        result += f'<details id="night-{token}-{key}-multi"><summary>多日累计榜 · {len(multi)} 条</summary><p class="night-footnote">独立统计，不与单日榜相加。</p>' + table(key, title + '多日累计榜', headers, multi, multi=True) + '</details>'
    gaps = list(dict.fromkeys(gap_detail(value) for value in gap_entries(section)))
    if gaps:
        result += f'<details id="night-{token}-{key}-gaps"><summary>本栏数据缺口</summary><ul>' + ''.join('<li>' + e(value) + '</li>' for value in gaps) + '</ul></details>'
    links = source_links(section)
    if links:
        result += f'<details id="night-{token}-{key}-sources" class="night-sources"><summary>数据来源</summary><p>{links}</p></details>'
    return result + '</section>'


def alignment_html(report, token):
    rows = records(report.get('alignment'))
    if not rows:
        return ''
    body = []
    for row in rows:
        name = e(stock(row))
        if safe_url(row.get('source_url')):
            name = f'<a href="{e(row["source_url"])}" target="_blank" rel="noopener noreferrer">{name} ↗</a>'
        body.append('<tr><td>' + name + '</td>' + ''.join('<td>' + e(row.get(k)) + '</td>' for k in ('module', 'result', 'note')) + '</tr>')
    return (f'<details id="night-{token}-alignment" class="night-alignment"><summary>截图逐项对齐 · {len(rows)} 条</summary>'
            '<p>事后来源复核，不代表当晚已保存原始快照；截图中的非主板项目仅用于对齐。</p>'
            '<div class="night-table-scroll" role="region" tabindex="0" aria-label="截图逐项对齐"><table><thead><tr><th scope="col">股票</th><th scope="col">模块</th><th scope="col">对齐结论</th><th scope="col">说明</th></tr></thead><tbody>' +
            ''.join(body) + '</tbody></table></div></details>')


def ordered_reports(reports):
    days = {}
    for report in records(reports):
        date = str(report.get('report_date') or '')
        try:
            dt.date.fromisoformat(date)
        except ValueError:
            continue
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date):
            continue
        if date not in days or str(report.get('executed_at', '')) > str(days[date].get('executed_at', '')):
            days[date] = report
    return [days[date] for date in sorted(days, reverse=True)]


def render_page(reports=None):
    reports = ordered_reports(load_reports() if reports is None else reports)
    result = '<section class="night-radar" id="topic-night-radar" data-night-radar><header class="night-heading"><div><span>收盘事实 · 晚间信息</span><h2>晚间投资雷达</h2></div><p>沪深主板 · 每晚22:00</p></header>'
    if not reports:
        return result + '<p class="night-empty">尚未执行。首次报告将在完成来源核验后展示。</p></section>'
    result += '<div class="night-date-tabs" role="tablist" aria-label="晚间投资雷达日期">'
    for i, report in enumerate(reports):
        date = report['report_date']; token = date.replace('-', '')
        result += (f'<button type="button" role="tab" id="night-tab-{token}" data-night-date="{date}" '
                   f'aria-controls="topic-night-radar-{token}" aria-selected="{str(i == 0).lower()}" tabindex="{0 if i == 0 else -1}">{date}</button>')
    result += '</div>'
    for i, report in enumerate(reports):
        date = report['report_date']; token = date.replace('-', '')
        result += f'<section id="topic-night-radar-{token}" role="tabpanel" aria-labelledby="night-tab-{token}" data-night-day="{date}" tabindex="0"' + (' hidden' if i else '') + '>'
        result += '<div class="night-meta"><span>行情日 ' + e(report.get('trade_date')) + '</span><span>信息截止 ' + e(clock(report.get('cutoff'))) + '</span><span>实际执行 ' + e(clock(report.get('executed_at'))) + '</span></div>'
        result += '<p class="night-summary"><b>' + e(STATUS.get(report.get('status'), '尚未执行')) + '</b> · ' + e(report.get('summary')) + '</p>'
        if report.get('mode') in ('historical', 'historical_review', 'post_hoc', 'alignment', 'historical_alignment', 'post_hoc_review') or report.get('alignment'):
            result += '<p class="night-footnote">事后来源复核 · 保留原统计日期，不冒充当晚原始快照。</p>'
        sections = report.get('sections') if isinstance(report.get('sections'), dict) else {}
        result += '<div class="night-grid">'
        for key, title, headers in SECTIONS:
            section = sections.get(key) if isinstance(sections.get(key), dict) else {}
            result += section_html(key, title, headers, section, token)
        result += '</div>' + alignment_html(report, token)
        result += '<p class="night-footnote">金额单位：万元。公告预期、回购预算及龙虎榜披露席位均按各自口径展示；营业部不等于已识别的真实游资账户。</p></section>'
    return result + '</section>'


def md(value):
    value = html.escape(text(value), quote=False).replace('\\', '\\\\').replace('\r', ' ').replace('\n', ' ')
    for symbol in ('|', '[', ']', '`', '*', '_'):
        value = value.replace(symbol, '\\' + symbol)
    return value


def render_markdown(report):
    """The six compact tables sent to chat; no private payload or path is rendered."""
    lines = [f'**晚间投资雷达 · {md(report.get("report_date"))}**', '',
             f'行情日：{md(report.get("trade_date"))}；信息截止：{md(clock(report.get("cutoff")))}；实际执行：{md(clock(report.get("executed_at")))}。', '',
             md(report.get('summary')), '']
    if report.get('alignment') or report.get('mode') in ('historical', 'historical_review', 'post_hoc', 'alignment', 'historical_alignment', 'post_hoc_review'):
        lines += ['事后来源复核，不冒充当晚原始快照。', '']
    sections = report.get('sections') if isinstance(report.get('sections'), dict) else {}
    for key, title, headers in SECTIONS:
        section = sections.get(key) if isinstance(sections.get(key), dict) else {}
        featured = records(section.get('featured'))
        lines += ['**' + title + '**', '']
        if section.get('status') in ('partial', 'unavailable'):
            lines += [md(gap_summary(section)) + '。', '']
        if featured:
            lines += ['| ' + ' | '.join(headers) + ' |', '| ' + ' | '.join('---' for _ in headers) + ' |']
            for row in featured:
                cells = [md(c) for c in row_cells(key, row)]
                url = safe_url(row.get('source_url'))
                if url:
                    cells[0] = '[' + cells[0] + '](<' + url + '>)'
                lines.append('| ' + ' | '.join(cells) + ' |')
            lines.append('')
        else:
            lines += ['本期无记录。' if section.get('status') in ('empty', 'complete') else '本栏暂无可核验的精选记录。', '']
    date = str(report.get('report_date', ''))
    suffix = '-' + date.replace('-', '') if re.fullmatch(r'\d{4}-\d{2}-\d{2}', date) else ''
    lines += ['金额单位：万元；营业部名称不代表已识别真实游资账户。', '',
              '[查看完整记录与来源](http://127.0.0.1:8767/latest.html#topic-night-radar' + suffix + ')']
    return '\n'.join(lines)
