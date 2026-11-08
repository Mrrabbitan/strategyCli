/* Optional offline browser regression. Only fictional DOM records are served.
 * npm-provided playwright or PLAYWRIGHT_MODULE; optional BROWSER_EXECUTABLE.
 * Run: node tests/test_investment_browser.cjs
 */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const js = fs.readFileSync(path.join(__dirname, '../templates/investment.js'), 'utf8');
const pages = ['hot','dragon','yichujifa','prelaunch','late-day','three-step','sector-radar','sector-funds','highmark','strategy','reports','etf-monitor'];
const panel = `<div data-module="three-step" data-module-until="2099-01-01T15:00:00+08:00">
<div class="research-status"><b>Research</b></div><p class="module-validity">Fictional fixture</p>
<span class="qualified-count">1</span><input data-research-search="three-step">
<select data-research-filter="three-step"><option value="all">all</option><option value="qualified">pass</option><option value="pending">pending</option><option value="other">other</option></select>
<span data-result-count></span><p class="filter-empty" hidden>empty</p><div class="research-group">
<details id="fixture-a" data-candidate data-search="fiction-a" data-bucket="qualified"><summary>A <span class="candidate-state">Pass</span></summary>Fictional only</details>
<details id="fixture-b" data-candidate data-search="fiction-b" data-bucket="pending"><summary>B <span class="candidate-state">Pending</span></summary>Fictional only</details>
</div></div>`;
const rules = '<a href="#strategy-event-timing">Event timing</a><article id="strategy-event-timing"><h2>Fictional research overlay</h2><details id="strategy-event-timing-rules"><summary>Rules and source</summary>Fictional rules only</details></article><article id="strategy-late-day">Fictional existing rules</article>';
const funds = '<article id="topic-sector-fund-pulse"><h2>Fictional sector funds</h2>'+
  '<p>Artificial publisher reference only</p><details id="fixture-fund-sources"><summary>Fund sources</summary>'+
  '<div style="height:900px">Fictional source evidence only</div></details>'+
  '<div style="height:1500px">Fictional scroll restoration area</div></article>';
const highmark = '<article id="topic-highmark-abnormal"><h2>Fictional highmark monitor</h2>'+
  '<p>Artificial threshold evidence only</p><details id="highmark-other"><summary>Other fictional tracked stocks</summary>'+
  '<div>Artificial records, not market data</div></details>'+
  '<details id="highmark-evidence"><summary>Highmark evidence</summary>'+
  '<div style="height:900px">Fictional calculation evidence only</div></details>'+
  '<div style="height:1500px">Fictional highmark reading restoration area</div></article>';
const flowHistory = `<section id="etf-em-history" data-flow-history data-flow-selected-date="2026-08-14">
<label>Historical date<select data-flow-date><option value="2026-08-14">14</option><option value="2026-08-13">13</option><option value="2026-08-12">12</option></select></label>
<div id="etf-flow-days"><section id="etf-flow-day-2026-08-14" data-flow-day="2026-08-14">Fictional latest day</section>
<section id="etf-flow-day-2026-08-13" data-flow-day="2026-08-13" hidden><details id="fictional-flow-evidence"><summary>Fictional flow evidence</summary>Artificial amounts only</details></section>
<section id="etf-flow-day-2026-08-12" data-flow-day="2026-08-12" hidden>Fictional earliest day</section></div></section>`;
const etfs = `<div data-etf-monitor data-etf-asof="2099-01-01"><b data-etf-validity>Fictional fixture</b>${flowHistory}
<label>Search<input data-etf-search></label><label>State<select data-etf-filter><option value="all">all</option><option value="trend">trend</option><option value="turn">turn</option><option value="repair">repair</option><option value="weak">weak</option></select></label>
<label>Sort<select data-etf-sort><option value="default">default</option><option value="pct5">5 days</option><option value="pct20">20 days</option><option value="vol20">volatility</option></select></label>
<span data-etf-count></span><p data-etf-empty hidden>No match</p><div data-etf-quote-at="2000-01-01T10:00:00+08:00"><span data-etf-quote-status>Artificial quote</span></div><table><tbody>
<tr data-etf-row data-etf-code="000001" data-etf-name="fixture alpha" data-etf-bucket="trend" data-etf-pct5="1" data-etf-pct20="4" data-etf-vol20=""><td><details id="etfm-detail-000001"><summary>Alpha</summary>Fictional metrics</details></td></tr>
<tr data-etf-row data-etf-code="000002" data-etf-name="fixture beta" data-etf-bucket="repair" data-etf-pct5="3" data-etf-pct20="-2" data-etf-vol20="20"><td><details id="etfm-detail-000002"><summary>Beta</summary>Fictional metrics</details></td></tr>
<tr data-etf-row data-etf-code="000003" data-etf-name="fixture gamma" data-etf-bucket="trend" data-etf-pct5="2" data-etf-pct20="5" data-etf-vol20="10"><td>Gamma</td></tr>
</tbody></table><div style="height:1800px">Fictional scroll area</div></div>`;
const html = '<!doctype html><style>[hidden]{display:none!important}</style>'+pages.map(p=>`<button data-page="${p}">${p}</button>`).join('')+
  pages.map(p=>`<section class="page" id="page-${p}" hidden>${p==='three-step'?panel:p==='strategy'?rules:p==='sector-funds'?funds:p==='highmark'?highmark:p==='etf-monitor'?etfs:p}</section>`).join('')+
  '<div id="update-state"></div><script>'+js+'</script>';
let revision = 'one';
const server = http.createServer((req,res)=>{
  if(req.url==='/api/dashboard-version'){res.setHeader('Content-Type','application/json');res.end(JSON.stringify({revision}));}
  else {res.setHeader('Content-Type','text/html; charset=utf-8');res.end(html);}
});
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  let browser;
  try {
    browser=await chromium.launch({headless:true,...(process.env.BROWSER_EXECUTABLE?{executablePath:process.env.BROWSER_EXECUTABLE}:{})});
    const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
    const versionReady=page.waitForResponse(r=>r.url().endsWith('/api/dashboard-version'));
    await page.goto(`http://127.0.0.1:${server.address().port}/#page-three-step`);await versionReady;
    await page.locator('[data-research-search]').fill('fiction-b');
    await page.locator('[data-research-filter]').selectOption('pending');
    await page.locator('#fixture-b summary').click();
    assert.equal(await page.locator('[data-candidate]:not([hidden])').count(),1);
    revision='two';const reloaded=page.waitForEvent('load');
    await page.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));await reloaded;
    assert.equal(await page.locator('.page:not([hidden])').getAttribute('id'),'page-three-step');
    assert.equal(await page.locator('[data-research-search]').inputValue(),'fiction-b');
    assert.equal(await page.locator('[data-research-filter]').inputValue(),'pending');
    assert.notEqual(await page.locator('#fixture-b').getAttribute('open'),null);
    await page.locator('[data-research-search]').fill('');await page.locator('[data-research-filter]').selectOption('all');
    await page.evaluate(()=>{
      document.querySelector('[data-module]').dataset.moduleUntil='2000-01-01T15:00:00+08:00';
      document.dispatchEvent(new Event('visibilitychange'));
    });
    assert.equal(await page.locator('.qualified-count').textContent(),'0 条');
    assert.equal(await page.locator('[data-candidate][data-bucket="other"]').count(),2);
    assert.equal(await page.locator('[data-page]').count(),12);
    await page.locator('[data-page="sector-radar"]').click();
    assert.equal(await page.locator('.page:not([hidden])').getAttribute('id'),'page-sector-radar');
    await page.locator('[data-page="sector-funds"]').click();
    assert.equal(await page.locator('.page:not([hidden])').getAttribute('id'),'page-sector-funds');
    assert.equal(await page.locator('[data-page="sector-funds"]').getAttribute('aria-current'),'page');
    const fundsVersionReady=page.waitForResponse(r=>r.url().endsWith('/api/dashboard-version'));
    await page.goto(`http://127.0.0.1:${server.address().port}/#topic-sector-fund-pulse`);
    await fundsVersionReady;
    assert.equal(await page.locator('.page:not([hidden])').getAttribute('id'),'page-sector-funds');
    await page.locator('#fixture-fund-sources summary').click();
    await page.evaluate(()=>window.scrollTo(0,720));
    const fundsScrollBefore=await page.evaluate(()=>window.scrollY);
    assert.ok(fundsScrollBefore>700);
    revision='three';const fundsReloaded=page.waitForEvent('load');
    await page.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));await fundsReloaded;
    await page.waitForFunction(()=>Math.abs(window.scrollY-720)<10);
    assert.equal(await page.locator('.page:not([hidden])').getAttribute('id'),'page-sector-funds');
    assert.notEqual(await page.locator('#fixture-fund-sources').getAttribute('open'),null);
    assert.ok(Math.abs((await page.evaluate(()=>window.scrollY))-fundsScrollBefore)<10);
    await page.locator('[data-page="highmark"]').click();
    assert.equal(await page.locator('.page:not([hidden])').getAttribute('id'),'page-highmark');
    assert.equal(await page.locator('[data-page="highmark"]').getAttribute('aria-current'),'page');
    assert.equal(await page.evaluate(()=>location.hash),'#page-highmark');
    assert.equal(await page.locator('#topic-highmark-abnormal').count(),1);
    assert.equal(await page.locator('#page-reports #topic-highmark-abnormal').count(),0);
    // Simulate a version refresh from before the topic was moved out of reports.
    // A different query forces a new document, so reading restoration really runs.
    await page.evaluate(()=>sessionStorage.setItem('investment-reading',JSON.stringify({
      page:'reports',scroll:720,filters:[],open:[]
    })));
    const highmarkVersionReady=page.waitForResponse(r=>r.url().endsWith('/api/dashboard-version'));
    await page.goto(`http://127.0.0.1:${server.address().port}/?fixture-migration=highmark#topic-highmark-abnormal`);
    await highmarkVersionReady;
    await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
    assert.equal(await page.locator('.page:not([hidden])').getAttribute('id'),'page-highmark');
    assert.equal(await page.locator('#topic-highmark-abnormal').evaluate(el=>el.closest('.page').id),'page-highmark');
    const migratedReading=await page.locator('#topic-highmark-abnormal').evaluate(el=>({
      scroll:window.scrollY,topicTop:el.getBoundingClientRect().top+window.scrollY
    }));
    assert.ok(Math.abs(migratedReading.scroll-migratedReading.topicTop)<10,
      'A moved legacy topic must keep its anchor position instead of the old reports scroll');
    assert.ok(Math.abs(migratedReading.scroll-720)>100);
    await page.locator('#highmark-other summary').click();
    await page.locator('#highmark-evidence summary').click();
    await page.evaluate(()=>window.scrollTo(0,720));
    const highmarkScrollBefore=await page.evaluate(()=>window.scrollY);
    assert.ok(highmarkScrollBefore>700);
    revision='four';const highmarkReloaded=page.waitForEvent('load');
    await page.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));await highmarkReloaded;
    await page.waitForFunction(()=>Math.abs(window.scrollY-720)<10);
    assert.equal(await page.locator('.page:not([hidden])').getAttribute('id'),'page-highmark');
    assert.notEqual(await page.locator('#highmark-other').getAttribute('open'),null);
    assert.notEqual(await page.locator('#highmark-evidence').getAttribute('open'),null);
    assert.ok(Math.abs((await page.evaluate(()=>window.scrollY))-highmarkScrollBefore)<10);
    await page.locator('[data-page="three-step"]').click();
    assert.equal(await page.locator('.page:not([hidden])').getAttribute('id'),'page-three-step');
    await page.goto(`http://127.0.0.1:${server.address().port}/#strategy-event-timing`);
    assert.equal(await page.locator('.page:not([hidden])').getAttribute('id'),'page-strategy');
    assert.equal(await page.locator('#strategy-event-timing-rules').getAttribute('open'),null);
    await page.locator('#strategy-event-timing-rules summary').click();
    assert.notEqual(await page.locator('#strategy-event-timing-rules').getAttribute('open'),null);
    await page.evaluate(()=>{location.hash='strategy-late-day';});
    assert.equal(await page.locator('.page:not([hidden])').getAttribute('id'),'page-strategy');
    await page.locator('[data-page="etf-monitor"]').click();
    assert.equal(await page.locator('.page:not([hidden])').getAttribute('id'),'page-etf-monitor');
    await page.locator('[data-etf-sort]').selectOption('vol20');
    assert.deepEqual(await page.locator('[data-etf-row]').evaluateAll(rows=>rows.map(row=>row.dataset.etfCode)),['000003','000002','000001']);
    await page.locator('[data-etf-sort]').selectOption('pct5');
    assert.equal(await page.locator('[data-etf-row]').first().getAttribute('data-etf-code'),'000002');
    await page.locator('[data-etf-search]').fill('beta');
    await page.locator('[data-etf-filter]').selectOption('repair');
    assert.equal(await page.locator('[data-etf-row]:not([hidden])').count(),1);
    await page.locator('#etfm-detail-000002 summary').click();
    await page.evaluate(()=>window.scrollTo(0,720));
    revision='five';const etfReloaded=page.waitForEvent('load');
    await page.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));await etfReloaded;
    await page.waitForFunction(()=>Math.abs(window.scrollY-720)<10);
    assert.equal(await page.locator('.page:not([hidden])').getAttribute('id'),'page-etf-monitor');
    assert.equal(await page.locator('[data-etf-search]').inputValue(),'beta');
    assert.equal(await page.locator('[data-etf-filter]').inputValue(),'repair');
    assert.equal(await page.locator('[data-etf-sort]').inputValue(),'pct5');
    assert.notEqual(await page.locator('#etfm-detail-000002').getAttribute('open'),null);
    await page.locator('[data-etf-search]').fill('no-such-fixture');
    assert.equal(await page.locator('[data-etf-empty]').isVisible(),true);
    await page.evaluate(()=>{location.hash='etfm-detail-000001';});
    await page.waitForFunction(()=>!document.querySelector('[data-etf-code="000001"]').hidden);
    assert.notEqual(await page.locator('#etfm-detail-000001').getAttribute('open'),null);
    await page.evaluate(()=>{
      document.querySelector('[data-etf-monitor]').dataset.etfAsof='2000-01-01';
      document.dispatchEvent(new Event('visibilitychange'));
    });
    assert.match(await page.locator('[data-etf-validity]').textContent(),/历史参考/);
    assert.match(await page.locator('[data-etf-quote-status]').textContent(),/时效不足/);
    await page.locator('[data-flow-date]').selectOption('2026-08-13');
    assert.equal(await page.locator('[data-flow-day]:not([hidden])').getAttribute('data-flow-day'),'2026-08-13');
    await page.locator('#fictional-flow-evidence summary').click();
    await page.evaluate(()=>window.scrollTo(0,720));
    revision='six';const flowReloaded=page.waitForEvent('load');
    await page.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));await flowReloaded;
    await page.waitForFunction(()=>Math.abs(window.scrollY-720)<10);
    assert.equal(await page.locator('[data-flow-date]').inputValue(),'2026-08-13');
    assert.notEqual(await page.locator('#fictional-flow-evidence').getAttribute('open'),null);
    await page.evaluate(()=>sessionStorage.setItem('investment-reading',JSON.stringify({
      page:'etf-monitor',flow:'2026-08-13',scroll:720,filters:[],open:[]
    })));
    await page.goto(`http://127.0.0.1:${server.address().port}/?fictional-flow-migration=1#etf-flow-day-2026-08-12`);
    assert.equal(await page.locator('[data-flow-date]').inputValue(),'2026-08-12');
    assert.equal(await page.locator('[data-flow-day]:not([hidden])').getAttribute('data-flow-day'),'2026-08-12');
    assert.deepEqual(errors,[]);
    console.log('Offline browser: twelve tabs, legacy anchors, ETF controls, historical flow dates/deep links and reading restoration passed.');
  } finally {if(browser)await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;});
