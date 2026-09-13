"""Artificial observations only; no personal watchlist is read by these tests."""
import copy
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from dragon_watchlist import render_dragon_watchlist_section


def fixture():
    stock={'code':'600001','name':'虚构观察甲','actionable':False,'historical_screen_pass':True,
           'friday_volume_ratio':1.05,'expansion_count':0,'monday_volume_trigger_shares':12000,
           'boards':2,'close':12.1,'break_count':1,'last_reseal':'14:20','turnover_pct':5,
           'monday_trigger_result':'仅作量能阈值观察','rationale':'虚构证据','risk_note':'竞价回封待验证',
           'history':[],'sources':[{'label':'人工来源','url':'https://example.com/evidence'}],
           'monday_auction':None,'monday_reseal':None}
    return {'analysis_date':'2026-08-30','target_date':'2026-08-31','quote_date':'2026-08-28',
            'actionable':False,'selected':[stock],'excluded':[],'alternates':[],
            'market':{'summary':'人工测试市场','sealed_mainboard':10,'failed_mainboard':2,'failed_ratio':1/6,
                      'breadth':'虚构广度','promotion':'晋级未知'},
            'coverage':{'source_limit_pool':12,'mainboard_consecutive_candidates':3,'historical_screen_pass':1},
            'snapshot_kind':'人工测试，非真实证券推荐','checks':[],
            'data_quality':{'historical_selection':'人工数据','limitations':[]},'sources':[]}


class DragonWatchlistTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'watchlist.json';self.payload=fixture()
    def render(self,day=dt.date(2026,8,31)):
        self.path.write_text(json.dumps(self.payload,ensure_ascii=False))
        return render_dragon_watchlist_section(self.path,as_of=day)
    def test_old_snapshot_fails_new_rank_gate(self):
        self.assertIn('未通过新版热点排名核验',self.render(dt.date(2026,9,14)))
    def test_missing_corrupt_and_future_snapshot(self):
        self.assertIn('缺失',render_dragon_watchlist_section(self.path))
        self.path.write_text('bad');self.assertIn('数据不可用',render_dragon_watchlist_section(self.path))
        self.assertEqual(self.render(dt.date(2026,8,29)),'')
    def test_disqualified_snapshot_never_grants_permission(self):
        for field,value in [('friday_volume_ratio',.9),('expansion_count',2),('actionable',True)]:
            self.payload=fixture();self.payload['selected'][0][field]=value
            self.assertIn('数据不可用',self.render())
    def test_fixture_is_not_executable(self):
        self.payload['selected'][0]['name']='<script>alert(1)</script>'
        self.payload['selected'][0]['sources'][0]['url']='javascript:alert(1)'
        page=self.render();self.assertNotIn('<script>alert(1)</script>',page)
        self.assertNotIn('href="javascript:',page)

if __name__=='__main__':unittest.main()
