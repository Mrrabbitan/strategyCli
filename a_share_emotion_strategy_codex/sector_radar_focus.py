"""Optional prelaunch review priority; never changes a native strategy or radar rank.

This consumes an already executed, same-session V3.4 report.  Unknown gates may
support a labelled evidence-review queue, never a passed observation assertion.
No feeds, publication, account data or persistence are used here.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import math
from collections import Counter
from pathlib import Path

from prelaunch_research import SOURCE_HASH, VERSION


REQUIRED = (
    '最近65个交易日日线完整', '行情及复权基准核验',
    'MA60趋势与五日斜率', '温和试盘', '试盘后至少两日缩量承接', '冻结平台与结构支撑',
)
TERMINAL = ('已启动', '失效', '观察到期', '排除')


def _number(value):
    return value if not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) else None


def _day(value):
    try:
        return dt.date.fromisoformat(str(value)[:10]).isoformat()
    except (TypeError, ValueError):
        return None


def _unique(values):
    return list(dict.fromkeys(str(v) for v in values if v))


def _ready_history(row, signal):
    bars = row.get('bars') or []
    days = [b.get('date') for b in bars if isinstance(b, dict)]
    return (len(days) >= 65 and len(days) == len(bars) and len(set(days)) == len(days)
            and all(_day(day) == day and day <= signal for day in days)
            and days == sorted(days) and days[-1] == signal)


def _entry(row, radar, signal):
    metrics = row.get('metrics') or {}
    levels = {x.get('label'): _number(x.get('value')) for x in row.get('levels') or [] if isinstance(x, dict)}
    conditions = row.get('conditions') or []
    gaps = [str(c.get('label')) + '：' + str(c.get('detail') or '待验证')
            for c in conditions if c.get('passed') is None]
    gaps += list(radar.get('security_reasons') or [])
    if not radar.get('announcement_verified'):
        gaps.append('证券公告原文及信息截止尚未完整复核')
    missing_cost = _number(metrics.get('净空间比')) is None
    if missing_cost:
        gaps.append('费用或最近压力证据不完整：毛空间不等于扣费空间，不授予潜伏核心资格')
    support, upper = levels.get('冻结支撑'), levels.get('冻结上沿')
    risks = [row.get('invalidation') or '跌破冻结支撑或出现公告硬风险时结束本轮观察；不下移支撑。']
    if support is not None:
        risks.append('收盘跌破冻结支撑 %.2f 元，结构失效。' % support)
    if upper is not None:
        risks.append('突破冻结上沿 %.2f 元或触发原加速条件后转已启动跟踪，不再列潜伏。' % upper)
    if row.get('valid_until'):
        risks.append('本轮冻结观察期限至 %s，不因重复研究顺延。' % row['valid_until'])
    return {'code': row['code'], 'name': row.get('name') or radar.get('name') or row['code'],
            'as_of': signal + 'T15:00:00+08:00', 'native_status': row.get('status'),
            'price': levels.get('参考收盘') or _number(row['bars'][-1].get('close')),
            'return_5d_pct': _number(metrics.get('5日涨幅%')),
            'return_20d_pct': _number(metrics.get('20日涨幅%')),
            'probe_date': metrics.get('试盘日期'), 'volume_ratio': _number(metrics.get('中位量比')),
            'probe_evidence': next((c.get('detail') for c in conditions if c.get('label') == '温和试盘'), None),
            'support': support, 'upper': upper, 'pressure': levels.get('最近压力'),
            'gross_rr': _number(metrics.get('毛空间比')), 'net_rr': _number(metrics.get('净空间比')),
            'frozen_id': row.get('frozen_id'), 'valid_until': row.get('valid_until'),
            'missing': _unique(gaps), 'risks': _unique(risks),
            'confirmation': row.get('confirmation') or '补齐全部原生资格后再复核，不将形态观察视为买点。',
            'sources': list(row.get('sources') or []), 'actionable': False}


def _space_counter(entry):
    # The frozen upper edge is known pressure. Nonnegative fees or a closer
    # resistance can only lower this bound on the original net RR>=2 gate.
    price, support, upper = entry['price'], entry['support'], entry['upper']
    if price is not None and support is not None and upper is not None and 0 < support < price <= upper:
        bound = (upper - price) / (price - support)
        entry['gross_rr_upper_bound'] = bound
        if bound < 2 - 1e-12:
            return ('已知冻结上沿下的毛空间上限仅 %.3f，已小于原规则净空间≥2；更近压力及非负费用只会缩小空间' % bound)
    return None


def build_prelaunch_focus(report, native_report):
    """Return a read-only priority layer for ``report['prelaunch_focus']``.

    Original ``top_codes``, per-sector scores and five-strategy cells are never
    mutated.  Each sector gets at most three review slots; complete native core
    observations precede pending morphology. Missing complete-sector ranking
    forces code ordering for the whole tier, never partial-score competition.
    """
    signal = report.get('signal_date')
    out = {'version': '1.0', 'signal_date': signal, 'as_of': report.get('as_of'),
           'rule_version': VERSION, 'rule_hash': SOURCE_HASH,
           'review_logic_hash': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
           'native_input_fingerprint': native_report.get('input_fingerprint'),
           'status': 'unavailable', 'sectors': [], 'missing': [], 'actionable': False,
           'ordering_note': '先看原生核心，再看待验证形态；原Skill门槛、正式前三及五策略判断均不变。'}
    if (not signal or _day(report.get('as_of')) != signal
            or native_report.get('signal_date') != signal or _day(native_report.get('as_of')) != signal
            or (native_report.get('rules') or {}).get('source_hash') != SOURCE_HASH
            or native_report.get('status') == 'unavailable'):
        out['missing'] = ['未取得同信号日、固定V3.4版本的有效原生报告；不把旧低位名单改标为当前。']
        return out
    radar_rows = {str(r.get('code')): r for r in report.get('candidates') or []}
    # Original invalid history can append an older record with a code also in
    # this run's watch/core group. It must not overwrite a same-session row.
    rows = {str(r.get('code')): r for group in ('invalid', 'started', 'watch', 'core')
            for r in native_report.get(group) or []
            if r.get('bars') and r['bars'][-1].get('date') == signal}
    focus, started, rejected, near_misses = {}, {}, {}, {}
    for code, radar in radar_rows.items():
        if code not in rows and radar.get('state') != 'excluded':
            rejected[code] = ['同日原生计算记录或必要日线缺失；旧失败记录不补位']
    for code, row in rows.items():
        radar = radar_rows.get(code)
        if not radar or radar.get('state') == 'excluded':
            continue
        if not _ready_history(row, signal):
            rejected[code] = ['同日65根完整原生日线未取得']
            continue
        conditions = row.get('conditions') or []
        values = {c.get('label'): c.get('passed') for c in conditions}
        if row.get('status') == '已启动':
            started[code] = _entry(row, radar, signal)
            continue
        failures = [c.get('label') for c in conditions if c.get('passed') is False]
        if any('组合上限' in str(reason) for reason in row.get('reasons') or []):
            failures.append('原生行业/催化/Top10组合上限')
        counterexample_ready = all(values.get(label) is True for label in REQUIRED
                                   if label != '试盘后至少两日缩量承接')
        if row.get('status') in TERMINAL or failures:
            rejected[code] = failures or [row.get('status')]
            if counterexample_ready and row.get('status') not in TERMINAL:
                entry = _entry(row, radar, signal)
                counter = _space_counter(entry)
                near_misses[code] = dict(entry, failure_reasons=rejected[code] + ([counter] if counter else []))
                if counter:
                    rejected[code] = rejected[code] + ['已知压力下的空间上限不足']
            continue
        missing_numeric = [label for label in REQUIRED if values.get(label) is not True]
        if missing_numeric:
            rejected[code] = ['形态必需证据未确认：' + '、'.join(missing_numeric)]
            continue
        entry = _entry(row, radar, signal)
        counter = _space_counter(entry)
        if counter:
            rejected[code] = ['已知压力下的空间上限不足']
            near_misses[code] = dict(entry, failure_reasons=[counter])
            continue
        check = (radar.get('strategy_checks') or {}).get('prelaunch') or {}
        entry['native_core'] = (row.get('eligible') is True and all(c.get('passed') is True for c in conditions)
                                and check.get('status') == 'passed' and check.get('observation_passed') is True
                                and _day(check.get('as_of')) == signal and check.get('rule_hash') == SOURCE_HASH)
        focus[code] = entry
    for sector in report.get('sectors') or []:
        sid = sector['id']
        members = {str(c) for c in sector.get('member_codes') or []}
        coverage = sector.get('coverage') or {}
        complete = coverage.get('complete') is True and coverage.get('membership_verified') is True
        entries = []
        for code in members & set(focus):
            entry = dict(focus[code])
            radar = radar_rows[code]
            entry['tier'] = 'core' if entry['native_core'] and complete and radar.get('state') == 'watch' and radar.get('announcement_verified') else 'pending'
            entry['missing'] = _unique(entry['missing'] + list(sector.get('missing') or []))
            if not complete:
                entry['missing'] = _unique(entry['missing'] + ['信号日成分/全板块比较未完整核验；仅作已取得范围的优先补证，不是正式前三'])
            entry['score'] = _number(((radar.get('ranks') or {}).get(sid) or {}).get('score'))
            entry['median_amount_5d'] = _number((radar.get('metrics') or {}).get('median_amount_5d'))
            entries.append(entry)
        rank_available = complete and bool(entries) and all(e['score'] is not None and e['median_amount_5d'] is not None for e in entries)
        entries.sort(key=lambda r: (0 if r['tier'] == 'core' else 1,
                                   -r['score'] if rank_available else 0,
                                   -r['median_amount_5d'] if rank_available else 0, r['code']))
        selected = entries[:3]
        out['sectors'].append({'id': sid, 'label': sector.get('label'), 'entries': selected,
            'available_count': len(entries), 'omitted_count': max(0, len(entries) - 3),
            'ordering': '沿用原板块四项量价观察分，同分依成交额和代码' if rank_available else '证据不全，按代码顺序展示；不是完整板块排名或上涨概率排序',
            'started': [started[c] for c in sorted(members & set(started))],
            'rejected_count': len(members & set(rejected)),
            'rejection_reasons': [{'reason': reason, 'count': count} for reason, count in
                                  sorted(Counter(r for c in members & set(rejected) for r in rejected[c]).items(),
                                         key=lambda value: (-value[1], value[0]))],
            'near_misses': [near_misses[c] for c in sorted(members & set(near_misses))[:3]],
            'missing': list(sector.get('missing') or [])})
    out['coverage'] = {'review_unique': len(focus), 'started_unique': len(started),
                       'excluded_from_priority': len(rejected),
                       'core_slots': sum(e['tier'] == 'core' for s in out['sectors'] for e in s['entries']),
                       'pending_slots': sum(e['tier'] == 'pending' for s in out['sectors'] for e in s['entries'])}
    out['status'] = 'partial' if (native_report.get('status') == 'partial' or native_report.get('missing')
                                or any(s['missing'] or any(e['missing'] for e in s['entries']) for s in out['sectors'])) else 'complete'
    out['missing'] = list(native_report.get('missing') or [])
    return out
