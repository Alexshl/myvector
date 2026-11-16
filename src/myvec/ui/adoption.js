// Card «Використання пошуку агентами» on the «Пошук» page (E8-06, ADR-037): myvec vs grep/find per project.
// Pure like spend.js / monitor.js: data -> html strings, no network, DOM, URL or storage access.
// Loading, polling and the route live in app.js.
import { html, raw, num, plural, bytes } from './util.js';
import { periodSeg } from './spend.js';

const NO_PROJECT = '(поза проєктами)';
const ZERO = ['нульова видача', 'нульові видачі', 'нульових видач'];

/** Whole percent of n/d: `—` when d is 0 / not a number, `0%`, `< 1%` below one percent. */
export function pct(n, d) {
  if (typeof d !== 'number' || !(d > 0) || typeof n !== 'number') return '—';
  if (n <= 0) return '0%';
  const p = 100 * n / d;
  return p < 1 ? '< 1%' : `${Math.round(p)}%`;
}

/** Copy sorted by grep/find calls desc, then myvec desc, then project asc. */
export function rows(projects) {
  return [...(projects || [])].sort((a, b) => (b.self - a.self) || (b.myvec - a.myvec) || (a.project < b.project ? -1 : a.project > b.project ? 1 : 0));
}

const head = (days, hrefs) => html`<div class="card-h" style="flex-wrap: wrap; row-gap: 6px;"><h2 id="mv-adopt-h" class="sec-t">Використання пошуку агентами</h2><span class="hint">myvec проти grep/find · з транскриптів, без тексту запитів</span><span class="grow"></span>${periodSeg('', days, hrefs)}</div>`;
const shell = (days, hrefs, body) => html`<section class="card adopt" aria-labelledby="mv-adopt-h" style="overflow: hidden;">${head(days, hrefs)}${body}</section>`;

// E8-16: with `share_explore` (myvec / (myvec + exploratory grep/find)) the bar and number show it, else the old share.
const share = (r) => {
  const hasX = 'share_explore' in r;
  const t = hasX ? 1 : r.myvec + r.self, n = hasX ? (r.share_explore || 0) : r.myvec;
  const none = hasX && typeof r.share_explore !== 'number';
  const w = none ? 0 : (hasX ? 100 * n : (t > 0 ? 100 * n / t : 0));
  const label = none ? '—' : pct(n, t);
  return html`<span class="pcell"><span class="prog"><span style="width: ${Math.max(w, n > 0 ? 2 : 0).toFixed(1)}%;"></span></span><span class="n">${label}</span></span>`;
};
const missCell = (r) => html`<span class="num right ${r.myvec > 0 && Math.round(100 * r.miss / r.myvec) > 50 ? 't-warning' : ''}">${pct(r.miss, r.myvec)}</span>`;
const cells = (r) => html`<span class="num right">${num(r.sessions_myvec)} <span class="of">/ ${num(r.sessions)}</span></span><span class="num right">${num(r.myvec)}</span><span class="num right">${num(r.self)}</span>${share(r)}${missCell(r)}<span class="num right">${pct(r.self_sub, r.self)}</span>`;
const row = (r) => html`<div class="trow"><span class="mono ell ${r.project === NO_PROJECT ? 'muted' : ''}" title="${r.project}">${r.project}</span>${cells(r)}</div>`;

/** data = /api/search/usage payload | {err} | null (loading); ui = {days, hrefs: {days(path, n)}}. */
export function card(data, ui = {}) {
  const days = ui.days || 14, hrefs = ui.hrefs;
  if (!data) return shell(days, hrefs, html`<div class="card-b stack" aria-busy="true"><span class="skel" style="width: 40%;"></span><span class="skel" style="width: 70%;"></span><span class="skel" style="width: 55%;"></span></div>`);
  if (data.err) return shell(days, hrefs, html`<div class="empty"><span class="t">Не вдалося отримати статистику</span><span style="font-size: 12.5px;">${data.err}</span><button type="button" class="btn sm" data-act="usageretry">Повторити</button></div>`);
  const col = data.collector || {};
  if (col.enabled === false) {
    return shell(days, hrefs, html`<div class="empty"><span class="t">Збір вимкнено</span><span style="font-size: 12.5px; max-width: 420px;">${raw('Увімкни <span class="mono">tokens_enabled = true</span> у <span class="mono">config.toml</span> і перезапусти демон (<span class="mono">myvec install-daemon</span>).')}</span></div>`);
  }
  const list = rows(data.projects);
  if (!list.length) {
    return shell(days, hrefs, html`<div class="empty"><span class="t">Пошуків і grep за період не було</span><span style="font-size: 12.5px;">Цифри з’являться після сесій Claude Code у проєктах з індексом.</span></div>`);
  }
  const T = data.total || {};
  const banner = col.backfill ? html`<div style="padding: 10px 16px 0;"><div class="banner t-info" role="status"><span class="grow">Транскрипти ще читаються (≈ ${bytes(col.pending_bytes)}) — цифри неповні й зростатимуть.</span></div></div><div style="height: 10px;"></div>` : '';
  const p50 = typeof T.p50_ms === 'number' ? html`<span class="num">пошуки myvec: p50 ${num(Math.round(T.p50_ms))} мс · ${num(T.zero_results || 0)} ${plural(T.zero_results || 0, ...ZERO)}</span>` : html`<span class="num">${num(T.zero_results || 0)} ${plural(T.zero_results || 0, ...ZERO)}</span>`;
  const ms = (v) => typeof v === 'number' ? `${num(Math.round(v))} мс` : '—';
  const lat = (typeof T.p50_ms_24h !== 'number' && typeof T.p50_ms !== 'number') ? '' : html`<span class="num">p50 доба / ${num(days)} ${plural(days, 'день', 'дні', 'днів')}: ${ms(T.p50_ms_24h)} / ${ms(T.p50_ms)}</span>`;
  const expl = typeof T.sessions_explore === 'number' ? html`<span class="num">Сесій з myvec серед розвідувальних: ${num(T.sessions_myvec_explore || 0)} / ${num(T.sessions_explore)}</span>` : '';
  const skipped = (T.pre_onset || T.foreign) ? html`<span class="muted">не враховано: ${num(T.pre_onset || 0)} до відліку, ${num(T.foreign || 0)} з іншої ОС</span>` : '';
  return shell(days, hrefs, html`${banner}<div class="scroll-x">
    <div class="thead" style="border-radius: 0;"><span>Проєкт</span><span class="right" title="сесії з myvec / усі сесії">Сесій</span><span class="right">myvec</span><span class="right">grep/find</span><span title="myvec / (myvec + розвідувальні grep/find); без grep у файлах, логах, підрахунків і пошуку за іменем">Частка myvec</span><span class="right" title="частка викликів myvec, після яких був grep/find">Промахи</span><span class="right" title="частка grep/find у субагентах">У субагентах</span></div>
    ${list.map(row)}
    <div class="trow tot"><span>Усього</span>${cells(T)}</div>
  </div>
  <div class="tfoot"><span>Сесій: з myvec / усього</span>${p50}${lat}${expl}${skipped}</div>`);
}
