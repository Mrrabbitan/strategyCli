"""Read-only ETF candle and volume-pressure observations; no flow payload rendering."""
from __future__ import annotations

import datetime as dt
import html
import ipaddress
import math
import re
from urllib.parse import parse_qsl, unquote, urlsplit
from zoneinfo import ZoneInfo

from research_store import read_json, research_path

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


def _price_gaps(value):
    """Do not resurrect retired flow modules through legacy snapshot notes."""
    if not isinstance(value, list):
        return []
    flow_context = re.compile(r'资金|普通(?:资金)?净额|(?:流入|流出).*0\s*/\s*(?:10|20)|历史.*日榜.*0\s*/\s*20', re.I)
    price_context = re.compile(r'日线|报价|复权|价格|OHLC|quote|qfq|\bprice\b', re.I)
    legacy_source_failure = re.compile(r'(?:同花顺|\bTHS\b).*(?:timeout|超时|失败|不可用)', re.I)
    return [gap for gap in value if isinstance(gap, str) and not flow_context.search(gap)
            and not (legacy_source_failure.search(gap) and not price_context.search(gap))]


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
        missing = _price_gaps(row.get('missing'))
        details = ''.join(f'<li>{_e(item)}</li>' for item in missing)
        body.append(f'<tr><td class="etfm-identity"><details id="etfp-candle-{row["code"]}" class="etfm-detail">'
                    f'<summary><b>{_e(row["name"])}</b><span class="etfm-code">{row["code"]} · {_e(row.get("as_of"))}</span></summary>'
                    '<div class="etfm-detail-body">'
                    f'<p>开 {_num(row.get("open"), 3)} · 收 {_num(row.get("close"), 3)} · 高 {_num(row.get("high"), 3)} · 低 {_num(row.get("low"), 3)}</p>'
                    f'<p>价格口径：{_e(row.get("price_basis"), "复权口径待核验")}。{_link(row.get("source"))}</p>'
                    f'<ul>{details or "<li>快照未列额外缺口，不代表已完成全市场价格核验。</li>"}</ul></div></details></td>'
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
        return ('<section id="etfm-pressure" class="etfm etfm-section"><h2>大阴线与量价压力</h2>'
                '<p class="etfm-empty">本轮数据不可用，等待可核验的新快照；未取得数据不等于没有大阴线。</p></section>')
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
    gaps = _price_gaps(report.get('gaps'))
    gap_html = ''.join(f'<li>{_e(gap)}</li>' for gap in gaps)
    return ('<section id="etfm-pressure" class="etfm etfm-section"><header class="etfm-section-heading"><div>'
            '<span class="etfm-kicker">CANDLES &amp; VOLUME PRESSURE</span><h2>大阴线与量价压力</h2></div></header>'
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
            '<p>与<a href="#etf-em-history">独立东财历史资金</a>联合复核前，须对齐日期、行业分类与ETF成分范围；'
            '映射未核验时不合并计算。大阴线不能直接命名为资金撤退，历史资金与日线形态不自动合成交易信号。</p></details>'
            f'<details id="etfp-gaps" class="etfm-sources"><summary>覆盖与待核验项</summary><ul>{gap_html or "<li>需继续核验ETF代理范围、完整日线日期与价格复权一致性。</li>"}</ul></details>'
            '</section>')
