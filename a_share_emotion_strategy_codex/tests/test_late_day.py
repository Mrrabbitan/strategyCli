"""Artificial evidence only; no market requests, orders or personal data."""
import copy
import datetime as dt
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from late_day_research import load_engine, run_research, source_hash, frozen_positions, publish_report, topic_payload, TZ
from research_store import research_path, read_json
from research_topics import render_topics


def sample(at=None):
    engine=load_engine()
    at=at or dt.datetime(2026,1,8,14,30,tzinfo=TZ)
    start=at.date()-dt.timedelta(days=60)
    dates=[start+dt.timedelta(days=i) for i in range(80) if (start+dt.timedelta(days=i)).weekday()<5]
    history=[{'date':str(d),'high':'10.5','upper_limit':'11','volume_shares':'8000000'} for d in dates if d<at.date()][-20:]
    history[0]['high']='11'
    bars=[{'end':t.isoformat(),'close':'10.4','low':'10.25','high':'10.5',
           'volume_shares':'100000','amount_cny':'1030000'} for t in engine.endpoints(at)]
    volume=100000+len(bars)*100000
    amount=1020000+len(bars)*1030000
    source='https://example.org/artificial'
    stock={'code':'600001','name':'Artificial sample; not a real recommendation',
        'security':{'source':source,'date':str(at.date()),'verified':True,'mainboard':True,'st':False,
                    'suspended':False,'delisting':False,'normal_limit':True},
        'shares':{'source':source,'valid_on':str(at.date()),'verified':True,'float_a':volume*10,'total':1000000000},
        'quote':{'source':source,'time':at.isoformat(),'price':'10.4','reference_close':'10','high':'10.5','upper_limit':'11',
                 'volume_shares':volume,'amount_cny':amount,'scope':'includes_opening_auction'},
        'history_meta':{'source':source,'verified':True,'adjustment':'none','limits_verified':True,
                        'volume_comparable':True,'scope':'includes_opening_auction'},'history':history,
        'minute_meta':{'source':source,'verified':True,'label':'interval_end','scope':'continuous_only',
                       'volume_unit':'share','amount_unit':'CNY'},'minutes':bars,
        'opening_auction':{'source':source,'time':at.replace(hour=9,minute=25).isoformat(),'verified':True,'final':True,
                           'price':'10.2','volume_shares':100000,'amount_cny':1020000},
        'industry':{'source':source,'time':at.isoformat(),'classification':'eastmoney_industry','mapping_verified':True,
                    'mapping_date':str(at.date()),'code':'ARTIFICIAL','pct':'1'}}
    return {'schema_version':1,'as_of':at.isoformat(),'stocks':[stock],
        'calendar':{'source':source,'verified':True,'valid_from':str(start),'valid_until':str(dates[-1]),'days':[str(d) for d in dates]},
        'coverage':{'verified':True,'universe_count':1,'scanned_count':1,'description':'Artificial single-stock fixture'},
        'market':{'source':source,'time':at.isoformat(),'index_pct':'1','rising':1700,'falling':1000,'flat':100,
                  'total':2800,'universe_verified':True}}


class LateDayRulesTests(unittest.TestCase):
    def setUp(self):
        self.e=load_engine();self.data=sample();self.at=self.e.stamp(self.data['as_of'])

    def evaluate(self,data=None,phase='live',now=None):
        value=data or self.data
        return self.e.evaluate(value,phase=phase,now=now or self.e.stamp(value['as_of']))

    def condition(self,key,data=None):
        return next(c for c in self.evaluate(data)['rows'][0]['checks'] if c['id']==key)['passed']

    def test_valid_and_exact_ranking_not_eligibility(self):
        r=self.evaluate();self.assertEqual(r['qualified_count'],1)
        self.assertEqual(r['state'],'qualified');self.assertFalse(r['rows'][0]['eligible'])
        self.assertEqual(r['valid_until'],'2026-01-08T14:31:30+08:00')

    def test_window_boundaries(self):
        for hour,minute,expected in ((14,29,False),(14,30,True),(14,56,True),(14,57,False)):
            d=sample(self.at.replace(hour=hour,minute=minute))
            self.assertIs(self.condition('time',d),expected)

    def test_raw_precision_price_boundaries(self):
        for price,expected in (('10.3',True),('10.6',True),('10.6000001',False),('10.2999999',False)):
            self.data['stocks'][0]['quote'].update(price=price,high='10.7')
            self.assertIs(self.condition('price'),expected)

    def test_turnover_and_cap_boundaries(self):
        stock=self.data['stocks'][0];volume=Decimal('21000000');stock['quote']['volume_shares']=str(volume)
        for rate,expected in (('6',True),('15',True),('5.99',False),('15.01',False)):
            stock['shares']['float_a']=str(volume*100/Decimal(rate))
            self.assertIs(self.condition('turnover'),expected)
        stock['shares']['float_a']='100000000'
        stock['quote']['price']='10'
        for total,expected in ((600000000,True),(3000000000,True),(599999999,False),(3000000001,False)):
            stock['shares']['total']=total;self.assertIs(self.condition('cap'),expected)

    def test_ratio_boundaries_native_and_conflict(self):
        q=self.data['stocks'][0]['quote']
        for value,expected in (('2',True),('5',True),('1.99',False),('5.01',False)):
            q.update(ratio_verified=True,ratio_method=self.e.RATIO_METHOD,volume_ratio=value)
            daily=Decimal(str(q['volume_shares']))/210/Decimal(value)*240
            for row in self.data['stocks'][0]['history']:row['volume_shares']=str(daily)
            self.assertIs(self.condition('ratio'),expected)
        q['ratio_method']='same_time_volume';self.assertIsNone(self.condition('ratio'))

    def test_early_dips_and_recent_dip(self):
        s=self.data['stocks'][0]
        for r in s['minutes'][:20]: r['close']='10.25'
        self.assertIs(self.condition('vwap'),True)
        s['minutes'][-1]['close']='10.25';self.assertIs(self.condition('vwap'),False)

    def test_exact_ninety_percent_and_first_equality(self):
        for row in self.data['stocks'][0]['minutes'][:22]:row['close']='10.25'
        self.assertIs(self.condition('vwap'),True) # first is equal; 21 of 210 below

    def test_declared_zero_volume_cannot_hide_missing_trades(self):
        row=self.data['stocks'][0]['minutes'][2]
        row.update(volume_shares=0,amount_cny=0,low='10.4',high='10.4')
        self.assertIsNone(self.condition('vwap'))

    def test_coverage_unknown_never_upgrades_current_candidate(self):
        self.data['coverage']['verified']=False
        self.assertEqual(self.evaluate()['qualified_count'],0)

    def test_all_qualified_rows_preserve_turnover_order(self):
        first=self.data['stocks'][0]
        rows=[]
        for i in range(1,7):
            stock=copy.deepcopy(first);stock['code']=f'60000{i}'
            for key in ('volume_shares','amount_cny'):stock['quote'][key]*=i;stock['opening_auction'][key]*=i
            stock['shares']['float_a']*=i;stock['shares']['total']*=i
            # Keep cap within 300yi without losing sufficient float shares.
            stock['shares']['total']=max(stock['shares']['float_a'],1000000000)
            for bar in stock['minutes']:
                for key in ('volume_shares','amount_cny'):bar[key]=str(Decimal(bar[key])*i)
            for bar in stock['history']:bar['volume_shares']=str(Decimal(bar['volume_shares'])*i)
            rows.append(stock)
        self.data['stocks']=rows;self.data['coverage'].update(universe_count=6,scanned_count=6)
        result=self.evaluate();self.assertEqual(result['qualified_count'],6)
        self.assertEqual(result['display_codes'],['600006','600005','600004','600003','600002','600001'])
        historical=self.evaluate(phase='review',now=self.at+dt.timedelta(hours=5))
        self.assertEqual(historical['qualified_count'],0)
        self.assertEqual(historical['historical_pass_count'],6)
        historical['rule_hash']=source_hash()
        self.assertEqual(len(topic_payload(historical)['late_day_result']['rows']),6)

    def test_unknown_coverage_is_not_historical_pass(self):
        self.data['coverage']['verified']=False
        r=self.evaluate(phase='review')
        self.assertEqual(r['historical_pass_count'],0)
        self.assertFalse(r['rows'][0]['research_passed'])
        self.assertEqual(r['rows'][0]['decision_state'],'insufficient')

    def test_below_ninety_percent_rejected(self):
        for row in self.data['stocks'][0]['minutes'][:23]:row['close']='10.25'
        self.assertIs(self.condition('vwap'),False)

    def test_missing_duplicate_future_and_lunch_minutes(self):
        for mode in ('missing','duplicate','future','lunch'):
            d=copy.deepcopy(self.data);rows=d['stocks'][0]['minutes']
            if mode=='missing': rows.pop(3)
            if mode=='duplicate':rows.insert(3,copy.deepcopy(rows[3]))
            if mode=='future':rows[-1]['end']='2026-01-08T14:31:00+08:00'
            if mode=='lunch':rows[3]['end']='2026-01-08T12:00:00+08:00'
            self.assertIsNone(self.condition('vwap',d),mode)

    def test_wrong_units_and_auction_scope(self):
        d=copy.deepcopy(self.data);d['stocks'][0]['minutes'][1]['volume_shares']=1000
        self.assertIsNone(self.condition('vwap',d))
        self.data['stocks'][0]['opening_auction']['verified']=False
        self.assertIsNone(self.condition('vwap'))

    def test_touch_not_close_and_excludes_today(self):
        self.assertIs(self.condition('touch'),True)
        stock=self.data['stocks'][0];stock['history'][0]['high']='10.5'
        self.assertIs(self.condition('touch'),False)
        stock['history'][0]['date']=str(self.at.date());self.assertIsNone(self.condition('touch'))

    def test_today_failed_limit_separate_veto(self):
        self.data['stocks'][0]['quote']['high']='11'
        self.assertIs(self.condition('touch'),True);self.assertIs(self.condition('failed_limit'),False)

    def test_market_sector_and_incomplete_breadth(self):
        self.data['market'].update(index_pct='-1',rising=1000,falling=1700)
        self.assertIs(self.condition('market'),False)
        self.data['market']['index_pct']='1';self.data['stocks'][0]['industry']['pct']='0'
        self.assertIs(self.condition('market'),False)
        self.data['market']['total']=3000;self.assertIsNone(self.condition('market'))

    def test_distance_and_staleness(self):
        q=self.data['stocks'][0]['quote'];q.update(price='10.7',high='10.8')
        self.assertIs(self.condition('distance'),False)
        q['time']=(self.at-dt.timedelta(seconds=91)).isoformat();self.assertIsNone(self.condition('price'))

    def test_quote_spread_and_future_source(self):
        self.data['stocks'][0]['industry']['time']=(self.at-dt.timedelta(seconds=61)).isoformat()
        self.assertEqual(self.evaluate()['qualified_count'],0)
        self.data['stocks'][0]['quote']['time']=(self.at+dt.timedelta(seconds=1)).isoformat()
        self.assertIsNone(self.condition('price'))

    def test_historical_or_old_evaluation_never_current(self):
        for phase,now in (('review',self.at),('live',self.at+dt.timedelta(seconds=91))):
            r=self.evaluate(phase=phase,now=now)
            self.assertEqual(r['qualified_count'],0);self.assertTrue(r['rows'][0]['research_passed'])
            self.assertEqual(r['rows'][0]['state'],'historical')

    def test_calendar_and_duplicate_symbols_fail_closed(self):
        self.data['calendar']['verified']=False
        with self.assertRaises(ValueError):self.evaluate()
        self.data=sample();self.data['stocks']*=2
        with self.assertRaises(ValueError):self.evaluate()

    def test_empty_distinct_from_no_data(self):
        self.data['stocks']=[];self.data['coverage']['scanned_count']=0
        self.assertEqual(self.evaluate()['state'],'empty')
        self.data['coverage']['verified']=False;self.assertEqual(self.evaluate()['state'],'insufficient')

    def position(self):
        s=self.data['stocks'][0]
        return {'id':'artificial-entry','code':s['code'],'confirmed':True,'bought_at':self.at.isoformat(),
                'cost':'10.4','entry_source':'https://example.org/entry','entry_minutes':s['minutes'][-30:]}

    def test_t_plus_one_and_unknown_holding(self):
        p=self.e.freeze_position(self.position());days=self.e.calendar_days(self.data,self.at)
        self.assertEqual(self.e.review_exit(p,{},self.at,days)['state'],'t_plus_one')
        self.assertEqual(self.e.review_exit(None,{},self.at,days)['state'],'scenario_only')

    def test_exit_next_morning_auction_and_reference(self):
        p=self.e.freeze_position(self.position());tomorrow=self.at+dt.timedelta(days=1)
        d=sample(tomorrow.replace(hour=9,minute=35));s=d['stocks'][0];days=self.e.calendar_days(d,tomorrow)
        r=self.e.review_exit(p,s,self.e.stamp(d['as_of']),days)
        self.assertEqual(r['state'],'exit_due') # final auction 10.2 is below 10.25
        s['opening_auction']['price']='10.4';s['quote']['price']='10.2'
        self.assertEqual(self.e.review_exit(p,s,self.e.stamp(d['as_of']),days)['state'],'exit_due')

    def test_two_minutes_and_deadline_not_auto_fill(self):
        p=self.e.freeze_position(self.position());d=sample((self.at+dt.timedelta(days=1)).replace(hour=9,minute=35))
        s=d['stocks'][0];s['opening_auction']['price']='10.4'
        for row in s['minutes'][-2:]:row['close']='10.25'
        at=self.e.stamp(d['as_of']);days=self.e.calendar_days(d,at)
        self.assertEqual(self.e.review_exit(p,s,at,days)['state'],'exit_due')
        r=self.e.review_exit(p,{},at.replace(hour=10,minute=0),days)
        self.assertEqual(r['state'],'exit_due');self.assertIn('execution',r)
        r=self.e.review_exit(p,{'execution_blocked':True},at.replace(hour=10,minute=0),days)
        self.assertEqual(r['state'],'exit_blocked')

    def test_holiday_rolls_to_next_session(self):
        p=self.e.freeze_position(self.position());days=self.e.calendar_days(self.data,self.at)
        days=[d for d in days if str(d)!='2026-01-09']
        r=self.e.review_exit(p,{},self.at+dt.timedelta(days=1),days)
        self.assertEqual(r['state'],'t_plus_one')


class LateDayPublicationTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        env=patch.dict(os.environ,{'AUTOSTRATEGY_PRIVATE_ROOT':temp.name});env.start();self.addCleanup(env.stop)
        self.e=load_engine();self.data=sample();self.at=self.e.stamp(self.data['as_of'])

    def test_publish_private_evidence_and_safe_page(self):
        self.data['private_secret']='DO-NOT-PUBLISH'
        r=run_research(self.data,now=self.at,rebuild=False)
        self.assertEqual(r['state'],'qualified')
        page=render_topics(now=self.at)
        self.assertIn('topic-late-day',page);self.assertNotIn('DO-NOT-PUBLISH',page)
        page=render_topics(now=self.at+dt.timedelta(minutes=2))
        self.assertIn('历史',page)

    def test_failure_keeps_last_success_and_older_publication_rejected(self):
        run_research(self.data,now=self.at,rebuild=False)
        first=read_json(research_path('late_day/last_success.json'))
        broken=copy.deepcopy(self.data);broken['calendar']['verified']=False
        run_research(broken,now=self.at+dt.timedelta(seconds=1),rebuild=False)
        self.assertEqual(read_json(research_path('late_day/last_success.json')),first)
        self.assertEqual(read_json(research_path('late_day/latest_attempt.json'))['state'],'failed')
        self.assertFalse(publish_report(first,rebuild=False)['changed'])

    def test_restart_immutable_reference(self):
        p={'id':'artificial','code':'600001','confirmed':True,'bought_at':self.at.isoformat(),'cost':10.4,
           'entry_source':'https://example.org/entry','entry_minutes':self.data['stocks'][0]['minutes'][-30:]}
        old=frozen_positions([p],self.e)
        self.assertEqual(frozen_positions([],self.e),old)
        p['entry_minutes'][0]['low']='9'
        with self.assertRaises(ValueError):frozen_positions([p],self.e)
        self.assertEqual(frozen_positions([],self.e),old)

    def test_seven_pages_late_day_moved_old_links_and_no_web_execution(self):
        from build_investment_site import build
        run_research(now=self.at,rebuild=False)
        out=build(self.at.date());page=(out/'latest.html').read_text()
        for name in ('hot','dragon','yichujifa','prelaunch','late-day','strategy','reports'):
            self.assertIn('id="page-'+name+'"',page)
        self.assertIn('id="strategy-late-day"',page);self.assertIn('id="topic-late-day"',page)
        late=page.split('<section id="page-late-day"',1)[1].split('<section id="page-strategy"',1)[0]
        reports=page.split('<section id="page-reports"',1)[1]
        self.assertIn('id="topic-late-day"',late)
        self.assertNotIn('id="topic-late-day"',reports)
        self.assertNotIn('/Users/',page);self.assertNotIn('frozen_positions.json',page)

    def test_placeholder_does_not_block_actual_earlier_market_cutoff(self):
        run_research(now=self.at+dt.timedelta(hours=6),rebuild=False)
        r=run_research(self.data,phase='review',now=self.at+dt.timedelta(hours=7),rebuild=False)
        saved=read_json(research_path('late_day/latest_attempt.json'))
        self.assertEqual(saved['as_of'],self.data['as_of'])
        self.assertEqual(saved['historical_pass_count'],1)

    def test_malformed_special_topic_is_isolated(self):
        from research_store import atomic_json
        r=run_research(self.data,now=self.at,rebuild=False)
        topic=topic_payload(r);topic['late_day_result']['rows'][0]['checks']=[{'passed':True}]
        atomic_json(research_path('topics/late-day/current.json'),topic)
        self.assertIn('独立专题资料不可用',render_topics(now=self.at))

    def test_future_entry_rejected_without_freezing(self):
        p={'id':'future','code':'600001','confirmed':True,'bought_at':self.at.isoformat(),'cost':10.4,
           'entry_source':'https://example.org/entry','entry_minutes':self.data['stocks'][0]['minutes'][-30:]}
        with self.assertRaises(ValueError):frozen_positions([p],self.e,as_of=self.at-dt.timedelta(minutes=1))
        self.assertFalse(research_path('late_day/frozen_positions.json').exists())

    def test_public_collection_keeps_unverified_semantics_pending(self):
        from late_day_feeds import collect, parse_quotes
        now=dt.datetime.now(TZ)
        f=['0']*90
        for i,value in {1:'Artificial feed fixture',2:'600001',3:'10.4',4:'10',6:'211000',
                        30:now.strftime('%Y%m%d%H%M%S'),33:'10.5',37:'21732',38:'10',45:'104',47:'11',49:'3'}.items():f[i]=value
        raw='v_sh600001="'+'~'.join(f)+'";'
        quotes=parse_quotes(raw,'https://example.org/artificial')
        self.assertEqual(quotes['600001']['volume_shares'],21100000)
        self.assertFalse(quotes['600001']['ratio_verified'])
        class Feed:
            def read(self,url,encoding='utf-8'):
                body=raw if 'qt.gtimg' in url else json.dumps({'data':{'sh600001':{'day':[]}}})
                return body,{'url':url,'retrieved_at':now.isoformat(),'sha256':'artificial'}
        with patch('late_day_feeds.calendar_payload',return_value=sample(now)['calendar']):
            data=collect(now,codes=['600001'],feed=Feed())
        self.assertEqual(len(data['stocks']),1)
        self.assertFalse(data['stocks'][0]['history_meta']['verified'])
        self.assertFalse(data['coverage']['verified'])
        r=self.e.evaluate(data,now=dt.datetime.now(TZ))
        self.assertEqual(r['qualified_count'],0);self.assertTrue(r['missing'])

    def test_collection_failure_not_successful_empty_pool(self):
        from late_day_feeds import collect
        now=dt.datetime.now(TZ)
        class BrokenFeed:
            def read(self,*args):raise OSError('artificial offline fault')
        with patch('late_day_feeds.calendar_payload',return_value=sample(now)['calendar']):
            data=collect(now,codes=['600001'],feed=BrokenFeed())
        result=self.e.evaluate(data,now=dt.datetime.now(TZ))
        self.assertEqual(result['qualified_count'],0)
        self.assertTrue(result['missing']);self.assertFalse(data['coverage']['verified'])

    def test_collection_all_mode_does_not_stop_at_thirty_or_one_quote_batch(self):
        from late_day_feeds import collect
        now=dt.datetime.now(TZ);codes=[str(600000+i) for i in range(1,66)]
        batch_sizes=[]
        class Feed:
            def read(self,url,encoding='utf-8'):
                if 'qt.gtimg.cn/q=' not in url:return '{}',{'url':url}
                requested=[s[-6:] for s in url.split('q=',1)[1].split(',')]
                batch_sizes.append(len(requested));lines=[]
                for code in requested:
                    f=['0']*90
                    for i,value in {1:'Artificial fixture',2:code,3:'10.4',4:'10',6:'211000',
                                    30:now.strftime('%Y%m%d%H%M%S'),33:'10.5',37:'21732',38:'10',45:'104',47:'11',49:'3'}.items():f[i]=value
                    lines.append('v_sh'+code+'="'+'~'.join(f)+'";')
                return ''.join(lines),{'url':url}
        with patch('late_day_feeds.calendar_payload',return_value=sample(now)['calendar']):
            data=collect(now,codes=codes,feed=Feed())
        self.assertEqual(len(data['stocks']),65)
        self.assertFalse(data['coverage']['bounded']);self.assertTrue(all(n<=60 for n in batch_sizes))

    def test_actual_parallel_publications_keep_latest_cutoff(self):
        from concurrent.futures import ThreadPoolExecutor
        report=self.e.evaluate(self.data,now=self.at);report['rule_hash']=source_hash()
        newer=copy.deepcopy(report)
        for key in ('as_of','generated_at','valid_until'):
            newer[key]=(self.e.stamp(newer[key])+dt.timedelta(seconds=30)).isoformat()
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda r:publish_report(r,rebuild=False),[newer,report]))
        self.assertEqual(read_json(research_path('late_day/latest_attempt.json'))['as_of'],newer['as_of'])
        self.assertEqual(read_json(research_path('topics/late-day/current.json'))['as_of'],newer['as_of'])


class LateDayNumericReviewTests(unittest.TestCase):
    def fixture(self):
        at=dt.datetime(2026,1,8,14,30,tzinfo=TZ)
        previous=['2025-12-31','2026-01-02','2026-01-05','2026-01-06','2026-01-07']
        source={'label':'Artificial data','url':'https://example.org/fixture','time':at.isoformat()}
        stock={'code':'600001','name':'Artificial numeric audit','minute_date':'2026-01-08',
            'quote':{'time':at.isoformat(),'price':'10.4','reference_close':'10','volume_shares':'30000000',
                     'provider_float_a_shares':'300000000','provider_total_shares':'1000000000',
                     'provider_turnover_pct':'10','provider_cap_yi':'104'},
            'history':[{'date':day,'volume_shares':'8000000'} for day in previous],
            'minutes':[{'time':(at+dt.timedelta(minutes=i)).isoformat(),'price':'10.4',
                        'volume_shares':str(25000000+i*100000),'amount_cny':str(260000000+i*1040000)} for i in range(27)],
            'sources':[source]}
        return stock,at,previous

    def test_numeric_match_is_only_pending_never_qualified(self):
        from late_day_review import review_stock
        stock,at,previous=self.fixture();r=review_stock(stock,at.date(),previous)
        self.assertEqual(r['state'],'pending');self.assertEqual(r['numeric_hit_count'],27)
        self.assertNotIn('qualified_count',r)
        self.assertEqual(r['representative']['time'],'2026-01-08T14:56:00+08:00')

    def test_missing_duplicate_cross_day_and_conflict_are_unknown(self):
        from late_day_review import review_stock
        for kind in ('missing','duplicate','cross_day','history','shares','volume'):
            stock,at,previous=self.fixture()
            if kind=='missing':stock['minutes'].pop(3)
            elif kind=='duplicate':stock['minutes'].insert(3,copy.deepcopy(stock['minutes'][3]))
            elif kind=='cross_day':stock['minute_date']='2026-01-07'
            elif kind=='history':stock['history'][0]['date']='2025-12-30'
            elif kind=='shares':stock['quote']['provider_total_shares']='900000000'
            else:stock['minutes'][5]['volume_shares']='100'
            r=review_stock(stock,at.date(),previous)
            self.assertEqual(r['state'],'unavailable',kind);self.assertEqual(r['numeric_hit_count'],0)
            self.assertIsNone(r['representative'])

    def test_rounded_display_does_not_relax_intervals(self):
        from late_day_review import review_stock
        stock,at,previous=self.fixture()
        for r in stock['minutes']:r['price']='10.600000001'
        r=review_stock(stock,at.date(),previous)
        self.assertEqual(r['state'],'numeric_rejected')
        self.assertIn('pct',r['representative']['failed'])

    def test_audit_schema_blocks_private_fields_and_future_evidence(self):
        from late_day_review import review_stock,validate_review,render_review
        stock,at,previous=self.fixture();row=review_stock(stock,at.date(),previous);row.pop('frames')
        data={'window_start':at.isoformat(),'window_end':(at+dt.timedelta(minutes=26)).isoformat(),
              'validation_day':'2026-01-09','quote_count':1,'universe_count':1,'rows':[row],
              'sources':[],'notes':['Artificial review only']}
        cutoff=(at+dt.timedelta(hours=1)).isoformat()
        validate_review(data,cutoff);self.assertIn('600001',render_review(data))
        bad=copy.deepcopy(data);bad['rows'][0]['private_position']='DO-NOT-PUBLISH'
        with self.assertRaises(ValueError):validate_review(bad,cutoff)
        bad=copy.deepcopy(data);bad['rows'][0]['representative']['time']='2026-01-09T14:30:00+08:00'
        with self.assertRaises(ValueError):validate_review(bad,cutoff)


if __name__=='__main__':unittest.main()
