"""Bridge the installed three-step skill to private snapshots, never web commands."""
from __future__ import annotations
import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import urllib.parse

from market_calendar import is_trading_day
from research_modules import TZ
from research_store import research_path, read_json, atomic_json, load_config

ROOT = Path(__file__).resolve().parent
BUNDLE = ROOT/'docs/playbooks/three-step-skill'
FILES = ('SKILL.md','agents/openai.yaml','references/rules.md','references/data.md',
         'scripts/engine.py','scripts/chips_runner.js','scripts/run.py','scripts/validate_input.py',
         'vendor/cyq.js','vendor/LICENSE','vendor/model.json')


def source_hash(folder=BUNDLE):
    h = hashlib.sha256()
    for name in FILES:
        h.update(name.encode()); h.update((folder/name).read_bytes())
    return h.hexdigest()


def install_skill(destination=None):
    target = destination or Path.home()/'.codex/skills/a-share-three-step'
    for name in FILES:
        out=target/name; out.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(BUNDLE/name,out)
    return source_hash(target)


def load_engine():
    installed = Path.home()/'.codex/skills/a-share-three-step'
    folder = installed if installed.exists() else BUNDLE
    if source_hash(folder) != source_hash(BUNDLE):
        raise ValueError('Installed skill differs from the registered distribution')
    spec=importlib.util.spec_from_file_location('three_step_skill',folder/'scripts/engine.py')
    engine=importlib.util.module_from_spec(spec); spec.loader.exec_module(engine)
    return engine


def calendar_payload(at):
    # Older calendar years support the fixed 210-bar window. Private overrides
    # retain precedence; missing years are never guessed as weekday-only sessions.
    config = read_json(ROOT/'examples/config/market_calendar.json',{})
    local = load_config('market_calendar.json')
    config['closures'].update(local.get('closures',{}))
    start = at.date()-dt.timedelta(days=430)
    end = at.date()+dt.timedelta(days=25)
    days=[]
    for i in range((end-start).days+1):
        date=start+dt.timedelta(days=i)
        if str(date.year) not in config['closures']:
            raise ValueError('Exchange calendar year is unavailable')
        if is_trading_day(date,config)[0]: days.append(str(date))
    complete=[d for d in days if dt.datetime.fromisoformat(d+'T15:00:00+08:00')<=at]
    return complete[-1], {'verified':True,'source':config.get('source'), 'days':days}


def daily_eastmoney(code, signal, feed):
    """Two attempts, exact-date/adjustment cache, no call to the unbounded SDK."""
    from urllib.parse import urlencode
    query={'secid':('1.' if code.startswith('6') else '0.')+code,
           'fields1':'f1,f2,f3,f4,f5,f6', 'fields2':'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61',
           'klt':'101','fqt':'1','end':signal.replace('-',''),'lmt':'210'}
    url='https://push2his.eastmoney.com/api/qt/stock/kline/get?'+urlencode(query)
    path=research_path('three_step/cache')/signal/(code+'-eastmoney-qfq.json')
    errors=[]
    for _ in range(2):
        try:
            body, src = feed.read(url)
            raw=json.loads(body)
            block=raw.get('data') or {}
            if block.get('code') != code:
                raise ValueError('Symbol mismatch or missing K line data')
            bars=[]
            for line in block.get('klines',[]):
                f=line.split(',')
                if len(f)!=11: raise ValueError('Unexpected Eastmoney field layout')
                if f[0]>signal: continue
                bars.append({'date':f[0],'open':f[1],'close':f[2],'high':f[3],'low':f[4],
                             'volume_shares':str(float(f[5])*100),'amount_cny':f[6],
                             'turnover_pct':f[10],'complete':True})
            if not bars or bars[-1]['date'] != signal:
                raise ValueError('Latest completed source date mismatch')
            payload={'provider':'eastmoney','adjustment':'qfq','adjustment_as_of':signal,
                     'verified':True,'source':url,'bars':bars,'source_evidence':src}
            checksum=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
            atomic_json(path,{'date':signal,'code':code,'provider':'eastmoney','sha256':checksum,'payload':payload})
            return payload,errors,False
        except Exception as exc:
            errors.append(type(exc).__name__)
    cached=read_json(path,{})
    value=cached.get('payload') or {}
    if (cached.get('date')==signal and cached.get('code')==code and cached.get('provider')=='eastmoney'
            and value.get('source')==url and value.get('adjustment_as_of')==signal
            and cached.get('sha256')==hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()):
        return value,errors,True
    return None,errors,False


def collect(at, signal, calendar, *, feed=None):
    from prelaunch_research import PublicFeed
    from late_day_feeds import parse_quotes
    from concurrent.futures import ThreadPoolExecutor, as_completed
    engine=load_engine()
    folder=research_path('three_step/collection')/dt.datetime.now(TZ).strftime('%Y%m%dT%H%M%S%f')
    feed=feed or PublicFeed(timeout=6,raw_directory=folder/'raw')
    data={'signal_date':signal,'calendar':calendar,'stocks':[], 'sources':[], 'missing':[], 'acquisition':{}}
    try:
        universe, expected, sources, errors=feed.universe()
        data['sources'].extend(sources);data['missing'].extend(errors)
    except Exception as exc:
        universe,expected=[],None
        data['missing'].append('全市场证券目录获取失败：'+type(exc).__name__)
    roster=[r for r in universe if engine.mainboard(r.get('code'))]
    codes=[r['code'] for r in roster]
    # Provider name and a positive price cannot establish exchange status, IPO
    # restrictions, effective circulating shares or historical ST classifications.
    data['universe']={'date':signal,'codes':codes,'expected_count':len(codes) if len(universe)==expected else None,
                      'verified':False,'source':'https://finance.sina.com.cn/stock/'}
    found={}
    def quotes(batch):
        url='https://qt.gtimg.cn/q='+','.join(('sh' if c.startswith('6') else 'sz')+c for c in batch)
        body,src=feed.read(url,'gb18030')
        return parse_quotes(body,url),src
    with ThreadPoolExecutor(max_workers=4) as pool:
        tasks=[pool.submit(quotes,codes[i:i+60]) for i in range(0,len(codes),60)]
        for f in as_completed(tasks):
            try:
                batch,src=f.result(); found.update(batch); data['sources'].append(src)
            except Exception as exc:
                data['missing'].append('报价批次不可用：'+type(exc).__name__)
    date_quotes={c:q for c,q in found.items() if q.get('time','')[:10]==signal}
    data['acquisition'].update(all_a_roster_expected=expected,all_a_roster_received=len(universe),
                               mainboard_roster_received=len(roster),quote_received=len(found),
                               signal_day_quotes=len(date_quotes),verified_security=0)
    for item in roster:
        q=date_quotes.get(item['code']) or {}
        data['stocks'].append({'code':item['code'],'name':item.get('name',item['code']),
                               'state':{},'turnover':{},'history':[], 'quote_context':q,
                               'sources':[{'url':'https://finance.sina.com.cn/stock/','label':'证券目录，仅作待核验范围'}]})
    # Connectivity probe precedes expensive detail work. Without verified status
    # and denominator evidence no record may advance, even if the probe succeeds.
    probe=data['stocks'][0] if data['stocks'] else None
    if probe:
        daily,failures,cache=daily_eastmoney(probe['code'],signal,feed)
        data['acquisition'].update(daily_probe_count=1,daily_probe_success=int(daily is not None),
                                  daily_retry_failures=len(failures),same_day_cache_used=cache)
        if daily:
            probe['daily']=daily
        else:
            data['missing'].append('东财210根前复权日K有限重试失败，且没有同源同日有效缓存；未计算获利筹码。')
    data['missing'].extend(['证券状态及当日有效流通A股股本尚未取得可逐项核验的完整资料；供应商简称和原生换手不作为资格替代。',
                            '历史60日逐日证券状态及有效涨停价缺失；不以9.9%涨幅或10%推算代替。',
                            '公告风险未完成原文核验；本次未确认全市场换手前500。'])
    if len(date_quotes)!=len(codes):
        data['missing'].append('部分报价不属于信号日，不用于该日筛选或排名。')
    data['missing']=list(dict.fromkeys(data['missing']))
    atomic_json(folder/'input.json',data)
    return data


def research(now, *, phase='auto', input_data=None):
    signal,calendar=calendar_payload(now)
    if input_data is None:
        latest,_=calendar_payload(dt.datetime.now(TZ))
        if signal!=latest:
            raise ValueError('Historical research requires an evidence file captured for that cutoff')
        data=collect(now,signal,calendar)
    else:
        if input_data.get('module_id') or input_data.get('signal_date')!=signal:
            raise ValueError('Import raw evidence for the resolved signal date, not a passing report')
        data=dict(input_data)
        # The project calendar, not a supplied list with missing sessions, sets
        # the consecutive and 60-session boundaries.
        data['calendar']=calendar
    engine=load_engine()
    report=engine.evaluate(data,as_of=now)
    report['rule_hash']=source_hash()
    report['acquisition']=data.get('acquisition',{})
    atomic_json(research_path('three_step/evidence')/(report['input_fingerprint']+'.json'),data)
    return report
