"""Adapt the installed Yichujifa skill's evidence; never invent execution gates."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from market_calendar import is_trading_day, previous_trading_day

TZ = ZoneInfo('Asia/Shanghai')
BRANCHES = {'one_to_two': '一进二', 'two_to_three': '二进三', 'hold_breakout': '首板后守位'}
SKILL = Path.home() / '.codex/skills/a-share-yichujifa'


def _runtime():
    return Path(os.environ.get('AUTOSTRATEGY_YICHUJIFA_RUNTIME',
                               str(Path.home() / 'Library/Application Support/Yichujifa'))).expanduser()


def _stamp(value):
    try:
        x = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value))
        return x.replace(tzinfo=TZ) if x.tzinfo is None else x.astimezone(TZ)
    except (TypeError, ValueError):
        return None


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _clean(value):
    """Local evidence locations must not become page text, including failures."""
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(x) for x in value]
    if isinstance(value, str):
        if value.startswith(('file://', '/Users/', '/private/', '/var/', '/tmp/')):
            return '本地私有证据'
        return re.sub(r'(?:/Users/|/private/|/var/|/tmp/)[^\n，；]*', '本地私有证据', value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _sources(items):
    output, seen = [], set()
    for item in items or []:
        url = item if isinstance(item, str) else item.get('url', '') if isinstance(item, dict) else ''
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password or url in seen:
            continue
        output.append({'label': item.get('label', '原始行情或公告来源') if isinstance(item, dict) else '公告原文', 'url': url})
        seen.add(url)
    return output


def _read_private(path):
    path = Path(path)
    root = _runtime().resolve()
    resolved = path.resolve()
    resolved.relative_to(root)
    if path.is_symlink() or resolved.suffix != '.json' or resolved.stat().st_size > 50_000_000:
        raise ValueError('私有研究输入不符合读取范围')
    result = json.loads(resolved.read_text(encoding='utf-8'))
    return result


def _rules(report):
    files = ('SKILL.md', 'references/rules.md', 'references/data-and-run.md', 'scripts/engine.py')
    try:
        digest = hashlib.sha256()
        for name in files:
            digest.update(name.encode()); digest.update((SKILL / name).read_bytes())
        return {'version': report.get('version') or '本机技能原文', 'source_hash': 'sha256:' + digest.hexdigest()}
    except OSError:
        return {'version': report.get('version') or '未取得本机规则版本', 'source_hash': None}


def _session(now):
    ok, reason = is_trading_day(now.date())
    t = now.time().replace(tzinfo=None)
    return ok and (dt.time(9, 50) <= t <= dt.time(11, 30) or dt.time(13) <= t <= dt.time(15))


def _next_close(day):
    for delta in range(1, 21):
        following = day + dt.timedelta(days=delta)
        good, reason = is_trading_day(following)
        if good:
            return dt.datetime.combine(following, dt.time(15), TZ)
        if '缺少' in reason or '不可用' in reason:
            break
    return None


def _unavailable(now, phase, reason):
    return {'schema_version': 1, 'module_id': 'yichujifa', 'as_of': None,
            'generated_at': now.isoformat(), 'valid_until': None, 'phase': phase,
            'status': 'unavailable', 'summary': '一触即发本次研究不可用，未形成成功空表。',
            'coverage': {}, 'missing': [_clean(reason)], 'sources': [], 'rules': _rules({}), 'candidates': []}


def _condition(label, passed, detail=''):
    return {'label': label, 'passed': passed if isinstance(passed, bool) else None, 'detail': _clean(str(detail))}


def _live_context(live, snapshots, report, now):
    """Require the native live evidence; a label alone cannot grant eligibility."""
    reasons = []
    if not isinstance(live, dict):
        return False, ['尚未取得盘中复核'], None
    if live.get('mode') != 'live' or live.get('replay_only'):
        reasons.append('历史重放或非实时报告不能作为当前参与条件')
    timing = _stamp(live.get('time'))
    if not timing or timing.date() != now.date() or not 0 <= (now - timing).total_seconds() <= 90:
        reasons.append('盘中复核已过期或时间无效')
    if not _session(now):
        reasons.append('当前不是9:50之后的有效盘中窗口')
    try:
        if report.get('as_of') != previous_trading_day(now.date()).isoformat():
            reasons.append('收盘观察报告不是上一交易日')
    except RuntimeError:
        reasons.append('上一交易日无法核验')
    if not isinstance(snapshots, list) or len(snapshots) < 3:
        reasons.append('缺少事件前基准与两次新行情证据')
    else:
        stamps = [_stamp(x.get('time')) if isinstance(x, dict) else None for x in snapshots[-3:]]
        if not all(stamps) or not (stamps[0] < stamps[1] < stamps[2] <= now and
                60 <= (stamps[2] - stamps[1]).total_seconds() <= 180 and
                0 <= (now - stamps[2]).total_seconds() <= 90 and
                all(s.date() == now.date() and _session(s) for s in stamps)):
            reasons.append('三次盘中采样时间不连续、非交易时段或已经过期')
    return not reasons, reasons, timing


def _row_times_valid(code, snapshots, now):
    stamps = []
    for frame in snapshots[-3:]:
        quote = (frame.get('quotes') or {}).get(code) or {}
        source_time = _stamp(quote.get('time'))
        frame_time = _stamp(frame.get('time'))
        if (not source_time or not frame_time or source_time.date() != now.date() or
                not 0 <= (frame_time - source_time).total_seconds() <= 90 or quote.get('verified') is not True):
            return False
        stamps.append(source_time)
    return (stamps[0] < stamps[1] < stamps[2] and (stamps[2] - stamps[1]).total_seconds() >= 60 and
            all((frame.get('quotes') or {}).get(code, {}).get('tradable') is True and
                (frame.get('quotes') or {}).get(code, {}).get('normal_limit_rule') is True for frame in snapshots[-2:]))


def _live_end(source_time):
    session_end = dt.datetime.combine(source_time.date(), dt.time(11, 30) if source_time.hour < 12 else dt.time(15), TZ)
    return min(source_time + dt.timedelta(seconds=90), session_end)


def _import_native(report, evidence=None, live=None, snapshots=None, as_of=None):
    """Import explicit native artifacts. No filenames or implicit live pointer lookup."""
    now = _stamp(as_of) or dt.datetime.now(TZ)
    if not isinstance(report, dict) or report.get('schema_version') != 1 or not isinstance(report.get('candidates'), list):
        return _unavailable(now, 'prepare', '收盘报告缺失、损坏或版本不兼容')
    report = _clean(report)
    source = _stamp(str(report.get('as_of', '')) + 'T15:00:00+08:00')
    generated = _stamp(report.get('generated_at'))
    if not source or source > now or generated and generated > now:
        return _unavailable(now, 'prepare', '收盘报告包含未来时点或无效行情日期')
    if not is_trading_day(source.date())[0]:
        return _unavailable(now, 'prepare', '报告行情日期不是已核验交易日')
    if evidence and evidence.get('as_of') != report.get('as_of'):
        return _unavailable(now, 'prepare', '原始证据与收盘报告的行情截止日期冲突')
    if report.get('mode') != 'close_watchlist':
        return _unavailable(now, 'prepare', '需要原始收盘观察报告；盘中或重放文件应单独导入')
    missing = list(report.get('errors') or [])
    original_coverage = report.get('coverage') or {}
    pools = original_coverage.get('pool_days') or {}
    if not pools:
        missing.append('历史涨停池覆盖未提供')
    for day, row in pools.items():
        if row.get('metadata_verified') is not True:
            missing.append(day + '：' + str(row.get('error') or '涨停池日期与完整性待验证'))
    requested, verified = original_coverage.get('requested_stocks'), original_coverage.get('dual_history_verified')
    if not isinstance(requested, int) or not isinstance(verified, int) or verified < requested:
        missing.append('历史日线双源核验覆盖不足')
    phase_evidence = report.get('phase') or {}
    if phase_evidence.get('verified') is not True:
        missing.append('最近多板阶段未核验')
    for sector in report.get('sectors') or []:
        if sector.get('verified') is not True:
            missing.append(str(sector.get('name', '行业')) + '：退潮与行业证据不足')
    validity = _next_close(source.date())
    if validity is None:
        missing.append('下一有效观察交易日无法核验')
    elif now > validity:
        missing.append('收盘观察窗口已过期，须重新运行技能')
    live_valid, live_missing, live_time = _live_context(live, snapshots, report, now)
    live_rows = {(r.get('branch'), r.get('code')): r for r in (live or {}).get('results', []) if isinstance(r, dict)}
    global_live_reasons = (live or {}).get('reasons') or []
    if live:
        missing.extend(live_missing)
        missing.extend(global_live_reasons)
    candidates, seen = [], set()
    for row in report['candidates']:
        if not isinstance(row, dict) or row.get('branch') not in BRANCHES or not re.fullmatch(r'\d{6}', str(row.get('code', ''))):
            missing.append('存在无法识别分支或代码的原始记录')
            continue
        key = (row['branch'], row['code'])
        if key in seen:
            missing.append('同一分支出现重复记录，未重复计入候选')
            continue
        seen.add(key)
        reasons = list(row.get('reasons') or [])
        rank = row.get('rank')
        top_three = isinstance(rank, int) and not isinstance(rank, bool) and 1 <= rank <= 3
        prequalified = row.get('prequalified') is True
        selected_live = live_rows.get(key) or {}
        reported_met = selected_live.get('status') == '条件已满足'
        eligible = bool(live_valid and reported_met and prequalified and top_three and not reasons and not global_live_reasons and
                        not selected_live.get('reasons') and row.get('status') == 'watch' and
                        row.get('rank_verified') is True and _row_times_valid(row['code'], snapshots, now))
        row_source = _stamp(((snapshots[-1].get('quotes') or {}).get(row['code']) or {}).get('time')) if eligible else None
        row_expiry = _live_end(row_source) if row_source else None
        if row_expiry and now > row_expiry:
            eligible = False
        if live:
            reasons += selected_live.get('reasons') or global_live_reasons
            if reported_met and not eligible:
                reasons += live_missing or ['盘中原始证据未通过当前时效或资格核验']
        five = row.get('five_day') or {}
        shape = row.get('shape') or {}
        announcement = row.get('announcement') or {}
        cond = [_condition('收盘预资格', prequalified, '；'.join(row.get('reasons') or []) or '收盘条件通过，仍须盘中确认'),
                _condition('行业原始前三', top_three if rank is not None else None, '原排名 ' + str(rank or '待验证') + '，执行淘汰后不补位'),
                _condition('排名覆盖核验', row.get('rank_verified'), '保留本行业本分支原始比较池'),
                _condition('五日量价完整', five.get('verified'), '最近五个完整交易日，不用盘中日线替代'),
                _condition('收盘公告原文核验', True if announcement.get('sources') and announcement.get('reviewed_through') else None,
                           announcement.get('reviewed_through') or '公告原文尚未核验'),
                _condition('当前盘中触发', True if eligible else False if live_valid and selected_live else None,
                           '；'.join(selected_live.get('reasons') or global_live_reasons or live_missing) or '原生实时复核条件通过')]
        levels = [{'label': label, 'value': _number(shape.get(field))} for field, label in (
            ('anchor_low', '首板低点 L / 硬失效线'), ('anchor_mid', '首板实体中点 M / 降级线'), ('anchor_high', '首板高点 H / 突破线'))] if shape else []
        bars = [{'date': x.get('date'), 'open': _number(x.get('open')), 'high': _number(x.get('high')),
                 'low': _number(x.get('low')), 'close': _number(x.get('close')), 'volume_shares': _number(x.get('volume')),
                 'amount_cny': _number(x.get('amount'))} for x in five.get('rows', []) if isinstance(x, dict) and str(x.get('date', '9999')) <= report['as_of']]
        status = ('条件已满足' if eligible else '历史条件已满足，待复核' if reported_met else
                  {'rejected': '不做', 'tracking': '已启动跟踪', 'downgraded': '降级观察'}.get(row.get('status'),
                  '可观察' if prequalified else '待验证观察'))
        candidates.append({'key': row['branch'] + ':' + row['code'], 'code': row['code'], 'name': row.get('name', row['code']),
            'group': BRANCHES[row['branch']], 'branch': row['branch'], 'status': status, 'eligible': eligible,
            'prequalified': prequalified, 'rank': rank, 'sector': row.get('sector'), 'conditions': cond, 'bars': bars,
            'live_as_of': row_source.isoformat() if row_source else None,
            'live_valid_until': row_expiry.isoformat() if row_expiry else None,
            'levels': levels, 'metrics': {'行业原排名': rank, '板内评分': _number(row.get('score')), '五日涨跌幅 %': _number(five.get('return_pct')),
                '五日均价': _number(five.get('ma5')), '五日趋势': five.get('trend'), '当前阶段': row.get('stage'),
                '流通市值 / 元': _number(row.get('float_cap_cny')), '最近收盘价': _number(row.get('last_close')),
                '首板日期': shape.get('anchor_date'), '首板后完整天数': shape.get('post_days'),
                '整理量 / 首板量': _number(shape.get('median_volume_ratio'))},
            'reasons': list(dict.fromkeys(reasons)), 'sources': _sources((row.get('sources') or []) + (announcement.get('sources') or [])),
            'native_shape': shape, 'announcement': announcement, 'live_result': _clean(selected_live)})
    unknown_reasons = ('待验证', '未核验', '不完整', '不全', '缺失', '未复核')
    missing.extend(reason for row in candidates for reason in row['reasons'] if any(word in reason for word in unknown_reasons))
    missing = list(dict.fromkeys(_clean(str(x)) for x in missing))
    eligible_count = sum(row['eligible'] for row in candidates)
    prequalified_count = sum(row['prequalified'] for row in candidates)
    live_sources = [_stamp(row['live_as_of']) for row in candidates if row.get('eligible')]
    current_live_source = min(live_sources) if live_sources else None
    current_source = current_live_source or source
    coverage = dict(original_coverage, candidate_records=len(candidates), prequalified_count=prequalified_count,
                    eligible_count=eligible_count, branch_counts={name: sum(row['branch'] == name for row in candidates) for name in BRANCHES})
    phase = 'replay' if live and (live.get('replay_only') or live.get('mode') == 'replay') else 'intraday' if live else 'prepare'
    market_checks = [
        _condition('最近多板883410阶段证据', phase_evidence.get('verified'),
                   f"收盘Day1：{phase_evidence.get('day1') or '未确认'}；收盘阶段 Day{phase_evidence.get('day_number') or '未知'}。日期推进本身不是修复确认。"),
        _condition('收盘观察窗口有效', now <= validity if validity else None,
                   '按真实交易日历确定下一观察日，过期报告不能取得当前资格'),
        _condition('9:50后盘中窗口', _session(now), '盘前、午休、休市不生成当前参与判断'),
        _condition('市场修复与新触发证据', True if eligible_count else None,
                   '；'.join(global_live_reasons or live_missing) or '查看原生盘中逐股结果；未通过者保留具体原因')]
    flag_labels = {'trend_weak': '行业趋势转弱', 'limit_contraction': '涨停数量收缩', 'core_losses': '固定核心股负反馈'}
    sector_reviews = []
    for row in report.get('sectors') or []:
        descriptions = [flag_labels.get(k, k) + ('：成立' if v is True else '：未成立' if v is False else '：待验证')
                        for k, v in (row.get('flags') or {}).items()]
        descriptions.append('五日涨停样本数：' + ' → '.join(str(v) if v is not None else '未知' for v in row.get('counts') or []))
        sector_reviews.append({'name': row.get('name', '行业待验证'), 'status': row.get('status', '数据不足'),
                               'reasons': descriptions, 'recovery': [row['reopen_condition']] if row.get('reopen_condition') else ['恢复条件尚未提供']})
    return {'schema_version': 1, 'module_id': 'yichujifa', 'as_of': current_source.isoformat(),
            'generated_at': now.isoformat(), 'valid_until': validity.isoformat() if validity else None, 'phase': phase,
            'live_as_of': current_live_source.isoformat() if current_live_source else None,
            'live_valid_until': _live_end(current_live_source).isoformat() if current_live_source else None,
            'status': 'partial' if missing else 'complete' if candidates else 'empty',
            'summary': f'三分支共 {len(candidates)} 条观察记录，收盘预资格 {prequalified_count} 条，当前盘中条件通过 {eligible_count} 条；观察记录不是可建仓数量。',
            'coverage': coverage, 'missing': missing, 'sources': _sources(report.get('sources')),
            'market_checks': market_checks, 'sector_reviews': sector_reviews,
            'rules': _rules(report), 'candidates': candidates, 'phase_evidence': phase_evidence,
            'sector_evidence': report.get('sectors') or [], 'native_report': report, 'live_review': _clean(live),
            'snapshots': _clean(snapshots), 'evidence_as_of': (evidence or {}).get('as_of'),
            'scope': report.get('scope'), 'automatic_order': False}


def import_native(report, evidence=None, live=None, snapshots=None, as_of=None):
    """Fail closed on malformed optional fields instead of publishing an empty success."""
    try:
        return _import_native(report, evidence, live, snapshots, as_of)
    except (AttributeError, ValueError, TypeError, KeyError, OSError):
        return _unavailable(_stamp(as_of) or dt.datetime.now(TZ), 'prepare', '原始研究数据结构、时点或可选盘中证据无法核验')


def research(as_of: dt.datetime, phase: str = 'prepare', input_data: dict | None = None,
             live_data: dict | None = None) -> dict:
    """Run the installed skill, or adapt supplied dictionaries without network I/O."""
    now = _stamp(as_of) or dt.datetime.now(TZ)
    if input_data is not None:
        if not isinstance(input_data, dict):
            return _unavailable(now, phase, '研究输入必须为已解析的原生数据，不能接受任意文件名')
        report = input_data.get('report', input_data)
        result = import_native(report, input_data.get('evidence'), (live_data or {}).get('live', live_data),
                               (live_data or {}).get('snapshots', input_data.get('snapshots')), now)
        if not live_data:
            result['phase'] = phase
        return result
    actual = dt.datetime.now(TZ)
    if abs((actual - now).total_seconds()) > 300:
        return _unavailable(now, phase, '历史研究必须提供截止时已保存的证据，不能用当前接口补造历史')
    runner = SKILL / 'scripts/run.py'
    if not runner.is_file():
        return _unavailable(now, phase, '本机一触即发技能不可用；未改用替代选股规则')
    out = _runtime() / 'runs' / (actual.strftime('%Y-%m-%d-%H%M%S-%f') + '-workbench')
    try:
        run = subprocess.run([sys.executable, str(runner), 'scan', '--output', str(out)], cwd=str(SKILL),
                             capture_output=True, text=True, timeout=240)
        if run.returncode:
            return _unavailable(actual, phase, '原生收盘筛选执行失败，未生成有效新报告')
        report, evidence = _read_private(out / 'report.json'), _read_private(out / 'evidence.json')
        live, frames = None, None
        if phase in ('intraday', 'live') and _session(actual):
            args = [sys.executable, str(runner), 'live', '--input', str(out / 'report.json'), '--output', str(out)]
            reviews = _runtime() / 'current-reviews.json'
            if reviews.is_file():
                _read_private(reviews)
                args += ['--reviews', str(reviews)]
            run = subprocess.run(args, cwd=str(SKILL), capture_output=True, text=True, timeout=360)
            if run.returncode:
                result = import_native(report, evidence, as_of=dt.datetime.now(TZ))
                result['status'] = 'partial'; result['missing'].append('本次原生盘中复核失败，收盘观察不能代表当前买点')
                return result
            live, frames = _read_private(out / 'live-review.json'), _read_private(out / 'snapshots.json')
        result = import_native(report, evidence, live, frames, dt.datetime.now(TZ))
        if not live:
            result['phase'] = phase
        return result
    except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired):
        return _unavailable(dt.datetime.now(TZ), phase, '原生技能执行、私有证据读取或数据核验失败')
