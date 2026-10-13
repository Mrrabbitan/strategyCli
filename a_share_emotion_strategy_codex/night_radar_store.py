"""Private dated editions, incremental disclosure windows and delivery receipts."""
from __future__ import annotations

import datetime as dt
import json

from research_store import research_path, atomic_json, read_json, update_lock
from night_radar import TZ, SECTIONS, DISCLOSURES, digest, stamp


def folder():
    return research_path('night_radar')


def stable(report):
    def clean(value):
        if isinstance(value, dict):
            return {k:clean(v) for k,v in value.items() if k not in
                    ('executed_at','run_started_at','retrieved_at','input_fingerprint','pending_seen','seen_before')}
        if isinstance(value,list): return [clean(v) for v in value]
        return value
    return digest(clean(report))


def window_context(cutoff):
    cutoff = stamp(cutoff)
    existing = read_json(folder()/'days'/(cutoff.date().isoformat()+'.json'), {})
    if existing.get('mode') == 'daily':
        return stamp(existing['window_start']), existing.get('seen_before',{})
    state = read_json(folder()/'state.json', {})
    # Historical audit never consumes present-day disclosure watermarks.
    last = stamp(state.get('latest_cutoff'))
    if last is None or last >= cutoff:
        return cutoff-dt.timedelta(days=1), {}
    watermarks = state.get('watermarks', {})
    windows = [stamp(watermarks.get(k)) or stamp(state.get('unfinished_since',{}).get(k))
               or cutoff-dt.timedelta(days=1) for k in DISCLOSURES]
    return min(windows), state.get('seen', {})


def load_reports():
    reports = []
    for path in (folder()/'days').glob('*.json'):
        report = read_json(path, {})
        if report.get('module_id') == 'night-radar' and report.get('schema_version') == 1:
            reports.append(report)
    return sorted(reports, key=lambda r: (r.get('report_date',''),r.get('cutoff','')), reverse=True)


def publish_report(report):
    if report.get('module_id') != 'night-radar' or report.get('schema_version') != 1:
        raise ValueError('Invalid night radar report')
    cutoff, actual = stamp(report.get('cutoff')), stamp(report.get('executed_at'))
    if cutoff is None or actual is None or cutoff > actual or report.get('report_date') != cutoff.date().isoformat():
        raise ValueError('Invalid report time')
    if set(report.get('sections', {})) != set(SECTIONS): raise ValueError('Missing report sections')
    report = json.loads(json.dumps(report, ensure_ascii=False, allow_nan=False))
    version = stable(report)
    with update_lock():
        path = folder()/'days'/(report['report_date']+'.json')
        old = read_json(path,{})
        # Replaying an earlier cutoff is an audit, never a replacement for a later edition.
        superseded = stamp(old.get('cutoff')) and stamp(old['cutoff']) > cutoff
        atomic_json(folder()/'history'/(version+'.json'), report)
        if superseded: return {'module':'night-radar','status':'superseded','changed':False,'notify':False}
        changed = stable(old) != version
        if changed: atomic_json(path, report)
        previous_attempt = read_json(folder()/'latest_attempt.json',{})
        if not stamp(previous_attempt.get('cutoff')) or stamp(previous_attempt['cutoff']) <= cutoff:
            atomic_json(folder()/'latest_attempt.json',report)
        current = read_json(folder()/'current.json',{})
        is_latest = not stamp(current.get('cutoff')) or stamp(current['cutoff']) <= cutoff
        if is_latest and report['status'] != 'unavailable': atomic_json(folder()/'current.json',report)
        if report['status'] == 'complete':
            previous = read_json(folder()/'last_success.json', {})
            if not stamp(previous.get('cutoff')) or stamp(previous['cutoff']) <= cutoff:
                atomic_json(folder()/'last_success.json',report)
        state = read_json(folder()/'state.json',{})
        last = stamp(state.get('latest_cutoff'))
        if report['mode'] == 'daily' and (last is None or cutoff >= last):
            for key in DISCLOSURES:
                state.setdefault('seen',{}).setdefault(key,{}).update(report.get('pending_seen',{}).get(key,{}))
                if report['sections'][key].get('collection_complete') is True:
                    state.setdefault('watermarks',{})[key] = cutoff.isoformat()
                    state.setdefault('unfinished_since',{}).pop(key,None)
                else:
                    state.setdefault('unfinished_since',{}).setdefault(key,report['window_start'])
            state['latest_cutoff'] = cutoff.isoformat()
            atomic_json(folder()/'state.json',state)
        delivery = _delivery_decision(report, version)
    return {'module':'night-radar','status':report['status'],'changed':changed,
            'notify':delivery['notify'],'delivery_reason':delivery['reason'],'edition':version}


def _delivery_decision(report, version):
    state = read_json(folder()/'delivery.json',{})
    failures = {k:s['status'] for k,s in report['sections'].items() if s['status'] in ('partial','unavailable')}
    fault_hash = digest(failures)
    new_disclosures = any(s.get('rows') for k,s in report['sections'].items() if k in DISCLOSURES)
    if version in state.get('sent',{}): return {'notify':False,'reason':'相同版本已发送'}
    if report['mode'] in ('trial','historical'): return {'notify':False,'reason':'按需试运行或事后核对'}
    if any(receipt.get('date') == report['report_date'] for receipt in state.get('sent',{}).values()):
        if state.get('fault_hash') != fault_hash:
            return {'notify':True,'reason':'同日新故障或恢复'}
        return {'notify':False,'reason':'本交易日晚报已发送；更新保留页面'}
    if report['trading_day']: return {'notify':True,'reason':'交易日晚报'}
    if new_disclosures: return {'notify':True,'reason':'休市新增披露'}
    previous = state.get('fault_hash',digest({}))
    if previous != fault_hash: return {'notify':True,'reason':'新故障或恢复'}
    return {'notify':False,'reason':'休市无新增'}


def delivery_decision(report):
    return _delivery_decision(report,stable(report))


def mark_sent(report_date, edition):
    """Called by the automation only for the edition it is about to emit in chat."""
    with update_lock():
        report = read_json(folder()/'days'/(dt.date.fromisoformat(report_date).isoformat()+'.json'),{})
        if stable(report) != edition: raise ValueError('Edition changed before delivery')
        state = read_json(folder()/'delivery.json',{})
        state.setdefault('sent',{})[edition] = {'date':report_date,'recorded_at':dt.datetime.now(TZ).isoformat()}
        state['fault_hash'] = digest({k:s['status'] for k,s in report['sections'].items()
                                     if s['status'] in ('partial','unavailable')})
        atomic_json(folder()/'delivery.json',state)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date',type=dt.date.fromisoformat)
    parser.add_argument('--mark-sent',help='Exact published edition fingerprint')
    parser.add_argument('--markdown',action='store_true')
    args = parser.parse_args()
    reports = load_reports()
    report = next((r for r in reports if not args.date or r['report_date']==str(args.date)),None)
    if report is None: raise SystemExit('No report for requested date')
    if args.mark_sent: mark_sent(report['report_date'],args.mark_sent)
    if args.markdown:
        from night_radar_views import render_markdown
        print(render_markdown(report))
    else: print(json.dumps(dict(delivery_decision(report),edition=stable(report),date=report['report_date']),ensure_ascii=False))
