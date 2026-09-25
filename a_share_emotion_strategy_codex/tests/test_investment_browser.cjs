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
const pages = ['hot','dragon','yichujifa','prelaunch','late-day','three-step','sector-radar','strategy','reports'];
const panel = `<div data-module="three-step" data-module-until="2099-01-01T15:00:00+08:00">
<div class="research-status"><b>Research</b></div><p class="module-validity">Fictional fixture</p>
<span class="qualified-count">1</span><input data-research-search="three-step">
<select data-research-filter="three-step"><option value="all">all</option><option value="qualified">pass</option><option value="pending">pending</option><option value="other">other</option></select>
<span data-result-count></span><p class="filter-empty" hidden>empty</p><div class="research-group">
<details id="fixture-a" data-candidate data-search="fiction-a" data-bucket="qualified"><summary>A <span class="candidate-state">Pass</span></summary>Fictional only</details>
<details id="fixture-b" data-candidate data-search="fiction-b" data-bucket="pending"><summary>B <span class="candidate-state">Pending</span></summary>Fictional only</details>
</div></div>`;
const rules = '<a href="#strategy-event-timing">Event timing</a><article id="strategy-event-timing"><h2>Fictional research overlay</h2><details id="strategy-event-timing-rules"><summary>Rules and source</summary>Fictional rules only</details></article><article id="strategy-late-day">Fictional existing rules</article>';
const html = '<!doctype html><style>[hidden]{display:none!important}</style>'+pages.map(p=>`<button data-page="${p}">${p}</button>`).join('')+
  pages.map(p=>`<section class="page" id="page-${p}" hidden>${p==='three-step'?panel:p==='strategy'?rules:p}</section>`).join('')+
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
    assert.equal(await page.locator('[data-page]').count(),9);
    await page.locator('[data-page="sector-radar"]').click();
    assert.equal(await page.locator('.page:not([hidden])').getAttribute('id'),'page-sector-radar');
    await page.locator('[data-page="three-step"]').click();
    assert.equal(await page.locator('.page:not([hidden])').getAttribute('id'),'page-three-step');
    await page.goto(`http://127.0.0.1:${server.address().port}/#strategy-event-timing`);
    assert.equal(await page.locator('.page:not([hidden])').getAttribute('id'),'page-strategy');
    assert.equal(await page.locator('#strategy-event-timing-rules').getAttribute('open'),null);
    await page.locator('#strategy-event-timing-rules summary').click();
    assert.notEqual(await page.locator('#strategy-event-timing-rules').getAttribute('open'),null);
    await page.evaluate(()=>{location.hash='strategy-late-day';});
    assert.equal(await page.locator('.page:not([hidden])').getAttribute('id'),'page-strategy');
    assert.deepEqual(errors,[]);
    console.log('Offline browser: nine tabs, rule anchors, filtering, reload restoration and expiry passed.');
  } finally {if(browser)await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;});
