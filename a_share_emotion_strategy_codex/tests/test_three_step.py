"""Synthetic records only: rule boundaries, evidence discipline and publication."""
import copy
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from three_step_research import BUNDLE, source_hash, calendar_payload, daily_eastmoney
from research_modules import TZ, publish, load_module, record_failure
from research_store import research_path, read_json
from refresh_research import DEFAULT_MODULES, run_module, refresh
from three_step_views import render_page

spec=importlib.util.spec_from_file_location('three_step_test_engine',BUNDLE/'scripts/engine.py')
engine=importlib.util.module_from_spec(spec);spec.loader.exec_module(engine)
NOW=dt.datetime(2026,9,18,8,tzinfo=TZ)
SIGNAL='2026-09-17'
SRC='https://example.invalid/synthetic-evidence'


def state(d):
    return dict(date=d,verified=True,source=SRC,ordinary_a=True,st=False,delisting=False,suspended=False,normal_limit=True)


def fixture(n=1):
    dates=[]
    cursor=dt.date(2025,10,1)
    while cursor<=dt.date(2026,9,18):
        if cursor.weekday()<5: dates.append(str(cursor))
        cursor+=dt.timedelta(days=1)
    prior=[d for d in dates if d<=SIGNAL]
    stocks=[]
    for i in range(n):
        bars=[]
        for d in prior[-210:]:
            close={prior[-4]:'10',prior[-3]:'10.1',prior[-2]:'10.3',prior[-1]:'10.5'}.get(d,'9.5')
            bars.append(dict(date=d,open='10.7',close=close,high='12',low='8',
                             volume_shares='1000000',amount_cny='10000000',turnover_pct='2',complete=True))
        history=[dict(date=d,state=state(d),verified=True,source=SRC,adjustment='none',
                      close='11' if d in (prior[-60],prior[-4],prior[-3]) else '10',limit_up='11',high='11') for d in prior[-60:]]
        history[-1]['close']='10.5'
        stocks.append(dict(code=f'600{i:03d}',name=f'虚构样例{i}',state=state(SIGNAL),
                           turnover=dict(date=SIGNAL,verified=True,source=SRC,denominator='circulating_a_shares',
                                         volume_shares='1000000',float_a_shares='100000000'),
                           daily=dict(provider='eastmoney',adjustment='qfq',adjustment_as_of=SIGNAL,
                                      verified=True,source=SRC,bars=bars),history=history,
                           sources=[dict(url=SRC,label='虚构证据')]))
    return dict(signal_date=SIGNAL,calendar=dict(verified=True,source=SRC,days=dates),
                universe=dict(verified=True,date=SIGNAL,source=SRC,expected_count=n,codes=[s['code'] for s in stocks]),
                stocks=stocks)


def constant(f):
    return lambda bars:[{'date':b['date'],'fraction':f} for b in bars]


def run(data=None,f='.81'):
    return engine.evaluate(data or fixture(),as_of=NOW,calculator=constant(f))


class ThreeStepRulesTests(unittest.TestCase):
    def test_exact_five_percent_and_compounding_not_red_candles(self):
        r=run();self.assertEqual(r['status'],'complete')
        self.assertEqual(len(r['candidates']),1)
        self.assertEqual(r['candidates'][0]['metrics']['three_day_return'],.05)
        # All last-three opens exceed closes: green candles still qualify.
        self.assertTrue(all(float(b['open'])>float(b['close']) for b in fixture()['stocks'][0]['daily']['bars'][-3:]))
        d=fixture();d['stocks'][0]['daily']['bars'][-1]['close']='10.50000001'
        self.assertEqual(run(d)['rows'][0]['steps'][0]['state'],'fail')

    def test_strict_chip_thresholds(self):
        for value,stage in (('.60',0),('.70',1),('.80',2)):
            with self.subTest(value=value):
                r=run(f=value);self.assertEqual(r['rows'][0]['steps'][stage]['state'],'fail')
                self.assertFalse(r['candidates'])
        self.assertTrue(run(f='.80000001')['candidates'])

    def test_daily_decline_equal_close_and_missing_are_not_backfilled(self):
        d=fixture();d['stocks'][0]['daily']['bars'][-2]['close']='10.1'
        self.assertEqual(run(d)['rows'][0]['steps'][0]['state'],'fail')
        d=fixture();d['stocks'][0]['daily']['bars'].pop(-2)
        self.assertEqual(run(d)['rows'][0]['steps'][0]['state'],'pending')

    def test_adjustment_status_and_source_conflicts(self):
        for key,value in (('adjustment','none'),('adjustment_as_of','2026-09-16'),('provider','other'),('conflict',True)):
            d=fixture();d['stocks'][0]['daily'][key]=value
            self.assertEqual(run(d)['rows'][0]['steps'][0]['state'],'pending')
        d=fixture();d['stocks'][0]['history'][-2]['state']['suspended']=True
        self.assertEqual(run(d)['rows'][0]['steps'][0]['state'],'fail')
        d=fixture();d['stocks'][0]['state'].pop('normal_limit')
        self.assertEqual(run(d)['rows'][0]['steps'][0]['state'],'pending')

    def test_global_rank_500_boundary_ties_and_no_rerank(self):
        d=fixture(501)
        # First 499 are price failures but still occupy original global ranks.
        for s in d['stocks'][:499]: s['daily']['bars'][-1]['close']='9'
        r=run(d)
        self.assertEqual([s['code'] for s in r['candidates']],['600499'])
        self.assertEqual(r['rows'][500]['metrics']['turnover_rank'],501)
        self.assertEqual(r['rows'][500]['steps'][1]['state'],'fail')
        self.assertEqual(r['funnel'][0]['fail'],499)
        self.assertEqual(r['funnel'][1]['entered'],2)

    def test_partial_roster_and_wrong_denominator_cannot_rank(self):
        for change in ('missing','denominator','state'):
            d=fixture(2)
            if change=='missing': d['stocks'].pop()
            if change=='denominator': d['stocks'][1]['turnover']['denominator']='free_float'
            if change=='state': d['stocks'][1]['state']={}
            r=run(d);self.assertFalse(r['coverage']['global_rank_verified'])
            self.assertIsNone(r['rows'][0]['metrics']['turnover_rank'])
            self.assertEqual(r['rows'][0]['steps'][1]['state'],'pending')

    def test_sealed_60_boundary_touch_duplicates_and_excluded_days(self):
        d=fixture();self.assertEqual(run(d)['rows'][0]['metrics']['limit_close_count'],3)
        d['stocks'][0]['history'] += [copy.deepcopy(d['stocks'][0]['history'][0])]
        self.assertEqual(run(d)['rows'][0]['metrics']['limit_close_count'],3)
        d['stocks'][0]['history'][0]['close']='10.999' # high touches, not sealed
        d['stocks'][0]['history'].pop() # remove now conflicting duplicate
        self.assertEqual(run(d)['rows'][0]['metrics']['limit_close_count'],2)
        d=fixture();s=d['stocks'][0];s['history'][0]['state']['st']=True
        self.assertEqual(run(d)['rows'][0]['metrics']['limit_close_count'],2)
        d=fixture();d['stocks'][0]['history'].pop(0)
        self.assertEqual(run(d)['rows'][0]['steps'][2]['state'],'pending')
        d=fixture();r=copy.deepcopy(d['stocks'][0]['history'][0]);r['date']=d['calendar']['days'][-62];r['state']=state(r['date'])
        d['stocks'][0]['history'].append(r)
        self.assertEqual(run(d)['rows'][0]['metrics']['limit_close_count'],3)

    def test_window_future_and_unit_guards(self):
        d=fixture();r=run(d)
        self.assertEqual(r['rows'][0]['chip']['bars'],210)
        before=r['rows'][0]['chip']['input_sha256']
        new=copy.deepcopy(d['stocks'][0]['daily']['bars'][-1]);new['date']='2026-09-18';new['close']='11'
        d['stocks'][0]['daily']['bars'].append(new)
        later=run(d);self.assertEqual(before,later['rows'][0]['chip']['input_sha256'])
        self.assertEqual(later['rows'][0]['future_bars_ignored'],1)
        d=fixture();d['stocks'][0]['daily']['bars'].pop(0)
        self.assertEqual(run(d)['rows'][0]['steps'][0]['state'],'pending')
        self.assertEqual(run(f='81')['rows'][0]['steps'][0]['state'],'pending')

    def test_conflicting_same_day_price_or_share_volume_stays_pending(self):
        for kind in ('price','volume'):
            d=fixture()
            if kind=='price': d['stocks'][0]['history'][-1]['close']='10.49'
            else: d['stocks'][0]['turnover']['volume_shares']='999999'
            r=run(d)
            self.assertEqual(r['rows'][0]['steps'][0]['state'],'pending')
            self.assertIn('冲突',r['rows'][0]['steps'][0]['reason'])

    def test_original_kernel_flat_price_and_zero_turnover(self):
        bars=fixture()['stocks'][0]['daily']['bars']
        for b in bars:
            for k in ('open','high','low','close'): b[k]='10'
        r=engine.chip_estimate(bars)
        self.assertAlmostEqual(r[-1]['fraction'],1)
        for b in bars:b['turnover_pct']='0'
        self.assertEqual(engine.chip_estimate(bars)[-1]['fraction'],0)

    def test_model_prefix_has_no_future_lookahead(self):
        bars=fixture()['stocks'][0]['daily']['bars'];a=engine.chip_estimate(bars)
        bars[-1].update(open='99',close='99',high='100',low='98')
        b=engine.chip_estimate(bars)
        self.assertEqual(a[:-1],b[:-1])

    def test_empty_and_unavailable_differ(self):
        d=fixture();d['stocks'][0]['daily']['bars'][-1]['close']='9'
        self.assertEqual(run(d)['status'],'empty')
        d['stocks'][0]['state']={}
        self.assertEqual(run(d)['status'],'partial')


class ThreeStepIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.env=patch.dict(os.environ,{'AUTOSTRATEGY_PRIVATE_ROOT':self.tmp.name});self.env.start();self.addCleanup(self.env.stop)

    def test_premarket_and_intraday_use_previous_complete_close(self):
        for h in (8,10,14):
            signal,cal=calendar_payload(NOW.replace(hour=h))
            self.assertEqual(signal,SIGNAL);self.assertGreater(len([d for d in cal['days'] if d<=signal]),210)
        self.assertEqual(calendar_payload(NOW.replace(hour=15))[0],'2026-09-18')

    def test_unverified_normalized_snapshot_cannot_bypass_engine(self):
        with self.assertRaises(ValueError):run_module('three-step',NOW,'prepare',input_data=run())
        self.assertNotIn('three-step',DEFAULT_MODULES)
        self.assertEqual(len(DEFAULT_MODULES),4)

    def test_partial_and_failed_attempts_preserve_last_success(self):
        r=run();r['rule_hash']=source_hash();r['generated_at']=NOW.isoformat()
        publish('three-step',r,attempted_at=NOW)
        success=read_json(research_path('three_step/last_success.json'),{})
        d=fixture();d['stocks'][0]['state']={};partial=run(d)
        publish('three-step',partial,attempted_at=NOW+dt.timedelta(seconds=1))
        record_failure('three-step','Synthetic fault',now=NOW+dt.timedelta(seconds=2))
        self.assertEqual(success,read_json(research_path('three_step/last_success.json'),{}))
        self.assertEqual(load_module('three-step',NOW+dt.timedelta(seconds=3))['state'],'unavailable')

    def test_stale_run_cannot_overwrite_and_pages_have_charts(self):
        r=run();publish('three-step',r,attempted_at=NOW)
        old=dict(r,run_started_at=(NOW-dt.timedelta(seconds=1)).isoformat())
        self.assertEqual(publish('three-step',old,attempted_at=NOW+dt.timedelta(seconds=1))['status'],'superseded')
        page=render_page(NOW)
        self.assertIn('<svg',page);self.assertIn('three-step-600000',page)
        self.assertIn('data-research-search="three-step"',page)
        self.assertIn('历史结果',render_page(NOW+dt.timedelta(days=3)))
        self.assertNotIn('/Users/',page)

    def test_explicit_refresh_dispatch_and_no_other_module_writes(self):
        with patch('three_step_research.research',return_value=run()):
            result=refresh(['three-step'],as_of=NOW,rebuild=False)
        self.assertEqual(result['modules'][0]['module'],'three-step')
        self.assertFalse(research_path('dragon/current.json').exists())

    def test_two_retry_same_date_cache_and_wrong_date_rejected(self):
        class Feed:
            count=0
            broken=False
            def read(self,url):
                self.count+=1
                if self.broken: raise OSError('artificial offline')
                return json.dumps({'data':{'code':'600000','klines':[SIGNAL+',10,10.5,12,8,100,100000,1,1,1,2']}}),{'url':url}
        feed=Feed();value,_,cache=daily_eastmoney('600000',SIGNAL,feed)
        self.assertEqual(value['bars'][0]['volume_shares'],'10000.0');self.assertFalse(cache)
        feed.broken=True;feed.count=0
        value,errors,cache=daily_eastmoney('600000',SIGNAL,feed)
        self.assertEqual(feed.count,2);self.assertTrue(cache);self.assertEqual(len(errors),2)
        value,_,cache=daily_eastmoney('600000','2026-09-16',feed)
        self.assertIsNone(value);self.assertFalse(cache)


if __name__=='__main__': unittest.main()
