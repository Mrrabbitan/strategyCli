"""Artificial THS responses only: no network or real research data."""
import json
import datetime as dt
import time
import unittest

from sector_fund_alternative import (collect_ths_snapshot, parse_ths_page,
    collect_sina_snapshot, parse_sina_minutes, SINA_COUNT, SINA_LIST, TZ,
    parse_ths_published_top10, collect_ths_published_top10)


def page(number=1, total=2, *, code=None, rank=None, net=None, chart=None):
    code = code or ('881901' if number == 1 else '881902')
    rank = rank or number
    net = net or ('0.30' if number == 1 else '-0.20')
    chart = chart if chart is not None else [
        dict(name='虚构甲', amount=.3, addr='http://q.10jqka.com.cn/thshy/detail/code/881901/'),
        dict(name='虚构乙', amount=-.2, addr='http://q.10jqka.com.cn/thshy/detail/code/881902/')]
    headers = ['序号', '行业', '行业指数', '涨跌幅', '流入资金(亿)',
               '流出资金(亿)', '净额(亿)', '公司家数', '领涨股', '涨跌幅', '当前价(元)']
    name = '虚构甲' if number == 1 else '虚构乙'
    outgoing = '0.70' if number == 1 else '1.20'
    return ('<table class="J-ajax-table"><thead><tr>'+''.join('<th>'+h+'</th>' for h in headers)+
            '</tr></thead><tbody><tr><td>'+str(rank)+'</td><td><a href="http://q.10jqka.com.cn/thshy/detail/code/'+code+'/">'+name+
            '</a></td><td>100</td><td>-1.20%</td><td>1.00</td><td>'+outgoing+'</td><td>'+net+
            '</td><td>3</td><td>虚构领涨</td><td>2.00%</td><td>10.00</td></tr></tbody></table>'+
            '<span class="page_info">'+str(number)+'/'+str(total)+'</span><script>var JS_DATA = '+json.dumps(chart)+';</script>')


class Response:
    status_code = 200
    headers = {'Date': 'Mon, 19 Oct 2026 06:00:00 GMT'}
    def __init__(self, text):
        self.text = text
    def raise_for_status(self):
        return None


class Session:
    def __init__(self, pages):
        self.pages, self.calls = pages, 0
    def get(self, *args, **kwargs):
        self.calls += 1
        return Response(self.pages[self.calls-1])


def publisher_chart(inflows=10, outflows=10):
    return ([dict(name='虚构甲' if i == 0 else '虚构流入'+str(i),
                  amount=float((30-i)/100),
                  addr='http://q.10jqka.com.cn/thshy/detail/code/'+str(881901+i)+'/')
             for i in range(inflows)]+
            [dict(name='虚构流出'+str(i), amount=-(i+1)/100,
                  addr='http://q.10jqka.com.cn/thshy/detail/code/'+str(881951+i)+'/')
             for i in range(outflows)])


class AlternativeTests(unittest.TestCase):
    def test_native_units_and_source_time_absent(self):
        item = parse_ths_page(page())['rows'][0]
        self.assertEqual(item['net_cny'], 30000000)
        self.assertEqual(item['change_pct'], -1.2)
        self.assertIsNone(item['fund_source_time'])

    def test_complete_two_pages_remain_reference_only(self):
        session, raw = Session([page(), page(2)]), []
        snapshot = collect_ths_snapshot(session=session, raw_sink=raw.append)
        self.assertEqual(session.calls, 2)
        self.assertEqual(snapshot['received_count'], 2)
        self.assertEqual(snapshot['status'], 'reference_only')
        self.assertIsNone(snapshot['fund_source_time'])
        self.assertFalse(snapshot['half_hour_available'])
        self.assertNotIn('main_cny', snapshot['top10'][0])
        self.assertEqual(raw[0]['headers']['Date'], Response.headers['Date'])

    def test_missing_page_and_duplicate_code_rejected(self):
        with self.assertRaisesRegex(ValueError, '唯一代码'):
            collect_ths_snapshot(session=Session([page(), page(2, code='881901')]))
        with self.assertRaisesRegex(ValueError, '页码'):
            collect_ths_snapshot(session=Session([page(), page()]))

    def test_budget_limits_and_header_units(self):
        session = Session([page(total=9)])
        with self.assertRaisesRegex(ValueError, '预算'):
            collect_ths_snapshot(session=session, max_pages=2)
        self.assertEqual(session.calls, 1)
        with self.assertRaisesRegex(ValueError, '单位'):
            parse_ths_page(page().replace('流入资金(亿)', '流入资金(万)'))

    def test_missing_number_and_conflicting_net_rejected(self):
        with self.assertRaisesRegex(ValueError, '数值'):
            parse_ths_page(page(net='--'))
        with self.assertRaisesRegex(ValueError, '不一致'):
            parse_ths_page(page(net='0.80'))

    def test_changing_chart_cannot_mix_pages(self):
        changed = [dict(name='虚构甲', amount=.4, addr='http://q.10jqka.com.cn/thshy/detail/code/881901/'),
                   dict(name='虚构乙', amount=-.2, addr='http://q.10jqka.com.cn/thshy/detail/code/881902/')]
        with self.assertRaisesRegex(ValueError, '截面变化'):
            collect_ths_snapshot(session=Session([page(), page(2, chart=changed)]))

    def test_publisher_list_keeps_auxiliary_gaps_and_unknown_full_universe(self):
        report = parse_ths_published_top10(page(chart=publisher_chart()))
        self.assertEqual(report['received_count'], 10)
        self.assertEqual(report['auxiliary_count'], 1)
        self.assertEqual(report['table_count'], 1)
        self.assertTrue(report['reference_only'])
        self.assertFalse(report['universe_complete'])
        self.assertFalse(report['coverage_complete'])
        self.assertEqual(report['expected_count'], 10)
        self.assertIsNone(report['industry_expected_count'])
        self.assertTrue(report['published_top10_complete'])
        self.assertEqual(report['coverage_kind'], 'publisher_top10')
        self.assertEqual(report['time_quality'], 'unknown_reference')
        self.assertIsNone(report['fund_source_time'])
        self.assertFalse(report['half_hour_available'])
        self.assertEqual(report['rows'][0]['change_pct'], -1.2)
        for key in ('change_pct', 'inflow_cny', 'outflow_cny', 'index', 'company_count'):
            self.assertIsNone(report['rows'][1][key])
        self.assertEqual(report['rows'][1]['net_cny'], 29000000)
        self.assertTrue(all(x['net_cny'] >= 0 for x in report['rows']))
        self.assertEqual(report['publisher_outflow_count'], 10)
        self.assertIn('没有api.total', report['coverage_basis'])

    def test_publisher_rejects_chart_table_conflict_or_duplicates(self):
        mismatch = publisher_chart()
        mismatch[0]['amount'] = .5
        with self.assertRaisesRegex(ValueError, '不一致'):
            parse_ths_published_top10(page(chart=mismatch))
        duplicate = [dict(name='虚构甲', amount=.3, addr='http://q.10jqka.com.cn/thshy/detail/code/881901/')]*2
        with self.assertRaisesRegex(ValueError, '重复'):
            parse_ths_published_top10(page(chart=duplicate))

    def test_publisher_order_change_is_rejected_not_locally_repaired(self):
        chart = publisher_chart()
        chart[2], chart[3] = chart[3], chart[2]
        with self.assertRaisesRegex(ValueError, '顺序异常'):
            parse_ths_published_top10(page(chart=chart))

    def test_publisher_short_list_never_fills_negative_rows_or_fourth_party_data(self):
        report = parse_ths_published_top10(page(chart=publisher_chart(inflows=8)))
        self.assertEqual(report['received_count'], 8)
        self.assertEqual(len(report['top10']), 8)
        self.assertFalse(report['universe_complete'])
        self.assertFalse(report['published_top10_complete'])
        negative_only = publisher_chart(inflows=0)
        with self.assertRaisesRegex(ValueError, '未取得'):
            parse_ths_published_top10(page(chart=negative_only))

    def test_publisher_wrong_group_counts_and_js20_cannot_be_entire_pool(self):
        with self.assertRaisesRegex(ValueError, '数量'):
            parse_ths_published_top10(page(chart=publisher_chart(inflows=11)))
        with self.assertRaisesRegex(ValueError, '数量'):
            parse_ths_published_top10(page(chart=publisher_chart(outflows=11)))
        with self.assertRaisesRegex(ValueError, '唯一代码'):
            collect_ths_snapshot(session=Session([page(chart=publisher_chart()),
                                                  page(2, chart=publisher_chart())]))

    def test_single_collector_uses_exactly_one_response_and_no_http_clock(self):
        session, raw = Session([page(chart=publisher_chart())]), []
        report = collect_ths_published_top10(session=session, raw_sink=raw.append)
        self.assertEqual(session.calls, 1)
        self.assertEqual(len(raw), 1)
        self.assertIsNone(report['fund_source_time'])
        self.assertEqual(report['auxiliary_count'], 1)


NOW = dt.datetime(2026, 10, 19, 10, 30, 20, tzinfo=TZ)


def sina_minutes(now=NOW, *, start=None, count=31):
    latest = now.replace(second=0)
    start = start or latest-dt.timedelta(minutes=count-1)
    rows = []
    for index in range(count):
        moment = start+dt.timedelta(minutes=index)
        rows.append(dict(opendate=moment.strftime('%Y-%m-%d'),
                         ticktime=moment.strftime('%H:%M:%S'),
                         inamount=str(200000000+index*1000000),
                         outamount='100000000', netamount=str(100000000+index*1000000),
                         avg_changeratio='.0123', ratioamount='.0345',
                         r0_ratio='.99'))
    return [str(count), list(reversed(rows))]


class SinaTests(unittest.TestCase):
    def test_units_own_time_ratios_and_exact_delta(self):
        row = parse_sina_minutes(sina_minutes(), 'new_fakea', '虚构甲', NOW)
        self.assertEqual(row['main_cny'], 130000000)
        self.assertEqual(row['delta_30m_cny'], 30000000)
        self.assertEqual(row['native_net_pct'], 3.45)
        self.assertEqual(row['change_pct'], 1.23)
        self.assertEqual(row['quote_time'], row['source_time'])
        self.assertTrue(row['quote_valid'])
        self.assertIsNone(row['turnover_cny'])
        self.assertEqual(row['flow_label'], '新浪行业资金净额')

    def test_stale_future_and_wrong_day_rejected(self):
        old = sina_minutes(now=NOW-dt.timedelta(minutes=2))
        with self.assertRaisesRegex(ValueError, '90秒'):
            parse_sina_minutes(old, 'new_fakea', '虚构甲', NOW)
        future = sina_minutes(now=NOW+dt.timedelta(minutes=1))
        with self.assertRaisesRegex(ValueError, '未来'):
            parse_sina_minutes(future, 'new_fakea', '虚构甲', NOW)
        yesterday = sina_minutes(now=NOW-dt.timedelta(days=1))
        with self.assertRaisesRegex(ValueError, '日期'):
            parse_sina_minutes(yesterday, 'new_fakea', '虚构甲', NOW)

    def test_explicit_delayed_reference_never_acquires_live_eligibility(self):
        old = sina_minutes(now=NOW-dt.timedelta(minutes=5))
        with self.assertRaisesRegex(ValueError, '90秒'):
            parse_sina_minutes(old, 'new_fakea', '虚构甲', NOW)
        row = parse_sina_minutes(old, 'new_fakea', '虚构甲', NOW,
                                 max_age_seconds=900)
        self.assertEqual(row['time_quality'], 'delayed_reference')
        self.assertTrue(row['reference_only'])
        self.assertEqual(row['fund_age_seconds'], 320)
        self.assertEqual(row['delta_30m_cny'], 30000000)
        fresh = parse_sina_minutes(sina_minutes(), 'new_fakea', '虚构甲', NOW,
                                   max_age_seconds=900)
        self.assertTrue(fresh['reference_only'])
        self.assertEqual(fresh['time_quality'], 'delayed_reference')
        too_old = sina_minutes(now=NOW-dt.timedelta(minutes=16))
        with self.assertRaisesRegex(ValueError, '900秒'):
            parse_sina_minutes(too_old, 'new_fakea', '虚构甲', NOW,
                               max_age_seconds=900)
        yesterday = sina_minutes(now=NOW-dt.timedelta(days=1))
        with self.assertRaisesRegex(ValueError, '日期'):
            parse_sina_minutes(yesterday, 'new_fakea', '虚构甲', NOW,
                               max_age_seconds=900)
        with self.assertRaisesRegex(ValueError, '900秒'):
            parse_sina_minutes(old, 'new_fakea', '虚构甲', NOW,
                               max_age_seconds=901)

    def test_collection_propagates_delayed_reference_mode(self):
        def fetch(url, timeout):
            if url == SINA_COUNT:
                return '1'
            if url == SINA_LIST:
                return [dict(cate_type='0', category='new_fakea', name='虚构甲')]
            return sina_minutes(now=NOW-dt.timedelta(minutes=5))
        strict = collect_sina_snapshot(NOW, fetch=fetch)
        self.assertEqual(strict['rows'], [])
        reference = collect_sina_snapshot(NOW, fetch=fetch, max_age_seconds=900)
        self.assertEqual(len(reference['rows']), 1)
        self.assertTrue(reference['reference_only'])
        self.assertEqual(reference['time_quality'], 'delayed_reference')
        self.assertTrue(reference['rows'][0]['reference_only'])

    def test_duplicate_missing_count_and_amount_conflicts(self):
        payload = sina_minutes()
        payload[1][1] = dict(payload[1][0])
        with self.assertRaisesRegex(ValueError, '重复'):
            parse_sina_minutes(payload, 'new_fakea', '虚构甲', NOW)
        payload = sina_minutes()
        payload[0] = '32'
        with self.assertRaisesRegex(ValueError, '分页'):
            parse_sina_minutes(payload, 'new_fakea', '虚构甲', NOW)
        payload = sina_minutes()
        payload[1][0]['netamount'] = '1'
        with self.assertRaisesRegex(ValueError, '不等于'):
            parse_sina_minutes(payload, 'new_fakea', '虚构甲', NOW)

    def test_cumulative_reset_rejects_whole_industry_even_if_price_rises(self):
        payload = sina_minutes()
        latest = payload[1][0]
        latest.update(avg_price='200', avg_changeratio='.09',
                      inamount='30000000', outamount='10000000', netamount='20000000')
        with self.assertRaisesRegex(ValueError, '回退.*整体拒绝'):
            parse_sina_minutes(payload, 'new_fakea', '虚构甲', NOW,
                               max_age_seconds=900)
        # The native response is descending: a reset inside it, not only its
        # last chronological row, must also invalidate the entire sequence.
        payload = sina_minutes()
        payload[1][15].update(inamount='10000000', outamount='1000000',
                              netamount='9000000')
        with self.assertRaisesRegex(ValueError, '回退'):
            parse_sina_minutes(payload, 'new_fakea', '虚构甲', NOW)

    def test_negative_net_with_both_cumulative_sides_growing_is_valid(self):
        payload = sina_minutes()
        for index, row in enumerate(reversed(payload[1])):
            incoming, outgoing = 100000000+index*1000000, 200000000+index*2000000
            row.update(inamount=str(incoming), outamount=str(outgoing),
                       netamount=str(incoming-outgoing), ratioamount='-.0345')
        result = parse_sina_minutes(payload, 'new_fakea', '虚构甲', NOW)
        self.assertEqual(result['main_cny'], -130000000)
        self.assertEqual(result['delta_30m_cny'], -30000000)
        self.assertEqual(result['inflow_cny'], 130000000)
        self.assertEqual(result['outflow_cny'], 260000000)

    def test_cumulative_rounding_tolerance_is_limited_to_two_cents(self):
        payload = sina_minutes()
        payload[1][0].update(outamount='99999999.98', netamount='130000000.02')
        self.assertEqual(parse_sina_minutes(payload, 'new_fakea', '虚构甲', NOW)['outflow_cny'],
                         99999999.98)
        payload[1][0].update(outamount='99999999.97', netamount='130000000.03')
        with self.assertRaisesRegex(ValueError, '回退'):
            parse_sina_minutes(payload, 'new_fakea', '虚构甲', NOW)

    def test_incomplete_window_and_lunch_do_not_create_delta(self):
        payload = sina_minutes(count=30)
        self.assertIsNone(parse_sina_minutes(payload, 'new_fakea', '虚构甲', NOW)['delta_30m_cny'])
        afternoon = NOW.replace(hour=13, minute=20)
        payload = sina_minutes(afternoon, count=21)
        self.assertIsNone(parse_sina_minutes(payload, 'new_fakea', '虚构甲', afternoon)['delta_30m_cny'])

    def test_complete_collection_and_missing_industry_isolation(self):
        listing = [dict(cate_type='0', category='new_fakea', name='虚构甲'),
                   dict(cate_type='0', category='new_fakeb', name='虚构乙')]
        calls = []
        def fetch(url, timeout):
            calls.append((url, timeout))
            if url == SINA_COUNT:
                return '2'
            if url == SINA_LIST:
                return listing
            return sina_minutes()
        report = collect_sina_snapshot(NOW, fetch=fetch)
        self.assertTrue(report['universe_complete'])
        self.assertEqual(report['expected_count'], 2)
        self.assertEqual(len(report['rows']), 2)
        self.assertEqual(len(report['raw']), 4)
        self.assertTrue(all(0 < x[1] <= 5 for x in calls))
        self.assertEqual(report['missing'], [])
        def partial(url, timeout):
            if 'new_fakeb' in url:
                raise TimeoutError('人工断线')
            return fetch(url, timeout)
        report = collect_sina_snapshot(NOW, fetch=partial)
        self.assertTrue(report['universe_complete'])
        self.assertEqual(len(report['rows']), 1)
        self.assertIn('new_fakeb', report['missing'][0])

    def test_bad_universe_and_deadline_cannot_be_empty_success(self):
        report = collect_sina_snapshot(NOW, fetch=lambda url, timeout: '0')
        self.assertFalse(report['universe_complete'])
        self.assertTrue(report['missing'])
        report = collect_sina_snapshot(NOW, deadline=time.monotonic()-1,
                                      fetch=lambda *_: self.fail('不得在过期预算访问'))
        self.assertFalse(report['universe_complete'])
        self.assertTrue(report['missing'])


if __name__ == '__main__':
    unittest.main()
