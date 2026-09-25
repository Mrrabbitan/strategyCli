"""Small, read-only daily radar view models; never computes stock eligibility.

The review queue is exactly the saved prelaunch focus (pending entries and
near misses), not a new scan over all candidates. Raw evidence, accounts and
the five-strategy matrix never leave this allow-listed presentation model.
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import math
import re
from urllib.parse import parse_qsl, urlsplit

from research_modules import TZ, content_hash, load_module, module_path, stamp
from research_store import read_json


STRATEGY_NAMES = {'prelaunch': '启动前潜伏', 'yichujifa': '一触即发',
                  'dragon': '龙空龙', 'late-day': '尾盘隔夜', 'three-step': '三步选股'}


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _records(value):
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def _text(value):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ''
    # A supplier error may contain a local cache filename. Keep that detail
    # private even when it appears in an otherwise approved explanatory field.
    return re.sub(r'(?:file://)?/(?:Users|private|tmp|var|home)/[^\s，。；<>]+',
                  '[本地证据]', str(value))


def _texts(value):
    values = value if isinstance(value, list) else [value]
    return list(dict.fromkeys(text for text in (_text(x) for x in values) if text))


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _day(value):
    try:
        value = str(value)
        return dt.date.fromisoformat(value).isoformat() if len(value) == 10 else None
    except (TypeError, ValueError):
        return None


def _sources(values):
    out = []
    for item in values if isinstance(values, list) else []:
        item = item if isinstance(item, dict) else {'url': item}
        url = item.get('url') or item.get('source')
        try:
            parsed = urlsplit(str(url))
            host = (parsed.hostname or '').lower()
            if parsed.scheme not in ('https', 'http') or not host or parsed.username or parsed.password:
                continue
            if host == 'localhost' or '.' not in host or host.endswith(('.local', '.localhost')):
                continue
            try:
                if not ipaddress.ip_address(host).is_global:
                    continue
            except ValueError:
                pass
            if any(re.search(r'token|secret|password|api.?key|credential', key, re.I)
                   for key, _ in parse_qsl(parsed.query)):
                continue
        except (TypeError, ValueError):
            continue
        value = {'label': _text(item.get('label') or '行情或公告证据'), 'url': str(url)}
        if not any(x['url'] == value['url'] for x in out):
            out.append(value)
    return out


def _levels(value, entry=None):
    levels = []
    for item in _records(value):
        number = _number(item.get('value'))
        if number is not None and _text(item.get('label')):
            levels.append({'label': _text(item['label']), 'value': number})
    for key, label in (('support', '冻结支撑'), ('upper', '冻结上沿'), ('pressure', '最近压力')):
        number = _number((entry or {}).get(key))
        if number is not None and not any(x['label'] == label and x['value'] == number for x in levels):
            levels.append({'label': label, 'value': number})
    return levels


def _row(entry, radar, sector, kind, historical, signal):
    code = str(entry.get('code') or radar.get('code') or '')
    dated_bars = {b['date']: b for b in _records(radar.get('bars'))
                  if _day(b.get('date')) and b['date'] <= signal}
    bars = [dated_bars[day] for day in sorted(dated_bars)]
    metrics = _mapping(radar.get('metrics'))
    price = _number(entry.get('price'))
    if price is None and bars:
        price = _number(bars[-1].get('close'))
    r5 = _number(entry.get('return_5d_pct'))
    if r5 is None and _number(metrics.get('return_5d')) is not None:
        r5 = metrics['return_5d'] * 100
    r20 = _number(entry.get('return_20d_pct'))
    if r20 is None and len(bars) >= 21:
        first, last = _number(bars[-21].get('close')), _number(bars[-1].get('close'))
        if first is not None and first > 0 and last is not None:
            r20 = (last / first - 1) * 100
    failures = _texts(entry.get('failure_reasons'))
    reasons = failures + _texts(entry.get('probe_evidence')) + _texts(entry.get('reasons'))
    if kind == 'review':
        status = '条件失败 · 尚未纳入观察' if failures else '优先补证 · 尚未纳入观察'
        next_check = _texts(entry.get('confirmation'))
        if failures:
            next_check.insert(0, '当前失败条件未解除，不进入正式观察；仅按原规则复核，不放宽门槛。')
    elif kind == 'started':
        status = '已启动跟踪 · 不列低位候选'
        next_check = _texts(entry.get('confirmation'))
        reasons = reasons or ['原生阶段已启动，单独跟踪，不改标为低位待启动。']
    else:
        status = '正式收盘观察 · 非买点'
        next_check = []
    return {'code': code, 'name': _text(entry.get('name') or radar.get('name') or code),
            'groups': [{'id': _text(sector.get('id')), 'label': _text(sector.get('label') or sector.get('id'))}],
            'price': price, 'reasons': list(dict.fromkeys(reasons)), 'next_check': next_check,
            'levels': _levels(entry.get('levels'), entry),
            'sources': _sources(list(entry.get('sources') or []) + list(radar.get('sources') or [])),
            'r5': r5, 'r20': r20, 'missing': _texts(entry.get('missing')),
            'status': status, 'historical': historical, 'admitted': kind == 'observed',
            'failure_reasons': failures, 'supporting_skills': [],
            'risks': _texts(entry.get('risks')), 'actionable': False}


def _merge(rows, row):
    code = row['code']
    if not re.fullmatch(r'\d{6}', code):
        return
    if code not in rows:
        rows[code] = row
        return
    current = rows[code]
    for key in ('groups', 'reasons', 'next_check', 'levels', 'sources', 'missing',
                'failure_reasons', 'supporting_skills', 'risks'):
        for value in row[key]:
            if value not in current[key]:
                current[key].append(value)
    if current['failure_reasons'] and not current['admitted']:
        current['status'] = '条件失败 · 尚未纳入观察'


def _supporting(row, signal):
    if row.get('state') != 'watch':
        return []
    support = []
    for sid, check in _mapping(row.get('strategy_checks')).items():
        check = _mapping(check)
        at = stamp(check.get('as_of'))
        if (sid in STRATEGY_NAMES and check.get('status') == 'passed'
                and check.get('observation_passed') is True
                and (not check.get('as_of') or (at is not None and at.date().isoformat() == signal))):
            support.append((sid, check))
    return support


def build_daily_model(report, historical=False):
    """Return ``review``, ``observed`` and ``started`` lists from one saved report.

    Every row is deduplicated by code with merged ``groups`` (id/label objects).
    ``r5``/``r20`` are percentage points, ``next_check`` is a list of strings,
    ``levels`` and ``sources`` are label/value and label/url lists respectively.
    Unknown evidence never becomes an observation. The argument is not mutated.
    """
    report = _mapping(report)
    signal = _day(report.get('signal_date'))
    model = {'signal_date': signal, 'as_of': _text(report.get('as_of')),
             'generated_at': _text(report.get('generated_at')), 'historical': bool(historical),
             'valid_until': stamp(report.get('valid_until')).isoformat() if stamp(report.get('valid_until')) else None,
             'status': _text(report.get('status') or 'unavailable'),
             'review': [], 'observed': [], 'started': [], 'counts': {},
             'missing': _texts(report.get('missing')), 'notes': [], 'actionable': False,
             'sources': _sources(report.get('sources')), 'forward': [],
             'coverage': {key: _number(_mapping(report.get('coverage')).get(key)) for key in
                          ('sectors_expected', 'membership_verified', 'sectors_complete', 'unique_stocks')}}
    if (report.get('status') == 'unavailable' or signal is None or stamp(report.get('as_of')) is None
            or stamp(report['as_of']).date().isoformat() != signal):
        model['status'] = 'unavailable'
        model['missing'].append('信号日与行情截止无法核对，不展示候选。')
        model['counts'] = {'review': 0, 'observed': 0, 'started': 0}
        return model
    candidates = {str(r.get('code')): r for r in _records(report.get('candidates'))}
    sectors = {str(s.get('id')): s for s in _records(report.get('sectors'))}
    if _mapping(report.get('coverage')).get('forward_ready') is True:
        for selected in (report.get('forward_top') or [])[:3]:
            sid = selected.get('id') or selected.get('sector_id') if isinstance(selected, dict) else selected
            sector = sectors.get(str(sid), {})
            if (not sector.get('forward_rank') or _mapping(sector.get('coverage')).get('complete') is not True
                    or _mapping(sector.get('coverage')).get('membership_verified') is not True):
                continue
            model['forward'].append({'id': _text(sid), 'label': _text(sector.get('label') or sid),
                                     'confirmation': _texts(sector.get('confirmation')),
                                     'risks': _texts(sector.get('risks')),
                                     'sources': _sources(sector.get('sources'))})
    observed, review, started = {}, {}, {}
    for sector in sectors.values():
        coverage = _mapping(sector.get('coverage'))
        if coverage.get('complete') is not True or coverage.get('membership_verified') is not True:
            continue
        for code in sector.get('top_codes') or []:
            radar = candidates.get(str(code), {})
            support = _supporting(radar, signal)
            if not support:
                continue
            item = _row(radar, radar, sector, 'observed', historical, signal)
            item['observation_origin'] = 'sector_top'
            for sid, check in support:
                item['supporting_skills'].append({'id': sid, 'label': STRATEGY_NAMES[sid]})
                item['reasons'] += [STRATEGY_NAMES[sid] + '：收盘观察条件通过']
                item['next_check'] += _texts(check.get('confirmation'))
                item['levels'] += _levels(check.get('levels'))
                item['sources'] += _sources(check.get('sources'))
                item['risks'] += _texts(check.get('risks'))
            _merge(observed, item)
    focus = _mapping(report.get('prelaunch_focus'))
    if focus.get('signal_date') == signal and focus.get('status') != 'unavailable':
        started_codes = {str(entry.get('code')) for group in _records(focus.get('sectors'))
                         for entry in _records(group.get('started')) if _entry_day_matches(entry, signal)}
        for focus_sector in _records(focus.get('sectors')):
            sector = sectors.get(str(focus_sector.get('id')), focus_sector)
            for entry in _records(focus_sector.get('entries')):
                code = str(entry.get('code'))
                radar = candidates.get(code, {})
                prelaunch = next((check for sid, check in _supporting(radar, signal) if sid == 'prelaunch'), None)
                coverage = _mapping(sector.get('coverage'))
                # Saved native core status is a separate original observation,
                # not a newly promoted sector top-three slot. Preserve it, but
                # never infer a core pass from an ordinary pending focus entry.
                if (_entry_day_matches(entry, signal) and entry.get('tier') == 'core'
                        and entry.get('native_core') is True and prelaunch is not None
                        and not entry.get('failure_reasons') and code not in started_codes
                        and coverage.get('complete') is True and coverage.get('membership_verified') is True):
                    core = _row(entry, radar, sector, 'observed', historical, signal)
                    core['observation_origin'] = 'prelaunch_core'
                    core['status'] = '潜伏核心 · 非板块前三排名'
                    core['supporting_skills'] = [{'id': 'prelaunch', 'label': STRATEGY_NAMES['prelaunch']}]
                    core['reasons'] += ['原生潜伏核心观察资格已核验；不是板块前三排名。']
                    core['next_check'] = _texts(entry.get('confirmation')) + _texts(prelaunch.get('confirmation'))
                    core['sources'] += _sources(prelaunch.get('sources'))
                    _merge(observed, core)
                if _entry_day_matches(entry, signal) and entry.get('tier') == 'pending' and str(entry.get('code')) not in observed:
                    _merge(review, _row(entry, candidates.get(str(entry.get('code')), {}), sector, 'review', historical, signal))
            for entry in _records(focus_sector.get('near_misses')):
                if _entry_day_matches(entry, signal) and str(entry.get('code')) not in observed:
                    _merge(review, _row(entry, candidates.get(str(entry.get('code')), {}), sector, 'review', historical, signal))
            for entry in _records(focus_sector.get('started')):
                if _entry_day_matches(entry, signal):
                    _merge(started, _row(entry, candidates.get(str(entry.get('code')), {}), sector, 'started', historical, signal))
        model['notes'].append('优先复核仅展示原报告已保存记录；历史报告可能已截断，不代表全部落选股。')
        if any(_number(s.get('omitted_count')) and s['omitted_count'] > 0 for s in _records(focus.get('sectors'))):
            model['notes'].append('原报告存在未保存的优先形态，不从全股票评分重建或补位。')
    else:
        model['notes'].append('该日没有可用的优先复核记录；不从全部股票或旧日期推造名单。')
    # A contradictory saved started flag is never shown as a latent prospect.
    for code in set(started) | set(observed):
        review.pop(code, None)
    model.update(review=list(review.values()), observed=list(observed.values()), started=list(started.values()))
    model['counts'] = {key: len(model[key]) for key in ('review', 'observed', 'started')}
    return model


def _entry_day_matches(entry, signal):
    if not entry.get('as_of'):
        return True  # Older saved focus entries inherit their dated container.
    source = stamp(entry['as_of'])
    return source is not None and source.date().isoformat() == signal


def _report_time(report, now):
    if report.get('module_id') != 'sector-radar' or report.get('status') not in ('complete', 'partial', 'empty'):
        return None
    signal, source = _day(report.get('signal_date')), stamp(report.get('as_of'))
    if signal is None or source is None or source > now or source.date().isoformat() != signal:
        return None
    if any(_day(bar.get('date')) and bar['date'] > signal
           for row in _records(report.get('candidates')) for bar in _records(row.get('bars'))):
        return None
    clocks = [source]
    for field in ('generated_at', 'published_at', 'run_started_at'):
        if report.get(field):
            clock = stamp(report[field])
            if clock is None or clock > now:
                return None
            clocks.append(clock)
    return max(clocks)


def _attempt_view(attempt, now):
    attempted = stamp(attempt.get('attempted_at') or attempt.get('generated_at'))
    source = stamp(attempt.get('as_of'))
    if attempted is None or attempted > now or (source is not None and source > now):
        return {}
    return {'status': _text(attempt.get('status')), 'attempted_at': attempted.isoformat(),
            'as_of': _text(attempt.get('as_of')), 'summary': _text(attempt.get('summary')),
            'missing': _texts(attempt.get('missing'))}


def load_daily_reports(now=None, limit=20):
    """Read private current/history snapshots into up to twenty daily models.

    Latest failed attempts are returned separately; they do not erase or
    rejuvenate the last valid report. Filesystem paths and raw source objects
    never appear in the result. Only reports already known at ``now`` qualify.
    """
    now = now or dt.datetime.now(TZ)
    now = now.replace(tzinfo=TZ) if now.tzinfo is None else now.astimezone(TZ)
    limit = min(20, max(0, int(limit)))
    # The site builder already owns update_lock. Reopening that flock here
    # would deadlock. These files are atomically replaced and historical files
    # are immutable. Standalone reads merge by verified report clocks and keep
    # a failure warning conservatively; this reader never publishes anything.
    loaded = load_module('sector-radar', now=now)
    folder = module_path('sector-radar').parent
    paths = list((folder / 'history').glob('*.json'))
    paths += [folder / 'latest_attempt.json']
    reports = [_mapping(loaded.get('data'))]
    for path in paths:
        if not path.is_symlink() and path.is_file():
            reports.append(_mapping(read_json(path, {})))
    attempt = _attempt_view(_mapping(loaded.get('attempt')), now)
    by_day = {}
    for report in reports:
        clock = _report_time(report, now)
        if clock is None:
            continue
        signal = report['signal_date']
        order = (clock, content_hash(report))
        if signal not in by_day or order > by_day[signal][0]:
            by_day[signal] = (order, report)
    selected = sorted(by_day, reverse=True)[:limit]
    newest = selected[0] if selected else None
    models = []
    for signal in selected:
        report = by_day[signal][1]
        expiry = stamp(report.get('valid_until'))
        historical = signal != newest or expiry is None or expiry < now or attempt.get('status') == 'unavailable'
        models.append(build_daily_model(report, historical=historical))
    state = models[0]['status'] if models else 'not_run'
    if models and models[0]['historical']:
        state = 'expired'
    if attempt.get('status') == 'unavailable':
        state = 'unavailable'
    return {'days': models, 'latest_attempt': attempt, 'state': state,
            'note': ('最近更新失败，以下保留原日期研究。' if attempt.get('status') == 'unavailable' else
                     '按真实信号日查看已保存列表；只作研究，不授予交易资格。'),
            'latest_signal_date': newest}
