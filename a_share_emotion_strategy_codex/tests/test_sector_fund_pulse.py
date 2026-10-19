"""Synthetic tests only: no networking or private real market samples."""
import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import sector_fund_pulse as pulse


NOW = dt.datetime(2026, 10, 19, 10, 30, tzinfo=pulse.TZ)


def quote(code='BK1001', name='虚构行业甲', main=1e8, change=1):
    return dict(f12=code, f14=name, f62=main, f184=2.5, f3=change,
                f6=4e9, f124=int(NOW.timestamp()), f2=1000)


def minutes(code='BK1001', *, start=None, count=31, base=7e7, step=1e6):
    start = start or NOW-dt.timedelta(minutes=30)
    return {'market': 90, 'code': code, 'klines': [
        f"{(start+dt.timedelta(minutes=i)).strftime('%Y-%m-%d %H:%M')},{base+i*step},0,0,{(base+i*step)*0.6},{(base+i*step)*0.4}"
        for i in range(count)]}


class FakeFeed:
    def __init__(self, quotes=None, data=None, fail=False):
        self.quotes = quotes if quotes is not None else [quote()]
        self.data = data or {q['f12']: minutes(q['f12']) for q in self.quotes}
        self.fail = fail
        self.calls = 0
        self.guard = threading.Lock()

    def eastmoney(self, endpoint, params):
        with self.guard:
            self.calls += 1
        if self.fail:
            raise RuntimeError('人工接口中断')
        if endpoint == 'clist/get':
            page = params['pn']
            return {'total': len(self.quotes), 'diff': self.quotes[(page-1)*100:page*100]}, 'https://example.invalid/industry'
        code = params['secid'].split('.')[1]
        data = self.data[code]
        if isinstance(data, Exception):
            raise data
        return data, 'https://example.invalid/minute/'+code


class PulseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.environment = patch.dict('os.environ', {'AUTOSTRATEGY_PRIVATE_ROOT': self.temporary.name})
        self.environment.start()
        self.calendar = patch.object(pulse, 'is_trading_day', return_value=(True, '交易日历通过'))
        self.calendar.start()

    def tearDown(self):
        self.calendar.stop()
        self.environment.stop()
        self.temporary.cleanup()

    def test_universe_pagination_and_unique(self):
        qs = [quote(f'BK{i:04}', f'人工{i}') for i in range(101)]
        raw = []
        self.assertEqual(len(pulse.collect_universe(FakeFeed(qs), raw)), 101)
        self.assertEqual(len(raw), 2)
        qs[-1] = qs[0]
        with self.assertRaisesRegex(ValueError, '唯一'):
            pulse.collect_universe(FakeFeed(qs), [])

    def test_universe_unknown_total_does_not_publish_partial_top10(self):
        feed = FakeFeed()
        original = feed.eastmoney
        feed.eastmoney = lambda endpoint, params: ({'diff': [quote()]}, 'https://example.invalid') if endpoint == 'clist/get' else original(endpoint, params)
        report, raw = pulse.collect(NOW, feed)
        self.assertEqual(report['status'], 'unavailable')
        self.assertIsNone(report['coverage']['expected'])
        self.assertEqual(report['top10'], [])

    def test_minute_units_and_delta(self):
        rows, delta = pulse.parse_minutes(minutes(), 'BK1001', NOW)
        self.assertEqual(rows[-1]['main_cny'], 1e8)
        self.assertEqual(delta, 3e7)
        self.assertEqual(pulse.amount(1e8), '+1.00亿元')

    def test_quote_clock_cannot_rescue_old_funds(self):
        old = minutes(start=NOW-dt.timedelta(minutes=32))
        with self.assertRaisesRegex(ValueError, '90秒'):
            pulse.inspect_industry(quote(), old, NOW, 'https://example.invalid')
        old = minutes(start=NOW-dt.timedelta(days=1, minutes=30))
        with self.assertRaisesRegex(ValueError, '日期'):
            pulse.parse_minutes(old, 'BK1001', NOW)

    def test_missing_clock_future_identity_and_unit_conflict(self):
        bad = minutes()
        bad['klines'][0] = '10:00,10,0,0,6,4'
        with self.assertRaisesRegex(ValueError, '日期'):
            pulse.parse_minutes(bad, 'BK1001', NOW)
        bad = minutes(start=NOW-dt.timedelta(minutes=29))
        with self.assertRaisesRegex(ValueError, '未来'):
            pulse.parse_minutes(bad, 'BK1001', NOW)
        bad = minutes('BK9999')
        with self.assertRaisesRegex(ValueError, '身份'):
            pulse.parse_minutes(bad, 'BK1001', NOW)
        bad = minutes()
        bad['klines'][-1] = '2026-10-19 10:30,100,0,0,59,40'
        with self.assertRaisesRegex(ValueError, '大单加超大单'):
            pulse.parse_minutes(bad, 'BK1001', NOW)

    def test_missing_duplicate_minutes_never_inflate_window(self):
        bad = minutes()
        bad['klines'].pop(10)
        self.assertIsNone(pulse.parse_minutes(bad, 'BK1001', NOW)[1])
        bad['klines'].append(bad['klines'][-1])
        with self.assertRaisesRegex(ValueError, '重复'):
            pulse.parse_minutes(bad, 'BK1001', NOW)

    def test_zero_and_negative_valid_but_not_attention(self):
        qs = [quote('BK1001'), quote('BK1002', '虚构乙')]
        data = {'BK1001': minutes(base=-3e7, step=0), 'BK1002': minutes('BK1002', base=0, step=0)}
        report, raw = pulse.collect(NOW, FakeFeed(qs, data))
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(report['top10'][0]['main_cny'], 0)
        self.assertEqual(report['attention_top3'], [])
        data['BK1002']['klines'][-1] = '2026-10-19 10:30,,0,0,0,0'
        report, raw = pulse.collect(NOW, FakeFeed(qs, data))
        self.assertEqual(report['top10'], [])
        self.assertEqual(report['coverage']['valid_funds'], 1)

    def test_ties_rank_delta_and_partial_universe(self):
        qs = [quote('BK1002', '虚构乙'), quote('BK1001')]
        report, raw = pulse.collect(NOW, FakeFeed(qs))
        self.assertEqual([r['code'] for r in report['top10']], ['BK1001', 'BK1002'])
        self.assertEqual([r['code'] for r in report['attention_top3']], ['BK1001', 'BK1002'])
        report, raw = pulse.collect(NOW, FakeFeed(qs, {'BK1001': minutes(), 'BK1002': RuntimeError('中断')}))
        self.assertEqual(report['top10'], [])
        self.assertEqual(report['attention_top3'], [])
        self.assertEqual(len(report['partial_reference']), 1)

    def test_cross_noon_and_first_30_minutes(self):
        noon = dt.datetime(2026, 10, 19, 13, 30, tzinfo=pulse.TZ)
        data = minutes(start=noon-dt.timedelta(minutes=29), count=30)
        self.assertIsNone(pulse.parse_minutes(data, 'BK1001', noon)[1])
        data['klines'].insert(0, '2026-10-19 11:30,0,0,0,0,0')
        self.assertIsNone(pulse.parse_minutes(data, 'BK1001', noon)[1])

    def test_auxiliary_quote_time_and_net_ratio_no_fabrication(self):
        q = quote()
        q['f124'] -= 120
        row = pulse.inspect_industry(q, minutes(), NOW, 'https://example.invalid')
        self.assertFalse(row['quote_valid'])
        self.assertIsNone(row['native_net_pct'])
        q = quote()
        q['f62'] = 2e8
        row = pulse.inspect_industry(q, minutes(), NOW, 'https://example.invalid')
        self.assertIn('修订差异', ';'.join(row['notes']))
        self.assertEqual(row['main_cny'], 1e8)

    def test_schedule_lunch_holidays_unknown_and_grace(self):
        for clock in ('11:33', '12:00', '15:03'):
            moment = dt.datetime.combine(NOW.date(), dt.time.fromisoformat(clock), pulse.TZ)
            self.assertFalse(pulse.schedule_context(moment, True)['run'])
        moment = NOW.replace(hour=11, minute=31)
        self.assertTrue(pulse.schedule_context(moment, True)['run'])
        self.assertFalse(pulse.schedule_context(NOW.replace(minute=36), True)['run'])
        with patch.object(pulse, 'is_trading_day', return_value=(False, '周末或交易所公告休市日')):
            self.assertFalse(pulse.schedule_context(NOW)['run'])
        with patch.object(pulse, 'is_trading_day', return_value=(False, '缺少2026年交易所休市日历，安全跳过')):
            self.assertTrue(pulse.schedule_context(NOW)['error'])

    def test_slot_duplicate_does_not_fetch_and_receipt_version(self):
        feed = FakeFeed()
        one = pulse.run(now=NOW, feed=feed, scheduled=True, no_page=True)
        count = feed.calls
        self.assertTrue(one['notify'])
        second = pulse.run(now=NOW, feed=feed, scheduled=True, no_page=True)
        self.assertFalse(second['notify'])
        self.assertEqual(feed.calls, count)
        with self.assertRaisesRegex(ValueError, '版本'):
            pulse.mark_sent('wrong')
        pulse.mark_sent(one['edition'])
        report = pulse.read_json(pulse.folder()/'current.json')
        self.assertFalse(pulse.decision(report)['notify'])

    def test_fault_suppression_recovery_and_last_success(self):
        fault = pulse.run(now=NOW, feed=FakeFeed(fail=True), no_page=True)
        self.assertTrue(fault['notify'])
        pulse.mark_sent(fault['edition'])
        again = pulse.run(now=NOW+dt.timedelta(seconds=1), feed=FakeFeed(fail=True), no_page=True)
        self.assertFalse(again['notify'])
        restored = pulse.run(now=NOW, feed=FakeFeed(), no_page=True)
        # A backwards sample is deliberately rejected; retry using a later source/sample.
        moment = NOW+dt.timedelta(minutes=1)
        q = quote(); q['f124'] = int(moment.timestamp())
        feed = FakeFeed([q], {'BK1001': minutes(start=moment-dt.timedelta(minutes=30))})
        restored = pulse.run(now=moment, feed=feed, no_page=True)
        self.assertTrue(restored['notify'])
        self.assertIn('恢复', restored['reason'])
        self.assertTrue((pulse.folder()/'last_success.json').exists())

    def test_rank_changes_need_same_session_universe_and_advanced_source(self):
        report, raw = pulse.collect(NOW, FakeFeed())
        old = copy.deepcopy(report)
        old['sampled_at'] = (NOW-dt.timedelta(minutes=30)).isoformat()
        old['all_rows'][0]['source_time'] = old['sampled_at']
        self.assertTrue(pulse.compare_previous(report, old)['available'])
        old['all_rows'][0]['source_time'] = NOW.isoformat()
        self.assertFalse(pulse.compare_previous(report, old)['available'])
        old['sampled_at'] = NOW.replace(hour=13, minute=0).isoformat()
        self.assertFalse(pulse.compare_previous(report, old)['available'])


    def test_live_clock_accepts_new_response_then_checks_final_freshness(self):
        finish = NOW+dt.timedelta(minutes=1)
        q = quote(); q['f124'] = int(finish.timestamp())
        data = minutes(start=finish-dt.timedelta(minutes=30))
        report, raw = pulse.collect(NOW, FakeFeed([q], {'BK1001': data}), clock=lambda: finish)
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(report['started_at'], NOW.isoformat())
        self.assertEqual(report['generated_at'], finish.isoformat())
        self.assertEqual(raw[0]['retrieved_at'], finish.isoformat())
        self.assertEqual(pulse.topic(report)['valid_until'], (finish+dt.timedelta(seconds=90)).isoformat())
        self.assertTrue(raw[-1]['source_fingerprint'])
        moments = iter([NOW, NOW, NOW+dt.timedelta(minutes=2)])
        report, _ = pulse.collect(NOW, FakeFeed(), clock=lambda: next(moments))
        self.assertEqual(report['status'], 'unavailable')
        self.assertIn('最终复核', ';'.join(report['missing']))

    def test_failed_slot_may_recover_but_success_slot_is_frozen(self):
        fault = pulse.run(now=NOW, feed=FakeFeed(fail=True), scheduled=True, no_page=True)
        pulse.mark_sent(fault['edition'])
        feed = FakeFeed()
        restored = pulse.run(now=NOW, feed=feed, scheduled=True, no_page=True)
        self.assertEqual(restored['status'], 'complete')
        self.assertTrue(restored['notify'])
        self.assertEqual(feed.calls, 2)
        pulse.run(now=NOW, feed=feed, scheduled=True, no_page=True)
        self.assertEqual(feed.calls, 2)

    def test_partial_quotes_still_send_new_complete_capital_ranking(self):
        q = quote(); q['f3'] = None
        first = pulse.run(now=NOW, feed=FakeFeed([q]), scheduled=True, no_page=True)
        self.assertEqual(first['status'], 'partial')
        pulse.mark_sent(first['edition'])
        moment = NOW+dt.timedelta(minutes=30)
        q['f124'] = int(moment.timestamp())
        data = {'BK1001': minutes(start=moment-dt.timedelta(minutes=30))}
        second = pulse.run(now=moment, feed=FakeFeed([q], data), scheduled=True, no_page=True)
        self.assertTrue(second['notify'])
        report = pulse.read_json(pulse.folder()/'current.json')
        self.assertTrue(report['ranking_complete'])
        self.assertEqual(report['attention_top3'], [])
        self.assertIn('盘口原生净占比', pulse.markdown(report))
        self.assertEqual(pulse.topic(report)['status'], 'partial')

    def test_topic_valid_and_no_raw_publication(self):
        from research_topics import validate
        report, raw = pulse.collect(NOW, FakeFeed())
        data = pulse.topic(report)
        validate(data, NOW)
        self.assertNotIn('klines', json.dumps(data))
        self.assertEqual(len(data['directions']), 1)
        self.assertIn('不是主板限定', data['summary'])

    def test_concurrent_success_same_slot_one_fetch(self):
        feed, results = FakeFeed(), []
        threads = [threading.Thread(target=lambda: results.append(pulse.run(now=NOW, feed=feed, scheduled=True, no_page=True))) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(feed.calls, 2)
        self.assertEqual(sum(r['status']=='duplicate' for r in results), 1)


    def _sina_reference(self, now=NOW):
        row = pulse.inspect_industry(quote(), minutes(), NOW, 'https://example.invalid')
        row.update(code='new_fake', name='虚构延迟行业', source_time=(now-dt.timedelta(minutes=5)).isoformat(),
                   quote_time=(now-dt.timedelta(minutes=5)).isoformat(), provider='sina', reference_only=True)
        return dict(provider='sina', rows=[row], raw=[], universe_complete=True,
                    expected_count=1, universe_fingerprint='artificial', missing=[],
                    scope='虚构新浪行业全集', source_url='https://example.invalid/sina')

    def _ths_reference(self):
        return dict(provider='同花顺', coverage_complete=True, expected_count=1,
                    fund_metric='原生流入减流出，未标主力', limitation='资金自身时点未知，仅供参考',
                    rows=[dict(code='881999', name='虚构时点未知行业', net_cny=2e8, change_pct=1.2)],
                    source_urls=['https://example.invalid/ths'])

    def test_provider_switch_never_compares_rank_changes(self):
        report, _ = pulse.collect(NOW, FakeFeed())
        old = copy.deepcopy(report)
        old['provider'] = 'sina'
        old['sampled_at'] = (NOW-dt.timedelta(minutes=30)).isoformat()
        old['all_rows'][0]['source_time'] = old['sampled_at']
        self.assertFalse(pulse.compare_previous(report, old)['available'])

    def test_complete_delayed_reference_never_promotes_live(self):
        report, _ = pulse.collect_with_fallback(NOW, feed=FakeFeed(fail=True),
            sina_collector=lambda *a, **k: self._sina_reference(),
            ths_collector=lambda **k: self.fail('不应继续取同花顺'))
        self.assertTrue(report['reference_complete'])
        self.assertFalse(report['ranking_complete'])
        self.assertEqual(report['attention_top3'], [])
        self.assertEqual(report['top10'], [])
        self.assertEqual(report['coverage']['valid_funds'], 0)
        self.assertEqual(report['reference_top10'][0]['main_cny'], 1e8)
        self.assertIn('延迟参考', pulse.markdown(report))
        from research_topics import validate
        validate(pulse.topic(report), NOW)
        self.assertEqual(pulse.topic(report)['valid_until'], NOW.isoformat())

    def test_sina_inconsistent_times_fall_back_to_unknown_time_reference(self):
        snapshot = self._sina_reference()
        row = copy.deepcopy(snapshot['rows'][0])
        row.update(code='new_fake2', source_time=(NOW-dt.timedelta(minutes=8)).isoformat())
        snapshot.update(rows=snapshot['rows']+[row], expected_count=2)
        report, _ = pulse.collect_with_fallback(NOW, feed=FakeFeed(fail=True),
            sina_collector=lambda *a, **k: snapshot,
            ths_collector=lambda **k: self._ths_reference())
        self.assertEqual(report['provider'], 'ths')
        self.assertIsNone(report['reference_top10'][0]['source_time'])
        self.assertIsNone(report['reference_top10'][0]['delta_30m_cny'])
        text = pulse.markdown(report)
        self.assertIn('原站未提供', text)
        self.assertNotIn('当日累计主力', text)
        self.assertEqual(pulse.topic(report)['sources'][0]['published_at'], '未取得源资金时间')

    def test_partial_reference_pool_does_not_become_full_ranking(self):
        snapshot = self._sina_reference()
        snapshot['expected_count'] = 2
        def missing_ths(**kwargs):
            raise ValueError('人工备用中断')
        report, _ = pulse.collect_with_fallback(NOW, feed=FakeFeed(fail=True),
            sina_collector=lambda *a, **k: snapshot, ths_collector=missing_ths)
        self.assertEqual(report['status'], 'unavailable')
        self.assertEqual(report['top10'], [])
        self.assertFalse(report.get('reference_complete', False))
        self.assertEqual(report['coverage']['failed_funds'], report['coverage']['expected'])

    def test_reference_slot_receipts_and_new_slot_are_independent(self):
        report, raw = pulse.collect_with_fallback(NOW, slot=NOW.isoformat(), feed=FakeFeed(fail=True),
            sina_collector=lambda *a, **k: self._sina_reference(),
            ths_collector=lambda **k: self._ths_reference())
        first = pulse.publish(report, raw)
        pulse.mark_sent(first['edition'])
        again = copy.deepcopy(report)
        again['sampled_at'] = (NOW+dt.timedelta(seconds=10)).isoformat()
        self.assertFalse(pulse.decision(again)['notify'])
        again['slot'] = (NOW+dt.timedelta(minutes=30)).isoformat()
        self.assertTrue(pulse.decision(again)['notify'])
        self.assertFalse((pulse.folder()/'last_success.json').exists())
        self.assertTrue((pulse.folder()/'last_reference.json').exists())


    def _published_ths(self):
        return dict(provider='同花顺', coverage_kind='publisher_top10',
                    published_top10_complete=True, expected_count=10,
                    universe_complete=False, coverage_complete=False,
                    fund_metric='原站普通资金净额，未注明主力', limitation='资金时点未知，仅原生参考',
                    coverage_basis='单响应原站发布前十，非全行业池',
                    source_urls=['https://example.invalid/ths'],
                    rows=[dict(code=f'8819{i:02d}',name=f'人工行业{i}',net_cny=(10-i)*1e8,
                               change_pct=None if i==0 else 1.0) for i in range(10)])

    def test_default_run_uses_only_ths_published_list(self):
        calls=[]
        def ths(**kwargs):
            calls.append(1)
            return self._published_ths()
        with patch.object(pulse, 'collect', side_effect=AssertionError('不可调用东财')):
            with patch.object(pulse, 'collect_with_fallback', side_effect=AssertionError('不可切换来源')):
                out=pulse.run(now=NOW,scheduled=True,no_page=True,ths_collector=ths)
                self.assertTrue(out['notify'])
                again=pulse.run(now=NOW,scheduled=True,no_page=True,ths_collector=ths)
        self.assertEqual(len(calls),1)
        self.assertEqual(again['status'],'duplicate')
        r=pulse.read_json(pulse.folder()/'current.json')
        self.assertEqual(r['provider'],'ths')
        self.assertEqual(r['source_policy'],'ths-only')
        self.assertFalse(r['universe_complete'])
        self.assertFalse(r['ranking_complete'])
        self.assertEqual(len(r['reference_top10']),10)
        self.assertEqual(r['attention_top3'],[])
        self.assertIn('原站前十覆盖：10/10',pulse.markdown(r))
        self.assertIn('缺失',pulse.markdown(r))
        self.assertNotIn('同场30分钟净额|',pulse.markdown(r))
        self.assertIn('未拉取全行业池',pulse.topic(r)['summary'])

    def test_ths_failure_does_not_substitute_or_return_empty_success(self):
        def unavailable(**kwargs):
            raise ValueError('人工401访问挑战；停止')
        with patch.object(pulse,'collect_with_fallback',side_effect=AssertionError('不能换源')):
            r,_=pulse.collect_ths_only(NOW,collector=unavailable)
        self.assertEqual(r['status'],'unavailable')
        self.assertFalse(r.get('reference_complete',False))
        self.assertEqual(r['provider'],'ths')
        self.assertEqual(r['reference_top10'] if 'reference_top10' in r else [],[])
        self.assertIn('401',r['missing'][0])

    def test_manual_reference_after_close_never_claims_scheduled_time(self):
        after=NOW.replace(hour=16)
        self.assertFalse(pulse.run(now=after,scheduled=True,no_page=True)['notify'])
        result=pulse.run(now=after,reference_refresh=True,no_page=True,
                         ths_collector=lambda **kwargs:self._published_ths())
        self.assertTrue(result['notify'])
        r=pulse.read_json(pulse.folder()/'current.json')
        self.assertIsNone(r['slot'])
        self.assertEqual(r['sampled_at'],after.isoformat())
        self.assertEqual(r['execution_phase'],'manual-reference')
        self.assertEqual(pulse.topic(r)['as_of_kind'],'capture')
        with self.assertRaises(ValueError):
            pulse.run(now=after,reference_refresh=True,scheduled=True,no_page=True)

    def test_publisher_list_missing_item_does_not_become_full_top10(self):
        snapshot=self._published_ths(); snapshot['rows'].pop()
        r,_=pulse.collect_ths_only(NOW,collector=lambda **kwargs:snapshot)
        self.assertEqual(r['status'],'unavailable')
        self.assertFalse(r.get('reference_complete',False))


if __name__ == '__main__':
    unittest.main()
