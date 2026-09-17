"""Bounded public two-stage collection; unverified provider semantics stay unknown."""
from __future__ import annotations
import concurrent.futures
import datetime as dt
import json
import re
import urllib.parse

from prelaunch_research import PublicFeed
from research_store import research_path, atomic_json
from late_day_research import calendar_payload, load_engine, TZ

PREFIXES=('600','601','603','605','000','001','002','003')


def parse_quotes(body, source):
    result={}
    for line in body.split(';'):
        f=line.split('~')
        if len(f)<50 or not re.fullmatch(r'\d{6}',f[2]): continue
        try:
            when=dt.datetime.strptime(f[30],'%Y%m%d%H%M%S').replace(tzinfo=TZ).isoformat()
            result[f[2]]={'code':f[2],'name':f[1],'time':when,'source':source,
                'price':f[3],'reference_close':f[4],'volume_shares':float(f[6])*100,
                'amount_cny':float(f[37])*10000,'high':f[33],'upper_limit':f[47],
                'scope':'includes_opening_auction','ratio_verified':False,
                'provider_ratio':f[49],'provider_turnover_pct':f[38],'provider_cap_yi':f[45]}
        except (ValueError,IndexError): continue
    return result


def numeric_prefilter(q, engine=None):
    """Provider numbers are provisional; final engine recomputes with evidence."""
    return (engine or load_engine()).provisional_filter(q)


def collect(at, *, codes=None, max_details=30, feed=None):
    if at.tzinfo is None or at>dt.datetime.now(TZ): raise ValueError('采集时点无效')
    if (dt.datetime.now(TZ)-at).total_seconds()>90:
        raise ValueError('历史时点只能导入对应证据，不能以当前行情回填')
    if codes and any(not re.fullmatch(r'\d{6}',c) for c in codes): raise ValueError('代码无效')
    folder=research_path('late_day/collection')/dt.datetime.now(TZ).strftime('%Y%m%dT%H%M%S%f')
    feed=feed or PublicFeed(timeout=8,raw_directory=folder/'raw')
    data={'schema_version':1,'as_of':at.isoformat(),'calendar':calendar_payload(at),'stocks':[],
          'coverage':{'verified':False,'description':'公开初筛；证券状态、股本、历史涨停、分钟标签及行业尚需原文核验',
                      'universe_count':0,'scanned_count':0},'market':{},'collection_missing':[]}
    missing=data['collection_missing']
    try:
        if codes:
            universe=[{'code':c} for c in sorted(set(codes))];expected=len(universe)
        else:
            universe,expected,_,errors=feed.universe();missing.extend(errors)
            universe=[r for r in universe if str(r.get('code','')).startswith(PREFIXES)]
        target=[r['code'] for r in universe]
        data['coverage'].update(universe_count=len(target),metadata_expected_all_a=expected)
        def quotes(batch):
            url='https://qt.gtimg.cn/q='+','.join(('sh' if c.startswith('6') else 'sz')+c for c in batch)
            body,_=feed.read(url,'gb18030');return parse_quotes(body,url)
        found={}
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            tasks=[pool.submit(quotes,target[i:i+60]) for i in range(0,len(target),60)]
            for f in tasks:
                try: found.update(f.result())
                except Exception as exc: missing.append('报价批次失败：'+type(exc).__name__)
        engine=load_engine()
        preliminary=[dict(code=c,passed=numeric_prefilter(q,engine),quote=q) for c,q in sorted(found.items())]
        atomic_json(folder/'numeric_prefilter.json',preliminary)
        candidates=[x['quote'] for x in preliminary if x['passed'] is True]
        if codes: candidates=list(found.values()) # explicit scope preserves rejected observations too
        candidates.sort(key=lambda q:(-float(q['amount_cny']),q['code']))
        selected=candidates[:max(1,min(max_details,500))]
        data['coverage'].update(prefilter_count=len(found),numeric_pass_count=sum(x['passed'] is True for x in preliminary),
                                detail_target=len(candidates),scanned_count=len(selected),bounded=len(selected)<len(candidates))
        if len(found)!=len(target): missing.append('报价覆盖不足，不称全市场')
        if len(selected)<len(candidates): missing.append('详情采集预算不足，未处理项不得算失败或合格')
        def details(q):
            code=q['code'];sym=('sh' if code.startswith('6') else 'sz')+code
            stock={'code':code,'name':q['name'],'quote':q,'security':{},'shares':{},'history_meta':{},
                   'history':[],'minutes':[],'minute_meta':{},'opening_auction':{},'industry':{},'raw_evidence':[]}
            urls=[('unadjusted_daily','https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?'+
                   urllib.parse.urlencode({'param':sym+',day,,,40,'})),
                  ('minute_raw','https://push2his.eastmoney.com/api/qt/stock/trends2/get?'+urllib.parse.urlencode({
                    'secid':('1.' if code.startswith('6') else '0.')+code,'fields1':'f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11',
                    'fields2':'f51,f52,f53,f54,f55,f56,f57,f58','ndays':'1','iscr':'0','iscca':'0'}))]
            for kind,url in urls:
                try:
                    body,src=feed.read(url);value=json.loads(body)
                    stock['raw_evidence'].append(dict(kind=kind,**src))
                    if kind=='unadjusted_daily':
                        rows=value.get('data',{}).get(sym,{}).get('day',[])
                        stock['history']=[{'date':r[0],'high':r[3],'upper_limit':None,'volume_shares':float(r[5])*100}
                                          for r in rows if r[0]<at.date().isoformat()][-20:]
                        stock['history_meta']={'source':url,'verified':False,'adjustment':'none',
                            'limits_verified':False,'volume_comparable':False,'scope':'includes_opening_auction'}
                except Exception as exc: stock['raw_evidence'].append({'kind':kind,'error_type':type(exc).__name__})
            return stock
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            data['stocks']=list(pool.map(details,selected))
        # Re-read shortlisted quotes at completion; do not label prefetch prices
        # with the newer clock from historical/minute requests.
        if selected:
            latest=quotes([q['code'] for q in selected])
            for stock in data['stocks']:
                if stock['code'] in latest: stock['quote']=latest[stock['code']]
        missing.append('证券状态、有效股本、逐日真实涨停价、分钟标签/竞价种子、固定行业及完整主板广度待核验；原文已留私有补证目录')
    except Exception as exc:
        data['collection_error']=type(exc).__name__
        missing.append('采集失败，不能记为成功空池')
    data['coverage']['scanned_count']=len(data['stocks'])
    data['as_of']=dt.datetime.now(TZ).isoformat()
    # Never claim a source timestamp that was not observed. Collection is only
    # a starting point for audit/import, not an automatic qualification engine.
    atomic_json(folder/'input.json',data)
    return data
