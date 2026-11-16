// Node load page and project «Тести» tab (E6-05, ADR-015). Pure like tracker.js: data -> html / svg strings.
// No network, DOM, URL or storage access: measuring widths, listeners and polling live in app.js.
import {
  html, raw, esc, num, dec1, dec2, bytes, hm, durS, cores, icon, pct, plural, short,
} from './util.js';

const GB = 1024 ** 3;
const gb = (b) => dec1(b / GB);
/** [value, unit] of a byte count in the unit `bytes()` picks (МБ below 1 ГБ). */
const bytesParts = (b) => { const t = bytes(b), i = t.lastIndexOf(' '); return [t.slice(0, i), t.slice(i + 1)]; };
const p2 = (x) => String(x).padStart(2, '0');
const ddmm = (ts) => { const d = new Date(ts * 1000); return `${p2(d.getDate())}.${p2(d.getMonth() + 1)}`; };
const ddmmhm = (ts) => `${ddmm(ts)} ${hm(ts)}`;
const when = (ts, now) => (ts == null ? '—' : short(ts, now) === hm(ts) ? hm(ts) : ddmmhm(ts));
const median = (xs) => { const s = xs.filter((x) => x != null).sort((a, b) => a - b); return s.length ? s[Math.floor(s.length / 2)] : null; };

export const PERIODS = [['1h', '1 год'], ['24h', '24 год'], ['30d', '30 днів']];
const PERIOD_SPAN = { '1h': 3600, '24h': 86400, '30d': 30 * 86400 };
const PERIOD_TITLE = { '1h': 'за годину', '24h': 'за 24 години', '30d': 'за 30 днів' };

const OUTCOME = {
  running: ['', 'триває'], passed: ['t-success', 'пройшли'], failed: ['t-danger', 'впали'],
  interrupted: ['t-warning', 'перервано'], ended: ['', 'завершено'], lost: ['', 'втрачено'],
};
const SOURCE = { wrapper: 'обгортка', passive: 'пасивно' };
const sourceLabel = (s) => SOURCE[s] || s;

// ------------------------------------------------------------------------------------------------ chart geometry
/** A "nice" tick step (1, 2, 2.5, 5 x 10^n) giving about `target` ticks up to `max`. */
export function niceStep(max, target = 4) {
  if (!(max > 0)) return 1;
  const raw0 = max / target, mag = 10 ** Math.floor(Math.log10(raw0)), f = raw0 / mag;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * mag;
}
const ticksTo = (max, step) => { const out = []; for (let v = 0; v <= max + 1e-9; v += step) out.push(+v.toFixed(6)); return out; };
const tickText = (v) => num(v);

/** Index ranges [from, to] of consecutive points; a hole longer than 2 x step starts a new segment. */
export function segments(points, stepS) {
  const out = [];
  let from = 0;
  for (let i = 1; i <= points.length; i++) {
    if (i === points.length || (stepS > 0 && points[i].ts - points[i - 1].ts > 2 * stepS)) {
      if (i > from) out.push([from, i - 1]);
      from = i;
    }
  }
  return out;
}

/** Geometry of the node CPU line: pixel mapping, ticks, segments. */
export function lineGeom(points, { w = 600, h = 200, threshold = null, step_s: stepS = 0, span = null, now = null, ymax = 100 } = {}) {
  const L = 36, R = 12, T = 8, B = 22;
  const n = points.length;
  let t0 = span != null && now != null ? now - span : n ? points[0].ts : 0;
  let t1 = span != null && now != null ? now : n ? points[n - 1].ts : 1;
  if (t1 <= t0) t1 = t0 + 1;
  const iw = Math.max(1, w - L - R), ih = Math.max(1, h - T - B);
  const x = (ts) => L + iw * Math.min(1, Math.max(0, (ts - t0) / (t1 - t0)));
  const y = (v) => T + ih * (1 - Math.min(ymax, Math.max(0, v)) / ymax);
  const xs = points.map((p) => x(p.ts));
  return {
    kind: 'line', w, h, L, R, T, B, iw, ih, x, y, xs, t0, t1, n, ymax, threshold, stepS,
    segs: segments(points, stepS),
    yTicks: ymax === 100 ? [0, 25, 50, 75, 100] : ticksTo(ymax, niceStep(ymax)),
    at: (i) => ({ x: xs[i], y: y(points[i].cpu_pct) }),
  };
}

/** Geometry of the run bars (chronological order, one slot per run). */
export function barsGeom(vals, { w = 400, h = 170, tick = null } = {}) {
  const L = 36, R = 8, T = 12, B = 22;
  const n = vals.length;
  const iw = Math.max(1, w - L - R), ih = Math.max(1, h - T - B);
  const top = Math.max(0, ...vals.map((v) => v || 0));
  const max = top > 0 ? top * 1.12 : 1;
  const step = tick || niceStep(max);
  const y = (v) => T + ih * (1 - Math.min(max, Math.max(0, v)) / max);
  const slot = iw / Math.max(1, n), bw = Math.min(28, Math.max(2, slot - 2));
  const cx = (i) => L + i * slot + slot / 2;
  return {
    kind: 'bars', w, h, L, R, T, B, iw, ih, y, n, slot, bw, cx, max, step, yTicks: ticksTo(max, step),
    at: (i) => ({ x: cx(i), y: y(vals[i] || 0) }),
  };
}

/** Index of the point/bar under the pointer; `xPx` is relative to the left edge of the svg. -1 for an empty chart. */
export function hitIndex(xPx, geom) {
  if (!geom || !geom.n) return -1;
  if (geom.kind === 'bars') return Math.max(0, Math.min(geom.n - 1, Math.floor((xPx - geom.L) / geom.slot)));
  let best = 0, bd = Infinity;
  for (let i = 0; i < geom.xs.length; i++) {
    const d = Math.abs(geom.xs[i] - xPx);
    if (d < bd) { bd = d; best = i; }
  }
  return best;
}

const grid = (g, fmt) => g.yTicks.map((v) => `<line x1="${g.L}" x2="${g.w - g.R}" y1="${g.y(v).toFixed(1)}" y2="${g.y(v).toFixed(1)}" stroke="var(--border)" stroke-width="1"/><text x="${g.L - 6}" y="${(g.y(v) + 3.5).toFixed(1)}" text-anchor="end">${esc(fmt(v))}</text>`).join('');
const svgWrap = (g, label, body) => `<svg width="${g.w}" height="${g.h}" viewBox="0 0 ${g.w} ${g.h}" role="img" aria-label="${esc(label)}">${body}<g class="hov" style="display: none;"><line y1="${g.T}" y2="${g.h - g.B}" stroke="var(--faint-foreground)" stroke-width="1"/><circle r="4" fill="var(--brand-primary)" stroke="var(--surface)" stroke-width="2"/></g><rect class="hit" x="${g.L}" y="0" width="${g.iw}" height="${g.h}" fill="transparent"/></svg>`;

/** CPU line chart as an svg string; `points` = [{ts, cpu_pct}] from /api/load/series. */
export function lineSvg(points, opts = {}) {
  const g = lineGeom(points, opts);
  const { label = 'Завантаження CPU', period = '24h' } = opts;
  let body = grid(g, (v) => `${v}%`);
  const nLab = g.w < 520 ? 4 : 6;
  for (let k = 0; k < nLab; k++) {
    const ts = g.t0 + (g.t1 - g.t0) * k / (nLab - 1);
    const anchor = k === 0 ? 'start' : k === nLab - 1 ? 'end' : 'middle';
    body += `<text x="${g.x(ts).toFixed(1)}" y="${g.h - 4}" text-anchor="${anchor}">${esc(period === '30d' ? ddmm(ts) : hm(ts))}</text>`;
  }
  if (g.threshold != null) {
    // the label sits at the left edge: the freshest point is on the right and never collides with it
    body += `<line x1="${g.L}" x2="${g.w - g.R}" y1="${g.y(g.threshold).toFixed(1)}" y2="${g.y(g.threshold).toFixed(1)}" stroke="var(--danger)" stroke-width="1.5" stroke-dasharray="4 4"/>`
      + `<text x="${g.L + 6}" y="${(g.y(g.threshold) - 5).toFixed(1)}" text-anchor="start" style="fill: var(--danger);">поріг ${esc(num(g.threshold))}%</text>`;
  }
  for (const [a, b] of g.segs) {
    if (a === b) { body += `<circle cx="${g.xs[a].toFixed(1)}" cy="${g.y(points[a].cpu_pct).toFixed(1)}" r="2" fill="var(--brand-primary)"/>`; continue; }
    const pts = [];
    for (let i = a; i <= b; i++) pts.push(`${g.xs[i].toFixed(1)},${g.y(points[i].cpu_pct).toFixed(1)}`);
    body += `<polygon points="${g.xs[a].toFixed(1)},${g.y(0)} ${pts.join(' ')} ${g.xs[b].toFixed(1)},${g.y(0)}" fill="var(--brand-primary)" fill-opacity="0.12"/>`
      + `<polyline points="${pts.join(' ')}" fill="none" stroke="var(--brand-primary)" stroke-width="2" stroke-linejoin="round"/>`;
  }
  if (g.n) body += `<circle cx="${g.xs[g.n - 1].toFixed(1)}" cy="${g.y(points[g.n - 1].cpu_pct).toFixed(1)}" r="4" fill="var(--brand-primary)" stroke="var(--surface)" stroke-width="2"/>`;
  return svgWrap(g, label, body);
}

const barColor = (r) => (r.outcome === 'failed' ? 'var(--danger)' : r.regression ? 'var(--warning)' : 'var(--brand-primary)');

/** X-axis label of the labeled bar: the date when the day changes from the previously labeled bar (or for the first), else the time. */
export const barLabel = (ts, prevTs) => (prevTs == null || ddmm(ts) !== ddmm(prevTs) ? ddmm(ts) : hm(ts));

/** Bars svg: `runs` chronological, `val(run)` -> number; failed = danger, run.regression = warning with "!". */
export function barsSvg(runs, val, opts = {}) {
  const vals = runs.map((r) => val(r) || 0);
  const g = barsGeom(vals, opts);
  const { label = 'Прогони', fmtTick = tickText } = opts;
  let body = grid(g, fmtTick);
  const every = Math.max(1, Math.ceil(g.n / Math.max(1, Math.floor(g.iw / 52))));
  let prevLabeled = null;
  runs.forEach((r, i) => {
    const x0 = g.cx(i) - g.bw / 2, top = g.y(vals[i]), h = Math.max(1, g.y(0) - top), rr = Math.min(3, h);
    body += `<path d="M${x0.toFixed(1)},${g.y(0).toFixed(1)} v${(-(h - rr)).toFixed(1)} q0,${-rr} ${rr},${-rr} h${(g.bw - 2 * rr).toFixed(1)} q${rr},0 ${rr},${rr} v${(h - rr).toFixed(1)} z" fill="${barColor(r)}"/>`;
    if (r.regression) body += `<text x="${g.cx(i).toFixed(1)}" y="${(top - 4).toFixed(1)}" text-anchor="middle" style="fill: var(--warning); font-weight: 650;">!</text>`;
    if (i % every === 0) {
      body += `<text x="${g.cx(i).toFixed(1)}" y="${g.h - 4}" text-anchor="middle">${esc(barLabel(r.started_at, prevLabeled))}</text>`;
      prevLabeled = r.started_at;
    }
  });
  return svgWrap(g, label, body);
}

// ------------------------------------------------------------------------------------------------ charts by name
const finishedRuns = (runs) => (runs || []).filter((r) => (r.outcome === 'passed' || r.outcome === 'failed' || r.outcome === 'ended') && r.wall_s != null).slice(0, 30).reverse();
export const chartRuns = finishedRuns;

const runTip = (r, fmt) => html`${when(r.started_at, Date.now() / 1000)} · <b>${fmt}</b>${r.regression ? ' · регресія' : ''}${r.outcome === 'failed' ? ' · впали' : ''}<br><span style="color: var(--muted-foreground);">${r.ticket_id || 'без тікета'}${r.agent ? ` · ${r.agent}` : ''} · ${sourceLabel(r.source)}</span>`;

/**
 * One chart by data-chart name: 'cpu' (data.series), 'dur' or 'cput' (data.runs, newest first as the API returns them).
 * Returns {svg, geom, tip(i)} where tip gives the html of the hover tooltip.
 */
export function chart(name, data, ui, { w, h }) {
  if (name === 'cpu') {
    const s = data.series || { points: [] }, pts = s.points || [], period = ui.period || '24h';
    const last = pts.length ? pts[pts.length - 1].cpu_pct : null;
    const opts = { w, h, threshold: ui.alertPct ?? 85, step_s: s.step_s || 0, span: PERIOD_SPAN[period], now: s.now ?? null, period,
      label: `Завантаження CPU ${PERIOD_TITLE[period] || ''}${last != null ? `, зараз ${num(Math.round(last))}%` : ''}` };
    const geom = lineGeom(pts, opts);
    return {
      svg: lineSvg(pts, opts), geom,
      tip: (i) => {
        const p = pts[i];
        if (!p) return '';
        const ts = period === '1h' ? hm(p.ts) : ddmmhm(p.ts);
        return String(html`${ts} · CPU <b>${pct(p.cpu_pct)}</b>${p.cpu_max != null && p.cpu_max > p.cpu_pct + 0.5 ? html` · макс. ${pct(p.cpu_max)}` : ''}${p.load1 != null ? html`<br><span style="color: var(--muted-foreground);">load ${dec2(p.load1)}</span>` : ''}`);
      },
    };
  }
  const runs = finishedRuns(data.runs);
  const isDur = name === 'dur';
  const val = isDur ? (r) => r.wall_s : (r) => (r.cpu_s || 0) / 60;
  const fmt = isDur ? (v) => durS(v) : (v) => `${dec1(v)} хв`;
  const opts = { w, h, label: `${isDur ? 'Тривалість' : 'CPU-час'} останніх ${num(runs.length)} прогонів`, fmtTick: (v) => num(v) };
  return { svg: barsSvg(runs, val, opts), geom: barsGeom(runs.map(val), opts), tip: (i) => (runs[i] ? String(runTip(runs[i], fmt(val(runs[i])))) : '') };
}

// ------------------------------------------------------------------------------------------------ shared blocks
const kpi = (label, value, unit, sub, hot = false) => html`<section class="card kt"><span class="lbl">${label}</span><span class="v ${hot ? 'hot' : ''}">${value}${unit ? html`<small>${unit}</small>` : ''}</span><span class="s">${sub}</span></section>`;

/** Big-number form of a duration: "41,8" (unit 'с' added by the caller) or "3 <small>хв</small> 12 <small>с</small>". */
const durParts = (sec) => {
  if (sec < 60) return dec1(sec);
  const t = Math.round(sec), h = Math.floor(t / 3600), m = Math.floor(t / 60) % 60, s = t % 60;
  return h ? html`${num(h)}<small>год</small> ${num(m)}<small>хв</small>` : html`${num(m)}<small>хв</small> ${num(s)}<small>с</small>`;
};

/** "тікет · агент" or "без тікета". */
const ticketAgent = (r) => (r.ticket_id ? `${r.ticket_id}${r.agent ? ` · ${r.agent}` : ''}` : r.agent ? `без тікета · ${r.agent}` : 'без тікета');

function outcomeBadge(r, now) {
  if (r.outcome === 'running') return html`<span class="live">триває ${durS(Math.max(0, now - r.started_at))}</span>`;
  const [cls, text] = OUTCOME[r.outcome] || ['', r.outcome];
  return html`<span class="bdg ${cls}">${text}</span>`;
}

/** One run row for the «Навантаження» page. */
export function runRow(r, ui) {
  const now = ui.now ?? Date.now() / 1000;
  const parts = [];
  if (r.cpu_s != null) parts.push(`CPU-час ${durS(r.cpu_s)}${r.avg_cores != null ? ` · сер. ${cores(r.avg_cores)}` : ''}`);
  else if (r.avg_cores != null) parts.push(`сер. ${cores(r.avg_cores)}`);
  if (r.peak_cores != null) parts.push(`пік ${dec1(r.peak_cores)}`);
  if (r.peak_rss_bytes != null) parts.push(`пам’ять ${bytes(r.peak_rss_bytes)}`);
  const g = r.outcome === 'running' && r.regression && typeof r.regression === 'object' ? r.regression : null;
  const med = g && g.median_wall_s != null && g.n ? `медіана ${num(g.n)} ${plural(g.n, 'прогін', 'прогони', 'прогонів')} ${durS(g.median_wall_s)}` : null;
  const href = ui.projectHref && r.project_path ? ui.projectHref(r.project_path) : null;
  const proj = r.project_path ? (href ? html`<a class="mono" style="font-weight: 600;" href="${href}">${r.project_path}</a>` : html`<span class="mono" style="font-weight: 600;">${r.project_path}</span>`) : html`<span class="muted">без проєкту</span>`;
  return html`<div class="run">
    <span class="row wrap" style="gap: 8px; min-width: 0;">${outcomeBadge(r, now)}${proj}<span class="mono muted ell">${r.command || r.label || ''}</span></span>
    ${r.outcome === 'running' ? (r.regression ? regMark(r) : '') : html`<span class="tnum">${durS(r.wall_s)}</span>`}
    <span class="meta">${parts.map((p) => html`<span>${p}</span>`)}<span>${ticketAgent(r)}</span><span class="tag">${sourceLabel(r.source)}</span>${med ? html`<span>${med}</span>` : ''}<span>${when(r.started_at, now)}</span></span>
  </div>`;
}
/** Label text of a regression: the API object (ADR-018) says how much longer / heavier than the median. */
function regText(g) {
  if (typeof g === 'string') return g;
  if (g && typeof g === 'object') {
    if (g.wall_pct != null) return `довше за медіану на ${num(g.wall_pct)}%`;
    if (g.cpu_pct != null) return `CPU більше за медіану на ${num(g.cpu_pct)}%`;
  }
  return 'регресія';
}
const regMark = (r) => html`<span class="reg">${icon('warn', 13, 1.8)}${regText(r.regression)}</span>`;

/**
 * Sustained-CPU banner (ADR-018): alert = `alert` of /api/load or /api/status (null = none), ui = {alertPct, projectHref(path)}.
 * Parts without data are left out; the button leads to the «Тести» tab of the culprit project.
 */
export function alertBanner(alert, ui = {}) {
  if (!alert) return '';
  const run = alert.run || null;
  const what = (run && (run.command || run.label)) || alert.top_name || '';
  const det = [alert.cores != null ? `≈${cores(alert.cores)}` : null, run && run.ticket_id ? `тікет ${run.ticket_id}` : null, run && run.agent ? `агент ${run.agent}` : null].filter(Boolean).join(', ');
  const who = alert.project || what
    ? html` Найбільше їсть ${alert.project ? html`<span class="mono">${alert.project}</span>` : ''}${alert.project && what ? ' · ' : ''}${what ? html`<span class="mono">${what}</span>` : ''}${det ? ` (${det})` : ''}.`
    : '';
  const href = alert.project && ui.projectHref ? `${ui.projectHref(alert.project)}?tab=tests` : null;
  return html`<div class="banners"><div class="banner t-danger" role="status">${icon('warn', 15, 1.8)}<span class="grow" style="color: var(--foreground);"><strong>CPU вище ${num(ui.alertPct ?? alert.pct)}% уже ${num(Math.max(1, Math.round(alert.minutes)))} хв.</strong>${who}</span>${href ? html`<a class="btn sm" href="${href}">До прогону</a>` : ''}</div></div>`;
}

const emptyCard = (title, text) => html`<section class="card"><div class="empty"><span class="t">${title}</span><span style="font-size: 12.5px;">${text}</span></div></section>`;

// ------------------------------------------------------------------------------------------------ «Навантаження»
/**
 * data = {load, series, current: {running, recent}, off, err}; ui = {period, alertPct, now, hrefs: {period(p)}, projectHref(path)}.
 */
export function loadPage(data, ui = {}) {
  const head = (sub) => html`<div class="row between wrap" style="align-items: flex-start; gap: 12px;"><div><h1 class="h1" style="margin: 0;">Навантаження</h1><div class="sub">${sub}</div></div></div>`;
  if (data.off) {
    return html`${head('Семплер навантаження')}${emptyCard('Семплер вимкнено або нема psutil', raw('Увімкни <span class="mono">load_enabled = true</span> у <span class="mono">config.toml</span>, переконайся, що встановлено <span class="mono">psutil</span>, і перезапусти демон (<span class="mono">myvec install-daemon</span>).'))}`;
  }
  if (!data.load) {
    if (data.err) return html`${head('')}<section class="card"><div class="empty"><span class="t">Не вдалося отримати навантаження</span><span style="font-size: 12.5px;">${data.err}</span><button type="button" class="btn sm" data-act="loadretry">Повторити</button></div></section>`;
    return html`${head('Завантажуємо…')}<section class="card card-b stack" aria-busy="true"><span class="skel" style="width: 40%;"></span><span class="skel" style="width: 70%;"></span><span class="skel" style="width: 55%;"></span></section>`;
  }
  const L = data.load, host = L.host || {}, s = L.snapshot;
  const now = ui.now ?? (s && s.ts) ?? Date.now() / 1000;
  const alertPct = ui.alertPct ?? 85;
  const period = ui.period || '24h';
  const nCores = host.cores || (s && s.cores.length) || 0;
  const sub = [host.hostname, host.cpu_model, nCores ? `${num(nCores)} ${plural(nCores, 'ядро', 'ядра', 'ядер')}` : null, host.ram_total ? `${num(Math.round(host.ram_total / GB))} ГБ` : null, 'оновлення кожні 5 с'].filter(Boolean).join(' · ');
  const hrefP = (p) => (ui.hrefs && ui.hrefs.period ? ui.hrefs.period(p) : `#/load?period=${p}`);
  const cpu = s ? Math.round(s.cpu_pct) : null;

  const kpis = html`<div class="kpis4">
    ${kpi('CPU зараз', cpu == null ? '—' : num(cpu), cpu == null ? '' : '%', `поріг тривоги ${num(alertPct)}%${ui.alertMinutes != null ? ` · ${num(ui.alertMinutes)} хв` : ''}`, cpu != null && cpu >= alertPct)}
    ${kpi('Load average', s ? dec2(s.load[0]) : '—', nCores ? `/ ${num(nCores)} ${plural(nCores, 'ядро', 'ядра', 'ядер')}` : '', s ? `5 хв ${dec2(s.load[1])} · 15 хв ${dec2(s.load[2])}` : 'нема знімка')}
    ${kpi('Пам’ять', s ? gb(s.mem_used) : '—', s ? `з ${num(Math.round(s.mem_total / GB))} ГБ` : '', s ? `swap ${bytes(s.swap_used)}` : 'нема знімка')}
    ${kpi('Температура CPU', s && s.temp_c != null ? num(Math.round(s.temp_c)) : '—', s && s.temp_c != null ? '°C' : '', L.temp_max_24h != null ? `макс. за добу ${num(Math.round(L.temp_max_24h))} °C` : 'датчика температури нема')}
  </div>`;

  const pts = (data.series && data.series.points) || [];
  const cpuCard = html`<section class="card" aria-labelledby="mv-cpu">
    <div class="card-h" style="flex-wrap: wrap;"><h2 id="mv-cpu" class="sec-t">CPU ноди</h2><span class="grow"></span>
      <div class="seg" role="group" aria-label="Період">${PERIODS.map(([k, label]) => html`<a href="${hrefP(k)}" class="${k === period ? 'on' : ''}" ${k === period ? raw('aria-current="true"') : ''}>${label}</a>`)}</div></div>
    <div class="card-b">${pts.length
    ? html`<div class="chart" data-chart="cpu" data-v="${period}:${pts.length}:${pts[pts.length - 1].ts}" style="height: 200px;"></div>`
    : html`<div class="muted" style="font-size: 12.5px; padding: 24px 0; text-align: center;">${data.series ? 'Даних за цей період ще немає — семплер пише точку раз на хвилину.' : 'Завантажуємо графік…'}</div>`}</div>
  </section>`;

  const totalCap = Math.max(1, nCores) * 100;
  const projects = (L.projects || []).filter((p) => p.cpu_pct >= 0.5).slice(0, 6);
  const total = (L.projects || []).reduce((a, p) => a + p.cpu_pct, 0);
  const projCard = html`<section class="card" aria-labelledby="mv-top">
    <div class="card-h"><h2 id="mv-top" class="sec-t">Проєкти за CPU</h2><span class="grow"></span><span class="hint">остання хвилина, % одного ядра</span></div>
    <div class="card-b hbars">${projects.length ? projects.map((p) => {
    const name = p.project == null ? 'інше' : p.project;
    const href = p.project != null && ui.projectHref ? ui.projectHref(p.project) : null;
    return html`<div class="hbar"><span class="stack" style="gap: 0; min-width: 0;">${href ? html`<a class="mono ell" style="font-weight: 600;" href="${href}">${name}</a>` : html`<span class="mono ell" style="font-weight: 600;">${name}</span>`}<span class="muted ell" style="font-size: 11.5px;">${p.top_name || ''}</span></span><span class="tr"><span style="width: ${Math.min(100, 100 * p.cpu_pct / totalCap).toFixed(1)}%; ${p.project == null ? 'background: var(--neutral);' : ''}"></span></span><span class="val">${num(Math.round(p.cpu_pct))}%</span></div>`;
  }) : html`<div class="muted" style="font-size: 12.5px;">Нічого помітного за останню хвилину.</div>`}
      ${projects.length ? html`<div class="muted" style="font-size: 12px;">Усього ${num(Math.round(total))}% з ${num(totalCap)}% (${num(nCores || 1)} ${plural(nCores || 1, 'ядро', 'ядра', 'ядер')}). Проєкт визначається за робочою текою процесу.</div>` : ''}</div>
  </section>`;

  const coresCard = html`<section class="card" aria-labelledby="mv-cores">
    <div class="card-h"><h2 id="mv-cores" class="sec-t">Ядра</h2><span class="grow"></span><span class="hint">зараз</span></div>
    <div class="card-b">${s && s.cores.length ? html`<div class="cores" style="--cols: ${s.cores.length <= 12 ? s.cores.length : Math.ceil(s.cores.length / 2)};">${s.cores.map((c, i) => html`<div class="core"><span class="m" title="Ядро ${i}: ${num(Math.round(c))}%"><span class="${c >= alertPct ? 'hot' : ''}" style="height: ${Math.min(100, Math.max(0, c)).toFixed(0)}%;"></span></span><span>${num(Math.round(c))}%</span></div>`)}</div>` : html`<div class="muted" style="font-size: 12.5px;">Нема знімка.</div>`}</div>
  </section>`;

  const cur = data.current || { running: [], recent: [] };
  const runs = [...cur.running, ...cur.recent];
  const runsCard = html`<section class="card" aria-labelledby="mv-runs-now" style="overflow: hidden;">
    <div class="card-h"><h2 id="mv-runs-now" class="sec-t">Прогони тестів</h2><span class="grow"></span><span class="hint">усі проєкти ноди</span></div>
    ${runs.length ? runs.map((r) => runRow(r, { ...ui, now })) : html`<div class="empty" style="padding: 24px 20px;"><span class="t">Прогонів ще немає</span><span style="font-size: 12.5px;">Запусти тести через <span class="mono">myvec measure -- &lt;команда&gt;</span>: прогін з’явиться тут.</span></div>`}
  </section>`;

  return html`${head(sub)}${alertBanner(L.alert, ui)}${kpis}${cpuCard}<div class="two">${projCard}${coresCard}</div>${runsCard}`;
}

// ------------------------------------------------------------------------------------------------ project tab «Тести»
/** Header sub line of the project page when the «Тести» tab is open. */
export function testsSub(summary) {
  if (!summary || !summary.n) return 'Прогонів тестів ще немає';
  const days = Math.max(1, Math.round((summary.last_at - summary.first_at) / 86400));
  return html`${num(summary.n)} ${plural(summary.n, 'прогін', 'прогони', 'прогонів')} за ${num(days)} ${plural(days, 'день', 'дні', 'днів')} · ${num(summary.n_wrapper)} через <span class="mono">myvec measure</span>, ${num(summary.n_passive)} пасивно`;
}

/** data = {runs (newest first), summary, err}; ui = {hostCores, now}. */
export function testsTab(data, ui = {}) {
  const now = ui.now ?? Date.now() / 1000;
  if (!data || (!data.runs && !data.err)) return html`<section class="card card-b stack" aria-busy="true"><span class="skel" style="width: 40%;"></span><span class="skel" style="width: 70%;"></span></section>`;
  if (data.err) return html`<section class="card"><div class="empty"><span class="t">Не вдалося отримати прогони</span><span style="font-size: 12.5px;">${data.err}</span><button type="button" class="btn sm" data-act="testsretry">Повторити</button></div></section>`;
  const runs = data.runs, sum = data.summary || {};
  if (!runs.length) return emptyCard('Прогонів ще немає', raw('Запусти тести через <span class="mono">myvec measure -- &lt;команда&gt;</span> — прогін з’явиться тут.'));
  const last = runs.find((r) => r.wall_s != null) || runs[0];
  const cr = chartRuns(runs);
  const anyReg = cr.some((r) => r.regression), anyFail = cr.some((r) => r.outcome === 'failed');
  const medWall = sum.median_wall_s ?? median(runs.filter((r) => r.outcome === 'passed').map((r) => r.wall_s));
  const medCpu = sum.median_cpu_s ?? median(runs.filter((r) => r.outcome === 'passed').map((r) => r.cpu_s));
  const cpuT = last.cpu_s == null ? null : last.cpu_s;
  const kpis = html`<div class="kpis4">
    ${kpi('Останній прогін', last.wall_s == null ? '—' : durParts(last.wall_s), last.wall_s != null && last.wall_s < 60 ? 'с' : '', medWall != null ? `медіана ${durS(medWall)}` : 'медіани ще нема')}
    ${kpi('CPU-час', cpuT == null ? '—' : durParts(cpuT), cpuT != null && cpuT < 60 ? 'с' : '', medCpu != null ? `медіана ${durS(medCpu)}` : 'медіани ще нема')}
    ${kpi('Ядер у середньому', last.avg_cores == null ? '—' : dec1(last.avg_cores), ui.hostCores && last.avg_cores != null ? `з ${num(ui.hostCores)}` : '', last.peak_cores != null ? `пік ${dec1(last.peak_cores)}` : 'пік невідомий')}
    ${kpi('Пік пам’яті', last.peak_rss_bytes == null ? '—' : bytesParts(last.peak_rss_bytes)[0], last.peak_rss_bytes == null ? '' : bytesParts(last.peak_rss_bytes)[1], 'дерево процесів прогону')}
  </div>`;
  const chartBlock = (id, title, hint, name, legend) => html`<section class="card" aria-labelledby="${id}"><div class="card-h"><h2 id="${id}" class="sec-t">${title}</h2><span class="grow"></span><span class="hint">${hint}</span></div>
    <div class="card-b stack" style="gap: 8px;">${cr.length ? html`<div class="chart" data-chart="${name}" data-v="${cr.length}:${cr[cr.length - 1].id ?? cr[cr.length - 1].started_at}" style="height: 170px;"></div>` : html`<div class="muted" style="font-size: 12.5px; padding: 24px 0; text-align: center;">Завершених прогонів ще немає.</div>`}
    <div class="chart-legend">${legend}</div></div></section>`;
  const legendDur = html`<span><i style="background: var(--brand-primary);"></i>прогін</span>${anyReg ? html`<span><i style="background: var(--warning);"></i>регресія${data.rule ? ` (+${num(data.rule.pct)}% до медіани ${num(data.rule.runs)})` : ''}</span>` : ''}${anyFail ? html`<span><i style="background: var(--danger);"></i>тести впали</span>` : ''}`;
  const charts = html`<div class="two">
    ${chartBlock('mv-dur', 'Тривалість прогону', 'секунди', 'dur', legendDur)}
    ${chartBlock('mv-cput', 'CPU-час прогону', 'хвилини процесора, усі ядра', 'cput', html`<span>Скільки процесорного часу з’їв прогін; ріст при тій самій тривалості — більше паралельних воркерів.</span>`)}
  </div>`;
  const row = (r) => html`<div class="trow"><span class="mono muted" style="font-size: 12px;">${when(r.started_at, now)}</span><span class="row cmdcell" style="gap: 6px; min-width: 0;"><span class="mono cmd" style="font-size: 12px;" title="${r.command || ''}">${r.command || r.label || ''}</span><span class="tag">${sourceLabel(r.source)}</span></span><span class="mono" style="font-size: 12px;" title="${r.ticket_id ? '' : 'без тікета'}">${r.ticket_id || '—'}</span><span style="font-size: 12px;">${r.agent || '—'}</span><span class="num right tnum" style="${r.regression ? 'color: var(--warning); font-weight: 650;' : ''}">${r.outcome === 'running' ? html`<span class="live">${durS(Math.max(0, now - r.started_at))}</span>` : durS(r.wall_s)}</span><span class="num right tnum">${r.cpu_s == null ? '—' : durS(r.cpu_s)}</span><span class="num right tnum">${r.avg_cores == null ? '—' : dec1(r.avg_cores)} / ${r.peak_cores == null ? '—' : dec1(r.peak_cores)}</span><span class="num right tnum">${r.peak_rss_bytes == null ? '—' : bytes(r.peak_rss_bytes)}</span><span>${r.regression ? regMark(r) : outcomeBadge(r, now)}</span></div>`;
  const hist = html`<section class="card runs" aria-labelledby="mv-rl" style="overflow: hidden;">
    <div class="card-h"><h2 id="mv-rl" class="sec-t">Історія прогонів</h2><span class="grow"></span><span class="hint">останні ${num(runs.length)}</span></div>
    <div class="scroll-x">
      <div class="thead" style="border-radius: 0;"><span>Час</span><span>Команда</span><span>Тікет</span><span>Агент</span><span class="right">Тривал.</span><span class="right">CPU-час</span><span class="right">Ядер сер./пік</span><span class="right">Пам’ять</span><span>Результат</span></div>
      ${runs.map(row)}
    </div>
  </section>`;
  return html`${kpis}${charts}${hist}`;
}
