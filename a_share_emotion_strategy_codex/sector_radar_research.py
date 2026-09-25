"""Close-only sector comparisons; native skill decisions remain independent."""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import math
from statistics import mean, median
from pathlib import Path
from zoneinfo import ZoneInfo

from research_store import research_path, atomic_json, update_lock

TZ = ZoneInfo('Asia/Shanghai')
VERSION = 'sector-radar-v1.1'
SKILLS = ('prelaunch', 'yichujifa', 'dragon', 'late-day', 'three-step')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def sessions(now):
    from late_day_research import calendar_payload
    calendar = calendar_payload(now)
    completed = [d for d in calendar['days'] if d < now.date().isoformat() or
                 (d == now.date().isoformat() and now.time().replace(tzinfo=None) >= dt.time(15))]
    if not completed:
        raise ValueError('无法确认最近完整交易日')
    day = completed[-1]
    targets = [d for d in calendar['days'] if d > day]
    if not targets:
        raise ValueError('无法确认下一交易日')
    return day, targets[0], calendar


def number(v):
    if isinstance(v, bool):
        raise ValueError('布尔值不是量价')
    v = float(v)
    if not math.isfinite(v):
        raise ValueError('非有限量价')
    return v


def security_state(stock, day):
    """Prefix/name can reject; only dated affirmative source evidence can admit."""
    code, name = str(stock.get('code', '')), str(stock.get('name', ''))
    s = stock.get('security') or {}
    excluded, unknown = [], []
    if len(code) != 6 or not code.isdigit() or not code.startswith(('600','601','603','605','000','001','002','003')):
        excluded.append('非沪深主板A股代码范围')
    if 'ST' in name.upper() or s.get('st') is True:
        excluded.append('ST或*ST')
    for key, label in [('suspended','停牌'), ('delisting','退市整理')]:
        if s.get(key) is True: excluded.append(label)
    if s.get('normal_limit') is False: excluded.append('非正常涨跌幅限制阶段')
    if s.get('mainboard') is False: excluded.append('非主板股票')
    if s.get('security_type') not in (None, 'a_share', 'A股', 'ordinary_a'):
        excluded.append('非普通A股证券')
    if s.get('verified') is not True or s.get('date') != day or not s.get('source'):
        unknown.append('信号日证券状态及来源未完整核验')
    for key, expected in [('mainboard',True),('st',False),('suspended',False),('delisting',False),('normal_limit',True)]:
        if type(s.get(key)) is not bool:
            unknown.append(key+'状态未知')
    if s.get('mainboard') is not True: unknown.append('主板身份未核验')
    if s.get('security_type') not in ('a_share','A股','ordinary_a'): unknown.append('普通A股类型未核验')
    return ('excluded', excluded) if excluded else ('pending', unknown) if unknown else ('verified', [])


def history(stock, days, signal, *, benchmark=False, now=None):
    daily = stock.get('daily') or {}
    if daily.get('verified') is not True or daily.get('as_of') != signal or daily.get('adjustment') not in ('qfq','forward_adjusted'):
        raise ValueError('同截面前复权日线待验证')
    if not daily.get('sources'): raise ValueError('日线来源缺失')
    if daily.get('conflict') or daily.get('complete') is False:
        raise ValueError('日线来源冲突或明确不完整')
    if now is not None:
        if str(daily.get('adjustment_as_of') or '')[:10] > now.astimezone(TZ).date().isoformat():
            raise ValueError('复权截面晚于分析日期')
        # Retrieval can finish after the requested analysis clock; it must not
        # be confused with a quote's own evidence clock.
        for source in [daily] + [s for s in daily['sources'] if isinstance(s,dict)]:
            for field in ('source_as_of','source_time'):
                if source.get(field):
                    value = dt.datetime.fromisoformat(source[field])
                    if value.tzinfo is None or value > now:raise ValueError('行情源时间未核验或晚于分析时点')
            if source.get('retrieved_at'):
                value = dt.datetime.fromisoformat(source['retrieved_at'])
                if value.tzinfo is None or value > dt.datetime.now(TZ)+dt.timedelta(seconds=2):
                    raise ValueError('取得时间无效或在未来')
    rows = [r for r in daily.get('bars',[]) if isinstance(r,dict) and str(r.get('date','')) <= signal]
    dates = [r.get('date') for r in rows]
    if dates != sorted(set(dates)): raise ValueError('日线重复或日期冲突')
    expected = [d for d in days if d <= signal][-26:]
    if len(expected) < 26 or dates[-26:] != expected: raise ValueError('最近26个交易日量价不完整')
    rows = [dict(r) for r in rows[-65:]]
    for row in rows:
        fields = ('open','high','low','close') if benchmark else ('open','high','low','close','volume_shares','amount_cny')
        for key in fields:
            row[key] = number(row[key])
        if (min(row[k] for k in ('open','high','low','close'))<=0 or row['high']<max(row['open'],row['close'])
            or row['low']>min(row['open'],row['close']) or row['low']>row['high']
            or (not benchmark and min(row['volume_shares'],row['amount_cny'])<0)): raise ValueError('量价区间或单位异常')
    return rows


def checked_cells(cells, signal, now):
    """A claimed native pass needs its own dated provenance, not a joined flag."""
    result = {}
    for key in SKILLS:
        cell = dict(cells.get(key) or {'status':'not_run','observation_passed':False,'reasons':['尚未执行此策略']})
        if cell.get('status') == 'passed' or cell.get('observation_passed') is True:
            date = str(cell.get('as_of') or '')
            valid_time = date == signal
            if not valid_time:
                try:
                    stamp = dt.datetime.fromisoformat(date)
                    valid_time = stamp.tzinfo is not None and stamp <= now and stamp.astimezone(TZ).date().isoformat() == signal
                except (ValueError, TypeError):
                    valid_time = False
            if not (valid_time and cell.get('rule_hash') and cell.get('rule_version') and cell.get('sources')):
                cell.update(status='pending', observation_passed=False,
                            reasons=list(cell.get('reasons') or [])+['原策略通过结论的信号日、版本或来源不完整'])
        result[key] = cell
    return result


def metrics(bars):
    prices = [b['close'] for b in bars[-6:]]
    peak, drawdown = prices[0], 0.0
    for p in prices:
        peak=max(peak,p);drawdown=max(drawdown,1-p/peak)
    positions=[(b['close']-b['low'])/(b['high']-b['low']) if b['high']>b['low'] else .5 for b in bars[-5:]]
    return {'return_5d':prices[-1]/prices[0]-1,'drawdown_5d':drawdown,
            'close_position_5d':mean(positions),'median_amount_5d':median(b['amount_cny'] for b in bars[-5:])}


def percentile(values):
    """Average rank of ties on 0..100, singleton=50; no missing-value filling."""
    ordered=sorted(values)
    if len(ordered)==1:return {ordered[0]:50.0}
    return {v:100*(ordered.index(v)+ordered.count(v)/2-.5)/(len(ordered)-1) for v in set(ordered)}


def scores(rows, fields):
    maps=[percentile([sign*number(row[key]) for row in rows]) for key,sign in fields]
    return [mean(m[sign*number(row[key])] for m,(key,sign) in zip(maps,fields)) for row in rows]


def equal_return(series, n, end=None):
    returns=[]
    length=len(next(iter(series.values())))
    stop=length if end is None else end
    for i in range(stop-n, stop):
        returns.append(mean(b[i]['close']/b[i-1]['close']-1 for b in series.values()))
    value=1.0
    for r in returns:value*=1+r
    return value-1


def sector_metrics(series, benchmark, passed):
    # All stocks have the identical last-26 calendar window; current membership
    # is held fixed for descriptive comparisons, never sold as historical returns.
    series={k:v[-26:] for k,v in series.items()}
    bm=benchmark[-26:]
    if len(bm)<26:raise ValueError('沪深300历史不足')
    relative=equal_return(series,3)-(bm[-1]['close']/bm[-4]['close']-1)
    prior=equal_return(series,3,end=23)-(bm[-4]['close']/bm[-7]['close']-1)
    breadth=mean(b[-1]['close']>=mean(x['close'] for x in b[-20:]) for b in series.values())
    previous=mean(b[-4]['close']>=mean(x['close'] for x in b[-23:-3]) for b in series.values())
    amounts=[sum(b[i]['amount_cny'] for b in series.values()) for i in range(26)]
    denominator=mean(amounts[-23:-3])
    if denominator<=0:raise ValueError('板块成交额基准无效')
    latest={c:b[-1]['close']/b[-2]['close']-1 for c,b in series.items()}
    positives=sum(max(0,r) for r in latest.values())
    return {'return_3d':equal_return(series,3),'relative_3d':relative,'relative_improvement':relative-prior,'breadth_ma20':breadth,
            'breadth_improvement':breadth-previous,'amount_ratio':mean(amounts[-3:])/denominator,
            'skill_pass_fraction':len(set(series)&set(passed))/len(series),
            'return_5d':equal_return(series,5),'return_20d':equal_return(series,20),
            'leading_concentration':max([max(0,r) for r in latest.values()] or [0])/positives if positives else None,
            'limit_up_count':None,'broken_limit_count':None}


def evaluate(data, checks, now, *, catalog=None):
    from sector_radar_catalog import SECTORS
    signal,target,cal=sessions(now)
    if data.get('signal_date')!=signal:raise ValueError('数据与最近完整交易日不符')
    definitions=catalog if catalog is not None else SECTORS
    supplied={x['id']:x for x in data.get('sectors',[]) if isinstance(x,dict)}
    if len(supplied)!=len(data.get('sectors',[])):raise ValueError('板块重复')
    allstocks={str(s['code']):s for s in data.get('stocks',[]) if isinstance(s,dict)}
    if len(allstocks)!=len(data.get('stocks',[])):raise ValueError('股票重复')
    candidates, series, states = {}, {}, {}
    for code, stock in sorted(allstocks.items()):
        state,reasons=security_state(stock,signal);states[code]=state
        cells=(checks.get('by_code') or {}).get(code,{})
        cells=checked_cells(cells,signal,now)
        row={'code':code,'name':stock.get('name',code),'groups':[],'state':state if state!='verified' else 'pending',
             'security_reasons':reasons,'strategy_checks':cells,'metrics':{},'bars':[],'ranks':{},'eligible':False,
             'sources':list((stock.get('daily') or {}).get('sources') or [])}
        if state!='excluded':
            try:
                series[code]=history(stock,cal['days'],signal,now=now);row['bars']=series[code];row['metrics']=metrics(series[code])
            except (ValueError,TypeError,KeyError,ZeroDivisionError) as exc:row['security_reasons'].append(str(exc))
        ann=stock.get('announcement') or {}
        reviewed=ann.get('as_of')
        try: ann_time=dt.datetime.fromisoformat(reviewed) if reviewed else None
        except (TypeError,ValueError):ann_time=None
        ann_ok=(ann.get('verified') is True and ann.get('sources') and ann_time is not None and ann_time.tzinfo is not None
                and ann_time<=now and ann_time.date().isoformat()>=signal)
        if ann.get('hard_risk'):
            row['state']='excluded';row['security_reasons'].append('公告硬风险：'+str(ann['hard_risk']))
        elif not ann_ok:row['security_reasons'].append('公告原文及信息截止待核验')
        row['announcement_verified']=bool(ann_ok)
        passes=[k for k,c in cells.items() if c.get('observation_passed') is True and c.get('status')=='passed']
        row['supporting_skills']=passes
        if state=='verified' and ann_ok and not ann.get('hard_risk') and code in series and passes:row['state']='watch'
        candidates[code]=row
    benchmark=[]
    try:benchmark=history({'daily':data.get('benchmark',{})},cal['days'],signal,benchmark=True,now=now)
    except (ValueError,TypeError,KeyError,ZeroDivisionError):pass
    sectors=[]
    for definition in definitions:
        sid=definition['id'];source=supplied.get(sid,{})
        raw_members=source.get('members') or []
        codes=[str(x['code']) for x in raw_members]
        unique=list(dict.fromkeys(codes))
        mapping_ok=True
        if 'ths_code' in definition:
            provider=source.get('provider')
            expected_code=definition.get('ths_code') if provider=='ths' else definition.get('em_code') if provider=='eastmoney' else None
            mapping_ok=bool(expected_code) and source.get('provider_code')==expected_code
        complete=(source.get('verified') is True and len(codes)==len(unique)==source.get('expected_count')
                  and str(source.get('membership_as_of') or '')[:10]==signal and bool(source.get('sources')) and mapping_ok)
        missing=list(source.get('missing') or [])
        if not mapping_ok:missing.append('供应商分类与固定映射不一致；需核验原分类导出证据，不能替换主题')
        if not complete:missing.append('信号日完整成分未核验，不发布完整板块前三')
        for code in unique:
            if code not in candidates:
                candidates[code]={'code':code,'name':next((m.get('name',code) for m in raw_members if str(m['code'])==code),code),
                    'groups':[],'state':'pending','security_reasons':['未取得该成分的证券和量价证据'],'strategy_checks':checked_cells({},signal,now),'metrics':{},'bars':[], 'sources':[],'ranks':{},'eligible':False}
            candidates[code]['groups'].append(sid)
        active=[c for c in unique if states.get(c)=='verified']
        unknown=[c for c in unique if states.get(c) not in ('verified','excluded')]
        numeric_complete=complete and not unknown and all(c in series for c in active)
        qualifies=[c for c in active if candidates[c]['state']=='watch']
        item={k:source.get(k) for k in ('provider','provider_code','approximate','difference','membership_as_of','retrieved_at')}
        item.update(id=sid,label=definition['label'],sources=source.get('sources') or [],member_codes=unique,
                    coverage={'complete':numeric_complete,'membership_verified':complete,'expected':source.get('expected_count'),
                              'received':len(unique),'active':len(active),'unknown':len(unknown),'history':sum(c in series for c in active)},
                    missing=missing,metrics={},top_codes=[],forward_rank=None,forward_score=None,confirmation=[],risks=[])
        if numeric_complete and active:
            board_r5=equal_return({c:series[c][-26:] for c in active},5)
            rows=[dict(candidates[c]['metrics'],relative_5d=candidates[c]['metrics']['return_5d']-board_r5) for c in active]
            points=scores(rows,[('relative_5d',1),('drawdown_5d',-1),('close_position_5d',1),('median_amount_5d',1)])
            for c,r,p in zip(active,rows,points):candidates[c]['ranks'][sid]={'rank':None,'score':p,'relative_5d':r['relative_5d']}
            chosen=sorted(qualifies,key=lambda c:(-candidates[c]['ranks'][sid]['score'],-candidates[c]['metrics']['median_amount_5d'],c))[:3]
            for rank,c in enumerate(chosen,1):candidates[c]['ranks'][sid]['rank']=rank
            item['top_codes']=chosen
            if benchmark:
                try:item['metrics']=sector_metrics({c:series[c] for c in active},benchmark,[c for c in active if candidates[c]['supporting_skills']])
                except (ValueError,ZeroDivisionError):item['missing'].append('板块比较指标不足')
        if unknown:item['missing'].append('部分成分的证券状态未知，不能缩池排名')
        if not numeric_complete:item['missing'].append('量价比较覆盖不足，待验证观察不占正式名次')
        if not benchmark:item['missing'].append('沪深300同截面历史待核验')
        item['missing']=list(dict.fromkeys(item['missing']))
        item['risks']=['本次成分固定回看，非历史可交易组合收益；主题可能重叠。','封板、炸板资料未核验时留空，不按0计算。']
        sectors.append(item)
    # A missing strategy can hide passing stocks only when no other skill has
    # already proved the disjunction. Never turn an unknown OR into a failure.
    relevant=[c for c in candidates if states.get(c)!='excluded']
    matrix_complete=all(all(cell.get('status') in ('passed','failed','not_applicable') for cell in candidates[c]['strategy_checks'].values())
                        and len(candidates[c]['strategy_checks'])==5 for c in relevant)
    observation_complete=all(bool(candidates[c].get('supporting_skills')) or
        all(cell.get('status') in ('failed','not_applicable') for cell in candidates[c]['strategy_checks'].values())
        for c in relevant)
    announcement_complete=all(candidates[c].get('announcement_verified') for c in relevant)
    forward_ready=(bool(sectors) and bool(benchmark) and observation_complete and
                   all(s['coverage']['complete'] and (s['metrics'] or s['coverage']['active']==0) for s in sectors))
    forward=[]
    if forward_ready:
        valid=[s for s in sectors if s['coverage']['active']>=3 and s['metrics'].get('relative_3d',-1)>0 and s['metrics'].get('breadth_ma20',0)>=.5]
        if valid:
            points=scores([s['metrics'] for s in valid],[('relative_improvement',1),('breadth_improvement',1),('amount_ratio',1),('skill_pass_fraction',1)])
            for s,p in zip(valid,points):s['forward_score']=p
            valid.sort(key=lambda s:(-s['forward_score'],-s['metrics']['relative_3d'],s['id']))
            for i,s in enumerate(valid[:3],1):
                s['forward_rank']=i;forward.append(s['id'])
                s['confirmation']=['后续1—5个交易日复核三日相对强度维持为正、MA20广度不低于一半，以及原Skill各自的确认条件。']
                s['risks'].append('相对强度转为非正、广度跌破一半或原策略失效时撤销本轮优先复核；相对强不保证正收益。')
    for s in sectors:
        own=set(s['member_codes'])
        s['overlap']=[{'sector_id':t['id'],'count':len(own&set(t['member_codes']))} for t in sectors if t['id']!=s['id'] and own&set(t['member_codes'])]
    missing=list(data.get('missing') or [])+list(checks.get('missing') or [])
    if not announcement_complete:missing.append('部分合格主板股票的公告风险复核未完成，不能将其计为已完成筛选。')
    if not matrix_complete:missing.append('五策略矩阵仍有未执行或待验证项；已知独立通过与未知结论分开展示。')
    if not forward_ready:missing.append('12板块或五策略比较覆盖不完整，暂不发布板块前瞻前三。')
    count=sum(len(s['top_codes']) for s in sectors)
    complete=forward_ready and not missing and all(not s['missing'] for s in sectors)
    report = {'schema_version':1,'module_id':'sector-radar','version':VERSION,'phase':'close','signal_date':signal,'target_date':target,
            'as_of':signal+'T15:00:00+08:00','generated_at':now.isoformat(),'valid_until':target+'T15:00:00+08:00',
            'status':('complete' if count else 'empty') if complete else 'partial',
            'summary':f'12方向收盘观察；{count}个正式个股席位，{len(forward)}个板块优先复核方向；不授予交易资格。',
            'rule_hash':digest({'version':VERSION,'catalog':definitions,
                'calculator':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'rules':hashlib.sha256((Path(__file__).parent/'docs/playbooks/sector-radar.md').read_bytes()).hexdigest()}),
            'coverage':{'sectors_expected':len(definitions),'membership_verified':sum(s['coverage']['membership_verified'] for s in sectors),
                        'sectors_complete':sum(s['coverage']['complete'] for s in sectors),'unique_stocks':len(candidates),
                        'watch_count':len({c for s in sectors for c in s['top_codes']}),'matrix_complete':matrix_complete,
                        'observation_complete':observation_complete,'announcement_complete':announcement_complete,
                        'forward_ready':forward_ready},
            'missing':list(dict.fromkeys(missing)),'sectors':sectors,'candidates':list(candidates.values()),'forward_top':forward,
            'executions':checks.get('executions',[]),'sources':data.get('sources',[]),'acquisition':data.get('acquisition',{}),
            'changes':[],'actionable':False,'research_only':True}
    if checks.get('prelaunch_focus_input'):
        from sector_radar_focus import build_prelaunch_focus
        report['prelaunch_focus'] = build_prelaunch_focus(report, checks['prelaunch_focus_input'])
    return report


def research(now, *, phase='auto', input_data=None):
    from sector_radar_feeds import collect
    from sector_radar_skills import run_checks
    from research_modules import load_module
    signal,target,calendar=sessions(now)
    if input_data is not None:
        if input_data.get('module_id') or input_data.get('signal_date')!=signal:
            raise ValueError('只接受同信号日原始证据，不接受预先标记通过的结果')
        data=json.loads(json.dumps(input_data,allow_nan=False))
    else:data=collect(now)
    data['calendar']=calendar
    fingerprint=digest(data)
    with update_lock():atomic_json(research_path('sector_radar/evidence')/(fingerprint+'.json'),data)
    supplied=dict(data.get('native_inputs') or {})
    for key in ('benchmark','market','industries'):
        if key not in supplied and data.get(key):supplied[key]=data[key]
    checks=run_checks(now,signal,data.get('stocks',[]),calendar,refresh_native=input_data is None,supplied=supplied)
    report=evaluate(data,checks,now)
    previous=load_module('sector-radar',now).get('data') or {}
    old={c for s in previous.get('sectors',[]) for c in s.get('top_codes',[])}
    new={c for s in report['sectors'] for c in s['top_codes']}
    report['changes']=[f'正式观察去重新增{len(new-old)}只、移出{len(old-new)}只。']
    report['input_fingerprint']=fingerprint
    report['generated_at']=dt.datetime.now(TZ).isoformat()
    return report
