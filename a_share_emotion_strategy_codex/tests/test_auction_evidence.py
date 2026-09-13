"""Artificial minute evidence only; no network or actual stock recommendations."""
import datetime as dt
import unittest
from zoneinfo import ZoneInfo

from auction_evidence import parse

TZ = ZoneInfo('Asia/Shanghai')
DAY = '2026-09-08'
CODE = '600001'


def payload(stamp='09:25', volume=100, amount=110000):
    return {'rc': 0, 'data': {'code': CODE, 'trends': [
        f'{DAY} 09:24,11,11,11,11,0,0,10',
        f'{DAY} {stamp},11,11,11,11,{volume},{amount},11']}}


class AuctionTests(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 9, 8, 9, 55, tzinfo=TZ)
        self.reference = {'verified': True, 'effective_date': DAY,
            'source_asof': DAY+'T09:25:00+08:00', 'source_url': 'https://example.invalid/reference',
            'kind': 'official_price_reference', 'price': 10}
        self.floating = dict(self.reference, kind='unrestricted_float_shares', shares=1000000)

    def test_exact_final_units_and_independent_denominators(self):
        result = parse(payload(), CODE, self.now, reference_evidence=self.reference, float_evidence=self.floating)
        self.assertTrue(result['qualified'])
        self.assertEqual(result['volume_shares'], 10000)
        self.assertEqual(result['turnover_pct'], 1)
        self.assertAlmostEqual(result['gap_pct'], 10)
        self.assertFalse(result['process_verified'])

    def test_next_minute_is_not_relabelled_as_final_auction(self):
        p = payload('09:26')
        p['data']['trends'].insert(1, f'{DAY} 09:25,11,11,11,11,0,0,10')
        result = parse(p, CODE, self.now)
        self.assertFalse(result['verified'])
        self.assertIn('9:26', result['reason'])
        self.assertTrue(result['minute_observations'][-1]['source_asof'].endswith('09:26:00+08:00'))

    def test_missing_denominator_does_not_use_daily_turnover(self):
        result = parse(payload(), CODE, self.now)
        self.assertTrue(result['verified'])
        self.assertFalse(result['qualified'])
        self.assertIsNone(result['turnover_pct'])
        self.assertIsNone(result['gap_pct'])

    def test_float_of_wrong_day_or_kind_fails(self):
        for override in ({'effective_date': '2026-09-07'}, {'kind': 'free_float_shares'}, {'source_asof': DAY+'T10:00:00+08:00'}):
            result = parse(payload(), CODE, self.now, reference_evidence=self.reference,
                           float_evidence=dict(self.floating, **override))
            self.assertFalse(result['qualified'])
            self.assertIsNone(result['turnover_pct'])

    def test_stale_future_conflict_or_wrong_units_not_final(self):
        wrong_day = payload(); wrong_day['data']['trends'] = [s.replace(DAY,'2026-09-07') for s in wrong_day['data']['trends']]
        duplicate = payload(); duplicate['data']['trends'].append(duplicate['data']['trends'][-1])
        for p in (wrong_day, duplicate, payload(amount=1100), payload(volume=0), payload('09:30')):
            self.assertFalse(parse(p,CODE,self.now)['verified'])
        self.assertFalse(parse(payload(),CODE,self.now.replace(hour=9,minute=24))['verified'])

    def test_non_high_open_is_verified_but_not_qualified(self):
        result = parse(payload(), CODE, self.now, reference_evidence=dict(self.reference,price=12), float_evidence=self.floating)
        self.assertTrue(result['verified'])
        self.assertFalse(result['qualified'])


if __name__ == '__main__':
    unittest.main()
