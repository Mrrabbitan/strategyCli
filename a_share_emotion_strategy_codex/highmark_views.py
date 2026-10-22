"""Concise, read-only views of regulatory-threshold research, never trade limits."""
from __future__ import annotations

import datetime as dt
import html
import math
import re
from urllib.parse import parse_qsl, unquote, urlsplit
from zoneinfo import ZoneInfo

from research_store import read_json, research_path


TZ = ZoneInfo('Asia/Shanghai')
ANCHOR = 'topic-highmark-abnormal'
PAGE_URL = 'http://127.0.0.1:8767/latest.html#page-highmark'
REFERENCE = '滚动参考，公告重置未全量核验'
WINDOWS = (('3', 20), ('10', 100), ('30', 200))
PHASES = {'preopen': '盘前', 'close': '盘后'}
BAD_METRICS = frozenset(('unavailable', 'insufficient', 'failed', 'invalid',
                         'not_applicable', 'missing', 'error'))


def _text(value):
    if value is None or isinstance(value, (dict, list, tuple)):
        return '—'
    text = str(value)
    if re.search(r'/(?:Users|home|private|var|tmp)/|file://|(?:password|token|secret)\s*[=:]', text, re.I):
        return '本地证据已留档'
    return text.replace('\r', ' ').replace('\n', ' ') or '—'


def _e(value):
    return html.escape(_text(value), quote=True)


def _md(value):
    text = html.escape(_text(value), quote=False).replace('\\', '\\\\')
    for symbol in ('|', '[', ']', '`', '*', '_'):
        text = text.replace(symbol, '\\' + symbol)
    return text


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _stamp(value):
    try:
        parsed = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.astimezone(TZ) if parsed.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def _clock(value):
    parsed = _stamp(value)
    return parsed.strftime('%Y-%m-%d %H:%M') if parsed else _text(value)


def _safe_url(value):
    if not isinstance(value, str) or re.search(r'[\s<>"\\]', value):
        return ''
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or _text(unquote(value)) != unquote(value)
                or any(re.search(r'password|token|secret|api.?key|credential', key, re.I)
                       for key, _ in parse_qsl(parsed.query))):
            return ''
        return value
    except ValueError:
        return ''


def _rows(report):
    raw = report.get('rows')
    return [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []


def _metric(row, key):
    metrics = row.get('metrics')
    metric = metrics.get(key, metrics.get(int(key), {})) if isinstance(metrics, dict) else {}
    if not isinstance(metric, dict):
        return {}
    return metric


def _metric_value(metric):
    if str(metric.get('status', '')).lower() in BAD_METRICS:
        return None
    return _number(metric.get('value', metric.get('deviation_pct')))


def _display_rows(report):
    """Keep current 2+ ladders and retained names near a severe threshold."""
    primary, other = [], []
    for row in _rows(report):
        boards = _number(row.get('boards'))
        include = 'boards' not in row or (boards is not None and boards >= 2)
        if not include:
            for key in ('10', '30'):
                metric = _metric(row, key)
                headroom = _number(metric.get('headroom_pp'))
                if (_metric_value(metric) is not None and headroom is not None
                        and headroom <= 30):
                    include = True
                    break
        (primary if include else other).append(row)
    return primary, other


def _metric_cell(row, key, threshold):
    metric = _metric(row, key)
    value = _metric_value(metric)
    if value is None:
        return '—', ''
    actual_threshold = _number(metric.get('threshold'))
    actual_threshold = threshold if actual_threshold is None else actual_threshold
    headroom = _number(metric.get('headroom_pp'))
    headroom = actual_threshold - value if headroom is None else headroom
    distance = (f'距阈值 {headroom:.2f} 个百分点' if headroom >= 0
                else f'已超阈值 {abs(headroom):.2f} 个百分点')
    return f'{value:+.2f}%', distance


def _next_day(row):
    candidates = []
    for key, _ in WINDOWS:
        metric = _metric(row, key)
        if _metric_value(metric) is None:
            continue
        value = _number(metric.get('next_day_flat_index_pct'))
        if value is not None:
            candidates.append((value, int(key)))
    if not candidates:
        return '—'
    value, days = min(candidates)
    return f'约 {value:+.2f}% · {days}日窗口'


def _stock(row):
    return ' '.join(_text(row.get(k)) for k in ('name', 'code') if row.get(k) is not None) or '—'


def _state(row):
    boards = _number(row.get('boards'))
    label = (f'{boards:g}连板' if boards and boards > 0 else '断板／趋势跟踪')
    if row.get('missing'):
        label += ' · 证据不齐'
    pending = row.get('pending_reset_kinds') or []
    if pending:
        label += ' · 下一交易日' + ('严重' if 'severe' in pending else '普通') + '重置（转载）'
    return label


def _reference(report):
    alignment = report.get('reset_alignment') or {}
    if isinstance(alignment, dict) and alignment.get('provider_records_complete') is True:
        return '已按公开转载记录对齐重置，仍为研究测算，未全量核实交易所记录'
    return REFERENCE


def _coverage(report):
    coverage = report.get('coverage')
    coverage = coverage if isinstance(coverage, dict) else {}
    values = [_number(coverage.get(key)) for key in ('scanned_count', 'total_count')]
    counts = '/'.join(f'{v:g}' if v is not None else '—' for v in values)
    scope = '当期涨停池完整' if coverage.get('pool_complete') is True else '当期涨停池覆盖不足'
    return f'完整计算 {counts} 只 · {scope}'


def _status(report):
    return {'complete': '本轮计算完成', 'partial': '部分证据缺失',
            'unavailable': '本轮数据不可用'}.get(report.get('status'), '尚未执行')


def _sources(report):
    raw = report.get('sources')
    result = []
    for source in raw if isinstance(raw, list) else []:
        if not isinstance(source, dict):
            continue
        url = _safe_url(source.get('url'))
        if url:
            result.append((_text(source.get('label')) if source.get('label') else '公开来源', url))
    return list(dict.fromkeys(result))


def _compact_sources(report):
    """The message links core market evidence and rules, not every quote request."""
    sources = _sources(report)
    selected = {}
    for label, url in sources:
        parsed = urlsplit(url)
        host, path = parsed.hostname or '', parsed.path.lower()
        if 'gettopicztpool' in path or '收盘涨停池' in label:
            selected.setdefault('pool', ('收盘涨停池', url))
        elif host in ('qt.gtimg.cn', 'web.ifzq.gtimg.cn'):
            selected.setdefault('market', ('腾讯日线与收盘报价', url))
        elif (host == 'sse.com.cn' or host.endswith('.sse.com.cn')) and ('规则' in label or 'law' in path):
            selected.setdefault('sse', ('上交所规则', url))
        elif (host == 'szse.cn' or host.endswith('.szse.cn')) and ('规则' in label or 'law' in path):
            selected.setdefault('szse', ('深交所规则', url))
    if selected:
        return [selected[key] for key in ('pool', 'market', 'sse', 'szse') if key in selected]
    return sources[:4]


def _window_lines(row):
    result = []
    for key, _ in WINDOWS:
        metric = _metric(row, key)
        if _metric_value(metric) is None:
            continue
        start = _text(metric.get('start', metric.get('max_start')))
        end = _text(metric.get('end', metric.get('max_end')))
        days = _number(metric.get('days', metric.get('length')))
        span = f'{start}—{end}'
        if days is not None:
            span += f'（{days:g}个交易日）'
        result.append(f'{key}日：{span}')
    return result


def _table(rows):
    body = ['<div class="table-scroll" style="overflow-x:auto"><table><thead><tr>'
            '<th>股票</th><th>连板／状态</th><th>3日偏离／距20</th>'
            '<th>10日偏离／距100</th><th>30日偏离／距200</th>'
            '<th>次日触线情景</th></tr></thead><tbody>']
    for row in rows:
        body.append(f'<tr><td><b>{_e(row.get("name"))}</b><small>{_e(row.get("code"))}</small></td><td>{_e(_state(row))}</td>')
        for key, threshold in WINDOWS:
            value, gap = _metric_cell(row, key, threshold)
            body.append(f'<td>{_e(value)}' + (f'<small>{_e(gap)}</small>' if gap else '') + '</td>')
        body.append(f'<td>{_e(_next_day(row))}</td></tr>')
    body.append('</tbody></table></div>')
    return ''.join(body)


def render(report=None, now=None):
    """Read saved research only; the view never requests market data."""
    report = read_json(research_path('highmark/current.json'), {}) if report is None else report
    report = report if isinstance(report, dict) else {}
    prefix = f'<section class="playbook highmark-monitor" id="{ANCHOR}"><h2>高标异动 · 盘前／盘后</h2>'
    if not report:
        return prefix + '<p>尚未执行；取得行情后展示，不补造名单。</p></section>'
    actual = _stamp(now) if now is not None else dt.datetime.now(TZ)
    generated = _stamp(report.get('generated_at'))
    if generated and actual and generated > actual:
        return prefix + '<p>报告时点异常，本轮不展示异动数值。</p></section>'
    phase = PHASES.get(report.get('phase'), '按需')
    body = [prefix, f'<p><b>{_e(report.get("date"))} · {phase} · {_status(report)}</b>'
            f' · 行情日 {_e(report.get("trade_date"))} · 行情截至 {_e(_clock(report.get("as_of")))}'
            f' · 计算 {_e(_clock(report.get("generated_at")))}</p>',
            f'<p class="muted">{_e(_coverage(report))}。{_reference(report)}；阈值距离不是可上涨额度，触线不代表必然停牌。</p>']
    rows = _rows(report)
    primary, other = _display_rows(report)
    if rows:
        body.append('<p class="muted">主表：当期两板及以上，以及10日或30日距阈值不超过30个百分点的保留股。</p>')
        body.append(_table(primary) if primary else '<p>当前主表没有达到展示门槛的记录；其余跟踪股保留在下方。</p>')
        body.append('<p class="muted">次日情景假设对应基准指数涨幅为0，且沿用当前滚动窗口；指数变化、窗口移出、除权及公告重置都会改变结果，不能据此推算实际涨停价。</p>')
        if other:
            body.append(f'<details id="highmark-other"><summary>其余跟踪股 · {len(other)}只</summary>{_table(other)}</details>')
    else:
        body.append('<p>未取得可展示的高标异动结果；不表示市场没有高标或异动风险。</p>')
    if report.get('status') in ('partial', 'unavailable') or report.get('missing'):
        body.append('<p>部分行情或公告证据未齐，缺失数值留空，已取得结果独立保留。</p>')
    if report.get('build_status') == 'failed':
        body.append('<p>本轮页面同步未完成；当前内容为已保存研究。</p>')
    details = []
    for row in rows:
        windows = _window_lines(row)
        note = row.get('reset_note')
        if windows or note:
            details.append('<li><b>' + _e(_stock(row)) + '</b>：' + _e('；'.join(windows))
                           + ('；' + _e(note) if note else '') + '</li>')
    links = ''.join(f'<li><a href="{_e(url)}" target="_blank" rel="noopener noreferrer">{_e(label)}</a></li>'
                    for label, url in _sources(report))
    if details or links:
        body.append(f'<details id="highmark-evidence"><summary>计算窗口与来源</summary><ul>{"".join(details)}</ul><ul>{links}</ul></details>')
    body.append('</section>')
    return ''.join(body)


def render_markdown(report):
    """Return the exact compact chat edition; safe links and no private payloads."""
    report = report if isinstance(report, dict) else {}
    phase = PHASES.get(report.get('phase'), '按需')
    lines = [f'**高标异动 · {_md(report.get("date"))} {phase}**', '',
             f'行情日 {_md(report.get("trade_date"))}；行情截至 {_md(_clock(report.get("as_of")))}；计算 {_md(_clock(report.get("generated_at")))}。{_md(_coverage(report))}。', '',
             f'{_reference(report)}；阈值距离不是可上涨额度，触线不代表必然停牌。', '']
    rows, other = _display_rows(report)
    if rows:
        lines += ['|股票|连板／状态|3日偏离／距20|10日偏离／距100|30日偏离／距200|次日触线情景|',
                  '|---|---|---|---|---|---|']
        for row in rows:
            cells = [_stock(row), _state(row)]
            for key, threshold in WINDOWS:
                value, gap = _metric_cell(row, key, threshold)
                cells.append(value + ('；' + gap if gap else ''))
            cells.append(_next_day(row))
            lines.append('|' + '|'.join(_md(cell) for cell in cells) + '|')
        lines += ['', '次日情景仅假设基准指数涨幅为0；指数变化、窗口移出、除权及公告重置都会改变结果，不据此推算实际涨停价。']
    else:
        lines += (['当前主表没有达到展示门槛的记录；其余跟踪股见页面。'] if other else
                  ['未取得可展示的高标异动结果；不表示市场没有高标或异动风险。'])
    if other:
        lines += ['', f'其余 {len(other)} 只跟踪股见页面展开。']
    if report.get('status') in ('partial', 'unavailable') or report.get('missing'):
        lines += ['', '部分行情或公告证据未齐，缺失数值留空。']
    if report.get('build_status') == 'failed':
        lines += ['', '页面同步失败，未声称同步完成。']
    links = [f'[{_md(label)}](<{url}>)' for label, url in _compact_sources(report)]
    links.append(f'[本地页面]({PAGE_URL})')
    lines += ['', ' · '.join(links)]
    return '\n'.join(lines)
