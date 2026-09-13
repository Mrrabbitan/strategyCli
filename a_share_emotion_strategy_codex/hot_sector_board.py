"""Dated top-five-sector research board; no auction or order execution."""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import math
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from hot_sector_leaders import VERSION as RANK_VERSION, build_hot_sector_context
from market_calendar import is_trading_day, previous_trading_day
from research_store import research_path, load_config

ROOT = Path(__file__).resolve().parent
CURRENT = research_path('hot_sectors/current.json')
VERSION = 'hot-sector-board-v2'
DISPLAY_SECTORS = 5
SCOPE = '沪深主板普通A股收盘涨停池；不含临板股，非全板块成分排名'


def policy_hash(config: dict) -> str:
    return hashlib.sha256(json.dumps(config['hot_sector_focus'], sort_keys=True).encode()).hexdigest()


def selection_evidence(row: dict, members: list[dict]) -> list[str]:
    """Explain observed rank inputs, without asserting a business catalyst or inflow."""
    labels = {'participation': '成交参与', 'seal_quality': '封板质量',
              'resilience': '承接代理', 'repeat_strength': '重复强势', 'board_position': '板位'}
    components = sorted(row['strength_components'].items(), key=lambda item: -item[1])
    supports = '、'.join(f'{labels[key]} {value:.1f} 分' for key, value in components[:2])
    amount_rank = 1 + sum(m['amount_cny'] > row['amount_cny'] for m in members)
    turnover = row['turnover_pct']
    turnover_note = '落在模型3%–18%的换手区间' if 3 <= turnover <= 18 else (
        '低于模型3%的换手基准，成交参与分受限' if turnover < 3 else '超过模型18%的换手基准，过热扣分')
    seal_note = '当日未记录开板' if row['breaks'] == 0 else f"当日开板{row['breaks']}次，次数越多承接代理分越低"
    return [
        f"板内综合第{row['sector_member_rank']}；主要得分来自{supports}。已确认{row['current_boards']}连板，数据源近期统计涨停{row['recent_limit_count']}次。",
        f"成交额{row['amount_cny']/1e8:.2f}亿元，在本行业{len(members)}只比较股中列第{amount_rank}；换手{turnover:.2f}%，{turnover_note}。",
        f"收盘封单/全天成交额{row['seal_amount_ratio']:.1%}；{seal_note}。封单是挂单存量，不代表实际净流入或次日可成交。",
    ]


def volume_audit(rows: list, boards: int, cutoff: str, close: float) -> dict:
    result = {'complete': False, 'ratio': None, 'expansion_count': None, 'days': [], 'error': ''}
    try:
        if boards < 1 or len(rows) < boards + 2:
            raise ValueError('缺少本轮首板及前一日量能')
        dates = [str(r[0]) for r in rows]
        if dates != sorted(set(dates)) or dates[-1] != cutoff:
            raise ValueError('日线日期不匹配或重复')
        if abs(float(rows[-1][2]) - close) > .011:
            raise ValueError('日线与收盘价格不一致')
        cycle = rows[-boards:]
        count = 0
        for i, row in enumerate(cycle, len(rows) - boards):
            prev = rows[i - 1]
            if previous_trading_day(dt.date.fromisoformat(row[0])).isoformat() != prev[0]:
                raise ValueError('周期内日线缺交易日')
            expected = (Decimal(str(prev[2])) * Decimal('1.1')).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
            if abs(Decimal(str(row[2])) - expected) > Decimal('.001'):
                raise ValueError('连板与未复权日线不一致，需核验除权或涨停参考价')
            v, before = float(row[5]), float(prev[5])
            if not all(math.isfinite(x) and x > 0 for x in (v, before)):
                raise ValueError('成交量缺失或不可比')
            ratio = v / before
            expanded = Decimal(str(row[5])) >= Decimal(str(prev[5])) * Decimal('1.2')
            count += int(expanded)
            result['days'].append({'date': row[0], 'volume_lots': v, 'previous_volume_lots': before,
                                   'ratio': ratio, 'expanded': expanded, 'cumulative': count})
        result.update(complete=True, ratio=result['days'][-1]['ratio'], expansion_count=count)
    except (ValueError, IndexError, TypeError, ArithmeticError) as exc:
        result.update(error=str(exc), days=[])
    return result


def build_board(pool_payload: dict, quotes: dict, histories: dict, *, cutoff: str,
                next_session: str, config: dict, announcements: dict | None = None) -> dict:
    day, next_day = dt.date.fromisoformat(cutoff), dt.date.fromisoformat(next_session)
    data = pool_payload.get('data') or {}
    pool = data.get('pool') or []
    if (str(data.get('qdate')) != cutoff.replace('-', '') or len(pool) != data.get('tc')
            or not pool or len({r.get('c') for r in pool}) != len(pool)):
        raise ValueError('涨停池日期、总数或唯一代码校验失败')
    if not is_trading_day(day)[0] or not is_trading_day(next_day)[0] or previous_trading_day(next_day) != day:
        raise ValueError('观察日不是下一交易日')
    context = build_hot_sector_context(pool, [], as_of=cutoff, source_verified=True, config=config)
    if context['status'] != 'complete':
        raise ValueError(';'.join(context['errors']))
    pool_map = {r['c']: r for r in pool}
    groups = []
    for sector in context['sectors'][:DISPLAY_SECTORS]:
        group = dict(sector, items=[])
        members = [r for r in context['members'].values() if r['theme'] == sector['theme']]
        for code in sector['top_codes']:
            row, raw = dict(context['members'][code]), pool_map[code]
            close = float(raw['p']) / 1000
            quote = quotes.get(code) or {}
            quote_ok = (str(quote.get('timestamp', '')).startswith(cutoff.replace('-', ''))
                        and str(quote.get('timestamp', ''))[8:12] >= '1500'
                        and abs(float(quote.get('price') or 0) - close) <= .011)
            audit = volume_audit(histories.get(code, []), row['current_boards'], cutoff, close)
            vetoes, pending = [], ['下一交易日9:25最终竞价尚未核验', '当日分歧回封尚未发生', '公告原文与交易资格待复核']
            if row['current_boards'] < 2:
                vetoes.append('仅首板，不满足原战法昨日≥2板')
            if audit['complete']:
                if audit['ratio'] < 1:
                    vetoes.append(f"昨日缩量 {audit['ratio']:.3f} 倍")
                if audit['expansion_count'] >= 2:
                    vetoes.insert(0, '本轮已第二次放量，退出风险优先')
            else:
                pending.append(audit['error'])
            if not quote_ok:
                pending.append('双源收盘报价未通过')
            ann = (announcements or {}).get(code, {})
            row.update(close=close, quote_verified=quote_ok, quote_timestamp=quote.get('timestamp'),
                       volume=audit, vetoes=vetoes, pending=pending,
                       status='不满足新增条件' if vetoes else '待核验',
                       actionable=False, auction_buy_eligible=False,
                       selection_reasons=selection_evidence(row, members),
                       announcement_items=ann.get('items', []), announcement_error=ann.get('error'),
                       first_seal=raw.get('fbt'), last_seal=raw.get('lbt'))
            group['items'].append(row)
        groups.append(group)
    return {'version': VERSION, 'rank_version': RANK_VERSION, 'policy_hash': policy_hash(config),
            'cutoff': cutoff, 'next_session': next_session, 'scope': SCOPE, 'pool_count': len(pool),
            'generated_at': dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec='seconds'),
            'expires_at': next_session + 'T15:00:00+08:00', 'research_only': True,
            'auction_feed': 'not_connected', 'actionable': False, 'sectors': groups,
            'ranking_method': context['methodology'],
            'sources': [{'label': '东方财富涨停池', 'url': 'https://quote.eastmoney.com/ztb/detail#type=ztgc'},
                        {'label': '腾讯收盘行情', 'url': 'https://qt.gtimg.cn/q=' + ','.join(('sh' if c.startswith('6') else 'sz')+c for g in groups for c in g['top_codes'])}]}


def load_board(path: Path | None = None) -> dict:
    path = path if path is not None else research_path("hot_sectors/current.json")
    try:
        data = json.loads(path.read_text())
        config = load_config('emotion_config.json')
        if (data['version'] != VERSION or data['rank_version'] != RANK_VERSION
                or data['policy_hash'] != policy_hash(config) or data['actionable'] is not False):
            raise ValueError('榜单版本或策略参数已变化')
        if not data['cutoff'] < data['next_session'] or not 0 < len(data['sectors']) <= DISPLAY_SECTORS:
            raise ValueError('榜单日期或数量不符')
        dt.datetime.fromisoformat(data['expires_at'])
        seen = set()
        for i, group in enumerate(data['sectors'], 1):
            if group['sector_rank'] != i or len(group['items']) > 3:
                raise ValueError('板块名次不符')
            for j, row in enumerate(group['items'], 1):
                if row['code'] in seen or row['sector_member_rank'] != j or row['actionable'] is not False:
                    raise ValueError('股票名次或资格不符')
                if not row.get('selection_reasons') or not all(isinstance(s, str) for s in row['selection_reasons']):
                    raise ValueError('缺少入选理由')
                seen.add(row['code'])
        rotation_path = path.parent / 'rotation.json'
        try:
            rotation = json.loads(rotation_path.read_text())
            data['rotation'] = rotation if rotation.get('cutoff') == data['cutoff'] else None
        except (OSError, ValueError):
            data['rotation'] = None
        return dict(data, available=True)
    except (OSError, ValueError, KeyError, TypeError):
        return {'available': False, 'error': '热点榜单待更新；不沿用旧版排名。'}


def render_board(data: dict) -> str:
    e = lambda value: html.escape(str(value))
    if not data.get('available'):
        return '<p class="notice">'+e(data.get('error', '热点榜待更新'))+'</p>'
    cards = []
    for group in data['sectors']:
        rows = []
        for row in group['items']:
            v = row['volume']
            ratio = f"{v['ratio']:.3f}×" if v['complete'] else '待核验'
            count = str(v['expansion_count']) if v['complete'] else '待核验'
            ledger = ''.join(f"<tr><td>{e(d['date'])}</td><td>{d['volume_lots']:,.0f}</td><td>{d['previous_volume_lots']:,.0f}</td><td>{d['ratio']:.3f}</td><td>{d['cumulative']}</td></tr>" for d in v['days'])
            ann_links = ''.join('<li><a target="_blank" rel="noopener noreferrer" href="'+e(x['url'])+'">'+e(x['date']+' '+x['title'])+'</a></li>' for x in row['announcement_items'] if x.get('url','').startswith('https://'))
            reasons = '；'.join(row['vetoes']) or '日线预筛未见否决，继续等待竞价与回封核验'
            rows.append(f'''<article class="focus-stock" data-focus-code="{e(row['code'])}"><header><div><span class="focus-rank">板内 {row['sector_member_rank']}</span><h3>{e(row['name'])} <small>{e(row['code'])}</small></h3></div><b>{row['strength_score']:.1f}<small>强度分 / 非胜率</small></b></header>
<dl><div><dt>收盘 / 元</dt><dd>{row['close']:.2f}</dd></div><div><dt>已确认连板</dt><dd>{row['current_boards']} 板</dd></div><div><dt>全天量倍数</dt><dd>{ratio}</dd></div><div><dt>本轮放量次数</dt><dd>{count}</dd></div></dl><p class="focus-veto">{e(reasons)}</p><p class="focus-pending">竞价直买：尚无合格依据</p>
<div class="focus-reasons"><b>为什么入选</b><ul>{''.join('<li>'+e(reason)+'</li>' for reason in row['selection_reasons'])}</ul></div>
<details><summary>量价证据与待核验事项</summary><p>收盘双源核验：{'通过' if row['quote_verified'] else '未通过'}；开板记录 {row['breaks']} 次。成交额 {row['amount_cny']/1e8:.2f} 亿元，换手 {row['turnover_pct']:.2f}%。</p><ul>{''.join('<li>'+e(x)+'</li>' for x in row['pending'])}</ul><div class="focus-ledger"><table><caption>逐日量能，单位：手</caption><thead><tr><th>日期</th><th>全天量</th><th>前日量</th><th>倍数</th><th>累计放量</th></tr></thead><tbody>{ledger}</tbody></table></div><p>公告列表仅供原文核验，获取列表不代表完成审阅。</p><ul>{ann_links or '<li>公告待补充</li>'}</ul></details></article>''')
        caveat = '<p class="focus-scope">本行业比较池仅有3只或更少：进入前三不等于达到足够强度，仍须逐只检查否决条件。</p>' if group['comparison_count'] <= 3 else ''
        cards.append(f'''<section class="focus-sector"><div class="focus-sector-title"><h2>{group['sector_rank']}. {e(group['theme'])}</h2><span>{group['limit_up_count']}只主板涨停 · 最高{group['max_boards']}板</span></div>{caveat}<div class="focus-stock-grid">{''.join(rows)}</div></section>''')
    count = sum(len(g['items']) for g in data['sectors'])
    blocked = sum(bool(r['vetoes']) for g in data['sectors'] for r in g['items'])
    rotation = data.get('rotation')
    rotation_html = '<p class="notice">轮动阶段解读待更新，不能沿用旧日期结论。</p>'
    if rotation:
        rotation_html = '<section class="focus-rotation"><h2>大盘与四组方向</h2><p>'+e(rotation['summary'])+'</p><div class="focus-rotation-grid">'+''.join('<article><h3>'+e(x['direction'])+'</h3><p>'+e(x['stage'])+'</p><p><b>反证：</b>'+e(x['invalidation'])+'</p><p><b>下一步：</b>'+e(x['next_check'])+'</p></article>' for x in rotation['directions'])+'</div><p>'+e(rotation['market'])+'</p><ul>'+''.join('<li><a target="_blank" rel="noopener noreferrer" href="'+e(s['url'])+'">'+e(s['label'])+'</a></li>' for s in rotation['sources'] if s['url'].startswith('https://'))+'</ul></section>'
    return f'''<div id="hot-sector-board" data-expires="{e(data['expires_at'])}"><div class="focus-heading"><div><p>行情截至 {e(data['cutoff'])} 收盘 · 下一交易日 {e(data['next_session'])}</p><h1>前五热点 · 板内前三</h1></div><div class="focus-count"><b>{count}</b>强度观察席位 / {blocked}席存在否决条件</div></div>
<p id="focus-validity" class="notice">这是收盘研究快照，未接入实时集合竞价。刷新网页不会取得9:25成交数据。排名不等于买入资格。</p>
<p class="focus-scope">{e(data['scope'])}。原始涨停池 {data['pool_count']} 只；先按涨停家数、最高连板、涨停成交额排板块，再按原模型强度分排股票。展示前{len(data['sectors'])}个板块的原始前三，不补后排。</p>
<div class="focus-checks"><article><h2>9:25 核验</h2><p>核对最终竞价成交价、实际成交量与竞价换手；高开或大封单本身不足以确认买点。</p></article><article><h2>开盘后确认</h2><p>先过昨日板位与量能门槛，再等待封板→开板→回封，且当时仍封住。</p></article><article><h2>否决优先</h2><p>昨日缩量、第二次按日放量或承接失败时取消新增资格。首板仅保留研究观察。</p></article></div>
{''.join(cards)}{rotation_html}<details class="evidence"><summary>评分口径与数据来源</summary><p>成交参与30%、封板质量20%、承接代理20%、重复强势20%、板位10%。权重未回测，不代表概率。板内分数使用各自行业基准，不能直接跨行业比较。{e(data['ranking_method'])}</p><ul>{''.join('<li><a href="'+e(s['url'])+'" target="_blank" rel="noopener noreferrer">'+e(s['label'])+'</a></li>' for s in data['sources'])}</ul></details></div>'''
