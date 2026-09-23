"""Offline, fictional source payloads. Never requests live quotes or real accounts."""
import copy
import datetime as dt
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import sector_radar_feeds as feeds
from sector_radar_catalog import SECTORS


STAMP = '2026-09-23T15:40:00+08:00'


def ths_page(rows, page=1, pages=1, code='307816', index='885893'):
    body = '<input id="requestQuery" value="code/%s"><input id="clid" value="%s">' % (code, index)
    body += '<table class="m-table m-pager-table"><tbody>'
    for ordinal, symbol, name in rows:
        body += '<tr><td>%s</td><td><a>%s</a></td><td><a>%s</a></td></tr>' % (ordinal, symbol, name)
    return body + '</tbody></table><span class="page_info">%s/%s</span>' % (page, pages)


class FakeFeed:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def read(self, url, encoding='utf-8'):
        self.calls.append(url)
        body = self.responses(url) if callable(self.responses) else self.responses[url]
        if isinstance(body, Exception):
            raise body
        return body, {'url': url, 'label': '人工供应商样例', 'retrieved_at': STAMP, 'sha256': feeds.digest(body)}


class FeedsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'AUTOSTRATEGY_PRIVATE_ROOT': self.temp.name})
        self.env.start()
        self.spec = next(s for s in SECTORS if s['id'] == 'national-chip-fund')

    def tearDown(self):
        self.env.stop(); self.temp.cleanup()

    def test_catalog_no_fund_proxy_and_explicit_pet_difference(self):
        self.assertEqual(len(SECTORS), 12)
        self.assertEqual(len({s['id'] for s in SECTORS}), 12)
        self.assertIsNone(self.spec['em_code'])
        pet = next(s for s in SECTORS if s['id'] == 'pet-copper')
        self.assertEqual(pet['em_label'], '复合集流体')
        self.assertIn('更宽', pet['em_difference'])

    def test_ths_preserves_excluded_boards_and_st(self):
        body = ths_page([(1, '600001', '示例甲'), (2, '688001', '示例乙'),
                         (3, '300001', '示例丙'), (4, '920001', '示例丁'), (5, '000001', '*ST示例')])
        got = feeds.collect_sector(self.spec, '2026-09-23', FakeFeed(lambda u: body))
        self.assertTrue(got['verified'])
        self.assertEqual(len(got['members']), 5)
        self.assertEqual(got['expected_count'], 5)
        self.assertEqual(got['count_basis'], 'pagination_last_ordinal')

    def test_multi_page_and_duplicates(self):
        first = ths_page([(1, '600001', '示例甲'), (2, '000001', '示例乙')], pages=2)
        second = ths_page([(3, '002001', '示例丙')], page=2, pages=2)
        feed = FakeFeed(lambda u: second if '/page/2/' in u else first)
        got = feeds.collect_ths(self.spec, feed)
        self.assertTrue(got['membership_complete'])
        self.assertEqual(got['expected_count'], 3)
        self.assertEqual(len(feed.calls), 3)  # Full pages plus stability check.
        second = ths_page([(3, '600001', '示例甲')], page=2, pages=2)
        got = feeds.collect_ths(self.spec, FakeFeed(lambda u: second if '/page/2/' in u else first))
        self.assertFalse(got['membership_complete'])
        self.assertIn('唯一代码', got['missing'][0])

    def test_page_identity_and_bad_total(self):
        with self.assertRaises(ValueError):
            feeds.parse_ths_page(ths_page([(1, '600001', '样例')], code='wrong'), expected_page=1, provider_code='307816')
        with self.assertRaises(ValueError):
            feeds.parse_ths_page(ths_page([(1, '600001', '样例')]), expected_page=2, provider_code='307816')
        with self.assertRaises(ValueError):
            feeds.parse_ths_page('<html>登录页面</html>', expected_page=1, provider_code='307816')
        got = feeds.collect_ths(self.spec, FakeFeed(lambda u: ths_page([(2, '600001', '示例')])) )
        self.assertFalse(got['membership_complete'])

    def test_page_changes_do_not_pass(self):
        calls = []
        def response(url):
            calls.append(url)
            if '/page/2/' in url:
                return ths_page([(3, '002001', '示例丙')], page=2, pages=2)
            if len(calls) == 1:
                return ths_page([(1, '600001', '示例甲'), (2, '000001', '示例乙')], pages=2)
            return ths_page([(1, '000001', '示例乙'), (2, '600001', '示例甲')], pages=2)
        got = feeds.collect_ths(self.spec, FakeFeed(response))
        self.assertFalse(got['membership_complete'])
        self.assertIn('发生变化', got['missing'][0])

    def test_observed_current_membership_never_backdates(self):
        body = ths_page([(1, '600001', '示例甲')])
        got = feeds.collect_sector(self.spec, '2026-09-22', FakeFeed(lambda u: body))
        self.assertFalse(got['verified'])
        self.assertTrue(got['membership_complete'])
        self.assertEqual(got['membership_as_of'], '2026-09-23')
        self.assertIn('不能回填', got['missing'][-1])

    def test_exact_day_cache_retains_original_date_and_hash(self):
        payload = {'membership_complete': True, 'membership_as_of': '2026-09-22',
                   'retrieved_at': '2026-09-22T15:40:00+08:00', 'members': [{'code': '600001', 'name': '示例'}]}
        feeds._cache_write('membership', self.spec['id'], '2026-09-22', payload)
        got = feeds.collect_sector(self.spec, '2026-09-22', FakeFeed(lambda u: ValueError('offline')))
        self.assertTrue(got['verified'])
        self.assertTrue(got['cache_used'])
        self.assertEqual(got['retrieved_at'], payload['retrieved_at'])
        from research_store import research_path, read_json, atomic_json
        path = research_path('sector_radar/cache/membership/2026-09-22/national-chip-fund.json')
        corrupt = read_json(path); corrupt['payload']['members'][0]['name'] = 'tampered'
        atomic_json(path, corrupt)
        self.assertIsNone(feeds._cache_read('membership', self.spec['id'], '2026-09-22'))

    def test_em_count_and_identity(self):
        raw = {'rc': 0, 'data': {'total': 2, 'diff': [{'f12': '600001', 'f13': 1, 'f14': '示例甲'},
                                                       {'f12': '688001', 'f13': 1, 'f14': '示例乙'}]}}
        got = feeds.parse_em_page(json.dumps(raw), page=1)
        self.assertEqual(len(got['members']), 2)
        raw['data']['total'] = 3
        with self.assertRaises(ValueError): feeds.parse_em_page(json.dumps(raw), page=1)
        raw['data']['total'] = 2; raw['data']['diff'][0].pop('f13')
        with self.assertRaises(ValueError): feeds.parse_em_page(json.dumps(raw), page=1)

    def test_empty_em_pool_is_distinct_from_missing(self):
        got = feeds.parse_em_page(json.dumps({'rc': 0, 'data': {'total': 0, 'diff': []}}), page=1)
        self.assertEqual(got['expected_count'], 0)
        with self.assertRaises(ValueError): feeds.parse_em_page(json.dumps({'data': None}), page=1)

    def test_security_name_never_proves_normal_status(self):
        a = feeds.security_evidence('600001', '示例甲', signal='2026-09-23')
        self.assertFalse(a['verified']); self.assertTrue(a['mainboard'])
        self.assertIsNone(a['st']); self.assertIsNone(a['suspended']); self.assertIsNone(a['normal_limit'])
        for code in ('688001', '300001', '301001', '920001', '800001', '900001', '200001', '510001'):
            self.assertFalse(feeds.security_evidence(code, '示例', signal='2026-09-23')['mainboard'])
        self.assertTrue(feeds.security_evidence('600001', '*ST样例', signal='2026-09-23')['st'])

    def test_daily_native_units_future_truncation_and_conflicts(self):
        node = {'data': {'code': '600001', 'klines': ['2026-09-22,10,10.5,11,9,123,123456,1,1,1,2',
                                                  '2026-09-23,10,10.5,11,9,999,999999,1,1,1,2']}}
        bars = feeds.parse_em_daily(json.dumps(node), '600001', '2026-09-22')
        self.assertEqual(len(bars), 1); self.assertEqual(bars[0]['volume_shares'], 12300)
        self.assertEqual(bars[0]['amount_cny'], 123456)
        with self.assertRaises(ValueError): feeds.parse_em_daily(json.dumps(node), '600002', '2026-09-22')
        node['data']['klines'].append(node['data']['klines'][0])
        with self.assertRaises(ValueError): feeds.parse_em_daily(json.dumps(node), '600001', '2026-09-22')

    def test_cross_history_and_adjustment_date_are_independent(self):
        days = []
        day = dt.date(2026, 6, 1)
        while len(days) < 65:
            if day.weekday() < 5:
                days.append(day.isoformat())
            day += dt.timedelta(days=1)
        signal = days[-1]
        rows = [[d, '10', '10.5', '11', '9', '100'] for d in days]
        tencent = json.dumps({'data': {'sh600001': {'qfqday': rows}}})
        cross = ';'.join(d.replace('-', '') + ',10,11,9,10.5,10000,102500' for d in days)
        ths = 'quotebridge_v4_line_hs_600001_01_last(' + json.dumps({'data': cross}) + ')'
        feed = FakeFeed(lambda u: ths if '10jqka' in u else tencent)
        result = feeds.collect_daily('600001', signal, days, feed, eastmoney_available=False)
        self.assertTrue(result['verified']); self.assertTrue(result['cross_verified'])
        self.assertEqual(result['bars'][-1]['amount_cny'], 102500)
        self.assertEqual(result['adjustment_as_of'], STAMP[:10])
        self.assertNotEqual(result['adjustment_as_of'], signal)
        self.assertEqual(result['as_of'], signal)

    def test_failed_cross_history_does_not_fabricate_amount(self):
        days = [(dt.date(2026, 5, 1) + dt.timedelta(days=i)).isoformat() for i in range(65)]
        tencent = json.dumps({'data': {'sh600001': {'qfqday': [[d, '10', '10.5', '11', '9', '100'] for d in days]}}})
        cross = ';'.join(d.replace('-', '') + ',10,11,9,10.7,10000,102500' for d in days)
        ths = 'quotebridge_v4_line_hs_600001_01_last(' + json.dumps({'data': cross}) + ')'
        out = feeds.collect_daily('600001', days[-1], days, FakeFeed(lambda u: ths if '10jqka' in u else tencent), eastmoney_available=False)
        self.assertFalse(out['verified']); self.assertFalse(out['cross_verified'])
        self.assertTrue(all(b['amount_cny'] is None for b in out['bars']))

    def test_expired_budget_prevents_new_requests(self):
        feed = FakeFeed(lambda u: 'unused')
        with patch.object(feeds.time, 'monotonic', return_value=20):
            limited = feeds._DeadlineFeed(feed, 10)
            with self.assertRaises(ValueError):
                feeds._read(limited, 'https://example.invalid/data')
        self.assertFalse(feed.calls)


    def test_retry_is_bounded(self):
        feed = FakeFeed(lambda u: OSError('offline'))
        with self.assertRaises(ValueError): feeds._read(feed, 'https://example.invalid/quotes')
        self.assertEqual(len(feed.calls), 2)

    def test_one_category_failure_does_not_abort_union(self):
        s1, s2 = copy.deepcopy(self.spec), copy.deepcopy(self.spec)
        s1['id'], s2['id'] = 'fiction-a', 'fiction-b'
        sectors = {s1['id']: dict(feeds._base_sector(s1, 'ths', s1['ths_code'], [], [], missing=['offline'])),
                   s2['id']: dict(feeds._base_sector(s2, 'ths', s2['ths_code'], [], [
                       {'code': '600001', 'name': '示例甲'}, {'code': '688001', 'name': '示例乙'}], expected=2, complete=True))}
        def collection(spec, signal, feed): return sectors[spec['id']]
        daily = {'verified': False, 'bars': [], 'sources': []}
        with patch.object(feeds, 'SECTORS', [s1, s2]), patch.object(feeds, 'collect_sector', side_effect=collection), \
             patch.object(feeds, 'collect_daily', return_value=daily), patch.object(feeds, 'calendar_payload', return_value=('2026-09-23', {'days': ['2026-09-23'], 'verified': True})):
            out = feeds.collect(STAMP, feed=FakeFeed(lambda u: OSError('offline')))
        self.assertEqual(out['acquisition']['union_received'], 2)
        self.assertEqual(out['acquisition']['history_requested'], 1)
        self.assertEqual(len(out['stocks']), 2)
        self.assertFalse(out['stocks'][0]['announcement']['verified'])


if __name__ == '__main__': unittest.main()
