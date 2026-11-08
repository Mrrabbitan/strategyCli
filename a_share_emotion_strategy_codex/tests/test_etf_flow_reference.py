"""Artificial HTML and isolated clocks only; no live collection or account data."""
import datetime as dt
import json
import unittest

from etf_flow_reference import parse_ths_two_sides, unavailable_flow, TZ


NOW = dt.datetime(2026, 8, 14, 17, tzinfo=TZ)


def evidence_html():
    chart = [dict(name='人工行业甲' if i == 0 else '人工流入'+str(i), amount=(30-i)/100,
                  addr=f'https://q.10jqka.com.cn/thshy/detail/code/{881901+i}/') for i in range(10)]
    chart += [dict(name='人工流出'+str(i), amount=-(i+1)/100,
                   addr=f'https://q.10jqka.com.cn/thshy/detail/code/{881951+i}/') for i in range(10)]
    headers = ['序号', '行业', '行业指数', '涨跌幅', '流入资金(亿)', '流出资金(亿)',
               '净额(亿)', '公司家数', '领涨股', '涨跌幅', '当前价(元)']
    cells = ['1', '<a href="https://q.10jqka.com.cn/thshy/detail/code/881901/">人工行业甲</a>',
             '100', '-1.20%', '1.00', '0.70', '0.30', '3', '人工个股', '2.00%', '10.00']
    return ('<table class="J-ajax-table"><thead><tr>'+''.join('<th>'+h+'</th>' for h in headers)
            +'</tr></thead><tbody><tr>'+''.join('<td>'+c+'</td>' for c in cells)
            +'</tr></tbody></table><span class="page_info">1/3</span><script>var JS_DATA = '
            +json.dumps(chart, ensure_ascii=False)+';</script>')


class EtfFlowReferenceTests(unittest.TestCase):
    def parse(self, body=None, **kw):
        return parse_ths_two_sides(evidence_html() if body is None else body,
                                  retrieved_at=NOW.isoformat(), now=NOW, **kw)

    def test_two_native_groups_keep_order_and_single_unit_conversion(self):
        flow = self.parse()
        self.assertEqual([x['net_yuan'] for x in flow['outflows']], [-1000000*(i+1) for i in range(10)])
        self.assertEqual([x['rank'] for x in flow['outflows']], list(range(1, 11)))
        self.assertEqual(flow['inflows'][0]['net_yuan'], 30000000)
        self.assertEqual(flow['inflows'][0]['change_pct'], -1.2)
        self.assertIsNone(flow['outflows'][0]['change_pct'])
        self.assertIsNone(flow['as_of']); self.assertFalse(flow['is_main_metric'])
        self.assertFalse(flow['universe_complete'])
        self.assertEqual(flow['status'], 'reference_only')

    def test_invalid_definition_auxiliary_identity_or_source_fail_closed(self):
        for body in (evidence_html().replace('净额(亿)', '主力净额(万元)'),
                     evidence_html().replace('<td>0.30</td>', '<td>0.33</td>'),
                     evidence_html().replace('881951/', '881901/'), '<html>访问验证</html>'):
            with self.assertRaises(ValueError): self.parse(body)
        with self.assertRaises(ValueError): self.parse(source='https://example.com/funds')
        with self.assertRaises(ValueError):
            parse_ths_two_sides(evidence_html(), retrieved_at=(NOW+dt.timedelta(seconds=1)).isoformat(), now=NOW)

    def test_failure_is_empty_and_cannot_be_mislabeled_today_or_main(self):
        flow = unavailable_flow('timeout')
        self.assertEqual(flow['inflows'], []); self.assertEqual(flow['outflows'], [])
        self.assertIsNone(flow['as_of']); self.assertIsNone(flow['retrieved_at'])
        self.assertFalse(flow['is_main_metric']); self.assertIn('超时', flow['note'])
        self.assertEqual(unavailable_flow('untrusted private exception')['status'], 'unavailable')
        self.assertNotIn('untrusted private exception', unavailable_flow('untrusted private exception')['note'])


if __name__ == '__main__': unittest.main()
