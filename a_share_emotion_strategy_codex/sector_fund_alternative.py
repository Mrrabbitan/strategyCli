"""Independent public provider adapters; never mix rows or flow definitions.

Sina exposes aggregate cash flow with its own minute dates and same-row quotes.
Its netamount is not EM large-order main flow. THS public HTML exposes aggregate
inflow/outflow in CNY 100 million, but no
market-data timestamp or claim that its net field is large-order main funds.
Consequently even a complete snapshot remains reference_only; HTTP headers and
retrieval clocks must never promote it to a live or half-hour main-fund signal.
"""
from __future__ import annotations

import datetime as dt
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import time

from bs4 import BeautifulSoup


THS_URL = 'https://data.10jqka.com.cn/funds/hyzjl/field/tradezdf/order/desc/page/{page}/ajax/1/free/1/'
TZ = dt.timezone(dt.timedelta(hours=8))
SINA_BASE = 'https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/'
SINA_LIST = SINA_BASE+'MoneyFlow.ssl_bkzj_bk?page=1&num=100&sort=netamount&asc=0&fenlei=0'
SINA_COUNT = SINA_BASE+'MoneyFlow.ssc_bkzj_bk?fenlei=0'
SINA_MINUTES = SINA_BASE+'MoneyFlow.ssx_bkzj_fszs?page=1&num=1000&bankuai={code}&sort=time'


def _number(text: str) -> Decimal:
    try:
        value = Decimal(text.strip().replace(',', '').removesuffix('%'))
    except InvalidOperation as exc:
        raise ValueError('同花顺资金数值缺失或无效') from exc
    if not value.is_finite():
        raise ValueError('同花顺资金数值非有限值')
    return value


def _code(url: str) -> str:
    match = re.search(r'/thshy/detail/code/(\d{6})/', url)
    if not match:
        raise ValueError('同花顺行业身份缺失')
    return match[1]


def parse_ths_page(html: str) -> dict:
    """Parse one unmodified public HTML page; this pure function never networks."""
    soup = BeautifulSoup(html, 'html.parser')
    table = soup.select_one('table.J-ajax-table')
    page = soup.select_one('.page_info')
    if table is None or page is None:
        raise ValueError('同花顺行业表或分页证据缺失')
    match = re.fullmatch(r'\s*(\d+)\s*/\s*(\d+)\s*', page.get_text())
    if not match:
        raise ValueError('同花顺分页格式无效')
    current, total_pages = map(int, match.groups())
    if not 1 <= current <= total_pages:
        raise ValueError('同花顺页码越界')
    headers = [x.get_text(' ', strip=True) for x in table.select('thead th')]
    required = ['序号', '行业', '行业指数', '涨跌幅', '流入资金(亿)',
                '流出资金(亿)', '净额(亿)', '公司家数']
    if headers[:8] != required:
        raise ValueError('同花顺字段或金额单位变化')
    rows = []
    for item in table.select('tbody tr'):
        cells = item.find_all('td', recursive=False)
        if len(cells) != 11:
            raise ValueError('同花顺行业行字段不完整')
        texts = [x.get_text(' ', strip=True) for x in cells]
        link = cells[1].find('a', href=True)
        if link is None or not texts[1]:
            raise ValueError('同花顺行业身份缺失')
        incoming, outgoing, net = [_number(x) for x in texts[4:7]]
        # All three native fields are rounded separately to 0.01 CNY 100m.
        if incoming < 0 or outgoing < 0 or abs(incoming-outgoing-net) > Decimal('0.02'):
            raise ValueError('同花顺流入流出与净额不一致')
        rank, companies = int(texts[0]), int(texts[7])
        if rank < 1 or companies < 1:
            raise ValueError('同花顺序号或成分数量无效')
        rows.append({
            'code': _code(link['href']), 'name': texts[1], 'page_rank': rank,
            'index': float(_number(texts[2])),
            'change_pct': float(_number(texts[3])),
            'inflow_cny': float(incoming*Decimal(100000000)),
            'outflow_cny': float(outgoing*Decimal(100000000)),
            'net_cny': float(net*Decimal(100000000)),
            'company_count': companies, 'fund_source_time': None,
        })
    chart_match = re.search(r'\bvar\s+JS_DATA\s*=\s*(\[.*?\])\s*;', html, re.S)
    if not chart_match:
        raise ValueError('同花顺全行业图表总数证据缺失')
    try:
        chart_raw = json.loads(chart_match[1])
        chart = [{'code': _code(x['addr']), 'name': x['name'],
                  'net_cny': float(_number(str(x['amount']))*Decimal(100000000))}
                 for x in chart_raw]
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError('同花顺全行业图表格式无效') from exc
    if not rows or not chart or len({x['code'] for x in chart}) != len(chart):
        raise ValueError('同花顺空表或图表行业重复')
    if len({x['code'] for x in rows}) != len(rows):
        raise ValueError('同花顺页内行业代码重复')
    return {'page': current, 'total_pages': total_pages, 'rows': rows,
            'chart': chart, 'expected_count': len(chart),
            'native_unit': '亿元', 'fund_source_time': None}


def collect_ths_snapshot(*, session=None, timeout: float = 8, max_pages: int = 4,
                         raw_sink=None, now=None) -> dict:
    """Bounded public collection, with at most max_pages requests and no retries.

    raw_sink(record), when supplied, receives every response for the caller's
    private storage. It is never written into public source files here.
    A complete return still has status reference_only and no delta/main fields.
    """
    if not 0 < timeout <= 15 or not 1 <= max_pages <= 8:
        raise ValueError('同花顺请求限制无效')
    if session is None:
        import requests
        session = requests.Session()
    parsed_pages, raw_fingerprints = [], []
    expected_pages = 1
    for number in range(1, max_pages+1):
        url = THS_URL.format(page=number)
        response = session.get(url, timeout=timeout,
                               headers={'User-Agent': 'Mozilla/5.0'})
        response.encoding = 'gbk'
        retrieved = (now or dt.datetime.now(TZ)).astimezone(TZ).isoformat()
        body = response.text
        fingerprint = hashlib.sha256(body.encode('utf-8')).hexdigest()
        record = {'provider': '同花顺', 'url': url,
                  'retrieved_at': retrieved, 'status': response.status_code,
                  'headers': dict(response.headers), 'body': body,
                  'body_fingerprint': fingerprint}
        if raw_sink:
            raw_sink(record)
        response.raise_for_status()
        parsed = parse_ths_page(body)
        if parsed['page'] != number:
            raise ValueError('同花顺返回页码与请求不符')
        if number == 1:
            expected_pages = parsed['total_pages']
            if expected_pages > max_pages:
                raise ValueError('同花顺分页超过请求预算')
        elif (parsed['total_pages'] != expected_pages or
              parsed['chart'] != parsed_pages[0]['chart']):
            raise ValueError('同花顺分页期间行业全集或资金截面变化')
        parsed_pages.append(parsed)
        raw_fingerprints.append(fingerprint)
        if number == expected_pages:
            break
    rows = [row for page in parsed_pages for row in page['rows']]
    chart = {x['code']: x for x in parsed_pages[0]['chart']}
    if (len(rows) != len(chart) or len({x['code'] for x in rows}) != len(rows) or
            {x['code'] for x in rows} != set(chart) or
            sorted(x['page_rank'] for x in rows) != list(range(1, len(chart)+1))):
        raise ValueError('同花顺分页总数、唯一代码或连续序号不一致')
    for row in rows:
        reference = chart[row['code']]
        if (reference['name'] != row['name'] or
                abs(reference['net_cny']-row['net_cny']) > 1000000):
            raise ValueError('同花顺行业表与全量图表冲突')
    ranked = sorted(rows, key=lambda x: (-x['net_cny'], x['code']))
    return {
        'provider': '同花顺', 'classification': '同花顺行业',
        'status': 'reference_only', 'coverage_complete': True,
        'expected_count': len(chart), 'received_count': len(rows),
        'fund_metric': '流入资金减流出资金；原站未标为大单主力净额',
        'native_unit': '亿元', 'normalized_unit': '元',
        'fund_source_time': None, 'retrieved_at': retrieved,
        'limitation': '资金自身时点未提供；仅供参考，不授予实时或半小时资金资格',
        'rows': ranked, 'top10': ranked[:10], 'half_hour_available': False,
        'source_urls': [THS_URL.format(page=i) for i in range(1, expected_pages+1)],
        'input_fingerprint': hashlib.sha256(
            json.dumps(raw_fingerprints).encode('utf-8')).hexdigest(),
    }


def parse_ths_published_top10(html: str) -> dict:
    """Read the publisher's inflow chart list, never infer a full universe.

    The site's companion chart script labels JS_DATA's nonnegative/negative
    groups as net-inflow/net-outflow top ten. JS_DATA normally has 20 entries;
    that is a publisher shortlist, not the entire industry catalogue. Auxiliary
    quotes are joined only from the table in this exact response.
    """
    parsed = parse_ths_page(html)
    chart, table = parsed['chart'], parsed['rows']
    if parsed['page'] != 1:
        raise ValueError('同花顺原站榜要求第一页原生表及图表')
    if sorted(row['page_rank'] for row in table) != list(range(1, len(table)+1)):
        raise ValueError('同花顺原站榜辅助表序号不连续')
    if any(not re.fullmatch(r'881\d{3}', x['code']) or
           not isinstance(x['name'], str) or not x['name'].strip() for x in chart):
        raise ValueError('同花顺原站图表行业代码或名称无效')
    incoming = [x for x in chart if x['net_cny'] >= 0]
    outgoing = [x for x in chart if x['net_cny'] < 0]
    if len(chart) > 20 or len(incoming) > 10 or len(outgoing) > 10:
        raise ValueError('同花顺原站前十图表数量或正负分组异常')
    if not incoming:
        raise ValueError('同花顺原站图中未取得可核验净流入项')
    by_code = {row['code']: row for row in table}
    # Check every overlap, including outflow entries that will not be displayed.
    for item in chart:
        auxiliary = by_code.get(item['code'])
        if auxiliary is not None and (auxiliary['name'] != item['name'] or
                abs(auxiliary['net_cny']-item['net_cny']) > 1000000):
            raise ValueError('同花顺同一响应表与图表身份或净额不一致')
    rows = []
    for item in incoming:
        auxiliary = by_code.get(item['code'])
        row = {'code': item['code'], 'name': item['name'], 'net_cny': item['net_cny'],
               'fund_source_time': None, 'change_pct': None, 'index': None,
               'inflow_cny': None, 'outflow_cny': None, 'company_count': None,
               'auxiliary_available': auxiliary is not None}
        if auxiliary is not None:
            row.update({k: auxiliary[k] for k in ('change_pct', 'index',
                       'inflow_cny', 'outflow_cny', 'company_count')})
        rows.append(row)
    # Preserve the publisher's order, including ties. A changed native ordering
    # is a schema/definition change to investigate, not permission to re-rank.
    if any(b['net_cny'] > a['net_cny'] for a, b in zip(rows, rows[1:])):
        raise ValueError('同花顺原站净流入前十顺序异常，不自行重排')
    ordered = rows
    return {
        'provider': '同花顺', 'classification': '同花顺行业',
        'status': 'reference_only', 'reference_only': True,
        'time_quality': 'unknown_reference', 'coverage_complete': False,
        'universe_complete': False, 'coverage_kind': 'publisher_top10',
        'publisher_list_verified': True, 'publisher_limit': 10,
        'published_top10_complete': len(ordered) == 10,
        'expected_count': 10, 'industry_expected_count': None,
        'received_count': len(ordered),
        'chart_count': len(chart), 'table_count': len(table),
        'publisher_inflow_count': len(incoming), 'publisher_outflow_count': len(outgoing),
        'auxiliary_count': sum(x['auxiliary_available'] for x in ordered),
        'displayed_total_pages': parsed['total_pages'],
        'coverage_basis': '单响应原站JS_DATA净流入前十及净流出前十图示；expected_count=10仅指原站前十目标项数，未取得完整行业池，没有api.total字段',
        'publisher_definition': '原站配套资金图脚本tooltip标注“资金净流入/流出前10”；未自行重算全行业排名',
        'definition_url': 'https://s.thsi.cn/website/datacenter/funds/zjlx_v1.20260305.js',
        'fund_metric': '原站流入资金减流出资金；未标为大单主力净额',
        'native_unit': '亿元', 'normalized_unit': '元',
        'fund_source_time': None, 'half_hour_available': False,
        'limitation': '同花顺原站发布净流入前十参考；资金自身日期和时点未知，完整行业池未拉取；辅助仅取同一响应匹配记录，不足十项不补位',
        'rows': ordered, 'top10': ordered,
    }


def collect_ths_published_top10(*, session=None, timeout: float = 5,
                               raw_sink=None, now=None) -> dict:
    """One ordinary public GET, no challenge handling, retries or page mixing."""
    if not 0 < timeout <= 15:
        raise ValueError('同花顺请求限制无效')
    if session is None:
        import requests
        session = requests.Session()
    url = THS_URL.format(page=1)
    response = session.get(url, timeout=timeout,
                           headers={'User-Agent': 'Mozilla/5.0'})
    response.encoding = 'gbk'
    retrieved = (now or dt.datetime.now(TZ)).astimezone(TZ).isoformat()
    body = response.text
    fingerprint = hashlib.sha256(body.encode('utf-8')).hexdigest()
    record = {'provider': 'ths', 'kind': 'ths-published-top10', 'url': url,
              'source_url': url, 'retrieved_at': retrieved,
              'status': response.status_code, 'headers': dict(response.headers),
              'body': body, 'source_fingerprint': fingerprint}
    if raw_sink:
        raw_sink(record)
    response.raise_for_status()
    result = parse_ths_published_top10(body)
    result.update(retrieved_at=retrieved, source_urls=[url],
                  input_fingerprint=fingerprint)
    return result


def _session(moment):
    value = moment.time().replace(tzinfo=None)
    if dt.time(9, 30) <= value <= dt.time(11, 30):
        return 'morning'
    if dt.time(13) <= value <= dt.time(15):
        return 'afternoon'
    return None


def _age_limit(max_age_seconds):
    if (isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, (int, float)) or
            not 0 < max_age_seconds <= 900):
        raise ValueError('新浪资金时效上限须大于0且不超过900秒')
    return max_age_seconds


def _fresh(moment, now, max_age_seconds=90):
    max_age_seconds = _age_limit(max_age_seconds)
    if (moment.date() != now.date() or not _session(moment) or
            not 0 <= (now-moment).total_seconds() <= max_age_seconds):
        raise ValueError('新浪资金自身时点非当日有效时段或延迟超过'+str(max_age_seconds)+'秒')


def parse_sina_minutes(payload, code: str, name: str, now, source_url=None,
                       *, max_age_seconds=90) -> dict:
    """Sina net flow is its native aggregate netamount, not EM main-order flow.

    Native ratios are fractions, amounts are CNY. Quotes and cash-flow share
    opendate/ticktime in each native minute row. No turnover amount is exposed.
    """
    max_age_seconds = _age_limit(max_age_seconds)
    if (not isinstance(payload, list) or len(payload) != 2 or
            not isinstance(payload[1], list) or not payload[1]):
        raise ValueError('新浪行业分钟资金结构或记录缺失')
    try:
        count = int(payload[0])
    except (TypeError, ValueError) as exc:
        raise ValueError('新浪分钟总数缺失') from exc
    if count != len(payload[1]) or not 1 <= count <= 1000:
        raise ValueError('新浪分钟分页不完整')
    parsed, cumulative_pairs = [], []
    for original in payload[1]:
        try:
            moment = dt.datetime.fromisoformat(
                original['opendate']+'T'+original['ticktime']).replace(tzinfo=TZ)
            if moment.date() != now.date() or not _session(moment) or moment > now:
                raise ValueError('新浪资金自身日期、交易时段或未来记录无效')
            incoming, outgoing, net = [_number(str(original[k]))
                                      for k in ('inamount', 'outamount', 'netamount')]
            if incoming < 0 or outgoing < 0 or abs(incoming-outgoing-net) > Decimal('.02'):
                raise ValueError('新浪资金流入减流出不等于净额')
            change = float(_number(str(original['avg_changeratio']))*100)
            native_net = float(_number(str(original['ratioamount']))*100)
            parsed.append(dict(time=moment.isoformat(), net_cny=float(net),
                               inflow_cny=float(incoming), outflow_cny=float(outgoing),
                               change_pct=change, native_net_pct=native_net))
            cumulative_pairs.append((incoming, outgoing))
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError('新浪资金自身时间、金额或同刻报价缺失') from exc
    moments = [dt.datetime.fromisoformat(x['time']) for x in parsed]
    if len(set(moments)) != len(moments):
        raise ValueError('新浪分钟资金存在重复时点')
    if all(b < a for a, b in zip(moments, moments[1:])):
        parsed.reverse()
        moments.reverse()
        cumulative_pairs.reverse()
    elif not all(b > a for a, b in zip(moments, moments[1:])):
        raise ValueError('新浪分钟资金时序混乱')
    # Gross cumulative sides must not decrease. A revision/reset changes the
    # basis for every subsequent subtraction; reject this entire industry,
    # rather than selecting an earlier row or manufacturing a negative delta.
    for before, after in zip(cumulative_pairs, cumulative_pairs[1:]):
        if any(current+Decimal('.02') < previous
               for previous, current in zip(before, after)):
            raise ValueError('新浪累计流入或流出回退，存在源修订或重置；该行业整体拒绝')
    latest = moments[-1]
    _fresh(latest, now, max_age_seconds)
    anchor = latest-dt.timedelta(minutes=30)
    window = [x for x in parsed if anchor <= dt.datetime.fromisoformat(x['time']) <= latest]
    delta = None
    if (_session(anchor) == _session(latest) and len(window) == 31 and
            dt.datetime.fromisoformat(window[0]['time']) == anchor and
            all((dt.datetime.fromisoformat(b['time'])-dt.datetime.fromisoformat(a['time'])).total_seconds() == 60
                for a, b in zip(window, window[1:]))):
        delta = window[-1]['net_cny']-window[0]['net_cny']
    last = parsed[-1]
    return {'code': code, 'name': name, 'source_time': latest.isoformat(),
            # Internal common ranking key only; external labels must use flow_label.
            'main_cny': last['net_cny'], 'delta_30m_cny': delta,
            'inflow_cny': last['inflow_cny'], 'outflow_cny': last['outflow_cny'],
            'delta_anchor': anchor.isoformat() if delta is not None else None,
            'quote_time': latest.isoformat(), 'change_pct': last['change_pct'],
            'native_net_pct': last['native_net_pct'], 'turnover_cny': None,
            'quote_main_cny': None, 'quote_valid': True,
            'provider': 'sina', 'flow_label': '新浪行业资金净额',
            'time_quality': 'delayed_reference' if max_age_seconds > 90 else 'strict_current',
            'reference_only': max_age_seconds > 90,
            'max_age_seconds': max_age_seconds,
            'fund_age_seconds': (now-latest).total_seconds(),
            'source_url': source_url or SINA_MINUTES.format(code=code),
            'notes': ['新浪原生净流入率；资金净额非大单主力口径', '原生分钟未提供成交额']+
                     (['延迟资金参考；不具有实时榜或选股资格'] if max_age_seconds > 90 else [])+
                     (['同场连续30分钟锚点不足，仅展示累计'] if delta is None else []),
            'minutes_fingerprint': hashlib.sha256(
                json.dumps(parsed, sort_keys=True).encode()).hexdigest()}


def collect_sina_snapshot(now, clock=None, deadline=None, *, fetch=None,
                          max_age_seconds=90) -> dict:
    """Return isolated Sina 48-industry raw evidence plus normalized ranking rows.

    count/list validate the provider's current universe (not a hardcoded 48).
    clock returns an aware datetime. deadline is an absolute time.monotonic value;
    total collection is capped at 45 seconds, six workers and five-second GETs.
    fetch(url, timeout) may be supplied by offline tests and returns decoded JSON.
    max_age_seconds defaults to the strict 90s cutoff. An explicit value above
    90 (at most 900s) always yields reference_only/delayed_reference, even if a
    particular response happens to be fresh. It never acquires live eligibility.
    The caller may compare only when universe_complete and all rows are present.
    """
    max_age_seconds = _age_limit(max_age_seconds)
    if now.tzinfo is None:
        raise ValueError('新浪分析时点须带时区')
    now = now.astimezone(TZ)
    clock = clock or (lambda: now)
    started = time.monotonic()
    deadline = min(deadline if deadline is not None else started+45, started+45)
    raw, failures, rows, universe = [], [], [], []
    universe_complete = False
    expected_count = None

    def request(url):
        available = deadline-time.monotonic()
        if available <= 0:
            raise TimeoutError('新浪总采集预算用尽')
        timeout = min(5, available)
        if fetch:
            payload = fetch(url, timeout)
        else:
            import requests
            response = requests.get(url, timeout=timeout,
                                    headers={'User-Agent': 'Mozilla/5.0'})
            response.raise_for_status()
            payload = response.json()
        retrieved = clock().astimezone(TZ)
        return payload, {'provider': 'sina', 'url': url, 'source_url': url,
                         'retrieved_at': retrieved.isoformat(), 'data': payload,
                         'source_fingerprint': hashlib.sha256(
                             json.dumps(payload, sort_keys=True).encode()).hexdigest()}

    try:
        count, evidence = request(SINA_COUNT)
        raw.append(dict(evidence, kind='sina-industry-count'))
        if isinstance(count, bool):
            raise ValueError('新浪行业总数无效')
        expected_count = int(count)
        if not 1 <= expected_count <= 100:
            raise ValueError('新浪行业总数超出完整单页覆盖范围')
        listing, evidence = request(SINA_LIST)
        raw.append(dict(evidence, kind='sina-industry-list'))
        if not isinstance(listing, list) or len(listing) != expected_count:
            raise ValueError('新浪行业总数与返回行数不一致')
        for item in listing:
            code, name = item.get('category'), item.get('name')
            if (str(item.get('cate_type')) != '0' or not isinstance(code, str) or
                    not re.fullmatch(r'new_[A-Za-z0-9_]+', code) or
                    not isinstance(name, str) or not name.strip()):
                raise ValueError('新浪行业分类、代码或名称无效')
            universe.append({'code': code, 'name': name})
        if len({x['code'] for x in universe}) != expected_count:
            raise ValueError('新浪行业代码重复')
        universe_complete = True
    except Exception as exc:
        failures.append('新浪行业全集：'+str(exc))
    if universe_complete:
        pool = ThreadPoolExecutor(max_workers=6)
        futures = {pool.submit(request, SINA_MINUTES.format(code=item['code'])): item
                   for item in universe}
        pending = set(futures)
        while pending and time.monotonic() < deadline:
            done, pending = wait(pending, timeout=min(.2, max(0, deadline-time.monotonic())),
                                 return_when=FIRST_COMPLETED)
            for future in done:
                item = futures[future]
                try:
                    payload, evidence = future.result()
                    raw.append(dict(evidence, kind='sina-industry-minutes', code=item['code']))
                    rows.append(parse_sina_minutes(payload, item['code'], item['name'],
                                                   clock().astimezone(TZ), evidence['source_url'],
                                                   max_age_seconds=max_age_seconds))
                except Exception as exc:
                    failures.append(item['code']+' '+item['name']+'：'+str(exc))
        for future in pending:
            future.cancel()
            item = futures[future]
            failures.append(item['code']+' '+item['name']+'：新浪总采集预算用尽')
        pool.shutdown(wait=False, cancel_futures=True)
    finished = clock().astimezone(TZ)
    valid = []
    for row in rows:
        try:
            source_moment = dt.datetime.fromisoformat(row['source_time'])
            _fresh(source_moment, finished, max_age_seconds)
            row['fund_age_seconds'] = (finished-source_moment).total_seconds()
            valid.append(row)
        except ValueError as exc:
            failures.append(row['code']+' '+row['name']+'：最终复核'+str(exc))
    return {'provider': 'sina', 'flow_label': '新浪行业资金净额',
            'time_quality': 'delayed_reference' if max_age_seconds > 90 else 'strict_current',
            'reference_only': max_age_seconds > 90, 'max_age_seconds': max_age_seconds,
            'scope': '新浪行业分类，包含其全部市场成分；不是东财行业或仅主板资金',
            'universe_complete': universe_complete, 'expected_count': expected_count,
            'universe': universe, 'rows': valid, 'raw': raw, 'missing': failures,
            'sampled_at': finished.isoformat(),
            'universe_fingerprint': hashlib.sha256(
                json.dumps(sorted(x['code'] for x in universe)).encode()).hexdigest()
                if universe_complete else None,
            'source_url': 'https://money.finance.sina.com.cn/moneyflow/'}
