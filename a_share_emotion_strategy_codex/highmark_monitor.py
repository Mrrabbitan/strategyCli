"""Dated, twice-daily main-board high-flyer deviation information research.

No strategy selection, broker connection, trading instruction, or daemon is used.
All prices, candidate identities, evidence, and delivery receipts remain private.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import math
from zoneinfo import ZoneInfo

from market_calendar import is_trading_day, previous_trading_day
from research_store import atomic_json, read_json, research_path, update_lock

TZ = ZoneInfo('Asia/Shanghai')
MODULE = 'highmark-abnormal'
RETENTION_SESSIONS = 30
PHASES = {'preopen': (9, 0), 'close': (15, 40)}


def folder():
    return research_path('highmark')


def stamp(value):
    if not value:
        return None
    try:
        result = dt.datetime.fromisoformat(value)
        return result.astimezone(TZ) if result.tzinfo else None
    except (TypeError, ValueError):
        return None


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def version(report):
    # Actual evidence times and values are versioned; the page build outcome is not.
    return digest({k: v for k, v in report.items() if k not in ('edition', 'build_status')})


def trading_sessions(end, count):
    result, cursor = [], end
    for _ in range(count * 4 + 40):
        ok, reason = is_trading_day(cursor)
        if '缺少' in reason or '不可用' in reason:
            raise ValueError(reason)
        if ok:
            result.append(cursor.isoformat())
            if len(result) == count:
                return list(reversed(result))
        cursor -= dt.timedelta(days=1)
    raise ValueError('交易日历无法生成完整回溯窗口')


def next_session(day):
    cursor = dt.date.fromisoformat(day)
    for _ in range(25):
        cursor += dt.timedelta(days=1)
        ok, reason = is_trading_day(cursor)
        if '缺少' in reason or '不可用' in reason:
            raise ValueError(reason)
        if ok:
            return cursor.isoformat()
    raise ValueError('无法核对下一交易日')


@contextlib.contextmanager
def collection_lock():
    path = folder() / 'collection.lock'
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            try:
                yield True
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def slot_path(date, phase):
    dt.date.fromisoformat(date)
    if phase not in PHASES:
        raise ValueError('Invalid phase')
    return folder() / 'days' / (date + '-' + phase + '.json')


def retained_items(trade_date):
    """Keep recently observed leaders; never treat old pool membership as today's fact."""
    allowed = set(trading_sessions(dt.date.fromisoformat(trade_date), RETENTION_SESSIONS))
    retained = {}
    # Previously saved, dated information reports are legitimate historical
    # membership evidence, not a scan of a paused strategy or a fabricated backfill.
    for path in sorted(research_path('night_radar/days').glob('*.json')):
        report = read_json(path, {})
        section = report.get('sections', {}).get('limit_up', {})
        if section.get('status') != 'complete':
            continue
        for row in section.get('rows', []):
            day = row.get('source_date')
            if day not in allowed or not row.get('source_url') or int(row.get('boards') or 0) < 2:
                continue
            code = row.get('code')
            old = retained.get(code, {})
            if code and day >= old.get('last_in_pool', ''):
                retained[code] = {'code': code, 'name': row.get('name', ''),
                                  'last_in_pool': day, 'boards': row.get('boards', 0),
                                  'source': row['source_url']}
    for path in sorted((folder() / 'days').glob('*.json')):
        report = read_json(path, {})
        if report.get('trade_date', '') > trade_date:
            continue
        for row in report.get('rows', []):
            seen = row.get('last_in_pool')
            if seen not in allowed:
                continue
            code = row.get('code')
            if not code:
                continue
            old = retained.get(code, {})
            if seen >= old.get('last_in_pool', ''):
                retained[code] = {'code': code, 'name': row.get('name', ''),
                                  'last_in_pool': seen, 'boards': row.get('boards', 0),
                                  'source': row.get('pool_source') or row.get('source')}
    return sorted(retained.values(), key=lambda r: r['code'])


def _metric(metric):
    result = dict(metric)
    result['value'] = metric.get('deviation_pp', metric.get('max_positive_deviation'))
    result['start'] = metric.get('max_start')
    result['end'] = metric.get('max_end')
    result['days'] = metric.get('max_length', metric.get('length'))
    result['next_day_flat_index_pct'] = (metric.get('next_session_scenario') or {}).get('day_pct')
    return result


def adjustment_coverage(bars, dates):
    """Require a reference or adjustment diagnostic for each native-close pair."""
    def finite(value):
        try:
            return not isinstance(value, bool) and math.isfinite(float(value)) and float(value) > 0
        except (TypeError, ValueError, OverflowError):
            return False
    indexed = {bar.get('date'): bar for bar in bars}
    for before, current in zip(dates, dates[1:]):
        prior, row = indexed.get(before, {}), indexed.get(current, {})
        reference = row.get('prev_close', row.get('pre_close', row.get('preclose')))
        if finite(reference):
            continue
        if any(all(finite(bar.get(key, bar.get(alias))) for bar in (prior, row))
               for key, alias in (('adjust_factor', 'adj_factor'), ('qfq_close', 'adj_close'))):
            continue
        return False
    return bool(dates)


def build_report(evidence, *, now, phase, trade_date, resets=None):
    from highmark_rules import calculate, SOURCES
    resets = resets or {}
    pool = evidence.get('pool') or {}
    today_items = {row['code']: row for row in pool.get('items', [])}
    retained = {row['code']: row for row in evidence.get('retained', [])}
    identities = {**retained, **today_items}
    stocks = evidence.get('stocks') or {}
    benchmarks = evidence.get('benchmarks') or {}
    rows = []
    missing = list(evidence.get('missing', []))
    expected_dates = trading_sessions(dt.date.fromisoformat(trade_date), 31)
    events = evidence.get('events') or {}
    provider_aligned = (events.get('provider_complete') is True and events.get('status') == 'complete'
                        and events.get('coverage_start', '9999') <= expected_dates[0]
                        and events.get('coverage_end') == trade_date)
    for code, item in sorted(identities.items()):
        data = stocks.get(code) or {}
        bars = [dict(bar) for bar in data.get('history') or []]
        adjusted = {bar.get('date'): bar.get('close') for bar in data.get('adjusted_history') or []}
        for bar in bars:
            if bar.get('date') in adjusted:
                bar['qfq_close'] = adjusted[bar['date']]
            if bar.get('volume_hands') == 0:
                bar['suspended'] = True
        benchmark = 'sh000002' if code.startswith('6') else 'sz399107'
        index = benchmarks.get(benchmark) or {}
        index_bars = index.get('history', []) if isinstance(index, dict) else index
        note = list(data.get('missing', []))
        reset = resets.get(code) or {}
        valid_reset = bool(reset.get('verified') is True and reset.get('source_url', '').startswith('https://')
                           and stamp(reset.get('verified_at')) and stamp(reset['verified_at']) <= now
                           and reset.get('exhaustive_review') is True
                           and reset.get('coverage_start', '9999') <= expected_dates[0]
                           and reset.get('coverage_end') == trade_date
                           and reset.get('exchange_published_date')
                           and reset['exchange_published_date'] <= trade_date)
        normal = reset.get('normal_reset') if valid_reset else None
        severe = reset.get('severe_reset') if valid_reset else None
        pending = []
        if provider_aligned and not valid_reset:
            for kind in ('normal', 'severe'):
                published = sorted({ev['published_date'] for ev in events.get(kind, {}).get(code, [])
                                    if ev.get('published_date') and ev['published_date'] <= trade_date})
                prior = [day for day in published if day < trade_date]
                start = next_session(prior[-1]) if prior else None
                if kind == 'normal':
                    normal = start
                else:
                    severe = start
                if trade_date in published:
                    pending.append(kind)
        # Feed eligibility excludes known ST, suspended or unverified close identities.
        usable = data.get('status') == 'complete' and data.get('eligible', True)
        available = {bar.get('date') for bar in bars}
        if not set(expected_dates).issubset(available):
            note.append('最近31个交易日日线不完整，不跨停牌或缺口补值')
            usable = False
        if not adjustment_coverage(bars, expected_dates):
            note.append('31日除权参考或同源复权关系不完整，偏离与次日情景暂不发布')
            usable = False
        if data.get('quote_verified') is False:
            usable = False
            note.append('收盘交叉核验未通过')
        index_dates = {bar.get('date') for bar in index_bars}
        if isinstance(index, dict) and index.get('status', 'complete') != 'complete':
            usable = False
            note.extend(index.get('missing') or ['分类指数独立收盘核验未完成'])
        if not set(expected_dates).issubset(index_dates):
            usable = False
            note.append('分类指数交易日不完整')
        index_quote = index.get('quote') or {} if isinstance(index, dict) else {}
        if index_bars and index_quote and abs(index_bars[-1]['close'] - index_quote.get('price', 0)) > .0005:
            usable = False
            note.append('分类指数末收盘与报价冲突')
        try:
            computed = calculate(bars, index_bars, trade_date, normal_reset=normal,
                                 severe_reset=severe, reset_verified=valid_reset,
                                 normal_reset_verified=valid_reset or provider_aligned,
                                 severe_reset_verified=valid_reset or provider_aligned) if usable else {}
        except (ValueError, TypeError, KeyError) as exc:
            computed = {}
            note.append(str(exc))
        # Calculation module may return top-level diagnostics alongside metrics.
        raw_metrics = computed.get('metrics', computed)
        metrics = {n: _metric(raw_metrics[n]) for n in ('3', '10', '30')
                   if n in raw_metrics and isinstance(raw_metrics[n], dict)}
        for key, metric in metrics.items():
            kind = 'normal' if key == '3' else 'severe'
            if kind in pending:
                # Today's disclosure resets the NEXT session, not the current
                # closing observation. A fresh one-session interval under a flat
                # index reaches the threshold only at the threshold itself.
                metric['pending_reset'] = next_session(trade_date)
                if isinstance(metric.get('value'), (int, float)):
                    metric['next_day_flat_index_pct'] = metric['threshold']
                    metric['next_session_scenario'] = {'day_pct': metric['threshold'],
                        'index_day_pct': 0.0, 'length': 1, 'start': 'next_session',
                        'end': 'next_session', 'reset_basis': 'provider_published_event',
                        'is_forecast': False, 'is_price_limit': False}
        if not all(isinstance(metrics.get(n, {}).get('value'), (int, float)) for n in ('3', '10', '30')):
            note.append('异动数值未完整取得')
        quote = data.get('quote') or (evidence.get('quotes') or {}).get(code) or {}
        is_new = code in today_items
        rows.append({'code': code, 'name': quote.get('name') or item.get('name', ''),
                     'boards': item.get('boards', item.get('board_count', 0)) if is_new else 0,
                     'origin': '当日两板及以上' if is_new else '近期高标继续跟踪',
                     'last_in_pool': trade_date if is_new else item.get('last_in_pool'),
                     'pool_source': item.get('source') or pool.get('source'),
                     'benchmark': benchmark, 'metrics': metrics, 'reference_only': True,
                     'quote_time': quote.get('source_time') or quote.get('timestamp'),
                     'price': quote.get('price'), 'missing': list(dict.fromkeys(note)),
                     'reset_note': ('已导入核对来源，仍为研究测算' if valid_reset else
                                    '按公开转载记录对齐重置，交易所完整性未核验' if provider_aligned else
                                    '公告重置未全量核验'),
                     'reset_alignment': 'verified_import' if valid_reset else 'provider_records' if provider_aligned else 'unknown',
                     'pending_reset_kinds': pending,
                     'security_status_verified': data.get('security_status_verified') is True,
                     'rule_diagnostics': {k: v for k, v in computed.items() if k != 'metrics'}
                     if 'metrics' in computed else {},
                     'excluded': usable is False})
    # Monitoring order is regulatory proximity, not a stock recommendation rank.
    def proximity(row):
        distances = [m['headroom_pp'] / m['threshold'] for m in row['metrics'].values()
                     if isinstance(m.get('headroom_pp'), (int, float)) and m.get('threshold')]
        return (min(distances) if distances else math.inf, -int(row['boards'] or 0), row['code'])
    rows.sort(key=proximity)
    valid_count = sum(all(isinstance(r['metrics'].get(n, {}).get('value'), (int, float))
                          for n in ('3', '10', '30')) for r in rows)
    pool_complete = pool.get('status') == 'complete'
    if not pool_complete:
        missing.append('当日高标池未完整核验；保留股不代表当日完整名单')
    # No unreviewed reset evidence is silently promoted to an official event count.
    missing.append('交易所普通/严重异动公布及重置未全量核验；10日4次同向异动次数暂不生成')
    report = {'module': MODULE, 'schema_version': 1, 'date': now.date().isoformat(),
              'phase': phase, 'trade_date': trade_date,
              'as_of': trade_date + 'T15:00:00+08:00', 'generated_at': now.isoformat(),
              'planned_at': now.replace(hour=PHASES[phase][0], minute=PHASES[phase][1],
                                        second=0, microsecond=0).isoformat(),
              'status': 'partial' if rows else ('partial' if pool_complete else 'unavailable'),
              'coverage': {'new_pool_count': len(today_items), 'retained_count': len(retained),
                           'scanned_count': valid_count, 'total_count': len(rows),
                           'pool_complete': pool_complete, 'universe_complete': False,
                           'kind': 'daily_2plus_and_30session_retained'},
              'rows': rows, 'missing': list(dict.fromkeys(missing)),
              'reset_alignment': {'provider_records_complete': provider_aligned, 'official_complete': False,
                                  'source_rows': events.get('raw_row_count'),
                                  'coverage_start': events.get('coverage_start'), 'coverage_end': events.get('coverage_end')},
              'sources': SOURCES[:2] + evidence.get('sources', []) + events.get('sources', []),
              'input_fingerprint': digest(evidence),
              'build_status': 'pending'}
    return report


def publish(report):
    report = json.loads(json.dumps(report, ensure_ascii=False, allow_nan=False))
    generated, cutoff = stamp(report.get('generated_at')), stamp(report.get('as_of'))
    if (not generated or (not cutoff and report.get('status') != 'unavailable')
            or (cutoff and cutoff > generated) or report.get('module') != MODULE):
        raise ValueError('Invalid report time or module')
    path = slot_path(report['date'], report['phase'])
    report['edition'] = version(report)
    with update_lock():
        old = read_json(path, {})
        if stamp(old.get('generated_at')) and stamp(old['generated_at']) > generated:
            return old
        atomic_json(folder() / 'history' / (report['edition'] + '.json'), report)
        atomic_json(path, report)
        current = read_json(folder() / 'current.json', {})
        if not stamp(current.get('generated_at')) or stamp(current['generated_at']) <= generated:
            atomic_json(folder() / 'current.json', report)
            atomic_json(folder() / 'latest_attempt.json', report)
        if report.get('coverage', {}).get('pool_complete') and report.get('coverage', {}).get('scanned_count') == len(report['rows']):
            atomic_json(folder() / 'last_complete_prices.json', report)
    return report


def fault_signature(report):
    coverage = report.get('coverage') or {}
    # Do not change the failure signature for clocks, error object IDs, or candidate names.
    return digest({'pool': coverage.get('pool_complete', False),
                   'prices': coverage.get('scanned_count', 0) == coverage.get('total_count', 0),
                   'events': report.get('reset_alignment', {}).get('provider_records_complete', False),
                   'page_failed': report.get('build_status') == 'failed',
                   'available': report.get('status') != 'unavailable'})


def decision(report=None):
    if report is None:
        trigger = read_json(folder() / 'last_run.json', {})
        if trigger.get('status') == 'skipped':
            return trigger
        report = read_json(folder() / 'current.json', {})
    if not report:
        return {'notify': False, 'reason': '尚无研究结果'}
    state = read_json(folder() / 'delivery.json', {})
    slot = report['date'] + '-' + report['phase']
    receipt = state.get('slots', {}).get(slot)
    has_values = report.get('coverage', {}).get('scanned_count', 0) > 0
    valid_empty = (report.get('coverage', {}).get('pool_complete') is True
                   and report.get('coverage', {}).get('total_count') == 0)
    signature = fault_signature(report)
    if report['edition'] in state.get('sent', {}) and state['sent'][report['edition']].get('fault') == signature:
        notify, reason = False, '相同版本已发送'
    elif receipt and receipt.get('fault') == signature:
        notify, reason = False, '本时段已发送，页面保留更新'
    elif not has_values and not valid_empty and state.get('fault') == signature:
        notify, reason = False, '同一持续故障'
    else:
        notify, reason = True, '定时高标风险表' if has_values else '已核验范围为空' if valid_empty else '首次取数故障'
    return {'notify': notify, 'reason': reason, 'edition': report['edition'],
            'date': report['date'], 'phase': report['phase'], 'build_status': report.get('build_status')}


def mark_sent(edition):
    with update_lock():
        report = read_json(folder() / 'current.json', {})
        if not report or report.get('edition') != edition or version(report) != edition:
            raise ValueError('版本已被并发更新，请重新准备回复')
        state = read_json(folder() / 'delivery.json', {})
        signature = fault_signature(report)
        slot = report['date'] + '-' + report['phase']
        receipt = {'date': report['date'], 'phase': report['phase'], 'fault': signature,
                   'recorded_at': dt.datetime.now(TZ).isoformat()}
        state.setdefault('sent', {})[edition] = receipt
        state.setdefault('slots', {})[slot] = receipt
        state['fault'] = signature
        atomic_json(folder() / 'delivery.json', state)
    return decision(report)


def run(*, phase, scheduled=False, now=None, collector=None, build_page=True):
    now = (now or dt.datetime.now(TZ)).astimezone(TZ)
    def skipped(reason):
        result = {'notify': False, 'status': 'skipped', 'reason': reason,
                  'date': now.date().isoformat(), 'phase': phase, 'executed_at': now.isoformat()}
        with update_lock():
            atomic_json(folder() / 'last_run.json', result)
        return result
    ok, reason = is_trading_day(now.date())
    if not ok:
        if '缺少' not in reason and '不可用' not in reason:
            return skipped(reason)
        report = {'module': MODULE, 'schema_version': 1, 'date': now.date().isoformat(),
                  'phase': phase, 'trade_date': None, 'as_of': None,
                  'generated_at': now.isoformat(), 'status': 'unavailable', 'rows': [],
                  'coverage': {'pool_complete': False, 'scanned_count': 0, 'total_count': 0},
                  'missing': [reason], 'sources': [], 'build_status': 'pending'}
        return finish(publish(report), build_page=build_page)
    clock = now.time().replace(tzinfo=None)
    if phase == 'preopen' and clock >= dt.time(9, 25):
        return skipped('已过盘前窗口，不补造盘前结果')
    if phase == 'close' and clock < dt.time(15, 0):
        return skipped('尚未形成完整收盘行情')
    old = read_json(slot_path(now.date().isoformat(), phase), {})
    if (scheduled and old and not decision(old)['notify'] and old.get('coverage', {}).get('pool_complete')
            and old.get('coverage', {}).get('scanned_count') == old.get('coverage', {}).get('total_count')):
        return skipped('本时段完整行情已发送，不重复采集')
    trade_date = (previous_trading_day(now.date()) if phase == 'preopen' else now.date()).isoformat()
    with collection_lock() as acquired:
        if not acquired:
            return skipped('已有同模块采集正在执行')
        retained = retained_items(trade_date)
        raw_dir = folder() / 'raw' / (now.strftime('%Y%m%dT%H%M%S%f') + '-' + phase)
        if collector is None:
            from highmark_feeds import collect_evidence
            collector = collect_evidence
        try:
            evidence = collector(trade_date, raw_dir=raw_dir, retained=retained)
        except Exception as exc:
            evidence = {'pool': {'status': 'unavailable', 'items': []}, 'stocks': {},
                        'benchmarks': {}, 'retained': retained,
                        'missing': ['采集未完成：' + type(exc).__name__], 'sources': []}
        if collector.__module__ == 'highmark_feeds':
            from highmark_events import collect_events
            start = trading_sessions(dt.date.fromisoformat(trade_date), 31)[0]
            try:
                evidence['events'] = collect_events(start, trade_date, raw_dir / 'events')
            except Exception as exc:
                evidence['events'] = {'status': 'unavailable', 'provider_complete': False,
                                      'missing': [type(exc).__name__]}
        evidence.setdefault('retained', retained)
        atomic_json(raw_dir / 'evidence.json', evidence)
        report = build_report(evidence, now=dt.datetime.now(TZ) if collector.__module__ == 'highmark_feeds' else now,
                              phase=phase, trade_date=trade_date,
                              resets=read_json(folder() / 'verified_resets.json', {}))
        report = publish(report)
    return finish(report, build_page=build_page)


def finish(report, *, build_page):
    build_status, build_error = 'not_requested', None
    if build_page:
        try:
            from build_investment_site import build
            build()
            build_status = 'success'
        except Exception as exc:
            build_status, build_error = 'failed', type(exc).__name__
    with update_lock():
        current = read_json(folder() / 'current.json', {})
        if current.get('edition') == report['edition']:
            report['build_status'] = build_status
            # The build error class is operational state, not market evidence.
            if build_error:
                atomic_json(folder() / 'page_failure.json', {'edition': report['edition'], 'error_type': build_error})
            atomic_json(folder() / 'current.json', report)
            atomic_json(folder() / 'latest_attempt.json', report)
            atomic_json(slot_path(report['date'], report['phase']), report)
        atomic_json(folder() / 'last_run.json', {'status': 'published', 'edition': report['edition'],
                                               'executed_at': report['generated_at']})
    return dict(decision(report), status=report['status'], coverage=report['coverage'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--phase', choices=tuple(PHASES), default='close')
    parser.add_argument('--scheduled', action='store_true')
    parser.add_argument('--decision', action='store_true')
    parser.add_argument('--markdown', action='store_true')
    parser.add_argument('--mark-sent')
    args = parser.parse_args()
    if args.run:
        output = run(phase=args.phase, scheduled=args.scheduled)
    elif args.mark_sent:
        output = mark_sent(args.mark_sent)
    elif args.markdown:
        from highmark_views import render_markdown
        print(render_markdown(read_json(folder() / 'current.json', {})))
        return
    else:
        output = decision()
    print(json.dumps(output, ensure_ascii=False))


if __name__ == '__main__':
    main()
