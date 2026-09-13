"""Artificial V3.4 cases; never fetch real prices or read private results."""
import copy
import datetime as dt
import unittest
from unittest.mock import patch

import prelaunch_research as p

NOW = dt.datetime(2026, 9, 11, 16, tzinfo=p.TZ)
DAY = '2026-09-11'
SOURCE = [{'label': '虚构证据', 'url': 'https://example.org/fictional'}]


def dated(**kwargs):
    return dict(verified=True, as_of=DAY, sources=SOURCE, **kwargs)


def fixture(count=1):
    dates = p._sessions(NOW.date(), 89, -1) + [DAY]
    bars = [{'date': day, 'open': 10., 'high': 10.8, 'low': 9.9, 'close': 10.,
             'volume_shares': 1000., 'amount_cny': 10000., 'limit_up': False,
             'limit_verified': True} for day in dates]
    bars[-4].update(open=10., high=10.25, low=10., close=10.2, volume_shares=2000., amount_cny=20400.)
    for b, close in zip(bars[-3:], (10.15, 10.16, 10.18)):
        b.update(open=close, high=close+.02, low=close-.02, close=close,
                 volume_shares=800., amount_cny=close*800.)
    benchmark = copy.deepcopy(bars)
    for i, b in enumerate(benchmark):
        c = 110. - i * .1
        b.update(open=c, high=c+.1, low=c-.1, close=c)
    stocks = [dict(code=f'600{n:03d}', name=f'虚构股票{n}', industry='虚构一级行业',
                   bars=copy.deepcopy(bars), float_cap_cny=1e10, cap_date=DAY, cap_verified=True,
                   adjustment_basis='fictional-fixed-basis', history_verified=True, sources=SOURCE,
                   security=dated(ordinary_a=True, normal_limits=True, suspended=False, risk_clear=True))
              for n in range(count)]
    data = {'signal_date': DAY, 'stocks': stocks, 'benchmark': {'code': '000300', 'bars': benchmark, 'verified': True},
            'market': dated(above_ma20_ratio=.6), 'industries': {
                '虚构一级行业': dated(classification='虚构固定一级分类', r5=0., r20=0.,
                                  above_ma20_ratio=.6, outperform_market_days5=4)},
            'coverage': {'universe_expected': count, 'history_requested': count}, 'sources': SOURCE}
    return data


def enrichment(data, **kwargs):
    stocks = {}
    for s in data['stocks']:
        stocks[s['code']] = dict(pressure=dated(lower=10.8, upper=10.85, method='虚构最近前高', proxy=True),
                                costs=dated(k_per_share=.01, assumptions='虚构名义金额及往返成本情景'),
                                business=dated(level=0))
    return dict(signal_date=DAY, input_fingerprint=p.fingerprint(data), rules_source_hash=p.SOURCE_HASH,
                stocks=stocks, **kwargs)


def run(data=None, review=True, **kwargs):
    data = fixture() if data is None else data
    extra = enrichment(data) if review is True else review or None
    return p.research(NOW, input_data=data, enrichment=extra, **kwargs)


class PrelaunchResearchTest(unittest.TestCase):
    def test_complete_core_uses_fixed_rules(self):
        r = run()
        self.assertEqual(r['status'], 'complete', r['missing'])
        self.assertEqual(len(r['core']), 1, r['watch'])
        row = r['core'][0]
        self.assertGreaterEqual(row['metrics']['净空间比'], 2)
        self.assertTrue(row['eligible'])
        self.assertIsNotNone(row['total_score'])
        self.assertEqual(r['parameters']['benchmark'], '沪深300')

    def test_no_enrichment_no_core_or_total_score(self):
        r = run(review=False)
        self.assertEqual(r['core'], [])
        self.assertIsNone(r['watch'][0]['total_score'])
        self.assertIsNone(r['watch'][0]['metrics']['净空间比'])

    def test_unknown_cost_only_gross_space(self):
        d = fixture(); e = enrichment(d); del e['stocks']['600000']['costs']
        r = run(d, e)
        self.assertEqual(r['core'], [])
        self.assertGreater(r['watch'][0]['metrics']['毛空间比'], 2)
        self.assertIsNone(r['watch'][0]['metrics']['净空间比'])

    def test_nearer_pressure_cannot_be_skipped(self):
        d = fixture(); e = enrichment(d)
        e['stocks']['600000']['pressure']['lower'] = 10.2
        r = run(d, e)
        self.assertEqual(r['core'], [])
        self.assertLess(r['watch'][0]['metrics']['净空间比'], 2)

    def test_enrichment_binding_rejects_wrong_data_day_or_rule(self):
        for key in ('signal_date', 'input_fingerprint', 'rules_source_hash'):
            d = fixture(); e = enrichment(d); e[key] = 'mismatch'
            r = run(d, e)
            self.assertEqual(r['core'], [])
            self.assertFalse(r['evidence_review_applied'])

    def test_source_disclosed_after_signal_close_does_not_pass(self):
        d = fixture(); e = enrichment(d)
        e['stocks']['600000']['business']['sources'] = [dict(SOURCE[0], published_at=DAY + 'T19:00:00+08:00')]
        self.assertEqual(run(d, e)['core'], [])

    def test_old_report_cannot_be_redated(self):
        d = fixture(); d['signal_date'] = '2026-09-04'
        r = run(d)
        self.assertEqual(r['status'], 'unavailable')
        self.assertEqual(r['candidates'], [])

    def test_market_unknown_or_defensive_never_core(self):
        for market in ({}, dated(above_ma20_ratio=.2)):
            d = fixture(); d['market'] = market
            self.assertEqual(run(d)['core'], [])

    def test_industry_missing_never_core(self):
        d = fixture(); d['industries'] = {}
        self.assertEqual(run(d)['core'], [])

    def test_cap_boundary_and_unknown_date(self):
        for cap, date, expected in ((5e10, DAY, 1), (5e10+.1, DAY, 0), (1e10, '2026-09-10', 0), (None, DAY, 0)):
            d = fixture(); d['stocks'][0].update(float_cap_cny=cap, cap_date=date)
            self.assertEqual(len(run(d)['core']), expected)

    def test_permission_before_score(self):
        d = fixture(); d['stocks'][0]['code'] = '300001'
        r = run(d)
        self.assertEqual(r['core'], [])
        self.assertEqual(r['invalid'][0]['status'], '排除')

    def test_65_bars_required(self):
        for count, expected in ((64, 0), (65, 1)):
            d = fixture(); d['stocks'][0]['bars'] = d['stocks'][0]['bars'][-count:]
            self.assertEqual(len(run(d)['core']), expected)

    def test_65_recent_sessions_require_no_missing_middle_day(self):
        d = fixture(); del d['stocks'][0]['bars'][-12]
        self.assertEqual(run(d)['core'], [])
        d = fixture(); del d['benchmark']['bars'][-12]
        self.assertEqual(run(d)['core'], [])

    def test_benchmark_identity_must_be_hs300(self):
        d = fixture(); d['benchmark']['code'] = '399006'
        self.assertEqual(run(d)['core'], [])

    def test_far_review_pressure_cannot_skip_known_platform(self):
        d = fixture(); e = enrichment(d)
        e['stocks']['600000']['pressure']['lower'] = 12.
        row = run(d, e)['core'][0]
        self.assertEqual(next(x['value'] for x in row['levels'] if x['label'] == '最近压力'), 10.8)
        self.assertLess(row['metrics']['净空间比'], 3)

    def test_limit_attestation_does_not_erase_verified_limit(self):
        d = fixture(); d['stocks'][0]['bars'][-5]['limit_up'] = True
        e = enrichment(d)
        e['stocks']['600000']['limit_checks'] = dated(dates={b['date']: {
            'raw_close': b['close'], 'official_limit_up': 11.5, 'normal_limits': True,
            'sources': SOURCE} for b in d['stocks'][0]['bars'][-5:]})
        r = run(d, e)
        self.assertEqual(r['core'], [])
        self.assertEqual(len(r['started']), 1)
        self.assertTrue(any('冲突' in c['label'] for c in r['started'][0]['conditions']))

    def test_flat_probe_clv_not_assumed_strong(self):
        d = fixture(); d['stocks'][0]['bars'][-4].update(open=10.2, high=10.2, low=10.2, close=10.2)
        r = run(d)
        self.assertEqual(r['core'], [])
        self.assertIsNone(r['watch'][0]['metrics']['试盘日期'])

    def test_actual_limit_overrides_good_shape(self):
        d = fixture(); d['stocks'][0]['bars'][-5]['limit_up'] = True
        r = run(d)
        self.assertEqual(r['core'], [])
        self.assertEqual(r['started'][0]['status'], '已启动')

    def test_unknown_actual_limit_does_not_pass(self):
        d = fixture(); d['stocks'][0]['bars'][-5]['limit_verified'] = False
        self.assertEqual(run(d)['core'], [])

    def test_dated_review_can_complete_industry_and_limit_evidence(self):
        d = fixture(); s = d['stocks'][0]; s['industry'] = None
        for b in s['bars'][-5:]:
            b.update(limit_up=None, limit_verified=False)
        e = enrichment(d)
        e['stocks']['600000']['industry'] = dated(id='虚构一级行业', classification='虚构固定一级分类')
        e['stocks']['600000']['limit_checks'] = dated(dates={b['date']: {
            'raw_close': b['close'], 'official_limit_up': 11.5, 'normal_limits': True,
            'sources': SOURCE} for b in s['bars'][-5:]})
        self.assertEqual(len(run(d, e)['core']), 1)

    def test_prior_platform_price_revision_does_not_silently_refreeze(self):
        d = fixture(); old = run(d)
        d['stocks'][0]['bars'][-12]['low'] = 9.8
        r = run(d, previous=old)
        self.assertEqual(r['core'], [])
        self.assertEqual(r['frozen_records'][0]['support'], 9.9)

    def test_probe_requires_two_complete_days(self):
        d = fixture(); bars = d['stocks'][0]['bars']
        bars[-4].update(volume_shares=1000.)
        bars[-2].update(open=10.15, high=10.21, low=10.15, close=10.2, volume_shares=2000.)
        r = run(d)
        self.assertEqual(r['core'], [])

    def test_intermediate_support_failure_not_cured_by_latest_recovery(self):
        d = fixture()
        d['stocks'][0]['bars'][-3].update(open=9.85, high=9.9, low=9.8, close=9.85)
        self.assertEqual(run(d)['core'], [])

    def test_freeze_is_idempotent(self):
        d = fixture(); first = run(d); second = run(d, previous=first)
        self.assertEqual(first['frozen_records'], second['frozen_records'])
        self.assertEqual(len(second['frozen_records']), 1)

    def test_basis_change_needs_review_and_never_refreezes(self):
        d = fixture(); old = run(d); d['stocks'][0]['adjustment_basis'] = 'changed'
        r = run(d, previous=old)
        self.assertEqual(r['core'], [])
        self.assertEqual(r['frozen_records'][0]['support'], old['frozen_records'][0]['support'])

    def test_frozen_support_failure_keeps_history(self):
        d = fixture(); old = run(d)
        d['stocks'][0]['bars'][-1].update(open=9.8, high=9.85, low=9.75, close=9.8)
        r = run(d, previous=old)
        self.assertEqual(r['invalid'][0]['status'], '失效')
        self.assertEqual(r['frozen_records'][0]['state'], 'invalid')
        self.assertEqual(r['frozen_records'][0]['support'], 9.9)

    def test_five_trading_day_window(self):
        r = run()
        self.assertEqual(r['valid_until'], '2026-09-18')
        old = copy.deepcopy(r); old['frozen_records'][0]['valid_until'] = '2026-09-10'
        again = run(previous=old)
        self.assertEqual(again['core'], [])
        self.assertEqual(len(again['frozen_records']), 1)
        self.assertEqual(again['frozen_records'][0]['state'], 'expired')

    def test_same_industry_and_catalyst_cap_three(self):
        d = fixture(5); r = run(d)
        self.assertEqual(len(r['core']), 3)
        self.assertEqual([x['code'] for x in r['core']], ['600000', '600001', '600002'])

    def test_missing_amount_prevents_core(self):
        d = fixture(); d['stocks'][0]['bars'][-1]['amount_cny'] = None
        self.assertEqual(run(d)['core'], [])

    def test_vr_previous_twenty_excludes_signal_day(self):
        d = fixture(); m = p._metrics(d['stocks'][0]['bars'])
        self.assertAlmostEqual(m['vr'], .8)
        self.assertAlmostEqual(m['probe']['vr'], 2.)

    def test_fingerprint_ignores_fetch_clock_not_values(self):
        d = fixture(); x = copy.deepcopy(d); x['generated_at'] = 'a'; x['sources'][0]['retrieved_at'] = 'b'
        self.assertEqual(p.fingerprint(d), p.fingerprint(x))
        x['stocks'][0]['float_cap_cny'] += 1
        self.assertNotEqual(p.fingerprint(d), p.fingerprint(x))

    def test_collect_never_networks_for_injected_input(self):
        with patch.object(p, 'collect', side_effect=AssertionError('network')):
            self.assertEqual(run()['status'], 'complete')

    def test_unknown_calendar_returns_unavailable(self):
        data = fixture()
        with patch.object(p, 'is_trading_day', return_value=(False, '缺少交易所日历')):
            r = p.research(NOW, input_data=data)
        self.assertEqual(r['status'], 'unavailable')
        self.assertIsNone(r['as_of'])

    def test_future_bars_are_not_in_signal(self):
        d = fixture(); future = dict(d['stocks'][0]['bars'][-1], date='2026-09-14')
        d['stocks'][0]['bars'].append(future)
        r = run(d)
        self.assertEqual(r['core'][0]['bars'][-1]['date'], DAY)


class PrelaunchIntradayTest(unittest.TestCase):
    def setUp(self):
        self.previous = run()
        self.now = dt.datetime(2026, 9, 14, 10, tzinfo=p.TZ)

    def quote(self, price=10.18, stamp=None):
        return {'quotes': {'600000': {'price': price,
                'timestamp': (stamp or self.now - dt.timedelta(seconds=20)).isoformat(),
                'source': SOURCE[0]}}}

    def check(self, data=None, previous=None, now=None):
        with patch.object(p, 'collect', side_effect=AssertionError('must not rescreen')):
            return p.research(now or self.now, phase='intraday', input_data=data if data is not None else self.quote(),
                              previous=self.previous if previous is None else previous)

    def test_intraday_keeps_close_snapshot_and_window(self):
        r = self.check()
        for key in ('as_of', 'valid_until', 'frozen_records', 'input_fingerprint'):
            self.assertEqual(r[key], self.previous[key])
        self.assertEqual(r['core'][0]['bars'], self.previous['core'][0]['bars'])
        self.assertEqual(r['core'][0]['total_score'], self.previous['core'][0]['total_score'])
        self.assertEqual(r['coverage']['intraday_valid'], 1)
        self.assertEqual(r['intraday_as_of'][:10], '2026-09-14')

    def test_touching_support_is_background_not_close_failure(self):
        r = self.check(self.quote(9.8))
        self.assertEqual(len(r['core']), 1)
        self.assertEqual(r['invalid'], [])
        self.assertEqual(r['frozen_records'][0]['state'], 'active')
        self.assertIn('不是收盘', ' '.join(r['core'][0]['intraday_context']['notes']))

    def test_above_upper_not_new_started_confirmation(self):
        r = self.check(self.quote(11.))
        self.assertEqual(r['started'], [])
        self.assertEqual(r['core'][0]['status'], self.previous['core'][0]['status'])

    def test_no_previous_never_starts_full_scan(self):
        r = self.check(previous={})
        self.assertEqual(r['status'], 'unavailable')
        self.assertEqual(r['candidates'], [])

    def test_expired_or_different_rule_previous_is_rejected(self):
        for key in ('expired', 'rule'):
            old = copy.deepcopy(self.previous)
            if key == 'expired':
                old['valid_until'] = '2026-09-11'
            else:
                old['rules']['source_hash'] = 'wrong'
            self.assertEqual(self.check(previous=old)['status'], 'unavailable')

    def test_stale_or_future_quote_does_not_gain_timestamp(self):
        for stamp in (self.now-dt.timedelta(seconds=91), self.now+dt.timedelta(seconds=1),
                      self.now-dt.timedelta(days=1)):
            r = self.check(self.quote(stamp=stamp))
            self.assertEqual(r['coverage']['intraday_valid'], 0)
            self.assertIsNone(r['intraday_as_of'])
            self.assertEqual(r['status'], 'partial')

    def test_lunch_does_not_fetch_or_treat_morning_quote_live(self):
        lunch = self.now.replace(hour=12)
        with patch.object(p.PublicFeed, 'quotes', side_effect=AssertionError('must not fetch at lunch')):
            r = p.research(lunch, phase='intraday', previous=self.previous)
        self.assertEqual(r['coverage']['intraday_valid'], 0)
        self.assertEqual(r['status'], 'partial')

    def test_fetch_targets_only_existing_valid_watch_rows(self):
        quote = self.quote()['quotes']
        old = copy.deepcopy(self.previous)
        old['started'] = [dict(old['core'][0], code='600001')]
        old['watch'] = [dict(old['core'][0], code='600002', valid_until='2026-09-11')]
        with patch.object(p.PublicFeed, 'quotes', return_value=(quote, SOURCE)) as fetch:
            r = p.research(self.now, phase='intraday', previous=old)
        fetch.assert_called_once_with(['600000'])
        self.assertEqual(r['coverage']['intraday_requested'], 1)

    def test_quote_failure_retains_close_and_can_recover(self):
        failed = self.check({'quotes': {}})
        self.assertEqual(failed['status'], 'partial')
        recovered = self.check(previous=failed)
        self.assertEqual(recovered['status'], 'complete')
        self.assertEqual(recovered['valid_until'], self.previous['valid_until'])


if __name__ == '__main__':
    unittest.main()
