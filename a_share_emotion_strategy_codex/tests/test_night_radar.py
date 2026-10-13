"""Artificial evidence exercises dates, accounting and independent publication."""
import copy
import datetime as dt
import os
import tempfile
import unittest
from unittest.mock import patch

import night_radar as nr
import night_radar_store as store
from research_store import read_json

CUT = dt.datetime(2026,10,12,22,tzinfo=nr.TZ)


def row(**updates):
    result = dict(code='600001',name='虚构公司',source_date='2026-10-12',period_start='2026-10-12',
                  period_end='2026-10-12',period='single',reason='人工原因',event_id='fake',
                  source_url='https://example.com/a',source_time='2026-10-12T20:00:00+08:00',verified=True)
    result.update(updates); return result


def evidence(**sections):
    value = {'schema_version':1,'trade_date':'2026-10-12','sections':
             {k:{'status':'complete','rows':[],'sources':[]} for k in nr.SECTIONS}}
    for key, rows in sections.items(): value['sections'][key]['rows'] = rows
    return value


def normalize(raw, **kwargs):
    return nr.normalize(raw,CUT,executed_at=CUT+dt.timedelta(minutes=1),**kwargs)


class RulesTests(unittest.TestCase):
    def test_mainboard_and_security_unknown(self):
        for code in ('600001','001001','002001','003001'):
            self.assertTrue(nr.mainboard(row(code=code)))
        for code in ('300001','688001','920001','200001','900001','510001','085145'):
            self.assertFalse(nr.mainboard(row(code=code)))
        for name in ('ST虚构','*ST虚构','虚构退','','600001'):
            self.assertFalse(nr.mainboard(row(name=name)))
        self.assertFalse(nr.mainboard(row(security_status='unknown')))

    def test_cross_board_copies_preserve_anonymous_multiplicity(self):
        seat={'seat_id':'0','name':'机构专用','buy_cny':100,'sell_cny':40,'net_cny':60}
        raw=[dict(seat,side='buy'),dict(seat,side='buy'),dict(seat,side='sell')]
        self.assertEqual(len(nr.merge_seats(raw)),2)
        self.assertEqual(sum(s['net_cny'] for s in nr.merge_seats(raw)),120)
        with self.assertRaises(ValueError): nr.merge_seats([dict(seat,net_cny=61)])

    def test_multi_reason_duplicates_and_periods_do_not_add(self):
        seats=[dict(seat_id='1',name='虚构营业部',buy_cny=50000,sell_cny=10000,net_cny=40000,side='buy')]
        rows=[row(seats=seats),row(seats=seats,reason='另一原因',event_id='copy'),
              row(seats=seats,period='multi',period_start='2026-10-09')]
        report=normalize(evidence(broker_seats=rows))['sections']['broker_seats']
        self.assertEqual(len(report['featured']),1)
        self.assertEqual(report['featured'][0]['net_cny'],40000)
        self.assertEqual(len(report['multi_rows']),1)
        rows[1]['seats']=[dict(seats[0],buy_cny=60000,net_cny=50000)]
        bad=normalize(evidence(broker_seats=rows))['sections']['broker_seats']
        self.assertEqual(bad['featured'],[])
        self.assertEqual(bad['status'],'partial')

    def test_no_unknown_or_future_time_and_verified_only(self):
        rows=[row(),row(event_id='late',source_time='2026-10-12T22:01:00+08:00'),
              row(event_id='missing',source_time=None),row(event_id='unverified',verified=False)]
        section=normalize(evidence(announcements=rows))['sections']['announcements']
        self.assertEqual(len(section['featured']),1)
        self.assertEqual(len(section['rows']),2)
        self.assertEqual(section['status'],'partial')

    def test_source_conflict_only_blocks_affected_column(self):
        report=normalize(evidence(limit_up=[row(boards=3,source_date='2026-10-13',period_end='2026-10-13')],
                                  institutions=[row(net_cny=100000)]))
        self.assertEqual(report['sections']['limit_up']['featured'],[])
        self.assertEqual(len(report['sections']['institutions']['featured']),1)
        self.assertEqual(report['status'],'partial')

    def test_positive_only_institution_top_does_not_fill(self):
        report=normalize(evidence(institutions=[row(net_cny=100000),row(code='600002',net_cny=0),
                           row(code='600003',net_cny=-100),row(code='300001',net_cny=999999)]))
        self.assertEqual([r['code'] for r in report['sections']['institutions']['featured']],['600001'])

    def test_equal_institution_net_does_not_hide_different_turnover_totals(self):
        section = normalize(evidence(institutions=[
            row(buy_cny=300, sell_cny=200, net_cny=100),
            row(event_id='second-reason', buy_cny=500, sell_cny=400, net_cny=100)
        ]))['sections']['institutions']
        self.assertEqual(section['featured'], [])
        self.assertEqual(section['status'], 'partial')

    def test_missing_industry_is_explicit_without_discarding_verified_net(self):
        section = normalize(evidence(institutions=[row(net_cny=100)]))['sections']['institutions']
        self.assertEqual(len(section['featured']), 1)
        self.assertEqual(section['status'], 'partial')
        self.assertTrue(any('行业' in reason for reason in section['missing']))

    def test_first_window_late_arrival_and_changed_event(self):
        early=row(source_time='2026-10-10T20:00:00+08:00')
        self.assertEqual(normalize(evidence(announcements=[early]))['sections']['announcements']['rows'],[])
        later=normalize(evidence(announcements=[early]),seen={'announcements':{'other':'hash'}})
        self.assertTrue(later['sections']['announcements']['rows'][0]['late_arrival'])
        seen={'announcements':{nr.event_key(early):nr.row_version(early)}}
        self.assertEqual(normalize(evidence(announcements=[early]),seen=seen)['sections']['announcements']['rows'],[])
        changed=normalize(evidence(announcements=[dict(early,title='更正金额')]),seen=seen)
        self.assertEqual(changed['sections']['announcements']['rows'][0]['change'],'更正或更新')

    def test_halt_announces_before_cutoff_marks_close_ladder(self):
        report=normalize(evidence(limit_up=[row(boards=3),row(code='600002',boards=1)],
                                  announcements=[row(stance='risk',summary='明日起停牌')]))
        self.assertEqual(len(report['sections']['limit_up']['rows']),1)
        self.assertEqual(report['sections']['limit_up']['featured'][0]['risk'],'明日起停牌')

    def test_bad_board_does_not_destroy_valid_other_column(self):
        report=normalize(evidence(limit_up=[row(boards='bad')],announcements=[row(title='人工重大合同')]))
        self.assertEqual(report['sections']['limit_up']['status'],'partial')
        self.assertEqual(len(report['sections']['announcements']['featured']),1)

    def test_duplicate_and_conflicting_disclosures(self):
        good=row(title='人工合同')
        report=normalize(evidence(announcements=[good,good]))
        self.assertEqual(len(report['sections']['announcements']['featured']),1)
        bad=normalize(evidence(announcements=[good,dict(good,title='冲突金额')]))
        self.assertEqual(bad['sections']['announcements']['featured'],[])
        self.assertEqual(bad['sections']['announcements']['status'],'partial')


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.env=patch.dict(os.environ,{'AUTOSTRATEGY_PRIVATE_ROOT':self.tmp.name})
        self.env.start(); self.addCleanup(self.env.stop)

    def test_repeat_cutoff_retains_original_window_and_does_not_erase_news(self):
        raw=evidence(announcements=[row(title='人工披露')])
        report=normalize(raw); first=store.publish_report(report)
        window,seen=store.window_context(CUT)
        repeated=normalize(raw,window_start=window,seen=seen)
        result=store.publish_report(repeated)
        self.assertFalse(result['changed'])
        self.assertEqual(len(store.load_reports()[0]['sections']['announcements']['featured']),1)
        store.mark_sent(report['report_date'],first['edition'])
        self.assertFalse(store.delivery_decision(repeated)['notify'])

    def test_trial_and_historical_do_not_advance_daily_state(self):
        report=normalize(evidence())
        for mode in ('trial','historical'):
            store.publish_report(dict(report,mode=mode))
            self.assertEqual(read_json(store.folder()/'state.json',{}),{})

    def test_same_day_later_run_retains_news_and_does_not_resend(self):
        report=normalize(evidence(announcements=[row(title='人工披露')]))
        first=store.publish_report(report); store.mark_sent(report['report_date'],first['edition'])
        window,seen=store.window_context(CUT+dt.timedelta(minutes=2))
        self.assertEqual(window,nr.stamp(report['window_start']))
        self.assertEqual(seen,{})
        later=dict(report,cutoff=(CUT+dt.timedelta(minutes=2)).isoformat(),executed_at=(CUT+dt.timedelta(minutes=3)).isoformat())
        self.assertFalse(store.publish_report(later)['notify'])
        self.assertEqual(len(store.load_reports()[0]['sections']['announcements']['featured']),1)

    def test_first_failure_preserves_unfinished_start_across_multiple_days(self):
        report=normalize({'schema_version':1,'sections':{}})
        store.publish_report(report)
        window,_=store.window_context(CUT+dt.timedelta(days=6))
        self.assertEqual(window,CUT-dt.timedelta(days=1))

    def test_old_publication_cannot_replace_newer_day_cutoff(self):
        report=normalize(evidence())
        store.publish_report(report)
        earlier=copy.deepcopy(report)
        earlier['cutoff']=(CUT-dt.timedelta(hours=1)).isoformat()
        self.assertEqual(store.publish_report(earlier)['status'],'superseded')
        self.assertEqual(store.load_reports()[0]['cutoff'],CUT.isoformat())

    def test_holiday_silence_new_news_failure_and_recovery(self):
        report=normalize(evidence()); report['trading_day']=False
        self.assertFalse(store.delivery_decision(report)['notify'])
        report['sections']['announcements']['rows']=[row()]
        self.assertTrue(store.delivery_decision(report)['notify'])
        report['sections']['announcements']['rows']=[]
        report['sections']['announcements'].update(status='unavailable',missing=['人工故障'])
        outcome=store.publish_report(report)
        self.assertTrue(outcome['notify'])
        store.mark_sent(report['report_date'],outcome['edition'])
        self.assertFalse(store.delivery_decision(report)['notify'])
        report['sections']['announcements'].update(status='empty',missing=[])
        self.assertTrue(store.delivery_decision(report)['notify'])


if __name__=='__main__': unittest.main()
