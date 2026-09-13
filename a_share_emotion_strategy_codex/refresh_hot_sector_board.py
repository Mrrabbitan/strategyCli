"""Refresh the dated close-only board without changing holdings or placing orders."""
from __future__ import annotations
import argparse
import concurrent.futures
import datetime as dt
import json
import urllib.parse
import urllib.request
from pathlib import Path

from hot_sector_board import CURRENT, ROOT, DISPLAY_SECTORS, build_board
from hot_sector_leaders import build_hot_sector_context
from market_calendar import is_trading_day
from research_store import load_config


def get(url: str, *, encoding: str = 'utf-8') -> str:
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://quote.eastmoney.com/'})
    with urllib.request.urlopen(req, timeout=15) as response:
        return response.read().decode(encoding)


def refresh(day: dt.date) -> dict:
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
    if day != now.date() or now.hour < 15 or not is_trading_day(day)[0]:
        raise ValueError('本入口只接受当前交易日收盘后数据；盘中不得生成收盘榜')
    next_day = day + dt.timedelta(days=1)
    for _ in range(20):
        if is_trading_day(next_day)[0]:
            break
        next_day += dt.timedelta(days=1)
    else:
        raise ValueError('下一交易日无法确认')
    config = load_config('emotion_config.json')
    pool_url = 'https://push2ex.eastmoney.com/getTopicZTPool?' + urllib.parse.urlencode({
        'ut': '7eea3edcaed734bea9cbfc24409ed989', 'dpt': 'wz.ztzt', 'Pageindex': 0,
        'pagesize': 1000, 'sort': 'fbt:asc', 'date': day.strftime('%Y%m%d')})
    payload = json.loads(get(pool_url))
    data = payload.get('data') or {}
    if str(data.get('qdate')) != day.strftime('%Y%m%d') or len(data.get('pool', [])) != data.get('tc'):
        raise ValueError('涨停池日期或覆盖不完整')
    context = build_hot_sector_context(data['pool'], [], as_of=day.isoformat(), source_verified=True, config=config)
    if context['status'] != 'complete':
        raise ValueError(';'.join(context['errors']))
    codes = [c for g in context['sectors'][:DISPLAY_SECTORS] for c in g['top_codes']]
    if not codes:
        raise ValueError('无达到热点门槛的行业')
    symbol = lambda code: ('sh' if code.startswith('6') else 'sz') + code
    quotes = {}
    text = get('https://qt.gtimg.cn/q=' + ','.join(symbol(c) for c in codes), encoding='gb18030')
    for line in text.splitlines():
        if '="' not in line:
            continue
        fields = line.split('="', 1)[1].split('~')
        if len(fields) > 37:
            quotes[fields[2]] = {'price': float(fields[3]), 'timestamp': fields[30]}
    folder = CURRENT.parent / 'raw' / day.isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'pool.json').write_text(json.dumps(payload, ensure_ascii=False))
    (folder / 'quotes.json').write_text(json.dumps(quotes, ensure_ascii=False))
    histories, announcements = {}, {}

    def evidence(code):
        errors = []
        try:
            history_url = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?' + urllib.parse.urlencode({'param': symbol(code)+',day,,,30,'})
            h = json.loads(get(history_url))
            (folder / (code+'-history.json')).write_text(json.dumps(h, ensure_ascii=False))
            rows = h['data'][symbol(code)].get('day', [])
        except Exception as exc:
            rows = []; errors.append('日线获取失败：'+str(exc))
        items = []
        try:
            ann_url = 'https://np-anotice-stock.eastmoney.com/api/security/ann?' + urllib.parse.urlencode({
                'sr': -1, 'page_size': 10, 'page_index': 1, 'ann_type': 'A', 'client_source': 'web', 'stock_list': code})
            ann = json.loads(get(ann_url))
            (folder / (code+'-announcements.json')).write_text(json.dumps(ann, ensure_ascii=False))
            for row in ((ann.get('data') or {}).get('list') or []):
                date = str(row.get('display_time') or row.get('notice_date') or '')[:10]
                if date and date <= day.isoformat():
                    items.append({'date': date, 'title': str(row.get('title') or ''),
                                  'url': 'https://data.eastmoney.com/notices/detail/'+code+'/'+str(row.get('art_code'))+'.html'})
        except Exception as exc:
            errors.append('公告获取失败：'+str(exc))
        return code, rows, {'items': items, 'error': '；'.join(errors)}

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        for code, rows, ann in executor.map(evidence, codes):
            histories[code], announcements[code] = rows, ann
    board = build_board(payload, quotes, histories, cutoff=day.isoformat(), next_session=next_day.isoformat(),
                        config=config, announcements=announcements)
    board['sources'][0]['url'] = pool_url
    temp = CURRENT.with_suffix('.tmp')
    temp.write_text(json.dumps(board, ensure_ascii=False, indent=2))
    temp.replace(CURRENT)
    return board


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', type=dt.date.fromisoformat, default=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date())
    args = parser.parse_args()
    board = refresh(args.date)
    print(json.dumps({'cutoff': board['cutoff'], 'next_session': board['next_session'],
                      'sectors': [{k: g[k] for k in ('theme', 'top_codes')} for g in board['sectors']],
                      'actionable': board['actionable']}, ensure_ascii=False))
