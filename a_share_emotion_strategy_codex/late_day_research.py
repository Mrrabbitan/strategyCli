"""On-demand late-day skill bridge. The dashboard only reads published results."""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import html
import importlib.util
import json
from pathlib import Path
import shutil
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from research_store import research_path, private_path, read_json, atomic_json, update_lock, load_config

TZ = ZoneInfo('Asia/Shanghai')
ROOT = Path(__file__).resolve().parent
BUNDLE = ROOT / 'docs/playbooks/late-day-skill'
FILES = ('SKILL.md','agents/openai.yaml','references/rules.md','references/data.md','references/scheduled-preview.md',
         'scripts/engine.py','scripts/run.py','scripts/validate_input.py')
STATE_NAMES = {'not_run':'尚未执行', 'preview':'预观察', 'qualified':'研究条件通过',
               'empty':'已完成筛选，无合格股', 'insufficient':'数据不足', 'failed':'本次执行失败',
               'review':'历史复核', 'next-open':'次日退出复核', 'rejected':'条件不满足',
               'historical':'历史记录', 'exit_review':'退出复核'}


def source_hash(folder=BUNDLE):
    h = hashlib.sha256()
    for name in FILES:
        h.update(name.encode()); h.update((folder/name).read_bytes())
    return h.hexdigest()


def install_skill(destination=None):
    target = destination or Path.home()/'.codex/skills/a-share-late-day'
    for name in FILES:
        out = target/name; out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(BUNDLE/name, out)
    return source_hash(target)


def load_engine():
    installed = Path.home()/'.codex/skills/a-share-late-day'
    # One maintained source, byte-identical installed distribution. Never select
    # a changed local engine silently against a different displayed rule card.
    folder = installed if installed.exists() else BUNDLE
    if source_hash(folder) != source_hash(BUNDLE):
        raise ValueError('本机技能与公开规则版本不一致，需先核验同步')
    spec = importlib.util.spec_from_file_location('late_day_skill_engine', folder/'scripts/engine.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def calendar_payload(at):
    from market_calendar import is_trading_day
    config = load_config('market_calendar.json')
    begin, end = at.date()-dt.timedelta(days=80), at.date()+dt.timedelta(days=35)
    days=[]
    for i in range((end-begin).days+1):
        day=begin+dt.timedelta(days=i)
        if str(day.year) not in config.get('closures',{}):
            raise ValueError('交易所日历年份缺失')
        if is_trading_day(day,config)[0]: days.append(day.isoformat())
    return {'verified':True,'source':'workbench verified exchange calendar',
            'valid_from':str(begin),'valid_until':str(end),'days':days}


def frozen_positions(incoming, engine, *, as_of=None):
    """Immutable private references. No costs/entry evidence go to the page."""
    path=research_path('late_day/frozen_positions.json')
    with update_lock():
        old=read_json(path,{})
        additions={}
        for p in incoming:
            item=engine.freeze_position(p)
            if as_of is not None and engine.stamp(item['bought_at'])>as_of:
                raise ValueError('未来买入记录不可冻结')
            if item['id'] in old and old[item['id']] != item:
                raise ValueError('持仓冻结记录冲突，不得下移或重置风险线')
            if item['id'] in additions and additions[item['id']] != item:
                raise ValueError('同批持仓标识冲突')
            additions[item['id']]=item
        if additions:
            old.update(additions);atomic_json(path,old)
        return list(old.values())


def topic_payload(report):
    state=report['state']
    # Whitelist only research observations, never the input or private positions.
    selected=report.get('preview_order') or report.get('display_codes',[])
    all_rows=report.get('rows',[])
    ranked=[r for r in all_rows if type(r.get('observation_rank')) is int]
    if ranked: selected=[r['code'] for r in sorted(ranked,key=lambda r:r['observation_rank'])]
    order={code:i for i,code in enumerate(selected)}
    rows=sorted(all_rows,key=lambda x:(order.get(x['code'],10001),x['code']))
    safe_rows=[{k:r.get(k) for k in ('code','name','state','decision_state','checks','metrics','source_time','price','research_passed','sources',
                                     'observation_rank','breakout_reason','confirmation','risk')}
               for r in rows]
    exits=[{k:x.get(k) for k in ('code','state','reasons','missing','sell_day','execution')}
           for x in report.get('exits',[])]
    historical_count=sum(r.get('research_passed') is True for r in rows)
    summary=(f"{STATE_NAMES.get(state,state)}；研究截面通过{historical_count}只，当前有效{report.get('qualified_count',0)}只。"
             '完整展示，不按前5只截断；不是预期收益排名，原始证据仅在本地。')
    return {'schema_version':1,'topic_id':'late-day','title':'尾盘隔夜战法 · 完整研究结果',
            'as_of':report['as_of'],'generated_at':report['generated_at'],'valid_until':report['valid_until'],
            'status':'unavailable' if state in ('failed','not_run') else 'partial' if report.get('missing') else 'complete',
            'summary':summary,'changes':['本策略与龙空龙、一触即发、潜伏独立；退出不受14:30限制。'],
            'missing':report.get('missing',[]),'directions':[],'sources':[],
            'late_day_result':{'state':state,'rule_version':report['rule_version'],
              'rule_hash':report['rule_hash'],'coverage':report.get('coverage',{}),
              'qualified_count':report.get('qualified_count',0),'historical_pass_count':historical_count,
              'rows':safe_rows,'exits':exits,
              'numeric_review':report.get('numeric_review')}}


def publish_report(report, *, rebuild=True):
    from research_topics import publish_topic
    folder=research_path('late_day')
    with update_lock():
        old=read_json(folder/'latest_attempt.json',{})
        digest=hashlib.sha256(json.dumps(report,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        atomic_json(folder/'history'/f'{digest}.json',report)
        if old and (old['generated_at']>report['generated_at'] or
                    (old.get('state') not in ('not_run','failed') and old['as_of']>report['as_of'])):
            return {'changed':False,'reason':'newer research already published'}
        atomic_json(folder/'latest_attempt.json',report)
        if report['state'] not in ('failed','not_run','insufficient') and not report.get('missing'):
            atomic_json(folder/'last_success.json',report)
    outcome=publish_topic(topic_payload(report))
    if rebuild:
        from build_investment_site import build
        build()
    return outcome


def run_research(data=None, *, as_of=None, phase='auto', now=None, rebuild=True):
    now=now or dt.datetime.now(TZ)
    at=as_of or now
    try:
        engine=load_engine()
        if data is None:
            report={'schema_version':1,'strategy':'late-day','rule_version':engine.VERSION,'as_of':at.isoformat(),
                    'generated_at':now.isoformat(),'valid_until':at.isoformat(),'state':'not_run',
                    'qualified_count':0,'rows':[],'exits':[],
                    'missing':['尚未导入或采集尾盘证据；未执行不等于无合格股票。真实盘中时效未验收。']}
        else:
            # Invalid inputs cannot mutate immutable purchase records.
            preliminary=engine.evaluate(data,as_of=as_of,phase=phase,now=now)
            preliminary['rule_hash']=source_hash()
            from research_topics import validate
            validate(topic_payload(preliminary),now)
            frozen=frozen_positions(data.get('positions',[]),engine,as_of=engine.stamp(preliminary['as_of']))
            report=engine.evaluate(data,as_of=as_of,phase=phase,now=now,positions=frozen)
            atomic_json(research_path('late_day/evidence')/(report['input_fingerprint']+'.json'),data)
        report['rule_hash']=source_hash()
    except (ValueError,KeyError,TypeError,OSError,ArithmeticError,AttributeError,IndexError) as exc:
        report={'schema_version':1,'strategy':'late-day','rule_version':'1.0.0','rule_hash':source_hash(),
                'as_of':min(at,now).isoformat(),'generated_at':now.isoformat(),'valid_until':min(at,now).isoformat(),
                'state':'failed','qualified_count':0,'rows':[],'exits':[],
                'missing':['输入、来源版本或冻结记录校验失败；详细信息保留私有，不沿用旧名单。']}
        atomic_json(research_path('late_day/errors')/(now.strftime('%Y%m%dT%H%M%S%f')+'.json'),
                    {'type':type(exc).__name__,'detail':str(exc)})
    publish_report(report,rebuild=rebuild)
    return report


def status(now=None):
    now=now or dt.datetime.now(TZ)
    r=read_json(research_path('late_day/latest_attempt.json'),{})
    if not r:
        return {'as_of':None,'generated_at':None,'state':'尚未执行','missing':[], 'link':'#topic-late-day'}
    state=STATE_NAMES.get(r.get('state'),'尚未执行')
    try:
        if r.get('state') not in ('not_run','failed') and dt.datetime.fromisoformat(r['valid_until'])<now:
            state='历史记录 · '+state
    except (ValueError,KeyError,TypeError): state='数据不足'
    return {'as_of':None if r.get('state') in ('not_run','failed') else r.get('as_of'),'generated_at':r.get('generated_at'),'state':state,
            'missing':r.get('missing',[]),'link':'#topic-late-day'}


def validate_topic(data):
    result=data['late_day_result']
    if not isinstance(result,dict) or result.get('state') not in STATE_NAMES:
        raise ValueError('Invalid late-day topic')
    if not isinstance(result.get('rows'),list) or len(result['rows'])>10000:
        raise ValueError('Invalid late-day display count')
    if (not isinstance(result.get('rule_hash'),str) or not isinstance(result.get('rule_version'),str)
            or type(result.get('qualified_count')) is not int or not 0<=result['qualified_count']<=10000
            or not isinstance(result.get('coverage'),dict) or not isinstance(result.get('exits'),list)):
        raise ValueError('Invalid late-day metadata')
    ranks=set()
    for row in result['rows']:
        if (not isinstance(row,dict) or any(not isinstance(row.get(k),str) for k in ('code','name','state'))
                or not isinstance(row.get('sources'),list) or len(row['sources'])>10):
            raise ValueError('Invalid late-day row')
        if (row.get('decision_state') is not None and row['decision_state'] not in STATE_NAMES
                or (row.get('research_passed') is not None and type(row['research_passed']) is not bool)):
            raise ValueError('Invalid historical decision')
        rank=row.get('observation_rank')
        if rank is not None:
            if (type(rank) is not int or not 1<=rank<=10 or rank in ranks
                    or result['state']!='preview' or result['qualified_count']!=0
                    or row.get('research_passed') is not False
                    or any(not isinstance(row.get(k),str) or not row[k].strip()
                           for k in ('breakout_reason','confirmation','risk'))):
                raise ValueError('Invalid preliminary observation rank')
            conditions={c.get('id'):c.get('passed') for c in row['checks'] if isinstance(c,dict)}
            required={'security','price','ratio','turnover','cap','touch','vwap','market','failed_limit','distance'}
            if (not required<=conditions.keys() or any(conditions[k] is not True for k in required)
                    or any(c.get('passed') is not True for c in row['checks'] if c.get('id')!='time')
                    or result['coverage'].get('verified') is not True):
                raise ValueError('Unverified conditions cannot enter the preliminary top ten')
            ranks.add(rank)
        if not isinstance(row.get('checks'),list) or len(row['checks'])>15:
            raise ValueError('Invalid late-day checks')
        for check in row['checks']:
            if (not isinstance(check,dict) or any(not isinstance(check.get(k),str) for k in ('label','detail'))
                    or 'passed' not in check or (check['passed'] is not None and type(check['passed']) is not bool)):
                raise ValueError('Invalid evidence state')
        for source in row['sources']:
            if not isinstance(source,dict) or any(not isinstance(source.get(k),str) for k in ('label','url')):
                raise ValueError('Invalid late-day source')
            urlsplit(source['url'])
    for x in result['exits']:
        if (not isinstance(x,dict) or not isinstance(x.get('code'),str) or not isinstance(x.get('state'),str)
                or any(not isinstance(x.get(k,[]),list) or any(not isinstance(t,str) for t in x.get(k,[])) for k in ('reasons','missing'))):
            raise ValueError('Invalid late-day exit')
    if ranks and ranks!=set(range(1,len(ranks)+1)):
        raise ValueError('Preliminary observation ranks must be contiguous')
    if result.get('numeric_review') is not None:
        from late_day_review import validate_review
        validate_review(result['numeric_review'],data['as_of'])


def render_page(now=None):
    """Read the existing private snapshot; never execute a scan in the renderer."""
    from research_topics import validate
    now=now or dt.datetime.now(TZ)
    configured=read_json(research_path('late_day/schedule_config.json'),{})
    schedule=('<aside class="notice" id="late-day-schedule"><b>定时计划 · 北京时间</b>'
              '<p>14:00主板初筛，冻结完整观察池；14:20仅对该池精筛，给最多十只突破潜质观察顺序。休市跳过。</p>'
              '<p>两个阶段均为预观察；14:30后仍需重新核验，资料不足不凑名额，由你决定操作。'
              '任务启停以Codex任务设置为准，执行依赖电脑、网络和Codex可用，不包含次日10:00自动提醒。</p></aside>') if configured.get('configured') is True else ''
    data=read_json(research_path('topics/late-day/current.json'),{})
    if not data:
        return schedule+'<section id="topic-late-day" class="playbook"><h2>尚未执行</h2><p>没有尾盘研究快照，未执行不等于无合格股票。</p><a href="#strategy-late-day">查看规则</a></section>'
    try:
        validate(data,now)
        return schedule+render_topic(data,now)
    except (ValueError,TypeError,AttributeError,KeyError):
        return schedule+'<section id="topic-late-day" class="playbook"><h2>数据不足</h2><p>本次尾盘资料校验失败，不恢复旧资格；其他策略不受影响。</p></section>'


def render_topic(data, now):
    e=lambda x: html.escape(str(x),quote=True)
    r=data['late_day_result']
    expired=dt.datetime.fromisoformat(data['valid_until'])<now
    headline=STATE_NAMES[r['state']]
    if expired and r['state'] not in ('not_run','failed'): headline='历史记录 · '+headline
    cards=[]; overview=[]; buckets={'qualified':0,'insufficient':0,'rejected':0,'preview':0}
    for row in r['rows']:
        decision=row.get('decision_state') or ('qualified' if row.get('research_passed') else
                    'rejected' if any(c['passed'] is False for c in row['checks']) else 'insufficient')
        buckets[decision]=buckets.get(decision,0)+1
        label=('历史判断 · ' if expired or r['state']=='review' else '')+STATE_NAMES.get(decision,decision)
        checks=''.join('<tr><td>'+e(c['label'])+'</td><td>'+{True:'通过',False:'不满足',None:'待验证'}[c['passed']]
                       +'</td><td>'+e(c['detail'])+'</td></tr>' for c in row['checks'])
        links=[]
        for source in row['sources']:
            url=urlsplit(source['url'])
            if url.scheme in ('https','http') and url.netloc and not url.username and not url.password:
                links.append(f'<li><a href="{e(source["url"])}" target="_blank" rel="noopener noreferrer">{e(source["label"])}</a> · {e(source.get("source_time") or "来源时点另见原始证据")}</li>')
        reasons='；'.join(c['label'] for c in row['checks'] if c['passed'] is False) or ('证据待补' if decision=='insufficient' else '见逐项证据')
        overview.append(f'<tr><td><a href="#late-day-stock-{e(row["code"])}">{e(row["name"])} {e(row["code"])}</a></td>'
                        f'<td>{e(label)}</td><td>{e(row.get("price") or "未取得")}</td><td>{e(reasons)}</td></tr>')
        rank_text=f'观察顺序 {row["observation_rank"]} · ' if row.get('observation_rank') else ''
        observation=(''.join(f'<p><b>{label}：</b>{e(row[key])}</p>' for label,key in
                     (('排序理由','breakout_reason'),('仍待确认','confirmation'),('失效与风险','risk'))) if rank_text else '')
        cards.append(f'<details class="research-stock" id="late-day-stock-{e(row["code"])}"><summary>{e(rank_text)}{e(row["name"])} {e(row["code"])} · {e(STATE_NAMES.get(decision,decision))}</summary>'
                     f'<p class="late-day-candidate-state">{e(label)}</p><p>源时间 {e(row.get("source_time"))}</p>'
                     +observation+
                     '<div class="table-scroll"><table><thead><tr><th>条件</th><th>核验</th><th>证据与缺口</th></tr></thead>'
                     f'<tbody>{checks}</tbody></table></div><details id="late-day-source-{e(row["code"])}"><summary>来源与时点</summary><ul>{"".join(links)}</ul></details>'
                     '<p>无实际持仓时仅给情景：次日风险优先，10:00结束本轮计划；成交未知。</p></details>')
    exit_text=''.join(f'<li>{e(x["code"])} · {e(x["state"])} · {e("；".join(x.get("reasons",[])))} '
                      f'· {e("；".join(x.get("missing",[])))} · 未确认成交</li>' for x in r['exits'])
    missing=''.join(f'<li>{e(x)}</li>' for x in data.get('missing',[]))
    count=0 if expired or r['state'] in ('review','next-open','preview','not_run','failed') else r['qualified_count']
    coverage=r.get('coverage',{})
    coverage_text=(f"详情 {coverage.get('scanned_count','未提供')}/{coverage.get('universe_count','未提供')}；"
                   f"初筛行情 {coverage.get('prefilter_count','未提供')} 项。{coverage.get('description','尚未执行')}")
    cutoff_label='研究请求截止（行情未取得）' if r['state'] in ('not_run','failed') else '行情截止'
    historical_count=sum(row.get('research_passed') is True for row in r['rows'])
    overview_table=('<div class="table-scroll"><table><thead><tr><th>股票</th><th>研究判断</th><th>截面价格</th><th>阻断条件</th></tr></thead>'
                    f'<tbody>{"".join(overview)}</tbody></table></div>') if overview else ''
    from late_day_review import render_review
    numeric_review=render_review(r.get('numeric_review'))
    ranked=sorted((row for row in r['rows'] if row.get('observation_rank')),key=lambda row:row['observation_rank'])
    preview_rows=''.join(f'<tr><td>{row["observation_rank"]}</td><td><a href="#late-day-stock-{e(row["code"])}">{e(row["name"])} {e(row["code"])}</a></td>'
                         f'<td>{e(row["breakout_reason"])}</td><td>{e(row["confirmation"])}</td><td>{e(row["risk"])}</td></tr>' for row in ranked)
    preview_table=(f'<h3>突破潜质观察顺序 · {len(ranked)}只</h3><p>14:20精筛仅对同日14:00初筛池；最多十只，不是收益概率排名，不授予参与资格。历史报告不代表现在仍满足。</p>'
                   '<div class="table-scroll"><table><thead><tr><th>顺序</th><th>股票</th><th>排序理由</th><th>仍待确认</th><th>失效与风险</th></tr></thead>'
                   f'<tbody>{preview_rows}</tbody></table></div>') if ranked else ''
    research_summary=(f'本研究截面：完整通过 <b>{historical_count}</b> 只；待验证 {buckets.get("insufficient",0)} 只；'
                      f'条件不满足 {buckets.get("rejected",0)} 只。全部 {len(r["rows"])} 条完整规则记录，不截断为前5只。')
    if numeric_review and not r['rows']:
        research_summary='完整策略合格名单：尚无可确认通过项。基础数值复核与数据不足项在下方分别列示。'
    return (f'<section class="playbook" id="topic-late-day" data-late-day-until="{e(data["valid_until"])}">'
            f'<h2>{e(data["title"])}</h2><p class="notice late-day-status">{e(headline)}；当前研究通过 <span class="late-day-count">{count}</span> 只。</p>'
            f'<p>{cutoff_label} {e(data["as_of"])} · 执行 {e(data["generated_at"])} · 有效至 {e(data["valid_until"])}</p>'
            f'<p>规则 {e(r["rule_version"])} · 指纹 {e(r["rule_hash"][:12])} · 静态专题不授予交易资格</p>'
            f'<p>覆盖：{e(coverage_text)}</p><ul>{missing}</ul>'
            f'<p class="late-day-historical-count">{research_summary}</p>'
            '<p>过去满足条件与当前有效资格分别计数；证据不足不代表全市场没有符合股票。</p>'
            +numeric_review
            +preview_table
            +overview_table
            +(''.join(cards) or ('<p>完整资格名单：暂无可确认通过项；数值复核与待补证据见上方。</p>' if numeric_review else '<p>没有可展示候选；请区分未执行、资料不足和有效空池。</p>'))
            +f'<h3>次日退出复核</h3><ul>{exit_text or "<li>持仓未知，只提供情景，不认定已买入或盈利。</li>"}</ul>'
            '<p>本策略支持按需研究及用户明确授权的预观察任务；不承诺自动在10:00通知。真实盘中时效需交易日验收；规则测试不证明收益。</p>'
            '<a href="#strategy-late-day">查看尾盘战法规则</a></section>')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input',type=Path);p.add_argument('--as-of',type=dt.datetime.fromisoformat)
    p.add_argument('--phase',choices=('auto','preview','live','review','next-open'),default='auto')
    p.add_argument('--collect',action='store_true');p.add_argument('--codes');p.add_argument('--max-details',type=int)
    p.add_argument('--no-build',action='store_true');p.add_argument('--install-skill',action='store_true')
    a=p.parse_args()
    if a.install_skill:
        print(install_skill());return
    if a.input and a.collect: p.error('选择导入或采集之一')
    at=a.as_of or dt.datetime.now(TZ)
    if at.tzinfo is None: p.error('分析时点须带时区')
    data=None
    if a.input:
        if a.input.is_symlink() or a.input.stat().st_size>100_000_000: p.error('输入须为有界普通JSON文件')
        data=json.loads(a.input.read_text(encoding='utf-8'))
    if a.collect:
        from late_day_feeds import collect
        try:
            data=collect(at,codes=a.codes.split(',') if a.codes else None,max_details=a.max_details)
        except (ValueError,KeyError,OSError) as exc:
            data={'schema_version':0,'collection_error':type(exc).__name__}
    report=run_research(data,as_of=a.as_of,phase=a.phase,rebuild=not a.no_build)
    print(json.dumps({k:report.get(k) for k in ('state','as_of','qualified_count','missing')},ensure_ascii=False))


if __name__=='__main__': main()
