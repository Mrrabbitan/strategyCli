"""Apply the installed Dragon Cycle rules to an immutable hot-sector snapshot.

Read-only research; an outflow flag never changes ranking or admission. Every
query recomputes the daily expansion ledger, so restarts and repeated intraday
queries cannot increase the expansion count.
"""
from __future__ import annotations
import copy
import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

from market_calendar import previous_trading_day, is_trading_day
from research_store import read_json, research_path

TZ = ZoneInfo('Asia/Shanghai')
VERSION = 'dragon-cycle-research-v1'
RULE_LABEL = '本机龙空龙规则快照（原技能无语义版本号）'
RISK_NOTE = '普通A股当日新买部分无法当日卖出；涨停未必买到、跌停未必卖出。未给成本与买入日，只列情景退出，不认定个人盈亏或已经成交。'


def rules():
    root = Path.home()/'.codex/skills/a-share-dragon-cycle'
    paths = [root/'SKILL.md'] + sorted((root/'references').glob('*.md'))
    available = [p for p in paths if p.is_file()]
    if not available:
        local = Path(__file__).resolve().parent/'docs/playbooks/dragon.md'
        available = [local] if local.is_file() else []
    fingerprint = hashlib.sha256(b'\n'.join(p.read_bytes() for p in available)).hexdigest()
    return {'version': RULE_LABEL, 'source_hash': fingerprint, 'research_engine': VERSION,
            'expansion_multiple': 1.2, 'count_from_first_board': True,
            'source_available': bool(available)}


def _number(value):
    if isinstance(value, bool):
        raise ValueError('布尔值不能作成交量')
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('非有限量价')
    return result


def consecutive_boards(bars, cutoff):
    """Recount the trailing ordinary 10% closes without changing frozen rank."""
    count = 0
    try:
        if len(bars) < 2 or bars[-1]['date'] != cutoff:
            raise ValueError('缺少截止日未复权日线')
        for index in range(len(bars)-1, 0, -1):
            row, previous = bars[index], bars[index-1]
            if previous_trading_day(dt.date.fromisoformat(row['date'])).isoformat() != previous['date']:
                raise ValueError('连板重算存在日线缺口')
            limit = (Decimal(str(_number(previous['close'])))*Decimal('1.1')).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
            if abs(Decimal(str(_number(row['close'])))-limit) > Decimal('.001'):
                return {'complete': True, 'count': count, 'error': ''}
            count += 1
        raise ValueError('历史窗口未覆盖本轮首板以前的非涨停日')
    except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
        return {'complete': False, 'count': count, 'error': str(exc)}


def volume_ledger(bars, boards, cutoff):
    """Unadjusted close/volume, including the first board and its predecessor.

    Invalid rows cannot establish entry eligibility. Independently validated
    expansion days remain visible as a lower bound for risk, even if another
    row is missing. A known second expansion is never hidden by incomplete data.
    """
    result = {'complete': False, 'days': [], 'ratio': None, 'expansion_count': 0,
              'errors': [], 'count_is_lower_bound': True}
    if not isinstance(boards, int) or isinstance(boards, bool) or boards < 1:
        result['errors'].append('缺少可靠连续板数'); return result
    dates = [str(r.get('date', '')) for r in bars]
    if len(bars) < boards+1 or dates != sorted(set(dates)) or not dates or dates[-1] != cutoff:
        result['errors'].append('本轮首板前一日、日期连续性或最新日线不完整'); return result
    cycle = bars[-boards:]
    count = 0
    for index, row in enumerate(cycle, len(bars)-boards):
        previous = bars[index-1]
        try:
            day = dt.date.fromisoformat(row['date'])
            if previous_trading_day(day).isoformat() != previous['date']:
                raise ValueError('前一交易日量能缺失')
            close, prev_close = _number(row['close']), _number(previous['close'])
            limit = (Decimal(str(prev_close))*Decimal('1.1')).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
            if abs(Decimal(str(close))-limit) > Decimal('.001'):
                raise ValueError('普通10%涨停参考价与日线冲突，需核验除权或证券状态')
            volume, prev_volume = _number(row['volume_shares']), _number(previous['volume_shares'])
            if min(volume, prev_volume) <= 0:
                raise ValueError('成交股数缺失或不可比')
            expanded = Decimal(str(volume)) >= Decimal(str(prev_volume))*Decimal('1.2')
            count += int(expanded)
            entry = {'date': row['date'], 'volume_shares': volume, 'previous_volume_shares': prev_volume,
                     'volume_ratio': volume/prev_volume, 'expanded': expanded, 'expansion_count': count,
                     'data_kind': 'daily_close', 'confirmed_as_of': row['date']+'T15:00:00+08:00',
                     'time_note': '收盘数据可得后确认，不倒推盘中最早触发时刻'}
            result['days'].append(entry)
            if row['date'] == cutoff:
                result['ratio'] = entry['volume_ratio']
        except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
            result['errors'].append(str(row.get('date', ''))+'：'+str(exc))
    result.update(expansion_count=count, complete=not result['errors'],
                  count_is_lower_bound=bool(result['errors']))
    return result


def _quote_at(quote, as_of):
    try:
        stamp = dt.datetime.strptime(str(quote['timestamp']), '%Y%m%d%H%M%S').replace(tzinfo=TZ)
        clock = stamp.time().replace(tzinfo=None)
        if (stamp.date() != as_of.date() or not is_trading_day(stamp.date())[0]
                or not (dt.time(9,30) <= clock <= dt.time(11,30) or dt.time(13) <= clock <= dt.time(15))
                or not 0 <= (as_of-stamp).total_seconds() <= 90):
            raise ValueError('盘中报价日期、交易时段或90秒时效未通过')
        if _number(quote['volume_shares']) < 0 or _number(quote['price']) <= 0:
            raise ValueError('盘中量价无效')
        return stamp
    except (KeyError, ValueError, TypeError):
        return None


def append_intraday(ledger, last_bar, quote, as_of):
    ledger = copy.deepcopy(ledger)
    stamp = _quote_at(quote or {}, as_of)
    if stamp is None:
        return ledger
    previous = _number(last_bar['volume_shares'])
    volume = _number(quote['volume_shares'])
    if previous <= 0:
        return ledger
    # The base ledger contains complete days only. Replace, never sum, the
    # current cumulative snapshot if a caller supplied an earlier query.
    base = [r for r in ledger['days'] if r['date'] != stamp.date().isoformat()]
    count = sum(bool(r['expanded']) for r in base)
    expanded = Decimal(str(volume)) >= Decimal(str(previous))*Decimal('1.2')
    count += int(expanded)
    base.append({'date': stamp.date().isoformat(), 'volume_shares': volume,
                 'previous_volume_shares': previous, 'volume_ratio': volume/previous,
                 'expanded': expanded, 'expansion_count': count, 'data_kind': 'intraday_cumulative',
                 'confirmed_as_of': stamp.isoformat(),
                 'time_note': '当日累计实量达到昨日全天1.2倍才确认；未达到不代表全天不会发生'})
    ledger.update(days=base, expansion_count=count, intraday_as_of=stamp.isoformat())
    return ledger


def refill_check(evidence, as_of):
    """Only explicitly verified order-book state events prove sealing order.

    Minute highs and pool first/last timestamps are descriptive evidence only;
    they cannot independently prove a seal-open-reseal currently remains sealed.
    """
    events = evidence.get('events', []) if isinstance(evidence, dict) else []
    valid = []
    for event in events:
        try:
            stamp = dt.datetime.fromisoformat(event['source_asof'])
            clock = stamp.astimezone(TZ).time().replace(tzinfo=None) if stamp.tzinfo else dt.time()
            if (stamp.tzinfo is None or stamp.date() != as_of.date() or stamp > as_of
                    or not (dt.time(9,25) <= clock <= dt.time(11,30) or dt.time(13) <= clock <= dt.time(15))
                    or event.get('state') not in {'sealed', 'opened'}
                    or event.get('orderbook_verified') is not True
                    or not str(event.get('source_url', '')).startswith('https://')):
                continue
            valid.append((stamp, event))
        except (TypeError, ValueError, KeyError):
            continue
    valid.sort(key=lambda pair: pair[0])
    states, stamps = [], set()
    for stamp, event in valid:
        if stamp in stamps:
            return False, [e for _, e in valid], '封板事件同一时点冲突，停止确认'
        stamps.add(stamp)
        if not states or event['state'] != states[-1]:
            states.append(event['state'])
    sequence = any(states[i:i+3] == ['sealed', 'opened', 'sealed'] for i in range(len(states)-2))
    current = (valid and states[-1] == 'sealed' and 0 <= (as_of-valid[-1][0]).total_seconds() <= 90)
    passed = bool(sequence and current)
    return passed, [e for _, e in valid], ('当日封板→开板→回封且最新封单状态仍有效' if passed else '当日回封顺序或当前仍封住的证据不足；分钟触价不替代封单')


def _time(raw):
    value = str(raw or '').zfill(6)
    try:
        return dt.datetime.strptime(value, '%H%M%S').strftime('%H:%M:%S')
    except ValueError:
        return None


def research(as_of: dt.datetime, phase='prepare', *, hot=None, previous=None):
    if as_of.tzinfo is None:
        raise ValueError('分析时点必须带时区')
    as_of = as_of.astimezone(TZ)
    if as_of > dt.datetime.now(TZ) + dt.timedelta(seconds=5):
        raise ValueError('不能研究未来时点')
    phase = 'intraday' if phase == 'live' else phase
    if phase not in {'prepare', 'intraday', 'close'}:
        raise ValueError('未知研究阶段')
    if hot is None:
        from refresh_hot_sector_board import research_hot
        hot = research_hot(as_of, phase, persist=False)
    previous = previous if previous is not None else read_json(research_path('dragon/current.json'), {})
    pins = copy.deepcopy(((previous or {}).get('monitoring') or {}).get('pins', []))
    if not isinstance(pins, list):
        pins = []
    cutoff, target = hot.get('cutoff'), hot.get('next_session')
    result = {'schema_version': 1, 'module_id': 'dragon', 'rules': rules(), 'phase': phase,
              'as_of': hot.get('as_of'), 'analysis_as_of': as_of.isoformat(),
              'generated_at': dt.datetime.now(TZ).isoformat(timespec='seconds'),
              'valid_until': hot.get('valid_until'), 'status': 'unavailable',
              'summary': '热点排名证据不足，停止产生新观察名单', 'candidates': [], 'selected': [],
              'excluded': [], 'alternates': [], 'missing': [], 'sources': copy.deepcopy(hot.get('sources', [])),
              'coverage': {'scope': '前五热点原始前三，先排名后量价；不补第四名'},
              'actionable': False, 'research_only': True, 'target_date': target,
              'analysis_date': as_of.date().isoformat(), 'quote_date': cutoff,
              'snapshot_kind': '按时点应用本机龙空龙规则', 'entry_checks': [RISK_NOTE],
              'hot_sector_focus': copy.deepcopy(hot.get('hot_sector_focus')),
              'monitoring': {'valid_until': None, 'observed': [], 'pins': pins},
              'market': {'status': 'missing', 'summary': '市场同刻量能、晋级与炸板证据不足，不能确认适合新增介入'},
              'cash_decision': '等待；若已盈利退出，没有退出后形成的新节点则保持空仓',
              'risk_note': RISK_NOTE}
    context = hot.get('hot_sector_focus') or {}
    if (hot.get('status') == 'unavailable' or context.get('status') != 'complete'
            or context.get('as_of') != cutoff or context.get('source_verified') is not True
            or not cutoff or not target):
        result['missing'] = hot.get('missing') or ['热点原始排名或源日期不完整']
        return result
    from refresh_hot_sector_board import resolve_sessions
    expected_cutoff, expected_target = resolve_sessions(as_of, phase)
    if cutoff != expected_cutoff.isoformat() or target != expected_target.isoformat():
        result['missing'] = ['热点快照不是本次研究所需交易日，禁止把旧名单重新标为当前']
        return result
    result['monitoring']['valid_until'] = target
    observed, eligible, excluded, missing = [], [], [], []
    previous_rows = {row.get('code'): row for row in (previous or {}).get('candidates', []) if isinstance(row, dict)}
    market_evidence = hot.get('market_evidence') or {}
    # Caller-supplied market evidence is usable only with its own period/source
    # and an explicit review, never simply because index prices increased.
    market_ok = False
    try:
        market_stamp = dt.datetime.fromisoformat(market_evidence['source_asof'])
        market_ok = (market_evidence.get('verified') is True and market_evidence.get('allows_entry') is True
                     and market_evidence.get('same_time_comparison') is True
                     and market_stamp.tzinfo is not None and market_stamp.date() == as_of.date()
                     and 0 <= (as_of-market_stamp).total_seconds() <= 90
                     and str(market_evidence.get('source_url', '')).startswith('https://'))
        if market_evidence.get('verified') is True:
            result['market'] = copy.deepcopy(market_evidence)
    except (KeyError, ValueError, TypeError):
        pass
    for group in hot.get('sectors', [])[:5]:
        for item in group.get('items', [])[:3]:
            code = item['code']
            member = context.get('members', {}).get(code) or {}
            if (code not in group.get('top_codes', []) or member.get('sector_member_rank') != item.get('sector_member_rank')
                    or item.get('sector_member_rank', 99) > 3):
                missing.append('原始板内名次冲突：'+code); continue
            bars = copy.deepcopy(item.get('bars', []))
            previous_row = previous_rows.get(code) or {}
            cache_note = None
            if (not bars and (previous or {}).get('quote_date') == cutoff
                    and previous_row.get('volume_ledger', {}).get('complete') is True):
                # Same-date immutable daily bars may be reused; current quotes,
                # auction flags and trade permissions are never carried forward.
                bars = copy.deepcopy(previous_row.get('bars', []))
                cache_note = '本次日线接口不足，复用本模块同一截止日已审计历史量价；当前条件仍重新核验'
            boards_review = consecutive_boards(bars, cutoff)
            ledger = volume_ledger(bars, item.get('current_boards'), cutoff)
            if boards_review['complete'] and boards_review['count'] != item.get('current_boards'):
                ledger = volume_ledger(bars, max(boards_review['count'], 1), cutoff)
                ledger['complete'] = False
                ledger['count_is_lower_bound'] = True
                ledger['errors'].append('源池连续板数与未复权日线重算冲突，取消新增资格')
            base_count, ratio = ledger['expansion_count'], ledger['ratio']
            if target == as_of.date().isoformat() and bars:
                ledger = append_intraday(ledger, bars[-1], item.get('current_quote'), as_of)
            current_stamp = _quote_at(item.get('current_quote') or {}, as_of) if target == as_of.date().isoformat() else None
            count = ledger['expansion_count']
            prior_refill = (item.get('breaks', 0) > 0 and bool(_time(item.get('first_seal')))
                            and bool(_time(item.get('last_seal'))) and ledger['complete'])
            prior_detail = (f"源池记录开板{item.get('breaks')}次，首次{_time(item.get('first_seal'))}、最后{_time(item.get('last_seal'))}；收盘涨停已与未复权日线核对，非逐笔承接证明" if prior_refill else '未取得完整昨日烂板回封交叉证据')
            auction = copy.deepcopy(item.get('auction') or {})
            auction_ok = False
            try:
                astamp = dt.datetime.fromisoformat(auction['source_asof'])
                auction_ok = (auction.get('verified') is True and auction.get('qualified') is True
                              and astamp.date().isoformat() == target and astamp <= as_of
                              and astamp.time().replace(tzinfo=None) == dt.time(9,25))
            except (KeyError, ValueError, TypeError):
                pass
            refill_ok, timeline, refill_note = refill_check(item.get('refill_evidence') or {}, as_of)
            security_ok = False
            sec = item.get('security_evidence') or {}
            try:
                sec_stamp = dt.datetime.fromisoformat(sec['source_asof'])
                security_ok = (sec.get('verified') is True and sec.get('ordinary_mainboard_10pct') is True
                               and sec.get('effective_date') == target and sec_stamp.tzinfo is not None
                               and sec_stamp <= as_of and str(sec.get('source_url', '')).startswith('https://'))
            except (TypeError, ValueError, KeyError):
                pass
            reasons, risks = [], []
            if count >= 2:
                risks.append('已达到第二次按日放量，优先退出预警并取消新增介入')
            if current_stamp is None and (previous or {}).get('quote_date') == cutoff:
                for past in (previous_row.get('volume_ledger') or {}).get('days', []):
                    try:
                        past_stamp = dt.datetime.fromisoformat(past['confirmed_as_of'])
                        if (past.get('data_kind') == 'intraday_cumulative' and past.get('expansion_count', 0) >= 2
                                and past_stamp.tzinfo is not None and past_stamp <= as_of and past_stamp.date() == as_of.date()):
                            risks.append('此前已确认第二次放量（截至'+past_stamp.isoformat()+'）；本次累计量不可用，风险未被解除')
                    except (TypeError, ValueError, KeyError):
                        continue
            if item.get('current_boards', 0) < 2:
                reasons.append('昨日仅首板，可观察强度但不满足昨日至少两板的介入资格')
            if ratio is not None and ratio < 1:
                reasons.append('昨日缩量，取消新增介入资格')
            # Risk evidence must be identified and dated; an arbitrary price
            # decline or fund outflow flag cannot stand in for acceptance failure.
            for risk in item.get('risk_evidence', []):
                try:
                    stamp = dt.datetime.fromisoformat(risk['source_asof'])
                    if (risk.get('verified') is True and stamp.tzinfo is not None and stamp <= as_of
                            and stamp.date() == as_of.date() and risk.get('kind') in {'acceptance_failed', 'cycle_broken', 'market_retreat'}
                            and str(risk.get('source_url', '')).startswith('https://')):
                        risks.append(str(risk.get('detail') or '独立量价风险退出条件触发'))
                except (TypeError, ValueError, KeyError):
                    pass
            conditions = [
                {'label': '热点原始前三', 'passed': True, 'detail': f"{group['theme']}原始第{item['sector_member_rank']}，{cutoff}收盘排名"},
                {'label': '昨日≥2板', 'passed': item.get('current_boards', 0) >= 2, 'detail': str(item.get('current_boards'))+'连板'},
                {'label': '连续板数交叉核验', 'passed': boards_review['count'] == item.get('current_boards') if boards_review['complete'] else None,
                 'detail': f"源池{item.get('current_boards')}板；日线重算{boards_review['count']}板" if boards_review['complete'] else boards_review['error']},
                {'label': '昨日不缩量', 'passed': ratio >= 1 if ratio is not None else None, 'detail': f'昨日全天量倍数 {ratio:.4f}' if ratio is not None else '缺完整日量'},
                {'label': '昨日回封证据（优先项）', 'passed': True if prior_refill else None, 'detail': prior_detail},
                {'label': '逐日放量未到第二次', 'passed': False if count >= 2 else True if ledger['complete'] else None,
                 'detail': f"已核验{count}次" + ('，计数完整' if ledger['complete'] else '，仅为已核验下限')},
                {'label': '当日累计量时效', 'passed': True if current_stamp else None,
                 'detail': '当日累计成交股数通过90秒时效核验' if current_stamp else '当日累计量未核验，不能排除盘中新增放量'},
                {'label': '9:25最终竞价', 'passed': True if auction_ok else None, 'detail': auction.get('reason') or '最终竞价缺失'},
                {'label': '当日回封且仍封住', 'passed': True if refill_ok else None, 'detail': refill_note},
                {'label': '市场环境未转弱', 'passed': True if market_ok else None, 'detail': str(result['market'].get('summary') or '市场同刻量能与接力证据待复核')},
                {'label': '当日证券及公告资格', 'passed': True if security_ok else None, 'detail': '当日普通主板10%限制及公告风险独立复核' if security_ok else '名称排除不替代当日停复牌、上市/退市与公告核验'}]
            historical_pass = (ledger['complete'] and ratio is not None and ratio >= 1 and base_count < 2
                               and boards_review['complete'] and boards_review['count'] == item.get('current_boards')
                               and item.get('current_boards', 0) >= 2)
            clock = as_of.time().replace(tzinfo=None)
            session_open = is_trading_day(as_of.date())[0] and (dt.time(9,30) <= clock < dt.time(11,30) or dt.time(13) <= clock < dt.time(15))
            allowed = (historical_pass and not risks and auction_ok and refill_ok and market_ok and security_ok
                       and current_stamp is not None and session_open)
            status = '退出预警' if risks else '不满足新增条件' if ratio is not None and ratio < 1 else (
                '条件式介入' if allowed else '首板观察' if item.get('current_boards',0) == 1 else '待验证观察')
            source_rows = [{'label': '同一时点热点原始排名', 'url': (hot.get('sources') or [{}])[0].get('url', '')}] + copy.deepcopy(item.get('history_sources') or previous_row.get('sources') or [])
            candidate = {'code': code, 'name': item['name'], 'group': group['theme'], 'status': status,
                         'eligible': bool(allowed), 'actionable': False, 'conditions': conditions,
                         'bars': bars, 'levels': [{'label': '昨日收盘涨停价（非建仓价）', 'value': item['close']}],
                         'metrics': {'昨日板位': item.get('current_boards'), '昨日全天量倍数': ratio,
                                     '已核验放量次数': count, '原始板内名次': item['sector_member_rank'],
                                     '9:25高开幅度%': auction.get('gap_pct'), '竞价换手%': auction.get('turnover_pct')},
                         'reasons': risks+reasons+ledger['errors'], 'sources': source_rows,
                         'volume_ledger': ledger, 'auction': auction, 'refill_timeline': timeline,
                         'board_count_review': boards_review,
                         'history_cache_note': cache_note,
                         'history_source_note': item.get('history_note'),
                         'prior_refill': {'verified': prior_refill, 'detail': prior_detail},
                         'sector_member_rank': item['sector_member_rank'], 'sector_rank': item['sector_rank'],
                         'historical_screen_pass': historical_pass, 'friday_volume_ratio': ratio,
                         'expansion_count': count, 'history': ledger['days'], 'boards': item.get('current_boards'),
                         'close': item['close'], 'risk_note': RISK_NOTE,
                         'invalidation': '第二次放量、昨日缩量、承接失败或市场转弱优先；未确认新节点继续等待'}
            if allowed:
                oldest = min(current_stamp, market_stamp, dt.datetime.fromisoformat(timeline[-1]['source_asof']))
                expiry = oldest + dt.timedelta(seconds=90)
                candidate['signal_valid_until'] = expiry.isoformat()
                candidate['live_as_of'] = oldest.isoformat()
                candidate['live_valid_until'] = expiry.isoformat()
            result['candidates'].append(candidate)
            if risks or (ratio is not None and ratio < 1):
                excluded.append(dict(candidate, exclusion_reasons=risks+reasons))
            # The current research roster is also the risk-observation roster.
            # Admission vetoes do not remove risk names from fund monitoring.
            observed.append({'code': code, 'name': item['name'],
                             'reason': '风险预警观察，禁止新增' if risks or (ratio is not None and ratio < 1)
                             else '首板研究，未获买入资格' if item.get('current_boards', 0) == 1
                             else '待验证研究观察，不代表买入资格'})
            if historical_pass and not risks:
                eligible.append(candidate)
    result['selected'] = eligible[:5]
    result['excluded'] = excluded
    result['monitoring']['observed'] = observed
    result['coverage'].update(source_pool_count=hot.get('pool_count'), observations=len(result['candidates']),
                              historical_pass=len(eligible), actionable=sum(c['eligible'] for c in result['candidates']),
                              fund_monitored=len(observed), excluded=len(excluded),
                              first_board_observations=sum(c['boards'] == 1 for c in result['candidates']),
                              risk_observations=sum(c['status'] == '退出预警' for c in result['candidates']))
    missing += ([] if market_ok else ['市场同刻量能、晋级、炸板及跌停证据未完整核验'])
    if result['candidates']:
        missing += ['没有持仓成本与买入日期；退出仅为情景风险提示']
        missing += ['证券状态、最终竞价及回封证据缺失的股票仅观察，不因排名补齐']
    result['missing'] = list(dict.fromkeys(missing))
    result['status'] = 'partial' if result['candidates'] else 'empty'
    if result['candidates'] and not any(c['volume_ledger']['complete'] or c['expansion_count'] >= 2 for c in result['candidates']):
        result['status'] = 'unavailable'
        result['missing'].append('本次全部日线量能不可用，保留上一有效研究及其原始时间，不覆盖既有风险记录')
    result['summary'] = (f"同一热点快照检查{len(result['candidates'])}席：{len(eligible)}只通过历史量价预筛、{len(excluded)}只出现否决/退出条件；当日可条件式介入{result['coverage']['actionable']}只" if result['candidates'] else '完整热点池没有可列示席位，空名单有效；继续等待')
    result['evidence_digest'] = hashlib.sha256(json.dumps({'hot': hot.get('evidence_digest'), 'rules': result['rules'],
        'candidates': result['candidates']}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    result['ranking_as_of'] = hot.get('ranking_as_of') or cutoff+'T15:00:00+08:00'
    intraday_stamps = [c['volume_ledger']['intraday_as_of'] for c in result['candidates'] if c['volume_ledger'].get('intraday_as_of')]
    result['intraday_as_of'] = max(intraday_stamps) if intraday_stamps else None
    if result['intraday_as_of']:
        result['as_of'] = max(result['as_of'], result['intraday_as_of'])
    return result
