"""Offline synthetic industry history fixtures; no real prices or fund amounts."""
import copy
import datetime as dt
import json
import os
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import etf_flow_history as history
from research_store import atomic_json, read_json, research_path

NOW = dt.datetime(2026, 8, 16, 12, tzinfo=history.TZ)
CUTOFF = '2026-08-14'
CALENDAR = {'closures': {'2026': []}, 'source': 'https://www.sse.com.cn/disclosure/dealinstruc/closed/'}
DATES = history.trading_window(CUTOFF, now=NOW, calendar=CALENDAR)


def html(count=51):
    links = ''.join(f'<a href="/bkzj/BK{i:04d}.html">人工行业{i}</a>' for i in range(count))
    return ('<html><!-- 2026-08-16 12:00:00 at server[1] --><div class="bkselectorpop">'
            '<ul class="pop_slider"><li>行业</li><li>概念</li><li>地域</li></ul>'
            '<div class="pop-cont">'+links+'</div>'
            '<div class="pop-cont"><a href="/bkzj/BK9000.html">人工概念</a></div>'
            '<div class="pop-cont"><a href="/bkzj/BK9999.html">人工地域</a></div></div></html>')


def catalog():
    return history.parse_catalog_pair([{'source': url, 'body': html(), 'retrieved_at': NOW.isoformat()}
                                       for url in history.CATALOG_URLS], now=NOW)


def raw(identity, dates=DATES, main='3', large='1', superlarge='2'):
    lines = [','.join([day, main, '0', '0', large, superlarge, '1', '0', '0', '0', '0', '0', '0', '0', '0']) for day in dates]
    return {'rc': 0, 'data': dict(identity, market=90, klines=lines)}


def parsed(identity, dates=DATES, **kwargs):
    return history.parse_history(raw(identity, dates, **kwargs), identity, dates,
                                 now=NOW, retrieved_at=NOW.isoformat())


def report(count=3, unit=True):
    cat, ids = catalog()
    histories = [parsed(identity, main=str(i-1 if i < count else 0), large=str(i-1 if i < count else 0), superlarge='0')
                 for i,identity in enumerate(ids)]
    return history.compute_report(cat, ids, histories, DATES, unit_verified=unit, now=NOW)


class IndustryHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'AUTOSTRATEGY_PRIVATE_ROOT': self.temp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop(); self.temp.cleanup()

    def test_calendar_holidays_and_rejection(self):
        self.assertEqual(len(DATES), 20)
        self.assertEqual(DATES[-1], CUTOFF)
        cal = {'closures': {'2026': [CUTOFF]}}
        for cutoff, config, now in [(CUTOFF, cal, NOW), ('2026-08-15', CALENDAR, NOW),
                                    ('2026-08-17', CALENDAR, NOW), (CUTOFF, {'closures': {}}, NOW),
                                    (CUTOFF, CALENDAR, NOW.replace(day=14, hour=14))]:
            with self.subTest(cutoff=cutoff), self.assertRaises(ValueError):
                history.trading_window(cutoff, now=now, calendar=config)
        window = history.trading_window(CUTOFF, 3, now=NOW, calendar={'closures': {'2026': ['2026-08-13']}})
        self.assertEqual(window, ['2026-08-11', '2026-08-12', CUTOFF])

    def test_catalog_requires_matching_classified_full_selectors(self):
        cat, ids = catalog()
        self.assertTrue(cat['complete']); self.assertEqual(cat['count'], 51)
        self.assertEqual(cat['group_counts'], [51,1,1]); self.assertIsNone(cat['source_total'])
        self.assertNotIn('BK9000', [row['code'] for row in ids])
        for second in (html(52), html(3), html().replace('</html>', ''), html().replace('2026-08-16','2026-08-15')):
            with self.assertRaises(ValueError):
                history.parse_catalog_pair([{'source': url,'body': html() if i == 0 else second,
                                             'retrieved_at': NOW.isoformat()} for i,url in enumerate(history.CATALOG_URLS)], now=NOW)

    def test_exact_source_fields_and_decimal_arithmetic(self):
        identity = {'code':'BK0001','name':'人工行业1'}
        value = parsed(identity, main='0.3', large='0.1', superlarge='0.2')
        self.assertEqual(len(value['rows']),20); self.assertEqual(value['missing_dates'], [])
        self.assertEqual(value['rows'][0]['large_net_yuan'],'0.1')
        payload = raw(identity); payload['data']['klines'][4] = payload['data']['klines'][4].replace(',3,',',3.001,',1)
        value = history.parse_history(payload,identity,DATES,now=NOW,retrieved_at=NOW.isoformat())
        self.assertEqual(len(value['rows']),19); self.assertEqual(value['missing_dates'],[DATES[4]])

    def test_identity_unsorted_duplicate_future_and_clock_rejected(self):
        identity = {'code':'BK0001','name':'人工行业1'}
        for mode in ('identity','unsorted','duplicate','future','early','missing','malformed','nan'):
            payload = raw(identity); at = NOW.isoformat()
            if mode == 'identity': payload['data']['name'] = '人工错误行业'
            if mode == 'unsorted': payload['data']['klines'].reverse()
            if mode == 'duplicate': payload['data']['klines'].append(payload['data']['klines'][-1])
            if mode == 'future': payload['data']['klines'].append(raw(identity,['2026-08-17'])['data']['klines'][0])
            if mode == 'early': at = NOW.replace(day=14,hour=14).isoformat()
            if mode == 'missing': at = None
            if mode == 'malformed': payload['data']['klines'] = ['bad']
            if mode == 'nan': payload = raw(identity,main='NaN')
            result = history.parse_history(payload,identity,DATES,now=NOW,retrieved_at=at)
            with self.subTest(mode=mode): self.assertFalse(result['rows'])

    def test_later_real_but_outside_cutoff_records_are_excluded(self):
        identity = {'code':'BK0001','name':'人工行业1'}
        current = NOW.replace(day=17,hour=10)
        payload = raw(identity,DATES+['2026-08-17'])
        result = history.parse_history(payload,identity,DATES,now=current,retrieved_at=current.isoformat())
        self.assertEqual(len(result['rows']),20); self.assertEqual(result['ignored_after_cutoff'],['2026-08-17'])

    def test_complete_rank_sign_order_zero_and_twenty_day_sum(self):
        value = report(25); day=value['days'][-1]
        self.assertEqual(day['status'],'complete'); self.assertEqual(len(day['inflows']),10)
        self.assertEqual(day['inflows'][0]['main_net_yuan'],23)
        self.assertEqual(len(day['outflows']),1); self.assertEqual(day['outflows'][0]['main_net_yuan'],-1)
        self.assertEqual(day['zero_count'],27)
        self.assertEqual(value['summary']['cumulative']['inflows'][0]['main_net_yuan'],460)
        history.validate_report(value,now=NOW)

    def test_missing_one_day_only_removes_that_day_and_cumulative_formal_lists(self):
        cat, ids = catalog()
        values=[parsed(x) for x in ids]; values[0]['rows'].pop(2)
        value=history.compute_report(cat,ids,values,DATES,unit_verified=True,now=NOW)
        self.assertEqual(value['coverage']['complete_days'],19)
        self.assertEqual(value['days'][2]['status'],'partial'); self.assertEqual(value['days'][2]['inflows'],[])
        self.assertEqual(value['summary']['cumulative']['verified_sectors'],50)
        self.assertEqual(value['summary']['cumulative']['inflows'],[])
        history.validate_report(value,now=NOW)

    def test_missing_unit_and_catalog_cannot_rank(self):
        value=report(unit=False)
        self.assertEqual(value['coverage']['verified_sectors'],0)
        self.assertEqual(value['days'][0]['status'],'unavailable')
        history.validate_report(value,now=NOW)
        cat,ids=catalog(); cat['complete']=False
        value=history.compute_report(cat,ids,[parsed(x) for x in ids],DATES,unit_verified=True,now=NOW)
        self.assertEqual(value['days'][0]['status'],'partial'); self.assertEqual(value['days'][0]['inflows'],[])

    def test_compute_rejects_duplicate_rows_identity_and_changed_arithmetic(self):
        cat,ids=catalog()
        for mode in ('duplicate','identity','arithmetic'):
            value=parsed(ids[0])
            if mode=='duplicate': value['rows'].append(copy.deepcopy(value['rows'][0]))
            if mode=='identity': value['rows'][0]['code']='BK9999'
            if mode=='arithmetic': value['rows'][0]['main_net_yuan']='100'
            with self.assertRaises(ValueError):
                history.compute_report(cat,ids,[value],DATES,unit_verified=True,now=NOW)
        cat['count']=50
        with self.assertRaises(ValueError):
            history.compute_report(cat,ids[:50],[parsed(x) for x in ids[:50]],DATES,unit_verified=True,now=NOW)

    def test_publication_rejects_forged_complete_status_future_and_private_fields(self):
        for mode in ('coverage','rank','sign','private','future','day','sum'):
            value=report()
            if mode=='coverage': value['days'][0]['verified_sectors']=1
            if mode=='rank': value['days'][0]['inflows'][0]['rank']=2
            if mode=='sign': value['days'][0]['outflows'][0]['main_net_yuan']=0
            if mode=='private': value['gaps'].append('/Users/artificial/private.json')
            if mode=='future': value['generated_at']=NOW.replace(day=17).isoformat()
            if mode=='day': value['days'][0]['date']='2026-08-15'
            if mode=='sum': value['days'][0]['inflows'][0]['large_net_yuan']=99
            with self.subTest(mode=mode), self.assertRaises(ValueError): history.validate_report(value,now=NOW)

    def test_publish_is_independent_and_archived(self):
        marker={'status':'synthetic unchanged'}
        atomic_json(research_path('etf_pressure/current.json'),marker)
        with patch.object(history,'_now',return_value=NOW): history.publish_report(report())
        self.assertEqual(read_json(research_path('etf_pressure/current.json')),marker)
        self.assertTrue(research_path('etf_flow_history/current.json').exists())
        self.assertEqual(len(list(research_path('etf_flow_history/archive').rglob('*.json'))),1)

    def collector(self, blocked=False, timeout=False):
        calls=[]
        simulate_timeout=timeout
        def getter(url,timeout):
            calls.append(url)
            result={'status_code':200,'retrieved_at':NOW.isoformat(),'error':None,'elapsed':0}
            if url in history.CATALOG_URLS: body=html()
            elif url==history.UNIT_URL: body='drawchart_phqs: function data1.push(arr[1]) data5.push(arr[5]) 单位:元 fflow/daykline/get'
            else:
                code=parse_qs(urlparse(url).query)['secid'][0][3:]
                identity={'code':code,'name':'人工行业'+str(int(code[2:]))}
                body=json.dumps(raw(identity))
                if blocked: result['status_code']=429; body='too many requests'
                if simulate_timeout: result['status_code']=None; result['error']='Timeout'; body=''
            result['body']=body.encode(); return result
        return getter,calls

    def test_full_collect_freezes_pool_and_cache_skips_all_requests(self):
        getter,calls=self.collector()
        value,cached=history.collect_report(CUTOFF,now=NOW,getter=getter,calendar=CALENDAR)
        self.assertFalse(cached); self.assertEqual(value['coverage']['verified_sectors'],51)
        self.assertEqual(len(calls),54); self.assertTrue(value['summary']['complete_window'])
        with patch.object(history,'_now',return_value=NOW): history.publish_report(value)
        calls.clear()
        second,cached=history.collect_report(CUTOFF,now=NOW,getter=getter,calendar=CALENDAR)
        self.assertTrue(cached); self.assertEqual(calls,[]); self.assertEqual(second,value)

    def test_access_block_stops_scheduling_and_timeout_has_no_retry(self):
        getter,calls=self.collector(blocked=True)
        value,_=history.collect_report(CUTOFF,now=NOW,getter=getter,calendar=CALENDAR)
        self.assertLessEqual(len(calls),8); self.assertFalse(value['summary']['complete_window'])
        self.assertTrue(any('停止' in message for message in value['gaps']))
        getter,calls=self.collector(timeout=True)
        value,_=history.collect_report(CUTOFF,now=NOW,getter=getter,calendar=CALENDAR)
        self.assertEqual(len(calls),54); self.assertEqual(len(set(calls)),54)
        self.assertEqual(value['coverage']['verified_sectors'],0)

    def test_original_frontend_unit_evidence_is_required(self):
        self.assertFalse(history.verify_unit_script('单位:元 fflow/daykline/get'))
        self.assertFalse(history.verify_unit_script('drawchart_phqs: function data1.push(arr[1]) data5.push(arr[5]) 单位:万元 fflow/daykline/get'))


if __name__=='__main__':
    unittest.main()
