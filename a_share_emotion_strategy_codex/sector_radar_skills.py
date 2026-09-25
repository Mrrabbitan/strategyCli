"""Independent native strategy checks for the read-only sector radar.

Raw inputs may be supplied under each strategy key.  Result flags, a radar
subset, or yesterday's report are never accepted as substitute evidence.
Native runs do not publish or update another strategy's current roster.
"""
from __future__ import annotations

import copy
import ast
import datetime as dt
import hashlib
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit

from research_store import atomic_json, read_json, research_path, update_lock, load_config

TZ = ZoneInfo('Asia/Shanghai')
MODULES = ('prelaunch', 'yichujifa', 'dragon', 'late-day', 'three-step')
ROOT = Path(__file__).resolve().parent


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _stamp(value):
    stamp = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value))
    if stamp.tzinfo is None:
        raise ValueError('Evidence timestamps must include a timezone')
    return stamp.astimezone(TZ)


def _list(value):
    return value if isinstance(value, list) else [value] if value else []


def _rule(module):
    if module == 'prelaunch':
        import prelaunch_research as native
        return native.VERSION, native.SOURCE_HASH
    if module == 'dragon':
        import dragon_research as native
        rule = native.rules()
        return rule['version'], rule['source_hash']
    if module == 'yichujifa':
        import yichujifa_research as native
        tree = ast.parse((native.SKILL/'scripts/engine.py').read_text(encoding='utf-8'))
        version = next((n.value.value for n in tree.body if isinstance(n, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == 'VERSION' for t in n.targets)
                        and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)), '本机技能原文')
        rule = native._rules({'version': version})
        return rule['version'], rule['source_hash']
    if module == 'late-day':
        import late_day_research as native
    else:
        import three_step_research as native
    engine = native.load_engine()
    return engine.VERSION, native.source_hash()


def _sources(values):
    result, seen = [], set()
    for value in _list(values):
        item = value if isinstance(value, dict) else {'url': value}
        url = item.get('url') or item.get('source')
        try:
            parsed = urlsplit(str(url))
        except ValueError:
            continue
        if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password or url in seen:
            continue
        result.append({'label': str(item.get('label') or '原生行情或公告证据'), 'url': url})
        seen.add(url)
    return result


def _cell(module, status='pending', *, report=None, row=None, reasons=None, passed=False):
    report, row = report or {}, row or {}
    rule = report.get('rules') or {}
    version = report.get('rule_version') or rule.get('version')
    rule_hash = report.get('rule_hash') or rule.get('source_hash')
    if not version or not rule_hash:
        try:
            version, rule_hash = _rule(module)
        except (OSError, ValueError, KeyError, SyntaxError):
            version, rule_hash = '规则文件不可用', None
    reasons = list(dict.fromkeys(str(v) for v in _list(reasons) if v))
    return {'status': status, 'observation_passed': bool(passed and status == 'passed'),
            'rule_version': report.get('rule_version') or rule.get('version') or version,
            'rule_hash': report.get('rule_hash') or rule.get('source_hash') or rule_hash,
            'as_of': report.get('as_of'), 'reasons': reasons,
            'confirmation': _list(row.get('confirmation')),
            'risks': _list(row.get('risks') or row.get('risk') or row.get('invalidation')),
            'levels': copy.deepcopy(row.get('levels') or []),
            'sources': _sources(row.get('sources') or report.get('sources') or []),
            'native_status': row.get('status') or row.get('decision_state') or row.get('state'),
            'actionable': False}


def _require_date(data, signal, field='signal_date'):
    if not isinstance(data, dict) or data.get(field) != signal:
        raise ValueError('Raw evidence date does not match the signal date')
    if 'by_code' in data or data.get('module_id') or ('rows' in data and 'stocks' not in data):
        raise ValueError('Supply native raw evidence, not a precomputed passing report')


def _previous_prelaunch(signal):
    """Union original/radar immutable structures; never reset an invalid platform."""
    import prelaunch_research as native
    snapshots = [read_json(research_path('prelaunch/current.json'), {}),
                 read_json(research_path('sector_radar/native/prelaunch_frozen.json'), {})]
    records, invalid = {}, {}
    priority = {'active': 0, 'expired': 1, 'started': 2, 'invalid': 3}
    for snap in snapshots:
        if (snap.get('rules') or {}).get('source_hash') != native.SOURCE_HASH:
            continue
        for record in snap.get('frozen_records', []):
            if any(record.get(k, '') > signal for k in ('frozen_at', 'ended_at', 'invalidated_at')):
                continue
            key = record.get('id')
            if not key:
                continue
            old = records.get(key)
            if old and any(old.get(k) != record.get(k) for k in ('support', 'upper', 'frozen_at')):
                raise ValueError('Conflicting immutable prelaunch platform evidence')
            if not old or priority.get(record.get('state'), 0) > priority.get(old.get('state'), 0):
                records[key] = copy.deepcopy(record)
        for row in snap.get('invalid', []):
            if row.get('status') in ('失效', '观察到期'):
                invalid[row.get('code')] = row
    return {'rules': {'source_hash': native.SOURCE_HASH},
            'frozen_records': list(records.values()), 'invalid': list(invalid.values())}


def _prelaunch_input(stocks, signal, supplied):
    """Translate acquired bars only; do not invent verified context or classifications."""
    rows = []
    for stock in stocks:
        daily = stock.get('daily') or {}
        row = copy.deepcopy(stock.get('native_prelaunch') or {})
        row.update(code=stock['code'], name=stock.get('name', stock['code']))
        if not row.get('bars'):
            row['bars'] = copy.deepcopy(daily.get('bars') or [])
        row.setdefault('sources', _sources(stock.get('sources') or daily.get('sources') or daily.get('source') or []))
        row.setdefault('history_verified', daily.get('cross_verified') is True)
        row.setdefault('adjustment_basis', daily.get('adjustment_basis') or
                       ('%s-qfq-%s' % (daily.get('provider'), signal) if
                        daily.get('adjustment') == 'qfq' and daily.get('adjustment_as_of') == signal else None))
        # Generic security and announcement dictionaries are not the richer
        # V3.4 audit/solvency review. Only its original evidence is admissible.
        rows.append(row)
    return {'signal_date': signal, 'stocks': rows,
            'benchmark': copy.deepcopy(supplied.get('benchmark') or {}),
            'market': copy.deepcopy(supplied.get('market') or {}),
            'industries': copy.deepcopy(supplied.get('industries') or {}),
            'coverage': {'history_requested': len(rows), 'scope': '板块雷达全部已取得股票；非全市场核心榜'},
            'sources': [], 'missing': []}


def _run_prelaunch(at, signal, stocks, calendar, supplied, refresh, folder):
    import prelaunch_research as native
    data = copy.deepcopy(supplied.get('prelaunch')) if supplied.get('prelaunch') is not None else _prelaunch_input(stocks, signal, supplied)
    _require_date(data, signal)
    codes = {s['code'] for s in data.get('stocks', [])}
    if not {s['code'] for s in stocks}.issubset(codes):
        raise ValueError('Prelaunch raw evidence must include every radar stock; no 240-stock truncation')
    # The original calculator applies its Top10 / industry / catalyst caps once
    # across the whole input, before any screenshot-sector grouping.
    with update_lock():
        previous = _previous_prelaunch(signal)
        report = native.research(at, 'close', input_data=data,
                                 enrichment=supplied.get('prelaunch_enrichment'), previous=previous)
        if report.get('signal_date') == signal and report.get('status') != 'unavailable':
            target = research_path('sector_radar/native/prelaunch_frozen.json')
            old = read_json(target, {})
            if old.get('signal_date', '') <= signal:
                atomic_json(target, {'signal_date': signal, 'rules': report['rules'],
                                     'frozen_records': report['frozen_records'], 'invalid': report['invalid']})
    atomic_json(folder / 'input.json', data)
    return report


def _run_yichujifa(at, signal, stocks, calendar, supplied, refresh, folder):
    """Native scan also revalidates pool metadata/limits for imported inputs."""
    import yichujifa_research as native
    if supplied.get('yichujifa') is None and not refresh:
        raise LookupError('尚未提供一触即发原始证据；本次未执行联网筛选')
    folder.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(native.SKILL / 'scripts/run.py'), 'scan',
               '--as-of', signal, '--output', str(folder), '--no-latest']
    if supplied.get('yichujifa') is not None:
        data = copy.deepcopy(supplied['yichujifa'])
        _require_date(data, signal, 'as_of')
        sessions = data.get('sessions') or []
        valid = [d for d in calendar['days'] if d <= signal]
        if not sessions or sessions != valid[-len(sessions):]:
            raise ValueError('Native scan sessions do not match the verified project calendar')
        if _stamp(data.get('information_cutoff') or data.get('generated_at')) > at:
            raise ValueError('Native scan input has future information')
        atomic_json(folder / 'input.json', data)
        command += ['--input', str(folder / 'input.json')]
    try:
        completed = subprocess.run(command, cwd=str(native.SKILL), capture_output=True,
                                   text=True, timeout=240)
    except subprocess.TimeoutExpired as exc:
        def tail(value):
            return (value.decode('utf-8', errors='replace') if isinstance(value, bytes) else str(value or ''))[-8000:]
        atomic_json(folder / 'process-error.json', {'timed_out': True, 'timeout_seconds': 240,
                'stdout': tail(exc.stdout), 'stderr': tail(exc.stderr)})
        raise
    if completed.returncode:
        atomic_json(folder / 'process-error.json', {'returncode': completed.returncode,
                'stdout': completed.stdout[-8000:], 'stderr': completed.stderr[-8000:]})
        raise ValueError('一触即发原生scan失败；未更新其latest指针')
    raw = read_json(folder / 'report.json', {})
    evidence = read_json(folder / 'evidence.json', {})
    if raw.get('as_of') != signal or evidence.get('as_of') != signal:
        raise ValueError('Native scan returned a different signal date')
    validation_time = at if supplied.get('yichujifa') is not None else max(at, dt.datetime.now(TZ))
    report = native.import_native(raw, evidence=evidence, as_of=validation_time)
    report['input_fingerprint'] = _digest(evidence)
    return report


def _run_dragon(at, signal, stocks, calendar, supplied, refresh, folder):
    import dragon_research as native
    from refresh_hot_sector_board import research_hot
    from market_calendar import is_trading_day
    trading, reason = is_trading_day(at.date())
    if '缺少' in reason or '不可用' in reason:
        raise ValueError(reason)
    phase = 'close' if trading and at.time().replace(tzinfo=None) >= dt.time(15) else 'prepare'
    hot = copy.deepcopy(supplied.get('dragon_hot'))
    if hot is not None:
        from hot_sector_board import build_board
        from refresh_hot_sector_board import resolve_sessions
        if not isinstance(hot.get('pool_payload'), dict):
            raise ValueError('龙空龙导入必须含原始pool_payload、quotes、histories；不信任已排序报告')
        _, target = resolve_sessions(at, phase)
        raw_hot = copy.deepcopy(hot)
        hot = build_board(raw_hot['pool_payload'], raw_hot.get('quotes') or {}, raw_hot.get('histories') or {},
                          cutoff=signal, next_session=target.isoformat(), config=load_config('emotion_config.json'))
        hot.update(as_of=signal+'T15:00:00+08:00', sources=raw_hot.get('sources') or [])
        if not hot['sources']:
            raise ValueError('原始热点输入缺可追溯来源')
    if hot is None:
        if not refresh:
            raise LookupError('尚未提供龙空龙原始热点排名；本次未执行联网筛选')
        hot = research_hot(at, phase, persist=False)
    if hot.get('cutoff') != signal:
        raise ValueError('龙空龙原榜不是本次信号日；不沿用旧榜')
    # Keep the freshly fetched source, but pass only signal-close evidence to
    # this close-only comparison. Today's quote/auction/refill must neither
    # advance yesterday's volume ledger nor claim tomorrow's entry conditions.
    atomic_json(folder / 'hot_source.json', hot)
    source_fingerprint = _digest(hot)
    close_stamp = signal + 'T15:00:00+08:00'
    ranking_stamp = hot.get('ranking_as_of') or close_stamp
    if _stamp(ranking_stamp).date().isoformat() != signal:
        raise ValueError('原始热点排名截止时间与信号日冲突')
    remove = {'current_quote', 'auction', 'refill_evidence', 'auction_as_of', 'auction_minutes'}
    def dated_close_context(value):
        if isinstance(value,list):
            return [item for item in (dated_close_context(v) for v in value) if item is not None]
        if not isinstance(value,dict):return None
        try:source=_stamp(value.get('source_asof') or value.get('source_as_of'))
        except (ValueError,TypeError):return None
        return value if source.date().isoformat()==signal and source<=_stamp(close_stamp) else None
    def close_only(value):
        if isinstance(value, dict):
            result={}
            for key,item in value.items():
                if key in remove:continue
                if key in {'risk_evidence','security_evidence','market_evidence'}:
                    item=dated_close_context(item)
                    if not item:continue
                result[key]=close_only(item)
            return result
        if isinstance(value, list):
            return [close_only(item) for item in value]
        return value
    evidence = close_only(hot)
    # The source's full ranking is still the same immutable close pool; the
    # actual execution time remains `at` and is never backfilled to that close.
    evidence['as_of'] = close_stamp
    evidence['ranking_as_of'] = ranking_stamp
    atomic_json(folder / 'hot_evidence.json', evidence)
    report = native.research(at, phase, hot=evidence, previous={})
    report['input_fingerprint'] = _digest(evidence)
    report['original_hot_fingerprint'] = source_fingerprint
    report['evidence_scope'] = '信号日完整收盘排名与历史量账；不含下一交易日盘中证据'
    return report


def _run_three_step(at, signal, stocks, calendar, supplied, refresh, folder):
    import three_step_research as native
    native_signal, native_calendar = native.calendar_payload(at)
    if native_signal != signal:
        raise ValueError('Three-step canonical signal date conflicts with radar date')
    data = copy.deepcopy(supplied.get('three-step'))
    if data is None:
        if not refresh:
            raise LookupError('尚未提供三步选股全主板原始证据；本次未执行联网筛选')
        data = native.collect(at, signal, native_calendar)
    else:
        _require_date(data, signal)
        universe = data.get('universe') or {}
        if universe.get('scope') != 'all_mainboard' and universe.get('whole_mainboard') is not True:
            raise ValueError('三步选股必须提供全主板排名池；不能使用板块雷达子集')
    data['calendar'] = copy.deepcopy(native_calendar)
    report = native.load_engine().evaluate(data, as_of=at)
    report['rule_hash'] = native.source_hash()
    atomic_json(folder / 'input.json', data)
    return report


def _late_inputs(signal, at):
    """Read raw saved evidence, never a report's precomputed passing flags."""
    paths = list(research_path('late_day/evidence').glob('*.json'))
    paths += list(research_path('late_day/scheduled/' + signal).glob('*input*.json'))
    result = []
    for path in paths:
        data = read_json(path, {})
        try:
            stamp = _stamp(data.get('as_of'))
        except (ValueError, TypeError):
            continue
        if stamp.date().isoformat() == signal and stamp <= at and dt.time(14, 30) <= stamp.time().replace(tzinfo=None) < dt.time(14, 57):
            result.append(data)
    return sorted(result, key=lambda data: data['as_of'])


def _run_late_day(at, signal, stocks, calendar, supplied, refresh, folder):
    import late_day_research as native
    data_items = supplied.get('late-day')
    data_items = _list(data_items) if data_items is not None else _late_inputs(signal, at)
    engine, reports, by_code = native.load_engine(), [], {}
    for data in data_items:
        if not isinstance(data, dict) or 'stocks' not in data or 'rows' in data:
            raise ValueError('尾盘复核只接收原始分钟证据，不接收通过名单')
        when = _stamp(data.get('as_of'))
        if when.date().isoformat() != signal or when > at or not dt.time(14, 30) <= when.time().replace(tzinfo=None) < dt.time(14, 57):
            raise ValueError('尾盘复核必须使用信号日14:30至14:57前已保存证据；不可用收盘K线倒推')
        report = engine.evaluate(data, as_of=when, phase='review', now=at, positions=[])
        report['rule_hash'] = native.source_hash()
        reports.append(report)
        for row in report['rows']:
            old = by_code.get(row['code'])
            if not old or report['as_of'] > old[0]:
                by_code[row['code']] = (report['as_of'], row)
    combined = {'as_of': signal + 'T15:00:00+08:00', 'rule_version': engine.VERSION,
                'rule_hash': native.source_hash(), 'phase': 'review',
                'rows': [dict(row, evidence_as_of=stamp) for stamp, row in by_code.values()],
                'status': 'partial' if not reports or any(r.get('missing') for r in reports) else 'complete',
                'missing': ['未取得同日有效尾盘原始分钟证据；未用收盘数据倒推'] if not reports else [],
                'input_fingerprint': _digest(data_items), 'source_reports': reports}
    atomic_json(folder / 'inputs.json', data_items)
    return combined


RUNNERS = {'prelaunch': _run_prelaunch, 'yichujifa': _run_yichujifa, 'dragon': _run_dragon,
           'late-day': _run_late_day, 'three-step': _run_three_step}


def _map_prelaunch(report, codes):
    # Invalid includes archived earlier rounds. Current rows must not be
    # overwritten by an appended historical record with the same stock code.
    rows = {r['code']: r for group in ('invalid', 'started', 'watch', 'core') for r in report.get(group, [])}
    result = {}
    for code in codes:
        row = rows.get(code)
        if row is None:
            result[code] = _cell('prelaunch', report=report, reasons=report.get('missing') or ['未取得该股完整原生计算记录'])
            continue
        checks = row.get('conditions') or []
        passed = row.get('eligible') is True and all(c.get('passed') is True for c in checks) and bool(checks)
        known_fail = (row.get('status') in ('失效', '排除', '观察到期', '已启动') or
                      any(c.get('passed') is False for c in checks) or
                      any('组合上限' in reason for reason in row.get('reasons', [])))
        status = 'passed' if passed else 'failed' if known_fail else 'pending'
        result[code] = _cell('prelaunch', status, report=report, row=row, passed=passed,
                             reasons=[row.get('status')] + row.get('reasons', []))
    return result


def _map_yichujifa(report, codes):
    grouped = {code: [] for code in codes}
    for row in report.get('candidates', []):
        if row.get('code') in grouped:
            grouped[row['code']].append(row)
    result = {}
    for code, rows in grouped.items():
        branches = []
        for row in rows:
            checks = [c for c in row.get('conditions', []) if c.get('label') != '当前盘中触发']
            passed = (row.get('prequalified') is True and bool(checks) and
                      all(c.get('passed') is True for c in checks) and
                      (report.get('phase_evidence') or {}).get('verified') is True)
            failed = row.get('status') in ('不做', '已启动跟踪', '降级观察') or any(c.get('passed') is False for c in checks)
            # Native prequalified false may simply reflect unknown evidence.
            unknown = any(c.get('passed') is None for c in checks) or any(
                word in reason for reason in row.get('reasons', []) for word in ('待验证', '未核验', '不完整', '不全', '缺失'))
            state = 'passed' if passed else 'failed' if failed and not unknown else 'pending'
            note = ['收盘预资格通过，次日9:50后修复与两次新行情仍须独立核验'] if passed else ['收盘预资格证据不足']
            branch = _cell('yichujifa', state, report=report, row=row, passed=passed,
                           reasons=row.get('reasons') or note)
            branch.update(branch=row.get('branch'), original_rank=row.get('rank'))
            branches.append(branch)
        winner = next((r for r in branches if r['observation_passed']), None)
        if winner:
            cell = copy.deepcopy(winner)
        elif branches:
            state = 'failed' if all(r['status'] == 'failed' for r in branches) else 'pending'
            cell = _cell('yichujifa', state, report=report, reasons=[x for r in branches for x in r['reasons']])
        else:
            # Absence from an incomplete scan is not evidence of disqualification.
            cell = _cell('yichujifa', report=report,
                         reasons=report.get('missing') or ['当前原生三分支未覆盖该股；未把缺记录记为淘汰'])
        cell['branches'] = branches
        result[code] = cell
    return result


def _map_dragon(report, codes):
    rows = {r['code']: r for r in report.get('candidates', [])}
    result = {}
    for code in codes:
        row = rows.get(code)
        if not row:
            complete_rank = (report.get('hot_sector_focus') or {}).get('source_verified') is True and report.get('status') != 'unavailable'
            result[code] = _cell('dragon', 'failed' if complete_rank else 'pending', report=report,
                                 reasons=['未进入原热点前五板块原始前三；不因其他股票失败补位'] if complete_rank else report.get('missing') or ['原热点全池排名不可核验'])
            continue
        risks = row.get('status') == '退出预警' or row.get('expansion_count', 0) >= 2
        passed = row.get('historical_screen_pass') is True and not risks
        failed = risks or row.get('status') == '不满足新增条件'
        cell = _cell('dragon', 'passed' if passed else 'failed' if failed else 'pending',
                     report=report, row=row, passed=passed,
                     reasons=row.get('reasons') or ['收盘量价预筛通过；次日竞价、回封及市场环境仍待验证'])
        cell['confirmation'] = ['仅收盘观察：次日9:25最终竞价、当日回封及市场证据完整后另行判断；不是当前介入信号']
        cell['volume_ledger'] = copy.deepcopy(row.get('volume_ledger') or {})
        cell['original_rank'] = row.get('sector_member_rank')
        result[code] = cell
    return result


def _map_three_step(report, codes):
    rows = {r['code']: r for r in report.get('rows', [])}
    result = {}
    for code in codes:
        row = rows.get(code)
        if not row:
            result[code] = _cell('three-step', report=report, reasons=report.get('missing') or ['全主板原生计算未覆盖该股'])
            continue
        steps = row.get('steps') or []
        passed = row.get('research_passed') is True and len(steps) == 3 and all(s.get('state') == 'pass' for s in steps) and (report.get('coverage') or {}).get('global_rank_verified') is True
        status = 'passed' if passed else 'failed' if any(s.get('state') == 'fail' for s in steps) else 'pending'
        cell = _cell('three-step', status, report=report, row=row, passed=passed,
                     reasons=[s.get('reason') for s in steps] + row.get('missing', []))
        cell['steps'] = copy.deepcopy(steps)
        cell['metrics'] = copy.deepcopy(row.get('metrics') or {})
        result[code] = cell
    return result


def _map_late_day(report, codes):
    rows = {r['code']: r for r in report.get('rows', [])}
    result = {}
    for code in codes:
        row = rows.get(code)
        if not row:
            result[code] = _cell('late-day', 'pending', report=report,
                                 reasons=['只有收盘资料，未取得该股同日有效尾盘原始证据；不得倒推尾盘资格'])
            continue
        passed = row.get('research_passed') is True
        failed = row.get('decision_state') in ('rejected', 'failed')
        cell = _cell('late-day', 'passed' if passed else 'failed' if failed else 'pending',
                     report=report, row=row, passed=passed,
                     reasons=['仅为信号日尾盘历史复核，不代表当前可参与'] +
                             [str(c.get('label')) + '：' + str(c.get('detail', '')) for c in row.get('checks', []) if c.get('passed') is not True])
        cell['as_of'] = row.get('evidence_as_of')
        cell['confirmation'] = ['不重授尾盘参与资格；有实际持仓时才应用下一交易日10:00结束纪律']
        result[code] = cell
    return result


MAPPERS = {'prelaunch': _map_prelaunch, 'yichujifa': _map_yichujifa, 'dragon': _map_dragon,
           'late-day': _map_late_day, 'three-step': _map_three_step}


def run_checks(as_of, signal_date, stocks, calendar, *, refresh_native=True, supplied=None):
    """Run independent native checks and return a five-column, non-actionable matrix.

    ``supplied`` contains *raw* native evidence, not reports. Three-step imports
    additionally name universe.scope=all_mainboard. Prelaunch accepts a separate
    original hash-bound prelaunch_enrichment. Public page builders never call us.
    """
    at, supplied = _stamp(as_of), supplied or {}
    signal = str(signal_date)
    if dt.datetime.fromisoformat(signal + 'T15:00:00+08:00') > at:
        raise ValueError('Sector radar may only evaluate completed trading days')
    days = calendar.get('days') or []
    if calendar.get('verified') is not True or not calendar.get('source') or signal not in days or days != sorted(set(days)):
        raise ValueError('A verified exchange calendar is required')
    if any(d <= at.date().isoformat() and d > signal and dt.datetime.fromisoformat(d+'T15:00:00+08:00') <= at for d in days):
        raise ValueError('Signal date must be the latest completed session at the analysis time')
    codes = [s['code'] for s in stocks]
    if len(codes) != len(set(codes)):
        raise ValueError('Radar stock identifiers must be unique before native checks')
    run_id = dt.datetime.now(TZ).strftime('%Y%m%dT%H%M%S%f')
    root = research_path('sector_radar/native/runs') / run_id
    result = {'by_code': {c: {} for c in codes}, 'executions': [], 'missing': []}
    def scope_failure(stock):
        code = stock['code']
        if len(code) != 6 or not code.isdigit() or not code.startswith(('000','001','002','003','600','601','603','605')):
            return '不属于沪深主板普通A股范围'
        if 'ST' in str(stock.get('name', '')).upper():
            return '证券简称包含ST风险标记，不进入观察资格'
        sec = stock.get('security') or {}
        if sec.get('verified') is True and sec.get('date') == signal and sec.get('source'):
            if any(sec.get(k) is True for k in ('st','delisting','suspended')) or sec.get('normal_limit') is False:
                return '已核验当日证券状态不合格'
        return None
    excluded = {s['code']: scope_failure(s) for s in stocks}
    def execute(module):
        folder = root / module
        began = dt.datetime.now(TZ).isoformat()
        focus_input = None
        try:
            report = RUNNERS[module](at, signal, stocks, calendar, supplied, refresh_native, folder)
            if module == 'prelaunch':
                focus_input = report
            cells = MAPPERS[module](report, codes)
            for cell in cells.values():
                if cell['observation_passed'] and (str(cell.get('as_of', ''))[:10] != signal or
                        not cell.get('rule_version') or not cell.get('rule_hash') or not cell.get('sources')):
                    cell.update(status='pending', observation_passed=False)
                    cell['reasons'].append('原生通过记录的来源、规则指纹或信号日期不完整；未升级观察')
            atomic_json(folder / 'native-result.json', report)
            execution = {'module': module, 'status': report.get('status', report.get('state', 'partial')),
                         'started_at': began, 'finished_at': dt.datetime.now(TZ).isoformat(),
                         'as_of': report.get('as_of'), 'input_fingerprint': report.get('input_fingerprint'),
                         'coverage': report.get('coverage') or {}, 'missing': report.get('missing') or [],
                         'actionable': False}
        except Exception as exc:
            focus_input = None
            issue = ('本次未执行：' if isinstance(exc, LookupError) else '原生证据执行受阻：') + str(exc)
            # Avoid exposing private paths or subprocess stderr in the page.
            if any(p in issue for p in ('/Users/', '/var/', '/private/', '/tmp/')):
                issue = '原生证据执行受阻；详细错误仅保存在本机私有记录'
            state = 'not_run' if isinstance(exc, LookupError) else 'pending'
            try:
                version, fingerprint = _rule(module)
            except (OSError, ValueError, KeyError, SyntaxError):
                version, fingerprint = '规则文件不可用', None
            failed_meta = {'rule_version':version,'rule_hash':fingerprint}
            cells = {c: _cell(module, state, report=failed_meta, reasons=[issue]) for c in codes}
            execution = {'module': module, 'status': 'not_run' if isinstance(exc, LookupError) else 'failed',
                         'started_at': began, 'finished_at': dt.datetime.now(TZ).isoformat(),
                         'as_of': None, 'missing': [issue], 'actionable': False}
            atomic_json(folder / 'error.json', {'type': type(exc).__name__, 'detail': str(exc)})
        execution['strategy'] = module
        try:
            execution['rule_version'], execution['rule_hash'] = _rule(module)
        except (OSError, ValueError, KeyError, SyntaxError):
            execution['rule_version'], execution['rule_hash'] = '规则文件不可用', None
        for code, reason in excluded.items():
            if reason:
                cells[code].update(status='failed', observation_passed=False, reasons=[reason])
        return module, cells, execution, focus_input
    with ThreadPoolExecutor(max_workers=4) as pool:
        tasks = [pool.submit(execute, module) for module in MODULES]
        for future in as_completed(tasks):
            module, cells, execution, focus_input = future.result()
            for code in codes:
                result['by_code'][code][module] = cells[code]
            result['executions'].append(execution)
            if focus_input is not None:
                result['prelaunch_focus_input'] = focus_input
            result['missing'] += [module + '：' + str(x) for x in execution['missing']]
    result['executions'].sort(key=lambda item: MODULES.index(item['module']))
    result['missing'] = list(dict.fromkeys(result['missing']))
    atomic_json(root / 'matrix.json', result)
    return result
