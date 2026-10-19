/* Offline browser regression using Python's fictional report fixture only.
 * Run with npm Playwright or PLAYWRIGHT_MODULE; BROWSER_EXECUTABLE is optional.
 */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {execFileSync} = require('node:child_process');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const project = path.resolve(__dirname, '..');
const content = execFileSync(process.env.PYTHON || 'python3', ['-c', [
  'import sys; sys.path.insert(0, "tests")',
  'from test_night_radar_views import fixture',
  'from night_radar_views import render_page',
  'print(render_page([fixture(), fixture("2026-08-11")]))'
].join('\n')], {cwd:project, encoding:'utf8'});
const css = fs.readFileSync(path.join(project, 'templates/investment.css'), 'utf8');
const js = fs.readFileSync(path.join(project, 'templates/investment.js'), 'utf8');
const pages = ['hot','dragon','yichujifa','prelaunch','late-day','three-step','sector-radar','sector-funds','strategy','reports'];
const html = '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><style>'+css+'</style></head><body>'+
  '<nav class="navigation">'+pages.map(p=>`<button data-page="${p}">${p}</button>`).join('')+'</nav><main>'+
  pages.map(p=>`<section class="page" id="page-${p}" hidden>${p==='reports'?content:'Fictional page'}</section>`).join('')+
  '<div id="update-state"></div></main><script>'+js+'</script></body></html>';
let revision = 'one';
const server = http.createServer((req,res)=>{
  if(req.url==='/api/dashboard-version'){
    res.setHeader('Content-Type','application/json'); res.end(JSON.stringify({revision,updated_at:'fictional'}));
  } else {res.setHeader('Content-Type','text/html; charset=utf-8');res.end(html);}
});
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  let browser;
  try {
    browser = await chromium.launch({headless:true,...(process.env.BROWSER_EXECUTABLE?{executablePath:process.env.BROWSER_EXECUTABLE}:{})});
    const page = await browser.newPage({viewport:{width:1100,height:750}});
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    const base=`http://127.0.0.1:${server.address().port}/`;
    await page.goto(base+'#page-reports');
    await page.waitForFunction(()=>document.querySelector('#update-state').textContent.includes('fictional'));
    assert.equal(await page.locator('.page:not([hidden])').getAttribute('id'),'page-reports');
    assert.equal(await page.locator('[data-night-day]:not([hidden])').getAttribute('data-night-day'),'2026-08-12');
    await page.locator('[data-night-date="2026-08-12"]').focus();
    await page.keyboard.press('ArrowRight');
    assert.equal(await page.locator('[data-night-day]:not([hidden])').getAttribute('data-night-day'),'2026-08-11');
    assert.equal(await page.locator('[data-night-date="2026-08-11"]').getAttribute('aria-selected'),'true');
    await page.locator('#night-20260811-limit_up-sources summary').click();
    await page.evaluate(()=>window.scrollTo(0,800));
    const before = await page.evaluate(()=>window.scrollY);
    revision='two'; const reloaded=page.waitForEvent('load');
    await page.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));await reloaded;
    await page.waitForFunction(()=>Math.abs(window.scrollY-800)<10);
    assert.equal(await page.locator('.page:not([hidden])').getAttribute('id'),'page-reports');
    assert.equal(await page.locator('[data-night-day]:not([hidden])').getAttribute('data-night-day'),'2026-08-11');
    assert.notEqual(await page.locator('#night-20260811-limit_up-sources').getAttribute('open'),null);
    assert.ok(Math.abs((await page.evaluate(()=>window.scrollY))-before)<10);
    await page.goto(base+'#topic-night-radar-20260811');
    assert.equal(await page.locator('[data-night-day]:not([hidden])').getAttribute('data-night-day'),'2026-08-11');
    await page.locator('[data-night-date="2026-08-11"]').focus();await page.keyboard.press('Home');
    assert.equal(await page.locator('[data-night-day]:not([hidden])').getAttribute('data-night-day'),'2026-08-12');
    await page.setViewportSize({width:390,height:844});
    assert.ok(await page.locator('[data-night-day="2026-08-12"] .night-table-scroll').first().evaluate(el=>el.scrollWidth>el.clientWidth));
    assert.equal(await page.locator('[data-page]').count(),10);
    assert.deepEqual(errors,[]);
    console.log('Night radar browser: date keyboard tabs, deep link, reload date/detail/scroll restoration, mobile horizontal tables and ten entries passed.');
  } finally {if(browser)await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;});
