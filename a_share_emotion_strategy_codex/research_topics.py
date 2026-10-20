"""Private, dated sector notes displayed independently of trading strategies."""
from __future__ import annotations

import datetime as dt
import html
import math
import re
from urllib.parse import urlsplit

from research_modules import TZ, content_hash, stamp
from research_store import research_path, read_json, atomic_json, update_lock


STOCK_TEXT = ('name', 'group', 'stage', 'returns', 'volume', 'flow', 'support',
              'pressure', 'evidence', 'counter', 'confirmation', 'invalidation', 'qualification')
RELATED_MODULES = ('hot', 'dragon', 'yichujifa', 'prelaunch')
DECISION_TIERS = ('conditional', 'observe', 'exit')
THS_FUND_REFERENCE_URLS = frozenset((
    'https://data.10jqka.com.cn/funds/hyzjl/',
    'https://data.10jqka.com.cn/funds/hyzjl/field/tradezdf/order/desc/page/1/ajax/1/free/1/',
))


def _decision_tiers(data, codes):
    """Check research priorities independently of strategy ranks and eligibility."""
    if 'decision_tiers' not in data:
        return
    tiers = data['decision_tiers']
    if not isinstance(tiers, list) or len(tiers) != len(DECISION_TIERS):
        raise ValueError('Decision comparison needs exactly three tiers')
    tier_ids, assigned = set(), set()
    for tier in tiers:
        if (not isinstance(tier, dict) or tier.get('id') not in DECISION_TIERS
                or any(not isinstance(tier.get(k), str) for k in ('label', 'summary'))):
            raise ValueError('Invalid decision tier')
        if tier['id'] in tier_ids:
            raise ValueError('Duplicate decision tier')
        tier_ids.add(tier['id'])
        items = tier.get('items')
        if not isinstance(items, list) or len(items) > len(codes):
            raise ValueError('Invalid decision tier items')
        ranks = set()
        for item in items:
            if (not isinstance(item, dict) or any(not isinstance(item.get(k), str)
                    for k in ('code', 'action', 'reason', 'conditions'))):
                raise ValueError('Invalid decision comparison item')
            code, rank = item['code'], item.get('rank')
            if code not in codes or code in assigned:
                raise ValueError('Unresolved or duplicate decision stock')
            if type(rank) is not int or rank < 1 or rank in ranks:
                raise ValueError('Invalid or duplicate decision priority')
            position = item.get('position_status', 'unknown')
            if position not in ('held', 'not_held', 'unknown'):
                raise ValueError('Invalid decision position status')
            if tier['id'] == 'exit' and position == 'unknown':
                raise ValueError('Exit comparison must distinguish holdings and candidates')
            if item.get('eligible', False) is not False:
                raise ValueError('Decision comparison cannot grant trading eligibility')
            assigned.add(code)
            ranks.add(rank)


def _source(source, generated):
    if not isinstance(source, dict) or any(not isinstance(source.get(k), str) for k in
                                          ('label', 'url', 'published_at', 'observation_period')):
        raise ValueError('Invalid source')
    urlsplit(source['url'])
    retrieved = stamp(source.get('retrieved_at'))
    if (source.get('retrieved_at') is not None and not retrieved) or (retrieved and retrieved > generated):
        raise ValueError('Source retrieved after research')


def _historical_fund_reference(data, cutoff, generated):
    """Validate optional historical THS evidence without granting a current list."""
    if 'historical_fund_reference' not in data:
        return
    reference = data['historical_fund_reference']
    if (data.get('topic_id') != 'sector-fund-pulse'
            or data.get('status') != 'unavailable' or data.get('directions') != []
            or not isinstance(reference, dict)):
        raise ValueError('Historical funds belong only to an unavailable empty current fund topic')
    if (reference.get('provider') != 'ths' or reference.get('source_policy') != 'ths-only'
            or reference.get('coverage_kind') != 'publisher_top10'
            or 'fund_source_time' not in reference or reference['fund_source_time'] is not None
            or not isinstance(reference.get('source_url'), str)
            or reference.get('source_url') not in THS_FUND_REFERENCE_URLS):
        raise ValueError('Invalid historical THS source policy or time quality')
    for record in (data, reference):
        if any(record.get(key, False) is not False for key in
               ('eligible', 'current_eligible', 'ranking_complete', 'reference_complete')):
            raise ValueError('Historical funds cannot grant current qualifications')
    try:
        raw_time = reference.get('sampled_at')
        if not isinstance(raw_time, str):
            raise ValueError('Missing historical fund capture time')
        source_capture = dt.datetime.fromisoformat(raw_time.replace('Z', '+00:00'))
        if source_capture.tzinfo is None or source_capture.utcoffset() is None:
            raise ValueError('Historical fund capture needs an explicit timezone')
    except (TypeError, ValueError) as exc:
        raise ValueError('Invalid historical fund capture time') from exc
    if not source_capture < cutoff or not source_capture < generated:
        raise ValueError('Historical fund capture is not older than the current attempt')
    rows = reference.get('rows')
    if not isinstance(rows, list) or len(rows) != 10:
        raise ValueError('Historical THS publisher reference requires ten original rows')
    codes = set()
    def finite_number(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        try:
            return math.isfinite(value)
        except OverflowError:
            return False
    for rank, row in enumerate(rows, 1):
        if (not isinstance(row, dict) or type(row.get('rank')) is not int
                or row['rank'] != rank or not isinstance(row.get('name'), str)
                or not row['name'].strip() or not isinstance(row.get('code'), str)
                or not re.fullmatch(r'881\d{3}', row['code']) or row['code'] in codes):
            raise ValueError('Invalid historical THS order or industry identity')
        net, change = row.get('net_cny'), row.get('change_pct')
        if not finite_number(net) or net < 0:
            raise ValueError('Invalid historical THS native fund amount')
        if change is not None and not finite_number(change):
            raise ValueError('Invalid historical THS auxiliary quote')
        if any(row.get(key, False) is not False for key in
               ('eligible', 'current_eligible', 'ranking_complete', 'reference_complete')):
            raise ValueError('Historical fund rows cannot grant current qualifications')
        codes.add(row['code'])


def validate(data, now):
    if not isinstance(data, dict) or data.get('schema_version') != 1:
        raise ValueError('Invalid topic schema')
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,39}', data.get('topic_id', '')):
        raise ValueError('Invalid topic identifier')
    cutoff, generated, expiry = (stamp(data.get(k)) for k in ('as_of', 'generated_at', 'valid_until'))
    if not cutoff or not generated or not expiry or not cutoff <= generated <= now or expiry < cutoff:
        raise ValueError('Invalid topic clocks')
    information = stamp(data.get('information_as_of'))
    if 'information_as_of' in data and (not information or not cutoff <= information <= generated):
        raise ValueError('Invalid topic information cutoff')
    if data.get('status') not in ('complete', 'partial', 'unavailable'):
        raise ValueError('Invalid topic status')
    if data.get('as_of_kind', 'market') not in ('market', 'capture', 'delayed'):
        raise ValueError('Invalid topic clock kind')
    if data.get('related_module') not in (None, *RELATED_MODULES):
        raise ValueError('Invalid related module')
    related = data.get('related_modules', [])
    if (not isinstance(related, list) or any(not isinstance(x, str) or x not in RELATED_MODULES for x in related)
            or len(related) != len(set(related))):
        raise ValueError('Invalid related modules')
    for key in ('title', 'summary'):
        if not isinstance(data.get(key), str):
            raise ValueError('Missing topic text')
    for key in ('changes', 'missing'):
        if not isinstance(data.get(key), list) or any(not isinstance(x, str) for x in data[key]):
            raise ValueError('Invalid topic notes')
    context = data.get('context_notes', [])
    if not isinstance(context, list) or len(context) > 30 or any(not isinstance(x, str) for x in context):
        raise ValueError('Invalid decision context notes')
    rows = data.get('directions')
    if not isinstance(rows, list) or len(rows) > 40:
        raise ValueError('Invalid topic directions')
    for row in rows:
        if not isinstance(row, dict) or any(not isinstance(row.get(k), str) for k in
                                          ('name', 'industry_state', 'market_state', 'evidence', 'counter', 'next')):
            raise ValueError('Invalid direction')
    _historical_fund_reference(data, cutoff, generated)
    for source in data.get('sources', []):
        _source(source, generated)
    stocks = data.get('stocks', [])
    if not isinstance(stocks, list) or len(stocks) > 60:
        raise ValueError('Invalid comparison stocks')
    codes, ranks = set(), set()
    for stock in stocks:
        if not isinstance(stock, dict) or any(not isinstance(stock.get(k), str) for k in STOCK_TEXT):
            raise ValueError('Invalid stock description')
        code = stock.get('code', '')
        if not re.fullmatch(r'\d{6}', code) or code in codes:
            raise ValueError('Duplicate or invalid comparison stock')
        codes.add(code)
        price, price_time = stock.get('close'), stamp(stock.get('price_as_of'))
        if isinstance(price, bool) or not isinstance(price, (float, int)) or not math.isfinite(price) or price <= 0:
            raise ValueError('Invalid comparison price')
        if not price_time or price_time > cutoff:
            raise ValueError('Stock price is beyond topic cutoff')
        if stock.get('eligible', False) is not False:
            raise ValueError('Independent comparison cannot grant trading eligibility')
        rank = stock.get('rank')
        if rank is not None:
            key = (stock['group'], rank)
            if type(rank) is not int or not 1 <= rank <= 3 or key in ranks:
                raise ValueError('Invalid or duplicate original top-three rank')
            ranks.add(key)
        if not isinstance(stock.get('sources'), list) or not stock['sources']:
            raise ValueError('Stock comparison needs dated sources')
        for source in stock['sources']:
            _source(source, generated)
    if stocks and any(not isinstance(data.get(k), str) or not data[k] for k in ('comparison_scope', 'comparison_method')):
        raise ValueError('Stock comparison needs scope and method')
    _decision_tiers(data, codes)
    if 'late_day_result' in data:
        if data['topic_id'] != 'late-day':
            raise ValueError('Late-day payload belongs to its own topic')
        from late_day_research import validate_topic
        validate_topic(data)
    return data


def _source_links(sources):
    items = []
    escape = lambda x: html.escape(str(x), quote=True)
    for source in sources:
        url = urlsplit(source['url'])
        if url.scheme not in ('https', 'http') or not url.netloc or url.username or url.password:
            continue
        items.append(f'<li><a href="{escape(source["url"])}" target="_blank" rel="noopener noreferrer">'
                     f'{escape(source["label"])}</a> · 发布 {escape(source["published_at"])}'
                     f' · 观察期 {escape(source["observation_period"])} · 取得 '
                     f'{escape(source.get("retrieved_at") or "原取得时间未记录，不以本次生成时间替代")}</li>')
    return ''.join(items)


def _render_historical_fund_reference(data):
    reference = data.get('historical_fund_reference')
    if reference is None:
        return ''
    escape = lambda value: html.escape(str(value), quote=True)
    rows = []
    for row in reference['rows']:
        change = ('缺失' if row.get('change_pct') is None
                  else f'{row["change_pct"]:+.2f}%')
        rows.append(f'<tr><td>{row["rank"]}</td><td>{escape(row["name"])} '
                    f'{escape(row["code"])}</td><td>{row["net_cny"]/1e8:+.2f}亿元</td>'
                    f'<td>{change}</td></tr>')
    return (f'<details class="historical-fund-reference"><summary>上次成功参考（历史，非本轮）</summary>'
            f'<p>旧采集时间：{escape(reference["sampled_at"])}；资金日期／时刻未知。'
            '此表不是本轮榜单，不补当前数据，不表示实时资金或交易资格。</p>'
            '<p>保留同花顺原站顺序，金额单位为亿元；原生资金净额不等于真实主力账户买卖。'
            '辅助涨幅缺失时留空，不从其他响应补齐。</p>'
            '<div style="overflow-x:auto"><table><thead><tr><th>原站序位</th><th>行业</th>'
            '<th>原生资金净额</th><th>同响应辅助涨幅</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>'
            f'<p><a href="{escape(reference["source_url"])}" target="_blank" '
            'rel="noopener noreferrer">历史参考来源</a></p></details>')


def _stock_comparison(data):
    stocks = data.get('stocks', [])
    if not stocks:
        return ''
    escape = lambda x: html.escape(str(x), quote=True)
    rows, details = [], []
    for stock in stocks:
        anchor = f'topic-{data["topic_id"]}-{stock["code"]}'
        rank = f'原第{stock["rank"]}位' if stock.get('rank') else '对照项'
        rows.append(f'<tr><td>{escape(stock["group"])} · {rank}</td>'
                    f'<td><a href="#{anchor}">{escape(stock["name"])} {stock["code"]}</a></td>'
                    f'<td>{escape(stock["stage"])}</td><td>{stock["close"]:.2f}</td>'
                    f'<td>{escape(stock["returns"])}</td><td>{escape(stock["pressure"])}</td>'
                    f'<td>{escape(stock["qualification"])}</td></tr>')
        fields = [('价格日期', stock['price_as_of']), ('成交与承接', stock['volume']),
                  ('资金持续性', stock['flow']), ('支持依据', stock['evidence']),
                  ('最强反证', stock['counter']), ('支撑观察', stock['support']),
                  ('第一压力', stock['pressure']), ('下一步确认', stock['confirmation']),
                  ('失效条件', stock['invalidation'])]
        body = ''.join(f'<p><strong>{label}：</strong>{escape(value)}</p>' for label, value in fields)
        details.append(f'<article id="{anchor}" class="research-stock"><h4>{escape(stock["name"])} '
                       f'{stock["code"]} · {escape(stock["stage"])}</h4>{body}'
                       f'<details><summary>本股来源与时间</summary><ul>{_source_links(stock["sources"])}</ul></details></article>')
    return f'''<h3>个股比较与确认条件</h3><p>{escape(data['comparison_scope'])}</p>
<p>{escape(data['comparison_method'])}</p><div style="overflow-x:auto"><table><thead><tr>
<th>比较组与顺序</th><th>股票</th><th>阶段</th><th>收盘</th><th>区间表现</th><th>第一压力</th><th>资格与缺口</th>
</tr></thead><tbody>{''.join(rows)}</tbody></table></div>{''.join(details)}'''


def _render_decision_tiers(data):
    if 'decision_tiers' not in data:
        return ''
    escape = lambda x: html.escape(str(x), quote=True)
    stocks = {stock['code']: stock for stock in data.get('stocks', [])}
    tiers = {tier['id']: tier for tier in data['decision_tiers']}
    sections = []
    for tier_id in DECISION_TIERS:
        tier = tiers[tier_id]
        rows = []
        for item in sorted(tier['items'], key=lambda x: x['rank']):
            stock = stocks[item['code']]
            anchor = f'topic-{data["topic_id"]}-{stock["code"]}'
            position = item.get('position_status', 'unknown')
            if tier_id == 'exit':
                treatment = '持仓退出管理' if position == 'held' else '移出候选池（未持仓）'
            else:
                treatment = {'held': '现有持仓复核', 'not_held': '未持仓候选',
                             'unknown': '持仓状态未确认'}[position]
            original = f'原第{stock["rank"]}位' if stock.get('rank') else '无原始前三名次'
            rows.append(f'<tr><td>{item["rank"]}</td><td><a href="#{anchor}">'
                        f'{escape(stock["name"])} {stock["code"]}</a>'
                        f'<small>{escape(stock["group"])} · {original}</small>'
                        f'<small>{treatment}</small></td><td>{escape(item["action"])}</td>'
                        f'<td>{escape(item["reason"])}</td><td>{escape(item["conditions"])}</td></tr>')
        body = ('<div class="decision-table"><table><thead><tr><th>复核顺序</th><th>股票与原名次</th>'
                '<th>处理方向</th><th>晋级或排除理由</th><th>确认／退出条件</th></tr></thead><tbody>'
                + ''.join(rows) + '</tbody></table></div>') if rows else '<p class="muted">本梯队暂空，不补位。</p>'
        sections.append(f'<section class="decision-tier" data-tier-id="{tier_id}">'
                        f'<h4>{escape(tier["label"])} · {len(tier["items"])}项</h4>'
                        f'<p>{escape(tier["summary"])}</p>{body}</section>')
    context = ''.join(f'<li>{escape(x)}</li>' for x in data.get('context_notes', []))
    context_html = f'<ul class="decision-context">{context}</ul>' if context else ''
    return ('<div class="decision-comparison"><h3>三梯队复核与条件</h3>'
            '<p class="notice">截至本专题行情时点，明日盘中确认尚未发生；下列条件未满足时不能称为可买。'
            '三梯队仅为研究复核顺序，不授予交易资格，不覆盖热点原始前三名次。'
            '退出区分别列示持仓退出管理与未持仓候选排除。</p>'
            + context_html + ''.join(sections) + '</div>')


def publish_topic(data, *, now=None):
    now = now or dt.datetime.now(TZ)
    validate(data, now)
    folder = research_path('topics') / data['topic_id']
    digest = content_hash(data)
    with update_lock():
        old = read_json(folder / 'current.json', {})
        if stamp(old.get('generated_at')) and stamp(old['generated_at']) > stamp(data['generated_at']):
            return {'changed': False, 'reason': 'newer research already published'}
        if old and content_hash(old) == digest:
            return {'changed': False, 'reason': 'same evidence'}
        if old:
            atomic_json(folder / 'history' / (content_hash(old) + '.json'), old)
        atomic_json(folder / 'current.json', data)
    return {'changed': True, 'fingerprint': digest}


def render_topics(*, now=None, exclude=(), include=None):
    now = now or dt.datetime.now(TZ)
    escape = lambda x: html.escape(str(x), quote=True)
    sections = []
    for path in sorted(research_path('topics').glob('*/current.json')):
        if path.parent.name in exclude or (include is not None and path.parent.name not in include):
            continue
        try:
            data = validate(read_json(path), now)
        except (ValueError, TypeError, AttributeError, KeyError):
            sections.append('<p class="notice">一份独立专题资料不可用；其他研究继续展示。</p>')
            continue
        if 'late_day_result' in data:
            from late_day_research import render_topic
            sections.append(render_topic(data, now))
            continue
        kind = data.get('as_of_kind', 'market')
        state = ('本轮资料不可用，未发布新结果' if data['status'] == 'unavailable' else
                 '延迟资金参考快照，不作为实时信号' if kind == 'delayed' else
                 '资金时点未知的参考快照，不作为实时信号' if kind == 'capture' else
                 '观察窗口已结束，以下仅为历史研究' if stamp(data['valid_until']) < now else
                 '资料不足，等待核验' if data['status'] != 'complete' else '本轮专题研究已完成')
        rows = ''.join('<tr>' + ''.join(f'<td>{escape(row[k])}</td>' for k in
                       ('name', 'industry_state', 'market_state', 'evidence', 'counter', 'next')) + '</tr>'
                       for row in data['directions'])
        direction_table = ('<div style="overflow-x:auto"><table><thead><tr><th>方向</th><th>产业状态</th>'
                           '<th>市场状态</th><th>依据</th><th>反证与缺口</th><th>下一步</th></tr></thead>'
                           f'<tbody>{rows}</tbody></table></div>') if rows else ''
        changes = ''.join(f'<li>{escape(x)}</li>' for x in data['changes'])
        missing = ''.join(f'<li>{escape(x)}</li>' for x in data['missing'])
        sources = _source_links(data.get('sources', []))
        information = (f' · 资料核验截至 {escape(data["information_as_of"])}（盘后资料不倒填行情判断）'
                       if data.get('information_as_of') else '')
        cutoff_label = {'capture': '采集截至（资金自身时点未知）',
                        'delayed': '延迟资金截至', 'market': '行情截止'}[data.get('as_of_kind', 'market')]
        sections.append(f'''<section class="playbook" id="topic-{escape(data['topic_id'])}">
<h2>{escape(data['title'])}</h2><p class="notice">{state}。静态专题不授予交易资格。</p>
<p>{cutoff_label} {escape(data['as_of'])}{information} · 本次计算 {escape(data['generated_at'])} · 观察期限 {escape(data['valid_until'])}</p>
<p>{escape(data['summary'])}</p><h3>相较上次的变化</h3><ul>{changes}</ul>
{_render_decision_tiers(data)}
{direction_table}
{_render_historical_fund_reference(data)}
{_stock_comparison(data)}
<details><summary>来源时间与未验证条件</summary><ul>{sources}</ul><ul>{missing}</ul></details>
</section>''')
    return ''.join(sections)


def render_topic_links(module, *, now=None):
    """Surface related independent research without adding strategy candidates."""
    now = now or dt.datetime.now(TZ)
    links = []
    for path in sorted(research_path('topics').glob('*/current.json')):
        try:
            data = validate(read_json(path), now)
        except (ValueError, TypeError, AttributeError, KeyError):
            continue
        related = set(data.get('related_modules', []))
        if data.get('related_module'):
            related.add(data['related_module'])
        if module not in related:
            continue
        status = '历史研究' if stamp(data['valid_until']) < now else '独立观察研究'
        cutoff_label = {'capture': '采集截至（资金自身时点未知）',
                        'delayed': '延迟资金截至', 'market': '行情截止'}[data.get('as_of_kind', 'market')]
        links.append(f'<li><a href="#topic-{data["topic_id"]}">{html.escape(data["title"])}</a>'
                     f' · {status} · {cutoff_label} {html.escape(data["as_of"])}</li>')
    return ('<aside class="playbook"><h3>相关专题</h3><p>观察比较与本策略核心资格分别核验。</p><ul>'
            + ''.join(links) + '</ul></aside>') if links else ''
