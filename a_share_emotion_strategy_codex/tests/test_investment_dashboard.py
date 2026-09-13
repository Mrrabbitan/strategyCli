"""Workbench regressions using isolated private storage and artificial records."""
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from investment_dashboard import render_dashboard, verified_reports
from build_investment_site import build
from research_store import private_path, atomic_json


class InvestmentDashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.env=patch.dict(os.environ,{'AUTOSTRATEGY_PRIVATE_ROOT':self.tmp.name});self.env.start();self.addCleanup(self.env.stop)
        self.y=patch('investment_dashboard.research_status',return_value={'as_of':None,'generated_at':None,'state':'测试空状态','missing':[],'link':None})
        self.y.start();self.addCleanup(self.y.stop)
    def test_four_pages_and_independent_rules_without_accounts(self):
        page=render_dashboard(dt.date(2026,9,11))
        for name in ('hot','dragon','strategy','reports'):self.assertIn(f'id="page-{name}"',page)
        for name in ('prelaunch','yichujifa','dragon'):self.assertIn(f'id="strategy-{name}"',page)
        for old in ('page-plans','page-etf','holdings-rows','portfolio_snapshot','account_total_cny'):
            # Removed hashes may exist as compatibility redirects, not pages or payload.
            self.assertNotIn(f'id="{old}"',page)
        self.assertNotIn('@@',page);self.assertNotIn('"holdings":',page)
        self.assertIn('第二次',page);self.assertIn('9:50',page);self.assertIn('RR',page)
    def test_build_clears_stale_files_and_is_idempotent(self):
        out=private_path('public');out.mkdir(parents=True);(out/'old-account.html').write_text('SECRET')
        build(dt.date(2026,9,11));self.assertFalse((out/'old-account.html').exists())
        before=(out/'version.json').read_bytes();build(dt.date(2026,9,11))
        self.assertEqual(before,(out/'version.json').read_bytes())
    def test_invalid_chart_does_not_replace_last_good_page(self):
        out=build(dt.date(2026,9,11));before=(out/'latest.html').read_bytes()
        atomic_json(private_path('research/fed/current.json'),{'charts':[{'file':'../../private.png'}]})
        with self.assertRaises(ValueError):build(dt.date(2026,9,11))
        self.assertEqual(before,(out/'latest.html').read_bytes())
    def test_only_verified_slot_record_is_published_without_accounts(self):
        day=dt.date(2026,9,11);slot=private_path('reports')/str(day)/'10-30.json'
        atomic_json(slot,{'trade_day_verified':True,'report_date':str(day),'slot':'10_30',
                         'generated_at':'2026-09-11T10:30:00+08:00','portfolio_snapshot':{'secret':'PRIVATE-ACCOUNT'},
                         'breadth':{'up':123},'indices':{},'risk_level':'观察'})
        slots=verified_reports(day);self.assertEqual(sum(x['url'] is not None for x in slots),1)
        out=build(day);page=(out/'reports'/str(day)/'10-30.html').read_text()
        self.assertNotIn('PRIVATE-ACCOUNT',page);self.assertNotIn('portfolio_snapshot',page)
        self.assertIn('123',page)
        atomic_json(slot,{'trade_day_verified':False,'report_date':str(day),'slot':'10_30'})
        build(day);self.assertFalse((out/'reports'/str(day)/'10-30.html').exists())
    def test_malformed_topic_does_not_break_other_modules(self):
        p=private_path('research/fed/current.json');p.parent.mkdir(parents=True);p.write_text('{bad')
        page=render_dashboard(dt.date(2026,9,11));self.assertIn('专题快照不可用',page)
        self.assertIn('id="page-hot"',page)

if __name__=='__main__':unittest.main()
