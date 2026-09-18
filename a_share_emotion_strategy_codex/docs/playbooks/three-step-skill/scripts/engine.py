"""Deterministic three-stage close research. No network, orders or scheduling."""
from __future__ import annotations
import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from zoneinfo import ZoneInfo

VERSION = '1.0.0'
TZ = ZoneInfo('Asia/Shanghai')
ROOT = Path(__file__).resolve().parents[1]
MODEL = json.loads((ROOT/'vendor/model.json').read_text())


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def number(value):
    if isinstance(value, bool) or value is None:
        raise ValueError('Missing numeric evidence')
    try:
        x = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('Invalid numeric evidence') from exc
    if not x.is_finite():
        raise ValueError('Nonfinite numeric evidence')
    return x


def day(value):
    return dt.date.fromisoformat(value).isoformat()


def mainboard(code):
    return isinstance(code, str) and bool(re.fullmatch(r'(60[0135]|00[0123])\d{3}', code))


def indexed(rows, cutoff):
    """Identical duplicates collapse; conflicts fail instead of choosing one."""
    found, future = {}, 0
    for row in rows:
        d = day(row.get('date'))
        if d > cutoff:
            future += 1
            continue
        if d in found and found[d] != row:
            raise ValueError('Conflicting records for the same date')
        found[d] = row
    return found, future


def security(state, date):
    if not isinstance(state, dict) or state.get('date') != date or state.get('verified') is not True or not state.get('source'):
        return 'pending'
    fields = ('ordinary_a', 'st', 'delisting', 'suspended', 'normal_limit')
    if any(type(state.get(k)) is not bool for k in fields) or state.get('conflict'):
        return 'pending'
    return ('pass' if state['ordinary_a'] and state['normal_limit'] and
            not any(state[k] for k in ('st', 'delisting', 'suspended')) else 'fail')


def turnover(stock, date):
    t = stock.get('turnover') or {}
    if (t.get('verified') is not True or t.get('date') != date or not t.get('source') or
            t.get('denominator') != 'circulating_a_shares' or t.get('conflict')):
        raise ValueError('Turnover denominator/date/source unverified')
    shares, floating = number(t.get('volume_shares')), number(t.get('float_a_shares'))
    if shares < 0 or floating <= 0:
        raise ValueError('Invalid turnover units')
    return shares/floating


def price_rows(stock, signal, days):
    data = stock.get('daily') or {}
    if data.get('conflict'):
        raise ValueError('Conflicting price sources')
    if (data.get('verified') is not True or data.get('provider') != 'eastmoney' or
            data.get('adjustment') != 'qfq' or data.get('adjustment_as_of') != signal or
            not data.get('source') or data.get('conflict')):
        raise ValueError('Same-cutoff Eastmoney qfq data not verified')
    rows, future = indexed(data.get('bars', []), signal)
    for d, row in rows.items():
        if d not in days or row.get('complete') is not True:
            raise ValueError('Unfinished or non-session daily bar')
        low, high = number(row.get('low')), number(row.get('high'))
        if low <= 0 or high < low or not all(low <= number(row.get(k)) <= high for k in ('open', 'close')):
            raise ValueError('Invalid adjusted OHLC')
        if number(row.get('volume_shares')) < 0 or number(row.get('turnover_pct')) < 0:
            raise ValueError('Invalid daily volume/native turnover')
    return data, rows, future


def chip_estimate(window):
    """Run the exact pinned JS; no replacement proxy when runtime is unavailable."""
    kernel = ROOT/'vendor/cyq.js'
    if hashlib.sha256(kernel.read_bytes()).hexdigest() != MODEL['kernel_sha256']:
        raise ValueError('Pinned chip kernel changed')
    node = os.environ.get('NODE_BINARY') or shutil.which('node')
    if not node:
        raise ValueError('Node runtime unavailable for pinned model')
    payload = [{**{k:float(number(b[k])) for k in ('open', 'close', 'high', 'low')},
                'date':b['date'], 'hsl':float(number(b['turnover_pct']))} for b in window]
    run = subprocess.run([node, str(ROOT/'scripts/chips_runner.js')], input=json.dumps(payload),
                         capture_output=True, text=True, timeout=15, check=True)
    result = json.loads(run.stdout)
    if len(result) != 210 or any(x['date'] != b['date'] or not 0 <= number(x['fraction']) <= 1
                                 for x,b in zip(result, window)):
        raise ValueError('Chip result unit/date mismatch')
    return result


def chip(stock, signal, days, daily, bars, calculator):
    window = [bars[d] for d in sorted(bars)][-210:]
    if len(window) != 210 or window[-1]['date'] != signal:
        raise ValueError('210 completed daily bars required')
    # Missing exchange sessions may only be skipped if suspension is evidenced.
    states, _ = indexed(stock.get('history', []), signal)
    for d in days:
        if window[0]['date'] <= d <= signal and d not in bars:
            state = (states.get(d) or {}).get('state', {})
            if not (state.get('verified') is True and state.get('source') and
                    state.get('date') == d and state.get('suspended') is True and not state.get('conflict')):
                raise ValueError('Unexplained missing daily session')
    history = calculator(window)
    if len(history) != 210 or any(x.get('date') != b['date'] or not 0 <= number(x.get('fraction')) <= 1
                                for x,b in zip(history,window)):
        raise ValueError('Chip fraction must use 0..1 and matching dates')
    evidence = {'model':MODEL['version'], 'kernel_sha256':MODEL['kernel_sha256'],
                'input_sha256':digest(window), 'source_fingerprint':digest(daily),
                'start':window[0]['date'], 'end':signal, 'bars':210,
                'adjustment':'qfq', 'adjustment_as_of':signal, 'unit':'fraction_0_1',
                'history':history[-90:], 'history_note':'同一210日输入的逐日前缀估算；早期点尚在预热，不是逐日210根滚动信号。'}
    return number(history[-1]['fraction']), evidence


def limit_days(stock, signal, sessions):
    records, _ = indexed(stock.get('history', []), signal)
    sealed, unknown = [], []
    for d in sessions[-60:]:
        r = records.get(d) or {}
        state = security(r.get('state'), d)
        if state == 'pending':
            unknown.append(d); continue
        if state == 'fail':
            continue
        if (r.get('verified') is not True or not r.get('source') or
                r.get('adjustment') != 'none' or r.get('conflict')):
            unknown.append(d); continue
        try:
            close, limit = number(r.get('close')), number(r.get('limit_up'))
            if close <= 0 or limit <= 0 or close > limit:
                raise ValueError('Conflicting actual close/limit')
            if close == limit:
                sealed.append(d)
        except (ValueError, TypeError):
            unknown.append(d)
    if len(sessions) < 60:
        unknown.append('60-session calendar incomplete')
    return sealed, unknown


def step(state, reason):
    return {'state':state, 'reason':reason}


def risk_metrics(bars, days):
    """Descriptive evidence only: no new eligibility threshold or score."""
    out={}
    last=number(bars[days[-1]]['close'])
    for n in (5,20):
        if all(d in bars for d in days[-n-1:]):
            out[f'return_{n}d']=float(last/number(bars[days[-n-1]]['close'])-1)
    if all(d in bars for d in days[-6:]):
        mean=sum(number(bars[d]['volume_shares']) for d in days[-6:-1])/5
        if mean>0: out['volume_vs_prior5_mean']=float(number(bars[days[-1]]['volume_shares'])/mean)
    if all(d in bars for d in days[-20:]):
        low=min(number(bars[d]['low']) for d in days[-20:])
        high=max(number(bars[d]['high']) for d in days[-20:])
        if high>low: out['position_20d']=float((last-low)/(high-low))
    return out


def evaluate(data, *, as_of=None, calculator=chip_estimate):
    """Input is verified evidence, never a precomputed list of passing symbols."""
    now = as_of or dt.datetime.now(TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    now = now.astimezone(TZ)
    signal = day(data['signal_date'])
    if dt.datetime.fromisoformat(signal+'T15:00:00+08:00') > now:
        raise ValueError('Signal close is unfinished or in the future')
    cal = data.get('calendar') or {}
    rawdays = cal.get('days', [])
    if (cal.get('verified') is not True or not cal.get('source') or
            rawdays != sorted(set(rawdays)) or signal not in rawdays):
        raise ValueError('Verified exchange calendar required')
    days = [day(d) for d in rawdays if d <= signal]
    if len(days) < 60:
        raise ValueError('At least 60 exchange sessions required')
    stocks = data.get('stocks', [])
    codes = [r.get('code') for r in stocks]
    if len(codes) != len(set(codes)) or any(not mainboard(c) for c in codes):
        raise ValueError('Unique mainboard identifiers required')
    universe = data.get('universe') or {}
    roster = universe.get('codes', [])
    whole = (universe.get('verified') is True and universe.get('date') == signal and bool(universe.get('source'))
             and not universe.get('conflict') and len(roster) == len(set(roster))
             and set(roster) == set(codes) and type(universe.get('expected_count')) is int
             and universe['expected_count'] == len(codes) and len(codes) > 0)
    states = {r['code']:security(r.get('state'),signal) for r in stocks}
    values = {}
    for s in stocks:
        if states[s['code']] == 'pass':
            try: values[s['code']] = turnover(s, signal)
            except (ValueError, TypeError): pass
    rank_complete = whole and 'pending' not in states.values() and len(values) == sum(x=='pass' for x in states.values())
    provisional = {code:i+1 for i,code in enumerate(sorted(values, key=lambda c:(-values[c],c)))}
    records, missing = [], list(data.get('missing', []))
    if not rank_complete:
        missing.append('完整合格主板池、证券状态或同日换手证据不足，不能确认全池前500资格。')
    for stock in stocks:
        code = stock['code']
        r = {'code':code, 'name':stock.get('name',code), 'research_passed':False, 'eligible':False,
             'scope_state':states[code], 'steps':[], 'metrics':{}, 'missing':[], 'bars':[],
             'sources':stock.get('sources', []), 'risk':stock.get('risk', ['公告、近期涨幅和交易风险仍需复核；高获利比例不证明安全。'])}
        r['metrics']['turnover_rank'] = provisional.get(code) if rank_complete else None
        r['metrics']['turnover_fraction'] = float(values[code]) if code in values else None
        def finish():
            while len(r['steps']) < 3:
                r['steps'].append(step('skipped','前一步未通过，本步骤未进入。'))
            records.append(r)
        if states[code] != 'pass':
            r['steps'].append(step(states[code], '证券范围或当日交易状态不合格。' if states[code]=='fail' else '当日证券状态待核验。'))
            finish(); continue
        try:
            daily, bars, future = price_rows(stock, signal, days)
            r['future_bars_ignored'] = future
            closes = [number(bars[d]['close']) for d in days[-4:]]
            returns = closes[-1]/closes[0]-1
            r['metrics']['three_day_return'] = float(returns)
            r['metrics'].update(risk_metrics(bars,days))
            r['bars'] = [bars[d] for d in sorted(bars)[-60:]]
            if not (all(b>a for a,b in zip(closes,closes[1:])) and returns <= Decimal('.05')):
                r['steps'].append(step('fail','连续三日严格上涨或累计涨幅≤5%不满足。')); finish(); continue
            historical, _ = indexed(stock.get('history',[]),signal)
            today=historical.get(signal) or {}
            if (today.get('verified') is True and today.get('adjustment')=='none' and
                    number(today.get('close')) != closes[-1]):
                # Forward-adjusted prices on their declared adjustment cutoff
                # have the same terminal close as unadjusted prices.
                raise ValueError('Conflicting signal-day raw and adjusted closes')
            if code in values and number(stock['turnover']['volume_shares']) != number(bars[signal]['volume_shares']):
                raise ValueError('Conflicting signal-day share volume')
            recent_states = [security((historical.get(d) or {}).get('state'),d) for d in days[-4:-1]]
            if 'fail' in recent_states:
                r['steps'].append(step('fail','最近连续窗口包含不合格交易状态，不能向前补日。')); finish(); continue
            if 'pending' in recent_states:
                raise ValueError('Recent session states not verified')
            fraction, evidence = chip(stock,signal,days,daily,bars,calculator)
            r['metrics']['profit_fraction'] = float(fraction)
            r['chip'] = evidence
        except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as exc:
            message=str(exc)
            if isinstance(exc,KeyError):
                issue='历史交易日或必要字段缺失；不向前补齐。'
            elif 'Conflicting' in message or 'mismatch' in message:
                issue='来源、同日记录或口径冲突；须先解决冲突。'
            elif '210' in message or 'missing daily' in message:
                issue='210根完整窗口不足，或缺少交易日/停牌证据。'
            elif 'states' in message:
                issue='连续窗口的历史证券状态待验证。'
            elif 'qfq' in message:
                issue='同源同信号日前复权截面未核验。'
            else:
                issue='价格、筹码模型或输入单位核验未通过；不能替换指标。'
            r['steps'].append(step('pending',issue))
            r['missing'].append(issue);r['error_type']=type(exc).__name__; finish(); continue
        if fraction <= Decimal('.60'):
            r['steps'].append(step('fail','估算获利比例未严格大于60%。')); finish(); continue
        r['steps'].append(step('pass','三日温和上涨且估算获利比例>60%。'))
        if fraction <= Decimal('.70'):
            r['steps'].append(step('fail','估算获利比例未严格大于70%。')); finish(); continue
        if not rank_complete:
            r['steps'].append(step('pending','不能确认完整主板池换手前500。')); finish(); continue
        if provisional[code] > 500:
            r['steps'].append(step('fail','完整主板池换手名次在500名以外。')); finish(); continue
        r['steps'].append(step('pass','全池换手前500且估算获利比例>70%。'))
        sealed, unknown = limit_days(stock,signal,days)
        r['metrics'].update(limit_close_dates=sealed, limit_close_count=len(sealed))
        r['missing_limit_dates'] = unknown
        if fraction <= Decimal('.80'):
            r['steps'].append(step('fail','估算获利比例未严格大于80%。'))
        elif unknown:
            r['steps'].append(step('pending','60日历史状态、未复权收盘或实际涨停价缺失/冲突。'))
        elif len(sealed) < 3:
            r['steps'].append(step('fail','60交易日收盘封板少于3次；触板不计。'))
        else:
            r['steps'].append(step('pass','60交易日至少3次收盘封板且估算获利比例>80%。'))
            r['research_passed'] = True
        finish()
    counts = []
    for i in range(3):
        tally = {s:sum(r['steps'][i]['state']==s for r in records) for s in ('pass','fail','pending','skipped')}
        counts.append({'stage':i+1, 'entered':sum(tally[s] for s in ('pass','fail','pending')), **tally})
    pending = sum(any(s['state']=='pending' for s in r['steps']) for r in records)
    if pending:
        missing.append(f'{pending}只股票的必要筛选证据待验证，未确认通过不等于市场空池。')
    final = sorted((r for r in records if r['research_passed']),key=lambda r:(r['metrics']['turnover_rank'],r['code']))
    status = 'partial' if missing or pending or not rank_complete else 'complete' if final else 'empty'
    subsequent = [d for d in rawdays if d > signal]
    until = (subsequent[0] if subsequent else signal)+'T15:00:00+08:00'
    return {'schema_version':1, 'module_id':'three-step', 'rule_version':VERSION, 'model':MODEL,
            'signal_date':signal, 'as_of':signal+'T15:00:00+08:00', 'phase':'close',
            'generated_at':dt.datetime.now(TZ).isoformat(), 'valid_until':until, 'status':status,
            'summary':f'逐层研究确认通过{len(final)}只；待验证{pending}只。按全池换手名次展示，不是收益概率排名。',
            'coverage':{'roster_received':len(stocks), 'roster_expected':universe.get('expected_count'),
                        'security_verified':sum(x!='pending' for x in states.values()),
                        'eligible_mainboard':sum(x=='pass' for x in states.values()),
                        'turnover_verified':len(values), 'global_rank_verified':rank_complete,
                        'price_computed':sum('three_day_return' in r['metrics'] for r in records),
                        'chip_computed':sum('chip' in r for r in records)},
            'funnel':counts, 'rows':records, 'candidates':final, 'missing':list(dict.fromkeys(missing)),
            'sources':data.get('sources',[]), 'input_fingerprint':digest(data),
            'data_notes':['全部沪深主板；筹码为东财模型估算，不代表真实账户盈利或主力持仓。',
                          '210根完整前复权日K与原生换手；收盘封板用独立未复权证据。',
                          '未设买卖、仓位或收益保证，规则校验不是收益回测。']}
