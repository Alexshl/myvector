// Pure view functions: state in, html out. No DOM access, no network.
import {
  html, raw, esc, num, dec1, dec2, dec3, dec4, count, plural, TOKENS, pathEl, bytes, hm, hms, dmy, dmyhm, short, dayLabel, dur, ms, hours,
  icon, projectHref, obsidianFile, isoDate, inline, markdown,
} from './util.js';
import * as tracker from './tracker.js';
import * as monitor from './monitor.js';
import * as spend from './spend.js';
import * as adoption from './adoption.js';
import * as problems from './problems.js';

const fk = (k) => raw(`data-fk="${esc(k)}"`);
const MODES = { auto: 'Авто', always: 'Завжди', off: 'Вимк' };
const W = {
  repos: ['репо', 'репо', 'репо'], projects: ['проєкт', 'проєкти', 'проєктів'], results: ['результат', 'результати', 'результатів'],
  chunks: ['шматок', 'шматки', 'шматків'], files: ['файл', 'файли', 'файлів'], events: ['подія', 'події', 'подій'],
  values: ['значення', 'значення', 'значень'], memories: ['спогад', 'спогади', 'спогадів'], branches: ['гілка', 'гілки', 'гілок'],
  searches: ['пошук', 'пошуки', 'пошуків'],
};
const mono = (t, style = '') => html`<span class="mono" style="${style}">${t}</span>`;
const dot = (t = '') => html`<span class="dot ${t}"></span>`;
const nowS = () => Date.now() / 1000;
const nodes = (list, out = []) => { for (const p of list) { out.push(p); nodes(p.children, out); } return out; };
const chunksOf = (p) => p.repos.reduce((a, r) => a + r.chunks, 0);
const lastSyncOf = (p) => Math.max(0, ...p.repos.map((r) => r.last_sync || 0));
const noBranch = (b) => !b || b === '-';
export const findProject = (S, path) => (S.status ? nodes(S.status.projects).find((p) => p.path === path) : undefined);

function activityLabel(p, now, compact = false) {   // compact: table cell (130px) - the green dot already says "active"
  if (p.mode === 'always') return 'завжди активний';
  if (p.mode === 'off') return 'вимкнено';
  if (!p.active) return 'неактивний';
  const left = dur((p.active_until || now) - now);
  return compact ? `ще ${left}` : `активний ще ${left}`;
}

// ---------------------------------------------------------------------------------------------- events
const REASON = { manual: 'вручну', watcher: 'вотчер', 'stale-index': 'застарілий індекс' };
const ACTION = { pin: 'закріплено', unpin: 'відкріплено', archive: 'в архів', promote: 'піднято в глобальну' };

/** Event -> display parts shared by the journal and the "latest events" card. */
export function describeEvent(ev) {
  const d = { cats: [], tag: 'Подія', tagCls: '', title: html`${ev.type}`, proj: ev.project || null, branch: null, route: null, info: '', danger: false };
  switch (ev.type) {
    case 'search': {
      const ident = ev.route === 'hybrid' && !/\s/.test(ev.q || '');
      d.cats = ['search']; d.tag = 'Пошук'; d.route = ev.route || null;
      d.title = ident ? mono(ev.q, 'font-size:12.5px;font-weight:600') : html`<span style="font-weight:550">«${ev.q}»</span>`;
      d.proj = ev.repo || ev.project || null; d.branch = noBranch(ev.branch) ? null : ev.branch;
      d.info = `${ms(ev.ms)} · ${count(ev.results || 0, W.results)} · ≈${count(ev.tokens || 0, TOKENS)} · ${ev.via === 'direct' ? 'напряму' : 'через демон'}`;
      break;
    }
    case 'sync_started':
      d.cats = ['sync']; d.tag = 'Синк'; d.tagCls = 't-primary'; d.title = html`Синк розпочато`;
      d.info = `причина: ${REASON[ev.reason] || ev.reason || '—'}`;
      break;
    case 'sync_finished': {
      d.cats = ['sync']; d.tag = 'Синк'; d.tagCls = 't-primary';
      d.title = html`Синк завершено — ${count(ev.files || 0, W.files)}, ${count(ev.chunks_embedded || 0, W.chunks)}`;
      const extra = [];
      if (ev.masked) extra.push(`${count(ev.masked, W.values)} замасковано`);
      if (ev.dropped) extra.push(`${count(ev.dropped, W.chunks)} відкинуто`);
      if (ev.errors) extra.push(`помилок: ${num(ev.errors)}`);
      d.info = [ev.seconds == null ? null : ms(ev.seconds * 1000), `причина: ${REASON[ev.reason] || ev.reason || '—'}`, ...extra].filter(Boolean).join(' · ');
      break;
    }
    case 'sync_failed':
      d.cats = ['sync', 'error']; d.tag = 'Помилка'; d.tagCls = 't-danger'; d.danger = true;
      d.title = html`Синк не вдався`; d.info = ev.error || '';
      break;
    case 'sync_cancelled':
      d.cats = ['sync']; d.tag = 'Синк'; d.tagCls = 't-primary'; d.title = html`Синк перервано — демон зупинився`;
      d.info = 'збережене не втрачено, наступний синк продовжить';
      break;
    case 'sync_busy':
      d.cats = ['sync']; d.tag = 'Синк'; d.tagCls = 't-primary'; d.title = html`Синк відкладено — інший синк тримає lock`;
      d.info = ev.detail || '';
      break;
    case 'watcher_start':
      d.cats = ['watcher']; d.tag = 'Вотчер'; d.title = html`Вотчер запущено`;
      d.info = ev.watching ? (ev.backend || 'файлові події') : 'без файлових подій (лише розклад)';
      break;
    case 'watcher_stop':
      d.cats = ['watcher']; d.tag = 'Вотчер'; d.title = html`Вотчер зупинено — вікно активності минуло`;
      break;
    case 'mode_changed':
      d.cats = ['mode']; d.tag = 'Режим'; d.title = html`Режим змінено: ${MODES[ev.mode] || ev.mode}`;
      d.info = ev.window_hours ? `вікно активності ${hours(ev.window_hours)}` : '';
      break;
    case 'project_activated':
      d.cats = ['mode']; d.tag = 'Режим'; d.title = html`Проєкт активовано`; d.info = 'вікно активності продовжено';
      break;
    case 'memory_action':
      d.cats = ['memory']; d.tag = 'Пам’ять'; d.title = html`Пам’ять: ${ACTION[ev.action] || ev.action}`; d.proj = null;
      d.info = ev.memory_id || '';
      break;
    case 'daemon_start':
      d.cats = ['daemon']; d.tag = 'Демон'; d.title = html`Демон запущено`; d.proj = null; d.info = `порт ${ev.port}`;
      break;
    case 'daemon_stop':
      d.cats = ['daemon']; d.tag = 'Демон'; d.title = html`Демон зупинено`; d.proj = null;
      break;
    case 'daemon_error':
      d.cats = ['daemon', 'error']; d.tag = 'Помилка'; d.tagCls = 't-danger'; d.danger = true; d.title = html`Помилка демона`;
      d.proj = null; d.info = ev.error || '';
      break;
    default:
      d.cats = ['other'];
  }
  return d;
}
export const isListed = (ev) => ev.type !== 'sync_progress';
const routeBadge = (route, small = false) => (route
  ? html`<span class="bdg nodot ${route === 'vector' ? 't-primary' : route === 'fts-fallback' ? 't-warning' : ''} ${small ? 'pill' : ''}">${route === 'vector' ? 'вектор' : route === 'fts-fallback' ? 'FTS' : 'гібрид'}</span>` : '');
const tagEl = (d) => html`<span class="tag ${d.tagCls}">${d.tag}</span>`;

// ---------------------------------------------------------------------------------------------- shell
export function view(S) {
  const r = S.route;
  const pages = { overview: overviewView, projects: projectsView, project: projectView, tickets: ticketsView, load: loadView, journal: journalView, memory: memoryView, search: searchView, settings: settingsView };
  return html`
    <nav class="rail" aria-label="Розділи">${rail(S)}</nav>
    <div class="mainc">${topbar(S)}<main class="page">${banners(S)}${(pages[r.name] || overviewView)(S)}</main></div>`;
}

export function rail(S) {
  const items = [
    ['overview', '#/', 'grid', 'Огляд'],
    ['projects', '#/projects', 'folders', 'Проєкти'],
    ['tickets', '#/tickets', 'tickets', 'Тікети'],
    ['load', '#/load', 'gauge', 'Навантаження'],
    ['journal', '#/journal', 'activity', 'Журнал'],
    ['memory', '#/memory', 'book', 'Пам’ять'],
    ['search', '#/search', 'search', 'Пошук'],
  ];
  const one = ([name, href, ic, label]) => {
    const on = S.route.name === name || (name === 'projects' && S.route.name === 'project');
    const cur = S.route.name === name ? raw('aria-current="page"') : '';
    return html`<div class="rit ${on ? 'on' : ''}"><a class="rail-btn" href="${href}" ${name === 'projects' && projectsFly(S) ? '' : raw(`data-tip="${label}"`)} aria-label="${label}" ${cur}>${icon(ic, 18)}</a>${name === 'projects' ? projectsFly(S) : ''}</div>`;
  };
  const on = S.route.name === 'settings';
  return html`
    <a class="rail-logo" href="#/" aria-label="MyVector, огляд" style="color: var(--brand-primary);">${icon('mark', 24, 1.8)}</a>
    ${items.map(one)}
    <div class="rail-spacer"></div>
    <div class="rit ${on ? 'on' : ''}"><a class="rail-btn" href="#/settings" data-tip="Налаштування" aria-label="Налаштування" ${on ? raw('aria-current="page"') : ''}>${icon('settings', 18)}</a></div>`;
}

/** Hover/focus flyout of the «Проєкти» rail item: every project, current one marked. Hidden on narrow screens (page only). */
export function projectsFly(S) {
  const all = S.status ? nodes(S.status.projects) : [];
  if (!all.length) return '';
  const cur = S.route.name === 'project' ? S.route.path : null;
  return html`<div class="rfly" role="group" aria-label="Проєкти"><div class="rfly-t">Проєкти</div>${all.map((p) => html`<a href="${projectHref(p.path)}" class="${p.path === cur ? 'on' : ''}" ${p.path === cur ? raw('aria-current="page"') : ''}><span class="mono ell">${p.path}</span><span class="cnt">${p.active ? dot('t-success') : ''} ${num(chunksOf(p))}</span></a>`)}</div>`;
}

export function topbar(S) {
  const r = S.route;
  const cur = { overview: 'Огляд', projects: 'Проєкти', tickets: 'Тікети', load: 'Навантаження', journal: 'Журнал', memory: 'Пам’ять', search: 'Пошук', settings: 'Налаштування' };
  const crumbs = r.name === 'project'
    ? html`<a href="#/projects">Проєкти</a><span class="sep">›</span><span class="cur mono">${r.path}</span>`
    : html`<span class="cur">${cur[r.name]}</span>`;
  const badge = S.statusErr
    ? html`${dot('t-danger')}Демон не працює`
    : S.status ? html`${dot('t-success')}Демон працює · <span class="mono" style="font-weight: 500;">${location.host}</span>`
      : html`${dot('t-warning')}Підключення…`;
  const dark = S.theme === 'dark';
  return html`
    <header class="topbar">
      <nav class="crumbs" aria-label="Шлях"><a href="#/" style="font-weight: 650; color: var(--foreground);">MyVector</a><span class="sep">›</span>${crumbs}</nav>
      <div class="grow"></div>
      <span class="bdg nodot" style="gap: 6px;" role="status">${badge}</span>
      <button type="button" class="btn icon ghost" aria-label="Перемкнути тему" data-act="theme" ${fk('theme')}>${icon(dark ? 'sun' : 'moon', 16)}</button>
    </header>`;
}

function banners(S) {
  const list = [];
  const g = S.status && S.status.global;
  if (S.statusErr) {
    const e = S.statusErr;
    list.push(html`<div class="banner t-danger" role="status">${icon('alert', 15, 1.8)}<span class="grow">${e.status
      ? html`Демон відповів помилкою: <span class="mono">${e.message}</span>.`
      : html`Демон не відповідає на <span class="mono">${location.host}</span>. Claude шукає напряму, але без журналу й фонового синку — запусти <span class="mono">myvec serve</span>.`}</span><button type="button" class="btn sm danger" data-act="retry" ${fk('retry')}>Перевірити знову</button></div>`);
  } else if (g) {
    if (g.ollama.reachable === false) {
      list.push(html`<div class="banner t-warning" role="status">${icon('warn', 15, 1.8)}<span class="grow"><strong>Ембединги недоступні — векторний пошук і ембединг нових змін не працюють.</strong> Ollama не відповідає на <span class="mono">${g.ollama.url}</span>; синк повториться, щойно вона повернеться.</span><span class="mono" style="font-size: 12px; color: var(--foreground);">ollama serve</span></div>`);
    }
    if (g.model_mismatch) {
      list.push(html`<div class="banner t-info" role="status">${icon('info', 15, 1.8)}<span class="grow"><strong>Модель змінено — потрібна переіндексація: <span class="mono">myvec reembed</span>.</strong> Індекс зібрано з <span class="mono">${g.model}</span> (${num(g.dims)}-d), у конфігу — <span class="mono">${g.config.model}</span> (${num(g.config.dims)}-d). Пошук і синк відмовляються працювати, доки індекс не перераховано.</span><button type="button" class="btn sm primary" data-act="copy" data-copy="myvec reembed" data-msg="Команду скопійовано" ${fk('copy-reembed')}>${icon('copy', 13)}Скопіювати команду</button></div>`);
    }
  }
  return list.length ? html`<div class="banners">${list}</div>` : '';
}

const header = (title, sub, actions = '') => html`
  <div class="row between wrap" style="align-items: flex-start; gap: 12px;">
    <div><h1 class="h1" style="margin: 0;">${title}</h1><div class="sub">${sub}</div></div>
    ${actions ? html`<div class="row wrap">${actions}</div>` : ''}
  </div>`;

const liveBadge = (S) => {
  const st = S.live.state;
  const t = { live: ['t-success', 'Наживо · SSE'], paused: ['', 'Потік призупинено'], connecting: ['t-warning', 'Підключення до потоку…'], offline: ['t-danger', 'Потік перервано, повторюю'] }[st];
  return { dot: t[0], text: t[1] };
};

// ---------------------------------------------------------------------------------------------- overview
const PROJ_COLS = 'minmax(190px,1fr) minmax(90px,120px) 208px 130px 64px 72px 40px';   // sum ≤ 795px: fits the main column on a 1280px screen

function modeSeg(p, aria, labelledBy = '') {
  return html`<div class="seg mv-seg" role="group" ${labelledBy ? raw(`aria-labelledby="${labelledBy}"`) : raw(`aria-label="${esc(aria)}"`)}>${['auto', 'always', 'off'].map((m) => html`<button type="button" class="${p.mode === m ? 'on' : ''}" aria-pressed="${String(p.mode === m)}" data-act="mode" data-p="${p.path}" data-v="${m}" ${fk(`mode:${p.path}:${m}`)}>${MODES[m]}</button>`)}</div>`;
}

function syncBtn(p, size = 15) {
  const busy = p.syncing;
  return html`<button type="button" class="btn icon ghost sm" aria-label="${busy ? `Синхронізація ${p.path} уже триває` : `Синхронізувати ${p.path}`}" data-act="sync" data-p="${p.path}" ${busy ? raw('disabled') : ''} ${fk(`sync:${p.path}`)}>${icon('refresh', size)}</button>`;
}

function projectRows(S, p, depth, now) {
  const collapsed = S.collapsed.has(p.path);
  const root = p.repos.find((r) => r.path === p.path);
  const pad = depth * 20;
  const last = lastSyncOf(p);
  const pr = p.syncing ? p.progress : null;
  const head = html`
    <div class="trow" style="grid-template-columns: ${PROJ_COLS};${p.syncing ? ' background: var(--brand-soft);' : ''}">
      <span class="row" style="gap: 4px; padding-left: ${pad}px;">${p.repos.length
        ? html`<button type="button" class="btn icon ghost sm" aria-label="${collapsed ? 'Розгорнути' : 'Згорнути'} ${p.path}" aria-expanded="${String(!collapsed)}" data-act="collapse" data-p="${p.path}" ${fk(`collapse:${p.path}`)}>${icon(collapsed ? 'chevRight' : 'chevDown', 13)}</button>`
        : html`<span style="width: 28px; flex: none;"></span>`}<a class="mono" href="${projectHref(p.path)}" style="font-size: 12.5px; font-weight: 600;">${p.path}</a><span class="muted" style="font-size: 12px; white-space: nowrap;">${p.repos.length} репо</span></span>
      ${root && !noBranch(root.current_branch) ? html`<span class="mono ell" style="font-size: 12px;" title="${root.current_branch}">${root.current_branch}</span>` : html`<span class="muted" style="font-size: 12px;">.myvector.toml</span>`}
      <span>${modeSeg(p, `Режим синку ${p.path}`)}</span>
      <span class="row" style="gap: 6px; font-size: 12.5px;">${dot(p.active ? 't-success' : '')}<span class="ell" title="${activityLabel(p, now)}">${activityLabel(p, now, true)}</span></span>
      ${p.syncing ? html`<span class="row" style="gap: 6px;"><span class="spinner" role="status" aria-label="Синхронізується"></span><span style="font-size: 12px;">зараз</span></span>` : html`<span class="date">${short(last, now)}</span>`}
      <span class="num" style="font-weight: 600;">${num(chunksOf(p))}</span>
      <span style="display: flex; justify-content: center;">${syncBtn(p)}</span>
    </div>`;
  const prog = pr && pr.blobs_total ? html`
    <div style="min-width: min-content; padding: 6px 16px 12px ${54 + pad}px; border-bottom: 1px solid var(--border); background: var(--brand-soft);">
      <div class="stack" style="gap: 6px;">
        <div class="row between wrap" style="font-size: 12px; gap: 6px 16px;"><span>Ембедимо лише змінені блоби · <span class="tnum">${count(pr.chunks_embedded, W.chunks)}</span></span><span class="mono tnum" style="font-weight: 550;">${num(pr.blobs_done)} / ${num(pr.blobs_total)} блобів</span></div>
        <div class="prog" role="progressbar" aria-label="Синк ${p.path}" aria-valuemin="0" aria-valuemax="${pr.blobs_total}" aria-valuenow="${pr.blobs_done}"><span style="width: ${pr.percent || 0}%;"></span></div>
      </div>
    </div>` : '';
  if (collapsed) return html`${head}${prog}`;
  const repos = p.repos.map((r) => {
    const rest = r.path.startsWith(p.path + '/') ? r.path.slice(p.path.length + 1) : null;
    return html`
      <div class="trow" style="grid-template-columns: ${PROJ_COLS};">
        ${rest != null
          ? html`<span class="mono ell" style="font-size: 12px; padding-left: ${44 + pad}px;" title="${r.path}">${rest}</span>`
          : html`<span class="row" style="gap: 6px; padding-left: ${44 + pad}px;"><span class="mono ell" style="font-size: 12px;" title="${p.path}">${p.path}</span><span class="tag" style="flex: none;">корінь</span></span>`}
        ${noBranch(r.current_branch) ? html`<span class="muted mono" style="font-size: 12px;" aria-label="без гілки">—</span>` : html`<span class="mono ell" style="font-size: 12px;" title="${r.current_branch}">${r.current_branch}</span>`}
        <span></span><span></span>
        <span class="date">${short(r.last_sync, now)}</span>
        <span class="num">${num(r.chunks)}</span>
        <span></span>
      </div>`;
  });
  return html`${head}${prog}${repos}${p.children.map((c) => projectRows(S, c, depth + 1, now))}`;
}

function attention(S, now) {
  const items = [];
  for (const p of nodes(S.status.projects)) {
    const masked = p.repos.reduce((a, r) => a + r.masked, 0), dropped = p.repos.reduce((a, r) => a + r.dropped, 0);
    if (masked || dropped) {
      const parts = [masked ? `${count(masked, W.values)} замасковано` : '', dropped ? `${count(dropped, W.chunks)} відкинуто` : ''].filter(Boolean).join(' й ');
      items.push({ tone: 't-warning', href: projectHref(p.path), text: html`${parts} в <span class="mono">${p.path}</span> — перевір, чи це не робочі ключі` });
    }
  }
  const day = now - 86400, seen = new Set();
  const evs = S.events;
  for (let i = evs.length - 1; i >= 0; i--) {
    const e = evs[i];
    if (e.type !== 'sync_failed' || e.ts < day || seen.has(e.project)) continue;
    seen.add(e.project);
    const healed = evs.slice(i + 1).some((x) => x.type === 'sync_finished' && x.project === e.project);
    items.push({ tone: healed ? 't-info' : 't-danger', href: '#/journal', text: html`<span class="mono">${e.project}</span>: синк не вдався о <span class="mono">${hm(e.ts)}</span>${healed ? ' — потім повторено успішно' : ''}` });
  }
  return items;
}

function eventLi(ev) {
  const d = describeEvent(ev);
  return html`<li class="events-li">
    <span class="date muted">${hm(ev.ts)}</span>
    <div class="stack" style="gap: 3px;"><span class="row wrap" style="gap: 6px;">${tagEl(d)}${routeBadge(d.route, true)}${d.title}</span>
    <span class="muted" style="font-size: 12px; overflow-wrap: anywhere;">${d.proj ? mono(d.proj, 'font-size:12px') : ''}${d.branch ? html` · <span class="mono">${d.branch}</span>` : ''}${d.proj && d.info ? ' · ' : ''}${d.danger ? html`<span style="color: var(--danger);">${d.info}</span>` : d.info}</span></div>
  </li>`;
}

function overviewSkeleton() {
  const cols = 'minmax(120px,1fr) minmax(80px,110px) minmax(90px,120px) 70px';
  const row = (a, b, c, d) => html`<div class="trow" style="grid-template-columns: ${cols};"><span><span class="skel" style="width: ${a}%;"></span></span><span><span class="skel" style="width: ${b}%;"></span></span><span><span class="skel" style="width: ${c}%;"></span></span><span><span class="skel" style="width: ${d}%;"></span></span></div>`;
  return html`
    ${header('Огляд', 'Завантажуємо статус…')}
    <div class="kpis">${[1, 2, 3, 4].map(() => html`<div class="card kpi" aria-busy="true"><span class="skel" style="width: 50%;"></span><span class="skel" style="width: 35%; height: 20px;"></span><span class="skel" style="width: 70%;"></span></div>`)}</div>
    <section class="card" aria-label="Проєкти завантажуються" aria-busy="true"><div style="padding: 12px;"><div class="tbl" style="box-shadow: none;">
      <div class="thead" style="grid-template-columns: ${cols};"><span>Шлях</span><span>Гілка</span><span>Активність</span><span class="right">Шматків</span></div>
      ${row(70, 60, 80, 70)}${row(55, 50, 65, 60)}${row(62, 45, 72, 55)}</div></div></section>`;
}

function firstIndexing(p) {
  const pr = p.progress || {};
  const pct = pr.percent == null ? 0 : Math.round(pr.percent);
  const done = icon('checkCircle', 15, 1.8, 'var(--success)');
  const step = (state, body) => html`<li class="row" style="gap: 10px; font-size: 12.5px;">${state === 'done' ? done : state === 'run' ? html`<span style="color: var(--brand-primary); display: inline-flex;">${icon('loader', 15, 1.8)}</span>` : icon('circle', 15, 1.8, 'var(--faint-foreground)')}<span class="${state === 'todo' ? 'muted' : ''}">${body}</span></li>`;
  return html`
    <section class="card" aria-labelledby="mv-first" style="max-width: 640px;">
      <div class="card-h"><h2 id="mv-first" class="sec-t">Перша індексація</h2><span class="grow"></span><span class="hint">пошук працює по готовій частині</span></div>
      <div class="card-b stack" style="gap: 14px;">
        <div class="row" style="gap: 8px;"><span style="color: var(--brand-primary); display: inline-flex;">${icon('loader', 18, 1.8)}</span><span style="font-size: 14px; font-weight: 600;">Індексуємо <span class="mono">${p.path}</span></span><span class="grow"></span><span class="mono tnum" style="font-size: 12px;">${pct}%</span></div>
        <div class="prog" role="progressbar" aria-label="Перша індексація ${p.path}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"><span style="width: ${pct}%;"></span></div>
        <div class="card"><ol class="card-b stack" style="gap: 10px; margin: 0; list-style: none;">
          ${step(pr.files ? 'done' : 'run', pr.files ? `Знайдено ${count(pr.files, W.files)}` : 'Шукаємо репозиторії й файли')}
          ${step(pr.blobs_total != null && pr.blobs_total === pr.blobs_done && pr.blobs_total ? 'done' : pr.files ? 'run' : 'todo',
            html`Ембединги <span class="mono tnum">${num(pr.blobs_done || 0)} / ${num(pr.blobs_total || 0)}</span> блобів · ${count(pr.chunks_embedded || 0, W.chunks)}`)}
          ${step('todo', 'Маніфести гілок')}
        </ol></div>
        <span class="muted" style="font-size: 12px;">Можна закрити вікно — індексація триває в демоні.</span>
      </div>
    </section>`;
}

/** The projects table card, shared by the overview and the #/projects page. */
function projectsCard(S, st, all, now, titled = true) {
  const g = st.global;
  const total = all.reduce((a, p) => a + chunksOf(p), 0);
  const repos = all.reduce((a, p) => a + p.repos.length, 0);
  const excluded = all.flatMap((p) => (p.exclude || []).map((x) => (/[*?[\]]/.test(x) ? x : `${p.path}/${x.replace(/\/$/, '')}`)));
  return html`
      <section class="card main" ${titled ? raw('aria-labelledby="mv-projects"') : raw('aria-label="Проєкти"')} style="overflow: hidden;">
        <div class="card-h">${titled ? html`<h2 id="mv-projects" class="sec-t">Проєкти</h2>` : ''}<span class="grow"></span><span class="hint">${g.daemon.watch_backend || 'файлові події'} · синк через ${dur(g.daemon.debounce_s || 10)} після змін · страхувальний кожні ${dur(g.daemon.safety_s || 900)}</span></div>
        <div class="scroll-x">
          <div class="thead" style="border-radius: 0; grid-template-columns: ${PROJ_COLS};"><span>Шлях</span><span>Гілка</span><span>Режим</span><span>Активність</span><span>Синк</span><span class="right">Шматків</span><span></span></div>
          ${st.projects.map((p) => projectRows(S, p, 0, now))}
        </div>
        <div class="tfoot" style="position: static;"><span class="tnum">${count(all.length, W.projects)} · ${num(repos)} репо${excluded.length ? html` · виключено: ${excluded.slice(0, 4).map((x, i) => html`${i ? ', ' : ''}<span class="mono">${x}</span>`)}${excluded.length > 4 ? '…' : ''}` : ''}</span><span class="num" style="font-weight: 600;">${num(total)}</span></div>
      </section>`;
}

function projectsView(S) {
  if (!S.status) return S.statusErr ? '' : overviewSkeleton();
  const st = S.status, all = nodes(st.projects), now = nowS();
  if (!all.length) return overviewView(S);
  return html`
    ${header('Проєкти', html`${count(all.length, W.projects)} · оновлено <span class="mono">${hm(st.now)}</span>`)}
    ${projectsCard(S, st, all, now, false)}`;
}

function overviewView(S) {
  if (!S.status) return S.statusErr ? '' : overviewSkeleton();
  const st = S.status, g = st.global, now = nowS();
  const all = nodes(st.projects), active = all.filter((p) => p.active);
  const total = all.reduce((a, p) => a + chunksOf(p), 0);
  const mem = g.memories, globalMem = mem.by_scope.global || 0, s = g.searches;
  const activeLabel = `${num(active.length)} ${plural(active.length, 'активний', 'активні', 'активних')}`;
  if (!all.length) {
    return html`
      ${header('Огляд', html`0 проєктів · оновлено <span class="mono">${hm(st.now)}</span>`)}
      <section class="card" aria-labelledby="mv-empty" style="max-width: 560px;">
        <div class="card-h"><h2 id="mv-empty" class="sec-t">Проєкти</h2><span class="grow"></span><span class="hint">перший запуск</span></div>
        <div class="empty">${icon('folderEmpty', 26, 1.4, 'var(--faint-foreground)')}<span class="t">Ще немає проєктів</span>
          <span style="font-size: 12.5px; max-width: 300px;">Відкрий папку в Claude Code і напиши <span class="mono">/myvector</span> — проєкт з’явиться тут.</span>
          <button type="button" class="btn sm" data-act="copy" data-copy="/myvector" data-msg="Команду скопійовано" ${fk('copy-myvector')}>${icon('copy', 13)}Скопіювати <span class="mono">/myvector</span></button></div>
      </section>`;
  }
  const firstIdx = all.filter((p) => p.syncing && chunksOf(p) === 0);
  const attn = attention(S, now);
  const recent = S.events.filter(isListed).slice(-6).reverse();
  const live = liveBadge(S);
  const actions = html`
    <a class="btn" href="#/search">${icon('search', 15)}Перевірити пошук</a>
    <button type="button" class="btn primary" data-act="syncActive" ${active.length ? '' : raw('disabled')} ${fk('sync-active')}>${icon('refresh', 15)}Синхронізувати активні</button>`;
  return html`
    ${header('Огляд', html`${count(all.length, W.projects)} · ${activeLabel} · оновлено <span class="mono">${hm(st.now)}</span>`, actions)}
    <div class="kpis">
      <div class="card kpi"><span class="k">Активні проєкти</span><span class="v tnum">${active.length}</span><span class="d">з ${all.length} · ${active.length ? active.map((p) => p.path).join(', ') : 'усі вотчери зупинено'}</span></div>
      <div class="card kpi"><span class="k">Шматків в індексі</span><span class="v tnum">${num(total)}</span><span class="d">${g.model} · ${g.dims}-d · ${bytes(g.db_size_bytes)}</span></div>
      <div class="card kpi"><span class="k">Спогадів</span><span class="v tnum">${num(mem.total)}</span><span class="d">${num(globalMem)} ${plural(globalMem, 'глобальний', 'глобальні', 'глобальних')} · ${num(mem.total - globalMem)} ${plural(mem.total - globalMem, 'проєктний', 'проєктні', 'проєктних')}${mem.archived ? ` · ${num(mem.archived)} в архіві` : ''}</span></div>
      <div class="card kpi"><span class="k">Пошуків за 24 год</span><span class="v tnum">${num(s.count_24h)}</span><span class="d">${s.p50_ms == null ? 'ще не було' : `p50 ${ms(s.p50_ms)} · p95 ${ms(s.p95_ms)}`}</span></div>
    </div>
    ${firstIdx.map(firstIndexing)}
    <div class="cols">
      ${projectsCard(S, st, all, now)}
      <div class="stack side" style="gap: 16px;">
        <section class="card card-b stack" aria-labelledby="mv-attn" style="gap: 12px;">
          <h2 id="mv-attn" class="sec-t">Потребує уваги</h2>
          ${attn.length ? attn.map((a) => html`<a href="${a.href}" class="lrow lrow-link"><span class="dot ${a.tone}"></span><span class="grow">${a.text}</span><span class="go">→</span></a>`) : html`<span class="muted" style="font-size: 12.5px;">Нічого не потребує уваги.</span>`}
        </section>
        <section class="card" aria-labelledby="mv-events">
          <div class="card-h"><h2 id="mv-events" class="sec-t">Останні події</h2><span class="row" style="gap: 6px; font-size: 12px; font-weight: 500; color: var(--muted-foreground);">${dot(live.dot)}${S.live.state === 'live' ? 'наживо' : S.live.state === 'paused' ? 'призупинено' : 'немає зв’язку'}</span><span class="grow"></span><a href="#/journal" style="font-size: 12.5px; font-weight: 550;">Журнал →</a></div>
          ${recent.length ? html`<ol style="list-style: none; margin: 0; padding: 0;">${recent.map(eventLi)}</ol>` : html`<div class="empty" style="padding: 28px 20px;"><span class="t">Подій ще немає</span><span style="font-size: 12.5px;">Вони з’являться після першого пошуку чи синку.</span></div>`}
        </section>
      </div>
    </div>`;
}

// ---------------------------------------------------------------------------------------------- project
const REPO_COLS = 'minmax(210px,1fr) minmax(120px,150px) 64px 84px 96px 76px minmax(180px,200px)';
const BR_COLS = 'minmax(150px,1fr) 84px minmax(150px,180px) minmax(130px,160px) minmax(96px,120px)';
const SEARCH_COLS = '56px minmax(220px,1fr) minmax(130px,160px) 84px 64px 64px 88px minmax(96px,110px)';
const WINDOWS = [0.5, 1, 2, 4, 8];
const BR_STATE = { current: ['поточна', 't-primary'], known: ['відома', ''], merged: ['злита', ''], gone: ['немає локально', ''] };

function secretsCell(r) {
  if (!r.masked && !r.dropped) return html`<span class="muted" style="font-size: 12px;">—</span>`;
  return html`<span style="font-size: 12px;">${[r.masked ? `${num(r.masked)} замасковано` : '', r.dropped ? `${num(r.dropped)} відкинуто` : ''].filter(Boolean).join(' · ')}</span>`;
}

function repoRows(S, p, det) {
  const repos = det ? det.repos : p.repos.map((r) => ({ ...r, branches: [], manifest_bytes: null }));
  return repos.map((r) => {
    const open = S.expanded.has(r.path), n = det ? r.branches.length : r.branches.length;
    const rest = r.path.startsWith(p.path + '/') ? r.path.slice(p.path.length + 1) : null;
    const isRoot = rest == null;
    const sync = p.syncing ? html`<span class="row" style="gap: 6px;"><span class="spinner" role="status" aria-label="Синхронізується"></span><span style="font-size: 12px;">${p.progress && p.progress.percent != null ? `${Math.round(p.progress.percent)}%` : '…'}</span></span>` : html`<span class="date">${short(r.last_sync)}</span>`;
    const row = html`
      <div class="trow ${open ? 'is-sel' : ''}" style="grid-template-columns: ${REPO_COLS};">
        <span class="row" style="gap: 4px;${isRoot ? ' padding-left: 32px;' : ''}">${isRoot ? '' : html`<button type="button" class="btn icon ghost sm" aria-label="${open ? 'Згорнути' : 'Розгорнути'} гілки ${r.path}" aria-expanded="${String(open)}" data-act="repo" data-v="${r.path}" ${fk(`repo:${r.path}`)}>${icon(open ? 'chevDown' : 'chevRight', 13)}</button>`}${isRoot
          ? html`<span class="mono" style="font-size: 12px;">${p.path}</span><span class="tag">корінь</span>`
          : html`<span class="mono ell" style="font-size: 12px;"><span class="muted">${p.path}/</span>${rest}</span>`}</span>
        ${noBranch(r.current_branch) ? html`<span class="mono muted" style="font-size: 12px;" aria-label="без гілки">—</span>` : html`<span class="mono ell" style="font-size: 12px;" title="${r.current_branch}">${r.current_branch}</span>`}
        ${isRoot ? html`<span class="num muted">—</span>` : html`<span class="num">${n || '—'}</span>`}
        <span class="num">${num(r.chunks)}</span>
        <span class="num">${r.manifest_bytes == null ? '—' : bytes(r.manifest_bytes)}</span>
        ${sync}
        ${secretsCell(r)}
      </div>`;
    if (!open || isRoot) return row;
    return html`${row}
      <div style="min-width: min-content; padding: 4px 16px 14px 48px; border-bottom: 1px solid var(--border); background: var(--surface-muted);">
        <div class="tbl" style="box-shadow: none;">
          <div class="thead" style="grid-template-columns: ${BR_COLS};"><span>Гілка</span><span class="right">Шматків</span><span class="right">Маніфест</span><span>Оновлено</span><span>Стан</span></div>
          ${r.branches.map((b) => {
            const [label, cls] = BR_STATE[b.state] || BR_STATE.known;
            return html`<div class="trow" style="grid-template-columns: ${BR_COLS};"><span class="mono" style="font-size: 12px;${b.current ? ' font-weight: 550;' : ''}">${b.branch}</span><span class="num">${num(b.chunks)}</span><span class="num">${bytes(b.manifest_bytes)} · ${count(b.files, W.files)}</span><span class="date">${b.updated ? dmyhm(b.updated) : '—'}</span><span><span class="bdg ${cls}">${label}${b.merged_into ? ` в ${b.merged_into}` : ''}</span></span></div>`;
          })}
          <div class="tfoot" style="position: static;"><span>Унікальних шматків на ${num(r.branches.length)} ${plural(r.branches.length, 'гілку', 'гілки', 'гілок')}: <span class="tnum" style="font-weight: 600;">${num(r.unique_chunks)}</span> — перемикання гілки переембеджує лише змінені файли</span></div>
        </div>
      </div>`;
  });
}

// ---------------------------------------------------------------------------------------------- tickets (tracker.js glue)
const trackerHrefs = {
  project: (path) => projectHref(path) + '?tab=tickets',
  ticket: (path, id) => `${projectHref(path)}?tab=tickets&t=${encodeURIComponent(id)}`,
  all: () => '#/tickets',
  problems: (path) => projectHref(path) + '?tab=problems',
};
const summaryOf = (S, path) => (S.tk.summary || []).find((x) => x.project_path === path) || {};
const ticketsOf = (S, path) => summaryOf(S, path).n_tickets || 0;

/** Tab registry of the project page; later tickets append their own entries (sessions, tests). */
function projectTabs(S, p) {
  const cur = S.route.tab || 'index';
  const n = ticketsOf(S, p.path);
  const pn = problems.problemCount(summaryOf(S, p.path));
  const ss = S.tk.sessions[p.path];
  const sn = ss ? ss.sessions.length : 0;
  const tn = ((S.mon.summary[p.path] || {}).n) || 0;
  const tokensOn = Boolean(S.status && S.status.tokens && S.status.tokens.enabled);
  const tabs = [
    { key: 'index', label: 'Індекс', href: projectHref(p.path) },
    ...(n > 0 || cur === 'tickets' ? [{ key: 'tickets', label: 'Тікети', cnt: n, href: projectHref(p.path) + '?tab=tickets' }] : []),
    ...(sn > 0 || cur === 'sessions' ? [{ key: 'sessions', label: 'Сесії', cnt: sn, href: projectHref(p.path) + '?tab=sessions' }] : []),
    ...(tn > 0 || cur === 'tests' ? [{ key: 'tests', label: 'Тести', cnt: tn, href: projectHref(p.path) + '?tab=tests' }] : []),
    ...(n > 0 || pn > 0 || cur === 'problems' ? [{ key: 'problems', label: 'Проблеми', cnt: pn, href: projectHref(p.path) + '?tab=problems' }] : []),
    ...(tokensOn || cur === 'tokens' ? [{ key: 'tokens', label: 'Витрати', href: projectHref(p.path) + '?tab=tokens' }] : []),
  ];
  if (tabs.length < 2) return '';
  return html`<nav class="ptabs" aria-label="Розділи проєкту">${tabs.map((t) => html`<a href="${t.href}" class="${cur === t.key ? 'on' : ''}" ${cur === t.key ? raw('aria-current="page"') : ''}>${t.label}${t.cnt != null ? html` <span class="cnt">${num(t.cnt)}</span>` : ''}</a>`)}</nav>`;
}

/** «Коментарі» картки тікета (E3-01): стан у S.tk.inbox / S.tk.draft, ключ '<path>\n<id>'. */
function commentsFor(S, path, id) {
  const k = path + '\n' + id;
  const card = S.tk.card[path];
  if (!card || card.error || card.id !== id || card.kind === 'group') return '';
  const ib = S.tk.inbox[k] || { items: [], done: [] };
  return tracker.commentsBlock(card.sections && card.sections.comments, ib.items, ib.done,
    { project: path, ticket: id, busy: S.tk.busy['inbox:' + k], draft: S.tk.draft[k] });
}

function projectTicketsBody(S, path) {
  const T = S.tk;
  return tracker.projectTicketsPage(T.tree[path] || null, {
    path, filter: T.filter, collapsed: T.collapsed[path] || new Set(), sel: S.route.t || null,
    card: T.card[path], hrefs: trackerHrefs, err: T.treeErr[path],
    node: T.node, replies: T.replies, busy: T.busy, cmHint: T.cm && T.cm.hint,
    cardExtra: S.route.t ? html`${commentsFor(S, path, S.route.t)}${spend.ticketBlock((S.spend.card[path + '\n' + S.route.t] || {}).card)}` : '',
  });
}

function projectProblemsBody(S, path) {
  return problems.problemsTab(S.tk.problems[path] || null, { path, hrefs: trackerHrefs, err: S.tk.problemsErr[path] });
}

function projectSessionsBody(S, path) {
  const T = S.tk;
  return tracker.sessionsPage(T.sessions[path] || null, { hrefs: trackerHrefs, busy: T.busy, err: T.sessionsErr[path] });
}

/** Shared by «Навантаження» and the «Тести» tab: links and thresholds for monitor.js. */
const monitorUi = (S) => ({
  period: S.mon.period, alertPct: S.mon.alertPct ?? undefined, alertMinutes: S.mon.alertMinutes ?? undefined, projectHref,
  hrefs: { period: (p) => `#/load?period=${p}` },
});

function loadView(S) {
  const M = S.mon;
  return monitor.loadPage({ load: M.load, series: M.series, current: M.current, off: M.off, err: M.err }, monitorUi(S));
}

function projectTestsBody(S, path) {
  const M = S.mon;
  const host = M.load && M.load.host;
  return monitor.testsTab(M.tests[path] || null, { hostCores: host && host.cores });
}

const SPEND_HREFS = { ...trackerHrefs, days: (path, n) => `${projectHref(path)}?tab=tokens&days=${n}` };

function projectTokensBody(S, path) {
  const days = S.route.days || 14;
  return spend.tokensTab(S.spend.project[path + '\n' + days] || null, { path, days, hrefs: SPEND_HREFS });
}

function ticketsView(S) {
  return tracker.allProjectsPage(S.tk.summary ? { projects: S.tk.summary } : null, { hrefs: trackerHrefs, err: S.tk.summaryErr, waiting: S.tk.waiting, node: S.tk.node, busy: S.tk.busy, cmHint: S.tk.cm && S.tk.cm.hint });
}

function projectView(S) {
  const path = S.route.path;
  if (!S.status) return S.statusErr ? '' : html`${header(html`<span class="mono">${path}</span>`, 'Завантажуємо…')}<section class="card card-b stack" aria-busy="true"><span class="skel" style="width: 40%;"></span><span class="skel" style="width: 70%;"></span><span class="skel" style="width: 55%;"></span></section>`;
  const p = findProject(S, path), now = nowS();
  if (!p) {
    return html`${header(html`<span class="mono">${path}</span>`, 'Проєкт не знайдено')}
      <section class="card"><div class="empty"><span class="t">Такого проєкту немає</span><span style="font-size: 12.5px;">Можливо, його видалено або перейменовано.</span><a class="btn sm" href="#/">До огляду</a></div></section>`;
  }
  const det = S.detail[path] && S.detail[path].path === path ? S.detail[path] : null;
  const sr = S.searches[path];
  const total = chunksOf(p);
  const g = S.status.global;
  const winOpts = WINDOWS.includes(p.window_hours) ? WINDOWS : [...WINDOWS, p.window_hours].sort((a, b) => a - b);
  const sec = det ? det.secrets : { masked: p.repos.reduce((a, r) => a + r.masked, 0), dropped: p.repos.reduce((a, r) => a + r.dropped, 0), files: [] };
  const w = det ? det.watch : null;
  const watching = det ? det.watching : p.watching;
  const ex = det ? det.exclude : null;
  const secRow = (f) => html`<li class="stack" style="gap: 1px;"><span class="mono ell" style="font-size: 12px;">${f.repo}/${f.path}</span><span class="muted">${[f.masked ? `${num(f.masked)} замасковано` : '', f.dropped ? `${num(f.dropped)} відкинуто` : ''].filter(Boolean).join(' · ')}</span></li>`;
  const exRow = (items, label) => items.length ? html`<li style="display: grid; grid-template-columns: minmax(0, 1fr) 112px; gap: 12px; align-items: baseline;"><span class="mono" style="font-size: 12px; overflow-wrap: anywhere;">${items.join(', ')}</span><span class="muted" style="white-space: nowrap; text-align: right;">${label}</span></li>` : '';
  const cut = (xs, n) => (xs.length > n ? [...xs.slice(0, n), '…'] : xs);
  return html`
    <div class="row between wrap" style="align-items: flex-start; gap: 12px 24px;">
      <div>
        <div class="row wrap" style="gap: 8px;"><h1 class="h1 mono" style="margin: 0;">${p.path}</h1><span class="bdg ${p.active ? 't-primary' : ''}">${activityLabel(p, now)}${p.mode === 'auto' && p.active ? ` · вікно ${hours(p.window_hours)}` : ''}</span></div>
        <div class="sub">${S.route.tab === 'tokens' ? spend.tokensSub(S.spend.project[path + '\n' + (S.route.days || 14)], S.route.days || 14) : S.route.tab === 'tests' && S.mon.summary[path] ? monitor.testsSub(S.mon.summary[path]) : html`${pathEl(p.root, g.user_home, 48)} · ${p.repos.length} репо · ${count(total, W.chunks)} · конфіг <span class="mono">.myvector.toml</span>`}</div>
      </div>
      ${S.route.tab === 'tokens' ? spend.periodSeg(path, S.route.days || 14, SPEND_HREFS) : html`      <div class="row wrap" style="gap: 10px 16px;">
        <div class="row" style="gap: 8px;"><span id="mv-mode-l" style="font-size: 12px; font-weight: 550; color: var(--muted-foreground);">Фоновий синк</span>${modeSeg(p, '', 'mv-mode-l')}</div>
        <div class="row" style="gap: 8px;"><label for="mv-window" style="font-size: 12px; font-weight: 550; color: var(--muted-foreground);">Вікно активності</label>
          <select id="mv-window" class="inp" data-change="window" data-p="${p.path}" ${fk('window')}>${winOpts.map((h) => html`<option value="${h}" ${h === p.window_hours ? raw('selected') : ''}>${hours(h)}</option>`)}</select></div>
        <button type="button" class="btn primary" data-act="sync" data-p="${p.path}" ${p.syncing ? raw('disabled') : ''} ${fk('sync-now')}>${icon('refresh', 15)}${p.syncing ? 'Синк триває' : 'Синхронізувати зараз'}</button>
      </div>`}
    </div>
    ${projectTabs(S, p)}
    ${S.route.tab === 'tickets' ? projectTicketsBody(S, path) : S.route.tab === 'sessions' ? projectSessionsBody(S, path) : S.route.tab === 'tests' ? projectTestsBody(S, path) : S.route.tab === 'tokens' ? projectTokensBody(S, path) : S.route.tab === 'problems' ? projectProblemsBody(S, path) : html`
    <div class="cols">
      <div class="stack main" style="gap: 16px;">
        <section class="card" aria-labelledby="mv-repos" style="overflow: hidden;">
          <div class="card-h"><h2 id="mv-repos" class="sec-t">Репозиторії</h2><span class="grow"></span><span class="hint">індекс за git-блобами · гілка = маніфест</span></div>
          <div class="scroll-x">
            <div class="thead" style="border-radius: 0; grid-template-columns: ${REPO_COLS};"><span>Репозиторій</span><span>Поточна гілка</span><span class="right">Гілок</span><span class="right">Шматків</span><span class="right">Маніфест</span><span>Синк</span><span>Секрети</span></div>
            ${p.repos.length ? repoRows(S, p, det) : html`<div class="empty" style="padding: 28px 20px;"><span class="t">Репозиторіїв ще немає</span><span style="font-size: 12.5px;">Вони з’являться після першого синку.</span></div>`}
          </div>
          <div class="tfoot" style="position: static;"><span>${p.repos.length} репо${(p.exclude || []).length ? html` · виключено: ${cut(p.exclude, 3).map((x, i) => html`${i ? ', ' : ''}<span class="mono">${x}</span>`)}` : ''}</span><span class="num" style="font-weight: 600;">${num(total)}</span></div>
        </section>

        <section class="card" aria-labelledby="mv-psearch" style="overflow: hidden;">
          <div class="card-h"><h2 id="mv-psearch" class="sec-t">Останні пошуки з цього проєкту</h2><span class="grow"></span><a href="#/journal" style="font-size: 12.5px; font-weight: 550;">Усі в журналі →</a></div>
          ${sr && sr.searches.length ? html`
            <div class="scroll-x">
              <div class="thead" style="border-radius: 0; grid-template-columns: ${SEARCH_COLS};"><span>Час</span><span>Запит</span><span>Звідки</span><span>Маршрут</span><span class="right">мс</span><span class="right">Хітів</span><span class="right">Токенів</span><span>Канал</span></div>
              ${sr.searches.map((e) => {
                const d = describeEvent(e), ident = e.route === 'hybrid' && !/\s/.test(e.q || '');
                return html`<div class="trow" style="grid-template-columns: ${SEARCH_COLS};"><span class="date">${hm(e.ts)}</span>${ident ? html`<span class="mono ell" style="font-size: 12.5px; font-weight: 550;">${e.q}</span>` : html`<span class="ell">${e.q}</span>`}<span class="mono ell muted" style="font-size: 12px;">${d.proj || p.path}</span><span>${e.route ? routeBadge(e.route) : html`<span class="muted">—</span>`}</span><span class="num">${num(Math.round(e.ms || 0))}</span><span class="num">${num(e.results || 0)}</span><span class="num">≈${num(e.tokens || 0)}</span><span style="font-size: 12px;">${e.via === 'direct' ? 'напряму' : 'через демон'}</span></div>`;
              })}
            </div>
            <div class="tfoot" style="position: static;"><span class="tnum">${sr.searches.length} з ${num(sr.total_24h)} ${plural(sr.total_24h, 'пошуку', 'пошуків', 'пошуків')} за 24 год</span><span class="num">${sr.p50_ms == null ? '' : `p50 ${ms(sr.p50_ms)}`}</span></div>`
            : html`<div class="empty" style="padding: 28px 20px;"><span class="t">Пошуків за 24 год не було</span><span style="font-size: 12.5px;">Пошуки Claude та з цього інтерфейсу з’являться тут.</span></div>`}
        </section>
      </div>

      <aside class="stack side" style="gap: 16px;">
        <section class="card" aria-labelledby="mv-watch">
          <div class="card-h"><h2 id="mv-watch" class="sec-t">Стеження</h2></div>
          <div class="card-b stack" style="gap: 10px; font-size: 13px;">
            <div class="row between" style="gap: 12px;"><span class="muted">${g.daemon.watch_backend || 'Файлові події'}</span><span class="row" style="gap: 6px;">${dot(watching ? 't-success' : '')}${watching ? `стежить за ${p.repos.length} репо` : 'зупинено'}</span></div>
            <div class="row between" style="gap: 12px;"><span class="muted">Синк після змін</span><span>через ${dur(g.daemon.debounce_s || 10)} тиші</span></div>
            <div class="row between" style="gap: 12px;"><span class="muted">Страхувальний синк</span><span>${w ? html`кожні ${dur(w.safety_s)}${watching && w.next_safety ? html` · наступний <span class="mono">${hm(w.next_safety)}</span>` : ''}` : '—'}</span></div>
            <div class="row between" style="gap: 12px;"><span class="muted">Останній доступ</span><span>${p.last_access ? html`<span class="mono">${short(p.last_access)}</span>` : '—'}</span></div>
            <div class="row between" style="gap: 12px;"><span class="muted">Ембединги</span><span><span class="mono">${g.model}</span> · ${g.dims}-d</span></div>
          </div>
        </section>

        <section class="card" aria-labelledby="mv-secrets">
          <div class="card-h"><h2 id="mv-secrets" class="sec-t">Секрети в коді</h2></div>
          <div class="card-b stack" style="gap: 12px;">
            <div style="display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px;">
              <div class="stack" style="gap: 2px;"><span class="lbl">Замасковано</span><span class="tnum" style="font-size: 21px; font-weight: 650;">${num(sec.masked)}</span><span class="muted" style="font-size: 12px;">шматок є, значення <span style="white-space: nowrap;">→&nbsp;<span class="mono">•••</span></span></span></div>
              <div class="stack" style="gap: 2px;"><span class="lbl">Відкинуто</span><span class="tnum" style="font-size: 21px; font-weight: 650;">${num(sec.dropped)}</span><span class="muted" style="font-size: 12px;">шматок не зберігається</span></div>
            </div>
            ${sec.files.length ? html`<div class="stack" style="gap: 6px;"><span class="lbl">Де (поточні гілки)</span><ul class="stack" style="list-style: none; margin: 0; padding: 0; gap: 8px; font-size: 12.5px;">${sec.files.map(secRow)}</ul></div>` : ''}
            <span class="muted" style="font-size: 12px;">Відомі формати токенів і приватні ключі відкидаються повністю, решта значень маскується. Файли в репо не змінюються; самі значення ніде не показуються.</span>
          </div>
        </section>

        <section class="card" aria-labelledby="mv-excl">
          <div class="card-h"><h2 id="mv-excl" class="sec-t">Виключення</h2><span class="grow"></span><button type="button" class="btn sm" data-act="copy" data-copy="${det ? det.config_file : p.root + '/.myvector.toml'}" data-msg="Шлях скопійовано" ${fk('copy-config')}>${icon('copy', 13)}Шлях <span class="mono">.myvector.toml</span></button></div>
          ${ex ? html`<ul class="stack" style="list-style: none; margin: 0; padding: 14px 16px; gap: 8px; font-size: 12.5px;">
            ${exRow(ex.project, 'з .myvector.toml')}${exRow(cut(ex.dirs.map((d) => d + '/'), 8), 'типові')}${exRow(ex.gitignore, 'з .gitignore')}${exRow(ex.secrets, 'секрети')}${exRow(cut(ex.files, 6), 'типові файли')}
          </ul>` : html`<div class="card-b"><span class="skel" style="width: 60%;"></span></div>`}
        </section>
      </aside>
    </div>`}`;
}

// ---------------------------------------------------------------------------------------------- journal
const JCOLS = '84px 84px minmax(260px,1fr) minmax(290px,310px) minmax(240px,320px)';
const FILTERS = [['all', 'Усе'], ['sync', 'Синк'], ['search', 'Пошук'], ['error', 'Помилки'], ['watcher', 'Вотчер']];

export function filteredEvents(S) {
  const f = S.filter, q = f.q.trim().toLowerCase();
  return S.events.filter(isListed).filter((ev) => {
    if (f.type !== 'all' && !describeEvent(ev).cats.includes(f.type)) return false;
    if (f.project) {
      const p = ev.project || '';
      if (p !== f.project && !p.startsWith(f.project + '/')) return false;
    }
    if (q && !JSON.stringify(ev).toLowerCase().includes(q)) return false;
    return true;
  });
}

function liveProgress(S, ev) {
  if (ev.type !== 'sync_started') return null;
  let last = null;
  for (let i = S.events.length - 1; i >= 0 && S.events[i].id > ev.id; i--) {
    const e = S.events[i];
    if (e.project !== ev.project) continue;
    if (['sync_finished', 'sync_failed', 'sync_cancelled', 'sync_busy', 'sync_started'].includes(e.type)) return null;
    if (e.type === 'sync_progress' && !last) last = e;
  }
  return last;
}

function journalRow(S, ev) {
  const d = describeEvent(ev);
  const pr = liveProgress(S, ev);
  const projCell = d.proj ? html`<span class="ell" style="font-size: 12px;"><span class="mono">${d.proj}</span>${d.branch ? html` · <span class="mono">${d.branch}</span>` : ''}</span>` : html`<span class="muted">—</span>`;
  if (d.danger) {
    return html`<div class="trow" style="grid-template-columns: ${JCOLS}; align-items: start; padding-top: 6px; padding-bottom: 6px; background: var(--danger-bg);">
      <span class="date">${hms(ev.ts)}</span><span>${tagEl(d)}</span>
      <span class="stack" style="gap: 2px;"><span style="font-weight: 600;">${d.title}</span><span style="font-size: 12.5px; overflow-wrap: anywhere;">${ev.type === 'sync_failed' ? 'Нічого не втрачено: шматки цього синку не записано; наступний синк спробує знову.' : ''}</span></span>
      ${projCell}
      <span class="mono" style="font-size: 12px; overflow-wrap: anywhere;">${d.info}</span></div>`;
  }
  return html`<div class="trow" style="grid-template-columns: ${JCOLS};">
    <span class="date">${hms(ev.ts)}</span><span>${tagEl(d)}</span>
    <span class="ell">${d.title}</span>
    ${projCell}
    ${pr && pr.blobs_total
      ? html`<span class="row" style="gap: 8px; font-size: 12px;"><span class="prog" style="width: 80px;" role="progressbar" aria-label="Прогрес синку" aria-valuemin="0" aria-valuemax="${pr.blobs_total}" aria-valuenow="${pr.blobs_done}"><span style="width: ${pr.percent || 0}%;"></span></span><span class="mono tnum">${num(pr.blobs_done)} / ${num(pr.blobs_total)}</span></span>`
      : html`<span class="row wrap" style="gap: 2px 8px; font-size: 12px;">${routeBadge(d.route)}<span>${d.info}</span></span>`}
  </div>`;
}

function journalView(S) {
  const evs = S.events.filter(isListed), list = filteredEvents(S), shown = list.slice(-S.filter.limit).reverse();
  const live = liveBadge(S), paused = S.live.state === 'paused';
  const counts = Object.fromEntries(FILTERS.map(([k]) => [k, k === 'all' ? evs.length : evs.filter((e) => describeEvent(e).cats.includes(k)).length]));
  const projects = S.status ? nodes(S.status.projects).map((p) => p.path) : [];
  const now = nowS();
  let lastDay = '';
  const rows = shown.map((ev) => {
    const day = dayLabel(ev.ts, now);
    const head = day !== lastDay ? html`<div class="group-row">${day}</div>` : '';
    lastDay = day;
    return html`${head}${journalRow(S, ev)}`;
  });
  return html`
    ${header('Журнал', `Події демона · ${count(evs.length, W.events)} у пам’яті (останні 500) · повний журнал — logs/events.jsonl`, html`
      <span class="bdg nodot" style="gap: 6px;" role="status" aria-live="polite">${dot(live.dot)}${live.text}</span>
      <button type="button" class="btn" data-act="pause" aria-pressed="${String(paused)}" ${fk('pause')}>${paused ? 'Продовжити потік' : 'Призупинити потік'}</button>
      <button type="button" class="btn" data-act="export" ${evs.length ? '' : raw('disabled')} ${fk('export')}>${icon('download', 15)}Експортувати JSONL</button>`)}
    <div class="row wrap" style="gap: 10px 14px;">
      <div class="seg mv-seg" role="group" aria-label="Тип подій">${FILTERS.map(([k, label]) => html`<button type="button" class="${S.filter.type === k ? 'on' : ''}" aria-pressed="${String(S.filter.type === k)}" data-act="filter" data-v="${k}" ${fk(`filter:${k}`)}>${label} <span class="cnt">${num(counts[k])}</span></button>`)}</div>
      <div class="grow"></div>
      <div class="row" style="gap: 8px;"><label for="mv-jproj" style="font-size: 12px; font-weight: 550; color: var(--muted-foreground);">Проєкт</label>
        <select id="mv-jproj" class="inp" data-change="jproj" ${fk('jproj')}><option value="">Усі проєкти</option>${projects.map((p) => html`<option value="${p}" ${p === S.filter.project ? raw('selected') : ''}>${p}</option>`)}</select></div>
      <div class="row" style="gap: 8px;"><label for="mv-jq" style="font-size: 12px; font-weight: 550; color: var(--muted-foreground);">Текст</label>
        <input id="mv-jq" class="inp" type="search" placeholder="запит, шлях, код помилки" style="width: 220px;" value="${S.filter.q}" data-input="jq" ${fk('jq')}></div>
    </div>
    <section class="tbl" aria-label="Події">
      <div class="thead" style="grid-template-columns: ${JCOLS};"><span>Час</span><span>Тип</span><span>Подія</span><span>Проєкт</span><span>Деталі</span></div>
      ${rows.length ? rows : html`<div class="empty" style="min-width: 0;"><span class="t">${evs.length ? 'Нічого не знайдено' : 'Подій ще немає'}</span><span style="font-size: 12.5px;">${evs.length ? 'Змін фільтр або текст пошуку.' : 'Вони з’являться після першого пошуку чи синку.'}</span></div>`}
      <div class="tfoot" style="position: static;"><span class="tnum">Показано ${num(shown.length)} з ${num(list.length)} ${plural(list.length, 'події', 'подій', 'подій')}</span>${shown.length < list.length ? html`<button type="button" class="btn sm" data-act="more" ${fk('more')}>Показати ще 50</button>` : ''}</div>
    </section>`;
}

// ---------------------------------------------------------------------------------------------- memory
const isoDateTime = (s) => (s ? `${isoDate(s)} ${s.slice(11, 16)}` : '—');

const LINK_LINE = /^(Related|Supersedes):\s*\[\[/;
/** Memory text -> {title (first line), body lines without the generated link lines}. */
function splitMemory(text) {
  const lines = (text || '').split('\n').map((l) => l.replace(/\s+$/, ''));
  const body = lines.filter((l) => !LINK_LINE.test(l.trim()));
  while (body.length && !body[0].trim()) body.shift();
  const title = (body.shift() || '').replace(/^#+\s*/, '');
  while (body.length && !body[0].trim()) body.shift();
  while (body.length && !body[body.length - 1].trim()) body.pop();
  return { title, body };
}
const subtreeCount = (by, scope) => Object.entries(by).reduce((a, [s, n]) => a + (s === scope || s.startsWith(scope + '/') ? n : 0), 0);

function memRow(S, m) {
  const { title } = splitMemory(m.text);
  const on = S.mem.selectedId === m.id;
  const clip = title.length > 170 ? title.slice(0, 170) + '…' : title;
  return html`<li><button type="button" class="mv-mem ${on ? 'on' : ''}" ${on ? raw('aria-current="true"') : ''} data-act="memsel" data-v="${m.id}" ${fk(`mem:${m.id}`)} ${m.archived ? raw('style="color: var(--muted-foreground);"') : ''}>
    <span class="row wrap" style="gap: 6px; font-size: 12px;"><span class="mono">${isoDate(m.updated || m.created)}</span><span class="tag">${m.scope}</span>${m.branch
      ? html`<span class="bdg nodot t-primary pill">лише <span class="mono">${m.branch}</span></span>`
      : m.origin_branch ? html`<span class="row" style="gap: 4px; color: var(--muted-foreground);">${icon('branch', 12)}<span class="mono">${m.origin_branch}</span></span>` : ''}${m.pinned ? html`<span class="row" style="gap: 4px; color: var(--muted-foreground);">${icon('pin', 12)}закріплено</span>` : ''}${m.archived ? html`<span class="bdg pill">в архіві</span>` : ''}</span>
    <span style="font-weight: 600;">${inline(clip)}</span>
  </button></li>`;
}

function memDetail(S, m) {
  const g = S.status && S.status.global, vault = g && g.vault ? g.vault.path : null;
  const { title, body } = splitMemory(m.text);
  const busy = S.busy.has(`mem:${m.id}`);
  const btn = (act, ic, label) => html`<button type="button" class="btn sm" data-act="${act}" data-v="${m.id}" ${busy ? raw('disabled') : ''} ${fk(`${act}:${m.id}`)}>${icon(ic, 13)}${label}</button>`;
  const rel = m.vault_file && vault && m.vault_file.startsWith(vault + '/') ? 'vault/' + m.vault_file.slice(vault.length + 1) : m.vault_file;
  return html`
    <article class="card" aria-labelledby="mv-mem-title" style="flex: 1.4 1 460px; min-width: 0;">
      <div class="card-h" style="flex-wrap: wrap; row-gap: 8px;">
        <span class="lbl">Спогад</span><span class="grow"></span>
        <div class="row wrap" style="gap: 6px;">
          ${m.archived ? '' : html`${btn(m.pinned ? 'unpin' : 'pin', 'pin', m.pinned ? 'Відкріпити' : 'Закріпити')}${m.scope === 'global' ? '' : btn('promote', 'promote', 'Підняти в глобальну')}${btn('archive', 'archive', 'В архів')}`}
          ${m.vault_file ? html`<a class="btn soft sm" href="${obsidianFile(m.vault_file)}">${icon('external', 13)}Відкрити в Obsidian</a>` : ''}
        </div>
      </div>
      <div class="card-b stack" style="gap: 12px;">
        <h2 id="mv-mem-title" style="margin: 0; font-size: 15px; font-weight: 600; line-height: 1.35;">${inline(title)}</h2>
        ${body.length ? html`<div class="stack mv-md" style="gap: 8px; font-size: 13.5px; line-height: 1.55;">${markdown(body)}</div>` : ''}
        ${m.related.length ? html`<div class="stack" style="gap: 6px;"><span class="lbl">Пов’язані</span><div class="row wrap" style="gap: 6px 14px; font-size: 13px;">${m.related.map((r) => (r.id ? html`<a href="#/memory/${r.id}" style="font-weight: 550;">[[${r.stem}]]</a>` : html`<span class="mono" style="font-size: 12.5px;">[[${r.stem}]]</span>`))}</div></div>` : ''}
      </div>
      <div style="border-top: 1px solid var(--border);">
        <dl class="dl">
          <div><dt>id</dt><dd class="mono">${m.id}</dd></div>
          <div><dt>Скоуп</dt><dd class="mono">${m.scope}</dd></div>
          ${m.branch ? html`<div><dt>Видима лише на гілці</dt><dd class="mono">${m.branch}</dd></div>` : ''}
          <div><dt>Гілка походження</dt><dd class="mono">${m.origin_branch || '—'}</dd></div>
          <div><dt>Коміт</dt><dd class="mono">${m.commit || '—'}</dd></div>
          <div><dt>Створено</dt><dd class="mono">${isoDateTime(m.created)}</dd></div>
          <div><dt>Оновлено</dt><dd class="mono">${isoDateTime(m.updated)}</dd></div>
          ${m.tags.length ? html`<div><dt>Теги</dt><dd class="mono">${m.tags.join(', ')}</dd></div>` : ''}
          <div style="grid-column: 1 / -1;"><dt>Файл</dt><dd class="mono" style="overflow-wrap: anywhere;">${rel || 'немає (імпортовано з зовнішнього джерела)'}</dd></div>
        </dl>
      </div>
    </article>`;
}

function memoryView(S) {
  const M = S.mem, g = S.status && S.status.global;
  const by = g ? g.memories.by_scope : {};
  const scopes = Object.keys(by).filter((k) => k !== 'global').sort();
  const chip = (scope, label, n) => html`<button type="button" class="${M.scope === scope ? 'on' : ''}" aria-pressed="${String(M.scope === scope)}" data-act="memscope" data-v="${scope}" ${fk(`scope:${scope}`)}>${label} <span class="cnt">${num(n)}</span></button>`;
  const vault = g && g.vault;
  const sel = M.items && (M.items.find((m) => m.id === M.selectedId) || (M.sel && M.sel.id === M.selectedId ? M.sel : null));
  const inScope = M.scope ? (g ? subtreeCount(by, M.scope) : null) : (g ? g.memories.total : null);
  const sub = g ? html`${count(g.memories.total, W.memories)} · ${num(by.global || 0)} ${plural(by.global || 0, 'глобальний', 'глобальні', 'глобальних')} · сховище ${pathEl(vault.path, g.user_home, 40)} · ${vault.git ? html`git${vault.last_commit ? html`, останній коміт <span class="mono">${short(vault.last_commit)}</span>` : ''}` : 'без git'}` : 'Завантажуємо…';
  return html`
    ${header('Пам’ять', sub, g ? html`<a class="btn" href="${obsidianFile(vault.path)}">${icon('external', 15)}Відкрити сховище в Obsidian</a>` : '')}
    <div class="row wrap" style="gap: 10px 14px;">
      <div class="seg mv-seg" role="group" aria-label="Скоуп" style="flex-wrap: wrap; max-width: 100%;">
        ${g ? html`${chip('', 'Усі', g.memories.total)}${by.global != null ? chip('global', 'Глобальна', by.global) : ''}${scopes.map((s) => chip(s, html`<span class="mono">${s}</span>`, subtreeCount(by, s)))}` : ''}
      </div>
      <div class="grow"></div>
      <div class="row" style="gap: 8px;"><label for="mv-mq" style="font-size: 12px; font-weight: 550; color: var(--muted-foreground);">Пошук у пам’яті</label><input id="mv-mq" class="inp" type="search" placeholder="текст, id або гілка" style="width: 220px;" value="${M.q}" data-input="mq" ${fk('mq')}></div>
      <div class="row" style="gap: 8px;"><button type="button" class="sw ${M.archived ? 'on' : ''}" role="switch" aria-checked="${String(M.archived)}" aria-labelledby="mv-arch-l" data-act="memarch" ${fk('memarch')}></button><span id="mv-arch-l" style="font-size: 12.5px;">Показати архів</span></div>
    </div>
    ${M.err ? html`<div class="banner t-danger" role="status">${icon('alert', 15, 1.8)}<span class="grow">${M.err}</span></div>` : ''}
    <div class="cols">
      <section class="card" aria-label="Спогади" style="flex: 1 1 420px; min-width: 0; overflow: hidden;">
        ${M.items == null
          ? html`<div style="padding: 14px 16px;" class="stack" aria-busy="true"><span class="skel" style="width: 30%;"></span><span class="skel" style="width: 85%;"></span><span class="skel" style="width: 40%;"></span><span class="skel" style="width: 70%;"></span></div>`
          : M.items.length ? html`<ul style="list-style: none; margin: 0; padding: 0;">${M.items.map((m) => memRow(S, m))}</ul>`
            : html`<div class="empty"><span class="t">${M.q ? 'Нічого не знайдено' : 'Спогадів тут ще немає'}</span><span style="font-size: 12.5px;">${M.q ? 'Спробуй інший запит або скоуп.' : html`Додай перший: <span class="mono">myvec remember "…"</span>.`}</span></div>`}
        ${M.items ? html`<div class="tfoot" style="position: static;"><span class="tnum">${M.q ? `${num(M.items.length)} знайдено` : html`${num(M.items.length)}${inScope != null && !M.archived ? ` з ${num(inScope)}` : ''} у скоупі ${M.scope ? mono(M.scope) : 'усі'}`}</span>${M.items.length >= M.limit ? html`<button type="button" class="btn sm" data-act="memmore" ${fk('memmore')}>Показати ще</button>` : ''}</div>` : ''}
      </section>
      ${sel ? memDetail(S, sel) : M.items && M.items.length ? html`<article class="card" style="flex: 1.4 1 460px; min-width: 0;"><div class="empty"><span class="t">Спогад не вибрано</span><span style="font-size: 12.5px;">Вибери запис зі списку.</span></div></article>` : ''}
    </div>`;
}

// ---------------------------------------------------------------------------------------------- search
const WEIGHT_LABELS = [['current_repo', 'Поточне репо'], ['project_memory', 'Пам’ять проєкту'], ['sibling_repo', 'Сусіднє репо'], ['sibling_memory', 'Пам’ять сусіднього репо'], ['global_memory', 'Глобальна пам’ять'], ['other_project', 'Інший проєкт']];

function scoreCell(h) {
  const b = h.breakdown || {};
  const rank = (v) => (v == null ? '—' : `#${v}`);
  const we = b.weight_eff == null ? 1 : b.weight_eff;
  const pw = b.path_weight == null ? 1 : b.path_weight;
  const tw = pw === 1 ? '' : html` × вага шляху <b>${dec2(pw)}</b>`;
  // vector route: score = similarity x weight (x path weight: tests E8-20, tickets E8-21); hybrid (or no query vector): normalised RRF x weight
  const formula = b.route === 'vector' && b.similarity != null
    ? html`схожість <b>${dec2(b.similarity)}</b> × вага <b>${dec2(we)}</b>${tw} = <b>${dec3(h.score)}</b>`
    : html`RRF <b>${dec4(b.rrf)}</b> → <b>${dec2((b.rrf || 0) / (b.rrf_max || 1))}</b> × вага <b>${dec2(we)}</b>${tw} = <b>${dec3(h.score)}</b>`;
  return html`<div class="mv-score"><span>вектор <b>${rank(b.vector_rank)}</b></span><span>FTS <b>${rank(b.fts_rank)}</b></span><span>${formula}</span></div>`;
}

const shownHits = (res) => res.results.filter((h) => h.in_output !== false);
const shownN = (res) => shownHits(res).length;

function hitView(h, i) {
  const n = html`<span class="mono muted" style="font-size: 12px; width: 18px; flex: none;">${i}</span>`;
  if (h.kind === 'memory') {
    return html`<li class="stack" style="gap: 8px; padding: 14px 16px; border-bottom: 1px solid var(--border);">
      <div class="row wrap between" style="gap: 6px 16px; align-items: flex-start;">
        <div class="hit-l">${n}<span class="tag t-primary">пам’ять</span><a href="#/memory/${h.id}" style="font-size: 12.5px; font-weight: 600;"><span class="mono">${h.scope}</span><span aria-hidden="true" style="padding: 0 6px; font-weight: 400;">·</span><span class="mono">${h.id}</span></a><span class="muted" style="font-size: 12px;">${h.date ? isoDate(h.date) : ''}</span></div>
        ${scoreCell(h)}
      </div>
      <div class="mv-memtext">${h.shown}</div>
    </li>`;
  }
  const loc = `${h.repo}/${h.path}:${h.start}–${h.end}`;
  return html`<li class="stack" style="gap: 8px; padding: ${h.expanded ? '14px 16px' : '9px 16px'}; border-bottom: 1px solid var(--border);">
    <div class="row wrap between" style="gap: 6px 16px; align-items: flex-start;">
      <div class="hit-l">${n}<button type="button" class="linkbtn mono hit-path" style="font-weight: ${h.expanded ? 600 : 550};" data-act="copy" data-copy="${loc}" data-msg="Шлях скопійовано" title="Скопіювати шлях" ${fk(`hit:${i}`)}>${loc}</button>${h.symbol ? html`<span class="mono muted hit-sym" title="${h.symbol}">${h.symbol}</span>` : ''}</div>
      ${scoreCell(h)}
    </div>
    ${h.shown ? html`<pre class="mv-code ${h.expanded ? '' : 'mv-code-sm'}">${h.shown}</pre>` : ''}
  </li>`;
}

const USAGE_HREFS = { days: (_p, n) => `#/search?days=${n}` };
function searchView(S) {
  const Q = S.search, res = Q.result, st = S.status;
  const projects = st ? nodes(st.projects) : [];
  const proj = projects.find((p) => p.path === Q.project);
  const scopes = [['project', proj ? html`Проєкт <span class="mono">${proj.path}</span>` : 'Проєкт'], ['all', 'Усі проєкти'], ['memory', 'Лише пам’ять']];
  const weights = (res && res.weights) || (st && st.global.config.weights) || {};
  const sn = (res && res.weight_softness) ?? (st && st.global.config.weight_softness);
  const softness = sn == null ? 0.5 : sn;
  const recent = [];
  for (let i = S.events.length - 1; i >= 0 && recent.length < 6; i--) {
    const e = S.events[i];
    if (e.type === 'search' && e.q && !recent.some((r) => r.q === e.q)) recent.push(e);
  }
  const nRepos = proj ? proj.repos.length : 0;
  const scopeNote = Q.last
    ? { project: `скоуп: ${Q.last.project || 'без проєкту'}${nRepos ? `, ${nRepos} репо` : ''} + пам’ять`, all: `скоуп: ${count(projects.length, W.projects)} + пам’ять`, memory: 'скоуп: лише пам’ять' }[Q.last.scope]
    : '';
  const shown = res ? shownHits(res) : [];
  const nExp = shown.filter((h) => h.kind === 'code' && h.expanded).length;
  return html`
    ${header('Пошук', 'Та сама видача, що отримує Claude: короткі хіти, перші кілька розгорнуті, у межах бюджету токенів')}
    <form class="card card-b stack" role="search" aria-label="Пошук в індексі" data-form="search" style="gap: 12px;">
      <div class="row wrap" style="gap: 12px; align-items: flex-end;">
        <div class="field" style="flex: 999 1 320px;"><label for="mv-q">Запит</label><input id="mv-q" class="inp" type="search" value="${Q.q}" data-input="sq" autocomplete="off" style="height: 38px; font-size: 14px;" ${fk('sq')}></div>
        <div class="field" style="flex: 0 1 auto;"><label for="mv-sproj">Проєкт</label>
          <select id="mv-sproj" class="inp" data-change="sproj" style="height: 38px;" ${projects.length ? '' : raw('disabled')} ${fk('sproj')}>${projects.length ? projects.map((p) => html`<option value="${p.path}" ${p.path === Q.project ? raw('selected') : ''}>${p.path}</option>`) : html`<option>немає проєктів</option>`}</select></div>
        <div class="field" style="flex: 0 1 auto;"><span id="mv-scope-l" style="font-size: 12px; font-weight: 550; color: var(--muted-foreground);">Скоуп</span>
          <div class="seg mv-seg" role="group" aria-labelledby="mv-scope-l" style="flex-wrap: wrap;">${scopes.map(([k, label]) => html`<button type="button" class="${Q.scope === k ? 'on' : ''}" aria-pressed="${String(Q.scope === k)}" data-act="sscope" data-v="${k}" ${fk(`ss:${k}`)}>${label}</button>`)}</div></div>
        <button type="submit" class="btn primary ${Q.loading ? 'is-busy' : ''}" style="height: 38px;" ${fk('ssubmit')}>${icon('search', 15)}${Q.loading ? 'Шукаємо…' : 'Шукати'}</button>
      </div>
      ${res ? html`<div class="row wrap" style="gap: 6px 12px; font-size: 12.5px;">
        <span class="bdg nodot ${res.route === 'fts-fallback' ? 't-warning' : 't-primary'}">маршрут: ${res.route === 'vector' ? 'вектор (природна мова)' : res.route === 'fts-fallback' ? 'лише FTS (вектор недоступний)' : 'гібрид (вектор + FTS)'}</span>
        <span class="muted">${ms(res.ms)} · ${count(shownN(res), W.results)} · ≈${count(res.tokens, TOKENS)} · через демон${res.context ? html` · репо <span class="mono">${res.context.repo}</span>${noBranch(res.context.branch) ? '' : html` · гілка <span class="mono">${res.context.branch}</span>`}` : ''} · ${scopeNote}${res.index_refreshing ? ' · індекс оновлюється у фоні' : res.stale ? ' · індекс застарів' : ''}</span></div>` : ''}
    </form>
    ${Q.err ? html`<div class="banner t-danger" role="status">${icon('alert', 15, 1.8)}<span class="grow">${Q.err}</span></div>` : ''}
    <div class="cols">
      <section class="card main" aria-labelledby="mv-res" style="overflow: hidden;">
        <div class="card-h" style="flex-wrap: wrap; row-gap: 6px;"><h2 id="mv-res" class="sec-t">Що бачить Claude</h2><span class="hint">бал = схожість (або RRF) × вага скоупу</span><span class="grow"></span>
          ${res && shown.length ? html`<button type="button" class="btn sm" data-act="copyText" ${fk('copytext')}>${icon('copy', 13)}Скопіювати як текст</button>` : ''}</div>
        ${!res ? html`<div class="empty"><span class="t">${Q.loading ? 'Шукаємо…' : 'Введи запит'}</span><span style="font-size: 12.5px; max-width: 380px;">Результат з’явиться тут з розбивкою балів: ранг у векторному списку й FTS, схожість або RRF, вага скоупу.</span></div>`
          : shown.length ? html`<ol style="list-style: none; margin: 0; padding: 0;">${shown.map((h, i) => hitView(h, i + 1))}</ol>
            <div class="tfoot" style="position: static;"><span class="tnum">${count(shown.length, ['хіт', 'хіти', 'хітів'])} · ${num(nExp)} ${plural(nExp, 'розгорнутий', 'розгорнуті', 'розгорнутих')} · ≈${count(res.tokens, TOKENS)}</span><span class="num">${ms(res.ms)}</span></div>`
            : html`<div class="empty"><span class="t">Нічого не знайдено</span><span style="font-size: 12.5px;">Спробуй інше формулювання, ширший скоуп або точний ідентифікатор.</span></div>`}
      </section>
      <aside class="stack side" style="gap: 16px; flex-basis: 360px;">
        ${recent.length ? html`<section class="card" aria-labelledby="mv-recent"><div class="card-h"><h2 id="mv-recent" class="sec-t">Останні запити</h2></div>
          <ul class="stack" style="list-style: none; margin: 0; padding: 10px 16px 14px; gap: 6px;">${recent.map((e) => html`<li class="row between" style="gap: 10px;"><button type="button" class="linkbtn" style="overflow-wrap: anywhere; font-size: 12.5px;" data-act="rerun" data-v="${e.q}" ${fk(`rerun:${e.id}`)}>${e.q}</button><span class="muted" style="font-size: 11.5px; white-space: nowrap;">${hm(e.ts)}</span></li>`)}</ul></section>` : ''}
        <section class="card" aria-labelledby="mv-how">
          <div class="card-h"><h2 id="mv-how" class="sec-t">Як рахується бал</h2></div>
          <div class="card-b stack" style="gap: 10px; font-size: 12.5px;">
            <p style="margin: 0;">Один токен без пробілів (<span class="mono">calcVatEu</span>, <span class="mono">Foo::bar</span>) — гібрид: вектор і FTS5, злиті через RRF. Речення — лише вектор.</p>
            <p style="margin: 0;">Вектор: бал = косинусна схожість × вага. Гібрид: RRF = сума <span class="mono">1 / (60 + ранг)</span> по списках, поділена на максимум <span class="mono">2 / 61</span> (виходить 0…1), × вага. Код і пам’ять рахуються на одній шкалі та зливаються за балом.</p>
            <p style="margin: 0;">Вага м’якшає до одиниці: <span class="mono">1 − ${dec1(softness)} × (1 − w)</span>, тому вона лише розрізняє близькі хіти, а не перекриває схожість.</p>
            <div class="stack" style="gap: 6px;">${WEIGHT_LABELS.filter(([k]) => weights[k] != null).map(([k, label]) => html`<div class="row between"><span class="muted">${label}</span><span class="mono">${dec1(weights[k])} → ${dec2(1 - softness * (1 - weights[k]))}</span></div>`)}</div>
          </div>
        </section>
      </aside>
    </div>
    ${adoption.card(S.usage[S.route.days || 14] || null, { days: S.route.days || 14, hrefs: USAGE_HREFS })}`;
}

// ---------------------------------------------------------------------------------------------- settings
function settingsView(S) {
  const g = S.status && S.status.global;
  if (!g) return html`${header('Налаштування', 'Завантажуємо…')}<section class="card card-b stack" aria-busy="true"><span class="skel" style="width: 40%;"></span><span class="skel" style="width: 70%;"></span></section>`;
  const o = g.ollama, d = g.daemon, c = g.config, v = g.vault;
  const row = (k, val, wide = false) => html`<div ${wide ? raw('style="grid-column: 1 / -1;"') : ''}><dt>${k}</dt><dd>${val}</dd></div>`;
  return html`
    ${header('Налаштування', html`Лише перегляд · змінюється у ${pathEl(g.home + '/config.toml', g.user_home, 200)}`)}
    <div class="cols">
      <section class="card" aria-labelledby="mv-set-emb" style="flex: 1 1 380px; min-width: 0;">
        <div class="card-h"><h2 id="mv-set-emb" class="sec-t">Ембединги</h2></div>
        <dl class="dl">
          ${row('Модель', html`<span class="mono">${g.model || c.model}</span>`)}
          ${row('Розмірність', html`<span class="mono">${num(g.dims || c.dims)}</span>`)}
          ${row('Ollama', html`<span class="mono">${o.url}</span>`, true)}
          ${row('Стан Ollama', o.reachable == null ? 'не перевіряється (тестовий ембедер)' : html`<span class="row" style="gap: 6px;">${dot(o.reachable ? 't-success' : 't-danger')}${o.reachable ? 'відповідає' : 'не відповідає'}</span>`)}
          ${row('Результатів у видачі (k)', html`<span class="mono">${c.k}</span>`)}
          ${g.model_mismatch ? row('Індекс', html`зібрано з <span class="mono">${g.model}</span> (${g.dims}-d) — потрібен <span class="mono">myvec reembed</span>`, true) : ''}
        </dl>
      </section>
      <section class="card" aria-labelledby="mv-set-data" style="flex: 1 1 380px; min-width: 0;">
        <div class="card-h"><h2 id="mv-set-data" class="sec-t">Дані й демон</h2></div>
        <dl class="dl">
          ${row('Вікно активності за замовчуванням', html`<span class="mono">${hours(c.window_hours)}</span>`)}
          ${row('Порт демона', html`<span class="mono">${d.port}</span>`)}
          ${row('Каталог даних', html`<span style="overflow-wrap: anywhere;">${pathEl(g.home, g.user_home, 200)}</span>`, true)}
          ${row('Сховище пам’яті', html`<span style="overflow-wrap: anywhere;">${pathEl(v.path, g.user_home, 200)}</span>`, true)}
          ${row('Git сховища', v.git ? html`так${v.last_commit ? html` · останній коміт <span class="mono">${dmyhm(v.last_commit)}</span>` : ''}` : 'ні')}
          ${row('Розмір індексу', html`<span class="mono">${bytes(g.db_size_bytes)}</span>`)}
          ${row('Процес демона', html`pid <span class="mono">${d.pid}</span> · запущено <span class="mono">${dmyhm(d.started)}</span>`, true)}
        </dl>
      </section>
    </div>`;
}
