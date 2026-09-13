"""Artificial Dragon Cycle fixtures; rule consistency, not a return backtest."""
import copy
import datetime as dt
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from dragon_research import research, volume_ledger, append_intraday, refill_check
from hot_sector_board import build_board
from refresh_hot_sector_board import resolve_sessions, research_hot, shared_daily_history

TZ = ZoneInfo('Asia/Shanghai')
ROOT = Path(__file__).resolve().parents[1]


def bar(day, close, volume):
    return {'date': day, 'open': close, 'high': close, 'low': close, 'close': close, 'volume_shares': volume}


def artificial_hot(volumes=(100,110,115), boards=2):
    days = ['2026-09-03','2026-09-04','2026-09-07']
    closes = [10,11,12.1]
    history = [[day,str(close),str(close),str(close),str(close),str(volume/100)] for day, close, volume in zip(days,closes,volumes)]
    history.insert(0,['2026-09-02','10','10','10','10','1'])
    pool = [{'c': f'60000{i}', 'n': f'人工示例{i}', 'hybk': '示例行业', 'lbc': boards,
             'p':12100, 'amount':100000000+i, 'hs':8, 'fund':10000000, 'zbc':1,
             'zttj':{'ct':boards}, 'fbt':93500, 'lbt':104500} for i in range(1,5)]
    config = json.loads((ROOT/'examples/config/emotion_config.json').read_text())
    hot = build_board({'data':{'qdate':20260907,'tc':len(pool),'pool':pool}},
        {r['c']:{'price':12.1,'timestamp':'20260907150000'} for r in pool},
        {r['c']:copy.deepcopy(history) for r in pool},cutoff='2026-09-07',next_session='2026-09-08',config=config)
    hot.update(as_of='2026-09-07T15:00:00+08:00',valid_until='2026-09-08T15:00:00+08:00',evidence_digest='artificial')
    return hot


class DragonResearchTests(unittest.TestCase):
    def setUp(self):
        self.now=dt.datetime(2026,9,8,9,55,tzinfo=TZ)

    def test_first_and_third_expansion_include_first_board(self):
        bars=[bar('2026-09-02',10,100),bar('2026-09-03',11,120),bar('2026-09-04',12.1,125),bar('2026-09-07',13.31,150)]
        ledger=volume_ledger(bars,3,'2026-09-07')
        self.assertTrue(ledger['complete'])
        self.assertEqual([r['expansion_count'] for r in ledger['days']],[1,1,2])
        self.assertIn('收盘数据',ledger['days'][-1]['time_note'])

    def test_intraday_repeated_queries_never_double_count(self):
        bars=[bar('2026-09-03',10,100),bar('2026-09-04',11,120),bar('2026-09-07',12.1,125)]
        ledger=volume_ledger(bars,2,'2026-09-07')
        quote={'timestamp':'20260908095500','price':13.31,'volume_shares':150}
        for volume in (150,155,155):
            ledger=append_intraday(ledger,bars[-1],dict(quote,volume_shares=volume),self.now)
            self.assertEqual(ledger['expansion_count'],2)
            self.assertEqual(len(ledger['days']),3)
        before=append_intraday(volume_ledger(bars,2,'2026-09-07'),bars[-1],dict(quote,volume_shares=140),self.now)
        self.assertEqual(before['expansion_count'],1)

    def test_previous_shrink_excluded_even_when_auction_strong(self):
        hot=artificial_hot((100,110,100))
        for group in hot['sectors']:
            for row in group['items']:
                row['auction']={'source_asof':'2026-09-08T09:25:00+08:00','qualified':True,'verified':True,'gap_pct':8,'turnover_pct':1}
        result=research(self.now,'live',hot=hot,previous={})
        self.assertEqual(len(result['selected']),0)
        self.assertFalse(any(r['eligible']for r in result['candidates']))
        self.assertTrue(all(any('缩量'in reason for reason in r['reasons'])for r in result['candidates']))

    def test_yesterday_refill_not_today_entry_and_rank_never_backfills(self):
        result=research(self.now,'live',hot=artificial_hot(),previous={})
        self.assertEqual(len(result['candidates']),3)
        self.assertEqual([r['sector_member_rank']for r in result['candidates']],[1,2,3])
        self.assertTrue(all(r['historical_screen_pass']for r in result['candidates']))
        self.assertFalse(any(r['eligible']for r in result['candidates']))
        self.assertTrue(all(r['prior_refill']['verified']for r in result['candidates']))
        self.assertTrue(all(not r['refill_timeline']for r in result['candidates']))

    def test_second_expansion_risk_has_priority_and_t_plus_one_visible(self):
        result=research(self.now,'live',hot=artificial_hot((100,120,144)),previous={})
        self.assertTrue(all(r['status']=='退出预警'for r in result['candidates']))
        self.assertFalse(result['selected'])
        self.assertIn('当日新买部分无法当日卖出',result['risk_note'])
        self.assertEqual(len(result['monitoring']['observed']),len(result['candidates']))
        self.assertTrue(all('风险预警观察' in row['reason'] for row in result['monitoring']['observed']))

    def test_intraday_second_expansion_flows_through_research_not_only_calculator(self):
        hot=artificial_hot((100,120,125))
        for group in hot['sectors']:
            for row in group['items']:
                row['current_quote']={'timestamp':'20260908095500','price':13.31,'volume_shares':150}
        result=research(self.now,'live',hot=hot,previous={})
        self.assertTrue(all(row['expansion_count']==2 for row in result['candidates']))
        self.assertTrue(all(row['status']=='退出预警' for row in result['candidates']))
        self.assertTrue(all(row['history'][-1]['data_kind']=='intraday_cumulative' for row in result['candidates']))

    def test_complete_native_evidence_requires_fresh_intraday_volume(self):
        hot=artificial_hot((100,110,115))
        hot['market_evidence']={'verified':True,'allows_entry':True,'same_time_comparison':True,
            'source_asof':'2026-09-08T09:54:50+08:00','source_url':'https://example.invalid/market'}
        events=[{'source_asof':'2026-09-08T'+clock+'+08:00','state':state,'orderbook_verified':True,'source_url':'https://example.invalid/orderbook'}
                for clock,state in [('09:50:00','sealed'),('09:52:00','opened'),('09:54:30','sealed')]]
        for group in hot['sectors']:
            for row in group['items']:
                row['auction']={'source_asof':'2026-09-08T09:25:00+08:00','qualified':True,'verified':True}
                row['refill_evidence']={'events':events}
                row['security_evidence']={'source_asof':'2026-09-08T09:00:00+08:00','verified':True,'ordinary_mainboard_10pct':True,
                    'effective_date':'2026-09-08','source_url':'https://example.invalid/security'}
        result=research(self.now,'live',hot=hot,previous={})
        self.assertFalse(any(row['eligible']for row in result['candidates']))
        for group in hot['sectors']:
            for row in group['items']:
                row['current_quote']={'timestamp':'20260908095450','price':13.31,'volume_shares':80}
        result=research(self.now,'live',hot=hot,previous={})
        self.assertTrue(all(row['eligible']for row in result['candidates']))
        self.assertTrue(all(row['signal_valid_until']=='2026-09-08T09:56:00+08:00'for row in result['candidates']))

    def test_pins_preserved_but_other_strategy_and_expired_rank_not_imported(self):
        pins=[{'code':'600099','name':'人工持续跟踪','confirmed':True,'valid_until':'2026-09-08'}]
        old={'monitoring':{'pins':pins,'observed':[{'code':'600098','name':'旧名单'}]}}
        result=research(self.now,'live',hot=artificial_hot(),previous=old)
        self.assertEqual(result['monitoring']['pins'],pins)
        self.assertNotIn('600098',[r['code']for r in result['monitoring']['observed']])
        stale=research(self.now+dt.timedelta(days=1),'live',hot=artificial_hot(),previous=old)
        self.assertEqual(stale['status'],'unavailable')
        self.assertFalse(stale['monitoring']['observed'])
        self.assertIsNone(stale['monitoring']['valid_until'])

    def test_refill_requires_orderbook_sequence_latest_state_and_freshness(self):
        events=[{'source_asof':'2026-09-08T'+clock+'+08:00','state':state,'orderbook_verified':True,'source_url':'https://example.invalid/orderbook'}
                for clock,state in [('09:50:00','sealed'),('09:52:00','opened'),('09:54:30','sealed')]]
        self.assertTrue(refill_check({'events':events},self.now)[0])
        self.assertFalse(refill_check({'events':events},self.now+dt.timedelta(minutes=2))[0])
        events.append(dict(events[-1],source_asof='2026-09-08T09:54:40+08:00',state='opened'))
        self.assertFalse(refill_check({'events':events},self.now)[0])
        self.assertFalse(refill_check({'events':[dict(e,orderbook_verified=False) for e in events]},self.now)[0])

    def test_known_risk_not_hidden_by_another_invalid_cycle_day(self):
        bars=[bar('2026-09-02',10,100),bar('2026-09-03',11,120),bar('2026-09-04',12.0,125),bar('2026-09-07',13.2,150)]
        ledger=volume_ledger(bars,3,'2026-09-07')
        self.assertFalse(ledger['complete'])
        self.assertEqual(ledger['expansion_count'],2)

    def test_failed_history_reuses_same_date_bars_and_never_erases_known_risk(self):
        hot=artificial_hot((100,120,144))
        previous=research(self.now,'intraday',hot=hot,previous={})
        broken=copy.deepcopy(hot)
        for group in broken['sectors']:
            for row in group['items']:row['bars']=[]
        result=research(self.now,'intraday',hot=broken,previous=previous)
        self.assertTrue(all(row['status']=='退出预警'for row in result['candidates']))
        self.assertTrue(all(row['history_cache_note']for row in result['candidates']))
        without=research(self.now,'intraday',hot=broken,previous={})
        self.assertEqual(without['status'],'unavailable')


class HotPhaseTests(unittest.TestCase):
    def test_shared_daily_unadjusted_units_and_source_time(self):
        now=dt.datetime(2026,9,8,9,tzinfo=TZ)
        source={'url':'https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol=sh600001',
            'retrieved_at':'2026-09-08T08:00:00+08:00','adjustment':'unadjusted',
            'observation_through':'2026-09-07','sha256':'a'*64}
        document={'as_of':'2026-09-07','generated_at':'2026-09-08T08:01:00+08:00',
            'stocks':[{'code':'600001','history_verified':True,'errors':[],'sources':[source],
                'bars':[{'date':'2026-09-07','open':10,'close':11,'high':11,'low':10,'volume':12345}]}]}
        result=shared_daily_history(now,dt.date(2026,9,7),document=document)
        self.assertEqual(result['600001']['rows'][0][-1],123.45)
        self.assertEqual(result['600001']['sources'][0]['retrieved_at'],source['retrieved_at'])
        for changes in ({'adjustment':'qfq'},{'observation_through':'2026-09-04'},{'retrieved_at':'2026-09-08T09:30:00+08:00'}):
            wrong=copy.deepcopy(document);wrong['stocks'][0]['sources'][0].update(changes)
            self.assertFalse(shared_daily_history(now,dt.date(2026,9,7),document=wrong))

    def test_premarket_uses_last_complete_day_close_uses_next_session(self):
        pre=dt.datetime(2026,9,14,9,tzinfo=TZ)
        self.assertEqual(tuple(str(d)for d in resolve_sessions(pre)),('2026-09-11','2026-09-14'))
        self.assertEqual(tuple(str(d)for d in resolve_sessions(pre.replace(hour=16),'close')),('2026-09-14','2026-09-15'))
        with self.assertRaises(ValueError):resolve_sessions(pre,'close')

    def test_verified_empty_and_source_failure_are_distinct(self):
        moment=dt.datetime(2026,9,8,9,tzinfo=TZ)
        with patch('refresh_hot_sector_board.get',return_value=json.dumps({'data':{'qdate':20260907,'tc':0,'pool':[]}})):
            result=research_hot(moment,persist=False)
        self.assertEqual(result['status'],'empty')
        self.assertTrue(result['coverage']['source_count_verified'])
        with patch('refresh_hot_sector_board.get',return_value=json.dumps({'data':{'qdate':20260904,'tc':0,'pool':[]}})):
            result=research_hot(moment,persist=False)
        self.assertEqual(result['status'],'unavailable')
        self.assertFalse(result['coverage']['source_count_verified'])


if __name__=='__main__':unittest.main()
