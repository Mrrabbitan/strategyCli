"""The workbench must not turn evidence gaps into current trading permissions."""
import datetime as dt
import os
import tempfile
import unittest
from html.parser import HTMLParser
from unittest.mock import patch

from research_modules import TZ, publish
from research_store import atomic_json, research_path
from research_views import render_module, evidence_chart, links, specific_evidence, candidate_card, intraday_context, prelaunch_evidence
from investment_dashboard import render_dashboard


class ViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.env=patch.dict(os.environ,{'AUTOSTRATEGY_PRIVATE_ROOT':self.tmp.name})
        self.env.start();self.addCleanup(self.env.stop)

    def data(self, module='yichujifa'):
        return {'schema_version':1,'module_id':module,'as_of':'2026-09-11T15:00:00+08:00',
                'valid_until':'2099-01-01T15:00:00+08:00','phase':'prepare','status':'partial',
                'coverage':{'prequalified_count':0},'candidates':[
                    {'code':'600001','name':'虚构样例甲','group':'一进二','status':'资料不足','eligible':False,
                     'conditions':[{'label':'公告原文','passed':None,'detail':'未核验'}],
                     'metrics':{'原始板内名次':3},'reasons':['不补位'],'bars':[]}]}

    def test_legacy_snapshot_is_not_new_candidate(self):
        atomic_json(research_path('dragon/current.json'),{'selected':[{'name':'不应出现的旧股'}]})
        page=render_module('dragon')
        self.assertIn('尚未执行',page);self.assertNotIn('不应出现的旧股',page)

    def test_records_are_not_counted_as_eligible(self):
        publish('yichujifa',self.data())
        page=render_module('yichujifa')
        self.assertIn('1 条',page);self.assertIn('class="qualified-count">0 条',page)
        self.assertIn('待验证',page);self.assertIn('虚构样例甲',page)

    def test_missing_live_time_cannot_render_positive(self):
        d=self.data('dragon');d['candidates'][0]['eligible']=True
        # Even when bypassing the publisher by supplying a stale raw file, the
        # renderer separately rejects the incomplete current signal.
        atomic_json(research_path('dragon/current.json'),d)
        page=render_module('dragon')
        self.assertNotIn('candidate-state positive',page)
        self.assertIn('当前需重新确认',page)

    def test_expired_and_failed_results_keep_evidence_without_permission(self):
        d=self.data('prelaunch');d['candidates'][0]['eligible']=True
        d['valid_until']='2026-01-01T15:00:00+08:00'
        atomic_json(research_path('prelaunch/current.json'),d)
        page=render_module('prelaunch');self.assertIn('历史研究',page)
        self.assertNotIn('candidate-state positive',page)
        d['valid_until']='2099-01-01T15:00:00+08:00'
        atomic_json(research_path('prelaunch/current.json'),d)
        atomic_json(research_path('prelaunch/attempt.json'),{'status':'unavailable'})
        page=render_module('prelaunch');self.assertIn('最近更新失败',page)
        self.assertNotIn('candidate-state positive',page)

    def test_chart_uses_own_units_and_excludes_future_invalid_data(self):
        row={'name':'虚构图例','bars':[
            {'date':'2026-09-10','open':10,'high':12,'low':9,'close':11,'volume_shares':12000},
            {'date':'2026-09-11','open':10,'high':9,'low':8,'close':11,'volume_shares':100},
            {'date':'2026-09-15','open':99,'high':100,'low':98,'close':99,'volume_shares':90000}],
            'levels':[{'label':'冻结支撑','value':9}]}
        page=evidence_chart(row,'2026-09-11T15:00:00+08:00')
        self.assertIn('成交量：股',page);self.assertIn('12000',page)
        self.assertIn('冻结支撑',page);self.assertNotIn('2026-09-15',page)
        self.assertNotIn('2026-09-11',page)

    def test_empty_chart_does_not_draw_synthetic_prices(self):
        page=evidence_chart({'bars':[{'date':'2026-09-11','close':0}]},'2026-09-11')
        self.assertNotIn('<svg',page);self.assertIn('暂不绘制',page)

    def test_native_dragon_ledger_and_refill_source_time_are_displayed(self):
        row={'volume_ledger':{'days':[
            {'date':'2026-09-07','volume_shares':12000,'volume_ratio':1.2,'expansion_count':1,'confirmed_as_of':'2026-09-07T15:00:00+08:00'},
            {'date':'2026-09-08','volume_shares':14400,'volume_ratio':1.2,'expansion_count':2,'time_note':'完整收盘后确认'}]},
            'refill_timeline':[{'source_asof':'2026-09-09T09:51:03+08:00','state':'opened','source_url':'https://example.invalid/quote'}]}
        page=specific_evidence('dragon',row)
        self.assertIn('14400 股',page);self.assertIn('1.2×',page);self.assertIn('<td>2</td>',page)
        self.assertIn('09:51:03',page);self.assertIn('开板',page);self.assertIn('完整收盘后确认',page)

    def test_candidate_expansion_identity_survives_ranking_changes(self):
        import re
        d=self.data();row=d['candidates'][0]
        id1=re.search(r'<details id="([^"]+)"',candidate_card('yichujifa',row,d,0))[1]
        id2=re.search(r'<details id="([^"]+)"',candidate_card('yichujifa',row,d,15))[1]
        self.assertEqual(id1,id2)

    def test_prelaunch_missing_and_old_intraday_context_remain_visible(self):
        page=intraday_context({'status':'unavailable','notes':['报价超过90秒']})
        self.assertIn('盘中背景待验证',page);self.assertIn('报价超过90秒',page)
        old=intraday_context({'source_asof':'2026-01-01T10:00:00+08:00','price':10,'notes':['触及冻结支撑']})
        self.assertIn('历史盘中背景',old);self.assertIn('触及冻结支撑',old)
        self.assertNotIn('data-context-until',old)

    def test_prelaunch_scoring_and_original_frozen_structure_are_visible_without_chart(self):
        row={'scores':{'板块持续性':20,'个股相对强度':10,'量价承接':10,'结构空间':20,'催化与业务证据':0},
             'code':'600001','total_score':60,'frozen_id':'artificial','valid_until':'2026-09-14','levels':[{'label':'最近压力','value':12}]}
        data={'frozen_records':[{'id':'artificial','code':'600001','valid_until':'2026-09-14','support':8,'upper':10,'frozen_at':'2026-09-07','method':'人工冻结结构'}]}
        page=prelaunch_evidence(row,data)
        self.assertIn('五维总分 / 100',page);self.assertIn('<dd>60</dd>',page)
        self.assertIn('冻结支撑 / 元',page);self.assertIn('人工冻结结构',page);self.assertIn('2026-09-14',page)
        self.assertIn('最近压力 / 元',page)

    def test_prelaunch_unknown_scores_are_not_five_zeroes(self):
        page=prelaunch_evidence({'scores':None},{})
        self.assertIn('缺失项不记0分',page);self.assertNotIn('<dd>0</dd>',page)

    def test_prelaunch_partial_scores_and_conflicting_deadline_are_not_confirmed(self):
        from research_modules import eligible_now
        row={'code':'600001','eligible':True,'scores':{'板块持续性':20},'total_score':100,'frozen_id':'artificial','valid_until':'2099-09-18'}
        data={'frozen_records':[{'id':'artificial','code':'600001','valid_until':'2026-09-14'}]}
        page=prelaunch_evidence(row,data)
        self.assertIn('总分冲突',page);self.assertNotIn('<dd>100</dd>',page)
        self.assertIn('期限缺失/冲突',page);self.assertIn('2026-09-14',page)
        self.assertFalse(eligible_now('prelaunch',row,data))

    def test_prelaunch_wrong_stock_or_duplicate_frozen_record_cannot_be_used(self):
        from research_modules import eligible_now
        row={'code':'600001','eligible':True,'frozen_id':'artificial','valid_until':'2099-09-18'}
        wrong={'id':'artificial','code':'600002','valid_until':'2099-09-18','support':123.45}
        for records in ([wrong],[dict(wrong,code='600001')]*2):
            page=prelaunch_evidence(row,{'frozen_records':records})
            self.assertNotIn('123.45',page);self.assertIn('冻结关联或期限',page)
            self.assertFalse(eligible_now('prelaunch',row,{'frozen_records':records}))

    def test_markup_and_source_urls_are_sanitized(self):
        d=self.data();d['candidates'][0]['name']='<script>alert(1)</script>'
        d['candidates'][0]['reasons']=['/Users/private/secret.json']
        publish('yichujifa',d);page=render_module('yichujifa')
        self.assertNotIn('<script>alert',page);self.assertNotIn('/Users/private',page)
        self.assertNotIn('javascript:',links([{'url':'javascript:alert(1)'}]))
        self.assertNotIn('password',links([{'url':'https://user:password@example.com'}]))

    def test_six_navigation_entries_and_unique_ids(self):
        class Parser(HTMLParser):
            def __init__(self):super().__init__();self.ids=[];self.nav=[]
            def handle_starttag(self,tag,attrs):
                attrs=dict(attrs)
                if 'id' in attrs:self.ids.append(attrs['id'])
                if 'data-page' in attrs:self.nav.append(attrs['data-page'])
        p=Parser();p.feed(render_dashboard())
        self.assertEqual(p.nav,['hot','dragon','yichujifa','prelaunch','strategy','reports'])
        self.assertEqual(len(p.ids),len(set(p.ids)))


if __name__=='__main__':unittest.main()
