"""Bounded, read-only half-hour Eastmoney industry capital-flow research.

No stock strategy is run. Actual sector constituents follow the vendor's whole
A-share industry classification, including ChiNext/STAR stocks. Runtime evidence
and reports are saved only through private research storage.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import math
import re
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from market_calendar import is_trading_day
from research_store import atomic_json, read_json, research_path, update_lock

TZ = ZoneInfo('Asia/Shanghai')
VERSION = '1.0.0'
SOURCE = 'https://data.eastmoney.com/bkzj/hy.html'
SCOPE = 'm:90+t:2+f:!50'
SLOTS = ('09:30', '10:00', '10:30', '11:00', '11:30',
         '13:00', '13:30', '14:00', '14:30', '15:00')
HOSTS = ('push2.eastmoney.com', 'push2delay.eastmoney.com')


def stamp(value):
    if isinstance(value, dt.datetime):
        return value.astimezone(TZ) if value.tzinfo else value.replace(tzinfo=TZ)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return dt.datetime.fromtimestamp(value, TZ)
    try:
        out = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return out.astimezone(TZ) if out.tzinfo else out.replace(tzinfo=TZ)
    except (ValueError, TypeError, OverflowError):
        return None


def number(value):
    if value is None or isinstance(value, bool) or value == '':
        raise ValueError('数值缺失')
    out = float(value)
    if not math.isfinite(out):
        raise ValueError('数值无效')
    return out


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def folder():
    return research_path('sector_fund_pulse')


def session(value):
    value = stamp(value)
    clock = value.time().replace(tzinfo=None)
    if dt.time(9, 30) <= clock <= dt.time(11, 30):
        return 'morning'
    if dt.time(13) <= clock <= dt.time(15):
        return 'afternoon'
    return None


def schedule_context(now, scheduled=False):
    now = stamp(now)
    trading, reason = is_trading_day(now.date())
    if not trading:
        if '缺少' in reason or '不可用' in reason:
            return {'run': True, 'error': reason, 'slot': None}
        return {'run': False, 'reason': reason, 'slot': None}
    clock = now.time().replace(tzinfo=None)
    close_grace = any(dt.time(*parts) <= clock <= dt.time(*end)
                      for parts, end in [((11, 30), (11, 32)), ((15, 0), (15, 2))])
    if not session(now) and not close_grace:
        return {'run': False, 'reason': '非连续交易时段，静默跳过', 'slot': None}
    if not scheduled:
        return {'run': True, 'slot': None}
    for item in reversed(SLOTS):
        slot = dt.datetime.combine(now.date(), dt.time.fromisoformat(item), TZ)
        maximum = 120 if item in ('11:30', '15:00') else 300
        if 0 <= (now - slot).total_seconds() <= maximum:
            return {'run': True, 'slot': slot.isoformat()}
    return {'run': False, 'reason': '已超过本轮采样容许时间，不能回填旧时点', 'slot': None}


def _fresh(source, now):
    if source is None or source.date() != now.date() or not session(source):
        raise ValueError('资金自身时间不属于当日有效交易时段')
    age = (now - source).total_seconds()
    if not 0 <= age <= 90:
        raise ValueError('源数据未来或延迟超过90秒')


class Feed:
    """Two read-only vendor entrances, 5 s per request and global deadline."""
    def __init__(self, *, budget=70, timeout=5, hosts=HOSTS):
        self.deadline = time.monotonic() + budget
        self.timeout = timeout
        self.hosts = tuple(hosts)[:2]

    def eastmoney(self, endpoint, params):
        last = None
        for host in self.hosts:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('本轮70秒采集预算用尽')
            url = f'https://{host}/api/qt/{endpoint}?' + urlencode(params)
            try:
                request = Request(url, headers={'User-Agent': 'Mozilla/5.0',
                                                'Referer': 'https://quote.eastmoney.com/'})
                with urlopen(request, timeout=min(self.timeout, remaining)) as handle:
                    payload = json.load(handle)
                if not isinstance(payload, dict) or not isinstance(payload.get('data'), dict):
                    raise ValueError('无有效data')
                return payload['data'], url
            except Exception as exc:
                last = exc
        raise RuntimeError('行业资金接口不可用：' + type(last).__name__)


def collect_universe(feed, raw, *, clock=None):
    rows, total, page = [], None, 1
    while total is None or len(rows) < total:
        data, url = feed.eastmoney('clist/get', dict(pn=page, pz=100, np=1,
            fs=SCOPE, fields='f12,f14,f2,f3,f6,f62,f184,f124',
            fltt=2, fid='f12', po=0))
        raw.append({'kind': 'industry-page', 'page': page, 'source_url': url,
                    'retrieved_at': stamp(clock()).isoformat() if clock else None,
                    'source_fingerprint': digest(data), 'data': data})
        batch = data.get('diff')
        if isinstance(batch, dict):
            batch = list(batch.values())
        count = data.get('total')
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ValueError('行业全集total未知或无效')
        if total is not None and count != total:
            raise ValueError('行业分页total发生变化')
        total = count
        if not isinstance(batch, list) or not batch:
            raise ValueError('行业分页缺失')
        rows.extend(batch)
        if len(rows) > total or page >= 20:
            raise ValueError('行业分页记录数冲突')
        page += 1
    codes = [r.get('f12') for r in rows]
    if len(set(codes)) != total or any(not isinstance(c, str) or not re.fullmatch(r'BK\d+', c) for c in codes):
        raise ValueError('行业唯一代码/全量数量校验失败')
    if any(not isinstance(r.get('f14'), str) or not r['f14'] for r in rows):
        raise ValueError('行业身份缺失')
    return rows


def parse_minutes(data, code, now):
    if str(data.get('market')) != '90' or data.get('code') != code:
        raise ValueError('资金证券身份冲突')
    rows = data.get('klines')
    if not isinstance(rows, list) or not rows:
        raise ValueError('资金分钟资料缺失')
    parsed, seen, previous = [], set(), None
    for row in rows:
        cells = row.split(',') if isinstance(row, str) else []
        if len(cells) != 6 or not re.match(r'^\d{4}-\d\d-\d\d[ T]', cells[0]):
            raise ValueError('资金字段数/自身日期缺失')
        source = stamp(cells[0])
        if source is None or source.date() != now.date() or not session(source) or source > now:
            raise ValueError('资金自身日期/交易时段/未来数据无效')
        if source in seen or (previous is not None and source <= previous):
            raise ValueError('资金分钟重复或倒序')
        values = [number(x) for x in cells[1:]]
        if abs(values[0] - values[3] - values[4]) > 0.02:
            raise ValueError('主力净额不等于大单加超大单，字段或单位冲突')
        parsed.append({'time': source.isoformat(), 'main_cny': values[0],
                       'small_cny': values[1], 'medium_cny': values[2],
                       'large_cny': values[3], 'super_cny': values[4]})
        previous = source
        seen.add(source)
    latest = stamp(parsed[-1]['time'])
    _fresh(latest, now)
    anchor_time = latest - dt.timedelta(minutes=30)
    anchor = next((r for r in parsed if stamp(r['time']) == anchor_time), None)
    delta = None
    if anchor is not None and session(anchor_time) == session(latest):
        # Exactly 30 adjacent minutes, no missing minute may stand in for the baseline.
        window = [stamp(r['time']) for r in parsed if anchor_time <= stamp(r['time']) <= latest]
        if len(window) == 31 and all((b-a).total_seconds() == 60 for a, b in zip(window, window[1:])):
            delta = parsed[-1]['main_cny'] - anchor['main_cny']
    return parsed, delta


def inspect_industry(quote, data, now, source_url):
    code, name = quote['f12'], quote['f14']
    minutes, delta = parse_minutes(data, code, now)
    latest = minutes[-1]
    out = {'code': code, 'name': name, 'source_time': latest['time'],
           'main_cny': latest['main_cny'], 'delta_30m_cny': delta,
           'delta_anchor': ((stamp(latest['time']) - dt.timedelta(minutes=30)).isoformat()
                            if delta is not None else None),
           'source_url': source_url, 'quote_time': None, 'change_pct': None,
           'turnover_cny': None, 'native_net_pct': None, 'quote_main_cny': None,
           'quote_valid': False, 'notes': [], 'minutes_fingerprint': digest(minutes)}
    try:
        quote_time = stamp(number(quote.get('f124')))
        _fresh(quote_time, now)
        if abs((quote_time - stamp(latest['time'])).total_seconds()) > 60:
            raise ValueError('行业报价与资金时间差超过60秒')
        change, turnover = number(quote.get('f3')), number(quote.get('f6'))
        if turnover < 0:
            raise ValueError('成交额为负')
        out.update(quote_time=quote_time.isoformat(), change_pct=change,
                   turnover_cny=turnover, quote_valid=True)
        for field, target in [('f184', 'native_net_pct'), ('f62', 'quote_main_cny')]:
            try:
                out[target] = number(quote.get(field))
            except ValueError:
                pass
        if out['quote_main_cny'] is not None and abs(out['quote_main_cny']-out['main_cny']) > 0.02:
            out['notes'].append('clist累计与分钟资金存在时点/修订差异；排名使用分钟自身净额')
        if out['native_net_pct'] is not None:
            out['notes'].append('净占比为clist原生快照口径，不视作30分钟净流入率')
    except (ValueError, TypeError, OverflowError) as exc:
        out['notes'].append(str(exc))
    if delta is None:
        out['notes'].append('同交易时段连续30分钟锚点不足，仅展示累计')
    return out


def rank_rows(rows, *, expected_count, universe_complete):
    """Full-market comparisons require every sector's valid own-time capital."""
    missing = []
    full = universe_complete and expected_count > 0 and len(rows) == expected_count
    if rows:
        moments = [stamp(r['source_time']) for r in rows]
        spread = (max(moments)-min(moments)).total_seconds()
        if spread > 60:
            full = False
            missing.append('行业资金时间跨度超过60秒，不能作完整全池比较')
    if not full:
        missing.append('行业全集或有效资金覆盖不完整，不发布全市场前十及关注前三')
    ordered = sorted(rows, key=lambda r: (-r['main_cny'], r['code']))
    for index, row in enumerate(ordered, 1):
        row['rank'] = index if full else None
    leaders = ordered[:10]
    eligible = [r for r in ordered if r['main_cny'] > 0 and r['delta_30m_cny'] is not None
                and r['delta_30m_cny'] > 0 and r['quote_valid'] and r['change_pct'] > 0]
    # Every industry's auxiliary quote evidence is required for a comparative top 3.
    quote_times = [stamp(r['quote_time']) for r in ordered if r['quote_valid']]
    quote_aligned = bool(quote_times) and (max(quote_times)-min(quote_times)).total_seconds() <= 60
    priority_complete = full and all(r['quote_valid'] for r in ordered) and quote_aligned
    priorities = sorted(eligible, key=lambda r: (-r['delta_30m_cny'], -r['main_cny'], r['code']))[:3] if priority_complete else []
    if full and not priority_complete:
        missing.append('部分行业缺同刻涨幅证据，关注前三暂不比较')
    for index, row in enumerate(priorities, 1):
        row['attention_rank'] = index
    return leaders if full else [], priorities, ordered, missing


def compare_previous(report, previous):
    if not previous or report['status'] != 'complete' or previous.get('status') != 'complete':
        return {'available': False, 'reason': '仅同日完整可比两轮显示变化'}
    now, old = stamp(report['sampled_at']), stamp(previous.get('sampled_at'))
    if (old is None or old.date() != now.date() or not 0 < (now-old).total_seconds() <= 2700
            or session(old) != session(now) or report['universe_fingerprint'] != previous.get('universe_fingerprint')):
        return {'available': False, 'reason': '日期、时段、全集或45分钟比较窗口不匹配'}
    before = {r['code']: r for r in previous['all_rows']}
    if any(stamp(r['source_time']) <= stamp(before[r['code']]['source_time']) for r in report['all_rows']):
        return {'available': False, 'reason': '源时间未前进或回退，不将修订解释为资金转向'}
    old_top = {r['code']: r for r in previous['top10']}
    new_top = {r['code']: r for r in report['top10']}
    return {'available': True,
            'previous_sampled_at': previous['sampled_at'],
            'new': [r['name'] for r in report['top10'] if r['code'] not in old_top],
            'out': [r['name'] for r in previous['top10'] if r['code'] not in new_top],
            'rank_changes': {r['code']: before[r['code']]['rank']-r['rank'] for r in report['top10']}}


def collect(now, feed, *, slot=None, clock=None):
    raw, rows, failures = [], [], []
    universe = []
    complete = False
    started = time.monotonic()
    try:
        universe = collect_universe(feed, raw, clock=clock or (lambda: now))
        complete = True
    except Exception as exc:
        failures.append('行业全集：' + str(exc))
    if universe:
        pool = ThreadPoolExecutor(max_workers=6)
        def task(quote):
            data, url = feed.eastmoney('stock/fflow/kline/get', dict(secid='90.'+quote['f12'],
                klt=1, lmt=0, fields1='f1,f2,f3,f7', fields2='f51,f52,f53,f54,f55,f56',
                ut='b2884a393a59ad64002292a3e90d46a5'))
            return data, url
        futures = {pool.submit(task, quote): quote for quote in universe}
        pending = set(futures)
        deadline = getattr(feed, 'deadline', started+70)
        while pending and time.monotonic() < deadline:
            done, pending = wait(pending, timeout=min(0.5, max(0, deadline-time.monotonic())), return_when=FIRST_COMPLETED)
            for future in done:
                quote = futures[future]
                try:
                    data, url = future.result()
                    retrieved = stamp(clock()) if clock else now
                    raw.append({'kind': 'industry-minutes', 'code': quote['f12'], 'source_url': url,
                                'retrieved_at': retrieved.isoformat(), 'source_fingerprint': digest(data), 'data': data})
                    rows.append(inspect_industry(quote, data, retrieved, url))
                except Exception as exc:
                    failures.append(quote['f12']+' '+quote['f14']+'：'+str(exc))
        for future in pending:
            quote = futures[future]
            future.cancel()
            failures.append(quote['f12']+' '+quote['f14']+'：本轮采集预算用尽')
        pool.shutdown(wait=False, cancel_futures=True)
    finished = stamp(clock()) if clock else now
    valid = []
    for row in rows:
        try:
            _fresh(stamp(row['source_time']), finished)
        except ValueError as exc:
            failures.append(row['code']+' '+row['name']+'：最终复核'+str(exc))
            continue
        if row['quote_valid']:
            try:
                _fresh(stamp(row['quote_time']), finished)
            except ValueError as exc:
                row['quote_valid'] = False
                row['change_pct'] = row['turnover_cny'] = row['native_net_pct'] = None
                row['notes'].append('最终报价复核：'+str(exc))
        valid.append(row)
    rows = valid
    top, priorities, all_rows, missing = rank_rows(rows, expected_count=len(universe), universe_complete=complete)
    missing += failures
    full = bool(complete and len(rows) == len(universe) and top)
    status = 'complete' if full and all(r['quote_valid'] for r in rows) else 'partial' if rows else 'unavailable'
    report = {'schema_version': 1, 'module_id': 'sector-fund-pulse', 'rule_version': VERSION,
        'started_at': now.isoformat(), 'sampled_at': finished.isoformat(), 'generated_at': finished.isoformat(), 'slot': slot,
        'scope': '东财全A股行业分类，含科创板与创业板成分；不是主板限定行业资金',
        'status': status, 'ranking_complete': full, 'universe_complete': complete,
        'coverage': {'expected': len(universe) if complete else None, 'valid_funds': len(rows),
                     'valid_quotes': sum(r['quote_valid'] for r in rows), 'failed_funds': len(universe)-len(rows)},
        'universe_fingerprint': digest(sorted(r['f12'] for r in universe)) if complete else None,
        'top10': top, 'attention_top3': priorities, 'all_rows': all_rows,
        'partial_reference': sorted(rows, key=lambda r: (-r['main_cny'], r['code']))[:10] if not full else [],
        'missing': missing, 'method': '累计净额降序取前十；关注仅累计净额、同场连续30分钟净增额、同刻涨幅均正，按30分钟净增额、累计净额、代码排序。',
        'source_url': SOURCE, 'raw_fingerprint': digest(raw),
        'failure_signature': digest({'missing': sorted(missing),
            'quote_faults': sorted((r['code'], r['notes']) for r in rows if not r['quote_valid']),
            'coverage': [len(universe) if complete else None, len(rows), sum(r['quote_valid'] for r in rows)]}),
        'changes': {'available': False}}
    return report, raw


def edition(report):
    return digest(report)


def decision(report):
    state = read_json(folder()/'delivery.json', {})
    version = edition(report)
    if version in state.get('sent', {}):
        return {'notify': False, 'reason': '本版本已发送', 'edition': version}
    if report.get('status') == 'skipped':
        return {'notify': False, 'reason': report.get('reason', '静默跳过'), 'edition': version}
    fault = report['status'] != 'complete'
    old_fault = state.get('fault_signature')
    signature = report.get('failure_signature') if fault else None
    if report.get('ranking_complete'):
        if report.get('slot') and any(r.get('slot') == report['slot'] and r.get('ranking_complete') is True
                                      for r in state.get('sent', {}).values()):
            return {'notify': False, 'reason': '本时点资金榜已发送', 'edition': version}
        reason = '半小时行业资金报告'
        if fault and old_fault != signature:
            reason += '；附新数据缺口'
        elif not fault and old_fault:
            reason += '；数据已恢复'
        return {'notify': True, 'reason': reason, 'edition': version}
    if fault and old_fault == signature:
        return {'notify': False, 'reason': '相同故障持续，保留页面不重复提醒', 'edition': version}
    reason = '首次数据故障或覆盖变化' if fault else '资金数据恢复' if old_fault else '半小时行业资金报告'
    return {'notify': True, 'reason': reason, 'edition': version}


def publish(report, raw):
    version = edition(report)
    with update_lock():
        existing = read_json(folder()/'current.json', {})
        if stamp(existing.get('sampled_at')) and stamp(existing['sampled_at']) > stamp(report['sampled_at']):
            return {'changed': False, 'notify': False, 'reason': '已有更新采样', 'edition': edition(existing)}
        atomic_json(folder()/'raw'/(report['raw_fingerprint']+'.json'), raw)
        atomic_json(folder()/'history'/(version+'.json'), report)
        atomic_json(folder()/'latest_attempt.json', report)
        atomic_json(folder()/'current.json', report)
        if report['status'] == 'complete':
            atomic_json(folder()/'last_success.json', report)
        if report.get('slot'):
            slots = read_json(folder()/'slots.json', {})
            slots[report['slot']] = {'edition': version, 'status': report['status'],
                                      'ranking_complete': report['ranking_complete']}
            atomic_json(folder()/'slots.json', slots)
    return {'changed': edition(existing) != version, **decision(report)}


def amount(value):
    return '缺失' if value is None else f'{value/1e8:+.2f}亿元'


def topic(report):
    now = stamp(report['generated_at'])
    cutoff = min([stamp(r['source_time']) for r in report['all_rows']], default=now)
    rows = []
    for row in (report['top10'] or report['partial_reference']):
        rows.append(dict(name=row['name']+' '+row['code'],
            industry_state=(f"累计第{row['rank']}名 · " if row['rank'] else '局部参考，不授予全池名次 · ')+amount(row['main_cny']),
            market_state=(f"涨幅{row['change_pct']:+.2f}%" if row['change_pct'] is not None else '涨幅缺失')+'；成交额'+amount(row['turnover_cny']),
            evidence='同场30分钟净增额'+amount(row['delta_30m_cny'])+'；资金时间'+row['source_time'],
            counter='；'.join(row['notes']) or '主力为供应商大单分类统计，不能识别真实账户',
            next='累计和30分钟均为正、涨幅为正后复核；转负或板块走弱则降序关注'))
    priority = [f"优先关注{r['attention_rank']}：{r['name']}；30分钟净增额{amount(r['delta_30m_cny'])}，累计{amount(r['main_cny'])}。" for r in report['attention_top3']]
    changes = priority or ['暂无完整证据支持关注前三；累计排名与窗口积累情况见表。']
    c = report.get('changes', {})
    if c.get('available'):
        changes.append('新入前十：'+('、'.join(c['new']) or '无')+'；退出前十：'+('、'.join(c['out']) or '无'))
    return {'schema_version': 1, 'topic_id': 'sector-fund-pulse', 'title': '半小时行业资金雷达',
            'as_of': cutoff.isoformat(), 'generated_at': now.isoformat(),
            'valid_until': (cutoff+dt.timedelta(seconds=90)).isoformat() if report['all_rows'] else now.isoformat(),
            'status': report['status'],
            'summary': f"采样{report['sampled_at']}；行业资金有效{report['coverage']['valid_funds']}/{report['coverage']['expected'] if report['coverage']['expected'] is not None else '全集未知'}。{report['scope']}。{report['method']}",
            'changes': changes, 'directions': rows, 'missing': report['missing'],
            'sources': [{'label': '东方财富行业资金', 'url': SOURCE,
                         'published_at': cutoff.isoformat() if report['all_rows'] else '未取得源资金时间',
                         'observation_period': '当日累计及同一上午/下午连续30分钟', 'retrieved_at': now.isoformat()}]}


@contextlib.contextmanager
def run_lock():
    path = folder()/'run.lock'
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def run(*, now=None, feed=None, scheduled=False, no_page=False):
    actual = stamp(now) if now is not None else dt.datetime.now(TZ)
    context = schedule_context(actual, scheduled)
    if not context['run']:
        return {'status': 'skipped', 'notify': False, 'reason': context['reason']}
    with run_lock():
        if context.get('slot'):
            slots = read_json(folder()/'slots.json', {})
            previous_slot = slots.get(context['slot'])
            if previous_slot and (isinstance(previous_slot, str) or previous_slot.get('status') == 'complete' or previous_slot.get('ranking_complete') is True):
                previous_version = previous_slot if isinstance(previous_slot, str) else previous_slot['edition']
                return {'status': 'duplicate', 'notify': False, 'reason': '本半小时槽已成功采样', 'edition': previous_version}
        if context.get('error'):
            report, raw = collect(actual, _BrokenFeed(context['error']), slot=context['slot'],
                                  clock=None if now is not None else lambda: dt.datetime.now(TZ))
        else:
            report, raw = collect(actual, feed or Feed(), slot=context['slot'],
                                  clock=None if now is not None else lambda: dt.datetime.now(TZ))
        report['changes'] = compare_previous(report, read_json(folder()/'current.json', {}))
        result = publish(report, raw)
    result['status'] = report['status']
    if not no_page and result.get('changed'):
        try:
            from research_topics import publish_topic
            from build_investment_site import build
            publish_topic(topic(report), now=stamp(report['generated_at']))
            build()
            result['page_status'] = 'updated'
        except Exception as exc:
            result['page_status'] = 'failed'
            result['page_error'] = str(exc)
    return result


class _BrokenFeed:
    def __init__(self, reason): self.reason = reason
    def eastmoney(self, *args): raise RuntimeError(self.reason)


def mark_sent(version):
    with update_lock():
        report = read_json(folder()/'current.json', {})
        if edition(report) != version:
            raise ValueError('报告版本已变化，未登记发送')
        state = read_json(folder()/'delivery.json', {})
        state.setdefault('sent', {})[version] = {'sampled_at': report['sampled_at'], 'slot': report.get('slot'),
                                               'ranking_complete': report.get('ranking_complete', False),
                                               'recorded_at': dt.datetime.now(TZ).isoformat()}
        state['fault_signature'] = report['failure_signature'] if report['status'] != 'complete' else None
        atomic_json(folder()/'delivery.json', state)


def markdown(report):
    lines = [f"**行业资金雷达 · {report['sampled_at']}**", '',
             report['scope']+'。', '',
             f"覆盖：{report['coverage']['valid_funds']}/{report['coverage']['expected'] if report['coverage']['expected'] is not None else '全集未知'}；状态：{report['status']}。", '']
    if not report['ranking_complete']:
        lines.append('未取得完整同期行业资金，暂不发布全市场前十及优先关注排名。')
    if report['top10']:
        lines += ['|排名（较上轮）|行业|当日累计净额|同场30分钟增量|涨幅|成交额|盘口原生净占比|源资金时间|',
                  '|---:|---|---:|---:|---:|---:|---:|---|']
        for r in report['top10']:
            change = '缺失' if r['change_pct'] is None else f"{r['change_pct']:+.2f}%"
            native = '缺失' if r['native_net_pct'] is None else f"{r['native_net_pct']:+.2f}%"
            delta = report.get('changes', {}).get('rank_changes', {}).get(r['code'])
            rank_change = f'（{delta:+d}）' if delta is not None else ''
            lines.append(f"|{r['rank']}{rank_change}|{r['name']}|{amount(r['main_cny'])}|{amount(r['delta_30m_cny'])}|{change}|{amount(r['turnover_cny'])}|{native}|{r['source_time'][11:19]}|")
    if report['attention_top3']:
        lines += ['', '**优先复核方向**（按30分钟净增额排序）']
        for r in report['attention_top3']:
            lines.append(f"{r['attention_rank']}. {r['name']}：30分钟{amount(r['delta_30m_cny'])}，累计{amount(r['main_cny'])}，涨幅{r['change_pct']:+.2f}%。")
    else:
        lines += ['', '关注前三暂空：窗口积累或同刻证据不足时继续等待。']
    if report.get('changes', {}).get('available'):
        lines += ['', '新入前十：'+('、'.join(report['changes']['new']) or '无')+'；退出前十：'+('、'.join(report['changes']['out']) or '无')+'。']
    if report['missing']:
        lines += ['', '数据缺口：'+'；'.join(report['missing'][:4])+('；其余见本地完整记录。' if len(report['missing']) > 4 else '。')]
    lines += ['', '主力净额为供应商分类统计；盘口原生净占比属于clist快照口径，可能与分钟净额存在时点或修订差异。排序表示当前资金关注证据，不是收益概率。',
              f'[资金来源]({SOURCE}) · [本地页面](http://127.0.0.1:8767/latest.html#topic-sector-fund-pulse)']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--scheduled', action='store_true')
    parser.add_argument('--no-page', action='store_true')
    parser.add_argument('--markdown', action='store_true')
    parser.add_argument('--decision', action='store_true')
    parser.add_argument('--mark-sent')
    args = parser.parse_args()
    if args.run:
        print(json.dumps(run(scheduled=args.scheduled, no_page=args.no_page), ensure_ascii=False))
    else:
        report = read_json(folder()/'current.json')
        if not report:
            raise SystemExit('尚无行业资金研究报告')
        if args.mark_sent:
            mark_sent(args.mark_sent)
        print(markdown(report) if args.markdown else json.dumps(decision(report), ensure_ascii=False))


if __name__ == '__main__':
    main()
