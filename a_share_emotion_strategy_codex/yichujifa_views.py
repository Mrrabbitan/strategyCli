"""Allowlisted Yichujifa evidence views; presentation never grants eligibility."""
from __future__ import annotations

import datetime as dt
import html
import math
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')


def _plain(value):
    if value is None:
        return '未提供'
    if isinstance(value, bool):
        return '是' if value else '否'
    if isinstance(value, float):
        return f'{value:,.3f}'.rstrip('0').rstrip('.') if math.isfinite(value) else '未提供'
    if isinstance(value, (list, dict)):
        return '结构化证据待核验'
    text = str(value)
    return '私有资料不在页面展示' if re.search(r'/(?:Users|home|private|var|tmp)/|file://|(?:token|password|secret)\s*[=:]', text, re.I) else text


def _e(value):
    return html.escape(_plain(value), quote=True)


def _stamp(value):
    try:
        time = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return time.replace(tzinfo=TZ) if time.tzinfo is None else time.astimezone(TZ)
    except (ValueError, TypeError):
        return None


def _items(values):
    return ''.join('<li>' + _e(x) + '</li>' for x in values if isinstance(x, (str, int, float))) if isinstance(values, list) else ''


def _links(values):
    output = []
    for row in values if isinstance(values, list) else []:
        url = row if isinstance(row, str) else row.get('url') if isinstance(row, dict) else None
        try:
            parsed = urlsplit(url)
            valid = parsed.scheme == 'https' and bool(parsed.netloc) and not parsed.username and not parsed.password and _plain(url) == url
        except (ValueError, TypeError):
            valid = False
        if valid:
            output.append('<a href="' + html.escape(url, quote=True) + '" target="_blank" rel="noopener noreferrer">原始来源 ↗</a>')
    return ' · '.join(dict.fromkeys(output))


def _table(headers, rows):
    return '<div class="table-scroll"><table><thead><tr>' + ''.join('<th>' + _e(x) + '</th>' for x in headers) + '</tr></thead><tbody>' + ''.join('<tr>' + ''.join('<td>' + _e(x) + '</td>' for x in row) + '</tr>' for row in rows) + '</tbody></table></div>'


def _kv(items):
    return '<dl class="candidate-metrics">' + ''.join('<div><dt>' + _e(label) + '</dt><dd>' + _e(value) + '</dd></div>' for label, value in items) + '</dl>'


def render_native_evidence(row, data):
    """Keep anchor prices visible even if no valid chart or in-range line exists."""
    shape = row.get('native_shape') if isinstance(row.get('native_shape'), dict) else {}
    blocks = ['<section class="yichujifa-native"><h4>一触即发专属证据</h4>']
    if row.get('branch') == 'hold_breakout' or shape:
        blocks.append('<h5>首板锚点 · 不随图表范围隐藏</h5>')
        blocks.append(_kv([('首板日期', shape.get('anchor_date')), ('首板最低价 L / 硬失效线', shape.get('anchor_low')),
                           ('首板实体中点 M / 降级线', shape.get('anchor_mid')), ('首板最高价 H / 突破线', shape.get('anchor_high')),
                           ('首板后完整交易日', shape.get('post_days')), ('整理量 / 首板量', shape.get('median_volume_ratio')),
                           ('守位状态', shape.get('status')), ('连续收盘恢复计数', shape.get('recovery_closes'))]))
        dips = shape.get('midpoint_dips')
        blocks.append('<p>实体中点降级记录：' + ('、'.join(_e(x) for x in dips) if isinstance(dips, list) and dips else '原报告未记录降级日期' if isinstance(dips, list) else '未提供') + '。</p>')
        blocks.append('<ul>' + _items(shape.get('reasons')) + '</ul>')
    announcement = row.get('announcement') if isinstance(row.get('announcement'), dict) else {}
    blocks.append('<h5>公告原文复核</h5><p>收盘复核截至 ' + _e(announcement.get('reviewed_through')) + '；不能代替当日近30分钟的盘中复核。</p>')
    blocks.append('<ul>' + (_items(announcement.get('alerts')) or '<li>未提供已核验的公告风险摘要；不等于没有风险。</li>') + '</ul>')
    for key, label in (('hard_risk', '已核验硬风险'), ('downgrade', '研究降级')):
        if announcement.get(key):
            blocks.append('<p>' + label + '：' + _e(announcement[key]) + '</p>')
    blocks.append('<p class="source-links">' + (_links(announcement.get('sources')) or '尚无可展示公告原文链接。') + '</p>')
    sector = next((s for s in data.get('sector_evidence', []) if isinstance(s, dict) and s.get('name') == row.get('sector')), {})
    if sector:
        blocks.append('<h5>所属行业固定核心与指数证据</h5>')
        index = sector.get('index') if isinstance(sector.get('index'), dict) else {}
        blocks.append(_kv([('行业', sector.get('name')), ('行业指数五日涨幅 %', index.get('return_pct')),
                           ('行业指数 MA5', index.get('ma5')), ('行业指数证据通过', index.get('verified'))]))
        cores = [x for x in sector.get('cores') or [] if isinstance(x, dict)]
        if cores:
            blocks.append(_table(['固定核心', '冻结日期', '两日涨跌 %', '收盘', 'MA5', '核验'],
                                 [[str(x.get('name') or '') + ' ' + str(x.get('code') or ''), x.get('selected_on'),
                                   x.get('return_2d_pct'), x.get('close'), x.get('ma5'), x.get('verified')] for x in cores]))
        else:
            blocks.append('<p>未取得已冻结的行业核心股证据，不以单只上涨代替板块修复。</p>')
    return ''.join(blocks) + '</section>'


def _cutoff(data, live):
    clocks = [dt.datetime.now(TZ)]
    for value in (data.get('generated_at'), live.get('time')):
        if _stamp(value):
            clocks.append(_stamp(value))
    return min(clocks)


def _frame(frame, cutoff):
    if not isinstance(frame, dict):
        return {}
    when = _stamp(frame.get('time'))
    return frame if when and when <= cutoff else {}


def _quote(frame, code):
    quote = (frame.get('quotes') or {}).get(code) if isinstance(frame.get('quotes'), dict) else None
    if not isinstance(quote, dict):
        return {}
    own, sample = _stamp(quote.get('time')), _stamp(frame.get('time'))
    return quote if own and sample and own <= sample else {}


def _elapsed(first, second):
    a, b = _stamp(first), _stamp(second)
    return (b - a).total_seconds() if a and b else None


def render_live_evidence(row, data):
    """Show three raw observations and their omissions; do not recompute a buy call."""
    live = data.get('live_review') if isinstance(data.get('live_review'), dict) else {}
    raw_frames = data.get('snapshots') if isinstance(data.get('snapshots'), list) else []
    frames = [_frame(x, _cutoff(data, live)) for x in raw_frames[-3:]]
    frames = [{}] * (3 - len(frames)) + frames
    replay = live.get('mode') == 'replay' or live.get('replay_only') or data.get('phase') == 'replay'
    blocks = ['<section class="yichujifa-live"><h4>盘中三次观察与同刻量证据</h4>']
    blocks.append('<p class="notice">' + ('历史重放：不是当前参与信号。' if replay else '按记录时点阅读；下列事实不代表条件一直有效，也不保证成交。') + '</p>')
    if not live or not all(frames):
        blocks.append('<p>尚未取得完整盘中复核：缺少9:50后的事件前基准、首次触发及至少间隔60秒的再次确认。未填入虚构行情。</p>')
    blocks.append('<p>原生复核时间 ' + _e(live.get('time')) + ' · 复核模式 ' + _e(live.get('mode')) + '。</p><ul>' + _items(live.get('reasons')) + '</ul>')
    labels = ('事件前基准', '首次触发观察', '再次确认观察')
    quotes = [_quote(frame, str(row.get('code'))) for frame in frames]
    blocks.append(_table(['观察', '采样时间', '个股自身源时间', '价格', '昨收', 'VWAP', '封板', '当日换手', '当日最低', '源核验'],
        [[label, frame.get('time'), q.get('time'), q.get('price'), q.get('previous_close'), q.get('vwap'),
          q.get('at_limit'), q.get('turnover_today'), q.get('day_low'), q.get('verified')]
         for label, frame, q in zip(labels, frames, quotes)]))
    seconds = _elapsed(quotes[1].get('time'), quotes[2].get('time'))
    blocks.append('<p>后两次个股自身行情间隔：' + _e(seconds) + ' 秒。规则要求至少60秒；相同旧报价不能重复计数，单有时间间隔仍不代表其余条件通过。</p>')
    if row.get('branch') == 'hold_breakout':
        blocks.append('<p>新事件需从未突破H转为突破H，并两次站在H和VWAP上方；不能把已经启动或持续高于H解释为新突破。</p>')
    else:
        blocks.append('<p>新事件需9:50后观察到未封板→新的换手封板或回封；持续一字不等于可买。</p>')
    index_rows = []
    for label, frame in zip(labels, frames):
        q = frame.get('emotion') if isinstance(frame.get('emotion'), dict) else {}
        own, sample = _stamp(q.get('time')), _stamp(frame.get('time'))
        if not own or not sample or own > sample:
            q = {}
        index_rows.append([label, q.get('code'), q.get('time'), q.get('price'), q.get('previous_close'), q.get('verified')])
    blocks.append('<h5>最近多板883410 · 自身时间与修复事实</h5>' + _table(['观察', '指数代码', '指数源时间', '指数值', '昨收', '核验'], index_rows))
    report = data.get('native_report') if isinstance(data.get('native_report'), dict) else {}
    peers = [x for x in report.get('pool_members', []) if isinstance(x, dict) and x.get('hybk') == row.get('sector') and x.get('c') != row.get('code') and x.get('lbc') in (1, 2)]
    peer_rows = []
    for peer in peers:
        a, b = [_quote(frame, peer.get('c')) for frame in frames[-2:]]
        if not a and not b:
            continue
        peer_rows.append([str(peer.get('n') or '') + ' ' + str(peer.get('c') or ''), a.get('time'), a.get('price'),
                          a.get('previous_close'), a.get('vwap'), b.get('time'), b.get('price'), b.get('previous_close'),
                          b.get('vwap'), _elapsed(a.get('time'), b.get('time'))])
    blocks.append('<h5>同一行业伙伴 · 后两次采样事实</h5>')
    blocks.append(_table(['伙伴', '首次源时间', '首次价', '昨收', '首次VWAP', '再次源时间', '再次价', '昨收', '再次VWAP', '间隔秒'], peer_rows) if peer_rows else '<p>未取得同一行业昨日首板/二板伙伴的两次报价，不能宣称板块已修复。</p>')
    if row.get('branch') == 'hold_breakout':
        blocks.append('<h5>守位分支同刻完整一分钟累计量</h5>')
        for label, q in zip(labels[-2:], quotes[-2:]):
            v = q.get('comparable_volume') if isinstance(q.get('comparable_volume'), dict) else {}
            minute_time, quote_time = _stamp(v.get('time')), _stamp(q.get('time'))
            if not minute_time or not quote_time or minute_time > quote_time:
                v = {}
            blocks.append('<h6>' + label + '</h6>' + _kv([('分钟量自身时间', v.get('time')), ('当前同刻累计量 / 股', v.get('volume')),
                          ('与五日基线中位数之比', v.get('ratio')), ('完整同刻数据核验', v.get('verified'))]))
            dates = v.get('baseline_dates') if isinstance(v.get('baseline_dates'), list) else []
            volumes = v.get('baseline_volumes') if isinstance(v.get('baseline_volumes'), list) else []
            baselines = [[dates[i] if i < len(dates) else None, volumes[i] if i < len(volumes) else None] for i in range(5)]
            blocks.append(_table(['此前交易日', '同一时刻累计量 / 股'], baselines))
            blocks.append('<p>' + (_links([v.get('source')]) or '尚未取得可展示的完整分钟量来源；不得用五分钟量或全天量近似。') + '</p>')
        blocks.append('<p>须完整覆盖此前五个交易日同刻一分钟累计量，达到中位数的1.2倍；缺数据不能记为0或判通过。</p>')
    else:
        blocks.append('<p>本分支观察新换手封板/回封；守位分支的同刻分钟量门槛不混用于此分支。</p>')
    last_reviews = frames[-1].get('reviews') if isinstance(frames[-1].get('reviews'), dict) else {}
    review = last_reviews.get(row.get('code')) if isinstance(last_reviews.get(row.get('code')), dict) else {}
    blocks.append('<h5>盘中当日公告与交易资格复核</h5>' + _kv([('复核时间', review.get('reviewed_through')),
                  ('当日公告检查日期', review.get('disclosures_checked_on')), ('硬风险', review.get('hard_risk')),
                  ('研究降级', review.get('downgrade'))]) + '<p>' + (_links(review.get('sources')) or '尚无本次盘中原文复核链接。') + '</p>')
    return ''.join(blocks) + '</section>'


def render_evidence(row, data):
    return render_native_evidence(row, data) + render_live_evidence(row, data)
