"""Same-vendor fallback for the complete public industry selector.

Only identities come from HTML. Its render time is never used as a quote or
fund-flow timestamp. Quotes retain stock/get's native f86 clock; capital always
comes from the separate minute-flow source in sector_fund_pulse.
"""
import concurrent.futures
import datetime as dt
import hashlib
from html.parser import HTMLParser
import json
import re
import time
from urllib.request import Request, urlopen

CATALOG_URLS = ('https://data.eastmoney.com/bkzj/hy.html',
                'https://data.eastmoney.com/bkzj/gn.html')


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


class _Selector(HTMLParser):
    """Read complete, closed HTML selector groups, not unrelated BK links."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.div_depth = 0
        self.pop_depth = None
        self.pop_count = 0
        self.closed = False
        self.slider = False
        self.li = None
        self.headers = []
        self.group_depth = None
        self.groups = []
        self.anchor = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get('class', '').split()
        if tag == 'div':
            self.div_depth += 1
            if 'bkselectorpop' in classes:
                self.pop_count += 1
                self.pop_depth = self.div_depth
            elif self.pop_depth is not None and 'pop-cont' in classes:
                self.group_depth = self.div_depth
                self.groups.append([])
        if self.pop_depth is None:
            return
        if tag == 'ul' and 'pop_slider' in classes:
            self.slider = True
        elif tag == 'li' and self.slider:
            self.li = []
        elif tag == 'a' and self.group_depth is not None:
            self.anchor = [attrs.get('href', ''), []]

    def handle_data(self, data):
        if self.li is not None:
            self.li.append(data)
        if self.anchor is not None:
            self.anchor[1].append(data)

    def handle_endtag(self, tag):
        if tag == 'li' and self.li is not None:
            self.headers.append(''.join(self.li).strip())
            self.li = None
        elif tag == 'ul':
            self.slider = False
        elif tag == 'a' and self.anchor is not None:
            href, parts = self.anchor
            self.groups[-1].append((href, ''.join(parts).strip()))
            self.anchor = None
        elif tag == 'div':
            if self.group_depth == self.div_depth:
                self.group_depth = None
            if self.pop_depth == self.div_depth:
                self.pop_depth = None
                self.closed = True
            self.div_depth -= 1


def parse_catalog_html(html):
    """Return a classified, complete navigation snapshot or reject it.

    There is no vendor numeric total in this document. Completeness is the
    entire explicit, non-paginated industry navigation, independently matched
    against a second vendor page. It must not be described as clist.total.
    """
    if not isinstance(html, str) or '</html>' not in html.lower():
        raise ValueError('行业目录HTML未完整闭合')
    parser = _Selector()
    parser.feed(html)
    parser.close()
    if parser.pop_count != 1 or not parser.closed or parser.pop_depth is not None:
        raise ValueError('行业完整选择器缺失、重复或截断')
    if parser.headers != ['行业', '概念', '地域'] or len(parser.groups) != 3:
        raise ValueError('行业/概念/地域目录身份不明确')
    parsed = []
    all_codes = set()
    for group in parser.groups:
        if not group:
            raise ValueError('行业分类组不完整')
        rows = []
        for href, name in group:
            match = re.fullmatch(r'/bkzj/(BK\d+)\.html', href)
            if match is None or not name:
                raise ValueError('行业代码或名称格式无效')
            code = match.group(1)
            if code in all_codes:
                raise ValueError('分类目录代码重复或跨分类冲突')
            all_codes.add(code)
            rows.append({'f12': code, 'f14': name})
        parsed.append(sorted(rows, key=lambda row: row['f12']))
    # The selector contains alphabetic full-category navigation, rather than a
    # short hot list. Do not allow an arbitrarily tiny substitute to pass.
    if len(parsed[0]) < 50:
        raise ValueError('行业完整导航数量异常，不能确认全集')
    return parsed


def _get_html(url, *, timeout):
    request = Request(url, headers={'User-Agent': 'Mozilla/5.0',
                                   'Referer': 'https://data.eastmoney.com/bkzj/hy.html'})
    with urlopen(request, timeout=timeout) as handle:
        return handle.read().decode('utf-8')


def collect_web_universe(feed, raw, *, clock=None, http=None, quote_workers=12):
    """Match two current public full selectors and attach independent quotes.

    `http(url, timeout=...) -> str` can be injected for deterministic tests.
    `feed.eastmoney` and its overall deadline are shared with the main sampler.
    Raw HTML, response identities and each native quote are returned in `raw`;
    the caller saves that list through its existing private snapshot publisher.
    Missing auxiliary quotes do not erase a verified industry identity.
    """
    getter = http or _get_html
    deadline = getattr(feed, 'deadline', time.monotonic() + 70)
    catalogues = []
    for url in CATALOG_URLS:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('行业目录采集预算用尽')
        html = getter(url, timeout=min(getattr(feed, 'timeout', 5), remaining))
        retrieved = clock() if clock else dt.datetime.now().astimezone()
        # Preserve the received body before any structural/date validation, so
        # a truncated, obsolete or changed response remains privately auditable.
        evidence = {'kind': 'industry-web-catalogue', 'source_url': url,
                    'retrieved_at': retrieved.isoformat(), 'html': html,
                    'source_fingerprint': _digest(html), 'validation_passed': False}
        raw.append(evidence)
        try:
            groups = parse_catalog_html(html)
            rendered = re.search(r'<!--\s*(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) at server\[', html)
            rendered_at = (dt.datetime.fromisoformat(rendered.group(1)).replace(tzinfo=retrieved.tzinfo)
                           if rendered else None)
            if (rendered_at is None or rendered_at.date() != retrieved.date()
                    or (rendered_at - retrieved).total_seconds() > 60):
                raise ValueError('公开行业目录缺当日网页生成日期或含未来时间；不沿用旧目录')
        except Exception as exc:
            evidence['validation_error'] = str(exc)
            raise
        evidence.update({'rendered_at': rendered.group(1),
                         'rendered_at_meaning': '网页生成时间；不是行业变更、报价或资金时间',
                         'coverage_basis': '完整非分页行业导航；非clist.total',
                         'group_counts': [len(group) for group in groups],
                         'catalog_fingerprint': _digest(groups), 'data': groups[0],
                         'validation_passed': True})
        catalogues.append(groups)
    if catalogues[0] != catalogues[1]:
        raise ValueError('同源独立完整分类目录不一致，暂不确认行业全集')
    rows = [dict(row) for row in catalogues[0][0]]
    # The vendor ulist endpoint returns native quote clocks in bounded batches.
    # Two 64-item batches were accepted where a single 128-item URL failed.
    # Its total is validated against this explicit request, not used as evidence
    # of industry-universe completeness (that comes from the matched selectors).
    by_code = {row['f12']: row for row in rows}
    for start in range(0, len(rows), 64):
        batch = rows[start:start + 64]
        if deadline - time.monotonic() <= 10:
            break
        try:
            data, url = feed.eastmoney('ulist.np/get', dict(
                secids=','.join('90.' + row['f12'] for row in batch),
                fields='f12,f14,f2,f3,f6,f124', fltt=2, invt=2))
            retrieved = clock() if clock else dt.datetime.now().astimezone()
            raw.append({'kind': 'industry-native-quote-batch', 'source_url': url,
                        'retrieved_at': retrieved.isoformat(),
                        'requested_codes': [row['f12'] for row in batch],
                        'source_fingerprint': _digest(data), 'data': data})
            diff = data.get('diff')
            if isinstance(diff, dict):
                diff = list(diff.values())
            if (isinstance(data.get('total'), bool) or data.get('total') != len(batch)
                    or not isinstance(diff, list) or len(diff) != len(batch)
                    or {row.get('f12'): row.get('f14') for row in diff}
                    != {row['f12']: row['f14'] for row in batch}):
                raise ValueError('批量报价代码、名称、total或记录数不一致')
            for item in diff:
                row = by_code[item['f12']]
                row.update({key: item.get(key) for key in ('f2', 'f3', 'f6', 'f124')})
                row.update(quote_source_url=url, catalog_source_url=CATALOG_URLS[0])
        except Exception as exc:
            raw.append({'kind': 'industry-native-quote-batch-failure',
                        'requested_codes': [row['f12'] for row in batch], 'error': str(exc)})
    # Leave enough time for independent capital queries. An absent quote remains
    # absent; never borrow the HTML's generation time to fabricate its clock.
    remaining = deadline - time.monotonic()
    quote_deadline = min(deadline - 10, time.monotonic() + min(25, max(0, remaining * .35)))
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=min(16, max(1, quote_workers)))
    futures = {}
    for row in rows:
        if row.get('f124') is not None:
            continue
        params = dict(secid='90.' + row['f12'], fields='f57,f58,f43,f48,f170,f86', fltt=2, invt=2)
        futures[pool.submit(feed.eastmoney, 'stock/get', params)] = row
    pending = set(futures)
    while pending and time.monotonic() < quote_deadline:
        done, pending = concurrent.futures.wait(pending, timeout=min(.25, max(0, quote_deadline-time.monotonic())),
                                                return_when=concurrent.futures.FIRST_COMPLETED)
        for future in done:
            row = futures[future]
            try:
                data, url = future.result()
                retrieved = clock() if clock else dt.datetime.now().astimezone()
                raw.append({'kind': 'industry-native-quote', 'code': row['f12'],
                            'source_url': url, 'retrieved_at': retrieved.isoformat(),
                            'source_fingerprint': _digest(data), 'data': data})
                if data.get('f57') != row['f12'] or data.get('f58') != row['f14']:
                    raise ValueError('单行业报价身份与目录不一致')
                row.update(f2=data.get('f43'), f3=data.get('f170'), f6=data.get('f48'),
                           f124=data.get('f86'), quote_source_url=url,
                           catalog_source_url=CATALOG_URLS[0])
            except Exception as exc:
                row['quote_error'] = str(exc)
    for future in pending:
        future.cancel()
        futures[future]['quote_error'] = '辅助报价采集预算用尽'
    pool.shutdown(wait=False, cancel_futures=True)
    return rows
