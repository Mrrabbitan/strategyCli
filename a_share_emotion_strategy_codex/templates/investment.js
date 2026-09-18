(() => {
  'use strict';
  const $ = s => document.querySelector(s);
  let activePage = 'hot', busy = false, version = null, timer;
  function switchPage(next) {
    if (!['hot','dragon','yichujifa','prelaunch','late-day','three-step','strategy','reports'].includes(next)) next = 'reports';
    activePage = next;
    document.querySelectorAll('.page').forEach(el => { el.hidden = el.id !== `page-${next}`; });
    document.querySelectorAll('[data-page]').forEach(el => {
      el.classList.toggle('active', el.dataset.page === next);
      if (el.dataset.page === next) el.setAttribute('aria-current', 'page'); else el.removeAttribute('aria-current');
    });
  }
  function locateHash() {
    let id;
    try { id = decodeURIComponent(location.hash.slice(1)); } catch { return; }
    if (['page-plans','page-etf','monitor-live'].includes(id)) id = 'page-reports';
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
  async function get(url) {
    const response = await fetch(url, {cache:'no-store', signal:AbortSignal.timeout(8000)});
    if (!response.ok) throw new Error('unavailable');return response.json();
  }
  function saveReading() {
    const filters = [...document.querySelectorAll('[data-research-search]')].map(el => ({module:el.dataset.researchSearch, search:el.value, filter:document.querySelector(`[data-research-filter="${el.dataset.researchSearch}"]`)?.value || 'all'}));
    sessionStorage.setItem('investment-reading', JSON.stringify({page:activePage, scroll:window.scrollY, filters, open:[...document.querySelectorAll('details[open][id]')].map(x=>x.id)}));
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
    document.querySelectorAll('[data-late-day-until]').forEach(el => {
      const until = Date.parse(el.dataset.lateDayUntil);
      if (Number.isFinite(until) && now > until && !el.dataset.lateDayExpired) {
        el.dataset.lateDayExpired = 'true';
        const status = el.querySelector('.late-day-status');
        if (status && !/尚未执行|执行失败/.test(status.textContent)) status.textContent = '历史记录 · 当前研究通过0只，需重新核验；历史判断不授予当前资格。';
        el.querySelectorAll('.late-day-candidate-state').forEach(row => {if (!row.textContent.startsWith('历史')) row.textContent=`历史判断 · ${row.textContent}；不授予当前资格`;});
      }
    });
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
      const revision = await get('/api/dashboard-version');
      if (version !== null && revision.revision && revision.revision !== version) { saveReading();location.reload();return; }
      version = revision.revision;
      $('#update-state').textContent = revision.updated_at ? `页面更新 ${revision.updated_at}；刷新页面不等于重新抓取选股行情。` : '页面等待首次构建。';
    } catch { $('#update-state').textContent = '页面版本检查不可用，当前内容未更新。'; }
    finally { busy = false; }
  }
  function schedule() { clearInterval(timer);if (!document.hidden) { refresh();timer=setInterval(refresh,30000); } }
  document.addEventListener('visibilitychange',schedule);
  locateHash();
  try {
    const stored = JSON.parse(sessionStorage.getItem('investment-reading') || 'null');
    sessionStorage.removeItem('investment-reading');
    if (stored) {
      switchPage(stored.page);
      const open = new Set(stored.open || []);
      document.querySelectorAll('details[id]').forEach(el => { el.open = open.has(el.id); });
      (stored.filters || []).forEach(f => {
        if (!['hot','dragon','yichujifa','prelaunch','three-step'].includes(f.module)) return;
        const search=document.querySelector(`[data-research-search="${f.module}"]`), filter=document.querySelector(`[data-research-filter="${f.module}"]`);
        if(search)search.value=f.search;if(filter)filter.value=f.filter;filterResearch(f.module);
      });
      requestAnimationFrame(()=>window.scrollTo(0,stored.scroll));
    }
  } catch { /* A stale reading preference never blocks the working surface. */ }
  schedule();
})();
