"""Partial historical number audit, kept separate from full skill qualification.

This consumes privately retained, explicitly normalized provider observations.
It does not fetch data, assert verified share effectiveness, reconstruct missing
minutes, or promote a numerical match to a strategy candidate.
"""
from __future__ import annotations

import datetime as dt
import html
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from late_day_research import load_engine, TZ

NAMES={'pct':'涨幅','volume_ratio':'量比','turnover_pct':'换手率','cap_yi':'总市值'}


def review_stock(stock, day, previous_five):
    """Audit all 27 observed labels; reject duplicate/gap/wrong-day evidence.

    Raw price precision is retained. A provider's minute label remains its own
    label, not proof of the canonical interval-end convention used by the full
    strategy. Daily volumes and cumulative intraday volumes must already have
    explicit share units; the feed adapter owns unit normalization.
    """
    engine=load_engine()
    at=dt.datetime.combine(day,dt.time(14,30),TZ)
    expected=[(at+dt.timedelta(minutes=i)).isoformat() for i in range(27)]
    result={'code':stock['code'],'name':stock['name'],'state':'unavailable',
            'expected_count':27,'observed_count':0,'numeric_hit_count':0,
            'representative':None,'missing':[],'sources':stock.get('sources',[]),'frames':[]}
    try:
        q=stock['quote']
        if engine.stamp(q['time']).date()!=day:
            raise ValueError('股本与参考昨收来源不是分析当日')
        if stock.get('minute_date')!=day.isoformat():
            raise ValueError('分时资料日期不匹配')
        history=stock['history']
        if [r['date'] for r in history]!=previous_five:
            raise ValueError('前五个交易日量不足或日期不匹配')
        avg=sum(engine.number(r['volume_shares']) for r in history)/5
        if any(engine.number(r['volume_shares'])<=0 for r in history):
            raise ValueError('历史量必须为正数，停牌或缺量待核验')
        floats,total=map(engine.number,(q['provider_float_a_shares'],q['provider_total_shares']))
        # Cross-check the provider's native share counts with its separately
        # rounded closing cap/turnover; this is consistency, not legal verification.
        cap=engine.number(q['price'])*total/100000000
        turnover=engine.number(q['volume_shares'])/floats*100
        if abs(cap-engine.number(q['provider_cap_yi']))>Decimal('.006') or abs(turnover-engine.number(q['provider_turnover_pct']))>Decimal('.006'):
            raise ValueError('供应商原生股本与市值/换手字段冲突')
        observations=stock['minutes']
        times=[engine.stamp(m['time']) for m in observations]
        if times!=sorted(set(times)) or any(t.date()!=day for t in times):
            raise ValueError('分钟重复、乱序或跨日')
        tail=[m for m,t in zip(observations,times) if dt.time(14,30)<=t.time().replace(tzinfo=None)<dt.time(14,57)]
        result['observed_count']=len(tail)
        if [engine.stamp(m['time']).isoformat() for m in tail]!=expected:
            raise ValueError('14:30—14:56分钟标签有缺口，不能认定空池')
        last_volume=last_amount=Decimal(0)
        for m in tail:
            t=engine.stamp(m['time'])
            vol,amount=map(engine.number,(m['volume_shares'],m['amount_cny']))
            if vol<last_volume or amount<last_amount or min(vol,amount)<=0:
                raise ValueError('累计成交量额倒退或单位无效')
            last_volume,last_amount=vol,amount
            calc=engine.numeric_snapshot(price=m['price'],reference_close=q['reference_close'],
                volume_shares=vol,mean5_volume=avg,float_a=floats,total=total,at=t)
            frame={'time':t.isoformat(),'price':str(engine.number(m['price'])),
                   'amount_cny':str(amount),**calc['values'],
                   'failed':[k for k,v in calc['checks'].items() if not v]}
            result['frames'].append(frame)
        result['numeric_hit_count']=sum(not frame['failed'] for frame in result['frames'])
        result['representative']=sorted(result['frames'],key=lambda x:(len(x['failed']),-engine.stamp(x['time']).timestamp()))[0]
        result['state']='pending' if result['numeric_hit_count'] else 'numeric_rejected'
    except (ValueError,KeyError,TypeError,ArithmeticError) as exc:
        result['missing'].append(str(exc))
        # An incomplete audit is never a successful empty numerical pool.
        result['frames']=[];result['numeric_hit_count']=0;result['representative']=None
    return result


def validate_review(data, cutoff):
    allowed={'window_start','window_end','validation_day','quote_count','universe_count',
             'notes','rows','sources'}
    if not isinstance(data,dict) or set(data)-allowed:
        raise ValueError('Invalid numerical audit metadata')
    start,end,limit=(dt.datetime.fromisoformat(x) for x in (data['window_start'],data['window_end'],cutoff))
    if any(t.tzinfo is None for t in (start,end,limit)) or not start<=end<=limit:
        raise ValueError('Numerical audit cannot include future observations')
    if type(data.get('quote_count')) is not int or type(data.get('universe_count')) is not int or not 0<=data['quote_count']<=data['universe_count']<=10000:
        raise ValueError('Invalid audit coverage')
    dt.date.fromisoformat(data['validation_day'])
    if not isinstance(data.get('notes'),list) or any(not isinstance(n,str) for n in data['notes']):
        raise ValueError('Invalid audit notes')
    rows=data.get('rows')
    if not isinstance(rows,list) or len(rows)>data['quote_count']:
        raise ValueError('Invalid numerical audit rows')
    codes=set()
    sources=list(data.get('sources',[]))
    for row in rows:
        if (set(row)-{'code','name','state','expected_count','observed_count','numeric_hit_count',
                      'representative','missing','sources'} or not re.fullmatch(r'\d{6}',row.get('code',''))
                or row['code'] in codes or not isinstance(row.get('name'),str)
                or row.get('state') not in ('numeric_rejected','pending','unavailable')):
            raise ValueError('Invalid numerical audit row')
        codes.add(row['code'])
        if any(type(row.get(k)) is not int for k in ('expected_count','observed_count','numeric_hit_count')):
            raise ValueError('Invalid numerical observation counts')
        if not 0<=row['numeric_hit_count']<=row['observed_count']<=row['expected_count']==27:
            raise ValueError('Invalid numerical observation coverage')
        if not isinstance(row.get('missing'),list) or any(not isinstance(n,str) for n in row['missing']):
            raise ValueError('Invalid row gaps')
        r=row.get('representative')
        if row['state']=='unavailable':
            if r is not None or row['numeric_hit_count']!=0: raise ValueError('Incomplete audit cannot pass')
        else:
            if (not isinstance(r,dict) or set(r)!={'time','price','amount_cny','pct','volume_ratio','turnover_pct','cap_yi','failed'}
                    or not start<=dt.datetime.fromisoformat(r['time'])<=end
                    or not isinstance(r.get('failed'),list) or any(k not in NAMES for k in r['failed'])):
                raise ValueError('Invalid representative observation')
            try:
                for key in ('price','amount_cny',*NAMES):
                    n=Decimal(str(r[key]))
                    if not n.is_finite(): raise ValueError('Non-finite audit number')
            except InvalidOperation as exc:
                raise ValueError('Invalid audit number') from exc
            if (row['state']=='pending') != (row['numeric_hit_count']>0):
                raise ValueError('Numerical matches and state disagree')
        sources.extend(row.get('sources',[]))
    for s in sources:
        if not isinstance(s,dict) or set(s)!={'label','url','time'} or any(not isinstance(v,str) for v in s.values()):
            raise ValueError('Invalid audit source')
        url=urlsplit(s['url'])
        if url.scheme not in ('http','https') or not url.netloc or url.username or url.password:
            raise ValueError('Unsafe audit source')


def render_review(data):
    if not data:return ''
    e=lambda value:html.escape(str(value),quote=True)
    rows=data['rows'];matches=sum(r['numeric_hit_count']>0 for r in rows)
    incomplete=sum(r['state']=='unavailable' for r in rows)
    notes=''.join(f'<li>{e(n)}</li>' for n in data['notes'])
    out=[];details=[]
    for row in rows:
        r=row['representative'];code=e(row['code'])
        state={'numeric_rejected':'数值门槛未同时满足','pending':'数值命中，完整条件待验证','unavailable':'数据不足'}[row['state']]
        cells=([r['time'][11:16]]+[f'{Decimal(str(r[k])):.4f}' for k in NAMES]) if r else ['未取得']*5
        failed='、'.join(NAMES[k] for k in r['failed']) if r else '；'.join(row['missing'])
        out.append(f'<tr><td><a href="#late-day-audit-{code}">{e(row["name"])} {code}</a></td>'
                   +''.join(f'<td>{e(c)}</td>' for c in cells)+f'<td>{e(failed or "无；其余条件待验证")}</td><td>{e(state)}</td></tr>')
        links=''.join(f'<li><a href="{e(s["url"])}" target="_blank" rel="noopener noreferrer">{e(s["label"])}</a> · {e(s["time"])}</li>' for s in row['sources'])
        details.append(f'<details id="late-day-audit-{code}"><summary>{e(row["name"])} {code} · {e(state)}</summary>'
                       f'<p>联合核验进度 {row["observed_count"]}/{row["expected_count"]} 个尾盘分钟标签；四项数值同时命中 {row["numeric_hit_count"]} 次。显示的是最少数值缺口的最后一帧，仅用于定位淘汰原因，不是入场时点。</p>'
                       '<p>证券状态及有效股本、前20日实际涨停、完整分钟VWAP、开盘竞价种子与同刻市场/行业证据仍需独立核验；数值未过即不升级。未把收盘价当作尾盘价。</p>'
                       f'<ul>{links}</ul></details>')
    source_links=''.join(f'<li><a href="{e(s["url"])}" target="_blank" rel="noopener noreferrer">{e(s["label"])}</a> · {e(s["time"])}</li>' for s in data['sources'])
    return (f'<section class="late-day-audit"><h3>尾盘逐分钟数值复核</h3>'
            f'<p>复核窗口 {e(data["window_start"])} — {e(data["window_end"])}；验证日 {e(data["validation_day"])}。</p>'
            f'<p><b>主板代码报价 {data["quote_count"]}/{data["universe_count"]}；研究记录 {len(rows)} 只；完成数值复核 {len(rows)-incomplete} 只；四项数值命中 {matches} 只；数据不足 {incomplete} 只。</b></p>'
            '<p class="notice">这是基础数值复核，不是完整选股通过名单。即使数值通过，仍须补齐其余资格；数据覆盖和来源限制也不能省略。不得据此认定全市场已完成七项筛选。</p>'
            f'<ul>{notes}</ul><div class="table-scroll"><table><thead><tr><th>股票</th><th>证据时间</th><th>涨幅%</th><th>量比</th><th>换手%</th><th>市值亿元</th><th>本帧未过项</th><th>状态</th></tr></thead><tbody>{"".join(out)}</tbody></table></div>'
            '<p>区间按原始精度判断；显示值经四舍五入，不作收益排名。列表完整展示，点击股票查看覆盖与来源。</p>'
            +''.join(details)+f'<details id="late-day-scan-sources"><summary>扫描来源及限制</summary><ul>{source_links}</ul></details>'
            '<h3>下一交易日怎样验证</h3><p>先冻结今天的样本、淘汰原因与原时点，不在看到明天涨跌后补选。记录9:25最终竞价、9:30可成交报价和10:00报价；没有完整合格项就验证空仓纪律，不把不合格股后来上涨算成策略收益。未确认买入、费用及成交，不计算真实盈亏。</p></section>')
