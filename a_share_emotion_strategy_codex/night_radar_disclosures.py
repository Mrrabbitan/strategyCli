"""Timestamped public disclosures for the read-only nightly radar.

The vendor's visibility time is not an exchange first-publication timestamp.
Announcement dates and mutable structured tables cannot establish historical
availability on their own. Raw evidence is written only to private storage.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
import re
import time
from zoneinfo import ZoneInfo

import requests

from research_store import atomic_json, research_path

TZ = ZoneInfo('Asia/Shanghai')
NOTICE_URL = 'https://np-anotice-stock.eastmoney.com/api/security/ann'
CONTENT_URL = 'https://np-cnotice-stock.eastmoney.com/api/content/ann'
DATA_URL = 'https://datacenter.eastmoney.com/securities/api/data/v1/get'
REPURCHASE_URL = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
MAX_PAGES = 100
MAX_ORIGINALS = 80
COLLECTION_BUDGET_SECONDS = 120


class _RawTarget:
    def __init__(self, path, *, deadline=None, budget=COLLECTION_BUDGET_SECONDS):
        self.path = Path(path)
        self.deadline = min(deadline if deadline is not None else float('inf'),
                            time.monotonic() + budget)

    def __fspath__(self):
        return str(self.path)


def _time(value):
    if isinstance(value, datetime):
        result = value
    elif value:
        text = str(value).strip().replace('Z', '+00:00')
        text = re.sub(r'(\d{2}:\d{2}:\d{2}):(\d+)$', r'\1.\2', text)
        try:
            result = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    return result.replace(tzinfo=TZ) if result.tzinfo is None else result.astimezone(TZ)


def _number(value):
    if value is None or isinstance(value, bool) or str(value).strip() in ('', '-', '--'):
        return None
    try:
        result = Decimal(str(value).replace(',', ''))
        return float(result) if result.is_finite() else None
    except InvalidOperation:
        return None


def _fingerprint(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _fetch(url, params, raw_dir, label, *, method='get'):
    errors = []
    for attempt in range(2):
        remaining = getattr(raw_dir, 'deadline', float('inf')) - time.monotonic()
        if remaining <= 0:
            errors.append('收集时间预算已用完')
            break
        try:
            if method == 'post':
                response = requests.post(url, data=params, timeout=min(12, remaining),
                                         headers={'User-Agent': 'Mozilla/5.0'})
            else:
                response = requests.get(url, params=params, timeout=min(12, remaining),
                                        headers={'User-Agent': 'Mozilla/5.0'})
            response.raise_for_status()
            payload = response.json()
            atomic_json(Path(raw_dir) / (label + '.json'), {
                'url': response.url, 'params': params,
                'retrieved_at': datetime.now(TZ).isoformat(), 'payload': payload,
                'fingerprint': _fingerprint(payload),
            })
            return payload
        except (requests.RequestException, ValueError) as exc:
            errors.append(type(exc).__name__ + ': ' + str(exc)[:180])
    atomic_json(Path(raw_dir) / (label + '-failure.json'), {'url': url, 'errors': errors})
    raise RuntimeError('; '.join(errors))


def _notice_pages(start, cutoff, raw_dir):
    params = {'sr': '-1', 'page_size': '100', 'page_index': '1', 'ann_type': 'A',
              'client_source': 'web', 'f_node': '0', 's_node': '0',
              'begin_time': start.date().isoformat(),
              'end_time': (cutoff.date() + timedelta(days=1)).isoformat()}
    first = _fetch(NOTICE_URL, params, raw_dir, 'notice-page-001')
    if first.get('success') != 1 or not isinstance(first.get('data'), dict):
        raise RuntimeError('公告列表未返回有效数据结构')
    data = first['data']
    total = data.get('total_hits')
    if not isinstance(total, int) or total < 0:
        raise RuntimeError('公告列表缺少可核验总数')
    pages = max(1, math.ceil(total / 100))
    if pages > MAX_PAGES:
        raise RuntimeError('公告分页超过单次上限，需缩小补录窗口')
    rows = list(data.get('list') or [])
    missing = []

    def load(page):
        try:
            result = _fetch(NOTICE_URL, dict(params, page_index=str(page)), raw_dir,
                            'notice-page-%03d' % page)
            part = result.get('data') or {}
            if result.get('success') != 1 or part.get('total_hits') != total:
                raise RuntimeError('总数变化或分页失败')
            return page, part.get('list') or [], None
        except RuntimeError as exc:
            return page, [], str(exc)

    with ThreadPoolExecutor(max_workers=4) as pool:
        for page, part, error in pool.map(load, range(2, pages + 1)):
            rows.extend(part)
            if error:
                missing.append('公告第%d页：%s' % (page, error))
    unique, conflicts = {}, set()
    for row in rows:
        key = row.get('art_code')
        if not key:
            missing.append('公告缺少编号')
        elif key in unique and unique[key] != row:
            missing.append('同编号公告内容冲突：' + key)
            conflicts.add(key)
        else:
            unique[key] = row
    if len(rows) != total or len(unique) != total:
        missing.append('公告分页不完整：接收%d条、唯一%d条、源总数%d条' %
                       (len(rows), len(unique), total))
    selected, unknown_time, later_versions, unresolved = [], 0, 0, []
    for row in unique.values():
        if row.get('art_code') in conflicts:
            unresolved.append({'id': row['art_code'], 'reason': 'source_conflict'})
            continue
        source_time = _time(row.get('display_time'))
        version_time = _time(row.get('eiTime'))
        if source_time is None:
            unknown_time += 1
            unresolved.append({'id': row['art_code'], 'reason': 'missing_source_time'})
        elif start <= source_time < cutoff and version_time and version_time >= cutoff:
            later_versions += 1
            unresolved.append({'id': row['art_code'], 'reason': 'later_content_version'})
        elif start <= source_time < cutoff:
            selected.append(row)
    if unknown_time:
        missing.append('%d条公告缺少有效源展示时间，未纳入截止时点' % unknown_time)
    if later_versions:
        missing.append('%d条公告的供应商内容版本晚于截止时点，未倒填历史' % later_versions)
    coverage = {'source_total': total, 'unique_notices': len(unique),
                'eligible_timestamped_notices': len(selected),
                'query_notice_dates': [params['begin_time'], params['end_time']],
                'visibility_window': [start.isoformat(), cutoff.isoformat()],
                'collection_complete': not missing, 'unresolved_ids': unresolved,
                'time_basis': '东方财富display_time；不等于交易所首次披露时间'}
    return selected, missing, coverage


def _data_pages(report, url, params, raw_dir, label):
    params = dict(params, reportName=report, columns='ALL', pageSize='500', pageNumber='1')
    first = _fetch(url, params, raw_dir, label + '-001')
    result = first.get('result')
    if first.get('success') is not True or not isinstance(result, dict):
        # Some data-center tables report a legitimate no-result with code 9201.
        if first.get('code') == 9201 and '无' in str(first.get('message', '')):
            return [], []
        raise RuntimeError(label + '未返回有效分页结果')
    total, pages = result.get('count'), result.get('pages')
    if not isinstance(total, int) or not isinstance(pages, int) or pages > MAX_PAGES:
        raise RuntimeError(label + '分页总数不可核验')
    rows = list(result.get('data') or [])
    missing = []
    for page in range(2, pages + 1):
        try:
            item = _fetch(url, dict(params, pageNumber=str(page)), raw_dir,
                          label + '-%03d' % page)
            part = item.get('result') or {}
            if item.get('success') is not True or part.get('count') != total:
                raise RuntimeError('分页总数变化')
            rows.extend(part.get('data') or [])
        except RuntimeError as exc:
            missing.append(label + '第%d页失败：%s' % (page, exc))
    if len(rows) != total:
        missing.append(label + '分页不完整')
    return rows, missing


def _original(art_code, raw_dir):
    payload = _fetch(CONTENT_URL, {'art_code': art_code, 'client_source': 'web',
                                 'page_index': '1'}, raw_dir, 'original-' + art_code)
    data = payload.get('data') or {}
    if payload.get('success') != 1 or data.get('art_code') != art_code:
        raise RuntimeError('公告正文编号不匹配')
    pages = int(data.get('page_size') or 1)
    if pages > 30:
        raise RuntimeError('原文过长，保留链接待人工复核')
    text = data.get('notice_content') or ''
    for page in range(2, pages + 1):
        part = _fetch(CONTENT_URL, {'art_code': art_code, 'client_source': 'web',
                                  'page_index': str(page)}, raw_dir,
                      'original-' + art_code + '-%02d' % page)
        body = part.get('data') or {}
        if part.get('success') != 1 or body.get('art_code') != art_code:
            raise RuntimeError('公告正文分页不完整')
        text += '\n' + (body.get('notice_content') or '')
    if len(text.strip()) < 60 or data.get('is_ai_summary') == 1:
        raise RuntimeError('公告正文缺失或仅有AI摘要')
    return {'text': text, 'pdf_url': data.get('attach_url'),
            'body_fingerprint': _fingerprint(text), 'notice_title': data.get('notice_title'),
            'source_time': data.get('eitime')}


RISK_WORDS = ('减持', '立案', '处罚', '诉讼', '仲裁', '停牌', '终止', '亏损', '预亏',
              '业绩下修', '下调', '债务逾期', '退市', '风险警示', '首亏', '预减')
OPPORTUNITY_WORDS = ('合同', '订单', '中标', '定点', '收购', '投资建设', '认证', '注册证',
                     '增持', '回购', '预增', '扭亏', '业绩预告', '筹划重大资产')
IMPORTANT_WORDS = RISK_WORDS + OPPORTUNITY_WORDS + ('设立', '产能', '产品', '进展', '合作协议', '增资', '对外投资')


def _market_repurchase_title(title):
    # Incentive-share cancellation and supporting legal opinions are not a
    # market buyback plan or its execution record.
    return ('回购' in title and not any(word in title for word in (
        '注销完成', '注销部分', '法律意见', '董事会意见',
        '核查意见', '独立董事', '激励计划', '限制性股票', '律师')))


def _procedural_announcement(title):
    if any(word in title for word in ('法律意见', '核查意见', '股东大会', '股东会',
                                     '前十大股东', '前十名股东', '前10大股东', '前10名股东')):
        return True
    if '转债' in title and any(word in title for word in ('转股', '停牌')):
        return True
    if '减持' in title and '届满' in title and any(word in title for word in ('未减持', '未实施')):
        return True
    return False


def _classification(title):
    if _procedural_announcement(title):
        return 'neutral', 99
    if '终止' in title and any(word in title for word in ('激励', '员工持股')):
        return 'neutral', 4
    if any(word in title for word in RISK_WORDS):
        return 'risk', 0
    if any(word in title for word in ('重大合同', '订单', '中标', '业绩预告', '回购')):
        return 'opportunity', 1
    if any(word in title for word in OPPORTUNITY_WORDS):
        return 'opportunity', 2
    return 'neutral', 3


def _base(notice, code_info):
    code = str(code_info.get('stock_code', ''))
    source_time = _time(notice.get('display_time'))
    return {'code': code, 'name': code_info.get('short_name') or code,
            'event_id': notice['art_code'] + ':' + code,
            'source_url': 'https://data.eastmoney.com/notices/detail/%s/%s.html' %
                          (code, notice['art_code']),
            'source_time': source_time.isoformat() if source_time else None,
            'source_time_basis': '供应商展示时间',
            'published_date': str(notice.get('notice_date') or '')[:10],
            'source_version': _fingerprint(notice), 'notice_id': notice['art_code'],
            'version_time': (_time(notice.get('eiTime')).isoformat()
                             if _time(notice.get('eiTime')) else None)}


def _plain(text):
    return re.sub(r'\s+', '', text).replace(',', '').replace('，', '')


def _has_amount(text, amount):
    if amount is None:
        return False
    compact = _plain(text)
    value = Decimal(str(amount))
    # The source PDF tables commonly declare the unit once in the header.
    scales = [Decimal(1)]
    if '万元' in compact:
        scales.append(Decimal(10000))
    if '亿元' in compact:
        scales.append(Decimal(100000000))
    for scale in scales:
        token = format(value / scale, 'f').rstrip('0').rstrip('.') if '.' in format(value / scale, 'f') else format(value / scale, 'f')
        if re.search(r'(?<![\d.])' + re.escape(token) + r'(?:\.0+)?(?![\d.])', compact):
            return True
    return False


def _period(text, report_date):
    # This structured table reports year-to-date metrics. A single-quarter
    # excerpt in the same document must never rename its cumulative values.
    year = str(report_date)[:4]
    suffix = {'03-31': '年第一季度', '06-30': '年半年度',
              '09-30': '年前三季度', '12-31': '年度'}.get(str(report_date)[5:10], '')
    return year + suffix if suffix else str(report_date)[:10]


def _profit_segment(text, report_date, low, high):
    """Locate the cumulative attributable-profit section, not global numbers."""
    compact = _plain(text)
    year, month = str(report_date)[:4], str(report_date)[5:7]
    expected = {'03': '第一季度', '06': '半年度', '09': '前三季度', '12': '年度'}.get(month)
    if not expected:
        return None
    markers = []
    for match in re.finditer(year + r'年(前三季度|第三季度|第一季度|半年度|上半年|度)(?:业绩预告|业绩预计|业绩预增|主要财务数据)', compact):
        period = match.group(1)
        markers.append((match.start(), '半年度' if period == '上半年' else ('年度' if period == '度' else period)))
    for match in re.finditer(year + r'年第三季度(?!报告)', compact):
        markers.append((match.start(), '单季'))
    for match in re.finditer(year + r'年([178])月1日(?:至|—|-)' + year + r'年(3|6|9|12)月(?:30|31)日', compact):
        if match.group(1) == '1':
            name = {'3': '第一季度', '6': '半年度', '9': '前三季度', '12': '年度'}[match.group(2)]
        else:
            name = '单季'
        markers.append((match.start(), name))
    markers.sort()
    # PDF text extraction can interleave numeric columns inside a wrapped row
    # label. Match that one bounded row, then reject any deducted-profit label.
    anchor = r'(?:归属于(?:上市|母).{0,100}?净利润|归母净利润)'
    matches = list(re.finditer(anchor, compact))
    for match in matches:
        if '扣除' in match.group() or '非经常性' in match.group():
            continue
        prior = [(position, name) for position, name in markers if position < match.start()]
        if not prior or prior[-1][1] != expected:
            continue
        previous_boundary = prior[-1][0]
        next_period = next((p for p, _ in markers if p > match.end()), len(compact))
        next_basis = next((m.start() for m in matches if m.start() > match.start()), len(compact))
        deduction = compact.find('扣除非经常性', match.end())
        end = min(next_period, next_basis, match.end() + 500,
                  deduction if deduction >= 0 else len(compact))
        segment = compact[max(previous_boundary, match.start() - 90):end]
        # Units may be declared in a table header rather than inside the row.
        header_units = compact[max(previous_boundary, match.start() - 180):match.start()]
        if _has_amount(segment + header_units, low) and _has_amount(segment + header_units, high):
            return segment + header_units
    return None


def _historical_progress(title, text):
    """Use the subject of this notice, never an old plan quoted in its body."""
    compact = _plain(text)
    if re.search(r'回购.{0,8}(?:完成|结果)', title):
        return '完成实施'
    if '终止' in title or '停止实施' in title:
        return '停止实施'
    if '调整' in title:
        return '调整方案（实施中）' if re.search(r'(?:累计|已经|已实际).{0,30}回购', compact) else '调整方案'
    if re.search(r'回购.{0,16}(?:进展|进度|实施情况)|首次.{0,8}回购', title):
        return '实施中'
    if ('预案' in title or re.search(r'回购(?:公司)?股份(?:的)?方案|回购报告书', title)) and '董事会' in compact and '审议通过' in compact:
        return '董事会预案'
    if '股东' in title and '审议通过' in compact:
        return '股东大会通过'
    return None


def _repurchase_amounts(title, text):
    """Parse semantic budget/actual blocks; mutable feed columns are not budgets."""
    compact = _plain(text)
    unit_scale = {'元': Decimal(1), '万元': Decimal(10000), '亿元': Decimal(100000000)}
    def money(number, unit):
        return float(Decimal(number) * unit_scale[unit])
    pair = (r'不(?:低|少)于(?:人民币)?([\d.]+)(亿元|万元|元)'
            r'.{0,35}?不(?:超过|高于|多于)(?:人民币)?([\d.]+)(亿元|万元|元)')
    pairs = []
    for match in re.finditer(pair, compact):
        context = compact[max(0, match.start() - 80):match.start()]
        if '回购' in context and not re.search(r'回购(?:股份)?数量[^。；]*$', context):
            pairs.append((money(match[1], match[2]), money(match[3], match[4])))
    for match in re.finditer(r'(?:预计回购金额|拟回购金额|回购预算).{0,12}?'
                             r'([\d.]+)(亿元|万元|元)(?:至|到|~|～|—|-)([\d.]+)(亿元|万元|元)', compact):
        pairs.append((money(match[1], match[2]), money(match[3], match[4])))
    # An amendment may quote both old and new budgets. Only the explicit new
    # range after "adjusted to" can disambiguate; no latest-number heuristic.
    adjusted = []
    for marker in re.finditer(r'调整(?:至|为)', compact):
        preceding = compact[max(0, marker.start() - 130):marker.start()]
        if not re.search(r'回购(?:股份)?(?:的)?资金总额|回购金额|回购预算', preceding):
            continue
        match = re.search(pair, compact[marker.end():marker.end() + 130])
        if match:
            adjusted.append((money(match[1], match[2]), money(match[3], match[4])))
    choices = set(adjusted or pairs)
    budget = next(iter(choices)) if len(choices) == 1 else (None, None)
    if budget[0] is not None and budget[0] > budget[1]:
        budget = (None, None)
    actual_values = []
    for match in re.finditer(r'(?:实际回购金额|累计已回购金额|成交总(?:金)?额|支付的总金额)'
                             r'(?:为|合计为|合计)?(?:人民币)?([\d.]+)(亿元|万元|元)', compact):
        start = max(0, match.start() - 280)
        context = compact[start:match.start()]
        if '回购' not in context:
            continue
        actual_values.append((money(match[1], match[2]), match[2]))
    # Exact-yuan lines take precedence over rounded summary boxes; conflicting
    # equally precise actual amounts remain unresolved rather than guessed.
    exact = {v for v, unit in actual_values if unit == '元'}
    amounts = exact or {v for v, _ in actual_values}
    actual = next(iter(amounts)) if len(amounts) == 1 else None
    return budget[0], budget[1], actual


def _repurchase_price_cap(text):
    compact = _plain(text)
    matches = []
    for m in re.finditer(r'回购(?:股份)?价格(?:上限)?(?:为|不超过|调整为|调整至)?(?:人民币)?([\d.]+)元', compact):
        matches.append(float(m[1]))
    adjusted = re.findall(r'(?:回购(?:股份)?价格上限).{0,45}?调整(?:为|至)(?:人民币)?([\d.]+)元', compact)
    if adjusted:
        values = set(map(float, adjusted))
        return next(iter(values)) if len(values) == 1 else None
    values = set(matches)
    return next(iter(values)) if len(values) == 1 else None


def _section(rows, missing, sources, coverage, *, unavailable=False, collection_complete=False):
    return {'status': 'unavailable' if unavailable else ('partial' if missing else 'complete'),
            'rows': rows, 'missing': list(dict.fromkeys(missing)), 'sources': sources,
            'coverage': coverage, 'collection_complete': bool(collection_complete),
            'unresolved_ids': coverage.get('unresolved_ids', [])}


def collect_disclosures(cutoff, window_start=None, raw_dir=None):
    """Return three independent sections; failures never masquerade as empty pools.

    Query two days behind ``window_start`` for late arrivals. The caller decides
    whether an unseen older version is a backfill, and handles delivery deduping.
    """
    cutoff = _time(cutoff)
    if cutoff is None:
        raise ValueError('cutoff must be a timestamp')
    window_start = _time(window_start) or cutoff - timedelta(days=1)
    if window_start >= cutoff:
        raise ValueError('window_start must precede cutoff')
    lookback = window_start - timedelta(days=2)
    raw_dir = _RawTarget(Path(raw_dir) if raw_dir else research_path('night_radar/raw/' + cutoff.strftime('%Y%m%d-%H%M%S')))
    sources = [{'label': '东方财富公告原始列表（展示时间及完整分页）', 'url': NOTICE_URL},
               {'label': '公司公告原文（东方财富存档）', 'url': 'https://data.eastmoney.com/notices/hsa/5.html'}]
    # Fetch all native lists before reading lengthy originals. Each list owns a
    # bounded budget; a busy announcement day cannot starve financial tables.
    def get_list(kind):
        target = _RawTarget(raw_dir, deadline=raw_dir.deadline, budget=60)
        try:
            if kind == 'notices':
                return _notice_pages(lookback, cutoff, target), None
            if kind == 'earnings':
                result = _data_pages('RPT_PUBLIC_OP_NEWPREDICT', DATA_URL,
                    {'sortColumns': 'NOTICE_DATE,SECURITY_CODE', 'sortTypes': '-1,-1',
                     'filter': "(NOTICE_DATE>='%s')(NOTICE_DATE<='%s')(PREDICT_FINANCE_CODE=\"004\")" %
                               (lookback.date(), cutoff.date() + timedelta(days=1))}, target, 'earnings')
            else:
                result = _data_pages('RPTA_WEB_GETHGLIST_NEW', REPURCHASE_URL,
                    {'sortColumns': 'UPD,DIM_DATE,DIM_SCODE', 'sortTypes': '-1,-1,-1',
                     'source': 'WEB', 'filter': "(UPDATEDATE>='%s')(UPDATEDATE<='%s')" %
                     (lookback.date(), cutoff.date() + timedelta(days=1))}, target, 'repurchases')
            return result, None
        except (RuntimeError, TypeError, ValueError) as exc:
            return None, str(exc)

    with ThreadPoolExecutor(max_workers=3) as pool:
        listing = dict(zip(('notices', 'earnings', 'repurchases'),
                           pool.map(get_list, ('notices', 'earnings', 'repurchases'))))
    notice_result, notice_error = listing['notices']
    if notice_error:
        return {key: _section([], [notice_error], sources, {}, unavailable=True)
                for key in ('announcements', 'earnings', 'repurchases')}
    notices, shared_missing, coverage = notice_result
    coverage['requested_window_start'] = window_start.isoformat()
    coverage['lookback_days'] = 2
    notices_by_code = {}
    selected = []
    for notice in notices:
        for info in notice.get('codes') or []:
            if str(info.get('ann_type', '')).startswith('A') and re.fullmatch(r'\d{6}', str(info.get('stock_code', ''))):
                notices_by_code.setdefault(info['stock_code'], []).append((notice, info))
                if (any(word in notice.get('title', '') for word in IMPORTANT_WORDS)
                        and not _procedural_announcement(notice.get('title', ''))):
                    selected.append((notice, info))
    selected.sort(key=lambda x: (_classification(x[0].get('title', ''))[1],
                                 -_time(x[0]['display_time']).timestamp(), x[1]['stock_code']))
    # Prioritize financial originals over boilerplate replies or legal opinions.
    def target_order(n):
        mainboard = any(str(c.get('stock_code', '')).startswith(('00', '60')) for c in n.get('codes', []))
        current = _time(n['display_time']) >= window_start
        return (0 if current and mainboard else (1 if current else 2),
                0 if any(k in n.get('title', '') for k in
                                           ('业绩预告', '业绩预增', '回购', '采购合同', '供应合同')) else 1,
                _classification(n.get('title', ''))[1], -_time(n['display_time']).timestamp())
    ordered_targets = sorted({n['art_code']: n for n, _ in selected}.values(), key=target_order)
    financial_targets = [n for n in ordered_targets if
        any(k in n.get('title', '') for k in ('业绩预告', '业绩预增', '业绩预亏'))
        or _market_repurchase_title(n.get('title', ''))][:MAX_ORIGINALS // 2]
    financial_ids = {n['art_code'] for n in financial_targets}
    announcement_targets = [n for n in ordered_targets if n['art_code'] not in financial_ids][
        :MAX_ORIGINALS - len(financial_targets)]
    originals, original_errors = {}, {}

    def read_group(targets):
        target = _RawTarget(raw_dir, deadline=raw_dir.deadline, budget=60)
        def read_original(notice):
            key = notice['art_code']
            try:
                body = _original(key, target)
                version_time = _time(body.get('source_time'))
                if version_time and version_time >= cutoff:
                    raise RuntimeError('当前原文版本晚于截止时点')
                return key, body, None
            except (RuntimeError, TypeError, ValueError) as exc:
                return key, None, str(exc)
        with ThreadPoolExecutor(max_workers=4) as pool:
            return list(pool.map(read_original, targets))

    # Separate original-reading groups guarantee financial evidence a share of
    # the remaining time, even when many long legal/contract documents exist.
    with ThreadPoolExecutor(max_workers=2) as pool:
        for results in pool.map(read_group, (financial_targets, announcement_targets)):
            for key, result, error in results:
                if result:
                    originals[key] = result
                else:
                    original_errors[key] = error
    ann_rows = []
    for notice, info in selected:
        base = _base(notice, info)
        original = originals.get(notice['art_code'])
        title = notice.get('title', '')
        stance, priority = _classification(title)
        text = original['text'] if original else ''
        compact = _plain(text)
        if '业绩' in title and any(term in compact for term in ('预计亏损', '预计净利润为负值', '首亏', '预减', '下修')):
            stance, priority = 'risk', 0
        kind = '正式公告'
        summary = title.split(':', 1)[-1].split('：', 1)[-1]
        if '采购合同' in title:
            summary += '；公司为采购方，合同金额不等于销售收入'
        elif '定点' in title:
            summary += '；生命周期预计金额不等于已确认收入'
        if not original:
            summary += '（仅核验公告标题）'
        base.update(title=title, summary=summary, stance=stance, priority=priority,
                    kind=kind, verified=bool(original),
                    evidence_status='原文已取得' if original else '仅标题',
                    verification_missing=original_errors.get(notice['art_code']),
                    pdf_url=original.get('pdf_url') if original else None)
        if original:
            base['source_version'] = original['body_fingerprint']
        ann_rows.append(base)
    ann_missing = list(shared_missing)
    ann_missing.append('公司互动问答未接入完整可核验列表；本栏为正式公告关键词筛选，不能覆盖截图全部互动消息')
    if len(originals) < len({n['art_code'] for n, _ in selected}):
        ann_missing.append('部分公告仅核验标题；精选原文最多%d份，未核验不作内容结论' % MAX_ORIGINALS)

    earnings, earnings_missing, earnings_failed, earnings_collected = [], list(shared_missing), False, False
    try:
        financial_result, financial_error = listing['earnings']
        if financial_error:
            raise RuntimeError(financial_error)
        financial, gaps = financial_result
        earnings_missing += gaps
        earnings_collected = not gaps
        seen = set()
        for item in financial:
            code = str(item.get('SECURITY_CODE') or '')
            notice_date = str(item.get('NOTICE_DATE') or '')[:10]
            possible = [(n, i) for n, i in notices_by_code.get(code, [])
                        if str(n.get('notice_date', ''))[:10] == notice_date
                        and any(word in n.get('title', '') for word in ('业绩预告', '业绩预增', '业绩预亏'))]
            if len(possible) != 1:
                if possible:
                    earnings_missing.append(code + '同日多份预告，未确认结构化指标对应版本')
                continue
            notice, info = possible[0]
            original = originals.get(notice['art_code'])
            text = original['text'] if original else ''
            low, high = _number(item.get('PREDICT_AMT_LOWER')), _number(item.get('PREDICT_AMT_UPPER'))
            segment = _profit_segment(text, item.get('REPORT_DATE'), low, high) if original else None
            verified = bool(segment)
            # Unverified mutable values cannot be presented as historical facts.
            if not verified:
                earnings_missing.append(code + '预告金额未与时点内原文核对，保留公告线索')
                continue
            base = _base(notice, info)
            key = (base['event_id'], str(item.get('REPORT_DATE')))
            if key in seen:
                continue
            seen.add(key)
            changes = [_number(item.get('ADD_AMP_LOWER')), _number(item.get('ADD_AMP_UPPER'))]
            for index, value in enumerate(changes):
                if value is not None and not _has_amount(segment, value):
                    changes[index] = None
                    earnings_missing.append(code + '同比幅度未在同期间归母段核验，置为未知')
            base.update(period_label=_period(text, item.get('REPORT_DATE')),
                        report_date=str(item.get('REPORT_DATE'))[:10], profit_low_cny=low,
                        profit_high_cny=high, change_low_pct=changes[0], change_high_pct=changes[1],
                        forecast_type=item.get('PREDICT_TYPE'), basis='归母净利润', estimated=False,
                        verified=True, pdf_url=original.get('pdf_url'),
                        source_version=original['body_fingerprint'],
                        forecast_notice='公司业绩预告，未经审计；非实际业绩')
            earnings.append(base)
            if low is not None and (low < 0 or (_number(item.get('ADD_AMP_UPPER')) or 0) < 0):
                for ann in ann_rows:
                    if ann['event_id'] == base['event_id']:
                        ann.update(stance='risk', priority=0)
        represented = {row['notice_id'] for row in earnings}
        other = {n['art_code'] for n, _ in selected if any(k in n.get('title', '') for k in
                                                        ('业绩预告', '业绩预增', '业绩预亏'))} - represented
        if other:
            earnings_missing.append('%d份预告未形成已核验归母区间（含单季或结构化覆盖缺失）；保留公告原文' % len(other))
    except (RuntimeError, TypeError, ValueError) as exc:
        earnings_missing.append(str(exc))
        earnings_failed = True

    repurchases, repurchase_missing, repurchase_failed, repurchase_collected = [], list(shared_missing), False, False
    try:
        buyback_result, buyback_error = listing['repurchases']
        if buyback_error:
            raise RuntimeError(buyback_error)
        buybacks, gaps = buyback_result
        repurchase_missing += gaps
        repurchase_collected = not gaps
        progress = {'001': '董事会预案', '002': '股东大会通过', '003': '股东大会否决',
                    '004': '实施中', '005': '停止实施', '006': '完成实施'}
        for item in buybacks:
            code = str(item.get('DIM_SCODE') or '')
            possible = [(n, i) for n, i in notices_by_code.get(code, [])
                        if str(n.get('notice_date', ''))[:10] == str(item.get('UPDATEDATE', ''))[:10]
                        and _market_repurchase_title(n.get('title', ''))]
            if len(possible) != 1:
                if possible:
                    repurchase_missing.append(code + '回购原文版本有歧义')
                continue
            notice, info = possible[0]
            original = originals.get(notice['art_code'])
            text = original['text'] if original else ''
            if not original:
                repurchase_missing.append(code + '回购原文未取得，保留公告线索')
                continue
            low, high, actual = _repurchase_amounts(notice.get('title', ''), text)
            cap = _repurchase_price_cap(text)
            if low is None or high is None:
                repurchase_missing.append(code + '计划预算区间未在原文预算段核验，置为未知')
            base = _base(notice, info)
            stage = _historical_progress(notice.get('title', ''), text)
            vendor_stage = progress.get(str(item.get('REPURPROGRESS')))
            if not stage or (vendor_stage and stage != vendor_stage):
                repurchase_missing.append(code + '结构化进度未与当时公告阶段一致，仅使用原文阶段')
            if stage not in ('实施中', '完成实施', '调整方案（实施中）'):
                actual = None
            # Unknown stage or unknown financial fields cannot receive a fully
            # verified badge. An actual-only record may still be useful.
            verified = bool(stage and ((low is not None and high is not None) or actual is not None))
            purpose = '见原文'
            compact = _plain(text)
            purpose_match = re.search(r'(?:本次拟回购的股份|回购股份用途|回购用途|本次回购的股份|回购的股份|本次回购股份).{0,40}?(?:用于|用作)([^。；]{1,100})', compact)
            purpose_text = purpose_match.group(1) if purpose_match else ''
            for phrase, label in [('转换公司发行的可转换', '用于可转债转股'),
                                  ('员工持股', '用于员工持股或股权激励'),
                                  ('股权激励', '用于股权激励'), ('减少注册资本', '注销并减少注册资本')]:
                if phrase in purpose_text:
                    purpose = label
                    break
            base.update(budget_low_cny=low, budget_high_cny=high, price_cap=cap, actual_cny=actual,
                        progress=stage or '进度待核验',
                        purpose=purpose, verified=verified, original_verified=True,
                        budget_verified=low is not None and high is not None,
                        actual_verified=actual is not None, progress_verified=stage is not None,
                        pdf_url=original.get('pdf_url'),
                        source_version=original['body_fingerprint'])
            repurchases.append(base)
    except (RuntimeError, TypeError, ValueError) as exc:
        repurchase_missing.append(str(exc))
        repurchase_failed = True
    return {
        'announcements': _section(ann_rows, ann_missing, sources, coverage,
                                  collection_complete=coverage.get('collection_complete', False)),
        'earnings': _section(earnings, earnings_missing, sources + [
            {'label': '东方财富业绩预告结构化数据；金额以原文核验', 'url': 'https://data.eastmoney.com/bbsj/yjyg.html'}],
            coverage, unavailable=earnings_failed and not earnings,
            collection_complete=coverage.get('collection_complete', False) and earnings_collected),
        'repurchases': _section(repurchases, repurchase_missing, sources + [
            {'label': '东方财富回购计划；不等于实际回购', 'url': 'https://data.eastmoney.com/gphg/hglist.html'}],
            coverage, unavailable=repurchase_failed and not repurchases,
            collection_complete=coverage.get('collection_complete', False) and repurchase_collected),
    }
