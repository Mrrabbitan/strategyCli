"""Fictional radar snapshots; no market requests, private account or live signals."""
import copy
import datetime as dt
import os
import tempfile
import unittest
from unittest.mock import patch
import sector_radar_research as sr
import research_modules as rm

NOW=dt.datetime(2026,9,18,16,tzinfo=sr.TZ)
DAYS=[(dt.date(2026,7,1)+dt.timedelta(days=i)).isoformat() for i in range(80)
      if (dt.date(2026,7,1)+dt.timedelta(days=i)).weekday()<5 and (dt.date(2026,7,1)+dt.timedelta(days=i))<=NOW.date()]
SIGNAL=DAYS[-1]
CAL={'verified':True,'days':DAYS+['2026-09-21']}
SRC=[{'label':'fictional source','url':'https://example.com/evidence'}]
CAT=[{'id':'sector-a','label':'虚构板块甲'},{'id':'sector-b','label':'虚构板块乙'}]

def fixture(n=4):
    stocks=[];matrix={}
    for i in range(n):
        code=f'{600001+i:06d}';bars=[]
        for j,day in enumerate(DAYS):
            p=10+(j*.01)*(i+1)
            bars.append(dict(date=day,open=p-.01,close=p,high=p+.1,low=p-.1,volume_shares=100000,amount_cny=1000000+i*100000))
        daily=dict(verified=True,as_of=SIGNAL,adjustment='qfq',bars=bars,sources=SRC)
        security=dict(verified=True,date=SIGNAL,mainboard=True,st=False,suspended=False,delisting=False,normal_limit=True,security_type='a_share',source='https://example.com/status')
        stocks.append(dict(code=code,name='虚构股'+str(i),security=security,daily=daily,announcement={'verified':True,'as_of':NOW.isoformat(),'hard_risk':False,'sources':SRC}))
        matrix[code]={k:dict(status='passed' if k=='three-step' else 'failed',observation_passed=k=='three-step',rule_version='fixture',rule_hash='fixture-hash',as_of=SIGNAL,sources=SRC,reasons=[]) for k in sr.SKILLS}
    benchmark=copy.deepcopy(stocks[0]['daily'])
    for b in benchmark['bars']:b.update(open=10,high=10.1,low=9.9,close=10)
    members=[{'code':s['code'],'name':s['name']} for s in stocks]
    sectors=[dict(d,verified=True,expected_count=n,members=copy.deepcopy(members),membership_as_of=SIGNAL,sources=SRC,provider='fictional',provider_code=d['id']) for d in CAT]
    return {'signal_date':SIGNAL,'calendar':CAL,'stocks':stocks,'sectors':sectors,'benchmark':benchmark}, {'by_code':matrix,'executions':[],'missing':[]}


def evaluate(data,checks):
    with patch.object(sr,'sessions',return_value=(SIGNAL,'2026-09-21',CAL)):
        return sr.evaluate(data,checks,NOW,catalog=CAT)


class RadarRankingTests(unittest.TestCase):
    def test_all_members_dedup_independent_top_and_overlap(self):
        data,checks=fixture();r=evaluate(data,checks)
        self.assertEqual(len(r['candidates']),4)
        self.assertTrue(all(len(s['top_codes'])==3 for s in r['sectors']))
        self.assertEqual(r['coverage']['unique_stocks'],4)
        self.assertEqual(r['sectors'][0]['overlap'][0]['count'],4)
        self.assertTrue(all(not c['eligible'] for c in r['candidates']))
        self.assertEqual(len(r['forward_top']),2)

    def test_no_fill_and_native_failure_not_overwritten(self):
        data,checks=fixture()
        for code,cells in list(checks['by_code'].items())[1:]:
            cells['three-step'].update(status='failed',observation_passed=False)
        r=evaluate(data,checks)
        self.assertEqual(len(r['sectors'][0]['top_codes']),1)
        self.assertEqual(r['candidates'][0]['strategy_checks']['dragon']['status'],'failed')

    def test_missing_membership_or_unknown_security_blocks_full_board_ranks(self):
        for case in ('members','state','dates'):
            data,checks=fixture()
            if case=='members':data['sectors'][0]['expected_count']=8
            elif case=='state':data['stocks'][0]['security']['verified']=False
            else:data['sectors'][0]['membership_as_of']='2026-09-21'
            r=evaluate(data,checks)
            self.assertEqual(r['sectors'][0]['top_codes'],[])
            self.assertEqual(r['forward_top'],[])
            self.assertEqual(r['status'],'partial')

    def test_no_strategy_votes_or_shared_scores(self):
        data,checks=fixture();a=evaluate(data,checks)
        for cell in checks['by_code']['600001'].values():
            cell.update(status='passed',observation_passed=True,score=999999)
        b=evaluate(data,checks)
        self.assertEqual(a['sectors'][0]['top_codes'],b['sectors'][0]['top_codes'])

    def test_announcements_and_risk_never_upgraded(self):
        data,checks=fixture();data['stocks'][0]['announcement']['hard_risk']='人工硬风险'
        data['stocks'][1]['announcement']['as_of']='2026-09-21T16:00:00+08:00'
        r=evaluate(data,checks)
        self.assertNotIn('600001',r['sectors'][0]['top_codes'])
        self.assertNotIn('600002',r['sectors'][0]['top_codes'])
        self.assertEqual(r['candidates'][0]['state'],'excluded')

    def test_history_conflict_missing_and_future_truncation(self):
        data,checks=fixture();original=evaluate(data,checks)
        data['stocks'][0]['daily']['bars'].append(dict(data['stocks'][0]['daily']['bars'][-1],date='2026-09-21',close=999))
        self.assertEqual(original['sectors'][0]['top_codes'],evaluate(data,checks)['sectors'][0]['top_codes'])
        data['stocks'][0]['daily']['bars'].pop(-2)
        self.assertEqual(evaluate(data,checks)['sectors'][0]['top_codes'],[])
        data,checks=fixture();data['stocks'][0]['daily']['bars'].append(data['stocks'][0]['daily']['bars'][-1])
        self.assertEqual(evaluate(data,checks)['forward_top'],[])

    def test_percentiles_ties_singletons_and_drawdown(self):
        self.assertEqual(sr.percentile([1,1,3]),{1:25,3:100})
        self.assertEqual(sr.percentile([4]),{4:50})
        d,c=fixture();b=d['stocks'][0]['daily']['bars']
        for row,p in zip(b[-6:],[10,12,9,10,11,10]):row['close']=p
        self.assertAlmostEqual(sr.metrics(b)['drawdown_5d'],.25)

    def test_security_prefix_alone_never_admits_and_nonmainboard_excludes(self):
        self.assertEqual(sr.security_state({'code':'600001','name':'虚构'},SIGNAL)[0],'pending')
        for code in ('688001','300001','920001','200001','900001'):
            self.assertEqual(sr.security_state({'code':code,'name':'虚构'},SIGNAL)[0],'excluded')
        self.assertEqual(sr.security_state({'code':'600001','name':'*ST虚构'},SIGNAL)[0],'excluded')

    def test_zero_not_missing_success_and_late_report_locks(self):
        data,checks=fixture();report=evaluate(data,checks)
        with tempfile.TemporaryDirectory() as path,patch.dict(os.environ,{'AUTOSTRATEGY_PRIVATE_ROOT':path}):
            rm.publish('sector-radar',report,attempted_at=NOW)
            success=rm.read_json(rm.module_path('sector-radar').parent/'last_success.json',{})
            self.assertTrue(success)
            rm.record_failure('sector-radar','虚构故障',now=NOW+dt.timedelta(seconds=1))
            self.assertEqual(rm.load_module('sector-radar',NOW+dt.timedelta(seconds=2))['state'],'unavailable')
            self.assertEqual(rm.read_json(rm.module_path('sector-radar').parent/'last_success.json',{}),success)

    def test_default_tasks_do_not_expand(self):
        from refresh_research import DEFAULT_MODULES
        self.assertEqual(DEFAULT_MODULES,('hot','dragon','yichujifa','prelaunch'))

    def test_pass_requires_current_provenance_and_real_boolean_security(self):
        for field,value in [('as_of','2026-09-21T15:00:00+08:00'),('rule_hash',None),('sources',[])]:
            data,checks=fixture()
            checks['by_code']['600001']['three-step'][field]=value
            result=evaluate(data,checks)
            self.assertNotIn('600001',result['sectors'][0]['top_codes'])
            self.assertEqual(result['candidates'][0]['strategy_checks']['three-step']['status'],'pending')
        data,checks=fixture();data['stocks'][0]['security']['suspended']=0
        self.assertEqual(evaluate(data,checks)['sectors'][0]['top_codes'],[])

    def test_benchmark_does_not_require_invented_amount(self):
        data,checks=fixture()
        for b in data['benchmark']['bars']:b.update(amount_cny=None,volume_shares=None)
        self.assertTrue(evaluate(data,checks)['coverage']['forward_ready'])
        data['stocks'][0]['daily']['bars'][-1]['amount_cny']=None
        self.assertFalse(evaluate(data,checks)['coverage']['forward_ready'])

    def test_strategy_union_known_does_not_require_all_five_known(self):
        data,checks=fixture()
        for cells in checks['by_code'].values():cells['late-day'].update(status='pending',observation_passed=False)
        r=evaluate(data,checks)
        self.assertFalse(r['coverage']['matrix_complete'])
        self.assertTrue(r['coverage']['forward_ready'])
        self.assertEqual(r['sectors'][0]['metrics']['skill_pass_fraction'],1)
        checks['by_code']['600001']['three-step'].update(status='failed',observation_passed=False)
        self.assertFalse(evaluate(data,checks)['coverage']['forward_ready'])

    def test_announcements_do_not_silently_change_strategy_pass_fraction(self):
        data,checks=fixture();data['stocks'][0]['announcement']['verified']=False
        r=evaluate(data,checks)
        self.assertFalse(r['coverage']['announcement_complete'])
        self.assertEqual(r['sectors'][0]['metrics']['skill_pass_fraction'],1)
        self.assertNotIn('600001',r['sectors'][0]['top_codes'])
        self.assertEqual(r['status'],'partial')

    def test_relative_returns_breadth_and_nonoverlap_amount_window(self):
        data,checks=fixture();stock=data['stocks'][0];bars=stock['daily']['bars'][-26:]
        for i,b in enumerate(bars):b['amount_cny']=100 if i<23 else 200
        result=sr.sector_metrics({'600001':bars},data['benchmark']['bars'],['600001'])
        expected=bars[-1]['close']/bars[-4]['close']-bars[-4]['close']/bars[-7]['close']
        self.assertAlmostEqual(result['relative_improvement'],expected)
        self.assertEqual(result['amount_ratio'],2)
        self.assertEqual(result['breadth_ma20'],1)
        self.assertEqual(result['skill_pass_fraction'],1)

    def test_conflicting_or_future_source_is_not_comparable(self):
        for extra in ({'conflict':True},{'complete':False},{'adjustment_as_of':'2026-09-21'},
                      {'source_time':'2026-09-21T15:00:00+08:00'}):
            data,checks=fixture();data['stocks'][0]['daily'].update(extra)
            self.assertEqual(evaluate(data,checks)['forward_top'],[])

    def test_registered_provider_mapping_cannot_be_substituted(self):
        data,checks=fixture();definitions=copy.deepcopy(CAT)
        definitions[0].update(ths_code='fixture-a',em_code=None)
        data['sectors'][0].update(provider='eastmoney',provider_code='unrelated-fund',membership_as_of=None)
        with patch.object(sr,'sessions',return_value=(SIGNAL,'2026-09-21',CAL)):
            result=sr.evaluate(data,checks,NOW,catalog=definitions)
        self.assertEqual(result['sectors'][0]['top_codes'],[])
        self.assertEqual(result['forward_top'],[])

    def test_unknown_security_does_not_hide_pending_matrix(self):
        data,checks=fixture()
        for stock in data['stocks']:
            stock['security']['verified']=False
            stock['announcement']['verified']=False
        for cells in checks['by_code'].values():
            for cell in cells.values():cell.update(status='pending',observation_passed=False)
        result=evaluate(data,checks)
        for field in ('matrix_complete','observation_complete','announcement_complete'):
            self.assertFalse(result['coverage'][field])

    def test_retrieval_clocks_are_not_new_radar_evidence(self):
        data,checks=fixture();a=evaluate(data,checks);b=copy.deepcopy(a)
        a['executions']=[{'started_at':'one','finished_at':'two','input_fingerprint':'one'}]
        b['executions']=[{'started_at':'three','finished_at':'four','input_fingerprint':'two'}]
        b['changes']=['empty bookkeeping'];b['input_fingerprint']='new-acquisition-clock'
        self.assertEqual(rm.content_hash(a),rm.content_hash(b))
        b['candidates'][0]['strategy_checks']['dragon']['as_of']='2026-09-21'
        self.assertNotEqual(rm.content_hash(a),rm.content_hash(b))

if __name__=='__main__':unittest.main()
