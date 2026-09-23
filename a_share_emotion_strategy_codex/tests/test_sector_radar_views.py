"""Sector-radar presentation checks use fictional securities, without network IO."""
import copy
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import sector_radar_views as views
from investment_dashboard import render_dashboard

NOW = dt.datetime(2026, 9, 23, 16, 0, tzinfo=views.TZ)
ROOT = Path(__file__).resolve().parents[1]


def fixture():
    check = {'status': 'passed', 'observation_passed': True, 'rule_version': 'fiction-1',
             'rule_hash': 'a' * 64, 'as_of': '2026-09-23T15:00:00+08:00',
             'reasons': ['虚构收盘资格通过'], 'confirmation': ['等待虚构确认'],
             'risks': ['虚构失效条件'], 'levels': [{'label': '测试参考', 'value': 10}],
             'sources': [{'label': '人工资料', 'url': 'https://example.com/fiction'}]}
    rows = []
    for i in range(13):
        rows.append({'code': str(600001 + i), 'name': f'虚构企业{i}',
                     'state': 'watch' if i < 3 else 'pending',
                     'groups': ['group-a', 'group-b'] if i == 0 else ['group-a'],
                     'security_reasons': [],
                     'strategy_checks': {'three-step': copy.deepcopy(check) if i < 3 else {'status': 'pending', 'reasons': ['缺失人工证据']}},
                     'metrics': {'return_5d': .035, 'drawdown_5d': -.01, 'close_position_5d': .8, 'median_amount_5d': 123456},
                     'ranks': {'group-a': {'rank': i + 1, 'score': 70.0, 'relative_5d': .01}},
                     'bars': [{'date': '2026-09-23', 'open': 10, 'high': 12, 'low': 9, 'close': 11, 'volume_shares': 1000}]})
    sectors = []
    for i in range(12):
        sid = 'group-a' if i == 0 else 'group-b' if i == 1 else f'group-{i}'
        sectors.append({'id': sid, 'label': f'人工板块{i}', 'provider': '测试供应商',
                        'provider_code': f'fiction{i}', 'approximate': i == 0,
                        'difference': '人工近似分类范围更宽' if i == 0 else '',
                        'membership_as_of': '2026-09-23T16:00:00+08:00',
                        'coverage': {'complete': i == 0, 'received': 13 if i == 0 else 0},
                        'missing': [] if i == 0 else ['人工成分缺失'],
                        'top_codes': ['600001', '600002', '600003'] if i == 0 else [],
                        'metrics': {'relative_3d': .01, 'skill_pass_fraction': .25},
                        'forward_rank': None, 'forward_score': None})
    return {'schema_version': 1, 'module_id': 'sector-radar', 'version': 'fiction-1',
            'as_of': '2026-09-23T15:00:00+08:00', 'generated_at': NOW.isoformat(),
            'valid_until': '2026-09-24T15:00:00+08:00', 'signal_date': '2026-09-23',
            'status': 'partial', 'missing': ['人工比较覆盖不足'], 'summary': '人工测试研究',
            'coverage': {'sectors_expected': 12, 'matrix_complete': False, 'unique_stocks': 13},
            'sectors': sectors, 'candidates': rows, 'forward_top': [], 'executions': [], 'changes': []}


class SectorRadarViewsTests(unittest.TestCase):
    def page(self, data=None, state='partial'):
        snapshot = fixture() if data is None else data
        with patch.object(views, 'load_module', return_value={'data': snapshot, 'state': state, 'note': '人工时效说明', 'attempt': {}}) as loaded:
            text = views.render_page(NOW)
        loaded.assert_called_once_with('sector-radar', now=NOW)
        return text

    def test_all_twelve_sectors_and_all_thirteen_candidates_not_just_top_ten(self):
        text = self.page()
        self.assertEqual(text.count('class="radar-sector"'), 12)
        self.assertEqual(text.count('<tr data-candidate '), 13)
        self.assertEqual(text.count('class="radar-top-stock"'), 3)
        self.assertIn('虚构企业12', text)
        self.assertIn('近似分类 · 范围不同', text)
        self.assertIn('人工近似分类范围更宽', text)
        self.assertIn('未发布板块前瞻前三', text)
        self.assertIn('1 只存在跨板块重叠', text)
        self.assertIn('五策略校验完整：否', text)
        self.assertIn('25.00%', text)
        self.assertIn('3.50%', text)
        self.assertIn('日线与成交量证据', text)

    def test_supplier_and_overlap_identifiers_have_readable_labels(self):
        data = fixture()
        data['sectors'][0]['provider'] = 'ths'
        data['sectors'][0]['overlap'] = [{'sector_id': 'group-b', 'count': 1}]
        text = self.page(data)
        self.assertIn('实际供应商 同花顺', text)
        self.assertIn('成分重叠：人工板块1 1只', text)
        self.assertNotIn('成分重叠：group-b', text)

    def test_all_five_independent_statuses_versions_and_links(self):
        data = fixture()
        checks = data['candidates'][0]['strategy_checks']
        for sid, state in zip((sid for sid, _ in views.STRATEGIES), ('passed', 'failed', 'pending', 'not_applicable', 'not_run')):
            checks[sid] = {'status': state, 'observation_passed': state == 'passed', 'rule_version': 'distinct-' + sid,
                           'as_of': '2026-09-23T15:00:00+08:00', 'reasons': ['独立原生理由']}
        text = self.page(data)
        for sid, _ in views.STRATEGIES:
            self.assertIn(f'href="#strategy-{sid}"', text)
            self.assertIn('distinct-' + sid, text)
        for label in views.CHECK_NAMES.values():
            self.assertIn(label, text)

    def test_old_or_failed_results_do_not_display_current_observation(self):
        for state in ('expired', 'unavailable'):
            text = self.page(state=state)
            self.assertIn('class="qualified-count">0 条', text)
            self.assertNotIn('data-bucket="qualified"', text)
            self.assertNotIn('>收盘观察 · 非买点<', text)
            self.assertIn('历史判断 · 观察条件通过', text)

    def test_pass_status_without_native_observation_assertion_not_promoted(self):
        data = fixture()
        for row in data['candidates']:
            row['strategy_checks']['three-step']['observation_passed'] = False
        text = self.page(data)
        self.assertIn('class="qualified-count">0 条', text)
        self.assertNotIn('class="radar-top-stock"', text)
        self.assertIn('待验证 · 观察资格未确认', text)

    def test_native_branches_stage_and_dragon_volume_ledger_remain_visible(self):
        data = fixture()
        checks = data['candidates'][0]['strategy_checks']
        checks['yichujifa'] = {'status': 'pending', 'native_status': '原生分支待复核', 'branches': [
            {'branch': '一进二', 'original_rank': 2, 'status': 'pending', 'native_status': '人工阶段一', 'reasons': ['分支一证据不足']},
            {'branch': '首板后守位', 'original_rank': 3, 'status': 'failed', 'native_status': '人工阶段二', 'reasons': ['分支二条件失败']}]}
        checks['dragon'] = {'status': 'failed', 'original_rank': 2, 'as_of': '2026-09-23T15:00:00+08:00',
                            'native_status': '退出预警', 'volume_ledger': {'private': 'HIDDEN-LEDGER', 'days': [
                                {'date': '2026-09-23', 'volume_shares': 144000, 'previous_volume_shares': 120000, 'volume_ratio': 1.2, 'expansion_count': 2, 'confirmed_as_of': '2026-09-23T15:00:00+08:00'},
                                {'date': '2026-09-24', 'volume_shares': 987654321, 'expansion_count': 3, 'confirmed_as_of': '2026-09-24T15:00:00+08:00'}]}}
        text = self.page(data)
        for evidence in ('原生分支待复核', '分支一证据不足', '分支二条件失败', '热点板内原始名次：2', '144000', '第二次放量', '退出预警'):
            self.assertIn(evidence, text)
        self.assertNotIn('HIDDEN-LEDGER', text)
        self.assertNotIn('987654321', text)

    def test_no_raw_data_or_private_paths_and_markup_escaped(self):
        data = fixture()
        data['raw'] = {'password': 'PRIVATE-RAW-CONTENT'}
        data['coverage']['unapproved'] = 'PRIVATE-COVERAGE-CONTENT'
        data['sectors'][0]['coverage']['path'] = 'PRIVATE-SECTOR-CONTENT'
        data['candidates'][0]['name'] = '<script>alert(1)</script>'
        data['candidates'][0]['security_reasons'] = ['/Users/private/account.json']
        data['candidates'][0]['raw_holdings'] = {'account': 'PRIVATE-HOLDING'}
        data['sources'] = [{'url': 'javascript:alert(1)'}, {'url': 'file:///tmp/secret'}, {'url': 'https://example.com/reference', 'label': 'safe'}]
        text = self.page(data)
        for prohibited in ('PRIVATE-RAW-CONTENT', 'PRIVATE-COVERAGE-CONTENT', 'PRIVATE-SECTOR-CONTENT', 'PRIVATE-HOLDING', '/Users/private', 'javascript:alert', 'file:///tmp', '<script>'):
            self.assertNotIn(prohibited, text)
        self.assertIn('&lt;script&gt;', text)
        self.assertIn('href="https://example.com/reference"', text)

    def test_future_bars_omitted_and_codes_deduplicated(self):
        data = fixture()
        data['candidates'][0]['bars'].append({'date': '2026-09-24', 'open': 9001, 'close': 9002, 'high': 9003, 'low': 9000})
        data['candidates'].append(copy.deepcopy(data['candidates'][0]))
        text = self.page(data)
        self.assertEqual(text.count('<tr data-candidate '), 13)
        self.assertNotIn('9003', text)

    def test_never_run_failure_and_valid_empty_are_distinct(self):
        self.assertIn('尚未执行', self.page({}, 'not_run'))
        self.assertIn('本轮研究受阻', self.page({}, 'unavailable'))
        data = fixture(); data['status'] = 'empty'; data['candidates'] = []
        for sector in data['sectors']: sector['top_codes'] = []
        text = self.page(data, 'empty')
        self.assertIn('筛选完成 · 正式观察为空', text)
        self.assertNotIn('本轮研究受阻', text)

    def test_nine_pages_and_old_anchors_work_without_private_data(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'AUTOSTRATEGY_PRIVATE_ROOT': temp}):
            text = render_dashboard(dt.date(2026, 9, 23))
        names = ('hot', 'dragon', 'yichujifa', 'prelaunch', 'late-day', 'three-step', 'sector-radar', 'strategy', 'reports')
        for name in names:
            self.assertIn(f'id="page-{name}"', text)
        self.assertEqual(text.count('class="nav-item'), 9)
        for old in ('strategy-late-day', 'strategy-three-step', 'dragon-cycle-watchlist', 'hot-sector-board'):
            self.assertIn(f'id="{old}"', text)
        self.assertNotIn('@@', text)

    @unittest.skipUnless(shutil.which('node'), 'Node required for client function checks')
    def test_csv_is_local_filtered_and_formula_safe(self):
        js = (ROOT / 'templates/investment.js').read_text()
        helper = js[js.index('  function radarCsvCell(value)'):js.index("  document.querySelector('[data-radar-export]')")]
        assertions = r'''
const assert = require('node:assert/strict');
assert.equal(radarCsvCell('=2+2'), '"\'=2+2"');
assert.equal(radarCsvCell('  +command'), '"\'  +command"');
assert.equal(radarCsvCell('@cmd'), '"\'@cmd"');
assert.equal(radarCsvCell('\tcommand'), '"\'\tcommand"');
assert.equal(radarCsvCell('000001'), '"\'000001"');
assert.equal(radarCsvCell('a"b'), '"a""b"');
const row = text => ({cells: [{textContent: text}]});
const table = {querySelectorAll: selector => selector === 'thead tr' ? [row('header')] : selector === 'tbody tr:not([hidden])' ? [row('visible')] : [row('MUST-NOT-EXPORT')]};
const csv = radarCsv(table);assert.ok(csv.startsWith('\ufeff'));assert.ok(csv.includes('visible'));assert.ok(!csv.includes('MUST-NOT-EXPORT'));
'''
        subprocess.run(['node', '-e', helper + assertions], check=True, capture_output=True, text=True)
        self.assertIn("new Blob([radarCsv(table)]", js)
        self.assertNotIn('/api/sector', js)

    @unittest.skipUnless(shutil.which('node'), 'Node required for client function checks')
    def test_filter_keeps_detail_rows_in_sync_and_reading_state_keeps_sector(self):
        js = (ROOT / 'templates/investment.js').read_text()
        filter_code = js[js.index('  function filterResearch(module)'):js.index("  document.querySelectorAll('[data-research-search]').forEach")]
        save_code = js[js.index('  function saveReading()'):js.index('  function filterResearch(module)')]
        harness = r'''
const assert = require('node:assert/strict');let activePage = 'sector-radar';
const input={value:'fiction',dataset:{researchSearch:'sector-radar'}};const bucket={value:'qualified'};const group={value:'groupA'};
const rows=[{dataset:{search:'fiction one',bucket:'qualified',radarGroups:'groupA',radarCode:'600001'}},{dataset:{search:'fiction two',bucket:'qualified',radarGroups:'groupB',radarCode:'600002'}}];
const details=rows.map(row=>({id:row.dataset.radarCode,dataset:{radarDetail:row.dataset.radarCode}}));
const count={textContent:''};const empty={hidden:true};
const panel={querySelector:s=>({'[data-research-search]':input,'[data-research-filter]':bucket,'[data-radar-group]':group,'[data-result-count]':count,'.filter-empty':empty})[s],querySelectorAll:s=>s==='[data-candidate]'?rows:s==='[data-radar-detail]'?details:[]};
const document={querySelector:s=>s.startsWith('[data-module=')?panel:s==='[data-radar-group]'?group:bucket,querySelectorAll:s=>s==='[data-research-search]'?[input]:s==='details[open][id]'?[details[0]]:[]};
let saved;const sessionStorage={setItem:(key,value)=>saved=JSON.parse(value)};const window={scrollY:444};
'''
        assertions = r'''
filterResearch('sector-radar');assert.equal(count.textContent,'1 条匹配记录');assert.equal(rows[0].hidden,false);assert.equal(rows[1].hidden,true);assert.equal(details[0].hidden,false);assert.equal(details[1].hidden,true);
saveReading();assert.equal(saved.page,'sector-radar');assert.equal(saved.filters[0].group,'groupA');assert.equal(saved.filters[0].search,'fiction');assert.equal(saved.filters[0].filter,'qualified');assert.equal(saved.scroll,444);assert.deepEqual(saved.open,['600001']);
'''
        subprocess.run(['node', '-e', harness + save_code + filter_code + assertions], check=True, capture_output=True, text=True)

    @unittest.skipUnless(shutil.which('node'), 'Node required for client function checks')
    def test_browser_expiry_demotes_forward_and_skill_statuses(self):
        js = (ROOT / 'templates/investment.js').read_text()
        expire = js[js.index('  function expireResearch()'):js.index('  async function refresh()')]
        harness = r'''
const assert = require('node:assert/strict');
const title={textContent:'Complete'},validity={textContent:'Current'},count={textContent:'1 条'};
const badge={textContent:'收盘观察',classList:{remove:()=>{}}},check={textContent:'观察条件通过'},forward={textContent:'未来1—5个交易日优先复核'};
const row={dataset:{bucket:'qualified'}};
const panel={dataset:{module:'sector-radar',moduleUntil:'2000-01-01T15:00:00+08:00'},querySelector:s=>({'.research-status b':title,'.module-validity':validity,'.qualified-count':count})[s],querySelectorAll:s=>({'.candidate-state':[badge],'[data-candidate]':[row],'.radar-check-state':[check],'[data-radar-forward-label]':[forward],'[data-live-until]':[]})[s]||[]};
const document={querySelectorAll:s=>s==='[data-module]'?[panel]:[]};let filtered=false;const filterResearch=()=>{filtered=true};
'''
        assertions = r'''
expireResearch();assert.equal(title.textContent,'历史研究');assert.equal(count.textContent,'0 条');assert.equal(row.dataset.bucket,'other');assert.equal(check.textContent,'历史判断 · 观察条件通过');assert.equal(forward.textContent,'历史前瞻 · 需重新复核');assert.ok(filtered);
expireResearch();assert.equal(check.textContent,'历史判断 · 观察条件通过');
'''
        subprocess.run(['node', '-e', harness + expire + assertions], check=True, capture_output=True, text=True)


if __name__ == '__main__':
    unittest.main()
