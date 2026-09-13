"""Refresh a verified close ranking, then attach separately dated auction evidence.

The ranking is always frozen before strategy-specific admission. Missing feeds
produce an unavailable/partial result, while a verified empty pool is a valid
empty result. No page build, account access, order, or automatic notification.
"""
from __future__ import annotations
import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import math
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

from hot_sector_board import DISPLAY_SECTORS, VERSION, build_board, policy_hash
from hot_sector_leaders import VERSION as RANK_VERSION, build_hot_sector_context
from market_calendar import is_trading_day, previous_trading_day
from research_store import atomic_json, research_path, load_config, update_lock, read_json
from auction_evidence import endpoint as auction_endpoint, parse as parse_auction, pending as pending_auction

TZ = ZoneInfo('Asia/Shanghai')


def get(url: str, *, encoding: str = 'utf-8') -> str:
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://quote.eastmoney.com/'})
    with urllib.request.urlopen(req, timeout=15) as response:
        return response.read().decode(encoding)


def next_trading_day(day):
    candidate = day + dt.timedelta(days=1)
    for _ in range(20):
        ok, reason = is_trading_day(candidate)
        if '缺少' in reason or '不可用' in reason:
            raise ValueError(reason)
        if ok:
            return candidate
        candidate += dt.timedelta(days=1)
    raise ValueError('下一交易日无法确认')


def resolve_sessions(as_of, phase='prepare'):
    phase = 'live' if phase == 'intraday' else phase
    if as_of.tzinfo is None:
        raise ValueError('分析时点必须带时区')
    as_of = as_of.astimezone(TZ)
    if phase not in {'prepare', 'live', 'close'}:
        raise ValueError('未知研究阶段')
    ok, reason = is_trading_day(as_of.date())
    if '缺少' in reason or '不可用' in reason:
        raise ValueError(reason)
    closed = ok and as_of.time().replace(tzinfo=None) >= dt.time(15)
    if phase == 'close' and not closed:
        raise ValueError('收盘研究只能在交易日收盘以后执行')
    cutoff = as_of.date() if closed and phase != 'live' else previous_trading_day(as_of.date())
    return cutoff, next_trading_day(cutoff)


def _pool_url(day):
    return 'https://push2ex.eastmoney.com/getTopicZTPool?' + urllib.parse.urlencode({
        'ut': '7eea3edcaed734bea9cbfc24409ed989', 'dpt': 'wz.ztzt', 'Pageindex': 0,
        'pagesize': 1000, 'sort': 'fbt:asc', 'date': day.strftime('%Y%m%d')})


def _quotes(text):
    quotes = {}
    for line in text.splitlines():
        if '=\"' not in line:
            continue
        fields = line.split('=\"', 1)[1].split('~')
        try:
            if len(fields) > 37:
                price, previous, lots = map(float, (fields[3], fields[4], fields[6]))
                if not all(math.isfinite(v) and v >= 0 for v in (price, previous, lots)):
                    continue
                quotes[fields[2]] = {'price': price, 'previous_close': previous,
                                    'volume_shares': lots * 100, 'timestamp': fields[30],
                                    'volume_basis': '腾讯当日累计成交量，原字段手×100'}
        except (ValueError, IndexError):
            continue
    return quotes


def _normal_history(payload, code, cutoff):
    symbol = ('sh' if code.startswith('6') else 'sz') + code
    rows = (payload.get('data') or {}).get(symbol, {}).get('day', [])
    result = []
    for row in rows:
        if len(row) < 6 or str(row[0]) > cutoff.isoformat():
            continue
        values = [float(value) for value in row[1:6]]
        if not all(math.isfinite(value) and value >= 0 for value in values):
            raise ValueError('未复权日线数值或量能异常')
        result.append(row[:6])
    return result


def shared_daily_history(as_of, cutoff, *, document=None):
    """Reuse already verified unadjusted daily evidence, never another pool.

    The installed skill's normalized `volume` field is actual shares. Only a
    same-cutoff, independently dated unadjusted source may enter the adapter;
    qfq evidence or undated recovered pools are deliberately not consulted.
    """
    if document is None:
        root = Path.home()/'Library/Application Support/Yichujifa'
        pointer = read_json(root/'latest.json', {})
        try:
            path = Path(pointer['evidence']).resolve()
            path.relative_to((root/'runs').resolve())
            document = read_json(path, {})
        except (ValueError, KeyError, TypeError):
            return {}
    try:
        generated = dt.datetime.fromisoformat(document['generated_at'])
        if (document.get('as_of') != cutoff.isoformat() or generated.tzinfo is None
                or generated > as_of or not isinstance(document.get('stocks'), list)):
            return {}
    except (KeyError, TypeError, ValueError):
        return {}
    result = {}
    for stock in document['stocks']:
        try:
            code = stock['code']
            if stock.get('history_verified') is not True or stock.get('errors'):
                continue
            sources = []
            for source in stock.get('sources', []):
                stamp = dt.datetime.fromisoformat(source['retrieved_at'])
                if (source.get('adjustment') == 'unadjusted' and source.get('observation_through') == cutoff.isoformat()
                        and stamp.tzinfo is not None and stamp <= as_of
                        and str(source.get('url', '')).startswith('https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?')
                        and isinstance(source.get('sha256'), str) and len(source['sha256']) == 64):
                    sources.append(source)
            if not sources:
                continue
            bars = stock['bars']
            dates = [row['date'] for row in bars]
            if not dates or dates != sorted(set(dates)) or dates[-1] != cutoff.isoformat():
                continue
            rows = []
            for bar in bars:
                values = [float(bar[k]) for k in ('open', 'close', 'high', 'low', 'volume')]
                if not all(math.isfinite(v) and v > 0 for v in values):
                    raise ValueError('无效共享日线量价')
                # Native stock['bars'].volume uses shares; legacy audit uses lots.
                rows.append([bar['date'], values[0], values[1], values[2], values[3], values[4]/100])
            result[code] = {'rows': rows, 'bars': bars, 'sources': sources,
                            'cache_generated_at': document['generated_at'],
                            'note': '复用同截止日原生研究日线，新浪未复权；源股数÷100传入手数审计，不复用其他策略排名或资格'}
        except (KeyError, TypeError, ValueError):
            continue
    return result


def _envelope(as_of, phase, cutoff=None, target=None):
    config = load_config('emotion_config.json')
    generated = dt.datetime.now(TZ).isoformat(timespec='seconds')
    return {'schema_version': 1, 'module_id': 'hot', 'phase': phase,
            'version': VERSION, 'rank_version': RANK_VERSION, 'policy_hash': policy_hash(config),
            'as_of': cutoff.isoformat()+'T15:00:00+08:00' if cutoff else None,
            'analysis_as_of': as_of.isoformat(timespec='seconds'), 'generated_at': generated,
            'cutoff': cutoff.isoformat() if cutoff else None,
            'next_session': target.isoformat() if target else None,
            'valid_until': target.isoformat()+'T15:00:00+08:00' if target else None,
            'status': 'unavailable', 'summary': '热点资料尚未通过日期与覆盖核验',
            'coverage': {'scope': '沪深主板普通A股收盘涨停池，非全板块成分'},
            'missing': [], 'candidates': [], 'sectors': [], 'sources': [],
            'rules': {'version': RANK_VERSION, 'source_hash': policy_hash(config)},
            'actionable': False, 'research_only': True}


def research_hot(as_of: dt.datetime, phase: str = 'prepare', *, persist: bool = False) -> dict:
    """Return a research snapshot; publishing is normally the coordinator's job."""
    phase = 'intraday' if phase == 'live' else phase
    as_of = as_of.astimezone(TZ) if as_of.tzinfo is not None else as_of
    cutoff, target = resolve_sessions(as_of, phase)
    if as_of > dt.datetime.now(TZ) + dt.timedelta(seconds=5):
        raise ValueError('不能查询未来时点')
    envelope = _envelope(as_of, phase, cutoff, target)
    raw, errors, config = {}, [], load_config('emotion_config.json')
    pool_url = _pool_url(cutoff)
    envelope['sources'] = [{'label': '东方财富完整收盘涨停池（含源qdate与总数）', 'url': pool_url}]
    try:
        payload = json.loads(get(pool_url))
        raw['pool'] = payload
        data = payload.get('data') or {}
        pool = data.get('pool')
        if (str(data.get('qdate')) != cutoff.strftime('%Y%m%d') or not isinstance(pool, list)
                or len(pool) != data.get('tc') or len({r.get('c') for r in pool}) != len(pool)):
            raise ValueError('涨停池源日期、总数或唯一代码校验失败；不改写qdate或沿用旧榜')
        context = build_hot_sector_context(pool, [], as_of=cutoff.isoformat(), source_verified=True, config=config)
        if context['status'] != 'complete':
            raise ValueError(';'.join(context['errors']))
        codes = [code for sector in context['sectors'][:DISPLAY_SECTORS] for code in sector['top_codes']]
        quote_map, histories, announcements, auctions = {}, {}, {}, {}
        shared_histories = shared_daily_history(as_of, cutoff)
        if codes:
            try:
                symbol = lambda code: ('sh' if code.startswith('6') else 'sz') + code
                quote_text = get('https://qt.gtimg.cn/q=' + ','.join(symbol(c) for c in codes), encoding='gb18030')
                raw['quote_text'] = quote_text
                for code, quote in _quotes(quote_text).items():
                    try:
                        stamp = dt.datetime.strptime(quote['timestamp'], '%Y%m%d%H%M%S').replace(tzinfo=TZ)
                        if stamp <= as_of:
                            quote_map[code] = quote
                    except (ValueError, TypeError):
                        continue
            except Exception as exc:
                errors.append('独立即时价格源不可用：'+type(exc).__name__)

        def evidence(code):
            local, missing = {}, []
            symbol = ('sh' if code.startswith('6') else 'sz') + code
            if code in shared_histories:
                local['shared_history'] = shared_histories[code]
                rows = shared_histories[code]['rows']
            else:
                try:
                    url = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?' + urllib.parse.urlencode({'param': symbol+',day,,,80,'})
                    local['history'] = json.loads(get(url))
                    rows = _normal_history(local['history'], code, cutoff)
                except Exception as exc:
                    rows = []; missing.append('未复权日线获取失败：'+type(exc).__name__)
            items = []
            try:
                url = 'https://np-anotice-stock.eastmoney.com/api/security/ann?' + urllib.parse.urlencode({
                    'sr': -1, 'page_size': 10, 'page_index': 1, 'ann_type': 'A', 'client_source': 'web', 'stock_list': code})
                local['announcements'] = json.loads(get(url))
                for row in ((local['announcements'].get('data') or {}).get('list') or []):
                    stamp = str(row.get('display_time') or row.get('notice_date') or '')
                    date = stamp[:10]
                    # Date-only same-day announcements are not proven disclosed yet.
                    if date and date < as_of.date().isoformat():
                        items.append({'date': date, 'title': str(row.get('title') or ''),
                                      'url': 'https://data.eastmoney.com/notices/detail/'+code+'/'+str(row.get('art_code'))+'.html'})
            except Exception as exc:
                missing.append('公告列表获取失败：'+type(exc).__name__)
            auction = pending_auction('目标交易日最终竞价尚未出现；收盘竞价不代替下一日', source_url=auction_endpoint(code))
            if target == as_of.date() and as_of.time().replace(tzinfo=None) >= dt.time(9, 25):
                try:
                    local['auction_minutes'] = json.loads(get(auction_endpoint(code)))
                    auction = parse_auction(local['auction_minutes'], code, as_of)
                except Exception as exc:
                    auction = pending_auction('盘前分钟获取失败：'+type(exc).__name__, source_url=auction_endpoint(code))
            return code, rows, {'items': items, 'error': '；'.join(missing)}, auction, local

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            for code, rows, ann, auction, local in executor.map(evidence, codes):
                histories[code], announcements[code], auctions[code], raw[code] = rows, ann, auction, local
        board = build_board(payload, quote_map, histories, cutoff=cutoff.isoformat(), next_session=target.isoformat(),
                            config=config, announcements=announcements)
        board.update({k: v for k, v in envelope.items() if k not in {'sectors', 'status', 'sources', 'summary'}})
        board['sources'][0]['url'] = pool_url
        candidates = []
        for sector in board['sectors']:
            for item in sector['items']:
                code = item['code']
                item['auction'] = auctions[code]
                item['current_quote'] = quote_map.get(code)
                shared = shared_histories.get(code)
                item['history_sources'] = ([dict(s, label='新浪未复权日线（同日原生缓存）') for s in shared['sources']] if shared else
                    [{'label': '腾讯未复权日线', 'url': 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param='+('sh' if code.startswith('6') else 'sz')+code+',day,,,80,'}])
                item['history_note'] = shared['note'] if shared else '本次获取腾讯未复权日线'
                if shared:
                    amounts = {bar['date']: bar.get('amount') for bar in shared['bars']}
                    for bar in item['bars']:
                        bar['amount_cny'] = amounts.get(bar['date'])
                if not item['quote_verified']:
                    # A current quote's own previous-close field may corroborate
                    # the baseline, but never masquerades as a historical quote.
                    quote = quote_map.get(code) or {}
                    if (str(quote.get('timestamp', '')).startswith(target.strftime('%Y%m%d'))
                            and abs(float(quote.get('previous_close', 0))-item['close']) <= .011):
                        item['baseline_reference_match'] = True
                item['auction_buy_eligible'] = False
                volume = item['volume']
                conditions = [
                    {'label': '原始板内前三', 'passed': True, 'detail': f"板内原始第{item['sector_member_rank']}，不补位"},
                    {'label': '昨日收盘量价', 'passed': True if volume['complete'] else None, 'detail': volume.get('error') or '未复权日线与源池涨停价核验'},
                    {'label': '9:25最终竞价', 'passed': True if auctions[code]['qualified'] else None, 'detail': auctions[code]['reason']},
                    {'label': '公告及交易资格', 'passed': None, 'detail': '公告标题仅供检索，正文、当日证券状态仍待复核'}]
                candidate = dict(item, group=sector['theme'], eligible=False, conditions=conditions,
                                 metrics={'原始板内名次': item['sector_member_rank'], '行业强度分': item['strength_score'],
                                          '昨收': item['close'], '昨日板位': item['current_boards'],
                                          '昨日全天量倍数': volume['ratio'], '放量次数': volume['expansion_count']},
                                 reasons=item['vetoes']+item['pending'], levels=[],
                                 sources=[{'label': '东方财富原始涨停池', 'url': pool_url}] + item['history_sources'])
                candidates.append(candidate)
        missing = ['当日最终竞价及分母未完整核验', '当日证券状态与公告正文未完整核验'] if candidates else []
        missing += errors
        history_missing = sum(not c['volume']['complete'] for c in candidates)
        if history_missing:
            missing.append(f'{history_missing}只逐日量能未完整通过')
        board.update(candidates=candidates, status='partial' if candidates and missing else 'complete' if candidates else 'empty',
                     missing=missing, auction_feed='point_in_time_checked' if target == as_of.date() and as_of.hour >= 9 else 'awaiting_session',
                     coverage={'scope': board['scope'], 'source_pool_count': len(pool), 'source_date_verified': True,
                               'source_count_verified': True, 'ranked_sectors': len(context['sectors']),
                               'displayed_sectors': len(board['sectors']), 'observations': len(candidates),
                               'history_verified': len(candidates)-history_missing,
                               'same_date_cached_histories': sum(c['code'] in shared_histories for c in candidates),
                               'auction_final_verified': sum(c['auction']['verified'] for c in candidates),
                               'auction_qualified': sum(c['auction']['qualified'] for c in candidates)},
                     summary=(f"以{cutoff.isoformat()}完整收盘涨停池重算前五热点，保留{len(candidates)}个原始前三席位；等待目标日竞价与策略证据" if candidates else '完整池已核验，无行业达到既定热点门槛，空榜有效；不补位'))
        board['ranking_as_of'] = board['as_of']
        auction_stamps = [row['auction'].get('source_asof') for row in candidates if row['auction'].get('verified')]
        board['auction_as_of'] = max(auction_stamps) if auction_stamps else None
        if board['auction_as_of']:
            board['as_of'] = max(board['as_of'], board['auction_as_of'])
        result = board
    except Exception as exc:
        envelope['missing'] = [str(exc)]
        envelope['coverage'].update(source_date_verified=False, source_count_verified=False)
        result = envelope
    result['raw_evidence'] = raw
    result['evidence_digest'] = hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if persist:
        # Compatibility for the original manual entry point. Unified research
        # calls persist=False and publishes only this module under its lock.
        with update_lock():
            atomic_json(research_path('hot_sectors/current.json'), result)
    return result


def refresh(day: dt.date, *, persist: bool = True) -> dict:
    """Backward-compatible daily refresh; past complete days are now allowed."""
    now = dt.datetime.now(TZ)
    if day > now.date():
        raise ValueError('未来交易日不可研究')
    if day == now.date():
        return research_hot(now, 'prepare', persist=persist)
    as_of = dt.datetime.combine(day, dt.time(15, 40), TZ)
    return research_hot(as_of, 'close', persist=persist)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', type=dt.date.fromisoformat)
    parser.add_argument('--as-of', type=dt.datetime.fromisoformat)
    parser.add_argument('--phase', choices=['prepare', 'intraday', 'live', 'close'], default='prepare')
    parser.add_argument('--no-persist', action='store_true')
    args = parser.parse_args()
    board = (refresh(args.date, persist=not args.no_persist) if args.date else
             research_hot(args.as_of or dt.datetime.now(TZ), args.phase, persist=not args.no_persist))
    print(json.dumps({k: board.get(k) for k in ('status', 'cutoff', 'next_session', 'summary', 'missing')}, ensure_ascii=False))
