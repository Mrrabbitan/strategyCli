"""Concise daily radar lists; read saved evidence without running stock screens."""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import re

from research_modules import TZ, stamp
from research_views import e, finite, links
from sector_radar_daily import build_daily_model, load_daily_reports

LISTS = (('review', '优先复核'), ('observed', '正式观察'), ('started', '已启动'))
STATE_NAMES = {'complete': '研究完成', 'partial': '部分核验',
               'empty': '筛选完成 · 正式观察为空', 'expired': '历史研究',
               'unavailable': '本轮研究受阻', 'not_run': '尚未执行'}


def records(value):
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def texts(value):
    return list(dict.fromkeys(str(x) for x in value if isinstance(x, (str, int, float)))) if isinstance(value, list) else []


def number(value):
    return f'{value:.2f}' if finite(value) else '待核验'


def key(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:16]


def clock_label(value):
    parsed = stamp(value)
    return parsed.astimezone(TZ).strftime('%m-%d %H:%M') if parsed else '未提供'


def brief_reason(value):
    """Shorten known report wording for display only; retain originals in details."""
    value = str(value)
    match = re.search(r'毛空间上限仅\s*([\d.]+).*小于原规则净空间≥2', value)
    if match:
        return f'毛空间上限 {match.group(1)}＜2，空间不足'
    if value == '试盘后至少两日缩量承接':
        return '缩量承接尚未满足至少两日'
    return value


def row_reason(row, kind):
    failures = texts(row.get('failure_reasons'))
    if failures:
        return '；'.join(brief_reason(x) for x in failures[:2])
    if kind == 'observed' and row.get('observation_origin') == 'prelaunch_core':
        return '原生潜伏核心已核验；独立于板块量价前三'
    if kind == 'observed':
        return '、'.join(x['label'] for x in records(row.get('supporting_skills'))) + '支持收盘观察'
    if kind == 'started':
        return '已脱离本轮潜伏阶段，不列低位第一候选'
    missing = texts(row.get('missing'))
    if missing:
        return '待补：' + '；'.join(x.split('：')[0] for x in missing[:2])
    return '；'.join(texts(row.get('reasons'))[:2]) or '原报告列为优先补证，尚未入选'


def row_card(row, kind, day, legacy_ids):
    code = str(row.get('code') or '')
    token = f'{day}-{kind}-{key(code)}'
    groups = ' / '.join(str(x.get('label') or '') for x in records(row.get('groups')))
    failures = texts(row.get('failure_reasons'))
    status = ('未入选 · 条件未满足' if failures else '未入选 · 待补证') if kind == 'review' else ('收盘观察 · 非买点' if kind == 'observed' else '跟踪，不追认潜伏资格')
    if row.get('observation_origin') == 'prelaunch_core':
        status = '潜伏核心 · 非板块前三排名'
    if row.get('historical'):
        status = '历史 · ' + status
    levels = records(row.get('levels'))
    levels_text = ' · '.join(e(x.get('label')) + ' ' + number(x.get('value')) for x in levels[:3])
    next_checks = texts(row.get('next_check'))
    next_text = ('仅复核结构变化，不放宽原门槛。' if failures else next_checks[0] if next_checks else '等待新的完整证据，不据此直接交易。')
    reason = row_reason(row, kind)
    original = texts(row.get('reasons'))
    missing = texts(row.get('missing'))
    risks = texts(row.get('risks'))
    more = ''
    for title, values in (('原始依据', original), ('尚待核验', missing), ('后续确认', next_checks), ('失效与风险', risks)):
        if values:
            more += '<p><b>' + title + '</b> ' + '；'.join(e(x) for x in values) + '</p>'
    more += '<p>' + links(row.get('sources')) + '</p>' if row.get('sources') else ''
    legacy = ''
    if code not in legacy_ids and re.fullmatch(r'\d{6}', code):
        legacy = f'<span class="radar-anchor" id="radar-stock-{code}"></span>'
        legacy_ids.add(code)
    r5 = f'{row["r5"]:+.2f}%' if finite(row.get('r5')) else '—'
    r20 = f'{row["r20"]:+.2f}%' if finite(row.get('r20')) else '—'
    return f'''<article class="radar-daily-row" data-radar-stock="{e(code)}">{legacy}
<div class="radar-stock-name"><b>{e(row.get('name'))}</b><span>{e(code)} · {e(groups)}</span><small class="radar-row-status">{e(status)}</small></div>
<div class="radar-close"><b>{number(row.get('price'))}</b><small>5日 {e(r5)} / 20日 {e(r20)}</small></div>
<div class="radar-decision"><p>{e(reason)}</p><small>{levels_text or e(next_text)}</small>
<details id="radar-note-{token}"><summary>核验要点与来源</summary>{more or '<p>该日没有保存更多证据。</p>'}</details></div></article>'''


def list_panel(model, kind, label, legacy_ids):
    day = model.get('signal_date') or 'unknown'
    rows = records(model.get(kind))
    empty = {'review': '该日没有保存优先复核标的，不用落选股补位。',
             'observed': '暂无完整核验通过的正式观察股；优先复核不等于入选。',
             'started': '该日没有保存已启动跟踪标的。'}[kind]
    if kind == 'observed' and model.get('status') == 'empty':
        empty = '筛选完成，正式观察为空。'
    hint = {'review': '低位形态优先看；以下尚未入选，先核对阻碍。',
            'observed': '原策略完整支持的收盘观察，仍需后续确认。',
            'started': '已启动单列，不混入低位待启动候选。'}[kind]
    content = ''.join(row_card(row, kind, day, legacy_ids) for row in rows)
    heading = '<div class="radar-column-head" aria-hidden="true"><span>标的 / 板块</span><span>收盘价 / 阶段涨幅</span><span>关键判断 / 结构位置</span></div>' if rows else ''
    return f'''<section id="radar-list-{day}-{kind}" data-radar-list="{kind}" role="tabpanel" aria-labelledby="radar-tab-{day}-{kind}" tabindex="0" {'hidden' if kind != 'review' else ''}>
<p class="radar-list-hint">{e(hint)}</p>{heading}{content or '<p class="radar-empty">' + empty + '</p>'}</section>'''


def day_panel(model, selected, legacy_ids):
    day = model['signal_date']
    historical = model.get('historical', False)
    lists = ''.join(f'<button type="button" id="radar-tab-{day}-{kind}" role="tab" data-radar-list-tab="{kind}" aria-selected="{str(kind == "review").lower()}" aria-controls="radar-list-{day}-{kind}" tabindex="{0 if kind == "review" else -1}">{label}<span>{len(records(model.get(kind)))}</span></button>' for kind, label in LISTS)
    coverage = model.get('coverage') or {}
    verified = coverage.get('membership_verified')
    expected = coverage.get('sectors_expected')
    coverage_note = f'成分已核验 {verified}/{expected} 板块；' if isinstance(verified, int) and isinstance(expected, int) else ''
    state_note = STATE_NAMES.get(model.get('status'), '证据待补')
    if model.get('status') == 'partial':
        state_note = coverage_note + '证据未齐，未入选标的仅供复核。'
    validity = '历史列表 · 不授予当前观察资格' if historical else '收盘研究 · 非即时买点'
    forward = records(model.get('forward'))
    forward_html = ''
    if forward:
        names = ' / '.join(e(row.get('label')) for row in forward)
        evidence = ''.join('<p><b>' + e(row.get('label')) + '</b> · 确认：' + '；'.join(e(x) for x in texts(row.get('confirmation'))) + ' · 失效：' + '；'.join(e(x) for x in texts(row.get('risks'))) + '</p>' for row in forward)
        forward_html = '<details class="radar-forward-line"><summary>板块优先复核：' + names + '</summary>' + evidence + '<small>仅为研究顺序，不是启动概率。</small></details>'
    notes = texts(model.get('notes'))
    note = next((x for x in notes if '未保存' in x), '')
    return f'''<section id="radar-day-{day}" data-radar-day="{day}" data-radar-until="{html.escape(model.get('valid_until') or '', quote=True)}" data-radar-historical="{str(bool(historical)).lower()}" role="tabpanel" aria-labelledby="radar-date-{day}" tabindex="0" {'' if selected else 'hidden'}>
<div class="radar-day-meta"><span>行情 {e(clock_label(model.get('as_of')))} · 研究 {e(clock_label(model.get('generated_at')))} · 上海时间</span><span data-radar-validity>{e(validity)}</span></div>
<p class="radar-coverage">{e(state_note)}</p>{forward_html}
<div class="radar-list-tabs" role="tablist" aria-label="{day} 名单类别">{lists}</div>
{''.join(list_panel(model, kind, label, legacy_ids) for kind, label in LISTS)}
{('<p class="radar-list-hint">' + e(note) + '</p>') if note else ''}</section>'''


def prelaunch_focus_section(data, historical):
    """Compatibility for focused render callers; no full-universe detail table."""
    return ('<p>历史复核 · 不取得当前资格</p>' if historical else '') + list_panel(build_daily_model(data, historical), 'review', '优先复核', set())


def render_page(now=None):
    now = now or dt.datetime.now(TZ)
    loaded = load_daily_reports(now=now)
    days = records(loaded.get('days'))
    state = loaded.get('state', 'not_run')
    dates = ''.join(f'<button type="button" id="radar-date-{row["signal_date"]}" data-radar-date="{row["signal_date"]}" role="tab" aria-controls="radar-day-{row["signal_date"]}" aria-selected="{str(i == 0).lower()}" tabindex="{0 if i == 0 else -1}">{e(row["signal_date"])}{("<small>最近研究</small>" if i == 0 else "")}</button>' for i, row in enumerate(days))
    legacy_ids = set()
    body = ''.join(day_panel(row, i == 0, legacy_ids) for i, row in enumerate(days))
    failure = ''
    if state == 'unavailable':
        failure = '<p class="radar-failure" role="status">本轮研究受阻；以下仅保留原日期列表，不视为最新筛选。</p>'
    if not days:
        body = '<p class="radar-empty">' + ('本轮研究受阻，暂未取得可展示的每日列表。' if state == 'unavailable' else '尚未执行：等待首次有效研究。') + '</p>'
    return f'''<div class="radar-daily" data-radar-daily>
<header class="radar-heading"><div><span class="eyebrow">每日观察</span><h1>板块雷达</h1></div><p>先看低位复核，再看正式观察</p></header>
{failure}<span id="radar-universe" class="radar-anchor"></span><span id="radar-prelaunch-focus" class="radar-anchor"></span>
<div class="radar-date-tabs" role="tablist" aria-label="研究日期">{dates}</div>{body}
<p class="radar-footnote">同股合并板块标签；优先复核是核验顺序，不是收益排名。仅展示已存研究日，完整证据留在本地。</p></div>'''
