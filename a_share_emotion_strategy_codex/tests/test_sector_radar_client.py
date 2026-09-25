"""Fictional DOM checks for compact daily radar navigation (no data/network)."""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / 'templates/investment.js').read_text()


def section(start, end):
    offset = JS.index(start)
    return JS[offset:JS.index(end, offset)]


HARNESS = r'''
const assert = require('node:assert/strict');
let focused = null, activePage = 'sector-radar', saved = null;
function button(dataset) {
  return {dataset, attrs:{}, handlers:{}, tabIndex:0,
    setAttribute(name,value){this.attrs[name]=value;},
    addEventListener(name,fn){this.handlers[name]=fn;}, focus(){focused=this;}};
}
function day(date) {
  const tabs = ['review','observed','started'].map(kind => button({radarListTab:kind}));
  const lists = tabs.map(b => ({dataset:{radarList:b.dataset.radarListTab},hidden:b.dataset.radarListTab!=='review'}));
  const validity = {textContent:'仅供观察'};
  const rowStatuses = [{textContent:'待验证'},{textContent:'历史 · 未入选'}];
  return {dataset:{radarDay:date},hidden:true,tabs,lists,validity,rowStatuses,
    querySelectorAll(selector){return selector==='[data-radar-list-tab]'?tabs:selector==='[data-radar-list]'?lists:selector==='[data-radar-validity]'?[validity]:selector==='.radar-row-status'?rowStatuses:[];}};
}
const days = [day('2026-02-04'),day('2026-02-03')];
const dates = days.map(d => button({radarDate:d.dataset.radarDay}));
const root = {dataset:{},querySelectorAll(selector){return selector==='[data-radar-day]'?days:selector==='[data-radar-date]'?dates:[];}};
const input = {value:'fiction',dataset:{researchSearch:'hot'}}, bucket={value:'qualified'};
const document = {
  querySelector(selector){return selector==='[data-radar-daily]'?root:selector==='[data-research-filter="hot"]'?bucket:null;},
  querySelectorAll(selector){return selector==='[data-research-search]'?[input]:selector==='details[open][id]'?[{id:'fictional-detail'}]:selector==='[data-radar-until]'?days:[];}
};
const window = {scrollY:371};
const sessionStorage = {setItem(key,value){assert.equal(key,'investment-reading');saved=JSON.parse(value);}};
'''


@unittest.skipUnless(shutil.which('node'), 'Node required for offline client checks')
class SectorRadarClientTests(unittest.TestCase):
    def run_js(self, code):
        result = subprocess.run(['node', '-e', code], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def tab_code(self):
        return section('  function selectRadarDay(date, kind)', '  function saveReading()')

    def test_tabs_have_default_click_and_keyboard_navigation(self):
        self.run_js(HARNESS + self.tab_code() + r'''
setupRadarTabs();
assert.equal(root.dataset.radarSelectedDate,'2026-02-04');
assert.equal(root.dataset.radarSelectedList,'review');
assert.equal(days[0].hidden,false);assert.equal(days[1].hidden,true);
assert.equal(dates[0].attrs['aria-selected'],'true');assert.equal(dates[1].tabIndex,-1);
days[0].tabs[1].handlers.click();
assert.equal(days[0].lists[1].hidden,false);assert.equal(days[0].lists[0].hidden,true);
assert.equal(days[0].tabs[1].attrs['aria-selected'],'true');
let prevented = 0;
const key = key => ({key,preventDefault(){prevented++;}});
dates[0].handlers.keydown(key('ArrowLeft'));
assert.equal(root.dataset.radarSelectedDate,'2026-02-03');assert.equal(focused,dates[1]);
days[1].tabs[0].handlers.keydown(key('End'));
assert.equal(root.dataset.radarSelectedList,'started');assert.equal(focused,days[1].tabs[2]);
days[1].tabs[2].handlers.keydown(key('ArrowRight'));
assert.equal(root.dataset.radarSelectedList,'review');
dates[1].handlers.keydown(key('Home'));
assert.equal(root.dataset.radarSelectedDate,'2026-02-04');
assert.equal(root.dataset.radarSelectedList,'observed'); // each day's selection survives
assert.equal(prevented,4);
dates[0].handlers.keydown(key('Enter'));assert.equal(prevented,4);
''')

    def test_reading_state_restores_dates_lists_scroll_filters_and_details(self):
        save = section('  function saveReading()', '  function filterResearch(module)')
        self.run_js(HARNESS + self.tab_code() + save + r'''
setupRadarTabs();selectRadarDay('2026-02-04','started');selectRadarDay('2026-02-03','observed');
saveReading();
assert.deepEqual(saved.radar,{date:'2026-02-03',lists:{'2026-02-04':'started','2026-02-03':'observed'}});
assert.equal(saved.page,'sector-radar');assert.equal(saved.scroll,371);
assert.deepEqual(saved.open,['fictional-detail']);
assert.deepEqual(saved.filters,[{module:'hot',search:'fiction',filter:'qualified'}]);
selectRadarDay('2026-02-04','review');restoreRadarReading(saved.radar);
assert.equal(root.dataset.radarSelectedDate,'2026-02-03');assert.equal(root.dataset.radarSelectedList,'observed');
selectRadarDay('2026-02-04');assert.equal(root.dataset.radarSelectedList,'started');
restoreRadarReading({date:'2001-01-01',lists:{'2026-02-04':'invalid'}});
assert.equal(root.dataset.radarSelectedDate,'2026-02-04');assert.equal(root.dataset.radarSelectedList,'started');
''')
        self.assertIn('radar:radarReading()', JS)
        self.assertIn('restoreRadarReading(stored.radar)', JS)
        self.assertIn('requestAnimationFrame(()=>window.scrollTo(0,stored.scroll))', JS)

    def test_legacy_deep_link_reveals_its_date_and_list(self):
        locate = section('  function locateHash()', "  document.querySelectorAll('[data-page]')")
        self.run_js(HARNESS + self.tab_code() + locate + r'''
let scrolled = false;
const target = {tagName:'DETAILS',open:false,
  closest(selector){return selector==='.page'?{id:'page-sector-radar'}:selector==='[data-radar-day]'?days[1]:selector==='[data-radar-list]'?days[1].lists[2]:null;},
  scrollIntoView(){scrolled=true;}};
document.getElementById = id => id==='radar-stock-600001'?target:null;
const location={hash:'#radar-stock-600001'};
const switchPage = next => {activePage=next;};
const requestAnimationFrame = fn => fn();
setupRadarTabs();locateHash();
assert.equal(activePage,'sector-radar');assert.equal(root.dataset.radarSelectedDate,'2026-02-03');
assert.equal(root.dataset.radarSelectedList,'started');assert.equal(days[1].lists[2].hidden,false);
assert.equal(target.open,true);assert.equal(scrolled,true);
''')

    def test_expiry_changes_history_notice_without_reclassifying_stocks(self):
        expiry = section('  function expireResearch()', '  async function refresh()')
        self.run_js(HARNESS + expiry + r'''
days[0].dataset.radarUntil='2001-01-01T15:00:00+08:00';
days[1].dataset.radarUntil='2999-01-01T15:00:00+08:00';
days[0].dataset.radarSelectedList='observed';
const previousLists = JSON.stringify(days[0].lists);
expireResearch();
assert.equal(days[0].dataset.radarHistorical,'true');assert.ok(days[0].validity.textContent.startsWith('历史记录'));
assert.equal(days[1].dataset.radarHistorical,undefined);assert.equal(days[1].validity.textContent,'仅供观察');
assert.deepEqual(days[0].rowStatuses.map(row=>row.textContent),['历史 · 待验证','历史 · 未入选']);
assert.equal(days[1].rowStatuses[0].textContent,'待验证');
assert.equal(days[0].dataset.radarSelectedList,'observed');assert.equal(JSON.stringify(days[0].lists),previousLists);
const once = days[0].validity.textContent;expireResearch();assert.equal(days[0].validity.textContent,once);
assert.deepEqual(days[0].rowStatuses.map(row=>row.textContent),['历史 · 待验证','历史 · 未入选']);
''')

    def test_shared_filter_noops_without_controls_and_still_filters_other_strategies(self):
        filtering = section('  function filterResearch(module)', "  document.querySelectorAll('[data-research-search]').forEach")
        self.run_js(filtering + r'''
const assert = require('node:assert/strict');
const document = {querySelector(){return {querySelector(){return null;}};}};
filterResearch('sector-radar');
const rows = [{dataset:{search:'fiction one',bucket:'qualified'}},{dataset:{search:'fiction two',bucket:'other'}}];
const count={textContent:''},empty={hidden:true};
const panel={querySelector(selector){return {'[data-research-search]':{value:'fiction'},'[data-research-filter]':{value:'qualified'},'[data-result-count]':count,'.filter-empty':empty}[selector];},querySelectorAll(selector){return selector==='[data-candidate]'?rows:[];}};
document.querySelector=()=>panel;filterResearch('hot');
assert.equal(rows[0].hidden,false);assert.equal(rows[1].hidden,true);assert.equal(count.textContent,'1 条匹配记录');
''')

    def test_retired_matrix_controls_are_gone_and_refresh_is_readonly(self):
        self.assertNotIn('radarCsv', JS)
        self.assertNotIn('data-radar-export', JS)
        self.assertNotIn('data-radar-group', JS)
        self.assertNotIn('data-radar-detail', JS)
        self.assertNotIn('/api/sector', JS)
        self.assertIn("get('/api/dashboard-version')", JS)
        self.assertIn('if (busy || document.hidden) return', JS)
        self.assertIn('saveReading();location.reload()', JS)


if __name__ == '__main__':
    unittest.main()
