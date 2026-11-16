// MyVector web UI: state, daemon API, live event stream, actions, routing.
import { html, raw, icon, count, hours } from './util.js';
import { view, findProject } from './views.js';
import * as tracker from './tracker.js';
import * as monitor from './monitor.js';
import * as spend from './spend.js';

const csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
const THEME_KEY = 'myvec-theme';
const EVENT_CAP = 1000;

const S = {
  route: { name: 'overview' },
  theme: document.documentElement.getAttribute('data-theme') || 'light',
  status: null,          // last good /api/status
  statusErr: null,       // {status?, message} while the daemon is unreachable / failing
  events: [],            // journal events, ascending by id
  live: { state: 'connecting' },
  filter: { type: 'all', project: '', q: '', limit: 50 },
  collapsed: new Set(),  // overview: collapsed project rows
  expanded: new Set(),   // project page: repos with open branch tables
  detail: {},            // project path -> /detail
  searches: {},          // project path -> /searches
  mem: { scope: '', q: '', archived: false, limit: 50, items: null, err: null, selectedId: null, sel: null },
  tk: { summary: null, summaryErr: null, tree: {}, treeErr: {}, problems: {}, problemsErr: {}, card: {}, filter: 'all', collapsed: {}, waiting: [], node: '', cm: null, sessions: {}, sessionsErr: {}, replies: {}, busy: {}, inbox: {}, draft: {} },   // tracker (E1-06); inbox/draft (E3-01): '<path>\n<id>' -> {items, done} / {text, status}
  mon: { load: null, series: null, current: null, off: false, err: null, period: '24h', alertPct: null, alertMinutes: null, summary: {}, tests: {} },   // load + tests (E6-05)
  usage: {},                          // search adoption (E8-06): days -> /api/search/usage payload | {err}
  spend: { project: {}, card: {} },   // token spend (E7-03): '<path>\n<days>' -> report, '<path>\n<id>' -> card payload
  search: { q: '', scope: 'project', project: null, loading: false, result: null, err: null, last: null },
  busy: new Set(),
  toasts: [],
};

// ------------------------------------------------------------------------------------------------ api
async function api(path, { method = 'GET', body } = {}) {
  const headers = {};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (method !== 'GET') headers['X-CSRF-Token'] = csrf;
  const r = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  let data = null;
  try { data = await r.json(); } catch { /* not json */ }
  if (!r.ok) {
    const e = new Error((data && (data.error || data.detail || data.message)) || `HTTP ${r.status}`);
    e.status = r.status;
    throw e;
  }
  return data;
}
const ppath = (p) => p.split('/').map(encodeURIComponent).join('/');
const qs = (o) => Object.entries(o).filter(([, v]) => v !== '' && v != null).map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join('&');

// ------------------------------------------------------------------------------------------------ render
let timer = 0, lastHTML = '', selectOpen = false;
const appEl = document.getElementById('app');
function render() {
  if (!timer) timer = setTimeout(doRender, 0);
}
function doRender() {
  timer = 0;
  if (selectOpen) { timer = setTimeout(doRender, 400); return; }   // re-rendering would close the native dropdown
  const out = view(S).toString();
  if (out === lastHTML) return;   // chart placeholders carry data-v, so new chart data changes the html
  const ae = document.activeElement;
  const key = ae && ae.dataset ? ae.dataset.fk : null;
  let sel = null;
  try { if (key && ae.selectionStart != null) sel = [ae.selectionStart, ae.selectionEnd]; } catch { /* input type without selection */ }
  appEl.innerHTML = out;
  lastHTML = out;
  drawCharts();
  if (key) {
    const el = appEl.querySelector(`[data-fk="${CSS.escape(key)}"]`);
    if (el) {
      el.focus({ preventScroll: true });
      try { if (sel) el.setSelectionRange(sel[0], sel[1]); } catch { /* ignore */ }
    }
  }
}

// ------------------------------------------------------------------------------------------------ toasts
let toastId = 0;
const toastsEl = document.getElementById('toasts');
function renderToasts() {
  toastsEl.innerHTML = S.toasts.map((t) => html`<div class="toast ${t.kind === 'error' ? 't-danger' : ''}" ${t.kind === 'error' ? raw('role="alert"') : ''}>
    ${t.kind === 'error' ? icon('alert', 15, 1.8) : icon('check', 15, 2)}
    <span class="grow">${t.body}</span>${t.action ? html`<button type="button" class="toast-btn" data-act="toastAction" data-v="${t.id}">${t.action.label}</button>` : ''}</div>`).map(String).join('');
}
function toast(kind, body, action) {
  const t = { id: ++toastId, kind, body, action };
  S.toasts.push(t);
  if (S.toasts.length > 4) S.toasts.shift();
  renderToasts();
  setTimeout(() => dismissToast(t.id), kind === 'error' ? 10000 : 6000);
}
function dismissToast(id) {
  S.toasts = S.toasts.filter((t) => t.id !== id);
  renderToasts();
}
const mono = (t) => html`<span class="mono">${t}</span>`;

// ------------------------------------------------------------------------------------------------ data loading
let statusTimer = 0;
async function loadStatus() {
  try {
    const wasDown = !!S.statusErr;
    S.status = await api('/api/status');
    S.statusErr = null;
    if (wasDown) { startStream(); loadEventsBacklog(); }
    if (!S.search.project) pickSearchProject();
  } catch (e) {
    S.statusErr = { status: e.status, message: e.message };
  }
  render();
}
function scheduleStatus() {
  clearTimeout(statusTimer);
  statusTimer = setTimeout(loadStatus, 250);
}
function pickSearchProject() {
  const all = flat(S.status.projects);
  const saved = localStorage.getItem('myvec-search-project');
  const p = all.find((x) => x.path === saved) || all.find((x) => x.active) || all[0];
  if (p) S.search.project = p.path;
}
const flat = (list, out = []) => { for (const p of list) { out.push(p); flat(p.children, out); } return out; };

async function loadDetail(path) {
  try { S.detail[path] = await api(`/api/projects/${ppath(path)}/detail`); } catch { /* shown from status only */ }
  render();
}
async function loadSearches(path) {
  try { S.searches[path] = await api(`/api/projects/${ppath(path)}/searches?limit=8`); } catch { /* keep old */ }
  render();
}
async function loadMemories() {
  const M = S.mem;
  try {
    const d = await api('/api/memories?' + qs({ scope: M.scope, q: M.q.trim(), include_archived: M.archived ? '1' : '', limit: M.limit }));
    M.items = d.memories;
    M.err = null;
    if (!M.selectedId && M.items.length) M.selectedId = M.items[0].id;
    await ensureSelectedMemory();
  } catch (e) {
    M.err = e.message;
    if (M.items == null) M.items = [];
  }
  render();
}
async function ensureSelectedMemory() {
  const M = S.mem;
  if (!M.selectedId || (M.items && M.items.some((m) => m.id === M.selectedId))) { M.sel = null; return; }
  if (M.sel && M.sel.id === M.selectedId) return;
  try { M.sel = (await api('/api/memories/' + encodeURIComponent(M.selectedId))).memory; } catch { M.sel = null; }
}

// node load and test runs (E6-05)
let loadingNow = false;
async function loadNow() {
  if (loadingNow) return;
  loadingNow = true;
  try { await loadNowOnce(); } finally { loadingNow = false; }
}
async function loadNowOnce() {
  const M = S.mon;
  try {
    M.load = await api('/api/load');
    if (M.load.alert_rule) { M.alertPct = M.load.alert_rule.pct; M.alertMinutes = M.load.alert_rule.minutes; }
    M.off = false;
    M.err = null;
  } catch (e) {
    if (e.status === 503) { M.off = true; M.err = null; } else M.err = e.message;
  }
  if (!M.off) {
    try { M.current = await api('/api/tests/current'); } catch { /* keep the old blocks */ }
  }
  render();
}
async function loadSeries() {
  const M = S.mon, period = M.period;
  try {
    const d = await api('/api/load/series?period=' + period);
    if (period === M.period) M.series = d;
  } catch (e) {
    if (e.status === 503) M.off = true;
  }
  render();
}
async function loadTests(path) {
  const M = S.mon;
  try {
    const d = await api(`/api/tests/runs?${qs({ project: path, limit: 50 })}`);
    M.tests[path] = { runs: d.runs, summary: d.summary, rule: d.regression_rule };
    M.summary[path] = d.summary;
  } catch (e) {
    M.tests[path] = { err: e.status === 404 ? 'Проєкт не знайдено.' : e.message };
  }
  if (!M.load) {
    try {
      M.load = await api('/api/load');
      if (M.load.alert_rule) { M.alertPct = M.load.alert_rule.pct; M.alertMinutes = M.load.alert_rule.minutes; }
    } catch { /* host cores are optional */ }
  }
  render();
}
async function loadTestSummary(path) {
  try { S.mon.summary[path] = (await api(`/api/tests/runs?${qs({ project: path, limit: 1 })}`)).summary; } catch { /* no tab counter */ }
  render();
}
const testsRunning = (path) => ((S.mon.tests[path] || {}).runs || []).some((r) => r.outcome === 'running');

// token spend (E7-03, ADR-016): one report per project and period, one block per opened ticket card
async function loadSpend(path, days) {
  const key = path + '\n' + days;
  try {
    S.spend.project[key] = await api(`/api/tokens/project?${qs({ project: path, days })}`);
  } catch (e) {
    if (!(S.spend.project[key] && !S.spend.project[key].err)) S.spend.project[key] = { err: e.status === 404 ? 'Проєкт не знайдено.' : e.message };
  }
  render();
}
// search adoption card (E8-06, ADR-037): one payload per period; a failed refresh keeps the old numbers
async function loadUsage(days) {
  try {
    S.usage[days] = await api(`/api/search/usage?${qs({ days })}`);
  } catch (e) {
    if (!(S.usage[days] && !S.usage[days].err)) S.usage[days] = { err: e.message };
  }
  render();
}
async function loadSpendCard(path, id) {
  try {
    S.spend.card[path + '\n' + id] = await api(`/api/tokens/ticket?${qs({ project: path, id })}`);
  } catch { /* the card simply has no spend block */ }
  render();
}

// charts: inline svg drawn to the measured width (monitor.js is pure; measuring and listeners are here)
const charts = new Map();   // data-chart name -> {el, geom, tip}
const hover = {};           // data-chart name -> hovered index, survives a redraw
let ctip = null, hoverName = null, chartW = 0;
function tipEl() {
  if (!ctip) {
    ctip = document.createElement('div');
    ctip.id = 'ctip';
    ctip.className = 'ctip';
    ctip.setAttribute('role', 'tooltip');
    ctip.hidden = true;
    document.body.appendChild(ctip);
  }
  return ctip;
}
function showTip(htmlStr, x, y) {
  const t = tipEl();
  t.innerHTML = htmlStr;
  t.hidden = false;
  const w = t.offsetWidth;
  t.style.left = Math.min(window.innerWidth - w - 8, Math.max(8, x - w / 2)) + 'px';
  t.style.top = Math.max(4, y - t.offsetHeight - 12) + 'px';
}
function clearHover() {
  for (const k of Object.keys(hover)) delete hover[k];
  hoverName = null;
  tipEl().hidden = true;
  appEl.querySelectorAll('[data-chart] .hov').forEach((g) => { g.style.display = 'none'; });
}
function hoverAt(name, i) {
  const c = charts.get(name), svg = c && c.el.firstElementChild;
  if (!svg || i < 0) return;
  const g = svg.querySelector('.hov'), { x, y } = c.geom.at(i);
  hover[name] = i;
  hoverName = name;
  g.querySelector('line').setAttribute('x1', x);
  g.querySelector('line').setAttribute('x2', x);
  g.querySelector('circle').setAttribute('cx', x);
  g.querySelector('circle').setAttribute('cy', y);
  g.style.display = '';
  const r = svg.getBoundingClientRect();
  showTip(c.tip(i), r.left + x, r.top + y);
}
function drawCharts() {
  charts.clear();
  const M = S.mon, path = S.route.path;
  appEl.querySelectorAll('[data-chart]').forEach((el) => {
    const w = el.clientWidth, h = el.clientHeight, name = el.dataset.chart;
    if (!w || !h) return;
    const r = name === 'tok'
      ? spend.chart(name, S.spend.project[path + '\n' + (S.route.days || 14)] || {}, {}, { w, h })
      : monitor.chart(name, { series: M.series, runs: ((M.tests[path] || {}).runs) }, { period: M.period, alertPct: M.alertPct ?? undefined }, { w, h });
    el.innerHTML = r.svg;
    charts.set(name, { el, geom: r.geom, tip: r.tip });
    if (hover[name] != null) hoverAt(name, Math.min(hover[name], r.geom.n - 1));
  });
  if (hoverName && !charts.has(hoverName)) clearHover();
  chartW = appEl.clientWidth;
}
function onChartPointer(e) {
  const el = e.target.closest ? e.target.closest('[data-chart]') : null;
  if (!el) { if (hoverName) clearHover(); return; }
  const name = el.dataset.chart, c = charts.get(name), svg = el.firstElementChild;
  if (!c || !svg) return;
  if (hoverName && hoverName !== name) clearHover();
  hoverAt(name, monitor.hitIndex(e.clientX - svg.getBoundingClientRect().left, c.geom));
}
document.addEventListener('pointermove', onChartPointer);
document.addEventListener('pointerdown', onChartPointer);
let resizeTimer = 0;
if (typeof ResizeObserver !== 'undefined') {
  new ResizeObserver(() => {
    if (appEl.clientWidth === chartW) return;
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(drawCharts, 120);
  }).observe(appEl);
}

// tracker data (E1-06): the summary of all projects, one project's tree, one open ticket card
async function loadTicketSummary() {
  try { S.tk.summary = (await api('/api/tickets')).projects; S.tk.summaryErr = null; } catch (e) { S.tk.summaryErr = e.message; }
  render();
}
async function loadWaiting() {
  try { const d = await api('/api/sessions/waiting'); S.tk.waiting = d.sessions; S.tk.node = d.node; S.tk.cm = d.cm; } catch { /* keep the old list */ }
  render();
}
async function loadSessions(path) {
  try { const d = await api('/api/sessions?all=1&project=' + encodeURIComponent(path)); S.tk.sessions[path] = d; S.tk.node = d.node; S.tk.cm = d.cm; delete S.tk.sessionsErr[path]; } catch (e) { S.tk.sessionsErr[path] = e.message; }
  render();
}
async function loadTicketTree(path) {
  try { S.tk.tree[path] = await api(`/api/tickets/${ppath(path)}/tree`); delete S.tk.treeErr[path]; } catch (e) {
    S.tk.treeErr[path] = e.status === 404 ? 'У проєкту нема теки тікетів.' : e.message;
  }
  render();
}
async function loadProblems(path) {
  try { S.tk.problems[path] = await api(`/api/tickets/${ppath(path)}/problems`); delete S.tk.problemsErr[path]; } catch (e) {
    S.tk.problemsErr[path] = e.status === 404 ? 'У проєкту нема теки тікетів.' : e.message;
  }
  render();
}
async function loadTicketCard(path, id) {
  let data = null, err = null;
  try { data = await api(`/api/tickets/${ppath(path)}/ticket/${encodeURIComponent(id)}`); } catch (e) { err = e.message || 'помилка'; }
  if (S.route.t !== id) return;
  if (err) S.tk.card[path] = { error: err, id }; else S.tk.card[path] = data;
  render();
}
// inbox (E3-01): files still in docs/todo/inbox are "надіслано"; a vanished file is "оброблено" and the card is re-read
const inboxKey = (path, id) => path + '\n' + id;
async function loadInbox(path, id) {
  let d;
  try { d = await api(`/api/tickets/${ppath(path)}/ticket/${encodeURIComponent(id)}/inbox`); } catch { return; }
  if (S.route.t !== id) return;
  const k = inboxKey(path, id);
  const prev = S.tk.inbox[k] || { items: [], done: [] };
  const now = new Set(d.items.map((x) => x.file));
  const gone = prev.items.filter((x) => !now.has(x.file));
  S.tk.inbox[k] = { items: d.items, done: prev.done.concat(gone) };
  if (gone.length) loadTicketCard(path, id);
  render();
}
const collapseKey = (path) => 'myvec-tk-collapsed:' + path;
function ensureCollapsed(path) {
  if (S.tk.collapsed[path]) return;
  let ids = [];
  try { ids = JSON.parse(localStorage.getItem(collapseKey(path)) || '[]'); } catch { /* private mode / bad json */ }
  S.tk.collapsed[path] = new Set(Array.isArray(ids) ? ids : []);
}
const tkPending = new Set();
let tkTimer = 0;
function scheduleTickets(project) {
  tkPending.add(project);
  clearTimeout(tkTimer);
  tkTimer = setTimeout(() => {
    const r = S.route, hit = [...tkPending];
    tkPending.clear();
    if (r.name === 'tickets' || r.name === 'project') loadTicketSummary();
    if (r.name === 'tickets') loadWaiting();
    if (r.name === 'project' && r.tab === 'problems' && hit.includes(r.path)) loadProblems(r.path);
    if (r.name === 'project' && r.tab === 'tickets' && hit.includes(r.path)) {
      loadTicketTree(r.path);
      if (r.t) { loadTicketCard(r.path, r.t); loadSpendCard(r.path, r.t); loadInbox(r.path, r.t); }
    }
  }, 300);
}

let sessTimer = 0;
function scheduleSessions() {
  clearTimeout(sessTimer);
  sessTimer = setTimeout(() => {
    const r = S.route;
    if (r.name === 'tickets') { loadWaiting(); loadTicketSummary(); }
    if (r.name === 'project') {
      loadSessions(r.path);
      if (r.tab === 'tickets') { loadTicketTree(r.path); if (r.t) loadTicketCard(r.path, r.t); }
    }
  }, 500);
}

function ingest(evs, live) {
  const last = S.events.length ? S.events[S.events.length - 1].id : 0;
  const fresh = evs.filter((e) => e.id > last).sort((a, b) => a.id - b.id);
  if (!fresh.length) return;
  S.events.push(...fresh);
  if (S.events.length > EVENT_CAP) S.events.splice(0, S.events.length - EVENT_CAP);
  if (live) fresh.forEach(onLiveEvent);
  render();
}
async function loadEventsBacklog() {
  try { ingest((await api('/api/events/recent?limit=500')).events, false); } catch { /* the stream will fill it */ }
}

const pendingSyncs = new Map();   // project -> true: syncs started from this page (toast when they end)
function onLiveEvent(ev) {
  const t = ev.type;
  if (t.startsWith('sync_') || t.startsWith('watcher_') || t === 'mode_changed' || t === 'project_activated' || t === 'memory_action') {
    if (t !== 'sync_progress') scheduleStatus(); else { applyProgress(ev); }
  }
  if (t === 'tickets_updated') scheduleTickets(ev.project);
  if (t === 'session_changed' || t === 'session_action') scheduleSessions();
  const r = S.route;
  if (r.name === 'project' && (ev.project === r.path || (ev.project || '').startsWith(r.path + '/'))) {
    if (t === 'sync_finished' || t === 'mode_changed' || t.startsWith('watcher_')) loadDetail(r.path);
    if (t === 'search') loadSearches(r.path);
  }
  if (r.name === 'memory' && t === 'memory_action') loadMemories();
  if (pendingSyncs.has(ev.project)) {
    if (t === 'sync_finished') {
      pendingSyncs.delete(ev.project);
      toast('success', html`Синк ${mono(ev.project)} завершено — ${count(ev.chunks_embedded || 0, ['шматок', 'шматки', 'шматків'])}`, { label: 'Журнал', fn: () => { location.hash = '#/journal'; } });
    } else if (t === 'sync_cancelled') {
      pendingSyncs.delete(ev.project);
    } else if (t === 'sync_failed') {
      pendingSyncs.delete(ev.project);
      toast('error', html`Синк ${mono(ev.project)} не вдався — ${ev.error || 'невідома помилка'}`, { label: 'Повторити', fn: () => A.sync({ dataset: { p: ev.project } }) });
    }
  }
}
function applyProgress(ev) {
  const p = S.status && flat(S.status.projects).find((x) => x.path === ev.project);
  if (p) { p.syncing = true; p.progress = { files: ev.files, blobs_done: ev.blobs_done, blobs_total: ev.blobs_total, chunks_embedded: ev.chunks_embedded, percent: ev.percent }; }
}

// ------------------------------------------------------------------------------------------------ live stream (SSE over fetch, with backoff)
let streamGen = 0, streamCtl = null;
function stopStream() {
  streamGen++;
  if (streamCtl) streamCtl.abort();
  streamCtl = null;
}
async function startStream() {
  stopStream();
  const gen = streamGen;
  let delay = 1000;
  S.live.state = 'connecting';
  render();
  while (gen === streamGen) {
    const ctl = (streamCtl = new AbortController());
    try {
      const last = S.events.length ? S.events[S.events.length - 1].id : 0;
      const r = await fetch('/api/events?since=' + last, { signal: ctl.signal, headers: { Accept: 'text/event-stream' } });
      if (!r.ok || !r.body) throw new Error('HTTP ' + r.status);
      S.live.state = 'live';
      delay = 1000;
      render();
      const reader = r.body.getReader(), dec = new TextDecoder();
      let buf = '';
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf('\n\n')) >= 0) {
          const block = buf.slice(0, i);
          buf = buf.slice(i + 2);
          const data = block.split('\n').filter((l) => l.startsWith('data:')).map((l) => l.slice(5).trimStart()).join('\n');
          if (!data) continue;
          try { ingest([JSON.parse(data)], true); } catch { /* malformed block */ }
        }
      }
    } catch (e) {
      if (gen !== streamGen) return;
    }
    if (gen !== streamGen) return;
    S.live.state = 'offline';
    render();
    await new Promise((res) => setTimeout(res, delay));
    delay = Math.min(delay * 2, 30000);
  }
}
function togglePause() {
  if (S.live.state === 'paused') { startStream(); return; }
  stopStream();
  S.live.state = 'paused';
  render();
}

// ------------------------------------------------------------------------------------------------ actions
async function copyText(text, msg = 'Скопійовано') {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;opacity:0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); } catch { /* nothing else to try */ }
    ta.remove();
  }
  toast('success', msg);
}
const MODE_NAMES = { auto: 'Авто', always: 'Завжди', off: 'Вимк' };

async function guarded(key, fn) {
  if (S.busy.has(key)) return;
  S.busy.add(key);
  render();
  try { await fn(); } finally { S.busy.delete(key); render(); }
}

const A = {
  theme() {
    S.theme = S.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', S.theme);
    try { localStorage.setItem(THEME_KEY, S.theme); } catch { /* private mode */ }
    render();
  },
  retry() { loadStatus(); },
  loadretry() { loadNow(); loadSeries(); },
  usageretry() { if (S.route.name === 'search') loadUsage(S.route.days || 14); },
  tokensretry() { if (S.route.name === 'project') loadSpend(S.route.path, S.route.days || 14); },
  testsretry() { if (S.route.name === 'project') loadTests(S.route.path); },
  copy(el) { copyText(el.dataset.copy, el.dataset.msg); },
  collapse(el) {
    const p = el.dataset.p;
    if (!S.collapsed.delete(p)) S.collapsed.add(p);
    render();
  },
  repo(el) {
    const r = el.dataset.v;
    if (!S.expanded.delete(r)) S.expanded.add(r);
    render();
  },
  async mode(el) {
    const { p, v } = el.dataset;
    const cur = findProject(S, p);
    if (cur && cur.mode === v) return;
    try {
      await api(`/api/projects/${ppath(p)}/mode`, { method: 'POST', body: { mode: v } });
      toast('success', html`Режим ${mono(p)}: ${MODE_NAMES[v]}`);
    } catch (e) { toast('error', html`Не вдалося змінити режим ${mono(p)} — ${e.message}`); }
    loadStatus();
  },
  async sync(el) {
    const p = el.dataset.p;
    await guarded(`sync:${p}`, async () => {
      try {
        const d = await api(`/api/projects/${ppath(p)}/sync`, { method: 'POST', body: {} });
        if (d.queued) { pendingSyncs.set(p, true); toast('success', html`Синк ${mono(p)} поставлено в чергу`); } else toast('success', html`Синк ${mono(p)} уже триває або в черзі`);
      } catch (e) { toast('error', html`Не вдалося запустити синк ${mono(p)} — ${e.message}`); }
    });
    loadStatus();
  },
  async syncActive() {
    const act = flat(S.status.projects).filter((p) => p.active && !p.syncing);
    if (!act.length) { toast('success', 'Немає активних проєктів без синку'); return; }
    await guarded('sync-active', async () => {
      const done = [];
      for (const p of act) {
        try { if ((await api(`/api/projects/${ppath(p.path)}/sync`, { method: 'POST', body: {} })).queued) { pendingSyncs.set(p.path, true); done.push(p.path); } } catch (e) { toast('error', html`Не вдалося запустити синк ${mono(p.path)} — ${e.message}`); }
      }
      if (done.length) toast('success', html`Синк поставлено в чергу: ${done.map((x, i) => html`${i ? ', ' : ''}${mono(x)}`)}`);
    });
    loadStatus();
  },
  filter(el) { S.filter.type = el.dataset.v; S.filter.limit = 50; render(); },
  more() { S.filter.limit += 50; render(); },
  pause() { togglePause(); },
  export() {
    const lines = S.events.filter((e) => e.type !== 'sync_progress').map((e) => JSON.stringify(e)).join('\n') + '\n';
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([lines], { type: 'application/x-ndjson' }));
    a.download = 'myvec-events.jsonl';
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  },
  toastAction(el) {
    const t = S.toasts.find((x) => x.id === Number(el.dataset.v));
    if (t && t.action) t.action.fn();
    if (t) dismissToast(t.id);
  },
  // tickets
  tkfilter(el) { S.tk.filter = el.dataset.v; render(); },
  tkcollapse(el) {
    const path = S.route.path, id = el.dataset.id;
    ensureCollapsed(path);
    const set = S.tk.collapsed[path];
    if (!set.delete(id)) set.add(id);
    try { localStorage.setItem(collapseKey(path), JSON.stringify([...set])); } catch { /* private mode */ }
    render();
  },
  tkrow(el) { location.hash = el.dataset.href; },
  async sesscopy(el) {
    const d = el.dataset;
    const reply = (document.getElementById('mv-reply-' + d.sid) || {}).value || S.tk.replies[d.sid] || '';
    const text = tracker.copyTexts(d.sid, d.project, d.ticket, reply)[d.kind];
    if (!text) return;
    await copyText(text, `Скопійовано: ${text.length > 60 ? text.slice(0, 57) + '…' : text}`);
  },
  async inboxsend(el) {
    const path = el.dataset.project, id = el.dataset.ticket, k = inboxKey(path, id), bk = 'inbox:' + k;
    if (S.tk.busy[bk]) return;
    const ta = document.getElementById('mv-new'), st = document.getElementById('mv-st');
    const text = ta ? ta.value : ((S.tk.draft[k] || {}).text || '');
    const status = st ? st.value : ((S.tk.draft[k] || {}).status || '');
    if (!text.trim() && !status) { toast('error', 'Напиши коментар або обери статус'); return; }
    S.tk.busy[bk] = true;
    render();
    try {
      const d = await api(`/api/tickets/${ppath(path)}/ticket/${encodeURIComponent(id)}/inbox`, { method: 'POST', body: { text, status: status || undefined } });
      const cur = S.tk.inbox[k] || { items: [], done: [] };
      S.tk.inbox[k] = { items: cur.items.concat(d.item), done: cur.done };
      delete S.tk.draft[k];
      toast('success', 'Коментар записано в docs/todo/inbox');
    } catch (e) {
      toast('error', html`Не вдалося надіслати: ${e.message}`);
    } finally {
      delete S.tk.busy[bk];
      render();
    }
  },
  async sessresume(el) {
    const sid = el.dataset.sid;
    if (S.tk.busy[sid]) return;
    const ta = document.getElementById('mv-reply-' + sid);
    const text = ta ? ta.value : (S.tk.replies[sid] || '');
    S.tk.busy[sid] = true;
    render();
    try {
      const d = await api(`/api/sessions/${encodeURIComponent(sid)}/resume`, { method: 'POST', body: el.dataset.qat ? { text, question_at: Number(el.dataset.qat) } : { text } });
      delete S.tk.replies[sid];
      toast('success', d.message || 'Готово');
    } catch (e) {
      toast('error', html`Не вдалося: ${e.message}`);
    } finally {
      delete S.tk.busy[sid];
      scheduleSessions();
      render();
    }
  },
  async sessoption(el) {
    const sid = el.dataset.sid;
    if (S.tk.busy[sid]) return;
    S.tk.busy[sid] = true;
    render();
    try {
      const d = await api(`/api/sessions/${encodeURIComponent(sid)}/resume`, { method: 'POST', body: { option: Number(el.dataset.n), label: el.dataset.label, question_at: Number(el.dataset.qat) } });
      delete S.tk.replies[sid];
      toast('success', d.message || 'Готово');
    } catch (e) {
      toast('error', html`Не вдалося: ${e.message}`);
    } finally {
      delete S.tk.busy[sid];
      scheduleSessions();
      render();
    }
  },
  // memory
  memscope(el) { S.mem.scope = el.dataset.v; S.mem.limit = 50; S.mem.selectedId = null; S.mem.items = null; render(); loadMemories(); },
  memarch() { S.mem.archived = !S.mem.archived; S.mem.items = null; render(); loadMemories(); },
  memmore() { S.mem.limit += 50; loadMemories(); },
  memsel(el) { location.hash = '#/memory/' + el.dataset.v; },
  pin(el) { memAction(el.dataset.v, 'pin', 'закріплено'); },
  unpin(el) { memAction(el.dataset.v, 'unpin', 'відкріплено'); },
  archive(el) { memAction(el.dataset.v, 'archive', 'переміщено в архів'); },
  promote(el) { memAction(el.dataset.v, 'promote', 'піднято в глобальну'); },
  // search
  sscope(el) { S.search.scope = el.dataset.v; render(); },
  rerun(el) { S.search.q = el.dataset.v; runSearch(); },
  async copyText() {
    const Q = S.search, l = Q.last;
    if (!l) return;
    try {
      const d = await api('/api/cli/search', { method: 'POST', body: { q: l.q, cwd: l.cwd, scope: l.apiScope, kind: l.kind, mode: 'auto' } });
      if (d.exit_code) throw new Error(d.stderr || 'помилка пошуку');
      await copyText(d.stdout, 'Видачу скопійовано як текст');
    } catch (e) { toast('error', html`Не вдалося скопіювати — ${e.message}`); }
  },
};

async function memAction(id, action, doneText) {
  await guarded(`mem:${id}`, async () => {
    try {
      await api(`/api/memories/${encodeURIComponent(id)}/${action}`, { method: 'POST', body: {} });
      toast('success', html`Спогад ${mono(id)} ${doneText}`);
      await loadMemories();
      scheduleStatus();
    } catch (e) { toast('error', html`Дія не вдалася для ${mono(id)} — ${e.message}`); }
  });
}

async function runSearch() {
  const Q = S.search, q = Q.q.trim();
  if (!q || Q.loading) return;
  const proj = S.status && flat(S.status.projects).find((p) => p.path === Q.project);
  const apiScope = Q.scope === 'all' ? 'all' : 'project', kind = Q.scope === 'memory' ? 'memory' : 'all';
  const cwd = proj ? proj.root : '';
  Q.loading = true; Q.err = null;
  render();
  try {
    Q.result = await api('/api/search', { method: 'POST', body: { q, scope: apiScope, kind, cwd } });
    Q.last = { q, scope: Q.scope, project: proj ? proj.path : null, apiScope, kind, cwd };
  } catch (e) {
    Q.err = e.status === 409 ? `${e.message}` : `Пошук не вдався: ${e.message}`;
  }
  Q.loading = false;
  render();
}

// ------------------------------------------------------------------------------------------------ events from the DOM
document.addEventListener('click', (e) => {
  const el = e.target.closest('[data-act]');
  if (!el || el.disabled) return;
  const fn = A[el.dataset.act];
  if (fn) { e.preventDefault(); fn(el, e); }
});
document.addEventListener('mousedown', (e) => {
  if (e.target.tagName === 'SELECT') { selectOpen = true; setTimeout(() => { selectOpen = false; }, 15000); }
});
document.addEventListener('focusout', (e) => { if (e.target.tagName === 'SELECT') selectOpen = false; });
document.addEventListener('keydown', (e) => {
  selectOpen = false;
  const row = e.target && e.target.dataset && e.target.dataset.act === 'tkrow' ? e.target : null;
  if (row && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); A.tkrow(row); }
});
document.addEventListener('submit', (e) => {
  if (e.target.dataset && e.target.dataset.form === 'search') { e.preventDefault(); runSearch(); }
});
let memTimer = 0;
document.addEventListener('input', (e) => {
  const k = e.target.dataset && e.target.dataset.input;
  if (!k) return;
  if (k === 'jq') { S.filter.q = e.target.value; S.filter.limit = 50; render(); }
  if (k === 'sq') S.search.q = e.target.value;
  if (k === 'cmtext') { const key = e.target.dataset.fk.slice(4); S.tk.draft[key] = { ...S.tk.draft[key], text: e.target.value }; }
  if (k === 'sreply') S.tk.replies[e.target.dataset.sid] = e.target.value;
  if (k === 'mq') {
    S.mem.q = e.target.value;
    clearTimeout(memTimer);
    memTimer = setTimeout(() => { S.mem.limit = 50; loadMemories(); }, 300);
  }
});
document.addEventListener('change', async (e) => {
  selectOpen = false;
  const k = e.target.dataset && e.target.dataset.change;
  if (!k) return;
  const v = e.target.value;
  if (k === 'cmstatus') { const key = e.target.dataset.fk.slice(5); S.tk.draft[key] = { ...S.tk.draft[key], status: v }; }
  if (k === 'jproj') { S.filter.project = v; S.filter.limit = 50; render(); }
  if (k === 'sproj') { S.search.project = v; try { localStorage.setItem('myvec-search-project', v); } catch { /* ignore */ } render(); }
  if (k === 'window') {
    const p = e.target.dataset.p;
    const cur = findProject(S, p);
    try {
      await api(`/api/projects/${ppath(p)}/mode`, { method: 'POST', body: { mode: cur ? cur.mode : 'auto', window_hours: Number(v) } });
      toast('success', html`Вікно активності ${mono(p)}: ${hours(Number(v))}`);
    } catch (err) { toast('error', html`Не вдалося змінити вікно ${mono(p)} — ${err.message}`); }
    loadStatus();
  }
});

// ------------------------------------------------------------------------------------------------ routing
function parseRoute() {
  const hash = location.hash.replace(/^#\/?/, ''), qi = hash.indexOf('?');
  const q = new URLSearchParams(qi < 0 ? '' : hash.slice(qi + 1));
  const parts = (qi < 0 ? hash : hash.slice(0, qi)).split('/').filter((x, i) => x !== '' || i > 0).map((x) => { try { return decodeURIComponent(x); } catch { return x; } });
  const [head, ...rest] = parts;
  switch (head) {
    case 'project': return rest.length ? { name: 'project', path: rest.join('/'), tab: ['tickets', 'sessions', 'tests', 'tokens', 'problems'].includes(q.get('tab')) ? q.get('tab') : null, t: q.get('t') || null, days: [7, 14, 30].includes(Number(q.get('days'))) ? Number(q.get('days')) : 14 } : { name: 'overview' };
    case 'load': return { name: 'load', period: ['1h', '24h', '30d'].includes(q.get('period')) ? q.get('period') : '24h' };
    case 'search': return { name: 'search', days: [7, 14, 30].includes(Number(q.get('days'))) ? Number(q.get('days')) : 14 };
    case 'journal': case 'settings': case 'tickets': case 'projects': return { name: head };
    case 'memory': return { name: 'memory', id: rest[0] || null };
    default: return { name: 'overview' };
  }
}
const TITLES = { overview: 'Огляд', projects: 'Проєкти', project: 'Проєкт', tickets: 'Тікети', load: 'Навантаження', journal: 'Журнал', memory: 'Пам’ять', search: 'Пошук', settings: 'Налаштування' };
function onRoute() {
  const prev = S.route;
  S.route = parseRoute();
  const r = S.route;
  document.title = `${r.name === 'project' ? r.path : TITLES[r.name]} · MyVector`;
  const moved = prev.name !== r.name || prev.path !== r.path;
  if (r.name === 'project') {
    ensureCollapsed(r.path);
    if (moved || (r.tab !== 'tickets' && r.tab !== 'sessions' && r.tab !== 'tests' && r.tab !== 'tokens' && r.tab !== 'problems')) { loadDetail(r.path); loadSearches(r.path); }
    if (moved) { loadTicketSummary(); loadSessions(r.path); } else if (r.tab === 'sessions' && prev.tab !== 'sessions') loadSessions(r.path);
    if (r.tab === 'tests') loadTests(r.path); else if (moved) loadTestSummary(r.path);
    if (r.tab === 'problems') loadProblems(r.path);
    if (r.tab === 'tokens' && (moved || prev.tab !== 'tokens' || prev.days !== r.days)) loadSpend(r.path, r.days);
    if (r.tab === 'tickets') {
      const fresh = moved || prev.tab !== 'tickets';
      if (fresh) loadTicketTree(r.path);
      if (r.t) { if (fresh || prev.t !== r.t) { S.tk.inbox = {}; loadTicketCard(r.path, r.t); loadSpendCard(r.path, r.t); loadInbox(r.path, r.t); } } else delete S.tk.card[r.path];
      if (r.t && prev.t !== r.t && matchMedia('(max-width: 760px)').matches) {
        setTimeout(() => { const c = document.getElementById('mv-tkcard'); if (c) c.scrollIntoView({ block: 'start' }); }, 80);
      }
    }
  }
  if (r.name === 'load') {
    const periodChanged = S.mon.period !== r.period;
    S.mon.period = r.period;
    if (periodChanged) S.mon.series = null;
    if (prev.name !== 'load') loadNow();
    if (prev.name !== 'load' || periodChanged) loadSeries();
  }
  if (r.name === 'search' && (prev.name !== 'search' || prev.days !== r.days)) loadUsage(r.days);
  if (r.name === 'tickets') { loadTicketSummary(); loadWaiting(); }
  if (r.name === 'memory') {
    if (r.id) S.mem.selectedId = r.id;
    if (S.mem.items == null || prev.name !== 'memory') loadMemories(); else ensureSelectedMemory().then(render);
  }
  if (moved) window.scrollTo(0, 0);
  render();
}
window.addEventListener('hashchange', onRoute);

// follow the OS theme until the user picks one
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (e) => {
  let saved = null;
  try { saved = localStorage.getItem(THEME_KEY); } catch { /* ignore */ }
  if (saved) return;
  S.theme = e.matches ? 'dark' : 'light';
  document.documentElement.setAttribute('data-theme', S.theme);
  render();
});

// ------------------------------------------------------------------------------------------------ boot
onRoute();
loadStatus();
loadEventsBacklog().then(startStream);
setInterval(() => { if (!document.hidden) loadStatus(); }, 5000);
document.addEventListener('visibilitychange', () => {
  if (document.hidden) return;
  loadStatus();
  if (S.route.name === 'load') { loadNow(); loadSeries(); }
  if (S.route.name === 'project' && S.route.tab === 'tokens') loadSpend(S.route.path, S.route.days || 14);
  if (S.route.name === 'search') loadUsage(S.route.days || 14);
});
// «Навантаження»: snapshot every 5 s, the series once a minute (the DB gets a row per minute); paused while the tab is hidden
setInterval(() => { if (!document.hidden && S.route.name === 'load') loadNow(); }, 5000);
setInterval(() => { if (!document.hidden && S.route.name === 'load') loadSeries(); }, 60000);
// «Тести» tab: poll only while a run is in progress
setInterval(() => { if (!document.hidden && S.route.name === 'project' && S.route.tab === 'tests' && testsRunning(S.route.path)) loadTests(S.route.path); }, 10000);
// inbox (E3-01): while a sent comment is waiting, look at the folder every 10 s
setInterval(() => {
  const r = S.route;
  if (document.hidden || r.name !== 'project' || r.tab !== 'tickets' || !r.t) return;
  const ib = S.tk.inbox[inboxKey(r.path, r.t)];
  if (ib && ib.items.length) loadInbox(r.path, r.t);
}, 10000);
// «Витрати» tab: the collector scans once a minute, so refresh at that pace; paused while the tab is hidden
setInterval(() => { if (!document.hidden && S.route.name === 'project' && S.route.tab === 'tokens') loadSpend(S.route.path, S.route.days || 14); }, 60000);
// «Пошук»: the adoption card is refreshed once a minute while the page is open and visible (E8-06)
setInterval(() => { if (!document.hidden && S.route.name === 'search') loadUsage(S.route.days || 14); }, 60000);
