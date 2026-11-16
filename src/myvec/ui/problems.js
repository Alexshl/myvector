// «Проблеми» проєкту (E3-04): чиста функція data -> html, як tracker.js. Імпортує лише util.js: без мережі, DOM
// і глобального стану. Посилання лише через переданий `hrefs` ({ ticket(path, id) }).
import { html, num, icon } from './util.js';

const SEV = {
  error: { ic: '✖', label: 'Помилки', cls: 'p-error' },
  warning: { ic: '⚠', label: 'Попередження', cls: 'p-warning' },
  health: { ic: 'ℹ', label: 'Поради', cls: 'p-health' },
};
const SHOWN = 10;
const PEEK = 3;

/** Number of problems for the tab label: errors + warnings + advice (summary: n_issues + n_health + n_process). */
export const problemCount = (p) => (p ? (p.n_issues || 0) + (p.n_health || 0) + (p.n_process || 0) : 0);

const copyBtn = (cmd, code) => html`<button type="button" class="btn sm" data-act="copy" data-copy="${cmd}" data-msg="Команду скопійовано" data-fk="prob-copy:${code}" aria-label="Скопіювати команду">${icon('copy', 13)}</button>`;

function ticketsList(g, ui) {
  const ids = g.tickets || [];
  if (!ids.length) return '';
  const peek = ids.slice(0, PEEK).join(', ') + (ids.length > PEEK ? ' …' : '');
  const more = ids.length - SHOWN;
  return html`<details class="p-tix"><summary><span class="p-chev">${icon('chevRight', 13)}</span><span class="mono">${peek}</span></summary>
    <div class="p-ids">${ids.slice(0, SHOWN).map((id) => html`<a class="mono" href="${ui.hrefs.ticket(ui.path, id)}">${id}</a>`)}${more > 0 ? html`<span class="muted">ще ${num(more)}</span>` : ''}</div></details>`;
}

function projectItems(g) {
  const items = g.project_items || [];
  if (!items.length) return '';
  return html`<ul class="p-items">${items.slice(0, SHOWN).map((x) => html`<li><span class="mono">${x.path}</span> <span class="muted">${x.message}</span></li>`)}${items.length > SHOWN ? html`<li class="muted">ще ${num(items.length - SHOWN)}</li>` : ''}</ul>`;
}

function group(g, data, ui) {
  const s = SEV[g.severity] || SEV.warning;
  const showBranch = g.legacy && data.migration_branch;
  return html`<section class="card p-group ${s.cls}" aria-label="${g.title}">
    <div class="p-head"><span class="p-ic" aria-hidden="true">${s.ic}</span><h3 class="p-title">${g.title}</h3><span class="grow"></span><span class="tnum p-n">${num(g.n)}</span></div>
    ${g.explain ? html`<p class="p-expl">${g.explain}</p>` : ''}
    ${g.fix || g.cmd ? html`<div class="p-do"><span class="lbl">Що робити:</span>${g.cmd ? html`<span class="p-cmd"><code class="mono">${g.cmd}</code>${copyBtn(g.cmd, g.code)}</span>` : ''}${g.fix && !g.cmd ? html`<span>${g.fix}</span>` : ''}</div>
      ${g.cmd && g.fix ? html`<p class="p-expl">${g.fix}</p>` : ''}` : ''}
    ${showBranch ? html`<div class="p-do"><span>або злити гілку <span class="mono">${data.migration_branch}</span>:</span>${data.merge_cmd ? html`<span class="p-cmd"><code class="mono">${data.merge_cmd}</code>${copyBtn(data.merge_cmd, g.code + '-merge')}</span>` : ''}</div>` : ''}
    ${ticketsList(g, ui)}${projectItems(g)}
    <span class="muted p-code">код <span class="mono">${g.code}</span></span>
  </section>`;
}

/** The «Проблеми» tab. data = /api/tickets/<p>/problems payload or null while loading; ui = { path, hrefs, err }. */
export function problemsTab(data, ui) {
  if (ui.err && !data) {
    return html`<section class="card"><div class="empty"><span class="t">Не вдалося завантажити проблеми</span><span style="font-size: 12.5px;">${ui.err}</span></div></section>`;
  }
  if (!data) return html`<section class="card card-b stack" aria-busy="true"><span class="skel" style="width: 40%;"></span><span class="skel" style="width: 70%;"></span></section>`;
  if (!data.groups.length) {
    return html`<section class="card"><div class="empty">${icon('checkCircle', 22, 1.5)}<span class="t">Проблем нема</span><span style="font-size: 12.5px;">Тікети проєкту без помилок, попереджень і порад.</span></div></section>`;
  }
  const c = data.counts || {};
  const summary = ['error', 'warning', 'health'].filter((k) => c[k]).map((k) => `${SEV[k].ic} ${num(c[k])}`).join(' · ');
  return html`<div class="stack" style="gap: 12px;">
    <div class="sub">${summary}</div>
    ${['error', 'warning', 'health'].map((k) => {
      const gs = data.groups.filter((g) => g.severity === k);
      return gs.length ? html`<div class="stack" style="gap: 10px;"><h2 class="sec-t p-sev ${SEV[k].cls}">${SEV[k].ic} ${SEV[k].label}</h2>${gs.map((g) => group(g, data, ui))}</div>` : '';
    })}
  </div>`;
}
