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
from zoneinfo import ZoneInfo
from investment_dashboard import ROOT, render_dashboard, verified_reports, latest_report_date, SLOT_LABELS
from research_store import private_path, research_path, read_json, atomic_json, update_lock
from fed_research import SNAPSHOT as FED_SNAPSHOT, ASSET_PREFIX

SLOTS = {k: '' for k in SLOT_LABELS}
TZ = ZoneInfo('Asia/Shanghai')


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
            rendered=render_dashboard(day, slots=SLOTS)
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
