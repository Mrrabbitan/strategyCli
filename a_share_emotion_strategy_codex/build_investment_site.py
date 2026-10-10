"""Atomically build the local read-only workbench without accounts or market requests."""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from html.parser import HTMLParser
from zoneinfo import ZoneInfo
from investment_dashboard import ROOT, render_dashboard, verified_reports, latest_report_date, SLOT_LABELS
from research_store import private_path, research_path, read_json, atomic_json, update_lock
from fed_research import SNAPSHOT as FED_SNAPSHOT, ASSET_PREFIX
from strategy_policy import is_paused, policy_status

SLOTS = {k: '' for k in SLOT_LABELS}
TZ = ZoneInfo('Asia/Shanghai')
PAUSABLE_PAGES = ('prelaunch', 'yichujifa', 'late-day', 'three-step')


class _PageSections(HTMLParser):
    """Locate complete outer pages without confusing nested research sections."""
    def __init__(self, page):
        super().__init__(convert_charrefs=False)
        self.page = page
        self.lines = [0]
        self.lines += [match.end() for match in re.finditer('\n', page)]
        self.stack, self.sections, self.anchors = [], {}, []

    def absolute_position(self):
        line, col = self.getpos()
        return self.lines[line - 1] + col

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if attributes.get('id'):
            self.anchors.append((self.absolute_position(), attributes['id']))
        if tag == 'section':
            self.stack.append((attributes.get('id'), self.absolute_position(), self.absolute_position() + len(self.get_starttag_text())))

    def handle_endtag(self, tag):
        if tag == 'section' and self.stack:
            name, start, inner = self.stack.pop()
            if name in {'page-' + module for module in PAUSABLE_PAGES}:
                self.sections[name] = (start, inner, self.absolute_position())


def _paused_page(module, anchors):
    """Summarize saved evidence as history; suppress every current pass label."""
    e = lambda value: html.escape(str(value or ''), quote=True)
    labels = {'prelaunch': '启动前潜伏', 'yichujifa': '一触即发',
              'late-day': '尾盘隔夜', 'three-step': '三步选股'}
    if module == 'late-day':
        topic = read_json(research_path('topics/late-day/current.json'), {})
        data = topic.get('late_day_result') or {} if isinstance(topic, dict) else {}
        data = data if isinstance(data, dict) else {}
        cutoff = data.get('as_of') or (topic.get('as_of') if isinstance(topic, dict) else None)
        rows = data.get('rows') or []
    else:
        from research_modules import load_module
        data = load_module(module)['data']
        cutoff = data.get('as_of')
        rows = data.get('candidates') or data.get('rows') or []
    rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    anchors = list(anchors)
    anchors += {
        'prelaunch': ['prelaunch-selection', 'prelaunch-pools', 'prelaunch-sector-evidence']
                     + ['prelaunch-pool-' + name for name in ('core', 'watch', 'started', 'invalid')],
        'yichujifa': ['yichujifa-selection', 'yichujifa-sector-evidence'],
        'late-day': ['topic-late-day', 'late-day-schedule'],
        'three-step': ['three-step-collection', 'three-step-other-records'],
    }[module]
    if module in ('prelaunch', 'yichujifa'):
        anchors += ['missing-' + module, 'sources-' + module]
        for index, row in enumerate(rows):
            identity = '|'.join((module, str(row.get('group')), str(row.get('sector') or row.get('theme') or ''),
                                 str(row.get('code', '')) or str(index)))
            anchors.append('candidate-' + hashlib.sha256(identity.encode()).hexdigest()[:14])
    elif module == 'late-day':
        anchors += [prefix + str(row.get('code', '')) for row in rows for prefix in ('late-day-stock-', 'late-day-source-')]
    else:
        anchors += ['three-step-' + str(row.get('code', '')) for row in rows]
    historical = ''.join('<li>' + e(row.get('name')) + ' ' + e(row.get('code')) + '</li>' for row in rows)
    old_anchors = ''.join(f'<span id="{e(anchor)}" class="radar-anchor"></span>' for anchor in dict.fromkeys(anchors))
    return f'''<div class="research-surface" data-strategy-paused="{e(module)}"><div class="research-heading"><div><span class="eyebrow">研究选择</span><h1>{labels[module]}</h1></div><div class="research-status"><b>已暂停</b></div></div>
<aside class="notice" role="status"><p>{e(policy_status(module)['reason'])}</p><p>暂停选股运行与参考；旧记录保留，不取得当前资格。</p><a href="#strategy-{e(module)}">查看原规则与暂停状态</a></aside>
<details class="paused-history"><summary>历史研究 · {len(rows)} 条记录</summary>{old_anchors}<p>原行情时点 {e(cutoff) or '未提供'}。按当时资料存档，不作为本轮候选。</p><ul>{historical or '<li>尚无可展示的历史记录。</li>'}</ul><p>完整来源、冻结锚点与原始结果仍保存在本地私有档案。</p></details></div>'''


def apply_strategy_policy(page):
    paused = [module for module in PAUSABLE_PAGES if is_paused(module)]
    if not paused:
        return page
    parser = _PageSections(page)
    parser.feed(page)
    changes = []
    for module in paused:
        boundary = parser.sections.get('page-' + module)
        if boundary is None:
            raise ValueError('A paused strategy page is missing')
        _, begin, end = boundary
        anchors = [name for position, name in parser.anchors if begin <= position < end]
        changes.append((begin, end, _paused_page(module, anchors)))
    for begin, end, content in sorted(changes, reverse=True):
        page = page[:begin] + content + page[end:]
    return page


def report_html(day, slot, payload):
    """Re-render an allowlisted market summary; never copy legacy account HTML."""
    e = lambda value: html.escape(str(value), quote=True)
    breadth = payload.get('breadth') or {}
    market = ''.join(f'<li>{e(k)}：{e(v)}</li>' for k,v in breadth.items() if isinstance(v,(int,float)) and k in ('up','down','flat','total','rising','falling','limit_up','limit_down'))
    indices = payload.get('indices') or {}
    index_rows = []
    for key, row in indices.items():
        if not isinstance(row,dict):continue
        name = row.get('name') or row.get('n') or key
        price = row.get('price',row.get('p','未提供')); pct=row.get('pct','未提供')
        index_rows.append(f'<tr><td>{e(name)}</td><td>{e(price)}</td><td>{e(pct)}</td></tr>')
    events = payload.get('monitor_events', {}).get('events', []) if isinstance(payload.get('monitor_events'),dict) else []
    event_rows = []
    from monitor_feed import stamp
    cutoff = stamp(payload.get('generated_at'))
    for ev in events:
        if not isinstance(ev,dict):continue
        event_time = stamp(ev.get('quote_time'))
        observed_time = stamp(ev.get('observed_at'))
        if cutoff is None or event_time is None or observed_time is None or event_time>cutoff or observed_time>cutoff or event_time.date()!=day:continue
        description = ev.get('kind') or ev.get('type') or '异动记录'
        net = ev.get('main_net_cny')
        detail = f'累计主力净额 {net:,.2f} 元' if isinstance(net,(int,float)) else f'窗口变动 {ev.get("move_pct","未提供")}%'
        event_rows.append(f'<li>{e(ev.get("name",""))} {e(description)} · {e(detail)} · {e(ev.get("quote_time"))}</li>')
    candidates = []
    for row in payload.get('emotion_dynamic_targets', {}).get('items', []) if isinstance(payload.get('emotion_dynamic_targets'),dict) else []:
        if isinstance(row,dict):
            action = row.get('recommendation') or {}
            candidates.append(f'<li>{e(row.get("name",""))} {e(row.get("code",""))}：{e(action.get("action","观察"))}</li>')
    content = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>{day} {slot.replace('_',':')} 时点记录</title><style>body{{font:16px/1.8 system-ui,sans-serif;color:#142b42;max-width:1000px;margin:auto;padding:24px}}table{{border-collapse:collapse;width:100%}}td,th{{padding:10px;border-bottom:1px solid #dce2e5;text-align:left}}.notice{{background:#fff4df;padding:16px}}a{{color:#285a7b}}</style><h1>{day} · {slot.replace('_',':')} {e(SLOT_LABELS[slot])}</h1><p class="notice">历史记录。生成时间 {e(payload.get('generated_at','未提供'))}；行情时间 {e(payload.get('quote_timestamp','未提供'))}。只呈现市场研究，不包含账户或旧ETF计划，历史候选不取得当前交易资格。</p><h2>市场概况</h2><p>当时风险级别：{e(payload.get('risk_level','未提供'))}</p><ul>{market}</ul><table><thead><tr><th>指数</th><th>价格</th><th>涨跌幅 %</th></tr></thead><tbody>{''.join(index_rows)}</tbody></table><h2>当时策略研究</h2><ul>{''.join(candidates) or '<li>没有可展示的已核验策略记录；不补造候选。</li>'}</ul><h2>截至该时点的异动</h2><ul>{''.join(event_rows) or '<li>该记录未附有效异动证据；不等于当时没有异动。</li>'}</ul><p>原始完整报告保存在本地私有档案，不由网页直接暴露。资金统计不是账户身份，不产生自动交易。</p></html>'''
    return content


def _validate_output(folder):
    for file in folder.rglob('*'):
        if file.is_symlink():raise ValueError('Symlinks cannot be served')
        if not file.is_file():continue
        if file.suffix not in ('.html','.png','.txt','.json'):raise ValueError('Unexpected output file')
        if file.suffix in ('.html','.json'):
            text=file.read_text(encoding='utf-8')
            if any(x in text for x in ('@@','/Users/','file://','ntfy_token','ntfy_topic','portfolio_snapshot','account_total_cny','available_quantity')):
                raise ValueError('Private or unresolved data in output')


def build(day=None, *, output=None, update_local=True):
    day=day or latest_report_date()
    out=Path(output) if output else private_path('public')
    out.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with update_lock():
        stage=Path(tempfile.mkdtemp(prefix='.workbench-build-',dir=out.parent))
        try:
            policy_pages = {module: '' for module in PAUSABLE_PAGES if is_paused(module)}
            rendered=apply_strategy_policy(render_dashboard(day, slots=SLOTS, policy_pages=policy_pages))
            (stage/'index.html').write_text(rendered,encoding='utf-8')
            (stage/'latest.html').write_text(rendered,encoding='utf-8')
            (stage/'robots.txt').write_text('User-agent: *\nDisallow: /\n')
            snapshot_path=research_path("fed/current.json")
            research=read_json(snapshot_path,{})
            for chart in research.get('charts',[]):
                name=chart['file']
                if not re.fullmatch(r'[0-9a-z-]+\.png',name):raise ValueError('Invalid chart filename')
                src=snapshot_path.parent/'charts'/name
                if src.is_symlink() or not src.is_file():raise ValueError('Missing declared chart')
                target=stage/ASSET_PREFIX/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(src,target)
            for item in verified_reports(day,SLOTS):
                if not item['url']:continue
                slot=item['slot'];raw=read_json(private_path('reports')/str(day)/(slot.replace('_','-')+'.json'),{})
                target=stage/item['url'];target.parent.mkdir(parents=True,exist_ok=True)
                target.write_text(report_html(day,slot,raw),encoding='utf-8')
            _validate_output(stage)
            inventory={str(p.relative_to(stage)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(stage.rglob('*')) if p.is_file()}
            revision=hashlib.sha256(json.dumps(inventory,sort_keys=True).encode()).hexdigest()
            previous=read_json(out/'version.json',{})
            if previous.get('revision')==revision:
                return out
            meta={'revision':revision,'updated_at':dt.datetime.now(TZ).isoformat(timespec='seconds'),'files':list(inventory)}
            atomic_json(stage/'version.json',meta)
            backup=out.with_name('.'+out.name+'-previous')
            if backup.exists():shutil.rmtree(backup)
            if out.exists():os.replace(out,backup)
            try:os.replace(stage,out)
            except BaseException:
                if backup.exists():os.replace(backup,out)
                raise
            if update_local:
                target=private_path('reports/latest.html');target.parent.mkdir(parents=True,exist_ok=True)
                temporary=target.with_suffix('.tmp');temporary.write_text(rendered,encoding='utf-8');temporary.replace(target)
            if backup.exists():shutil.rmtree(backup)
            return out
        finally:
            if stage.exists():shutil.rmtree(stage)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date',type=dt.date.fromisoformat)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--no-update-local',action='store_true')
    args=parser.parse_args()
    print(build(args.date,output=args.output,update_local=not args.no_update_local))
