"""Six independent, dated information tables. No trading/selection skill is run."""
from __future__ import annotations

from collections import Counter, defaultdict
import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
VERSION = '1.0.0'
SECTIONS = ('limit_up', 'announcements', 'earnings', 'repurchases', 'broker_seats', 'institutions')
DISCLOSURES = ('announcements', 'earnings', 'repurchases')


def stamp(value):
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=TZ) if value.tzinfo is None else value.astimezone(TZ)
    try:
        value = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return stamp(value)
    except (ValueError, TypeError):
        return None


def number(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (ValueError, TypeError, InvalidOperation):
        return None


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def mainboard(row):
    code, name = str(row.get('code', '')), str(row.get('name') or '').strip()
    return bool(re.fullmatch(r'(?:600|601|603|605|000|001|002|003)\d{3}', code)
                and name and not name.isdigit() and not re.search(r'ST|退', name.upper())
                and row.get('security_status') not in ('unknown', 'delisting', 'non_stock'))


def row_version(row):
    return digest({k: v for k, v in row.items() if k not in
                   ('retrieved_at', 'source_time', 'late_arrival', 'version', 'change')})


def event_key(row):
    return str(row.get('event_id') or row.get('source_url') or
               digest([row.get('code'), row.get('title'), row.get('period_label'), row.get('progress')]))


def merge_seats(seats):
    """Cross-list copies occur once; equal anonymous seats inside one list survive."""
    groups = defaultdict(Counter)
    templates = {}
    for row in seats:
        buy, sell, net = (number(row.get(k)) for k in ('buy_cny', 'sell_cny', 'net_cny'))
        if None in (buy, sell, net) or buy < 0 or sell < 0 or abs(buy-sell-net) > Decimal('.02'):
            raise ValueError('席位买卖净额不一致')
        key = (str(row.get('seat_id', '')), str(row.get('name', '')), str(buy), str(sell), str(net))
        groups[row.get('side', 'single')][key] += 1
        templates[key] = dict(row, buy_cny=float(buy), sell_cny=float(sell), net_cny=float(net))
    output = []
    for key in sorted(templates):
        for _ in range(max(counter[key] for counter in groups.values())):
            output.append({k: v for k, v in templates[key].items() if k != 'side'})
    return output


def _period(row):
    period = row.get('period')
    if period not in ('single', 'multi'):
        raise ValueError('缺少单日或累计期间')
    end, start = row.get('period_end') or row.get('source_date'), row.get('period_start')
    if not start and period == 'single': start = end
    if not end or (start and start > end) or (period == 'single' and start != end):
        raise ValueError('龙虎榜期间冲突')
    return row['code'], period, start or ('unknown:'+str(row.get('event_id') or row.get('reason'))), end


def broker_rows(rows):
    events = defaultdict(list)
    rejected = []
    for row in rows:
        try:
            if row.get('seats_complete') is False:
                raise ValueError('买卖双榜不完整，不生成合计')
            seats = merge_seats(row.get('seats') or [])
            if not seats: raise ValueError('席位明细缺失')
            signature = digest(sorted([[(s.get('seat_id') or ''), s['name'], s['buy_cny'],
                                       s['sell_cny'], s['net_cny']] for s in seats], key=str))
            events[_period(row)].append((signature, row, seats))
        except (ValueError, KeyError) as exc:
            rejected.append({'code': row.get('code'), 'reason': str(exc)})
    output = []
    for _, group in events.items():
        if len({x[0] for x in group}) != 1:
            rejected.append({'code': group[0][1]['code'], 'reason': '同期间多原因席位清单冲突，未相加'})
            continue
        _, row, seats = group[0]
        regular = [s for s in seats if not re.search(r'机构专用|沪股通|深股通', s['name'])]
        regular.sort(key=lambda s: (-abs(s['net_cny']), s['name']))
        output.append(dict(row, seats=seats, top_seats=regular[:3],
                           reasons=sorted({x[1].get('reason', '') for x in group}),
                           net_cny=float(sum((number(s['net_cny']) for s in regular), Decimal(0))),
                           institution_net_cny=float(sum((number(s['net_cny']) for s in seats
                                                        if '机构专用' in s['name']), Decimal(0)))))
    return output, rejected


def institution_rows(rows):
    groups = defaultdict(list)
    rejected = []
    for row in rows:
        try: groups[_period(row)].append(row)
        except (ValueError, KeyError) as exc: rejected.append({'code': row.get('code'), 'reason': str(exc)})
    result = []
    for group in groups.values():
        nets = {number(r.get('net_cny')) for r in group}
        totals = {(number(r.get('buy_cny')), number(r.get('sell_cny')),
                   number(r.get('net_cny'))) for r in group}
        if len(nets) != 1 or None in nets or len(totals) != 1:
            rejected.append({'code': group[0]['code'], 'reason': '同期间机构统计冲突'})
            continue
        row = dict(group[0], reasons=sorted({r.get('reason', '') for r in group}))
        buy, sell = number(row.get('buy_cny')), number(row.get('sell_cny'))
        if buy is not None and sell is not None and abs(buy-sell-number(row['net_cny'])) > Decimal('.02'):
            rejected.append({'code': row['code'], 'reason': '机构买卖净额不一致'})
            continue
        result.append(row)
    return result, rejected


def _order(section, rows):
    code = lambda r: str(r.get('code', ''))
    num = lambda r, k: float(number(r.get(k)) or 0)
    if section == 'limit_up': return sorted(rows, key=lambda r: (-int(r.get('boards', 0)), code(r)))
    if section == 'earnings': return sorted(rows, key=lambda r: (-num(r, 'profit_low_cny'), code(r)))
    if section in ('broker_seats', 'institutions'):
        return sorted(rows, key=lambda r: (-(abs(num(r,'net_cny')) if section == 'broker_seats' else num(r,'net_cny')), code(r)))
    # Stable sorting keeps equal-priority, equal-time rows deterministic.
    rows = sorted(rows, key=code)
    rows.sort(key=lambda r: str(r.get('source_time') or ''), reverse=True)
    rows.sort(key=lambda r: (0 if r.get('stance') == 'risk' else 1, num(r,'priority') if number(r.get('priority')) is not None else 50))
    return rows


def normalize(evidence, cutoff, *, window_start=None, seen=None, executed_at=None):
    cutoff = stamp(cutoff)
    actual = stamp(executed_at) or dt.datetime.now(TZ)
    if cutoff is None or cutoff > actual: raise ValueError('截止时点无效或在未来')
    if evidence.get('schema_version') != 1: raise ValueError('不支持的证据版本')
    if evidence.get('module_id') == 'night-radar': raise ValueError('须导入原生证据，不能信任已计算榜单')
    window = stamp(window_start) or cutoff-dt.timedelta(days=1)
    trade_date = evidence.get('trade_date')
    from market_calendar import is_trading_day
    trading, calendar_note = is_trading_day(cutoff.date())
    calendar_ok = calendar_note in ('交易日历通过', '周末或交易所公告休市日')
    if trade_date:
        day = dt.date.fromisoformat(trade_date)
        if day > cutoff.date() or not is_trading_day(day)[0]: trade_date = None
        elif day == cutoff.date() and cutoff.time() < dt.time(15): trade_date = None
    sections, pending_seen = {}, {}
    for key in SECTIONS:
        source = evidence.get('sections', {}).get(key, {})
        missing = list(source.get('missing') or [])
        rejected, rows = [], []
        status = source.get('status', 'unavailable')
        if status not in ('complete', 'partial', 'unavailable', 'empty'): status = 'unavailable'
        if status == 'unavailable': missing = missing or ['本栏未取得可核验数据']
        if key not in DISCLOSURES and (not calendar_ok or not trade_date):
            status = 'unavailable'; missing.append('交易日或行情日期未核验')
        raw_rows = source.get('rows', [])
        if not isinstance(raw_rows,list):
            raw_rows=[]; status='unavailable'; missing.append('本栏记录结构不可用')
        duplicates, conflicts = {}, set()
        if key in DISCLOSURES:
            for original in raw_rows:
                if not isinstance(original,dict): continue
                identity = event_key(original)
                if identity in duplicates and row_version(duplicates[identity]) != row_version(original):
                    conflicts.add(identity)
                duplicates[identity] = original
            if conflicts: missing.append('同编号披露内容冲突，已隔离')
            raw_rows = [v for k,v in duplicates.items() if k not in conflicts]
        for original in raw_rows if status != 'unavailable' else []:
            if not isinstance(original,dict):
                missing.append('部分记录格式无效'); continue
            row = dict(original)
            if not mainboard(row):
                rejected.append({'code':row.get('code'), 'reason':'不属于已识别的非ST沪深主板范围'})
                if (re.fullmatch(r'(?:600|601|603|605|000|001|002|003)\d{3}',str(row.get('code','')))
                        and (not row.get('name') or str(row.get('name','')).isdigit() or row.get('security_status')=='unknown')):
                    missing.append('部分主板证券名称或状态未核验')
                continue
            if key in DISCLOSURES:
                at = stamp(row.get('source_time'))
                if at is None or at > cutoff:
                    if at is None: missing.append('部分信息缺少可靠公开可见时间')
                    continue
                version, identity = row_version(row), event_key(row)
                previous = (seen or {}).get(key, {}).get(identity)
                if previous == version: continue
                if at <= window and not seen: continue
                if at <= window: row['late_arrival'] = True
                if previous: row['change'] = '更正或更新'
                row['version'] = version
                pending_seen.setdefault(key, {})[identity] = version
                if row.get('verified') is not True:
                    missing.append('部分记录仅完成结构化核对，原文尚未确认')
                    row['verification_note'] = '结构化资料；原文未完成核验'
            else:
                source_day = row.get('period_end') or row.get('source_date') or source.get('source_date')
                if source_day != trade_date:
                    missing.append('部分行情记录日期冲突'); continue
                at = stamp(row.get('source_time'))
                if at and at > cutoff:
                    missing.append('已排除截止之后的信息'); continue
                if key == 'limit_up':
                    boards = number(row.get('boards'))
                    if boards is None or boards != boards.to_integral_value() or boards < 1:
                        missing.append('部分连板次数无效，已隔离'); continue
                    row['boards'] = int(boards)
            rows.append(row)
        if key == 'broker_seats':
            rows, errors = broker_rows(rows); rejected.extend(errors)
            if errors: missing.append('部分席位期间或金额冲突，已隔离')
        elif key == 'institutions':
            rows, errors = institution_rows(rows); rejected.extend(errors)
            if errors: missing.append('部分机构统计冲突，已隔离')
            if any(not r.get('industry') for r in rows):
                missing.append('部分行业字段未取得；已核验的机构买卖净额仍独立展示')
        rows = _order(key, rows)
        multi = [r for r in rows if r.get('period') == 'multi']
        candidates = [r for r in rows if r.get('period') != 'multi']
        if key == 'limit_up': candidates = [r for r in candidates if int(r.get('boards') or 0) >= 2]
        if key == 'institutions': candidates = [r for r in candidates if number(r.get('net_cny')) is not None and number(r['net_cny']) > 0]
        if key == 'earnings':
            candidates = [r for r in candidates if (number(r.get('change_low_pct')) is not None and number(r['change_low_pct']) > 0)
                          or '扭亏' in str(r.get('forecast_type'))]
        if key in DISCLOSURES: candidates = [r for r in candidates if r.get('verified') is True]
        featured = candidates if key == 'limit_up' else candidates[:15 if key == 'announcements' else 10]
        if key == 'limit_up': rows = candidates
        if missing and status != 'unavailable': status = 'partial'
        elif status in ('complete', 'empty') and not rows: status = 'empty'
        sections[key] = {'status':status, 'rows':rows, 'featured':featured, 'multi_rows':multi,
                         'missing':list(dict.fromkeys(missing)), 'sources':source.get('sources') or [],
                         'coverage':{'source_records':len(source.get('rows') or []), 'mainboard_records':len(rows),
                                     'featured':len(featured)}, 'source_coverage':source.get('coverage',{}),
                         'collection_complete':source.get('collection_complete', status in ('complete','empty')),
                         'excluded':rejected}
    # A halt learned before the information cutoff annotates the factual close ladder.
    risk_by_code = defaultdict(list)
    for r in sections['announcements']['rows']:
        if r.get('stance') == 'risk' and r.get('verified') is True:
            risk_by_code[r['code']].append(r.get('summary') or r.get('title'))
    for r in sections['limit_up']['rows']:
        r['risk'] = '；'.join(filter(None, risk_by_code[r['code']]))
    states = [s['status'] for s in sections.values()]
    status = 'unavailable' if all(s=='unavailable' for s in states) else ('partial' if any(s in ('partial','unavailable') for s in states) else 'complete')
    mode = 'historical' if cutoff.date() < actual.date() else ('trial' if cutoff.time() < dt.time(22) else 'daily')
    return {'schema_version':1,'module_id':'night-radar','version':VERSION,'report_date':cutoff.date().isoformat(),
            'trade_date':trade_date,'cutoff':cutoff.isoformat(),'as_of':cutoff.isoformat(),'executed_at':actual.isoformat(),
            'window_start':window.isoformat(),'status':status,'mode':mode,'trading_day':trading,
            'summary':'事后来源复核' if mode=='historical' else ('22:00前试运行，信息截至实际执行时点' if mode=='trial' else '收盘事实与晚间披露'),
            'sections':sections,'input_fingerprint':digest(evidence),'pending_seen':pending_seen,
            'seen_before':seen or {}}


def research(now, *, phase='close', input_data=None, scheduled=False):
    from night_radar_store import window_context
    from night_radar_feeds import collect
    window, seen = window_context(now)
    evidence = input_data if input_data is not None else collect(now, window_start=window)
    report = normalize(evidence, now, window_start=window, seen=seen)
    if scheduled:
        actual = stamp(report['executed_at'])
        cutoff = stamp(now)
        if cutoff.time() != dt.time(22) or not dt.timedelta(0) <= actual-cutoff < dt.timedelta(days=1):
            raise ValueError('定时晚报必须使用最近一次22:00截止')
        report['mode'] = 'daily'
        report['summary'] = ('22:00晚报；实际采集时间 ' + actual.strftime('%m-%d %H:%M') +
                             '；晚于截止公开的信息留待下一期')
    return report
