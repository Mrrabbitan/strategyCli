"""Fictional native evidence, isolated private storage, no market calls or accounts."""
import copy
import datetime as dt
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sector_radar_skills as s
from research_store import research_path, read_json, atomic_json

ROOT = Path(__file__).resolve().parents[1]
SOURCE = [{'url': 'https://example.invalid/fictional', 'label': '人工样例'}]
DAY = '2026-09-11'
AT = dt.datetime(2026, 9, 11, 16, tzinfo=s.TZ)


def fixtures(name):
    spec = importlib.util.spec_from_file_location('radar_fixture_' + name, ROOT/'tests'/('test_'+name+'.py'))
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value


def calendar():
    import prelaunch_research as p
    return {'verified': True, 'source': SOURCE[0]['url'],
            'days': p._sessions(AT.date(), 240, -1) + [DAY] + p._sessions(AT.date(), 10)}


class SectorRadarSkillsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'AUTOSTRATEGY_PRIVATE_ROOT': self.temp.name})
        self.env.start()
        self.folder = Path(self.temp.name)/'native'
        self.stock = {'code':'600000', 'name':'虚构样例', 'daily':{}}

    def tearDown(self):
        self.env.stop(); self.temp.cleanup()

    def test_prelaunch_actual_kernel_all_241_no_sampling_and_original_caps(self):
        fixture = fixtures('prelaunch_research')
        data = fixture.fixture(241)
        report = s._run_prelaunch(AT, DAY, data['stocks'], calendar(),
             {'prelaunch': data, 'prelaunch_enrichment': fixture.enrichment(data)}, False, self.folder)
        self.assertEqual(report['coverage']['processed'], 241)
        self.assertEqual(len(report['core']), 3)  # original one-industry / catalyst cap
        mapped = s._map_prelaunch(report, [x['code'] for x in data['stocks']])
        self.assertEqual(sum(x['observation_passed'] for x in mapped.values()), 3)
        self.assertFalse(research_path('prelaunch/current.json').exists())
        self.assertTrue(research_path('sector_radar/native/prelaunch_frozen.json').exists())

    def test_prelaunch_unknown_cost_never_core_and_frozen_support_retained(self):
        fixture = fixtures('prelaunch_research')
        data = fixture.fixture(); review = fixture.enrichment(data)
        first = s._run_prelaunch(AT, DAY, data['stocks'], calendar(),
             {'prelaunch': data, 'prelaunch_enrichment': review}, False, self.folder)
        support = first['frozen_records'][0]['support']
        del review['stocks']['600000']['costs']
        again = s._run_prelaunch(AT, DAY, data['stocks'], calendar(),
             {'prelaunch': data, 'prelaunch_enrichment': review}, False, self.folder)
        self.assertEqual(again['core'], [])
        self.assertEqual(again['frozen_records'][0]['support'], support)
        self.assertEqual(s._map_prelaunch(again,['600000'])['600000']['status'], 'pending')

    def test_prelaunch_smaller_import_cannot_silently_drop_rest(self):
        with self.assertRaisesRegex(ValueError,'every radar stock'):
            s._run_prelaunch(AT, DAY, [self.stock], calendar(),
                            {'prelaunch':{'signal_date': DAY, 'stocks': []}}, False, self.folder)

    def test_dragon_import_rebuilds_original_top_three_and_volume_exit(self):
        signal = '2026-09-07'; at = dt.datetime(2026,9,7,16,tzinfo=s.TZ)
        pool = [{'c': f'60000{i}', 'n': f'人工示例{i}', 'hybk': '示例行业', 'lbc': 2,
                 'p':12100,'amount':100000000+i,'hs':8,'fund':10000000,'zbc':1,
                 'zttj':{'ct':2},'fbt':93500,'lbt':104500} for i in range(1,5)]
        history = [['2026-09-02','10','10','10','10','1'],
                   ['2026-09-03','10','10','10','10','1'],
                   ['2026-09-04','11','11','11','11','1.2'],
                   ['2026-09-07','12.1','12.1','12.1','12.1','1.44']]
        raw = {'pool_payload': {'data': {'qdate':20260907,'tc':4,'pool':pool}},
               'quotes':{r['c']:{'price':12.1,'timestamp':'20260907150000'} for r in pool},
               'histories':{r['c']:copy.deepcopy(history) for r in pool}, 'sources':SOURCE}
        result = s._run_dragon(at, signal, [], {}, {'dragon_hot':raw}, False, self.folder)
        matrix = s._map_dragon(result, [r['c'] for r in pool])
        self.assertEqual(len(result['candidates']), 3)
        self.assertTrue(all(not cell['observation_passed'] for cell in matrix.values()))
        self.assertTrue(all(row['expansion_count']==2 for row in result['candidates']))
        self.assertEqual(sum(cell.get('original_rank') is not None for cell in matrix.values()),3)
        self.assertFalse(research_path('dragon/current.json').exists())

    def test_dragon_precomputed_pass_report_is_not_raw_input(self):
        with self.assertRaisesRegex(ValueError,'pool_payload'):
            s._run_dragon(AT, DAY, [], calendar(),
                          {'dragon_hot': {'cutoff': DAY, 'status':'complete','candidates':[]}}, False, self.folder)

    def test_dragon_during_market_prepares_last_close_without_intraday_promotion(self):
        at = dt.datetime(2026,9,14,14,tzinfo=s.TZ)
        fake = {'cutoff':DAY,'as_of':'2026-09-14T09:25:00+08:00',
                'ranking_as_of':DAY+'T15:00:00+08:00','status':'unavailable',
                'auction_as_of':'2026-09-14T09:25:00+08:00',
                'market_evidence':{'source_asof':at.isoformat(),'verified':True},
                'sectors':[{'items':[{'code':'600001','current_quote':{'timestamp':'20260914140000'},
                    'auction':{'qualified':True},'refill_evidence':{'verified':True},
                    'risk_evidence':[{'kind':'today_volume'}]}]}],
                'missing':['人工源故障'],'sources':SOURCE}
        with patch('refresh_hot_sector_board.research_hot',return_value=fake) as fetch, \
             patch('dragon_research.research',side_effect=lambda at,phase,hot,previous: dict(hot)) as native:
            result=s._run_dragon(at,DAY,[],calendar(),{},True,self.folder)
        self.assertEqual(fetch.call_args.args[1],'prepare')
        self.assertEqual(fetch.call_args.kwargs,{'persist':False})
        self.assertEqual(native.call_args.args[1],'prepare')
        self.assertEqual(result['as_of'][:10],DAY)
        self.assertEqual(native.call_args.args[0],at)
        filtered=native.call_args.kwargs['hot']
        self.assertNotIn('market_evidence',filtered)
        self.assertNotIn('auction_as_of',filtered)
        for key in ('current_quote','auction','refill_evidence','risk_evidence'):
            self.assertNotIn(key,filtered['sectors'][0]['items'][0])
        self.assertIn('auction',read_json(self.folder/'hot_source.json')['sectors'][0]['items'][0])
        self.assertIn('original_hot_fingerprint',result)
        fake['sectors'][0]['items'].append({'code':'600002','risk_evidence':[
            {'source_asof':DAY+'T14:00:00+08:00','kind':'acceptance_failed','verified':True},
            {'source_asof':at.isoformat(),'kind':'market_retreat','verified':True}]})
        with patch('refresh_hot_sector_board.research_hot',return_value=fake), \
             patch('dragon_research.research',side_effect=lambda at,phase,hot,previous: dict(hot)):
            retained=s._run_dragon(at,DAY,[],calendar(),{},True,self.folder)
        self.assertEqual(len(retained['sectors'][0]['items'][1]['risk_evidence']),1)
        self.assertEqual(retained['sectors'][0]['items'][1]['risk_evidence'][0]['kind'],'acceptance_failed')
        wrong=dict(fake,cutoff='2026-09-14')
        with patch('refresh_hot_sector_board.research_hot',return_value=wrong):
            with self.assertRaisesRegex(ValueError,'原榜不是本次信号日'):
                s._run_dragon(at,DAY,[],calendar(),{},True,self.folder)

    def test_yichujifa_timeout_retains_process_evidence_separately(self):
        error=s.subprocess.TimeoutExpired(['fictional-native-scan'],240,
                                        output=b'collected fictional rows',stderr=b'network waiting')
        with patch('sector_radar_skills.subprocess.run',side_effect=error):
            with self.assertRaises(s.subprocess.TimeoutExpired):
                s._run_yichujifa(AT,DAY,[],calendar(),{},True,self.folder)
        process=read_json(self.folder/'process-error.json')
        self.assertTrue(process['timed_out'])
        self.assertEqual(process['stderr'],'network waiting')
        atomic_json(self.folder/'error.json',{'type':'TimeoutExpired'})
        self.assertEqual(read_json(self.folder/'process-error.json'),process)

    def test_yichujifa_nonzero_exit_keeps_stderr_and_stdout(self):
        from types import SimpleNamespace
        result=SimpleNamespace(returncode=2,stdout='fake progress',stderr='fake parse failure')
        with patch('sector_radar_skills.subprocess.run',return_value=result):
            with self.assertRaisesRegex(ValueError,'原生scan失败'):
                s._run_yichujifa(AT,DAY,[],calendar(),{},True,self.folder)
        process=read_json(self.folder/'process-error.json')
        self.assertEqual(process['returncode'],2)
        self.assertEqual(process['stderr'],'fake parse failure')
        self.assertEqual(process['stdout'],'fake progress')

    def test_three_step_uses_canonical_full_window_not_short_radar_calendar(self):
        native = fixtures('three_step')
        data = native.fixture(); data['universe']['scope'] = 'all_mainboard'
        fake_engine = type('Engine', (), {'evaluate': staticmethod(lambda evidence, as_of:
                       native.engine.evaluate(evidence, as_of=as_of, calculator=native.constant('.81')))})
        with patch('three_step_research.calendar_payload', return_value=(native.SIGNAL,data['calendar'])), \
             patch('three_step_research.load_engine', return_value=fake_engine):
            report = s._run_three_step(native.NOW, native.SIGNAL, [], {'days':[]},
                                      {'three-step':data}, False, self.folder)
        self.assertTrue(report['coverage']['global_rank_verified'])
        self.assertTrue(s._map_three_step(report,['600000'])['600000']['observation_passed'])

    def test_three_step_subset_and_unknown_rank_cannot_pass(self):
        native = fixtures('three_step'); data = native.fixture()
        with patch('three_step_research.calendar_payload', return_value=(native.SIGNAL,data['calendar'])):
            with self.assertRaisesRegex(ValueError,'全主板'):
                s._run_three_step(native.NOW, native.SIGNAL, [], data['calendar'],
                                  {'three-step':data}, False, self.folder)
        data['universe']['verified'] = False
        report = native.run(data)
        self.assertFalse(s._map_three_step(report,['600000'])['600000']['observation_passed'])

    def test_yichujifa_real_scan_offline_no_latest_and_no_network(self):
        cal = calendar(); sessions = [d for d in cal['days'] if d <= DAY][-70:]
        data = {'schema_version':1, 'as_of':DAY,'sessions':sessions,'stocks':[],
                'pools':{d:{'rows':[],'metadata_verified':True,'metadata_date':d.replace('-',''),
                            'response_complete':True} for d in sessions[-6:]},
                'coverage':{'requested_stocks':0,'dual_history_verified':0,
                            'pool_days':{d:{'metadata_verified':True} for d in sessions[-6:]}},
                'generated_at':AT.isoformat(),'information_cutoff':AT.isoformat(), 'sources': SOURCE}
        with patch('sector_radar_skills.subprocess.run', wraps=s.subprocess.run) as call:
            report = s._run_yichujifa(AT, DAY, [], cal, {'yichujifa':data}, False, self.folder)
        self.assertIn('--no-latest',call.call_args.args[0])
        self.assertIn('--input',call.call_args.args[0])
        self.assertEqual(report['as_of'][:10], DAY)
        self.assertFalse(report['candidates'])
        self.assertTrue((self.folder/'evidence.json').exists())

    def test_yichujifa_cross_branches_not_all_required_and_fourth_not_promoted(self):
        fixture = fixtures('yichujifa_research')
        import yichujifa_research as native
        raw = fixture.fixture()
        raw['candidates'].append(dict(raw['candidates'][0], branch='two_to_three', rank=4,
                                      prequalified=False, status='rejected', reasons=['板块内超过前三，不补位']))
        report = native.import_native(raw, as_of=fixture.NOW)
        cell = s._map_yichujifa(report,['600001'])['600001']
        self.assertTrue(cell['observation_passed'])
        self.assertEqual(len(cell['branches']), 2)
        self.assertFalse(cell['branches'][1]['observation_passed'])
        self.assertFalse(cell['actionable'])
        report['candidates'] = []
        report['missing'] = ['历史涨停池不完整']
        self.assertEqual(s._map_yichujifa(report,['600001'])['600001']['status'],'pending')

    def test_late_day_only_saved_intraday_raw_recomputed_without_positions(self):
        fixture = fixtures('late_day'); at = dt.datetime(2026,1,8,14,30,tzinfo=s.TZ)
        data = fixture.sample(at)
        data['positions'] = [{'confirmed':True,'id':'must-not-be-used'}]
        report = s._run_late_day(at.replace(hour=16), '2026-01-08', [], data['calendar'],
                                {'late-day':data},False,self.folder)
        cell = s._map_late_day(report,['600001'])['600001']
        self.assertTrue(cell['observation_passed'])
        self.assertEqual(cell['as_of'], at.isoformat())
        self.assertEqual(report['source_reports'][0]['qualified_count'],0)
        self.assertEqual(report['source_reports'][0]['exits'],[])
        self.assertFalse(research_path('late_day/frozen_positions.json').exists())
        data['as_of'] = at.replace(hour=15).isoformat()
        with self.assertRaisesRegex(ValueError,'倒推'):
            s._run_late_day(at.replace(hour=16), '2026-01-08', [], data['calendar'],
                            {'late-day':data},False,self.folder)

    def test_no_saved_tail_evidence_is_pending_not_successful_empty(self):
        report = s._run_late_day(AT, DAY, [], calendar(),{},False,self.folder)
        cell = s._map_late_day(report,['600001'])['600001']
        self.assertEqual(cell['status'],'pending')
        self.assertFalse(cell['observation_passed'])
        self.assertTrue(report['missing'])

    def test_failure_isolated_and_stale_passing_report_downgraded(self):
        def fail(*args): raise ValueError('人工故障')
        def success(*args):
            return {'as_of':'2026-09-10T15:00:00+08:00','status':'complete','missing':[],
                    'coverage':{'global_rank_verified':True}, 'sources':SOURCE,
                    'rows':[{'code':'600000','research_passed':True,'steps':[{'state':'pass'}]*3}]}
        runners = dict.fromkeys(s.MODULES, fail); runners['three-step']=success
        with patch.dict(s.RUNNERS, runners, clear=True):
            result=s.run_checks(AT,DAY,[self.stock],calendar(),refresh_native=False)
        self.assertEqual(len(result['executions']),5)
        self.assertEqual(result['by_code']['600000']['three-step']['status'],'pending')
        self.assertFalse(result['by_code']['600000']['three-step']['observation_passed'])
        self.assertTrue(all('strategy' in r for r in result['executions']))
        self.assertTrue(any(r['status']=='complete' for r in result['executions']))

    def test_scope_exclusions_and_disabled_network_are_explicit(self):
        stocks=[self.stock,{'code':'300001','name':'虚构创业板'},{'code':'600001','name':'ST虚构'}]
        result=s.run_checks(AT,DAY,stocks,calendar(),refresh_native=False)
        self.assertTrue(all(c['status']=='failed' for c in result['by_code']['300001'].values()))
        self.assertTrue(all(c['status']=='failed' for c in result['by_code']['600001'].values()))
        self.assertEqual(result['by_code']['600000']['dragon']['status'],'not_run')
        self.assertEqual(result['by_code']['600000']['yichujifa']['status'],'not_run')
        self.assertEqual(result['by_code']['600000']['three-step']['status'],'not_run')

    def test_conflicting_frozen_reference_cannot_be_moved(self):
        import prelaunch_research as native
        base={'rules':{'source_hash':native.SOURCE_HASH},'frozen_records':[
              {'id':'fictional','code':'600000','support':10,'upper':12,'frozen_at':DAY,'state':'active'}]}
        atomic_json(research_path('prelaunch/current.json'),base)
        changed=copy.deepcopy(base);changed['frozen_records'][0]['support']=9
        atomic_json(research_path('sector_radar/native/prelaunch_frozen.json'),changed)
        with self.assertRaisesRegex(ValueError,'immutable'):
            s._previous_prelaunch(DAY)


if __name__=='__main__': unittest.main()
