"""Independent Eastmoney model-main-flow history for a frozen industry pool.

Network runs only through explicit ``collect``. Renderers and compute helpers are
offline; this module never reads or writes the THS current funds reference.
"""
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import copy
import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlencode

from etf_monitor import TZ, PRIVATE, _date, _market_name, _now, _stamp
from market_calendar import is_trading_day
from research_store import atomic_json, load_config, read_json, research_path, update_lock
from sector_fund_catalog import CATALOG_URLS, parse_catalog_html

HISTORY_URL = 'https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get'
UNIT_URL = 'https://data.eastmoney.com/newstatic/js/bkzj/detail.js'
CODE_URL = 'https://github.com/akfamily/akshare/blob/master/akshare/stock/flow/stock_fund_em.py'
METRIC = '东财模型主力净额'
SCOPE = '本轮冻结行业池的源生资金历史回溯；当前分类不代表过去各日当时的行业目录或成分，未证明历史分类生效日或成分互斥，不能跨行业求和称全市场净额。'
AMOUNTS = ('main_net_yuan', 'large_net_yuan', 'superlarge_net_yuan')
MAX_BODY = 2 * 1024 * 1024


def _digest(value):
    body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    return hashlib.sha256(body).hexdigest()


def trading_window(cutoff, days=20, *, now=None, calendar=None):
    """Resolve actual exchange sessions, refusing missing-year calendar coverage."""
    now = _now(now)
    day = _date(cutoff) if isinstance(cutoff, str) else cutoff
    if not isinstance(day, dt.date) or isinstance(day, dt.datetime) or type(days) is not int or not 1 <= days <= 120:
        raise ValueError('Invalid historical window')
    if day > now.date() or (day == now.date() and now.time().replace(tzinfo=None) < dt.time(15)):
        raise ValueError('Cutoff must be a completed CN trading day')
    cal = calendar if calendar is not None else load_config('market_calendar.json')
    if not isinstance(cal, dict) or not isinstance(cal.get('closures'), dict):
        raise ValueError('Exchange calendar is unavailable')
    result, cursor = [], day
    while len(result) < days:
        closures = cal['closures'].get(str(cursor.year))
        if not isinstance(closures, list) or any(not _date(x) for x in closures):
            raise ValueError('Exchange calendar year is missing or invalid')
        ok, _ = is_trading_day(cursor, config=cal)
        if cursor == day and not ok:
            raise ValueError('Cutoff is not an exchange trading day')
        if ok:
            result.append(cursor.isoformat())
        cursor -= dt.timedelta(days=1)
        if (day-cursor).days > 400:
            raise ValueError('Exchange calendar cannot resolve the window')
    return list(reversed(result))


def parse_catalog_pair(documents, *, now=None):
    """Match two current complete selectors; never treat mixed BK links as industries."""
    now = _now(now)
    if not isinstance(documents, list) or len(documents) != 2:
        raise ValueError('Both current industry selectors are required')
    groups = []
    clocks = []
    for expected, item in zip(CATALOG_URLS, documents):
        if not isinstance(item, dict) or item.get('source') != expected:
            raise ValueError('Unexpected catalog source')
        at = _stamp(item.get('retrieved_at'))
        body = item.get('body')
        if at is None or at > now or at.date() != now.date() or not isinstance(body, str):
            raise ValueError('Catalog retrieval clock is not current')
        parsed = parse_catalog_html(body)
        match = re.search(r'<!--\s*(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) at server\[', body)
        rendered = dt.datetime.fromisoformat(match.group(1)).replace(tzinfo=TZ) if match else None
        if rendered is None or rendered.date() != now.date() or rendered > at + dt.timedelta(seconds=60):
            raise ValueError('Catalog generation date is missing, old or future')
        groups.append(parsed); clocks.append(at)
    if groups[0] != groups[1]:
        raise ValueError('The two classified selectors disagree')
    identities = [{'code': row['f12'], 'name': _market_name(row['f14'])} for row in groups[0][0]]
    catalog = {'count': len(identities), 'complete': True, 'classification': '东方财富行业',
               'source': CATALOG_URLS[0], 'as_of': max(clocks).date().isoformat(),
               'scope_note': SCOPE, 'coverage_basis': '两份官方完整非分页行业选择器逐代码名称匹配；不是clist.total',
               'source_total': None, 'group_counts': [len(x) for x in groups[0]], 'fingerprint': _digest(identities)}
    return catalog, identities


def verify_unit_script(body):
    """The official daily chart maps unscaled arr[1..5] and labels it CNY yuan."""
    if not isinstance(body, str):
        return False
    marker = body.find('drawchart_phqs: function')
    excerpt = body[marker:marker+2500] if marker >= 0 else ''
    return ("单位:元" in excerpt and 'data1.push(arr[1])' in excerpt
            and 'data5.push(arr[5])' in excerpt and 'fflow/daykline/get' in body)


def parse_history(payload, identity, dates, *, now=None, retrieved_at=None):
    """Verify identity, ordered unique dates and exact Decimal arithmetic per day."""
    now = _now(now)
    code, name = identity['code'], identity['name']
    result = {'code': code, 'name': name, 'rows': [], 'missing_dates': list(dates), 'issues': [],
              'ignored_after_cutoff': [], 'retrieved_at': retrieved_at}
    at = _stamp(retrieved_at)
    if at is None or at > now or at < dt.datetime.combine(dt.date.fromisoformat(dates[-1]), dt.time(15), TZ):
        result['issues'] = ['历史资金取得时点早于窗口末日收盘、缺失或晚于当前时点。']; return result
    data = payload.get('data') if isinstance(payload, dict) else None
    if (not isinstance(payload, dict) or payload.get('rc') != 0 or not isinstance(data, dict)
            or data.get('code') != code or data.get('name') != name or data.get('market') != 90):
        result['issues'] = ['历史资金响应身份与本轮冻结行业目录不一致。']; return result
    raw = data.get('klines')
    if not isinstance(raw, list) or not raw or len(raw) > 5000:
        result['issues'] = ['历史资金日线缺失或记录数异常。']; return result
    previous = ''
    for line in raw:
        fields = line.split(',') if isinstance(line, str) else []
        day = _date(fields[0]) if fields else None
        if day is None or day.isoformat() <= previous or day > now.date():
            result['rows'] = []; result['issues'].append('源日线日期无效、未升序、重复或含真正未来日期。'); return result
        previous = day.isoformat()
        if previous > dates[-1]:
            result['ignored_after_cutoff'].append(previous)
            continue
        if previous not in dates:
            continue
        if day == now.date() and now.time().replace(tzinfo=None) < dt.time(15):
            result['issues'].append('尚未完成的当日日线已排除。'); continue
        if len(fields) != 15:
            result['issues'].append(previous + ' 资金字段数量变化。'); continue
        try:
            # Source order: main, small, medium, large, superlarge, ratios, unused.
            values = [Decimal(fields[index]) for index in range(1, 11)]
            if not all(x.is_finite() for x in values):
                raise InvalidOperation
        except InvalidOperation:
            result['issues'].append(previous + ' 资金字段非有限数值。'); continue
        main, large, superlarge = values[0], values[3], values[4]
        if main != large + superlarge:
            result['issues'].append(previous + ' 主力与大单加超大单不符，按源精度未通过。'); continue
        result['rows'].append({'date': previous, 'code': code, 'name': name,
                              **{key: str(value) for key, value in zip(AMOUNTS, (main, large, superlarge))}})
    observed = {row['date'] for row in result['rows']}
    result['missing_dates'] = [day for day in dates if day not in observed]
    return result


def _rank(rows):
    def ordered(sign):
        selected = [row for row in rows if Decimal(row['main_net_yuan']) * sign > 0]
        selected.sort(key=lambda row: (-Decimal(row['main_net_yuan']) * sign, row['code']))
        return [{'code': row['code'], 'name': row['name'], 'rank': rank,
                 **{key: float(Decimal(row[key])) for key in AMOUNTS}}
                for rank, row in enumerate(selected[:10], 1)]
    return ordered(1), ordered(-1), sum(Decimal(row['main_net_yuan']) == 0 for row in rows)


def compute_report(catalog, identities, histories, dates, *, unit_verified, now=None, sources=None, gaps=None):
    """Build daily and cumulative formal lists only when the frozen pool is complete."""
    now = _now(now)
    if not dates or dates != sorted(set(dates)) or any(not _date(day) for day in dates):
        raise ValueError('Invalid trading dates')
    ids = {row['code']: row['name'] for row in identities}
    if (len(ids) != len(identities) or any(not re.fullmatch(r'BK\d+', code) for code in ids)
            or catalog.get('count') != len(identities)):
        raise ValueError('Catalog identities are inconsistent')
    if catalog.get('complete') is True and (len(identities) < 50
            or catalog.get('group_counts', [None])[0] != len(identities)
            or catalog.get('fingerprint') != _digest(identities)):
        raise ValueError('Frozen complete catalog was shortened or altered')
    expected = len(identities)
    by_code = {}
    for result in histories:
        if result.get('code') not in ids or result.get('name') != ids[result['code']]:
            continue
        if result['code'] in by_code:
            raise ValueError('Duplicate industry evidence')
        seen_dates = []
        for row in result.get('rows', []):
            if (row.get('code') != result['code'] or row.get('name') != result['name']
                    or row.get('date') not in dates or row['date'] in seen_dates):
                raise ValueError('Parsed history identity or dates are inconsistent')
            amounts = [Decimal(str(row.get(key))) for key in AMOUNTS]
            if not all(value.is_finite() for value in amounts) or amounts[0] != amounts[1]+amounts[2]:
                raise ValueError('Parsed main-flow arithmetic is inconsistent')
            seen_dates.append(row['date'])
        if seen_dates != sorted(seen_dates):
            raise ValueError('Parsed history dates are not ordered')
        by_code[result['code']] = result
    can_rank = bool(catalog.get('complete') is True and expected and unit_verified)
    day_results = []
    full_histories = []
    for code, result in by_code.items():
        if len(result.get('rows', [])) == len(dates) and [r['date'] for r in result['rows']] == dates:
            full_histories.append(result)
    for day in dates:
        rows = [row for result in by_code.values() for row in result.get('rows', []) if row['date'] == day]
        verified = len(rows) if unit_verified else 0
        complete = can_rank and verified == expected
        incoming, outgoing, zeros = _rank(rows) if complete else ([], [], 0)
        day_results.append({'date': day, 'status': 'complete' if complete else 'partial' if verified else 'unavailable',
                            'expected_sectors': expected, 'verified_sectors': verified,
                            'inflows': incoming, 'outflows': outgoing, 'zero_count': zeros if complete else None})
    cumulative = []
    for result in full_histories:
        cumulative.append({'code': result['code'], 'name': result['name'],
                           **{key: str(sum((Decimal(row[key]) for row in result['rows']), Decimal(0))) for key in AMOUNTS}})
    all_complete = can_rank and len(cumulative) == expected
    incoming, outgoing, zeros = _rank(cumulative) if all_complete else ([], [], 0)
    errors = list(gaps or [])
    errors += [SCOPE,
               '东财模型主力净额为源生大单与超大单净额之和，不能识别机构或游资真实交易主体。',
               '本榜与同花顺普通资金净额是不同来源、分类与模型，独立显示，不混排或补缺。',
               '正负分组各最多十项，净零单列；不足不补位，净额也不等于ETF申赎。']
    if not catalog.get('complete'):
        errors.append('本轮行业目录未通过完整性核验，未形成正式全池日榜。')
    if not unit_verified:
        errors.append('未确认本轮资金金额的元单位，不发布数值排名。')
    failed_count = expected-len(full_histories)
    if failed_count:
        errors.append(f'{failed_count}个行业未取得完整{len(dates)}日核验数据；受影响日期的正式前十留空。')
    ignored = sorted({x for h in histories for x in h.get('ignored_after_cutoff', [])})
    if ignored:
        errors.append('已排除源响应中晚于请求截止日的记录，不用后续或当日未完成记录覆盖历史窗口。')
    return {'schema_version': 1, 'provider': 'eastmoney', 'metric': METRIC, 'unit': 'CNY_yuan',
            'generated_at': now.isoformat(), 'retrieved_at': max((r.get('retrieved_at') for r in histories if _stamp(r.get('retrieved_at'))), default=now.isoformat()),
            'cutoff': dates[-1], 'window_start': dates[0], 'window_end': dates[-1], 'trading_dates20': dates,
            'catalog': copy.deepcopy(catalog),
            'coverage': {'expected_sectors': expected, 'verified_sectors': len(full_histories) if unit_verified else 0,
                         'complete_days': sum(x['status'] == 'complete' for x in day_results), 'expected_days': len(dates)},
            'days': day_results,
            'summary': {'complete_window': bool(all_complete), 'latest_date': dates[-1], 'latest_status': day_results[-1]['status'],
                        'cumulative': {'status': 'complete' if all_complete else 'partial' if cumulative and unit_verified else 'unavailable',
                                       'expected_sectors': expected, 'verified_sectors': len(cumulative) if unit_verified else 0,
                                       'inflows': incoming, 'outflows': outgoing, 'zero_count': zeros if all_complete else None}},
            'gaps': list(dict.fromkeys(errors)), 'sources': sources or []}


def _http(url, timeout=6):
    """One ordinary request, with no redirects, retries or credential workarounds."""
    import requests
    started = time.monotonic()
    try:
        response = requests.get(url, timeout=timeout, allow_redirects=False,
                                headers={'User-Agent': 'Mozilla/5.0', 'Referer': CATALOG_URLS[0]})
        body = response.content
        return {'status_code': response.status_code, 'body': body[:MAX_BODY+1],
                'retrieved_at': _now().isoformat(), 'elapsed': time.monotonic()-started,
                'error': 'body_too_large' if len(body) > MAX_BODY else None}
    except requests.RequestException as exc:
        return {'status_code': None, 'body': b'', 'retrieved_at': _now().isoformat(),
                'elapsed': time.monotonic()-started, 'error': type(exc).__name__}


def _blocked(response):
    if response.get('status_code') in (401, 403, 429):
        return True
    body = response.get('body', b'').decode('utf-8', errors='replace')[:12000]
    return bool(re.search(r'captcha|access denied|verify you are human|访问验证|安全验证|验证码', body, re.I))


def _request(url, target, getter):
    result = getter(url, timeout=6)
    body = result.get('body', b'')
    if isinstance(body, str):
        body = body.encode()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.write_bytes(body)
    meta = {key: result.get(key) for key in ('status_code', 'retrieved_at', 'elapsed', 'error')}
    meta.update(source=url, sha256=_digest(body), bytes=len(body))
    atomic_json(target.with_suffix('.audit.json'), meta)
    return dict(result, body=body), meta


def _valid_http(result):
    return result.get('status_code') == 200 and not result.get('error') and len(result.get('body', b'')) <= MAX_BODY and not _blocked(result)


def collect_report(cutoff, days=20, *, force=False, now=None, getter=None, workers=5, calendar=None):
    now = _now(now)
    dates = trading_window(cutoff, days, now=now, calendar=calendar)
    if type(workers) is not int or not 4 <= workers <= 6:
        raise ValueError('History collection uses four to six bounded workers')
    current = read_json(research_path('etf_flow_history/current.json'), {})
    if (not force and isinstance(current, dict) and current.get('provider') == 'eastmoney'
            and current.get('cutoff') == dates[-1] and current.get('trading_dates20') == dates
            and current.get('summary', {}).get('complete_window') is True
            and current.get('catalog', {}).get('complete') is True):
        try:
            validate_report(current, now=now)
        except (ValueError, TypeError, KeyError, InvalidOperation):
            pass
        else:
            return current, True
    getter = getter or _http
    folder = research_path('etf_flow_history/evidence') / now.strftime('%Y%m%dT%H%M%S%f')
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_json(folder/'calendar.json', {'trading_dates': dates, 'source': (calendar or load_config('market_calendar.json')).get('source')})
    documents, sources, gaps = [], [], []
    catalog = {'count': 0, 'complete': False, 'classification': '东方财富行业', 'source': CATALOG_URLS[0],
               'as_of': now.date().isoformat(), 'scope_note': SCOPE, 'coverage_basis': '目录尚未核验',
               'source_total': None, 'group_counts': [], 'fingerprint': None}
    identities, histories, unit_ok = [], [], False
    try:
        for index, url in enumerate(CATALOG_URLS):
            response, meta = _request(url, folder/f'catalog-{index}.raw', getter)
            sources.append({'label': '东方财富完整分类选择器', 'url': url, 'as_of': now.date().isoformat(),
                            'retrieved_at': meta['retrieved_at'], 'basis': '仅行业组；目录生成时间不是资金时间或历史分类生效时间。'})
            if not _valid_http(response):
                raise ValueError('目录请求失败或受限，停止本轮，不调用历史池。')
            documents.append({'source': url, 'retrieved_at': meta['retrieved_at'], 'body': response['body'].decode('utf-8')})
        catalog, identities = parse_catalog_pair(documents, now=_now() if getter is _http else now)
        atomic_json(folder/'catalog-frozen.json', {'catalog': catalog, 'identities': identities, 'frozen_at': _now().isoformat()})
        response, meta = _request(UNIT_URL, folder/'unit-proof.raw.js', getter)
        unit_ok = _valid_http(response) and verify_unit_script(response['body'].decode('utf-8', errors='replace'))
        sources.append({'label': '东方财富原站日资金单位', 'url': UNIT_URL, 'as_of': None,
                        'retrieved_at': meta['retrieved_at'], 'basis': '日线原值直接绘图并注明单位:元；不是将万元误作元。'})
        if not unit_ok:
            raise ValueError('原站单位证据缺失或受限，未继续行业历史请求。')
    except (ValueError, UnicodeError) as exc:
        gaps.append(str(exc))
        report = compute_report(catalog, identities, [], dates, unit_verified=False, now=now, sources=sources, gaps=gaps)
        atomic_json(folder/'result.json', report)
        return report, False
    stop = threading.Event()

    def one(identity):
        params = {'lmt': '0', 'klt': '101', 'fields1': 'f1,f2,f3,f7',
                  'fields2': ','.join('f'+str(i) for i in range(51,66)), 'secid': '90.'+identity['code']}
        url = HISTORY_URL+'?'+urlencode(params)
        if stop.is_set():
            return dict(identity, rows=[], missing_dates=dates, issues=['访问受限后未再排程。'], retrieved_at=None)
        response, meta = _request(url, folder/'history'/(identity['code']+'.raw.json'), getter)
        if _blocked(response):
            stop.set()
        if not _valid_http(response):
            result = dict(identity, rows=[], missing_dates=dates, issues=['历史接口请求失败或受限。'], retrieved_at=meta['retrieved_at'])
        else:
            try:
                payload = json.loads(response['body'])
                result = parse_history(payload, identity, dates, now=_now() if getter is _http else now, retrieved_at=meta['retrieved_at'])
            except (ValueError, UnicodeError, TypeError):
                result = dict(identity, rows=[], missing_dates=dates, issues=['原响应未通过资金日线结构核验。'], retrieved_at=meta['retrieved_at'])
        atomic_json(folder/'parsed'/(identity['code']+'.json'), result)
        return result

    pool = ThreadPoolExecutor(max_workers=workers)
    pending, cursor = {}, 0
    try:
        while cursor < len(identities) and len(pending) < workers:
            pending[pool.submit(one, identities[cursor])] = identities[cursor]; cursor += 1
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for task in done:
                identity = pending.pop(task)
                try:
                    histories.append(task.result())
                except Exception:
                    histories.append(dict(identity, rows=[], missing_dates=dates, issues=['历史请求内部失败，保留缺口。'], retrieved_at=None))
            while not stop.is_set() and cursor < len(identities) and len(pending) < workers:
                pending[pool.submit(one, identities[cursor])] = identities[cursor]; cursor += 1
        if stop.is_set():
            gaps.append('接口出现拒绝、限流或访问验证，已停止后续排程；未请求行业保持缺失。')
    finally:
        pool.shutdown(wait=True)
    finished = _now() if getter is _http else now
    sources += [{'label': '东方财富行业资金历史', 'url': HISTORY_URL, 'as_of': dates[-1],
                 'retrieved_at': finished.isoformat(), 'basis': '本轮冻结行业池逐行业身份核验；每天按Decimal原精度校验主力=大单+超大单。'},
                {'label': 'AKShare原维护者字段映射', 'url': CODE_URL, 'as_of': None,
                 'retrieved_at': finished.isoformat(), 'basis': 'f51日期，f52主力，f55大单，f56超大单；未使用不明的末尾字段补造涨幅。'}]
    report = compute_report(catalog, identities, histories, dates, unit_verified=unit_ok, now=finished, sources=sources, gaps=gaps)
    atomic_json(folder/'result.json', report)
    return report, False


def validate_report(report, *, now=None):
    """Validate the publication boundary, also used for network-free cache reuse."""
    now = _now(now)
    expected = {'schema_version','provider','metric','unit','generated_at','retrieved_at','cutoff','window_start','window_end',
                'trading_dates20','catalog','coverage','days','summary','gaps','sources'}
    if (not isinstance(report, dict) or set(report) != expected or report['schema_version'] != 1
            or report['provider'] != 'eastmoney' or report['metric'] != METRIC or report['unit'] != 'CNY_yuan'
            or not _date(report['cutoff']) or not _stamp(report['generated_at'])
            or _stamp(report['generated_at']) > now or not _stamp(report['retrieved_at'])
            or _stamp(report['retrieved_at']) > now):
        raise ValueError('Invalid independent industry history report')
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False)
    if PRIVATE.search(encoded) or any(k in encoded for k in ('"payload"', '"raw_path"', '"account"', '"portfolio"')):
        raise ValueError('Private raw fields cannot be published')
    dates = report['trading_dates20']
    if (not isinstance(dates, list) or not 1 <= len(dates) <= 120 or dates != sorted(set(dates))
            or any(not _date(day) for day in dates) or dates[-1] != report['cutoff']
            or report['window_start'] != dates[0] or report['window_end'] != dates[-1]
            or _date(dates[-1]) > now.date()
            or (_date(dates[-1]) == now.date() and now.time().replace(tzinfo=None) < dt.time(15))):
        raise ValueError('Invalid completed historical window')
    catalog, coverage = report['catalog'], report['coverage']
    expected = catalog['count']
    if (type(expected) is not int or not 0 <= expected <= 1000
            or catalog.get('classification') != '东方财富行业'
            or catalog.get('source') not in CATALOG_URLS or not _date(catalog.get('as_of'))
            or _date(catalog['as_of']) > now.date()):
        raise ValueError('Invalid frozen industry catalog')
    if catalog.get('complete') is True and (expected < 50 or catalog.get('group_counts', [None])[0] != expected
            or not re.fullmatch(r'[0-9a-f]{64}', catalog.get('fingerprint') or '')):
        raise ValueError('Complete catalog lacks its matching frozen scope')
    if coverage['expected_sectors'] != expected or coverage['expected_days'] != len(dates):
        raise ValueError('Coverage scope mismatch')
    def check_list(group, sign):
        if not isinstance(group, list) or len(group) > 10:
            raise ValueError('Invalid top-ten list')
        seen, previous = set(), None
        for rank, row in enumerate(group, 1):
            if (set(row) != {'code', 'name', 'rank', *AMOUNTS} or row['rank'] != rank
                    or not re.fullmatch(r'BK\d+', row['code']) or row['code'] in seen
                    or not isinstance(row['name'], str) or _market_name(row['name']) != row['name']):
                raise ValueError('Invalid ranked industry identity')
            values = [Decimal(str(row[key])) for key in AMOUNTS]
            if not all(v.is_finite() for v in values) or values[0] != values[1]+values[2] or values[0]*sign <= 0:
                raise ValueError('Invalid ranked main-flow values')
            order = (-values[0]*sign, row['code'])
            if previous is not None and order < previous:
                raise ValueError('Rank is inconsistent with source amounts')
            seen.add(row['code']); previous = order
        return seen
    def check_day(day):
        verified = day['verified_sectors']
        if type(verified) is not int or not 0 <= verified <= expected or day['expected_sectors'] != expected:
            raise ValueError('Invalid daily coverage')
        status = day['status']
        if status not in ('complete','partial','unavailable'):
            raise ValueError('Invalid daily status')
        if status == 'complete' and not (expected and verified == expected and catalog['complete'] is True):
            raise ValueError('Incomplete pool cannot publish formal top-ten lists')
        if status != 'complete' and (day['inflows'] or day['outflows']):
            raise ValueError('Incomplete days cannot publish formal top-ten lists')
        incoming = check_list(day['inflows'], 1)
        if incoming & check_list(day['outflows'], -1):
            raise ValueError('One industry cannot occur on both sides')
    if not isinstance(report['days'], list) or [d['date'] for d in report['days']] != dates:
        raise ValueError('Daily coverage must cover the declared dates exactly')
    for day in report['days']:
        check_day(day)
    complete_days = sum(d['status'] == 'complete' for d in report['days'])
    if coverage['complete_days'] != complete_days:
        raise ValueError('Complete-day count is inconsistent')
    summary = report['summary']; cumulative = summary['cumulative']
    check_day(cumulative)
    if (summary['latest_date'] != dates[-1] or summary['latest_status'] != report['days'][-1]['status']
            or coverage['verified_sectors'] != cumulative['verified_sectors']
            or summary['complete_window'] != (complete_days == len(dates))
            or (cumulative['status'] == 'complete') != summary['complete_window']):
        raise ValueError('Historical summary is inconsistent')
    return report


def publish_report(report):
    """Publish only to the independent history module; archive before pointer swap."""
    validate_report(report)
    stored = copy.deepcopy(report)
    folder = research_path('etf_flow_history')
    digest = _digest({k:v for k,v in stored.items() if k not in ('generated_at','retrieved_at')})
    with update_lock():
        previous = read_json(folder/'current.json', {})
        if isinstance(previous, dict) and previous.get('cutoff') and previous['cutoff'] > stored['cutoff']:
            raise ValueError('Refusing to replace a newer historical window')
        atomic_json(folder/'archive'/stored['cutoff']/(digest+'.json'), stored)
        atomic_json(folder/'current.json', stored)
    return stored


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    command = sub.add_parser('collect')
    command.add_argument('--cutoff', required=True)
    command.add_argument('--days', type=int, default=20)
    command.add_argument('--force', action='store_true')
    command.add_argument('--workers', type=int, default=5)
    command.add_argument('--build', action='store_true')
    args = parser.parse_args(argv)
    try:
        report, cached = collect_report(args.cutoff, args.days, force=args.force, workers=args.workers)
        if not cached:
            report = publish_report(report)
        if args.build:
            from build_investment_site import build
            build()
    except (ValueError, OSError):
        parser.exit(2, 'Industry history collection could not verify its calendar, sources or snapshot.\n')
    print(json.dumps({'cutoff': report['cutoff'], 'cached': cached, 'catalog_count': report['catalog']['count'],
                      'coverage': report['coverage'], 'complete_window': report['summary']['complete_window']}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
