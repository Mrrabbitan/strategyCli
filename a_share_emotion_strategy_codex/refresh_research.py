"""Run research from a local task; the web server deliberately cannot call this."""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
from pathlib import Path

from market_calendar import is_trading_day
from research_modules import TZ, FOLDERS, stamp, load_module, publish, record_failure
from build_investment_site import build
from research_store import private_path


def choose_phase(now):
    valid, reason = is_trading_day(now.date())
    if not valid:
        return 'prepare'
    if now.hour >= 15:
        return 'close'
    return 'prepare' if now.time().replace(tzinfo=None) < dt.time(9, 30) else 'intraday'


def _read_input(path, *, frames=False):
    allowed = (private_path().resolve(), (Path.home() / 'Library/Application Support/Yichujifa').resolve())
    resolved = path.resolve()
    if not any(resolved == root or root in resolved.parents for root in allowed):
        raise ValueError('Input must be in an approved private research directory')
    if path.is_symlink() or path.suffix != '.json' or path.stat().st_size > 100_000_000:
        raise ValueError('Input must be a regular bounded JSON research file')
    data = json.loads(path.read_text(encoding='utf-8'))
    # The native live sampler writes a JSON array; reports and reviews remain
    # objects. Frame timestamps and eligibility are checked by the adapter.
    if not isinstance(data, list if frames else dict):
        raise ValueError('Input must be a frame array' if frames else 'Input must be a research object')
    return data


def run_module(module, now, phase, *, input_data=None, hot=None):
    if isinstance(input_data, dict) and 'module_id' in input_data and input_data['module_id'] != module:
        raise ValueError('Input belongs to a different research module')
    if isinstance(input_data, dict) and input_data.get('module_id') == module:
        # Native adapters can also export a normalized, versioned private snapshot.
        if input_data.get('schema_version') != 1:
            raise ValueError('Unsupported normalized research version')
        if stamp(input_data.get('as_of')) and stamp(input_data['as_of']) > now:
            raise ValueError('Input contains research after the requested cutoff')
        return input_data
    if module == 'hot':
        from refresh_hot_sector_board import research_hot
        return research_hot(now, phase=phase, persist=False)
    if module == 'dragon':
        from dragon_research import research
        return research(now, phase=phase, hot=hot, previous=load_module(module, now)['data'])
    if module == 'yichujifa':
        from yichujifa_research import research
        live_data = ({'live':input_data.get('live'), 'snapshots':input_data.get('snapshots')}
                     if isinstance(input_data,dict) and input_data.get('live') is not None else None)
        return research(now, phase=phase, input_data=input_data, live_data=live_data)
    if module == 'prelaunch':
        from prelaunch_research import research
        enrichment = input_data.get('enrichment') if isinstance(input_data,dict) else None
        payload = input_data.get('prelaunch_input',input_data) if isinstance(input_data,dict) else input_data
        return research(now, phase=phase, input_data=payload, enrichment=enrichment,
                        previous=load_module(module, now)['data'])
    raise ValueError('Unknown module')


def refresh(modules, *, as_of=None, phase='auto', input_data=None, rebuild=True):
    now = as_of or dt.datetime.now(TZ)
    started = dt.datetime.now(TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    now = now.astimezone(TZ)
    if now > dt.datetime.now(TZ) + dt.timedelta(seconds=2):
        raise ValueError('A future research cutoff is not allowed')
    phase = choose_phase(now) if phase == 'auto' else phase
    if phase not in ('prepare', 'intraday', 'close'):
        raise ValueError('Unknown research phase')
    requested = list(dict.fromkeys(modules))
    if not requested or any(m not in FOLDERS for m in requested):
        raise ValueError('Unknown research module')
    if input_data is not None and len(requested) != 1:
        raise ValueError('Import one research module at a time')
    results = []

    def deliver(module, data):
        # Use the actual completion time. The data itself retains the requested
        # cutoff and its own market times; publication does not invent evidence.
        data = dict(data, run_started_at=started.isoformat())
        outcome = publish(module, data, attempted_at=dt.datetime.now(TZ))
        if rebuild and outcome['changed']:
            try:
                build()
                outcome['page_status'] = 'updated'
            except Exception:
                # A failed renderer preserves the last good site and does not
                # invalidate successfully acquired research or another worker.
                outcome['page_status'] = 'failed'
        return outcome

    def execute(module, supplied=None, hot_data=None):
        try:
            data = run_module(module, now, phase, input_data=supplied, hot=hot_data)
            return deliver(module, data), data
        except Exception as exc:
            # Detailed raw runs stay private; no path/credential-bearing exception
            # messages are returned to the page or automation report.
            outcome = record_failure(module, '本次研究执行或数据核验失败，请检查本地原始证据。', run_started_at=started)
            if rebuild and outcome['changed']:
                try:
                    build()
                except Exception:
                    outcome['page_status'] = 'failed'
            outcome['error_type'] = type(exc).__name__
            return outcome, None

    def hot_and_dragon():
        local = []
        hot_data = None
        if 'hot' in requested:
            outcome, hot_data = execute('hot', input_data)
            local.append(outcome)
        if 'dragon' in requested:
            # If explicitly running only dragon, its adapter obtains/validates a
            # matching hot context. A failed hot refresh is never silently hidden.
            if 'hot' in requested and (not hot_data or hot_data.get('status') == 'unavailable'):
                unavailable = {'schema_version': 1, 'module_id': 'dragon', 'status': 'unavailable',
                               'summary': '热点比较池未取得有效新结果，龙空龙等待重新核验。',
                               'missing': ['不能沿用过期热点名次生成新名单。']}
                local.append(deliver('dragon', unavailable))
            else:
                local.append(execute('dragon', input_data, hot_data)[0])
        return local

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        tasks = []
        if 'hot' in requested or 'dragon' in requested:
            tasks.append(pool.submit(hot_and_dragon))
        for module in ('yichujifa', 'prelaunch'):
            if module in requested:
                tasks.append(pool.submit(lambda m=module: [execute(m, input_data)[0]]))
        for task in concurrent.futures.as_completed(tasks):
            results.extend(task.result())
    return {'phase': phase, 'requested_at': now.isoformat(), 'modules': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--module', choices=['all'] + list(FOLDERS), default='all')
    parser.add_argument('--phase', choices=['auto', 'prepare', 'intraday', 'close'], default='auto')
    parser.add_argument('--as-of', help='Shanghai analysis cutoff, ISO timestamp; never a future time')
    parser.add_argument('--input', type=Path, help='Private native report or normalized research snapshot')
    parser.add_argument('--evidence', type=Path, help='Yichujifa native evidence JSON alongside --input report')
    parser.add_argument('--live-review', type=Path, help='Yichujifa native live-review JSON, not a replay')
    parser.add_argument('--snapshots', type=Path, help='Native three-frame evidence for --live-review')
    parser.add_argument('--enrichment', type=Path, help='Prelaunch evidence bound to input and rules fingerprints')
    parser.add_argument('--no-build', action='store_true', help='Publish private results without building the page')
    args = parser.parse_args()
    when = stamp(args.as_of) if args.as_of else dt.datetime.now(TZ)
    if when is None:
        parser.error('Invalid --as-of timestamp')
    modules = list(FOLDERS) if args.module == 'all' else [args.module]
    input_data = _read_input(args.input) if args.input else None
    if args.evidence or args.live_review or args.snapshots:
        if args.module != 'yichujifa' or input_data is None:
            parser.error('Native live/evidence files require --module yichujifa and --input')
        input_data = {'report':input_data, 'evidence':_read_input(args.evidence) if args.evidence else None,
                      'live':_read_input(args.live_review) if args.live_review else None,
                      'snapshots':_read_input(args.snapshots, frames=True) if args.snapshots else None}
    if args.enrichment:
        if args.module != 'prelaunch' or input_data is None:
            parser.error('Bound enrichment requires --module prelaunch and --input')
        input_data = {'prelaunch_input':input_data,'enrichment':_read_input(args.enrichment)}
    result = refresh(modules, as_of=when, phase=args.phase,
                     input_data=input_data, rebuild=not args.no_build)
    print(json.dumps(result, ensure_ascii=False))
    return 2 if any(x['status'] == 'unavailable' or x.get('page_status') == 'failed' for x in result['modules']) else 0


if __name__ == '__main__':
    raise SystemExit(main())
