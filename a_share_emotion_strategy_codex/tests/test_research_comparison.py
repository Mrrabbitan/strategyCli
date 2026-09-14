"""Artificial flow records only; no network or real stocks."""
import unittest

from research_comparison import verified_flow_history


def row(day, price='10.00', net='100', five='500'):
    return '<tr>' + ''.join(f'<td>{x}</td>' for x in
                          (day, price, '0%', '0', five, net, '1%', '0', '0%', '0', '0%')) + '</tr>'


class FlowTests(unittest.TestCase):
    def test_intraday_and_duplicate_tables_do_not_double_count(self):
        bars = [dict(date=f'2026-01-{i:02}', close=10) for i in range(5, 10)]
        rows = ''.join(row(f'202601{i:02}') for i in range(5, 10))
        body = '<div id="history_table">' + row('20260109', price='10.01') + rows + rows
        result = verified_flow_history(body, bars, '2026-01-09')
        self.assertEqual(result['net_yi'], {'1': .01, '5': .05})
        self.assertEqual(len(result['rows']), 5)
        self.assertTrue(result['five_sum_matches'])
        self.assertEqual(result['rejected_rows'], 1)

    def test_same_price_conflicting_flow_stays_unknown(self):
        body = '<div id="history_table">' + row('20260109') + row('20260109', net='200')
        result = verified_flow_history(body, [dict(date='2026-01-09', close=10)], '2026-01-09')
        self.assertEqual(result['ambiguous_dates'], ['2026-01-09'])
        self.assertEqual(result['net_yi'], {})

    def test_no_future_row_or_missing_date_fills_window(self):
        bars = [dict(date=f'2026-01-{i:02}', close=10) for i in range(5, 11)]
        body = '<div id="history_table">' + ''.join(row(f'202601{i:02}') for i in (5, 6, 7, 9, 10))
        result = verified_flow_history(body, bars, '2026-01-09')
        self.assertNotIn('5', result['net_yi'])
        self.assertEqual(result['rows'][-1]['date'], '2026-01-09')


if __name__ == '__main__':
    unittest.main()
