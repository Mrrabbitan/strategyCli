from __future__ import annotations
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hot_sector_board import build_board, load_board, render_board, volume_audit


class BoardTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT/'examples/config/emotion_config.json').read_text())
        self.pool = [{'c':f'6000{i:02}', 'n':f'测试{i}', 'hybk':theme, 'lbc':2,
                      'p':12100, 'amount':100000000+i*1000, 'hs':8, 'fund':5000000,
                      'zbc':0, 'zttj':{'ct':2}} for i,theme in enumerate(['甲']*4+['乙']*3+['丙']*3+['丁']*3+['戊']*3+['己']*2)]
        self.payload = {'data':{'qdate':20260908,'tc':len(self.pool),'pool':self.pool}}
        self.quotes = {r['c']:{'price':12.1,'timestamp':'20260908160000'} for r in self.pool}
        self.history = [['2026-09-03','9','9','9','9','90'],
                        ['2026-09-04','10','10','10','10','100'],
                        ['2026-09-07','10','11','11','10','120'],
                        ['2026-09-08','11','12.1','12.1','11','144']]
        self.histories = {r['c']:copy.deepcopy(self.history) for r in self.pool}

    def build(self, **overrides):
        args=dict(pool_payload=self.payload,quotes=self.quotes,histories=self.histories,
                  cutoff='2026-09-08',next_session='2026-09-09',config=self.config)
        args.update(overrides)
        return build_board(**args)

    def test_two_expansions_override_rank_without_backfill(self):
        board=self.build()
        self.assertEqual(len(board['sectors']),5)
        self.assertEqual([len(g['items']) for g in board['sectors']],[3,3,3,3,3])
        for g in board['sectors']:
            self.assertEqual([r['code'] for r in g['items']],g['top_codes'])
            for r in g['items']:
                self.assertEqual(r['volume']['expansion_count'],2)
                self.assertTrue(any('第二次' in reason for reason in r['vetoes']))
                self.assertFalse(r['auction_buy_eligible'])

    def test_shrink_day_blocks_even_when_quote_and_rank_pass(self):
        histories=copy.deepcopy(self.histories)
        for rows in histories.values():rows[-1][5]='119'
        board=self.build(histories=histories)
        self.assertTrue(all(any('缩量' in s for s in r['vetoes']) for g in board['sectors'] for r in g['items']))

    def test_missing_or_stale_quotes_never_create_auction_permission(self):
        for quotes in ({},{c:dict(q,timestamp='20260907160000') for c,q in self.quotes.items()}):
            board=self.build(quotes=quotes)
            self.assertTrue(all(not r['quote_verified'] and not r['actionable'] for g in board['sectors'] for r in g['items']))

    def test_wrong_pool_date_or_partial_pool_fails(self):
        for change in ({'qdate':20260907},{'tc':99}):
            payload=copy.deepcopy(self.payload);payload['data'].update(change)
            with self.assertRaises(ValueError):self.build(pool_payload=payload)

    def test_volume_missing_day_or_conflicting_limit_fails(self):
        for rows in (self.history[:-1],self.history+[self.history[-1]],
                     [*self.history[:-1],['2026-09-08','11','11.9','12.1','11','144']]):
            self.assertFalse(volume_audit(rows,2,'2026-09-08',12.1)['complete'])

    def test_first_board_does_not_inherit_entry_qualification(self):
        payload=copy.deepcopy(self.payload)
        for r in payload['data']['pool']:r['lbc']=1
        board=self.build(pool_payload=payload)
        self.assertTrue(all(any('仅首板' in s for s in r['vetoes']) for g in board['sectors'] for r in g['items']))

    def test_policy_change_and_duplicate_rank_reject_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'current.json'
            board=self.build();path.write_text(json.dumps(board))
            self.assertTrue(load_board(path)['available'])
            board['policy_hash']='old';path.write_text(json.dumps(board))
            self.assertFalse(load_board(path)['available'])
            board=self.build();board['sectors'][0]['items'][0].pop('selection_reasons')
            path.write_text(json.dumps(board))
            self.assertFalse(load_board(path)['available'])
            board=self.build();board['sectors'][0]['items'][1]['sector_member_rank']=1
            path.write_text(json.dumps(board))
            self.assertFalse(load_board(path)['available'])

    def test_render_escapes_names_and_exposes_expiry(self):
        board=self.build();board['available']=True
        board['sectors'][0]['items'][0]['name']='<script>bad</script>'
        board['sectors'][0]['items'][0]['selection_reasons'].append('<img src=x onerror=bad>')
        page=render_board(board)
        self.assertNotIn('<script>bad',page)
        self.assertIn('&lt;script&gt;',page)
        self.assertNotIn('<img src=x',page)
        self.assertEqual(page.count('data-focus-code='),15)
        self.assertEqual(page.count('为什么入选'),15)
        self.assertIn('data-expires="2026-09-09T15:00:00+08:00"',page)
        self.assertIn('未接入实时集合竞价',page)


if __name__=='__main__':unittest.main()
