"""Read-only, conservative views of ETF candles and ordinary sector flow references."""
from __future__ import annotations

import datetime as dt
import html
import ipaddress
import math
import re
from urllib.parse import parse_qsl, unquote, urlsplit
from zoneinfo import ZoneInfo

from research_store import read_json, research_path
from sector_fund_alternative import THS_PUBLIC_URLS

TZ = ZoneInfo('Asia/Shanghai')
PRIVATE = re.compile(r'file:|/(?:Users|home|private|tmp|var)/|(?:^|\s)/[^\s]+|[A-Za-z]:\\|'
                     r'(?:token|password|secret|api[_-]?key)\s*[=:：]|账户|持仓成本|生活费|总资产', re.I)


def _finite(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def _text(value, fallback='—'):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return fallback
    text = str(value)
    return '私有信息不展示' if PRIVATE.search(unquote(text)) else text[:1200]


def _e(value, fallback='—'):
    return html.escape(_text(value, fallback), quote=True)


def _num(value, digits=2, suffix='', signed=False):
    if not _finite(value):
        return '—'
    return (f'{value:+,.{digits}f}' if signed else f'{value:,.{digits}f}') + suffix


def _pct(value):
    cls = 'etfm-up' if _finite(value) and value > 0 else 'etfm-down' if _finite(value) and value < 0 else 'etfm-flat'
    return f'<span class="{cls}">{_num(value, suffix="%", signed=True)}</span>'


def _amount(value):
    if not _finite(value):
        return '—'
    divisor, unit = (1e8, ' 亿') if abs(value) >= 1e8 else (1e4, ' 万') if abs(value) >= 1e4 else (1, ' 元')
    return _num(value / divisor, suffix=unit, signed=True)


def _date(value):
    try:
        return dt.date.fromisoformat(value) if isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value) else None
    except ValueError:
        return None


def _clock(value):
    try:
        result = dt.datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else None
        return result.astimezone(TZ) if result is not None and result.tzinfo is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _now(value):
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=TZ) if value.tzinfo is None else value.astimezone(TZ)
    return dt.datetime.now(TZ)


def _url(value):
    if not isinstance(value, str) or len(value) > 2000 or re.search(r'[\x00-\x20<>"\\]', value):
        return None
    try:
        parts = urlsplit(value)
        host = parts.hostname
        if parts.scheme != 'https' or not host or parts.username or parts.password or PRIVATE.search(unquote(value)):
            return None
        if host.lower() == 'localhost' or host.lower().endswith(('.local', '.localhost')):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            pass
        if any(re.search(r'token|secret|password|credential|signature|key|auth|session', key, re.I)
               for key, _ in parse_qsl(parts.query)):
            return None
        return value
    except (ValueError, TypeError):
        return None


def _link(value, label='来源 ↗'):
    url = _url(value)
    return (f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{_e(label)}</a>'
            if url else '<span class="etfm-muted">来源链接待补</span>')


def _flow_rows(value):
    """Retain the publisher's first ten slots, never sort or fill rejected slots."""
    rows, rejected, seen = [], 0, set()
    for row in value[:10] if isinstance(value, list) else []:
        if not isinstance(row, dict):
            rejected += 1; continue
        code, rank = row.get('code'), row.get('rank')
        if (not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,20}', code)
                or code in seen or not isinstance(row.get('name'), str)
                or not isinstance(rank, int) or isinstance(rank, bool) or not 1 <= rank <= 10
                or not _finite(row.get('net_yuan'))):
            rejected += 1; continue
        seen.add(code); rows.append(row)
    return rows, rejected


def _flow_table(rows, title):
    if not rows:
        return '<p class="etfm-empty">本轮该榜数据不可用；不拿历史榜或其他来源补位。</p>'
    body = ''.join(f'<tr><td><b>{_e(row["name"])}</b><small>{_e(row["code"])}</small></td>'
                   f'<td>{row["rank"]}</td><td>{_amount(row["net_yuan"])}</td>'
                   f'<td>{_pct(row.get("change_pct"))}</td></tr>' for row in rows)
    return ('<div class="etfm-table-wrap"><table class="etfm-table etfm-heat-table">'
            f'<caption class="etfm-sr-only">{title}，按原站发布顺序</caption>'
            '<thead><tr><th scope="col">行业</th><th scope="col">原始名次</th>'
            '<th scope="col">普通净额</th><th scope="col">来源涨跌幅</th></tr></thead>'
            f'<tbody>{body}</tbody></table></div>')


def _flow(value, now):
    flow = value if isinstance(value, dict) else {}
    retrieved = _clock(flow.get('retrieved_at'))
    reference = (flow.get('status') == 'reference_only' and flow.get('provider') == '同花顺'
                 and flow.get('metric') == '普通资金净额（未标主力）' and flow.get('as_of') is None
                 and flow.get('source') in THS_PUBLIC_URLS
                 and flow.get('coverage_kind') == 'publisher_top10_each'
                 and flow.get('universe_complete') is False and flow.get('is_main_metric') is False
                 and retrieved is not None and retrieved <= now)
    inflows, bad_in = _flow_rows(flow.get('inflows')) if reference else ([], 0)
    outflows, bad_out = _flow_rows(flow.get('outflows')) if reference else ([], 0)
    state = '普通净额参考 · 资金时点未知' if reference else '本轮资金榜不可用'
    warning = ('主力口径未核验 · 当日资金时间尚缺。以下为同花顺普通资金净额，原站未标为主力；'
               '即使两榜有值，也不能据此识别主力、机构或实时流向。')
    if not reference:
        warning += ' 本轮未取得可用的新榜，旧榜不冒充本轮成功。'
    if reference and (now-retrieved).total_seconds() > 4 * 86400:
        warning += ' 采集已超过4日，以下仅留历史参考。'
    rejected = f'<p class="etfm-note">{bad_in + bad_out}个原始位置因字段不足未展示；不取第十一名补位，不改写原名次。</p>' if bad_in + bad_out else ''
    return ('<section id="etfp-flow" class="etfm-section"><header class="etfm-section-heading"><div>'
            '<span class="etfm-kicker">ORDINARY FLOW / REFERENCE</span><h2>净流入与净流出参考</h2></div>'
            f'<span class="etfm-badge">{state}</span></header><p class="etfm-warning">{warning}</p>'
            '<p class="etfm-note">资金自身日期 / 时刻：未知（不以采集时间代替）。'
            f'采集时间：{_e(retrieved.isoformat() if retrieved else None)}。'
            '覆盖：原站各前十，非全行业；保留原站顺序，不重排、不补位。</p>'
            f'<p class="etfm-note">本轮状态：{_e(flow.get("note"), "未取得本轮可核验资金证据")} '
            f'已展示流入{len(inflows)}/10项、流出{len(outflows)}/10项。</p>'
            '<div class="etfm-duo"><div class="etfm-panel" id="etfp-inflows"><h3>净流入参考前十</h3>'
            + _flow_table(inflows, '净流入参考前十') + '</div>'
            '<div class="etfm-panel" id="etfp-outflows"><h3>净流出参考前十</h3>'
            + _flow_table(outflows, '净流出参考前十') + '</div></div>' + rejected +
            f'<p class="etfm-note">指标：普通资金净额（未标主力），金额以人民币元换算。{_link(flow.get("source"), "同花顺原站 ↗")}</p></section>')


def _candles(value, as_of):
    rows, rejected, seen = [], 0, set()
    for row in value if isinstance(value, list) else []:
        if not isinstance(row, dict):
            rejected += 1; continue
        code = row.get('code')
        if (not isinstance(code, str) or not re.fullmatch(r'\d{6}', code) or code in seen
                or not isinstance(row.get('name'), str) or row.get('as_of') != as_of):
            rejected += 1; continue
        seen.add(code)
        o, c, h, low = (row.get(key) for key in ('open', 'close', 'high', 'low'))
        valid = (all(_finite(value) and value > 0 for value in (o, c, h, low))
                 and h >= max(o, c) and low <= min(o, c) and h >= low)
        computed = None
        body = body_range = position = None
        if valid:
            body = 100*(c/o-1)
            if h > low:
                body_range, position = 100*abs(c-o)/(h-low), 100*(c-low)/(h-low)
                computed = body <= -2 + 1e-9 and body_range >= 60 - 1e-9 and position <= 25 + 1e-9
            else:
                computed = False
        # The renderer can reject a contradictory flag, but cannot manufacture
        # a missing engine result from a plausible shape.
        flag = row.get('is_large_bear')
        verified = valid and isinstance(flag, bool) and flag == computed
        rows.append(dict(row, _body=body, _body_range=body_range, _position=position,
                         _verified=verified, _large=bool(verified and flag)))
    return rows, rejected


def _candle_table(rows, current):
    if not rows:
        return '<p class="etfm-empty">缺少可展示的已核验ETF日线，不能判断大阴线。</p>'
    body = []
    for row in rows:
        label = ('符合大阴线' if row['_large'] else '未达大阴线' if row['_verified'] else '待核验 · 形态字段不足或冲突')
        if not current:
            label = '历史 / 待核验 · ' + label
        cls = 'etfm-badge-weak' if row['_large'] and current else 'etfm-badge'
        missing = [item for item in row.get('missing', []) if isinstance(item, str)] if isinstance(row.get('missing'), list) else []
        details = ''.join(f'<li>{_e(item)}</li>' for item in missing)
        body.append(f'<tr><td class="etfm-identity"><details id="etfp-candle-{row["code"]}" class="etfm-detail">'
                    f'<summary><b>{_e(row["name"])}</b><span class="etfm-code">{row["code"]} · {_e(row.get("as_of"))}</span></summary>'
                    '<div class="etfm-detail-body">'
                    f'<p>开 {_num(row.get("open"), 3)} · 收 {_num(row.get("close"), 3)} · 高 {_num(row.get("high"), 3)} · 低 {_num(row.get("low"), 3)}</p>'
                    f'<p>价格口径：{_e(row.get("price_basis"), "复权口径待核验")}。{_link(row.get("source"))}</p>'
                    f'<ul>{details or "<li>快照未列额外缺口，不代表已完成全市场及资金核验。</li>"}</ul></div></details></td>'
                    f'<td>{_pct(row.get("change_pct"))}<small>相对昨收</small></td>'
                    f'<td>{_pct(row["_body"])}<small>收盘 / 开盘 − 1</small></td>'
                    f'<td>{_num(row["_body_range"], suffix="%")}</td><td>{_num(row["_position"], suffix="%")}</td>'
                    f'<td>{_num(row.get("volume_ratio20"), suffix=" 倍")}<small>完整日量 / 前20日均量</small></td>'
                    f'<td><span class="etfm-badge {cls}">{label}</span></td></tr>')
    return ('<div class="etfm-table-wrap"><table class="etfm-table etfm-heat-table">'
            '<caption class="etfm-sr-only">ETF代理样本的大阴线与实体复核</caption><thead><tr>'
            '<th scope="col">ETF代理</th><th scope="col">昨收涨跌</th><th scope="col">实体跌幅</th>'
            '<th scope="col">实体占振幅</th><th scope="col">收盘位置</th><th scope="col">20日量比</th>'
            '<th scope="col">形态结论</th></tr></thead><tbody>' + ''.join(body) + '</tbody></table></div>')


def render(report=None, now=None):
    """Render only the supplied/private snapshot; no collection or execution."""
    now = _now(now)
    report = read_json(research_path('etf_pressure/current.json'), {}) if report is None else report
    if (not isinstance(report, dict) or report.get('schema_version') != 1
            or isinstance(report.get('schema_version'), bool)):
        return ('<section id="etfm-pressure" class="etfm etfm-section"><h2>资金与大阴线观察</h2>'
                '<p class="etfm-empty">本轮数据不可用，等待可核验的新快照；未取得数据不等于没有资金流出或大阴线。</p></section>')
    day, generated = _date(report.get('as_of')), _clock(report.get('generated_at'))
    old = day is not None and (now.date()-day).days > 4
    future = day is None or day > now.date() or generated is None or generated > now
    complete = (day is not None and now >= dt.datetime.combine(day, dt.time(15), TZ)
                and generated is not None and generated >= dt.datetime.combine(day, dt.time(15), TZ))
    current = not (old or future) and complete
    rows, rejected = _candles(report.get('candles'), day.isoformat() if day else None)
    verified = [row for row in rows if row['_verified']]
    large = [row for row in verified if row['_large']]
    selected = large or sorted(rows, key=lambda row: (row.get('change_pct') if _finite(row.get('change_pct')) else float('inf'),
                                                     row['_body'] if _finite(row['_body']) else float('inf')))[:3]
    if future:
        validity = '日期异常或未来时点 · 仅留存档，不作当前判断'
    elif old:
        validity = '历史参考 · 行情已超过4日，需更新'
    elif not complete:
        validity = '尚未取得完整收盘 · 不确认当日大阴线'
    else:
        validity = '已完成收盘样本 · 非实时信号'
    count_note = (f'已核验{len(verified)}个ETF代理样本，其中{len(large)}只符合形态。' if large else
                  f'已核验{len(verified)}个ETF代理样本未见符合；以下列当日涨跌最弱{len(selected)}只供复核。' if verified else
                  '尚无形态字段一致的已核验样本；以下仅列可展示记录供复核。')
    if not large and selected and all(_finite(row.get('change_pct')) and row['change_pct'] >= 0 for row in selected):
        count_note += ' 所列涨跌幅均非负，只是样本排序，不代表明显下跌。'
    if not current:
        count_note = '历史 / 待核验记录，不能确认本轮大阴线。' + count_note
    if rejected:
        count_note += f' {rejected}条记录因代码、重复或行情日期不一致未展示。'
    gaps = [gap for gap in report.get('gaps', []) if isinstance(gap, str)] if isinstance(report.get('gaps'), list) else []
    gap_html = ''.join(f'<li>{_e(gap)}</li>' for gap in gaps)
    return ('<section id="etfm-pressure" class="etfm etfm-section"><header class="etfm-section-heading"><div>'
            '<span class="etfm-kicker">FLOW &amp; PRICE PRESSURE</span><h2>资金流向与大阴线观察</h2></div></header>'
            + _flow(report.get('flow'), now) +
            '<section id="etfp-bear" class="etfm-section"><header class="etfm-section-heading"><div>'
            '<span class="etfm-kicker">COMPLETED DAILY CANDLES</span><h2>大阴线 · ETF代理复核</h2></div>'
            f'<span class="etfm-badge">{validity}</span></header>'
            f'<p class="etfm-note">价格日期：{_e(report.get("as_of"))} · 计算时间：{_e(report.get("generated_at"))}。'
            f'{_e(report.get("universe_label"), "固定ETF代理样本")}；不是全行业扫描。</p>'
            '<p class="etfm-note">初始形态条件：实体（收盘 / 开盘 − 1）≤ −2%，实体占高低振幅≥60%，收盘位于日振幅下25%。'
            '需完整收盘；阈值是研究参数，未验证收益优势。实体跌幅与相对昨收涨跌分列，跳空不会被混为同一指标。</p>'
            f'<p class="etfm-warning">{count_note}</p>{_candle_table(selected, current)}'
            '<p class="etfm-note">无符合时列当日跌幅较大样本，按相对昨收涨跌从弱到强列最多3只；仅是已覆盖样本复核顺序，不能称全行业没有大阴线。'
            '下跌日收阳并不等于大阴，也不自动证明止跌。放量只说明交易活跃，不能由价格或量比识别具体机构、游资或主力撤退。</p></section>'
            '<details id="etfp-quadrants" class="etfm-sources"><summary>资金与价格四象限怎么读</summary>'
            '<div class="etfm-duo"><article><h3>价格走强</h3><p>若同一日期、同一成分的可靠净流入也增强，可复核趋势承接；'
            '若资金净流出，则检查量价背离与兑现压力，不能据此直接卖出。</p></article>'
            '<article><h3>价格走弱</h3><p>若可靠资金同刻净流出，可提高风险复核优先级；若净流入，则检查承接或下跌中换手，'
            '不能据此直接抄底。</p></article></div>'
            '<p>本页普通净额资金时点未知、ETF与行业成分不同，四象限暂不合并计算；大阴线不能直接命名为资金撤退。'
            '原站参考榜与日线形态均不构成确认买入或自动退出信号。</p></details>'
            f'<details id="etfp-gaps" class="etfm-sources"><summary>覆盖与待核验项</summary><ul>{gap_html or "<li>需继续核验资金自身时间、主力口径、全行业覆盖与价格复权一致性。</li>"}</ul></details>'
            '</section>')
