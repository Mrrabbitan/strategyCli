"""Bounded, dated evidence collection for the private sector radar.

Membership is observed from vendor pages, never reconstructed from a current
roster for an earlier signal date. Provider names are not evidence of normal
trading status, and price history is not announcement clearance.
"""
from __future__ import annotations
import concurrent.futures
import datetime as dt
import hashlib
import html
from html.parser import HTMLParser
import json
import math
import re
import time
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from prelaunch_research import PublicFeed, normalize_bars
from research_store import atomic_json, read_json, research_path, update_lock
from sector_radar_catalog import SECTORS, VERSION
from three_step_research import calendar_payload

TZ = ZoneInfo('Asia/Shanghai')
MAINBOARD_PREFIXES = ('600', '601', '603', '605', '000', '001', '002', '003')
MAX_PAGES = 100  # Guard malformed pagination; never silently truncate a roster.
COLLECTION_BUDGET_SECONDS = 300
MEMBERSHIP_BUDGET_SECONDS = 90
ATTEMPT_CACHE_SECONDS = 600


class _DeadlineFeed:
    def __init__(self, feed, deadline):
        self.feed, self.deadline = feed, deadline

    def read(self, url, encoding='utf-8'):
        if time.monotonic() >= self.deadline:
            raise TimeoutError('本轮采集时间预算耗尽，未请求的证据保留待验证')
        return self.feed.read(url, encoding)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _time(value):
    if isinstance(value, dt.datetime):
        return value.astimezone(TZ) if value.tzinfo else value.replace(tzinfo=TZ)
    out = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return out.astimezone(TZ) if out.tzinfo else out.replace(tzinfo=TZ)


def _read(feed, url, encoding='utf-8', attempts=2):
    errors = []
    for _ in range(attempts):
        try:
            body, source = feed.read(url, encoding)
            if not isinstance(body, str) or not body.strip():
                raise ValueError('Empty response')
            if not source.get('retrieved_at'):
                raise ValueError('Missing acquisition timestamp')
            _time(source['retrieved_at'])
            return body, source
        except Exception as exc:
            errors.append(type(exc).__name__)
    raise ValueError('有限重试失败：' + ', '.join(errors))


class _Table(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0; self.rows = []; self.row = None; self.cell = None
        self.inputs = {}; self.info = ''; self.in_info = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input' and attrs.get('id'):
            self.inputs[attrs['id']] = attrs.get('value', '')
        if tag == 'span' and 'page_info' in attrs.get('class', '').split():
            self.in_info = True
        if tag == 'table':
            if self.depth or 'm-pager-table' in attrs.get('class', '').split():
                self.depth += 1
        if self.depth and tag == 'tr':
            self.row = []
        if self.depth and tag == 'td':
            self.cell = []

    def handle_data(self, value):
        if self.cell is not None:
            self.cell.append(value)
        if self.in_info:
            self.info += value

    def handle_endtag(self, tag):
        if tag == 'span':
            self.in_info = False
        if self.depth and tag == 'td' and self.cell is not None:
            if self.row is not None:
                self.row.append(''.join(self.cell).strip())
            self.cell = None
        if self.depth and tag == 'tr' and self.row:
            self.rows.append(self.row); self.row = None
        if tag == 'table' and self.depth:
            self.depth -= 1


def parse_ths_page(body, *, expected_page, provider_code, index_code=None):
    """Reject login/challenge pages, wrong board IDs and pagination substitutions."""
    parsed = _Table(); parsed.feed(body)
    match = re.fullmatch(r'\s*(\d+)\s*/\s*(\d+)\s*', parsed.info)
    if not match:
        raise ValueError('同花顺分页信息缺失')
    page, pages = map(int, match.groups())
    if page != expected_page or not 1 <= pages <= MAX_PAGES or page > pages:
        raise ValueError('同花顺分页返回错误或超出完整采集安全界限')
    query = parsed.inputs.get('requestQuery')
    if query and query != 'code/' + provider_code:
        raise ValueError('同花顺板块代码冲突')
    clid = parsed.inputs.get('clid')
    if index_code and clid and clid != index_code:
        raise ValueError('同花顺指数标识冲突')
    if expected_page == 1 and not query:
        raise ValueError('同花顺板块身份未确认')
    members = []
    for cells in parsed.rows:
        if len(cells) < 3 or not cells[0].isdigit() or not re.fullmatch(r'\d{6}', cells[1]):
            raise ValueError('同花顺成分行字段无效')
        if not cells[2]:
            raise ValueError('同花顺成分名称缺失')
        members.append({'code': cells[1], 'name': cells[2], 'ordinal': int(cells[0])})
    if not members:
        raise ValueError('无成分数据，不能区分有效空板块和接口异常')
    return {'page': page, 'pages': pages, 'members': members}


def _validate_members(members, expected):
    if not isinstance(expected, int) or expected < 0 or expected > 20000:
        raise ValueError('成分总数无效')
    codes = [str(m.get('code', '')) for m in members]
    if len(members) != expected or len(set(codes)) != expected:
        raise ValueError('分页总数或唯一代码不一致')
    if any(not re.fullmatch(r'\d{6}', c) for c in codes):
        raise ValueError('证券代码格式冲突')
    if any(not isinstance(m.get('name'), str) or not m['name'].strip() for m in members):
        raise ValueError('证券名称缺失')


def parse_em_page(body, *, page, page_size=100):
    value = json.loads(body)
    node = value.get('data')
    if value.get('rc') not in (None, 0) or not isinstance(node, dict):
        raise ValueError('东财成分接口未返回有效data')
    total = node.get('total')
    if isinstance(total, bool) or not isinstance(total, int) or not 0 <= total <= 20000:
        raise ValueError('东财成分总数无效')
    original = node.get('diff')
    if isinstance(original, dict):
        original = list(original.values())
    if original is None and total == 0:
        original = []
    if not isinstance(original, list):
        raise ValueError('东财成分数组缺失')
    if len(original) != max(0, min(page_size, total - (page - 1) * page_size)):
        raise ValueError('东财页内数量不符')
    members = []
    for row in original:
        code, market = str(row.get('f12', '')), row.get('f13')
        # Market 0 includes Shenzhen and some Beijing listings. Do not infer
        # mainboard eligibility from market alone; excluded securities stay here.
        if market not in (0, 1, 2) or not re.fullmatch(r'\d{6}', code):
            raise ValueError('东财证券身份字段缺失')
        if not isinstance(row.get('f14'), str) or not row['f14'].strip():
            raise ValueError('东财证券名称无效')
        stamp = row.get('f124')
        source_time = dt.datetime.fromtimestamp(stamp, TZ).isoformat() if isinstance(stamp, (int, float)) and stamp > 0 else None
        members.append({'code': code, 'name': row.get('f14'), 'vendor_market': market,
                        'quote_source_time': source_time})
    return {'expected_count': total, 'members': members}


def _base_sector(spec, provider, code, sources, members, *, expected=None, missing=None, complete=False):
    dates = {_time(s['retrieved_at']).date().isoformat() for s in sources if s.get('retrieved_at')}
    observed = next(iter(dates)) if len(dates) == 1 else None
    return {'id': spec['id'], 'label': spec['label'], 'provider': provider, 'provider_code': code,
            'approximate': provider == 'eastmoney',
            'difference': spec['em_difference'] if provider == 'eastmoney' else '同花顺原分类；多分类交叉归属保留。',
            'membership_as_of': observed, 'membership_date_basis': 'observed_current_not_historical',
            'retrieved_at': max((s['retrieved_at'] for s in sources), default=None),
            'verified': False, 'membership_complete': complete, 'expected_count': expected,
            'members': members, 'sources': sources, 'missing': missing or []}


def collect_ths(spec, feed):
    sources, members, missing, expected = [], [], [], None
    try:
        body, src = _read(feed, spec['ths_url'], 'gb18030'); sources.append(src)
        first = parse_ths_page(body, expected_page=1, provider_code=spec['ths_code'], index_code=spec['ths_index'])
        members.extend(first['members'])
        size = len(first['members'])
        for page in range(2, first['pages'] + 1):
            url = ('https://q.10jqka.com.cn/' + spec['ths_kind'] + '/detail/field/199112/order/desc/'
                   + f'page/{page}/ajax/1/code/' + spec['ths_code'])
            body, src = _read(feed, url, 'gb18030'); sources.append(src)
            got = parse_ths_page(body, expected_page=page, provider_code=spec['ths_code'])
            if got['pages'] != first['pages']:
                raise ValueError('同花顺分页数量在采集中变化')
            if page < first['pages'] and len(got['members']) != size:
                raise ValueError('同花顺非末页条数不一致')
            members.extend(got['members'])
        expected = members[-1]['ordinal']
        if [m['ordinal'] for m in members] != list(range(1, expected + 1)):
            raise ValueError('同花顺页序或末页总数不连续')
        _validate_members(members, expected)
        if first['pages'] > 1:
            # A gain-sorted web table can move while paging; require a stable
            # first page, unique codes and continuous ordinals, otherwise partial.
            body, src = _read(feed, spec['ths_url'], 'gb18030'); sources.append(src)
            repeated = parse_ths_page(body, expected_page=1, provider_code=spec['ths_code'], index_code=spec['ths_index'])
            if repeated != first:
                raise ValueError('同花顺成分页在采集中发生变化，不能确认完整截面')
    except Exception as exc:
        missing.append(str(exc))
    result = _base_sector(spec, 'ths', spec['ths_code'], sources, members, expected=expected,
                          missing=missing, complete=not missing)
    result['count_basis'] = 'pagination_last_ordinal'  # Not an invented vendor total.
    return result


def collect_eastmoney(spec, feed):
    sources, members, missing, expected = [], [], [], None
    if not spec.get('em_code'):
        return _base_sector(spec, 'eastmoney', None, [], [], missing=[spec['em_difference']])
    try:
        page = 1
        while True:
            query = {'pn': page, 'pz': 100, 'po': 0, 'np': 1, 'fltt': 2, 'invt': 2,
                     'fid': 'f12', 'fs': 'b:' + spec['em_code'], 'fields': 'f12,f13,f14,f124'}
            url = 'https://push2.eastmoney.com/api/qt/clist/get?' + urlencode(query)
            body, src = _read(feed, url); sources.append(src)
            got = parse_em_page(body, page=page)
            if expected is not None and expected != got['expected_count']:
                raise ValueError('东财成分总数在采集中变化')
            expected = got['expected_count']; members.extend(got['members'])
            if page * 100 >= expected:
                break
            page += 1
            if page > MAX_PAGES:
                raise ValueError('东财分页超出完整采集安全界限')
        _validate_members(members, expected)
    except Exception as exc:
        missing.append(str(exc))
    result = _base_sector(spec, 'eastmoney', spec['em_code'], sources, members,
                          expected=expected, missing=missing, complete=not missing)
    result['provider_label'] = spec['em_label']; result['count_basis'] = 'vendor_total'
    return result


def _cache_write(kind, ident, day, payload):
    if not re.fullmatch(r'[a-zA-Z0-9-]+', ident) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
        raise ValueError('Invalid internal cache key')
    path = research_path('sector_radar/cache') / kind / day / (ident + '.json')
    wrapper = {'catalog_version': VERSION, 'kind': kind, 'id': ident, 'as_of': day,
               'sha256': digest(payload), 'payload': payload}
    with update_lock():
        atomic_json(path, wrapper)


def _cache_read(kind, ident, day):
    path = research_path('sector_radar/cache') / kind / day / (ident + '.json')
    value = read_json(path, {})
    payload = value.get('payload')
    if (isinstance(payload, dict) and value.get('catalog_version') == VERSION
            and value.get('kind') == kind and value.get('id') == ident and value.get('as_of') == day
            and value.get('sha256') == digest(payload)):
        return payload
    return None


def collect_sector(spec, signal, feed):
    now = dt.datetime.now(TZ)
    attempted = _cache_read('membership-attempt', spec['id'], now.date().isoformat())
    cache_recent = False
    if attempted and attempted.get('attempted_at'):
        age = (now - _time(attempted['attempted_at'])).total_seconds()
        cache_recent = 0 <= age <= ATTEMPT_CACHE_SECONDS
    if cache_recent:
        ths = attempted
        ths['cache_used'] = True
        ths['cache_note'] = '复用同次短时采集尝试，保留原获取时间、失败状态和分页进度；不称重新更新。'
    else:
        ths = collect_ths(spec, feed)
        ths['attempted_at'] = dt.datetime.now(TZ).isoformat()
        _cache_write('membership-attempt', spec['id'], now.date().isoformat(), ths)
    chosen = ths
    attempts = [{'provider': 'ths', 'complete': ths['membership_complete'],
                 'received': len(ths['members']), 'members': ths['members'], 'missing': ths['missing'], 'sources': ths['sources']}]
    if not ths['membership_complete'] and spec.get('em_code'):
        em = collect_eastmoney(spec, feed)
        attempts.append({'provider': 'eastmoney', 'complete': em['membership_complete'],
                         'received': len(em['members']), 'members': em['members'], 'missing': em['missing'], 'sources': em['sources']})
        if em['membership_complete'] or len(em['members']) > len(ths['members']):
            chosen = em
    chosen['provider_attempts'] = attempts
    chosen['catalog_version'] = VERSION
    observed = chosen['membership_as_of']
    chosen['verified'] = chosen['membership_complete'] and observed == signal
    chosen['fingerprint'] = digest({k: v for k, v in chosen.items() if k != 'fingerprint'})
    if chosen['membership_complete'] and observed:
        _cache_write('membership', spec['id'], observed, chosen)
    if not chosen['verified']:
        cached = _cache_read('membership', spec['id'], signal)
        if cached and cached.get('membership_complete') and cached.get('membership_as_of') == signal:
            cached = dict(cached, verified=True, cache_used=True, provider_attempts=attempts)
            cached['cache_note'] = '复用原信号日完整成分快照，保留原始取得时间；今日读取不是今日更新。'
            cached['fingerprint'] = digest({k: v for k, v in cached.items() if k != 'fingerprint'})
            return cached
        chosen['missing'] = list(chosen['missing']) + ['未取得信号日完整成分快照；当前观察的成分不能回填历史日期。']
    chosen['fingerprint'] = digest({k: v for k, v in chosen.items() if k != 'fingerprint'})
    return chosen


def security_evidence(code, name, *, signal, source=None):
    """Static board identity is separate from date-specific exchange status."""
    main = code.startswith(MAINBOARD_PREFIXES) and len(code) == 6 and code.isdigit()
    known_a = main or code.startswith(('300', '301', '688', '689', '4', '8', '920'))
    risk_name = 'ST' in name.upper()
    return {'verified': False, 'date': signal, 'mainboard': main,
            'st': True if risk_name else None, 'suspended': None, 'delisting': True if '退' in name else None,
            'normal_limit': None, 'security_type': 'a_share' if known_a else 'unknown', 'source': source,
            'identity_basis': '证券代码板别初核；供应商简称仅作风险线索，不证明当日完整交易状态',
            'missing': ['当日ST/停复牌/退市整理/正常涨跌幅限制缺完整证券状态证据']}


def parse_em_daily(body, code, signal):
    node = (json.loads(body).get('data') or {})
    if node.get('code') != code:
        raise ValueError('日线证券代码冲突或缺失')
    bars = []
    for line in node.get('klines', []):
        fields = line.split(',')
        if len(fields) != 11:
            raise ValueError('东财日线字段数冲突')
        if fields[0] > signal:
            continue
        turnover = float(fields[10])
        if not math.isfinite(turnover) or turnover < 0:
            raise ValueError('日线原生换手字段无效')
        bars.append({'date': fields[0], 'open': fields[1], 'close': fields[2], 'high': fields[3],
                     'low': fields[4], 'volume_shares': float(fields[5]) * 100,
                     'amount_cny': fields[6], 'turnover_pct': turnover})
    bars = normalize_bars(bars, signal)
    if not bars or bars[-1]['date'] != signal:
        raise ValueError('日线末日不等于信号日')
    if any(b['amount_cny'] is None or b['amount_cny'] <= 0 for b in bars):
        raise ValueError('日线原生成交额无效')
    return bars


def _daily_url(code, signal, index=False):
    secid = '1.000300' if index else ('1.' if code.startswith('6') else '0.') + code
    return 'https://push2his.eastmoney.com/api/qt/stock/kline/get?' + urlencode({
        'secid': secid, 'fields1': 'f1,f2,f3,f4,f5,f6',
        'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61',
        'klt': '101', 'fqt': '1', 'end': signal.replace('-', ''), 'lmt': '210'})


def _complete_daily(bars, expected):
    return len(bars) >= 65 and [b['date'] for b in bars[-65:]] == expected[-65:]


def collect_daily(code, signal, expected, feed, *, eastmoney_available=True, index=False):
    missing, sources, bars, provider = [], [], [], None
    if eastmoney_available:
        try:
            body, src = _read(feed, _daily_url(code, signal, index)); sources.append(src)
            bars = parse_em_daily(body, '000300' if index else code, signal)
            provider = 'eastmoney'
        except Exception as exc:
            missing.append('东财日线：' + str(exc))
    else:
        missing.append('东财日线连接探测失败，开启本轮有界降级')
    if not bars:
        cached = _cache_read('daily', 'index000300' if index else code, signal)
        if cached and cached.get('verified') and cached.get('as_of') == signal:
            return dict(cached, cache_used=True, acquisition_missing=missing)
        try:
            sym = 'sh000300' if index else ('sh' if code.startswith('6') else 'sz') + code
            url = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?' + urlencode({'param': sym + ',day,,,210,qfq'})
            body, src = _read(feed, url); sources.append(src)
            node = json.loads(body).get('data', {}).get(sym) or {}
            original = node.get('qfqday') or node.get('day') or []
            bars = normalize_bars([{'date': r[0], 'open': r[1], 'close': r[2], 'high': r[3], 'low': r[4],
                                    'volume_shares': float(r[5]) * 100, 'amount_cny': None}
                                   for r in original if len(r) >= 6 and r[0] <= signal], signal)
            provider = 'tencent-qfq'
            if not index:
                url = 'https://d.10jqka.com.cn/v4/line/hs_' + code + '/01/last.js'
                body, src = _read(feed, url); sources.append(src)
                match = re.fullmatch(r'\s*[\w.]+\((.*)\)\s*;?\s*', body, re.S)
                if not match or ('line_hs_' + code + '_01') not in body.split('(')[0]:
                    raise ValueError('交叉日线证券标识冲突')
                raw = json.loads(match[1]); cross = []
                for line in raw.get('data', '').split(';'):
                    f = line.split(',')
                    if len(f) < 7:
                        continue
                    day = dt.datetime.strptime(f[0], '%Y%m%d').date().isoformat()
                    if day <= signal:
                        cross.append({'date': day, 'open': f[1], 'high': f[2], 'low': f[3], 'close': f[4],
                                      'volume_shares': f[5], 'amount_cny': f[6]})
                cross = {b['date']: b for b in normalize_bars(cross, signal)}
                if not _complete_daily(bars, expected):
                    raise ValueError('65个连续完整交易日日线不足')
                for b in bars[-65:]:
                    other = cross.get(b['date'])
                    if (not other or any(abs(b[k] - other[k]) > .011 for k in ('open', 'high', 'low', 'close'))
                            or abs(b['volume_shares'] - other['volume_shares']) > max(100, b['volume_shares'] * .002)
                            or not other.get('amount_cny')):
                        raise ValueError('交叉65日复权价格/成交股数不一致，不混用成交额')
                for b in bars:
                    if b['date'] in cross:
                        b['amount_cny'] = cross[b['date']]['amount_cny']
                provider = 'tencent-qfq+ths-cross-verified'
        except Exception as exc:
            missing.append('腾讯/同花顺交叉日线：' + str(exc))
    verified = (_complete_daily(bars, expected) and bool(bars) and bars[-1]['date'] == signal
                and (index or all(b.get('amount_cny') and b['amount_cny'] > 0 for b in bars[-65:])))
    observation_dates = {_time(src['retrieved_at']).date().isoformat() for src in sources if src.get('retrieved_at')}
    adjustment_as_of = next(iter(observation_dates)) if len(observation_dates) == 1 else None
    result = {'verified': bool(verified), 'provider': provider, 'adjustment': 'qfq',
              'cross_verified': bool(verified and provider == 'tencent-qfq+ths-cross-verified'),
              'adjustment_as_of': adjustment_as_of,
              'adjustment_date_basis': 'observed_current_vendor_snapshot_not_historical_factor_publication',
              'adjustment_note': '同一供应商截面前复权；未来日K已截断，历史重放须另核当时可得复权因子',
              'as_of': signal if bars and bars[-1]['date'] == signal else (bars[-1]['date'] if bars else None),
              'bars': bars, 'sources': sources, 'missing': [] if verified else missing,
              'acquisition_missing': missing, 'volume_unit': '股', 'amount_unit': '人民币元'}
    result['fingerprint'] = digest(result)
    if verified:
        _cache_write('daily', 'index000300' if index else code, signal, result)
    return result


def collect(as_of, *, feed=None):
    started = time.monotonic()
    at = _time(as_of)
    signal, calendar = calendar_payload(at)
    expected = [d for d in calendar['days'] if d <= signal]
    folder = research_path('sector_radar/collection') / dt.datetime.now(TZ).strftime('%Y%m%dT%H%M%S%f')
    feed = feed or PublicFeed(timeout=5, raw_directory=folder / 'raw')
    membership_feed = _DeadlineFeed(feed, started + MEMBERSHIP_BUDGET_SECONDS)
    history_feed = _DeadlineFeed(feed, started + COLLECTION_BUDGET_SECONDS)
    result = {'schema_version': 1, 'catalog_version': VERSION, 'signal_date': signal,
              'requested_at': at.isoformat(), 'calendar': calendar, 'sectors': [], 'stocks': [],
              'benchmark': {}, 'sources': [], 'missing': [], 'acquisition': {}}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        tasks = {pool.submit(collect_sector, spec, signal, membership_feed): spec for spec in SECTORS}
        found = {}
        for future in concurrent.futures.as_completed(tasks):
            spec = tasks[future]
            try:
                found[spec['id']] = future.result()
            except Exception as exc:
                found[spec['id']] = _base_sector(spec, 'unknown', None, [], [], missing=[type(exc).__name__ + ': ' + str(exc)])
        result['sectors'] = [found[s['id']] for s in SECTORS]
    union = {}
    conflicts = set()
    for sector in result['sectors']:
        result['sources'].extend(sector['sources'])
        for row in sector['members']:
            code = row['code']
            if code in union and union[code]['name'] != row['name']:
                conflicts.add(code)
            union.setdefault(code, {'code': code, 'name': row['name'], 'sectors': []})['sectors'].append(sector['id'])
    # A single bounded endpoint probe prevents repeating a systematic disconnect
    # hundreds of times. Fallback runs for every potentially eligible stock.
    potential = sorted(c for c in union if c.startswith(MAINBOARD_PREFIXES) and 'ST' not in union[c]['name'].upper() and '退' not in union[c]['name'])
    em_available = True
    if potential:
        try:
            body, src = _read(history_feed, _daily_url(potential[0], signal))
            result['sources'].append(src)
            parse_em_daily(body, potential[0], signal)
        except Exception as exc:
            em_available = False
            result['missing'].append('东财日线探测失败，逐股改用已核验双源或精确日期缓存；不估算成交额。')
    def stock(code):
        item = dict(union[code]); item['sectors'] = sorted(set(item['sectors']))
        item['security'] = security_evidence(code, item['name'], signal=signal)
        item['announcement'] = {'verified': False, 'as_of': signal, 'hard_risk': None, 'sources': [],
                                'missing': ['公告风险未完成原文与发布时间核验；不把缺公告当无风险']}
        if code in conflicts:
            item['security']['missing'].append('成分供应商名称冲突')
        if code in potential:
            try:
                item['daily'] = collect_daily(code, signal, expected, history_feed, eastmoney_available=em_available)
            except Exception as exc:
                item['daily'] = {'verified': False, 'as_of': None, 'bars': [], 'sources': [],
                                 'missing': ['独立日线采集失败：' + type(exc).__name__]}
        else:
            item['daily'] = {'verified': False, 'as_of': None, 'bars': [], 'sources': [],
                             'missing': ['板别或简称存在明确排除线索，保留原成员但不扩大历史请求']}
        return item
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        result['stocks'] = list(pool.map(stock, sorted(union)))
    try:
        result['benchmark'] = collect_daily('000300', signal, expected, history_feed, eastmoney_available=em_available, index=True)
    except Exception as exc:
        result['benchmark'] = {'verified': False, 'as_of': None, 'bars': [], 'sources': [],
                               'missing': ['沪深300独立采集失败：' + type(exc).__name__]}
    result['benchmark'].update(code='000300', name='沪深300')
    result['sources'].extend(result['benchmark'].get('sources', []))
    result['acquisition'] = {'sectors_expected': len(SECTORS),
                             'membership_complete': sum(s['membership_complete'] for s in result['sectors']),
                             'membership_signal_verified': sum(s['verified'] for s in result['sectors']),
                             'union_received': len(union), 'history_requested': len(potential),
                             'history_verified': sum(s['daily']['verified'] for s in result['stocks']),
                             'security_verified': 0, 'announcement_verified': 0,
                             'eastmoney_daily_available': em_available,
                             'budget_seconds': COLLECTION_BUDGET_SECONDS,
                             'elapsed_seconds': round(time.monotonic() - started, 2),
                             'budget_exhausted': time.monotonic() - started >= COLLECTION_BUDGET_SECONDS,
                             'sampling': '所有已取得成分完整保留；全部可能符合主板板别者逐股补历史，时间预算后缺证据逐股待验证，无任意数量截断'}
    if result['acquisition']['budget_exhausted']:
        result['missing'].append('本轮300秒采集时间预算耗尽；未完成的逐股历史仍留名单并标待验证，不称完整筛选。')
    result['missing'].extend(['证券交易状态与公告风险仍须完整原文核验；简称与日线不能代替。'])
    for sector in result['sectors']:
        if sector['missing']:
            result['missing'].append(sector['label'] + '：' + '；'.join(sector['missing']))
    if not result['benchmark']['verified']:
        result['missing'].append('沪深300完整比较窗口不足')
    result['fingerprint'] = digest(result)
    with update_lock():
        atomic_json(folder / 'input.json', result)
    return result
