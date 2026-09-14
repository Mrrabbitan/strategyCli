"""A market research workbench with no account or order interface."""
from __future__ import annotations
import datetime as dt
import html
import json
from pathlib import Path
from zoneinfo import ZoneInfo
from research_store import private_path, research_path, read_json

ROOT = Path(__file__).resolve().parent
TZ = ZoneInfo('Asia/Shanghai')
SLOT_LABELS = {'09_00': '盘前', '10_30': '早盘', '13_30': '午后', '14_30': '尾盘', '19_00': '复盘'}

def e(value):
    return html.escape(str(value), quote=True)


def latest_report_date():
    days = []
    for p in private_path('reports').glob('*/*.json'):
        if p.stem.replace('-', '_') not in SLOT_LABELS:
            continue
        data = read_json(p, {})
        try:
            day = dt.date.fromisoformat(p.parent.name)
            if data.get('trade_day_verified') is True and day <= dt.datetime.now(TZ).date():
                days.append(day)
        except (ValueError, AttributeError):
            continue
    return max(days) if days else dt.datetime.now(TZ).date()


def verified_reports(report_date, slots=None):
    result = []
    for slot in slots or SLOT_LABELS:
        if slot not in SLOT_LABELS:
            continue
        file = slot.replace('_', '-')
        data = read_json(private_path('reports') / report_date.isoformat() / (file + '.json'), {})
        valid = isinstance(data, dict) and data.get('trade_day_verified') is True and data.get('report_date') == report_date.isoformat() and data.get('slot') == slot
        result.append({'slot': slot, 'label': f'{slot.replace("_", ":")} {SLOT_LABELS[slot]}',
                       'url': f'reports/{report_date}/{file}.html' if valid else None})
    return result


def research_status(strategy):
    from research_modules import load_module
    current = load_module(strategy)
    data = current['data']
    return {'as_of': data.get('as_of'), 'generated_at': data.get('generated_at'),
            'state': current['note'], 'count': None, 'missing': data.get('missing', []),
            'link': '#page-' + strategy}


def render_playbooks():
    registry = read_json(ROOT / 'docs/playbooks/registry.json', {})
    cards = []
    for play in registry.get('playbooks', []):
        sid = play['id']
        if sid not in ('prelaunch', 'yichujifa', 'dragon'):
            continue
        state = research_status(sid)
        facts = ''.join(f'<li>{e(x)}</li>' for x in play.get('conditions', []))
        steps = ''.join(f'<span>{e(x)}</span>' for x in play.get('steps', []))
        exits = ''.join(f'<li>{e(x)}</li>' for x in play.get('exit_rules', []))
        limits = ''.join(f'<li>{e(x)}</li>' for x in play.get('limits', []))
        missing = ''.join(f'<li>{e(x)}</li>' for x in play.get('missing_behavior', []))
        filename = Path(play.get('doc_file', '')).name
        doc = ROOT / 'docs/playbooks' / filename
        text = doc.read_text(encoding='utf-8') if doc.is_file() and doc.suffix == '.md' else ''
        source = play.get('source_url') or ''
        link = f'<a href="{e(source)}" target="_blank" rel="noopener noreferrer">规则来源 ↗</a>' if source.startswith('https://') else e(play.get('source_label', '本机规则原文'))
        result_link = f'<a href="{e(state["link"])}">查看最近研究 ↗</a>' if state.get('link') else ''
        evidence = ''.join(f'<li>{e(x)}</li>' for x in state['missing'][:6]) if isinstance(state['missing'], list) else '<li>证据需重新核验</li>'
        cards.append(f'''<article class="playbook" id="strategy-{sid}"><header><span class="eyebrow">{e(play['version'])}</span><h2>{e(play['name'])}</h2><p>{e(play['summary'])}</p></header>
<div class="strategy-steps">{steps}</div><div class="strategy-columns"><section><h3>必须满足</h3><ul>{facts}</ul></section><section><h3>退出与等待</h3><ul>{exits}</ul><h3>范围与约束</h3><ul>{limits}</ul></section></div>
<aside class="notice"><b>最近研究：{e(state['state'])}</b><p>行情截至 {e(state['as_of'] or '未提供')} · 研究生成 {e(state['generated_at'] or '未提供')}</p><ul>{evidence}</ul>{result_link}</aside>
<details><summary>数据缺失时如何处理</summary><ul>{missing}</ul></details><details><summary>查看规则说明与执行口径</summary><pre class="full-rules">{e(text)}</pre></details><p class="muted">{link} · 内容指纹 {e(play.get('source_hash', '')[:12])}</p></article>''')
    return ''.join(cards) or '<p class="notice">规则资料缺失，请检查安装；不依赖账户或候选文件。</p>'


def render_dashboard(report_date=None, report=None, slots=None, alerts='', **_compat):
    from research_views import render_module
    from fed_research import render_fed_research
    from research_topics import render_topics
    report_date = report_date or latest_report_date()
    data = {'reports': verified_reports(report_date, slots), 'report_date': str(report_date)}
    values = {
        '@@HOT_SECTORS@@': render_module('hot'), '@@DRAGON@@': render_module('dragon'),
        '@@YICHUJIFA@@': render_module('yichujifa'), '@@PRELAUNCH@@': render_module('prelaunch'),
        '@@FED_RESEARCH@@': render_fed_research(), '@@PLAYBOOKS@@': render_playbooks(),
        '@@RESEARCH_TOPICS@@': render_topics(),
        '@@DATA@@': json.dumps(data, ensure_ascii=False).replace('<', '\\u003c'),
        '@@CSS@@': (ROOT / 'templates/investment.css').read_text(),
        '@@JS@@': (ROOT / 'templates/investment.js').read_text(),
    }
    page = (ROOT / 'templates/investment.html').read_text()
    for key, value in values.items():
        page = page.replace(key, value)
    return page
