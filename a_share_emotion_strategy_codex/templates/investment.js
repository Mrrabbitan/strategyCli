(() => {
  'use strict';
  const $ = s => document.querySelector(s);
  const model = JSON.parse($('#dashboard-data').textContent);
  const esc = v => String(v ?? '—').replace(/[&<>"']/g, x => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
  const today = () => new Intl.DateTimeFormat('sv-SE', {timeZone:'Asia/Shanghai'}).format(new Date());
  const signed = v => typeof v === 'number' && Number.isFinite(v) ? `${v > 0 ? '+' : ''}${v.toFixed(2)}` : '未提供';
  const money = v => typeof v === 'number' && Number.isFinite(v) ? `${signed(v/10000)}万元` : '数据不可用';
  let activePage = 'hot', reportIndex = 0, busy = false, version = null, timer;
  function switchPage(next) {
    if (!['hot','dragon','yichujifa','prelaunch','strategy','reports'].includes(next)) next = 'reports';
    activePage = next;
    document.querySelectorAll('.page').forEach(el => { el.hidden = el.id !== `page-${next}`; });
    document.querySelectorAll('[data-page]').forEach(el => {
      el.classList.toggle('active', el.dataset.page === next);
      if (el.dataset.page === next) el.setAttribute('aria-current', 'page'); else el.removeAttribute('aria-current');
    });
    if (next === 'reports' && !$('#report-frame').childElementCount) showReport(reportIndex);
  }
  function locateHash() {
    let id;
    try { id = decodeURIComponent(location.hash.slice(1)); } catch { return; }
    if (['page-plans','page-etf'].includes(id)) id = 'page-reports';
    const target = document.getElementById(id), parent = target?.closest('.page');
    if (!parent) return;
    switchPage(parent.id.slice(5));
    if (target.tagName === 'DETAILS') target.open = true;
    requestAnimationFrame(() => target.scrollIntoView({block:'start'}));
  }
  document.querySelectorAll('[data-page]').forEach(b => b.addEventListener('click', () => {
    history.replaceState(null, '', `#page-${b.dataset.page}`); switchPage(b.dataset.page); window.scrollTo({top:0});
  }));
  window.addEventListener('hashchange', locateHash);
  $('#report-tabs').innerHTML = model.reports.map((x,i) => `<button data-report="${i}" aria-pressed="${i===0}">${esc(x.label)}</button>`).join('');
  function showReport(i) {
    reportIndex = i; const row = model.reports[i]; if (!row) return;
    document.querySelectorAll('[data-report]').forEach(b => b.setAttribute('aria-pressed', String(Number(b.dataset.report)===i)));
    $('#report-state').textContent = `${model.report_date} · ${row.label}${row.url ? ' · 历史记录' : ' · 尚无核验记录'}`;
    $('#report-full').hidden = !row.url;
    if (row.url) {
      $('#report-full').href = row.url;
      $('#report-frame').innerHTML = `<iframe class="report-iframe" sandbox="" title="${esc(row.label)}历史记录" src="${esc(row.url)}"></iframe>`;
    } else $('#report-frame').innerHTML = '<p class="notice">此时点没有通过核验的记录，不使用其他日期填补。</p>';
  }
  document.querySelectorAll('[data-report]').forEach(b => b.addEventListener('click', () => showReport(Number(b.dataset.report))));
  $('#event-date').value = today(); $('#event-date').max = today();
  function renderStatus(d) {
    $('#monitor-health').textContent = d.note;
    $('#monitor-health').classList.toggle('error', ['offline','degraded','disabled'].includes(d.state));
    $('#monitor-clock').textContent = `读取于 ${d.as_of?.slice(11,19) || '未知'}`;
    $('#sector-count').textContent = d.sector_count ? `${d.sector_count} 个板块` : '覆盖未知';
    const q = d.quality || {};
    $('#sector-quality').textContent = `名单 ${d.membership_date || '未提供'} · 新鲜覆盖 ${typeof q.fresh_coverage === 'number' ? (q.fresh_coverage*100).toFixed(1)+'%' : '未知'}`;
    const flow = d.flow || {}, stocks = flow.stocks || [];
    $('#flow-count').textContent = `${stocks.length} 只跟踪`;
    $('#flow-quality').textContent = flow.reason || '资金数据不可用；盘中时效待验收';
    $('#monitor-delivery').textContent = d.delivery_note;
    $('#flow-current').innerHTML = stocks.length ? `<h3>观察股最新资金状态</h3><div class="flow-table"><table><thead><tr><th>股票</th><th>当日累计主力净额</th><th>资金源时间</th><th>状态</th></tr></thead><tbody>${stocks.map(r => `<tr><td>${esc(r.name)} ${esc(r.code)}</td><td>${money(r.main_net_cny)}</td><td>${esc(r.source_asof || r.quote_time || '未提供')}</td><td>${esc(r.reason || (r.main_net_cny < 0 ? '净流出风险' : r.status))}</td></tr>`).join('')}</tbody></table></div>` : '<p class="muted">暂无可用的龙空龙监测名单；不会自动加入议息专题股票。</p>';
  }
  function renderEvents(data, status) {
    const events = data.events || [];
    if (!events.length) {
      const healthy = status?.state === 'healthy';
      $('#monitor-events').innerHTML = `<p class="notice">${esc(data.date)}：${healthy && data.date===today() ? '当前有效覆盖内暂无已记录的新异动。' : '暂无可展示事件；请结合服务状态和覆盖判断，不能据此认定无异动。'}</p>`;return;
    }
    $('#monitor-events').innerHTML = events.map(r => {
      const flow = r.type === 'dragon_fund_outflow', recovery = r.type === 'dragon_fund_recovery';
      const title = flow ? '龙空龙 · 首次观测到资金净流出' : recovery ? '龙空龙 · 累计净額恢复非负' : r.type === 'health' ? r.kind : `板块${r.direction || '价格异动'}`;
      const detail = flow || recovery ? `当日累计主力净额 ${money(r.main_net_cny)}；供应商分类，非真实账户身份。` : r.type === 'sector_movement' ? `近${Number(r.seconds)/60}分钟 ${signed(r.move_pct)}%，实际窗口 ${Number(r.window_seconds).toFixed(0)}秒；不代表此刻仍持续。` : '请同时查看当前服务状态与未覆盖范围。';
      return `<article class="event-card"><h3 class="${recovery ? 'event-recovery' : flow ? 'event-risk' : ''}">${esc(title)}</h3><b>${esc(r.name)} ${esc(r.code || '')}</b><p>${esc(detail)}</p><div class="event-meta">事件行情 ${esc(r.quote_time)} · 首次记录 ${esc(r.observed_at)} · ${esc(r.source || r.provider || '监控服务')}</div></article>`;
    }).join('');
  }
  async function get(url) {
    const response = await fetch(url, {cache:'no-store', signal:AbortSignal.timeout(8000)});
    if (!response.ok) throw new Error('unavailable');return response.json();
  }
  function saveReading() {
    const filters = [...document.querySelectorAll('[data-research-search]')].map(el => ({module:el.dataset.researchSearch, search:el.value, filter:document.querySelector(`[data-research-filter="${el.dataset.researchSearch}"]`)?.value || 'all'}));
    sessionStorage.setItem('investment-reading', JSON.stringify({page:activePage, scroll:window.scrollY, reportIndex, date:$('#event-date').value, filters, open:[...document.querySelectorAll('details[open][id]')].map(x=>x.id)}));
  }
  function filterResearch(module) {
    const panel = document.querySelector(`[data-module="${module}"]`);if (!panel) return;
    const query = panel.querySelector('[data-research-search]').value.trim().toLowerCase();
    const bucket = panel.querySelector('[data-research-filter]').value;
    let count = 0;
    panel.querySelectorAll('[data-candidate]').forEach(el => {
      el.hidden = !el.dataset.search.toLowerCase().includes(query) || (bucket !== 'all' && el.dataset.bucket !== bucket);
      if (!el.hidden) count++;
    });
    panel.querySelectorAll('.research-group').forEach(el => {el.hidden = ![...el.querySelectorAll('[data-candidate]')].some(card => !card.hidden);});
    panel.querySelector('[data-result-count]').textContent = `${count} 条匹配记录`;
    panel.querySelector('.filter-empty').hidden = count > 0 || (!query && bucket === 'all');
  }
  document.querySelectorAll('[data-research-search]').forEach(el => el.addEventListener('input',()=>filterResearch(el.dataset.researchSearch)));
  document.querySelectorAll('[data-research-filter]').forEach(el => el.addEventListener('change',()=>filterResearch(el.dataset.researchFilter)));
  function expireResearch() {
    const now = Date.now();
    document.querySelectorAll('[data-context-until]').forEach(el => {
      if (now > Date.parse(el.dataset.contextUntil)) { el.textContent = '历史盘中背景 · 需重新取得报价';el.removeAttribute('data-context-until'); }
    });
    document.querySelectorAll('[data-module]').forEach(panel => {
      const until = Date.parse(panel.dataset.moduleUntil);
      if (Number.isFinite(until) && now > until && !panel.dataset.expired) {
        panel.dataset.expired = 'true';
        panel.querySelector('.research-status b').textContent = '历史研究';
        panel.querySelector('.module-validity').textContent = '观察窗口已结束；以下只作历史研究，须重新执行策略核验。';
        panel.querySelector('.qualified-count').textContent = '0 条';
        panel.querySelectorAll('.candidate-state').forEach(el=>{el.textContent='历史记录 · 不取得当前资格';el.classList.remove('positive');});
        panel.querySelectorAll('[data-candidate]').forEach(el=>{el.dataset.bucket='other';});
        filterResearch(panel.dataset.module);
      }
      panel.querySelectorAll('[data-live-until]').forEach(el=>{
        if (Number.isFinite(Date.parse(el.dataset.liveUntil)) && now > Date.parse(el.dataset.liveUntil)) {
          el.textContent=el.dataset.signalKind==='prelaunch'?'原观察期限已结束 · 需重新筛选':'历史盘中复核 · 当前需重新确认';el.classList.remove('positive');
          el.removeAttribute('data-live-until');
          const remaining = panel.querySelectorAll('.candidate-state.positive').length;
          panel.querySelector('.qualified-count').textContent = `${remaining} 条`;
        }
      });
    });
  }
  async function refresh() {
    if (busy || document.hidden) return;
    expireResearch();
    busy = true;
    try {
      const [status, events] = await Promise.all([get('/api/monitor-status'),get(`/api/monitor-events?date=${encodeURIComponent($('#event-date').value || today())}`)]);
      renderStatus(status);renderEvents(events,status);
    } catch {
      $('#monitor-health').textContent = '本机监控接口离线；以下保留最后读到的事件，当前覆盖未知。';
      $('#monitor-health').classList.add('error');
      $('#flow-quality').textContent = '连接中断，以下数值不是当前有效资金状态';
    }
    try {
      const revision = await get('/api/dashboard-version');
      if (version !== null && revision.revision && revision.revision !== version) { saveReading();location.reload();return; }
      version = revision.revision;
      $('#update-state').textContent = revision.updated_at ? `页面更新 ${revision.updated_at}；刷新页面不等于重新抓取选股行情。` : '页面等待首次构建。';
    } catch { $('#update-state').textContent = '页面版本检查不可用，当前内容未更新。'; }
    finally { busy = false; }
  }
  $('#monitor-refresh').addEventListener('click',refresh);$('#event-date').addEventListener('change',refresh);
  function schedule() { clearInterval(timer);if (!document.hidden) { refresh();timer=setInterval(refresh,30000); } }
  document.addEventListener('visibilitychange',schedule);
  locateHash();
  try {
    const stored = JSON.parse(sessionStorage.getItem('investment-reading') || 'null');
    sessionStorage.removeItem('investment-reading');
    if (stored) {
      switchPage(stored.page);showReport(stored.reportIndex);$('#event-date').value=stored.date;
      const open = new Set(stored.open || []);
      document.querySelectorAll('details[id]').forEach(el => { el.open = open.has(el.id); });
      (stored.filters || []).forEach(f => {
        if (!['hot','dragon','yichujifa','prelaunch'].includes(f.module)) return;
        const search=document.querySelector(`[data-research-search="${f.module}"]`), filter=document.querySelector(`[data-research-filter="${f.module}"]`);
        if(search)search.value=f.search;if(filter)filter.value=f.filter;filterResearch(f.module);
      });
      requestAnimationFrame(()=>window.scrollTo(0,stored.scroll));
    }
  } catch { /* A stale reading preference never blocks the working surface. */ }
  schedule();
})();
