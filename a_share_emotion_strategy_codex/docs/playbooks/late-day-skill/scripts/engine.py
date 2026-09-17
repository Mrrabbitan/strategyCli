"""Late-day research rules. Pure functions, explicit evidence, no orders/network."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import json
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
VERSION = '1.3.0'
RATIO_METHOD = 'cumulative_per_minute_over_previous5_full_day_per_minute'
SCOPE = 'includes_opening_auction'
LABELS = {'security':'证券资格', 'time':'尾盘时间', 'price':'涨幅3%—6%',
          'ratio':'量比2—5', 'turnover':'换手6%—15%', 'cap':'总市值60—300亿',
          'touch':'此前20日触板', 'vwap':'分钟均价线强势', 'market':'市场与行业',
          'failed_limit':'当日冲板失败否决', 'distance':'偏离均价不超过3%'}
NUMERIC_BOUNDS = {'pct':(3,6), 'volume_ratio':(2,5), 'turnover_pct':(6,15), 'cap_yi':(60,300)}


def number(value):
    if isinstance(value, bool) or value is None:
        raise ValueError('缺少有效数值')
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('数值不可解析') from exc
    if not result.is_finite():
        raise ValueError('数值非有限')
    return result


def stamp(value):
    result = dt.datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(result, dt.datetime) or result.tzinfo is None:
        raise ValueError('时间必须包含时区')
    return result.astimezone(TZ)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode()).hexdigest()


def clock(at):
    return at.time().replace(tzinfo=None)


def continuous(at):
    return dt.time(9, 30) <= clock(at) < dt.time(11, 30) or dt.time(13) <= clock(at) < dt.time(14, 57)


def endpoints(at):
    """Canonical completed one-minute ends; auction is a separate seed."""
    end = at.replace(second=0, microsecond=0)
    out = []
    for hour, minute, count in ((9, 30, 120), (13, 0, 120)):
        start = at.replace(hour=hour, minute=minute, second=0, microsecond=0)
        out.extend(start + dt.timedelta(minutes=i) for i in range(1, count + 1)
                   if start + dt.timedelta(minutes=i) <= end)
    return out


def elapsed_minutes(at):
    seconds = 0
    for hour, minute, count in ((9,30,120),(13,0,120)):
        start=at.replace(hour=hour,minute=minute,second=0,microsecond=0)
        seconds += max(0,min(count*60,(at-start).total_seconds()))
    return number(seconds)/60


def provisional_filter(q):
    """Only provider-number preselection; never grants final qualification."""
    try:
        pct=(number(q['price'])/number(q['reference_close'])-1)*100
        return all(numeric_checks({'pct':pct,'volume_ratio':q['provider_ratio'],
                   'turnover_pct':q['provider_turnover_pct'],'cap_yi':q['provider_cap_yi']}).values())
    except (ValueError,KeyError,ArithmeticError): return None


def numeric_checks(values):
    """Shared intervals, with raw precision; no security or trading eligibility."""
    return {key:low <= number(values[key]) <= high for key,(low,high) in NUMERIC_BOUNDS.items()}


def numeric_snapshot(*, price, reference_close, volume_shares, mean5_volume,
                     float_a, total, at):
    """Arithmetic for an observed historical snapshot, never a complete signal.

    Callers must separately audit timestamps, units, share effectiveness, minute
    boundaries and the remaining rules. This helper grants no qualification.
    """
    p,ref,vol,avg,flt,shares = map(number,(price,reference_close,volume_shares,mean5_volume,float_a,total))
    elapsed=elapsed_minutes(stamp(at))
    if min(p,ref,vol,avg,flt,shares,elapsed)<=0 or flt>shares:
        raise ValueError('数值初筛字段或单位无效')
    values={'pct':(p/ref-1)*100,'volume_ratio':vol/elapsed/(avg/240),
            'turnover_pct':vol/flt*100,'cap_yi':p*shares/100000000}
    return {'values':{k:str(v) for k,v in values.items()},'checks':numeric_checks(values),
            'eligible':False}


def fresh(obj, at, times):
    if not obj.get('source'):
        raise ValueError('缺少来源')
    t = stamp(obj['time'])
    if t.date() != at.date() or not 0 <= (at - t).total_seconds() <= 90:
        raise ValueError('来源时间过期、跨日或在未来')
    if not continuous(t) and clock(t) not in (dt.time(11, 30), dt.time(15)):
        raise ValueError('来源时间不属于有效行情时段')
    times.append(t)
    return t


def calendar_days(data, at):
    c = data['calendar']
    if c.get('verified') is not True or not c.get('source'):
        raise ValueError('交易日历未核验')
    begin, end = dt.date.fromisoformat(c['valid_from']), dt.date.fromisoformat(c['valid_until'])
    days = [dt.date.fromisoformat(x) for x in c['days']]
    if days != sorted(set(days)) or not begin <= at.date() <= end:
        raise ValueError('交易日历范围、顺序或重复异常')
    if any(d < begin or d > end or d.weekday() >= 5 for d in days):
        raise ValueError('交易日历记录异常')
    return days


def minutes_audit(stock, at):
    meta, auction = stock['minute_meta'], stock['opening_auction']
    if (meta.get('verified') is not True or not meta.get('source')
            or meta.get('label') != 'interval_end' or meta.get('scope') != 'continuous_only'
            or meta.get('volume_unit') != 'share' or meta.get('amount_unit') != 'CNY'):
        raise ValueError('分钟时间标签、单位或范围未核验')
    if auction.get('verified') is not True or not auction.get('source') or auction.get('final') is not True:
        raise ValueError('开盘最终竞价种子未核验')
    a = stamp(auction['time'])
    if a.date() != at.date() or clock(a) != dt.time(9, 25) or a > at:
        raise ValueError('开盘竞价时间错误')
    rows = stock['minutes']
    times = [stamp(r['end']) for r in rows]
    if times != endpoints(at):
        raise ValueError('分钟缺失、重复、未来记录或午休记录；不得删行提高比例')
    volume, amount = number(auction['volume_shares']), number(auction['amount_cny'])
    if volume < 0 or amount < 0 or (volume == 0) != (amount == 0):
        raise ValueError('竞价金额数量异常')
    active = []
    previous = None
    for r in rows:
        v, money, close, low, high = (number(r[k]) for k in ('volume_shares','amount_cny','close','low','high'))
        if v < 0 or money < 0 or not 0 < low <= close <= high:
            raise ValueError('分钟量价异常')
        if v == 0:
            if money != 0 or previous is None or close != previous or low != close or high != close:
                raise ValueError('零成交分钟不是可核验的沿用价格')
        elif not low - Decimal('.01') <= money / v <= high + Decimal('.01'):
            raise ValueError('分钟金额、股数与价格不一致，疑似单位错误')
        volume += v
        amount += money
        if volume <= 0:
            raise ValueError('无法形成累计均价')
        vwap = amount / volume
        active.append({'end':r['end'], 'close':float(close), 'low':float(low),
                       'vwap':float(vwap), 'above':close >= vwap, 'traded':v > 0})
        previous = close
    return active, volume, amount


def evaluate_stock(stock, data, at, days):
    checks, metrics, times = [], {}, []
    def check(key, fn):
        try:
            passed, detail = fn()
            if type(passed) is not bool:
                raise ValueError('判断不是已核验布尔值')
        except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
            passed, detail = None, '证据不足：' + str(exc)
        checks.append({'id':key, 'label':LABELS[key], 'passed':passed, 'detail':detail})

    def security():
        s = stock['security']
        if s.get('verified') is not True or not s.get('source') or s.get('date') != at.date().isoformat():
            raise ValueError('当日证券状态未核验')
        keys = ('mainboard','st','suspended','delisting','normal_limit')
        if any(type(s.get(k)) is not bool for k in keys):
            raise ValueError('证券状态缺失')
        return (stock['code'].startswith(('600','601','603','605','000','001','002','003'))
                and s['mainboard'] and s['normal_limit'] and not any(s[k] for k in ('st','suspended','delisting')),
                '主板普通A股；ST、退市、停牌和特殊价格限制独立核验')

    def quote():
        q = stock['quote']
        fresh(q, at, times)
        if q.get('conflict') or q.get('scope') != SCOPE:
            raise ValueError('报价冲突或累计范围不明')
        for key in ('price','reference_close','high','upper_limit','volume_shares','amount_cny'):
            if number(q[key]) <= 0: raise ValueError('报价字段无效')
        if number(q['high']) < number(q['price']) or number(q['high']) > number(q['upper_limit']):
            raise ValueError('价格与当日实际涨停价冲突')
        return q

    def shares():
        s = stock['shares']
        if s.get('verified') is not True or not s.get('source') or s.get('valid_on') != at.date().isoformat():
            raise ValueError('当日生效股本未核验')
        if not 0 < number(s['float_a']) <= number(s['total']): raise ValueError('股本单位或范围错误')
        return s

    def price():
        q = quote(); value = (number(q['price']) / number(q['reference_close']) - 1) * 100
        metrics['pct'] = float(value)
        return NUMERIC_BOUNDS['pct'][0] <= value <= NUMERIC_BOUNDS['pct'][1], f'当前相对参考昨收 {value:.6f}%'

    def history():
        meta = stock['history_meta']
        if (meta.get('verified') is not True or not meta.get('source') or meta.get('adjustment') != 'none'
                or meta.get('limits_verified') is not True or meta.get('volume_comparable') is not True
                or meta.get('scope') != SCOPE):
            raise ValueError('未复权历史、涨停价或成交量可比性未核验')
        expected = [d.isoformat() for d in days if d < at.date()][-20:]
        rows = stock['history']
        if len(expected) != 20 or [r['date'] for r in rows] != expected:
            raise ValueError('不是此前连续20个完整交易日')
        for row in rows:
            if not 0 < number(row['high']) <= number(row['upper_limit']) or number(row['volume_shares']) <= 0:
                raise ValueError('历史价格或量异常')
        return rows

    def ratio():
        q = quote(); rows = history()
        elapsed = elapsed_minutes(stamp(q['time']))
        if elapsed <= 0: raise ValueError('交易分钟尚不足')
        computed = number(q['volume_shares']) / elapsed / (sum(number(r['volume_shares']) for r in rows[-5:]) / 5 / 240)
        value = computed
        origin = '计算量比，240分钟基准，不是历史同刻量比'
        if q.get('ratio_verified') is True:
            if q.get('ratio_method') != RATIO_METHOD: raise ValueError('原生量比口径冲突')
            value = number(q['volume_ratio'])
            if abs(value - computed) > max(Decimal('.05'), computed * Decimal('.02')):
                raise ValueError('原生与计算量比不一致')
            origin = '已核验原生量比'
        metrics.update(volume_ratio=float(value), computed_ratio=float(computed))
        return NUMERIC_BOUNDS['volume_ratio'][0] <= value <= NUMERIC_BOUNDS['volume_ratio'][1], f'{origin} {value:.6f}'

    def turnover():
        q, s = quote(), shares(); value = number(q['volume_shares']) / number(s['float_a']) * 100
        metrics['turnover_pct'] = float(value)
        return NUMERIC_BOUNDS['turnover_pct'][0] <= value <= NUMERIC_BOUNDS['turnover_pct'][1], f'成交股数/流通A股股数 {value:.6f}%'

    def cap():
        q, s = quote(), shares(); value = number(q['price']) * number(s['total']) / 100000000
        metrics['cap_yi'] = float(value)
        return NUMERIC_BOUNDS['cap_yi'][0] <= value <= NUMERIC_BOUNDS['cap_yi'][1], f'A股价×总股本 {value:.6f}亿元（A+H同此筛选口径）'

    def touch():
        rows = history(); dates = [r['date'] for r in rows if number(r['high']) == number(r['upper_limit'])]
        metrics['touch_dates'] = dates
        return bool(dates), '历史实际触板日期：' + ('、'.join(dates) or '无')

    def vwap():
        q = quote(); bars, v, a = minutes_audit(stock, at)
        if v > number(q['volume_shares']) or a > number(q['amount_cny']) + Decimal('.01'):
            raise ValueError('分钟累计量额超过当前报价，来源冲突')
        if bars and stamp(q['time']) == stamp(bars[-1]['end']):
            if v != number(q['volume_shares']) or abs(a-number(q['amount_cny']))>Decimal('.01'):
                raise ValueError('同刻分钟与报价累计量额不一致，不能以零成交补缺')
        traded = [r for r in bars if r['traded']]
        if len(traded) < 30 or len(bars) < 30: raise ValueError('有效分钟不足30个')
        above = sum(r['above'] for r in traded)
        current = number(q['amount_cny']) / number(q['volume_shares'])
        metrics.update(above_ratio=above / len(traded), minute_count=len(bars), active_minutes=len(traded),
                       latest30_above=all(r['above'] for r in bars[-30:]), vwap=float(current),
                       candidate_risk_reference=min(r['low'] for r in bars[-30:]))
        return (above * 10 >= 9 * len(traded) and all(r['above'] for r in bars[-30:]) and number(q['price']) > current,
                f'有效分钟站上比例 {above}/{len(traded)}；最近30分钟 {all(r["above"] for r in bars[-30:])}；不是逐笔证明')

    def market():
        m, sec = data['market'], stock['industry']
        fresh(m, at, times); fresh(sec, at, times)
        if (m.get('universe_verified') is not True or sec.get('classification') != 'eastmoney_industry'
                or sec.get('mapping_verified') is not True or sec.get('mapping_date') != at.date().isoformat()
                or not sec.get('code')): raise ValueError('主板广度覆盖或固定行业归属未核验')
        up, down, flat, total = (number(m[k]) for k in ('rising','falling','flat','total'))
        if min(up,down,flat) < 0 or total <= 0 or up + down + flat != total:
            raise ValueError('主板广度覆盖不足')
        defensive = number(m['index_pct']) < 0 and down > up
        return not defensive and number(sec['pct']) > 0, f'市场防守={defensive}；固定行业 {sec["code"]} {sec["pct"]}%'

    def failed_limit():
        q = quote(); touched = number(q['high']) == number(q['upper_limit'])
        return not touched, '当日触板后回落' if touched else '未见当日触板'

    def distance():
        q = quote(); avg = number(q['amount_cny']) / number(q['volume_shares'])
        value = (number(q['price']) / avg - 1) * 100
        metrics['vwap_distance_pct'] = float(value)
        return value <= 3, f'现价偏离累计VWAP {value:.6f}%'

    check('security', security)
    check('time', lambda: (at.date() in days and dt.time(14,30) <= clock(at) < dt.time(14,57), '仅限制新增，不限制次日退出'))
    for key, fn in (('price',price),('ratio',ratio),('turnover',turnover),('cap',cap),('touch',touch),
                    ('vwap',vwap),('market',market),('failed_limit',failed_limit),('distance',distance)):
        check(key, fn)
    if times and (max(times) - min(times)).total_seconds() > 60:
        checks.append({'id':'alignment','label':'报价同刻性','passed':None,'detail':'报价时点跨度超过60秒'})
    known_false = any(c['passed'] is False for c in checks)
    unknown = any(c['passed'] is None for c in checks)
    state = 'rejected' if known_false else 'insufficient' if unknown else 'qualified'
    if clock(at) < dt.time(14,30): state = 'preview'
    sources=[]
    for key in ('quote','security','shares','history_meta','minute_meta','opening_auction','industry'):
        obj=stock.get(key,{})
        if obj.get('source'):
            sources.append({'label':key,'url':obj['source'],'source_time':obj.get('time') or obj.get('date') or obj.get('valid_on')})
    return {'code':stock.get('code',''), 'name':stock.get('name',''), 'state':state,
            'checks':checks, 'metrics':metrics, 'source_time':stock.get('quote',{}).get('time'),
            'amount_cny':stock.get('quote',{}).get('amount_cny'), 'price':stock.get('quote',{}).get('price'),
            'evidence_missing':unknown, 'eligible':False, 'sources':sources}


def freeze_position(position):
    """Use actual user-confirmed entry evidence; caller persists this immutably."""
    if position.get('confirmed') is not True: raise ValueError('未确认实际买入')
    bought = stamp(position['bought_at'])
    if number(position['cost']) <= 0: raise ValueError('成本缺失')
    rows = position['entry_minutes']
    if not position.get('entry_source') or [stamp(r['end']) for r in rows] != endpoints(bought)[-30:] or len(rows) != 30:
        raise ValueError('缺买入前30个完整交易分钟的原始证据')
    low = min(number(r['low']) for r in rows)
    if low <= 0: raise ValueError('风险参考无效')
    return {'id':position['id'], 'code':position['code'], 'bought_at':bought.isoformat(),
            'cost':float(number(position['cost'])), 'risk_reference':float(low),
            'entry_fingerprint':fingerprint(rows), 'confirmed':True}


def review_exit(position, stock, at, days):
    if not position or position.get('confirmed') is not True:
        return {'state':'scenario_only','reasons':['持仓未知，仅给条件式退出，不认定已买入或盈利']}
    buy = stamp(position['bought_at'])
    if buy > at: raise ValueError('持仓来自未来')
    later = [d for d in days if d > buy.date()]
    if not later: return {'state':'insufficient','reasons':['下一交易日日历缺失']}
    sell_day = later[0]
    if at.date() < sell_day:
        return {'state':'t_plus_one','reasons':['买入当日不可卖出；尾盘走弱也不能记作已止损']}
    reasons, missing = [], []
    if at.date() > sell_day or clock(at) >= dt.time(10): reasons.append('下一交易日10:00结束本轮计划；成交仍需确认')
    try:
        auction = stock['opening_auction']
        a = stamp(auction['time'])
        if (auction.get('verified') is not True or auction.get('final') is not True or not auction.get('source')
                or a.date() != at.date() or clock(a) != dt.time(9,25) or a > at):
            raise ValueError('最终竞价未核验')
        if number(auction['price']) < number(position['risk_reference']):
            reasons.append('最终竞价低于冻结参考，9:30后首个可交易机会优先处理')
    except (ValueError,KeyError,TypeError): missing.append('最终竞价缺失')
    if continuous(at):
        try:
            fresh(stock['quote'], at, [])
            if number(stock['quote']['price']) < number(position['risk_reference']): reasons.append('跌破买入前冻结风险参考')
            bars, _, _ = minutes_audit(stock, at)
            if len(bars) >= 2 and not bars[-1]['above'] and not bars[-2]['above']:
                reasons.append('连续两个完整一分钟收盘低于各自当日VWAP')
        except (ValueError,KeyError,TypeError,ArithmeticError): missing.append('当日实时量价证据不足')
    state = 'exit_due' if reasons else 'insufficient' if missing else 'observe_until_1000'
    if reasons and (stock.get('execution_blocked') is True or stock.get('security',{}).get('suspended') is True):
        state = 'exit_blocked'
    return {'state':state, 'reasons':reasons, 'missing':missing, 'sell_day':sell_day.isoformat(),
            'execution':'未执行交易；卖出受阻或未取得成交确认时不得记为已退出'}


def evaluate(data, *, as_of=None, phase='auto', now=None, positions=None):
    if not isinstance(data,dict): raise ValueError('输入必须为JSON对象')
    now = stamp(now or dt.datetime.now(TZ))
    at = stamp(as_of or data['as_of'])
    if at > now: raise ValueError('分析时点不能在未来')
    if data.get('schema_version') != 1: raise ValueError('输入版本不支持')
    days = calendar_days(data, at)
    if phase not in ('auto','preview','live','review','next-open'): raise ValueError('未知阶段')
    if phase == 'auto':
        phase = ('review' if at.date() != now.date() or at.date() not in days or clock(at) >= dt.time(14,57)
                 else 'preview' if clock(at) < dt.time(14,30) else 'live')
    live = phase == 'live' and at.date() == now.date() and 0 <= (now-at).total_seconds() <= 90
    rows = data.get('stocks', [])
    if not isinstance(rows,list) or any(not isinstance(s,dict) or not isinstance(s.get('code'),str)
                                      or not isinstance(s.get('name'),str) for s in rows):
        raise ValueError('股票输入结构无效')
    codes = [s['code'] for s in rows]
    if len(codes) != len(set(codes)) or any(len(c)!=6 or not c.isdigit() for c in codes):
        raise ValueError('重复或无效代码')
    results = [evaluate_stock(s, data, at, days) for s in rows]
    cov = data.get('coverage', {})
    coverage_ok = (cov.get('verified') is True and cov.get('scanned_count') == len(rows)
                   and isinstance(cov.get('universe_count'),int) and cov['universe_count'] >= len(rows)
                   and bool(cov.get('description')))
    if not coverage_ok:
        for r in results:
            if r['state']=='qualified': r['state']='insufficient'
    for r in results:
        # Preserve the actual decision when the presentation becomes historical.
        # Unknown coverage must not become a historical pass either.
        r['decision_state'] = r['state']
        r['research_passed'] = r['state'] == 'qualified'
        if phase == 'review' or (phase == 'live' and not live): r['state'] = 'historical'
        if phase == 'preview': r['state'] = 'preview'
        if phase == 'next-open': r['state'] = 'exit_review'
    qualified = [r for r in results if r['state']=='qualified']
    qualified.sort(key=lambda r:(-number(r['amount_cny']), r['code']))
    historical_passes = sorted((r for r in results if r['research_passed']),
                               key=lambda r:(-number(r['amount_cny']), r['code']))
    incomplete = any(r['evidence_missing'] for r in results) or not coverage_ok or data.get('collection_error')
    state = ('insufficient' if incomplete else 'qualified' if qualified else 'empty')
    if phase in ('preview','review','next-open'): state = phase
    if not live and phase == 'live': state = 'review'
    expiry = min(at+dt.timedelta(seconds=90), at.replace(hour=14,minute=57,second=0,microsecond=0)) if live else at
    exits = []
    for p in positions or []:
        s = next((x for x in rows if x['code']==p['code']), {})
        exits.append(dict(code=p['code'], position_id=p['id'], **review_exit(p,s,at,days)))
    return {'schema_version':1, 'strategy':'late-day', 'rule_version':VERSION, 'as_of':at.isoformat(),
            'generated_at':now.isoformat(), 'valid_until':max(at,expiry).isoformat(), 'phase':phase,
            'state':state, 'qualified_count':len(qualified),
            'historical_pass_count':len(historical_passes),
            'display_codes':[r['code'] for r in historical_passes],
            'coverage':cov, 'rows':results, 'exits':exits, 'input_fingerprint':fingerprint(data),
            'missing':(['覆盖或关键证据不足；不是成功空池'] if incomplete else []),
            'limitations':['规则一致性校验不是收益回测；不自动下单、不保证成交',
                           '分钟收盘与VWAP比较不证明逐笔从未跌破；历史通过不代表当前参与资格']}
