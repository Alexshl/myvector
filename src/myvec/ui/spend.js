// Token spend: project tab «Витрати» and the «Витрати» block of a ticket card (E7-03, ADR-016).
// Pure like tracker.js / monitor.js: data -> html / svg strings, no network, DOM, URL or storage access.
// Measuring widths, listeners and polling live in app.js.
import { html, raw, num, dec1, plural } from './util.js';
import { barsSvg, barsGeom } from './monitor.js';

const p2 = (x) => String(x).padStart(2, '0');
const NF2 = new Intl.NumberFormat('uk-UA', { maximumFractionDigits: 2 });
const ddmm = (ts) => { const d = new Date(ts * 1000); return `${p2(d.getDate())}.${p2(d.getMonth() + 1)}`; };

export const PERIODS = [[7, '7 днів'], [14, '14 днів'], [30, '30 днів']];

/** Compact number as [value, unit]: < 1000 whole, «тис.» whole, «млн» / «млрд» with one decimal (uk-UA). */
export function tkParts(n) {
  n = Math.max(0, Math.round(n || 0));
  if (n < 1000) return [num(n), ''];
  if (Math.round(n / 1e3) < 1000) return [num(Math.round(n / 1e3)), 'тис.'];
  if (Math.round(n / 1e5) / 10 < 1000) return [dec1(n / 1e6), 'млн'];
  return [dec1(n / 1e9), 'млрд'];
}
export const tk = (n) => { const [v, u] = tkParts(n); return u ? `${v} ${u}` : v; };

/** `claude-sonnet-5-5[-20251101]` -> `Sonnet 5.5`; anything else is shown as is. */
export function modelLabel(id) {
  const m = /^claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?$/.exec(id || '');
  if (!m) return id || '?';
  return `${m[1][0].toUpperCase()}${m[1].slice(1)} ${m[2]}${m[3] != null ? `.${m[3]}` : ''}`;
}

export const agentLabel = (a) => (a === 'main' ? 'оркестратор' : a);
const slash = (xs) => (xs && xs.length ? xs.join('/') : '—');

/** Hot days (ADR-016): more than twice the median of the non-zero days, and only with >= 3 non-zero days. */
export function hotDays(vals) {
  const nz = vals.filter((v) => v > 0).sort((a, b) => a - b);
  if (nz.length < 3) return vals.map(() => false);
  const mid = Math.floor(nz.length / 2);
  const med = nz.length % 2 ? nz[mid] : (nz[mid - 1] + nz[mid]) / 2;
  return vals.map((v) => v > 2 * med);
}

// ------------------------------------------------------------------------------------------------ chart
/** Axis unit for the day bars: millions, thousands or plain tokens. */
function axisUnit(vals) {
  const top = Math.max(0, ...vals);
  return top >= 1e6 ? [1e6, 'млн'] : top >= 1e3 ? [1e3, 'тис.'] : [1, ''];
}

/** `name` is 'tok' only; data = the project report ({days}). Returns {svg, geom, tip(i)} like monitor.chart. */
export function chart(name, data, ui, { w, h }) {
  const days = (data && data.days) || [];
  const vals = days.map((d) => d.total || 0);
  const hot = hotDays(vals);
  const runs = days.map((d, i) => ({ started_at: d.ts, regression: hot[i], outcome: 'passed', v: vals[i] }));
  const [div] = axisUnit(vals);
  const opts = { w, h, label: 'Токени за днями', fmtTick: (v) => NF2.format(v / div) };
  return {
    svg: barsSvg(runs, (r) => r.v, opts).toString(),
    geom: barsGeom(vals, opts),
    tip: (i) => {
      const d = days[i];
      if (!d) return '';
      return String(html`${ddmm(d.ts)} · <b>${tk(d.total)}</b> · output ${tk(d.output)}${hot[i] ? ' · понад удвічі вище медіани' : ''}`);
    },
  };
}

// ------------------------------------------------------------------------------------------------ project tab
const kpi = (label, value, unit, sub, mono = false) => html`<section class="card kt"><span class="lbl">${label}</span><span class="v ${mono ? 'mono' : ''}" ${mono ? raw('style="font-size: 20px;"') : ''}>${value}${unit ? html`<small>${unit}</small>` : ''}</span><span class="s">${sub}</span></section>`;
const emptyCard = (title, text) => html`<section class="card"><div class="empty"><span class="t">${title}</span><span style="font-size: 12.5px;">${text}</span></div></section>`;

/** 7/14/30 switch for the project header; links come from hrefs.days(path, n). */
export const periodSeg = (path, days, hrefs) => html`<div class="seg" role="group" aria-label="Період">${PERIODS.map(([n, label]) => html`<a href="${hrefs && hrefs.days ? hrefs.days(path, n) : '#'}" class="${n === days ? 'on' : ''}" ${n === days ? raw('aria-current="true"') : ''}>${label}</a>`)}</div>`;

/** Under the project title when the tab is open. */
export function tokensSub(data, days) {
  const n = days || (data && data.days && data.days.length) || 14;
  return `Витрати токенів за ${num(n)} ${plural(n, 'день', 'дні', 'днів')} · з транскриптів Claude Code, без тексту повідомлень`;
}

/** data = /api/tokens/project payload | {err} | null (loading); ui = {path, days, hrefs: {ticket(path, id), days(path, n)}}. */
export function tokensTab(data, ui = {}) {
  if (!data) return html`<section class="card card-b stack" aria-busy="true"><span class="skel" style="width: 40%;"></span><span class="skel" style="width: 70%;"></span><span class="skel" style="width: 55%;"></span></section>`;
  if (data.err) return html`<section class="card"><div class="empty"><span class="t">Не вдалося отримати витрати</span><span style="font-size: 12.5px;">${data.err}</span><button type="button" class="btn sm" data-act="tokensretry">Повторити</button></div></section>`;
  if (data.enabled === false) {
    return html`${emptyCard('Збір витрат вимкнено', raw('Увімкни <span class="mono">tokens_enabled = true</span> у <span class="mono">config.toml</span> і перезапусти демон (<span class="mono">myvec install-daemon</span>).'))}`;
  }
  const T = data.totals || {};
  if (!T.total) {
    return html`${emptyCard('Витрат за цей період нема', 'Транскрипти Claude Code ще не проскановано або за період не було відповідей моделі в цьому проєкті.')}`;
  }
  const agents = data.agents || [], tickets = data.tickets || [], days = data.days || [];
  const sum = T.total, maxT = Math.max(1, ...agents.map((a) => a.total)), maxK = Math.max(1, ...tickets.map((t) => t.total));
  const [tv, tu] = tkParts(sum), [ov, ou] = tkParts(T.output);
  const top = data.top_ticket;
  const kpis = html`<div class="kpis4">
    ${kpi('Усього токенів', tv, tu, 'разом із кешем')}
    ${kpi('Згенеровано (output)', ov, ou, 'найдорожча частина')}
    ${kpi('Частка кешу', num(Math.round(100 * (T.cache_share || 0))), '%', 'повторне читання контексту')}
    ${top ? kpi('Найдорожчий тікет', top.id, '', `${tk(top.total)} · переважно ${agentLabel(top.top_agent)}`, true) : kpi('Найдорожчий тікет', '—', '', 'тікетів з витратами нема')}
  </div>`;
  const vals = days.map((d) => d.total), anyHot = hotDays(vals).some(Boolean);
  const unit = axisUnit(vals)[1];
  const chartCard = html`<section class="card" aria-labelledby="mv-tday"><div class="card-h"><h2 id="mv-tday" class="sec-t">Токени за днями</h2><span class="grow"></span><span class="hint">${unit ? `${unit === 'млн' ? 'мільйони' : 'тисячі'}` : 'токени'}, усі агенти</span></div>
    <div class="card-b stack" style="gap: 8px;"><div class="chart" data-chart="tok" data-v="${days.length}:${sum}:${days.length ? days[days.length - 1].ts : 0}" style="height: 160px;" role="img" aria-label="Токени за днями"></div>
    <div class="chart-legend"><span><i style="background: var(--brand-primary);"></i>день</span>${anyHot ? html`<span><i style="background: var(--warning);"></i>понад удвічі вище медіани</span>` : ''}</div></div></section>`;
  const agentRow = (a) => html`<div class="trow"><span class="${a.orch ? 'muted' : ''}" style="font-weight: 600;">${agentLabel(a.agent)}${a.orch ? html` <span class="tag">без тікета</span>` : ''}</span><span class="tk-model" style="font-size: 12px;" title="${slash((a.models || []).map(modelLabel))}">${slash((a.models || []).map(modelLabel))}</span><span style="font-size: 12px;">${slash(a.efforts)}</span><span class="num right">${num(a.runs)}</span><span class="num right">${tk(a.output)}</span><span class="num right">${tk(a.total)}</span><span class="pcell"><span class="prog"><span style="width: ${(100 * a.total / maxT).toFixed(1)}%;"></span></span><span class="n">${num(Math.round(100 * a.total / sum))}%</span></span></div>`;
  const agentsCard = html`<section class="card runs tk-agents" aria-labelledby="mv-tag" style="overflow: hidden;">
    <div class="card-h"><h2 id="mv-tag" class="sec-t">За агентами</h2><span class="grow"></span><span class="hint">модель · effort · запусків</span></div>
    <div class="scroll-x">
      <div class="thead" style="border-radius: 0;"><span>Агент</span><span>Модель</span><span>Effort</span><span class="right">Запусків</span><span class="right">Output</span><span class="right">Усього</span><span>Частка</span></div>
      ${agents.map(agentRow)}
    </div>
    <div class="tfoot" style="position: static;"><span>${agents.some((a) => a.orch) ? 'Оркестратор на main без тікета — окремим рядком, не розноситься' : ''}</span><span class="num">${tk(sum)}</span></div>
  </section>`;
  const ticketRow = (t) => html`<div class="hbar"><span class="stack" style="gap: 0; min-width: 0;"><span class="row" style="gap: 6px; min-width: 0;">${ui.hrefs && ui.hrefs.ticket ? html`<a class="mono" style="font-weight: 600; flex: none; white-space: nowrap;" href="${ui.hrefs.ticket(ui.path, t.id)}">${t.id}</a>` : html`<span class="mono" style="font-weight: 600; flex: none; white-space: nowrap;">${t.id}</span>`}${t.title ? html`<span class="ell">${t.title}</span>` : ''}</span><span class="muted" style="font-size: 11.5px;">переважно ${agentLabel(t.top_agent)}</span></span><span class="tr"><span style="width: ${(100 * t.total / maxK).toFixed(1)}%;"></span></span><span class="val" style="white-space: nowrap;">${tk(t.total)}</span></div>`;
  const ticketsCard = html`<section class="card tk-tickets" aria-labelledby="mv-ttk" style="overflow: hidden;">
    <div class="card-h"><h2 id="mv-ttk" class="sec-t">Найдорожчі тікети</h2><span class="grow"></span><span class="hint">що «з’їло» найбільше</span></div>
    <div class="card-b hbars">${tickets.length ? tickets.map(ticketRow) : html`<div class="muted" style="font-size: 12.5px;">За період нема витрат, прив’язаних до тікетів.</div>`}</div>
  </section>`;
  return html`${kpis}${chartCard}${agentsCard}${ticketsCard}`;
}

// ------------------------------------------------------------------------------------------------ ticket card block
/** «Витрати» section of a ticket card from /api/tokens/ticket `card`; null / no rows -> ''. */
export function ticketBlock(card) {
  if (!card || !card.by_agent || !card.by_agent.length) return '';
  const rows = card.by_agent, maxT = Math.max(1, ...rows.map((r) => r.total));
  const total = (card.summary && card.summary.total) || rows.reduce((a, r) => a + r.total, 0);
  const sub = (r) => [slash((r.models || []).map(modelLabel)), slash(r.efforts), `${num(r.runs)} ${plural(r.runs, 'запуск', 'запуски', 'запусків')}`, `output ${tk(r.output)}`].join(' · ');
  return html`<section class="card" aria-labelledby="mv-tc"><div class="card-h"><h2 id="mv-tc" class="sec-t">Витрати</h2><span class="grow"></span><span class="hint tnum">${tk(total)} · ${num(Math.round(100 * (card.cache_share || 0)))}% кеш</span></div>
    <div class="card-b stack" style="gap: 8px; font-size: 12.5px;">${rows.map((r) => html`<div class="tk-arow"><span style="font-weight: 600; overflow-wrap: anywhere;">${agentLabel(r.agent)}</span><span class="pcell"><span class="prog"><span style="width: ${(100 * r.total / maxT).toFixed(1)}%;"></span></span></span><span class="num right">${tk(r.total)}</span><span class="muted" style="grid-column: 1 / -1; font-size: 11.5px; margin-top: -4px;">${sub(r)}</span></div>`)}
      <div class="muted" style="font-size: 12px;">У Log при закритті: <span class="mono">${card.line}</span></div></div></section>`;
}
