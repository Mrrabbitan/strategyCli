"""Read-only, allowlisted ETF observations; never execute a screen or expose storage."""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import ipaddress
import math
import re
from urllib.parse import parse_qsl, unquote, urlsplit
from zoneinfo import ZoneInfo

from research_store import read_json, research_path

TZ = ZoneInfo('Asia/Shanghai')
BUCKETS = {'trend': '趋势延续', 'turn': '初步反转', 'repair': '修复观察',
           'weak': '趋势偏弱', 'unknown': '数据不足'}
PRIVATE = re.compile(r'file:|(?:^|\s)(?:/[^\s]+|[A-Za-z]:\\[^\s]+)|/(?:Users|home|private|tmp|var)/|'
                     r'(?:token|password|secret|api[_-]?key|账户|持仓成本)\s*[=:：]', re.I)


def _text(value, fallback='—'):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return fallback
    value = str(value)
    if PRIVATE.search(unquote(value)):
        return '私有信息不展示'
    return value[:2000]


def _e(value, fallback='—'):
    return html.escape(_text(value, fallback), quote=True)


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _num(value, digits=2, suffix='', signed=False):
    if not _finite(value):
        return '—'
    return (f'{value:+.{digits}f}' if signed else f'{value:,.{digits}f}') + suffix


def _pct(value):
    cls = 'etfm-up' if _finite(value) and value > 0 else 'etfm-down' if _finite(value) and value < 0 else 'etfm-flat'
    return f'<span class="{cls}">{_num(value, suffix="%", signed=True)}</span>'


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def _clock(value):
    if not isinstance(value, str):
        return None
    try:
        result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.replace(tzinfo=TZ) if result.tzinfo is None else result.astimezone(TZ)
    except ValueError:
        return None


def _now(value):
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=TZ) if value.tzinfo is None else value.astimezone(TZ)
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time.max, TZ)
    return dt.datetime.now(TZ)


def _url(value):
    if not isinstance(value, str) or len(value) > 2000 or re.search(r'[\x00-\x20\\]', value):
        return None
    try:
        parts = urlsplit(value)
        host = parts.hostname
        if parts.scheme != 'https' or not host or parts.username or parts.password:
            return None
        if host.lower() == 'localhost' or host.lower().endswith(('.local', '.localhost')):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            pass
        if PRIVATE.search(unquote(value)):
            return None
        if any(re.search(r'token|secret|password|credential|signature|key|auth|session', k, re.I)
               for k, _ in parse_qsl(parts.query)):
            return None
        return value
    except (ValueError, TypeError):
        return None


def _link(value, label='来源 ↗'):
    url = _url(value)
    return (f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{_e(label)}</a>'
            if url else '<span class="etfm-muted">来源链接待补</span>')


def _list(value):
    return [x for x in value if isinstance(x, str)] if isinstance(value, list) else []


def _bullets(value, empty='暂无已核验条件。'):
    rows = _list(value)
    return '<ul>' + ''.join(f'<li>{_e(x)}</li>' for x in rows) + '</ul>' if rows else f'<p class="etfm-muted">{_e(empty)}</p>'


def _valid_rows(rows, as_of, us=False):
    output, rejected, seen = [], 0, set()
    for row in rows:
        if not isinstance(row, dict):
            rejected += 1
            continue
        code = row.get('code')
        pattern = r'[A-Z][A-Z0-9.\-]{0,11}' if us else r'\d{6}'
        valid = (isinstance(code, str) and re.fullmatch(pattern, code) and code not in seen
                 and isinstance(row.get('name'), str) and row.get('as_of') == as_of
                 and isinstance(row.get('metrics'), dict)
                 and isinstance(row.get('bars', []), list))
        previous = ''
        if valid:
            for bar in row.get('bars', []):
                if (not isinstance(bar, dict) or not _date(bar.get('date'))
                        or bar['date'] > as_of or bar['date'] <= previous
                        or not _finite(bar.get('close')) or bar['close'] <= 0):
                    valid = False
                    break
                previous = bar['date']
        if not valid:
            rejected += 1
            continue
        seen.add(code)
        output.append(row)
    return output, rejected


def _spark(row):
    bars = row.get('bars', [])[-60:]
    if len(bars) < 2:
        return '<span class="etfm-muted">价格序列不足</span>'
    values = [x['close'] for x in bars]
    low, high = min(values), max(values)
    extent = high - low
    points = ' '.join(f'{4 + i * 152 / (len(values)-1):.2f},{44 - (v-low) / extent * 36:.2f}'
                      if extent else f'{4 + i * 152 / (len(values)-1):.2f},26.00'
                      for i, v in enumerate(values))
    cls = 'etfm-up' if values[-1] >= values[0] else 'etfm-down'
    title_id = 'etfm-spark-' + row['code']
    return (f'<figure class="etfm-spark {cls}"><svg viewBox="0 0 160 52" role="img" aria-labelledby="{title_id}">'
            f'<title id="{title_id}">{_e(row["name"])} 最近{len(bars)}个已保存交易日收盘走势</title>'
            '<path class="etfm-chart-baseline" d="M4 46H156"/>'
            f'<polyline points="{points}" fill="none" stroke="currentColor" stroke-width="2"/></svg>'
            f'<figcaption>{bars[0]["date"][5:]} — {bars[-1]["date"][5:]}</figcaption></figure>')


def _metrics(row, us=False):
    metrics = row['metrics']
    fields = [('收盘价', row.get('close'), ''), ('MA5', metrics.get('ma5'), ''),
              ('MA20', metrics.get('ma20'), ''), ('MA60', metrics.get('ma60'), ''),
              ('5日前MA20', metrics.get('ma20_5ago'), ''), ('20日年化波动', metrics.get('vol20'), '%'),
              ('60日最大回撤', metrics.get('drawdown60'), '%'),
              ('RS20 / SPY' if us else 'RS20 / 沪深300', metrics.get('rs20'), '个百分点')]
    return '<dl class="etfm-metrics">' + ''.join(
        f'<div><dt>{label}</dt><dd>{_num(value, 3 if suffix == "" else 2, suffix)}</dd></div>'
        for label, value, suffix in fields) + '</dl>'


def _bucket(row):
    value = row.get('bucket')
    return value if isinstance(value, str) and value in BUCKETS else 'unknown'


def _badge(row, historical=False):
    bucket = _bucket(row)
    return f'<span class="etfm-badge etfm-badge-{bucket}">{"历史 · " if historical else ""}{BUCKETS[bucket]}</span>'


def _detail(row, historical=False):
    return (f'<details id="etfm-detail-{row["code"]}" class="etfm-detail">'
            f'<summary><b>{_e(row["name"])}</b><span class="etfm-code">{row["code"]} · 展开</span></summary>'
            f'<div class="etfm-detail-body"><p>{_e(row.get("state"), BUCKETS[_bucket(row)])}</p>{_metrics(row)}'
            f'<h4>{"原报告条件" if historical else "继续观察的条件"}</h4>{_bullets(row.get("conditions"))}'
            f'<p class="etfm-invalidation"><b>失效条件</b> {_e(row.get("invalid_condition"), "尚未提供，不能据此形成参与信号")}</p>'
            f'{_bullets(row.get("missing"), "逐项缺口见覆盖说明；未列出不代表所有风险已排除。")}'
            f'<p class="etfm-note">行情日 {_e(row["as_of"])} · {_link(row.get("source"))}</p></div></details>')


def _cn_table(rows, historical):
    body = []
    for index, row in enumerate(rows):
        m = row['metrics']
        attrs = ' '.join(f'data-etf-{k}="{v if _finite(v) else ""}"'
                         for k, v in [('pct5', m.get('pct5')), ('pct20', m.get('pct20')), ('vol20', m.get('vol20'))])
        body.append(f'<tr data-etf-row data-etf-order="{index}" data-etf-code="{row["code"]}" '
                    f'data-etf-name="{_e(row["name"])}" data-etf-bucket="{_bucket(row)}" {attrs}>'
                    f'<td class="etfm-identity">{_detail(row, historical)}</td><td class="etfm-chart-cell">{_spark(row)}</td>'
                    f'<td data-label="5日">{_pct(m.get("pct5"))}</td><td data-label="20日">{_pct(m.get("pct20"))}</td>'
                    f'<td data-label="60日">{_pct(m.get("pct60"))}</td>'
                    f'<td data-label="年化波动">{_num(m.get("vol20"), suffix="%")}</td>'
                    f'<td class="etfm-state-cell" data-label="状态">{_badge(row, historical)}</td></tr>')
    return ('<div class="etfm-toolbar"><label>查找ETF<input data-etf-search type="search" placeholder="名称或代码" autocomplete="off"></label>'
            '<label>走势状态<select data-etf-filter><option value="all">全部状态</option><option value="trend">趋势延续</option>'
            '<option value="turn">初步反转</option><option value="repair">修复观察</option><option value="weak">趋势偏弱</option></select></label>'
            '<label>排序<select data-etf-sort><option value="default">观察顺序</option><option value="pct5">5日涨幅</option>'
            '<option value="pct20">20日涨幅</option><option value="vol20">20日波动</option></select></label>'
            f'<span class="etfm-result-count"><span data-etf-count>{len(rows)} 只ETF</span></span></div>'
            '<div class="etfm-table-wrap"><table class="etfm-table etfm-cn-table"><caption class="etfm-sr-only">国内ETF完整收盘走势，点击名称展开条件与指标</caption>'
            '<thead><tr><th scope="col">ETF / 详情</th><th scope="col">近60个交易日</th><th scope="col">5日</th>'
            '<th scope="col">20日</th><th scope="col">60日</th><th scope="col">年化波动</th><th scope="col">观察状态</th></tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div><p data-etf-empty class="etfm-empty"{" hidden" if rows else ""}>'
            '没有匹配的国内ETF。可清空搜索或切换为全部状态；数据不足时不补造名单。</p>')


def _candidates(rows, bucket, historical):
    candidates = [x for x in rows if _bucket(x) == bucket]
    if not candidates:
        return '<p class="etfm-empty">当前快照没有此类观察项；不为凑名单降低条件。</p>'
    return ''.join(f'<article class="etfm-candidate"><header><a href="#etfm-detail-{x["code"]}">{_e(x["name"])}</a>'
                   f'<span class="etfm-code">{x["code"]}</span></header><p>{_e(x.get("state"), BUCKETS[bucket])}</p>'
                   f'{_bullets(x.get("conditions"))}<p class="etfm-note">失效：{_e(x.get("invalid_condition"), "条件待补")}</p></article>'
                   for x in candidates)


def _heat(rows):
    usable = [x for x in rows if isinstance(x, dict) and isinstance(x.get('industry'), str)
              and type(x.get('count')) is int and x['count'] >= 0]
    usable.sort(key=lambda x: (-x['count'], -x['amount_yuan'] if _finite(x.get('amount_yuan')) else 0))
    output = []
    for n, row in enumerate(usable[:8], 1):
        evidence_id = 'etfm-heat-' + hashlib.sha256(row['industry'].encode('utf-8')).hexdigest()[:12]
        trend = row.get('trend') if isinstance(row.get('trend'), dict) else {}
        tie_note = '<small>家数并列</small>' if sum(x['count'] == row['count'] for x in usable) > 1 else ''
        stocks = []
        for stock in row.get('stocks', []) if isinstance(row.get('stocks'), list) else []:
            if isinstance(stock, str):
                stocks.append(_e(stock))
            elif isinstance(stock, dict):
                stocks.append(_e(stock.get('name', stock.get('n', ''))) + ' ' + _e(stock.get('code', stock.get('c', '')), ''))
        raw_direction = trend.get('ma20_direction')
        direction = {'up': '上行', 'down': '下行', 'rising': '上行', 'falling': '下行', 'flat': '持平'}.get(raw_direction, raw_direction) if isinstance(raw_direction, str) else None
        output.append(f'<tr><td><span class="etfm-rank">{n:02d}</span><b>{_e(row["industry"])}</b></td>'
                      f'<td>{row["count"]} 家{tie_note}</td><td>{_num(row.get("amount_yuan") / 1e8 if _finite(row.get("amount_yuan")) else None)} 亿</td>'
                      f'<td>{_pct(trend.get("pct5"))} / {_pct(trend.get("pct20"))}<small>MA20 {_e(direction, "走势待补")} · 收盘{_e(trend.get("close_vs_ma20"), "位置待补")}</small></td>'
                      f'<td><details id="{evidence_id}"><summary>名单与口径</summary><p>{"、".join(stocks) or "名单待补"}</p>'
                      f'<p>{_e(row.get("source"), "统计口径待补") if not _url(row.get("source")) else "逐股唯一行业，按源分类记录。"}</p>'
                      f'<p>{_link(row.get("source"), "涨停池来源 ↗")} · {_link(trend.get("source"), "走势来源 ↗")}</p></details></td></tr>')
    if not output:
        return '<p class="etfm-empty">缺少已核验涨停池，暂不生成板块热度排名。</p>'
    return ('<div class="etfm-table-wrap"><table class="etfm-table etfm-heat-table"><thead><tr><th>行业</th><th>涨停数</th>'
            '<th>涨停股成交额</th><th>5日 / 20日</th><th>证据</th></tr></thead><tbody>' + ''.join(output) + '</tbody></table></div>')


def _us_table(rows, historical):
    if not rows:
        return '<p class="etfm-empty">美股完整日线尚未取得，不以新闻涨幅补造强弱排名。</p>'
    body = []
    for row in rows:
        m = row['metrics']
        body.append(f'<tr><td><b>{row["code"]}</b><small>{_e(row["name"])}</small></td><td>{_pct(m.get("pct5"))}</td>'
                    f'<td>{_pct(m.get("pct20"))}</td><td>{_pct(m.get("pct60"))}</td>'
                    f'<td>{_num(m.get("rs20"), suffix=" pp", signed=True)}</td><td>{_badge(row, historical)}</td>'
                    f'<td><details id="etfm-us-{row["code"]}"><summary>指标 / 来源</summary>{_metrics(row, True)}<p>{_link(row.get("source"))}</p>'
                    f'{_bullets(row.get("missing"), "行情来源及复权口径见方法说明。")}</details></td></tr>')
    return ('<div class="etfm-table-wrap"><table class="etfm-table etfm-us-table"><thead><tr><th>美股ETF</th><th>5日</th><th>20日</th>'
            '<th>60日</th><th>RS20 / SPY</th><th>状态</th><th>详情</th></tr></thead><tbody>' + ''.join(body) + '</tbody></table></div>')


def _intraday(value, current):
    heading = ('<section id="etfm-intraday" class="etfm-section"><header class="etfm-section-heading">'
               '<div><span class="etfm-kicker">INTRADAY / OBSERVATION ONLY</span><h2>盘中复核</h2></div>'
               '<p>独立行情时点，不用收盘数据倒填</p></header>')
    if not isinstance(value, dict) or value.get('status') != 'observation_only':
        return heading + '<p class="etfm-empty">尚未取得两帧新行情。当前没有盘中确认结果，收盘观察不等于9:55条件已满足。</p></section>'
    observed = _clock(value.get('observed_at'))
    if not observed or observed > current or not isinstance(value.get('rows'), list):
        return heading + '<p class="etfm-empty">盘中时点或结构尚未核验，本次复核不可用。</p></section>'
    rows = []
    for row in value['rows']:
        if not isinstance(row, dict) or not isinstance(row.get('code'), str) or not re.fullmatch(r'\d{6}', row['code']):
            continue
        stamp = _clock(row.get('as_of'))
        if not stamp or stamp > current or not _finite(row.get('price')) or row['price'] <= 0:
            continue
        quote_stale = stamp.date() != current.date() or (current-stamp).total_seconds() > 90
        rows.append(f'<tr data-etf-quote-at="{stamp.isoformat()}"><td><b>{_e(row.get("name"))}</b><small>{row["code"]}</small></td>'
                    f'<td>{_num(row["price"], 3)}</td><td>{_pct(row.get("pct"))}</td>'
                    f'<td>{_num(row.get("vwap"), 3)}</td><td>待核验</td>'
                    f'<td><small>{stamp.strftime("%m-%d %H:%M:%S")} 北京时间</small>'
                    f'<span data-etf-quote-status>{"历史报价 · 时效不足" if quote_stale else "观察报价 · 未确认"}</span>'
                    f'{_bullets(row.get("conditions"), "观察条件尚未补齐")}</td></tr>')
    tag = '历史盘中观察 · 时效不足' if observed.date() != current.date() or (current-observed).total_seconds() > 90 else '盘中观察 · 未确认'
    content = ('<div class="etfm-table-wrap"><table class="etfm-table etfm-intraday-table"><thead><tr><th>国内ETF</th>'
               '<th>价格</th><th>涨跌幅</th><th>均价线</th><th>可比同刻量比</th><th>时点 / 条件</th></tr></thead>'
               '<tbody>' + ''.join(rows) + '</tbody></table></div>') if rows else '<p class="etfm-empty">本轮没有可展示的有效盘中记录。</p>'
    return (heading + f'<p class="etfm-note">{tag} · 取得 {observed.strftime("%Y-%m-%d %H:%M:%S")} 北京时间。'
            f'{_e(value.get("note"), "仅供观察，不自动授予参与信号。")}</p>' + content +
            '<p class="etfm-note">未取得同刻历史分钟量基准及两次独立新行情证据，不显示确认买入。'
            '日成交量或来源不明的量比不能替代可比同刻量比；此处统一标为待核验。</p></section>')


def _empty(reason):
    return ('<section class="etfm" data-etf-monitor data-etf-asof=""><header class="etfm-heading">'
            '<div><span class="etfm-kicker">ETF / MARKET OBSERVATORY</span><h1>ETF监控</h1></div>'
            '<span class="etfm-status" data-etf-validity>数据不可用</span></header>'
            f'<div class="etfm-empty etfm-empty-large"><h2>等待可核验的收盘快照</h2><p>{_e(reason)}</p>'
            '<p>这里将展示国内走势、涨停行业热度、美股对照及观察条件。尚无有效数据时，不生成排名或参与信号。</p></div></section>')


def render(report=None, now=None):
    """Render only canonical public fields; malformed snapshots cannot break other tabs."""
    current = _now(now)
    if report is None:
        report = read_json(research_path('etf_monitor/current.json'), default=None)
    if not isinstance(report, dict) or not report:
        return _empty('尚未发布ETF监控快照，或本次读取未成功。')
    if (type(report.get('schema_version')) is not int or report['schema_version'] != 1
            or any(not isinstance(report.get(k, []), list) for k in ('cn', 'us', 'heat', 'events', 'sources', 'gaps'))
            or not isinstance(report.get('coverage', {}), dict) or not isinstance(report.get('focus', {}), dict)):
        return _empty('快照结构未通过校验；保留缺口，不继续使用其中的观察状态。')
    if report.get('status') not in ('complete', 'partial', 'empty', 'ready'):
        return _empty('本次研究未完成有效发布，请等待来源与行情日期复核。')
    cn_date, us_date = _date(report.get('as_of_cn')), _date(report.get('as_of_us'))
    dates = [x for x in (cn_date, us_date) if x]
    if not cn_date or (report.get('us') and not us_date) or any(x > current.date() for x in dates):
        return _empty('行情日期缺失、无效或晚于当前日期；该快照不可用。')
    for key in ('generated_at', 'published_at'):
        if report.get(key):
            stamp = _clock(report[key])
            if not stamp or stamp > current:
                return _empty('生成或发布时间无效，或出现未来时间；该快照不可用。')
    historical = any((current.date() - x).days > 4 for x in dates)
    cn, cn_bad = _valid_rows(report.get('cn', []), report['as_of_cn'])
    us, us_bad = _valid_rows(report.get('us', []), report.get('as_of_us'), us=True)
    focus = report.get('focus', {})
    priority = next((x for x in cn if x['code'] == focus.get('priority_code')), None)
    status = '历史参考 · 需刷新' if historical else '部分覆盖 · 收盘观察' if report.get('status') == 'partial' else '收盘观察 · 非实时'
    stale = ('<p class="etfm-warning">距行情日期已超过4个自然日，以下全部仅供历史参考，需刷新后再判断。</p>' if historical else '')
    if report.get('backfill'):
        stale += '<p class="etfm-note">历史补录快照：恢复原日期事实，不视为当时已发送或实时监控记录。</p>'
    if cn_bad or us_bad:
        stale += f'<p class="etfm-warning">{cn_bad + us_bad} 条记录因结构、重复代码或行情日期异常未展示；不以缺失记录补齐排名。</p>'
    scope = report.get('coverage', {}).get('cn_scope', '固定ETF观察池；并非全市场基金排名')
    gap_items = _list(report.get('gaps'))
    if not cn:
        gap_items.append('国内完整日线覆盖为空。')
    if not us:
        gap_items.append('美股完整日线覆盖为空。')
    sources = []
    for source in report.get('sources', []):
        if isinstance(source, dict):
            sources.append(f'<li>{_link(source.get("url"), source.get("label", "数据来源 ↗"))}'
                           f'<span>行情 {_e(source.get("as_of"))} · 取得 {_e(source.get("retrieved_at"))}</span>'
                           f'<p>{_e(source.get("basis"), "口径尚未补齐")}</p></li>')
    events = []
    for event in report.get('events', []):
        if isinstance(event, dict) and isinstance(event.get('title'), str):
            events.append(f'<li><b>{_e(event["title"])}</b><p>{_e(event.get("summary"), "影响仍需验证")}</p>'
                          f'<span>{_e(event.get("date"))} · {_link(event.get("source"))}</span></li>')
    return (f'<section class="etfm" data-etf-monitor data-etf-asof="{cn_date.isoformat()}">'
            '<header class="etfm-heading"><div><span class="etfm-kicker">ETF / MARKET OBSERVATORY</span><h1>ETF监控</h1>'
            '<p>趋势、热度与海外对照，分开验证。</p></div>'
            f'<div class="etfm-heading-meta"><span class="etfm-status" data-etf-validity>{status}</span>'
            f'<p>A股 {cn_date.isoformat()} · 美股 {us_date.isoformat() if us_date else "待补"}</p></div></header>{stale}'
            '<nav class="etfm-jumps" aria-label="ETF监控区域"><a href="#etfm-cn">国内走势</a><a href="#etfm-intraday">盘中复核</a><a href="#etfm-turn">拐点观察</a>'
            '<a href="#etfm-heat">行业热度</a><a href="#etfm-us">美股对照</a><a href="#etfm-evidence">覆盖与方法</a></nav>'
            '<div class="etfm-summary"><article class="etfm-summary-focus"><span>原报告重点</span>'
            f'<strong>{_e(priority["name"]) if priority else "暂无优先观察项"}</strong>'
            f'<p>{_e(focus.get("basis"), "等待趋势、波动与来源证据齐备；热度不能替代趋势。")}</p></article>'
            f'<article><span>国内有效展示</span><strong>{len(cn)} <small>只</small></strong><p>{_e(scope)}</p></article>'
            f'<article><span>海外有效展示</span><strong>{len(us)} <small>只</small></strong><p>独立收盘时点，不与A股当日涨幅直接拼接。</p></article></div>'
            '<section id="etfm-cn" class="etfm-section"><header class="etfm-section-heading"><div><span class="etfm-kicker">01 / DOMESTIC</span>'
            '<h2>国内ETF走势</h2></div><p>看持续性，也看承受波动的代价</p></header>'
            f'{_cn_table(cn, historical)}<p class="etfm-note">涨跌幅按完整交易日计算；小图展示已保存收盘序列。不同ETF分别取自身价格轴，曲线高度不可横向比较。</p></section>'
            f'{_intraday(report.get("intraday"), current)}'
            '<section id="etfm-turn" class="etfm-section"><header class="etfm-section-heading"><div><span class="etfm-kicker">02 / TURNING POINTS</span>'
            '<h2>拐点观察</h2></div><p>初步反转仍须确认，修复不等于趋势反转</p></header><div class="etfm-duo">'
            f'<div class="etfm-panel"><h3>初步反转观察</h3>{_candidates(cn, "turn", historical)}</div>'
            f'<div class="etfm-panel"><h3>修复观察</h3>{_candidates(cn, "repair", historical)}</div></div></section>'
            '<section id="etfm-heat" class="etfm-section"><header class="etfm-section-heading"><div><span class="etfm-kicker">03 / MARKET HEAT</span>'
            '<h2>涨停行业前八</h2></div><p>先按家数，同数按涨停股成交额排序</p></header>'
            f'{_heat(report.get("heat", []))}<p class="etfm-note">每股只计一个来源行业。成交额仅为涨停股合计，并非整个行业成交或净流入。'
            '走势可能采用对应行业指数，成分口径差异见来源；没有直接对应的ETF时不强行映射。个股涨停热度不代表ETF必然上涨。</p></section>'
            '<section id="etfm-us" class="etfm-section"><header class="etfm-section-heading"><div><span class="etfm-kicker">04 / GLOBAL CONTEXT</span>'
            '<h2>美股强弱对照</h2></div><p>最近一个已完成的美股交易日</p></header>'
            f'{_us_table(us, historical)}<div class="etfm-crossmarket"><article><h3>事实层</h3><p>上表是美股ETF自身收益与均线状态。'
            'RS20表示相对SPY的20日收益差，正值也可能只是跌得更少。</p></article>'
            '<article><h3>推断层 · 需要A股验证</h3><p>海外科技、太阳能、消费等方向的变化可作为对应产业的风险偏好线索；'
            '需再核对国内ETF趋势、成分及政策。美股上涨不等于A股次日上涨，未计算相关性时不宣称统计联动。</p></article></div></section>'
            '<section class="etfm-section"><header class="etfm-section-heading"><div><span class="etfm-kicker">05 / CATALYSTS</span>'
            '<h2>政策与事件</h2></div></header>'
            + ('<ul class="etfm-events">' + ''.join(events) + '</ul>' if events else
               '<p class="etfm-empty">本次未提供已核验政策或事件。没有事件数据不代表没有政策风险，也不据此推测利好。</p>') +
            '</section><section id="etfm-evidence" class="etfm-section etfm-evidence"><header class="etfm-section-heading">'
            '<div><span class="etfm-kicker">06 / EVIDENCE</span><h2>数据覆盖与方法</h2></div></header>'
            f'<p class="etfm-note">发布状态：{status}。以上数量为实际可展示记录；不是全市场覆盖率。生成时间：{_e(report.get("generated_at"))}。</p>'
            f'<div class="etfm-duo"><div><h3>本轮缺口</h3>{_bullets(gap_items, "快照未列出额外缺口；这不构成数据完全或无风险的保证。")}</div>'
            '<div><h3>指标怎么读</h3><p>5 / 20 / 60日收益：最新收盘相对N个完整交易日前收盘的变化；不是自然日。'
            'MA是同一价格口径下的收盘均值；MA20方向比较5个交易日前。</p>'
            '<p>20日年化波动：20个日对数收益率的样本标准差 × √252；数值越大，近期波动越强。'
            '60日最大回撤：窗口内峰值到随后低点的最大跌幅；不代表未来损失上限。</p>'
            '<p>国内RS20以沪深300为基准，美股以SPY为基准；缺失显示“—”。行情复权与跨市场汇率口径以逐项来源为准。'
            '分类只是已保存规则的观察状态，不是收益保证或自动交易指令。</p></div></div>'
            f'<details id="etfm-sources" class="etfm-sources"><summary>查看来源与复权口径</summary><ul>{"".join(sources) or "<li>暂无可展示来源，需补充核验。</li>"}</ul></details>'
            '</section></section>')
