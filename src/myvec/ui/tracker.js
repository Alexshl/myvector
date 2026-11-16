// Tracker views (ADR-008): pure functions data -> html. Imports only util.js: no network, no DOM, no
// global state. Links come from the injected `hrefs` ({ project(path), ticket(path, id), all() }),
// so the same module can be reused by the cloud Worker UI with its own routes.
import { html, raw, num, count, icon, pct, isoDate, inline, markdown } from './util.js';

// status key -> [emoji, Ukrainian label]
export const STATUS = {
  todo: ['⬜', 'До роботи'],
  in_progress: ['🟦', 'В роботі'],
  review: ['🟨', 'На ревю'],
  waiting: ['⏳', 'Чекає відповіді'],
  blocked: ['⛔', 'Заблоковано'],
  partial: ['🟧', 'Частково'],
  done: ['✅', 'Готово'],
  cancelled: ['🚫', 'Скасовано'],
  frozen: ['❄️', 'Заморожено'],
};
const ACTIVE = ['in_progress', 'review', 'waiting', 'partial'];
const BAR_ORDER = ['done', 'review', 'in_progress', 'partial', 'waiting', 'blocked', 'todo'];
export const FILTERS = [['all', 'Усі'], ['active', 'Активні'], ['waiting', 'Чекають'], ['problems', 'Проблеми']];
const W = { tickets: ['тікет', 'тікети', 'тікетів'], projects: ['проєкт', 'проєкти', 'проєктів'] };
const TREE_COLS = 'minmax(220px,1fr) 150px 170px 150px 64px';
const PROJ_COLS = 'minmax(170px,1fr) 170px 92px 92px 92px 108px';

export const statusBadge = (key) => {
  const s = STATUS[key];
  return s
    ? html`<span class="st s-${key}"><span class="e" aria-hidden="true">${s[0]}</span>${s[1]}</span>`
    : html`<span class="st">${key || '—'}</span>`;
};

const isGroup = (n) => n.kind === 'group';
const shortDate = (s) => (s ? `${s.slice(8, 10)}.${s.slice(5, 7)}` : '');

// ---------------------------------------------------------------------------------------------- tree logic
/** Flat nodes -> forest of {node, children}; the parent link may point anywhere (any depth); unknown parents become roots. */
export function buildTree(nodes) {
  const by = new Map(nodes.map((n) => [n.id, { node: n, children: [] }]));
  const roots = [];
  const seen = new Set();
  for (const n of nodes) {
    const e = by.get(n.id);
    const p = n.parent && n.parent !== n.id ? by.get(n.parent) : null;
    (p ? p.children : roots).push(e);
  }
  // parent cycles: nothing is lost, the first node of an unreachable cycle becomes a root
  const mark = (e) => { if (seen.has(e.node.id)) return; seen.add(e.node.id); e.children.forEach(mark); };
  roots.forEach(mark);
  for (const n of nodes) if (!seen.has(n.id)) { const e = by.get(n.id); roots.push(e); mark(e); }
  return roots;
}

/** ctx: { issues: Set|Map of ticket ids with issues, sessions?: { [id]: { waiting } } (seam for sessions) }. Groups never match. */
export function matches(node, filter, ctx = {}) {
  if (filter === 'all' || !filter) return true;
  if (isGroup(node)) return false;
  if (filter === 'active') return ACTIVE.includes(node.status);
  if (filter === 'waiting') return node.status === 'waiting' || !!(ctx.sessions && ctx.sessions[node.id] && ctx.sessions[node.id].waiting);
  if (filter === 'problems') return node.status === 'blocked' || !!(ctx.issues && ctx.issues.has(node.id));
  return true;
}

export function filterCounts(nodes, ctx = {}) {
  const leaves = nodes.filter((n) => !isGroup(n));
  const out = {};
  for (const [k] of FILTERS) out[k] = leaves.filter((n) => matches(n, k, ctx)).length;
  return out;
}

const issueMap = (issues) => {
  const m = new Map();
  for (const i of issues || []) if (i.ticket_id) (m.get(i.ticket_id) || m.set(i.ticket_id, []).get(i.ticket_id)).push(i.message);
  return m;
};
const leavesUnder = (e, out = []) => { for (const c of e.children) { if (isGroup(c.node)) leavesUnder(c, out); else out.push(c.node); } return out; };

/** Rows to render: [{entry, depth, open}] respecting collapsed groups (ignored under a filter) and ancestors of matches. */
function visibleRows(roots, filter, ctx, collapsed) {
  const out = [];
  const hit = (e) => matches(e.node, filter, ctx) || e.children.some(hit);
  const walk = (e, depth) => {
    if (filter !== 'all' && !hit(e)) return;
    const closed = filter === 'all' && isGroup(e.node) && collapsed.has(e.node.id);
    out.push({ entry: e, depth, closed });
    if (!closed) e.children.forEach((c) => walk(c, depth + 1));
  };
  roots.forEach((e) => walk(e, 0));
  return out;
}

// ---------------------------------------------------------------------------------------------- sessions (ADR-010)
/** Seconds -> "42 хв", "2 год 8 хв", "1 д 3 год". */
export function waitText(sec, compact = false) {
  const m = Math.floor(Math.max(0, sec || 0) / 60);
  if (m < 1) return compact ? '<1 хв' : 'менше хвилини';
  if (m < 60) return `${num(m)} хв`;
  const h = Math.floor(m / 60);
  if (h < 24) return m % 60 ? `${num(h)} год ${num(m % 60)} хв` : `${num(h)} год`;
  return h % 24 ? `${num(Math.floor(h / 24))} д ${num(h % 24)} год` : `${num(Math.floor(h / 24))} д`;
}
const STATE_TXT = { working: 'працює', waiting: 'чекає', idle: 'тиша', ended: 'завершена' };
const STATE_DOT = { working: 't-success', waiting: 't-warning', idle: '', ended: '' };
const MODE_CHIP = { send: 'через cm', restart_send: 'не запущена · перезапуск cm', resume_send: 'не запущена · відновлення cm', held: 'у терміналі', unavailable: 'недоступно' };
const shortId = (id) => (id && id.length > 14 ? `${id.slice(0, 8)}…${id.slice(-4)}` : id || '');
const urgency = (s) => (s.effective_state === 'waiting' ? 3 : s.effective_state === 'working' ? 2 : s.effective_state === 'idle' ? 1 : 0);

/** Copy texts of a session: UUID, `cm resume`, fork and the hint for the manager session. */
export function copyTexts(sid, project, ticket, reply) {
  const where = [project, ticket].filter(Boolean).join(', ');
  const tail = reply && reply.trim() ? ` і передай їй: «${reply.trim()}»` : '';
  return {
    uuid: sid,
    resume: `cm resume ${sid}`,
    fork: `cm resume ${sid} --fork`,
    manager: `Віднови сесію ${sid}${where ? ` (${where})` : ''} через cm${tail}`,
  };
}

/** "Сесія" column of the tree: the most urgent session as dot + node chip + state text; "—" without sessions. */
export function sessionCell(sessions, node) {
  const list = (sessions || []).slice().sort((a, b) => urgency(b) - urgency(a));
  const s = list[0];
  if (!s) return html`<span class="faint">—</span>`;
  const st = s.effective_state;
  const wait = st === 'waiting';
  const txt = wait ? `чекає ${waitText(s.wait_s, true)}` : STATE_TXT[st] || st;
  return html`<span class="sess"><span class="dot ${STATE_DOT[st] || ''}"></span>${node ? html`<span class="nodechip">${node}</span>` : ''}<span class="${wait ? '' : 'muted'}" style="${wait ? 'color: var(--warning); font-weight: 600;' : ''}">${txt}</span>${list.length > 1 ? html`<span class="muted tnum">+${num(list.length - 1)}</span>` : ''}</span>`;
}

const cpBtn = (kind, s, ui, label, extra = {}) => html`<button type="button" class="btn sm" data-act="sesscopy" data-kind="${kind}" data-sid="${s.session_id}" data-project="${extra.project || ''}" data-ticket="${extra.ticket || ''}">${icon('copy', 13)}${label}</button>`;
const holdHint = (ui) => (ui.nodeName || ui.node ? `Сесія відкрита в терміналі на ${ui.nodeName || ui.node}` : 'Сесія відкрита в терміналі');

/** Action buttons by mode. send: answer; restart_send/resume_send: answer+resume; held: disabled + fork; unavailable: disabled + hint. */
function sessActions(s, ui, o = {}) {
  const busy = ui.busy && ui.busy[s.session_id];
  const m = s.mode;
  if (s.effective_state === 'working') return '';   // a running session is not resumed: copy buttons only
  if (m === 'held') {
    return html`<button type="button" class="btn sm" disabled title="${holdHint(ui)}">Відновити</button>${cpBtn('fork', s, ui, 'Форк', o)}`;
  }
  if (m === 'unavailable' || !m) {
    return html`<button type="button" class="btn sm" disabled title="${ui.cmHint || 'cm недоступний: оновіть claude-manager: `cm update`'}">Відновити</button>`;
  }
  const label = o.compact
    ? (m === 'send' ? 'Відповісти' : 'Відновити')
    : (m === 'send' ? 'Відповісти' : 'Відповісти й відновити');
  if (o.compact && m === 'send') {
    return html`<a class="btn sm primary" href="${o.href || '#'}">${icon('send', 12)}${label}</a>`;
  }
  const qat = s.answer_kind === 'auq' && s.question_at ? raw(`data-qat="${Number(s.question_at)}"`) : '';
  return html`<button type="button" class="btn sm primary" data-act="sessresume" data-sid="${s.session_id}" ${qat} ${busy ? raw('disabled') : ''}>${icon(o.compact ? 'play' : 'send', 12)}${busy ? 'Надсилаю…' : label}</button>`;
}

/** One session in the ticket card: state, question, cwd, reply textarea (by mode), copy buttons. */
export function sessionBlock(s, ui) {
  const st = s.effective_state;
  const wait = st === 'waiting';
  const writable = s.mode && s.mode !== 'held' && s.mode !== 'unavailable' && st !== 'working';
  const reply = (ui.replies && ui.replies[s.session_id]) || '';
  const ex = { project: ui.project, ticket: ui.ticket };
  const auq = wait && s.answer_kind === 'auq';
  const labels = auq && s.question_options && Array.isArray(s.question_options.labels) ? s.question_options.labels : [];
  const busy = ui.busy && ui.busy[s.session_id];
  const qol = s.question_options && Array.isArray(s.question_options.labels) ? s.question_options.labels : [];
  const yes = qol.some((x) => typeof x === 'string' && x.startsWith('Yes'));
  return html`<div class="sesscard" role="status" data-fk="sess:${s.session_id}">
    <div class="row wrap" style="gap: 6px;"><span class="dot ${STATE_DOT[st] || ''}"></span><strong style="${wait ? 'color: var(--warning);' : ''}">${wait ? `Сесія чекає на тебе ${waitText(s.wait_s)}` : `Сесія ${STATE_TXT[st] || st}`}</strong><span class="grow"></span>${ui.node ? html`<span class="nodechip" title="Нода, з якої показано дані">${ui.node}</span>` : ''}<span class="tag">${auq ? 'питання · через cm' : MODE_CHIP[s.mode] || 'недоступно'}</span></div>
    ${wait && s.question ? html`<div class="q">${s.question}</div>` : ''}
    ${wait && !auq && s.mode === 'send' && yes ? html`<div class="muted" style="font-size: 12px;">Варіанти виглядають як діалог дозволу — відповідай у терміналі</div>` : ''}
    <div class="mono muted" style="font-size: 11.5px; overflow-wrap: anywhere;">${s.cwd ? html`${s.cwd} · ` : ''}${shortId(s.session_id)}</div>
    ${auq ? html`<div class="row wrap" style="gap: 6px;">${labels.map((l, i) => (l ? html`<button type="button" class="btn sm" data-act="sessoption" style="max-width: 100%; white-space: normal; text-align: left; height: auto;" data-sid="${s.session_id}" data-n="${i + 1}" data-qat="${s.question_at}" data-label="${l}" ${busy ? raw('disabled') : ''}>${i + 1}. ${l}</button>` : ''))}</div>
    <label for="mv-reply-${s.session_id}" class="sr-only">Свій варіант</label>
    <input type="text" id="mv-reply-${s.session_id}" class="inp" data-fk="reply:${s.session_id}" data-input="sreply" data-sid="${s.session_id}" placeholder="Свій варіант (один рядок)" maxlength="500" value="${reply.replace(/\s+/g, ' ')}">` : writable ? html`<label for="mv-reply-${s.session_id}" class="sr-only">Відповідь сесії</label>
    <textarea id="mv-reply-${s.session_id}" class="inp" data-fk="reply:${s.session_id}" data-input="sreply" data-sid="${s.session_id}" placeholder="Відповідь, яку отримає сесія" maxlength="16000">${reply}</textarea>` : ''}
    <div class="row wrap" style="gap: 6px;">${sessActions(s, ui)}${cpBtn('uuid', s, ui, 'UUID', ex)}${cpBtn('resume', s, ui, 'cm resume', ex)}${cpBtn('manager', s, ui, 'Текст для manager', ex)}</div>
  </div>`;
}

const waitHref = (w, ui) => (w.ticket_id ? ui.hrefs.ticket(w.ticket_project, w.ticket_id) : w.project_path ? ui.hrefs.project(w.project_path) : '');

/** "Чекають на тебе" on the all-projects page; list = /api/sessions/waiting `sessions` (longest wait first). Hidden when empty. */
export function waitingCard(list, ui) {
  if (!list || !list.length) return '';
  const row = (w) => {
    const href = waitHref(w, ui);
    const dead = w.mode === 'restart_send' || w.mode === 'resume_send';
    const tag = w.mode === 'held' ? 'у терміналі' : dead ? 'не запущена' : '';
    return html`<div class="wait-row" data-fk="wait:${w.session_id}">
      <div class="row wrap" style="gap: 8px; min-width: 0;"><span class="mono" style="font-weight: 600; overflow-wrap: anywhere;"${!w.ticket_project && !w.project_path && w.project_label ? html` title="${w.cwd || w.project_label}"` : ''}>${w.ticket_project || w.project_path || w.project_label || '—'}</span>${w.ticket_id ? html`<a class="mono muted" style="font-size: 12px;" href="${href}">${w.ticket_id}</a>` : ''}${ui.node ? html`<span class="nodechip">${ui.node}</span>` : ''}<span style="color: var(--warning); font-weight: 600; font-size: 12.5px;">чекає ${waitText(w.wait_s)}</span></div>
      <div class="row" style="gap: 6px;">${sessActions(w, ui, { compact: true, href, project: w.ticket_project, ticket: w.ticket_id })}<button type="button" class="btn sm icon" aria-label="Скопіювати UUID" data-act="sesscopy" data-kind="uuid" data-sid="${w.session_id}">${icon('copy', 13)}</button></div>
      <div class="q">${tag ? html`<span class="tag" style="margin-right: 6px;">${tag}</span>` : ''}${w.question || w.ticket_title || ''}</div></div>`;
  };
  return html`<section class="card" aria-labelledby="mv-wait" style="overflow: hidden;">
    <div class="card-h"><span class="dot t-warning"></span><h2 id="mv-wait" class="sec-t">Чекають на тебе</h2><span class="grow"></span><span class="hint">найдовше очікування зверху</span></div>
    ${list.map(row)}</section>`;
}

/** "Сесії" tab of a project. data = /api/sessions payload or null (loading); ui = { hrefs, node, busy, err }. */
export function sessionsPage(data, ui) {
  if (ui.err && !data) return html`<section class="card"><div class="empty"><span class="t">Не вдалося завантажити сесії</span><span style="font-size: 12.5px;">${ui.err}</span></div></section>`;
  if (!data) return skel(60, 80, 50);
  const list = data.sessions || [];
  const cm = data.cm || {};
  const u = { ...ui, node: data.node || ui.node, cmHint: cm.hint };
  const row = (s) => {
    const st = s.effective_state;
    const wait = st === 'waiting';
    const href = waitHref(s, ui);
    return html`<div class="wait-row" data-fk="sess:${s.session_id}">
      <div class="row wrap" style="gap: 8px; min-width: 0;"><span class="sess"><span class="dot ${STATE_DOT[st] || ''}"></span><span style="${wait ? 'color: var(--warning); font-weight: 600;' : ''}">${wait ? `чекає ${waitText(s.wait_s)}` : STATE_TXT[st] || st}</span></span>${s.ticket_id ? html`<a class="mono" href="${href}">${s.ticket_id}</a><span class="muted ell">${inline(s.ticket_title || '')}</span>` : html`<span class="muted">без тікета</span>`}${u.node ? html`<span class="nodechip">${u.node}</span>` : ''}${s.branch ? html`<span class="mono muted" style="font-size: 12px;">${s.branch}</span>` : ''}</div>
      <div class="row" style="gap: 6px;">${sessActions(s, u, { compact: true, href, project: s.ticket_project, ticket: s.ticket_id })}<button type="button" class="btn sm icon" aria-label="Скопіювати UUID" data-act="sesscopy" data-kind="uuid" data-sid="${s.session_id}">${icon('copy', 13)}</button></div>
      <div class="q mono" style="font-size: 11.5px; overflow-wrap: anywhere;">${s.cwd || ''} · ${shortId(s.session_id)}${wait && s.question ? html`<br><span style="font-family: var(--font-ui); font-size: 12.5px;">${s.question}</span>` : ''}</div></div>`;
  };
  return html`<section class="card" aria-labelledby="mv-ss" style="overflow: hidden;">
    <div class="card-h"><h2 id="mv-ss" class="sec-t">Сесії</h2><span class="grow"></span><span class="hint">${cm.available === false ? cm.hint : 'керування через cm'}</span></div>
    ${list.length ? list.map(row) : html`<div class="empty"><span class="t">Сесій ще немає</span><span style="font-size: 12.5px;">Вони з’являються, коли Claude Code відкрито в цьому проєкті.</span></div>`}</section>`;
}

// ---------------------------------------------------------------------------------------------- pieces
const progBar = (v) => {
  const p = v == null ? 0 : Math.max(0, Math.min(100, Math.round(v)));
  return html`<span class="pcell"><span class="prog ${p === 100 ? 't-success' : ''}"><span style="width: ${p}%;"></span></span><span class="n">${v == null ? '—' : pct(v)}</span></span>`;
};
const plain = (s) => String(s == null ? '' : s).replace(/`/g, '');
const warnIc = (title) => html`<span class="warn-ic" title="${title}">${icon('warn', 14, 1.8)}</span>`;
const skel = (...w) => html`<section class="card card-b stack" aria-busy="true">${w.map((x) => html`<span class="skel" style="width: ${x}%;"></span>`)}</section>`;

function treeRow(r, ctx, ui, path) {
  const n = r.entry.node, g = isGroup(n), sel = ui.sel === n.id;
  const href = ui.hrefs.ticket(path, n.id);
  const msgs = ctx.issueMsgs.get(n.id);
  let kids = '';
  if (g) {
    const ls = leavesUnder(r.entry).filter((x) => x.status !== 'cancelled');
    kids = `${num(ls.filter((x) => x.status === 'done').length)}/${num(ls.length)} готово`;
  }
  return html`<div class="trow ${g ? 'grp' : ''} ${sel ? 'is-sel' : ''}" style="grid-template-columns: ${TREE_COLS};" role="row" tabindex="0" aria-selected="${String(sel)}" data-act="tkrow" data-href="${href}" data-fk="tk:${n.id}">
    <span class="tname" style="--d: ${r.depth};">${g
    ? html`<button type="button" class="chev ${r.closed ? 'closed' : ''}" data-act="tkcollapse" data-id="${n.id}" aria-expanded="${String(!r.closed)}" aria-label="${r.closed ? 'Розгорнути' : 'Згорнути'} ${n.id}" style="border: 0; background: none; padding: 0; cursor: pointer;">${icon('chevDown', 14, 2)}</button>`
    : html`<span style="width: 16px; flex: none;"></span>`}<span class="tid">${n.id}</span><span class="tt" title="${plain(n.title)}">${inline(n.title)}</span>${msgs ? warnIc(msgs.join('\n')) : ''}</span>
    <span>${g ? html`<span class="muted" style="font-size: 12px;">${kids}</span>` : statusBadge(n.status)}</span>
    <span class="c-prog">${progBar(n.progress)}</span>
    <span class="c-sess">${g ? '' : sessionCell(n.sessions, ctx.node)}</span>
    <span class="c-upd mono muted" style="font-size: 12px;">${g ? '' : shortDate(n.updated)}</span></div>`;
}

/** Tree card with the filter segment. data = /tree payload; ui = { path, filter, collapsed, sel, hrefs, sessions? }. */
export function treeCard(data, ui) {
  const filter = ui.filter || 'all';
  const collapsed = ui.collapsed || new Set();
  const msgs = issueMap(data.issues);
  const sessions = {};
  for (const n of data.nodes) if ((n.sessions || []).some((x) => x.effective_state === 'waiting')) sessions[n.id] = { waiting: true };
  const ctx = { issues: new Set(msgs.keys()), issueMsgs: msgs, sessions, node: ui.node };
  const counts = filterCounts(data.nodes, ctx);
  const rows = visibleRows(buildTree(data.nodes), filter, ctx, collapsed);
  const total = data.nodes.filter((n) => !isGroup(n)).length;
  return html`<section class="card tree" aria-labelledby="mv-tree" style="overflow: hidden;">
    <div class="card-h" style="flex-wrap: wrap;"><h2 id="mv-tree" class="sec-t">Дерево тікетів</h2><span class="grow"></span>
      <div class="seg mv-seg" role="group" aria-label="Фільтр">${FILTERS.map(([k, l]) => html`<button type="button" class="${filter === k ? 'on' : ''}" aria-pressed="${String(filter === k)}" data-act="tkfilter" data-v="${k}" data-fk="tkf:${k}">${l}<span class="cnt">${num(counts[k])}</span></button>`)}</div></div>
    <div class="scroll-x" role="table" aria-label="Тікети">
      <div class="thead" style="border-radius: 0; grid-template-columns: ${TREE_COLS};" role="row"><span>Тікет</span><span>Статус</span><span class="c-prog">Прогрес</span><span class="c-sess">Сесія</span><span class="c-upd">Оновл.</span></div>
      ${rows.length ? rows.map((r) => treeRow(r, ctx, ui, ui.path)) : html`<div class="empty" style="min-width: 0;"><span class="t">Нічого не знайдено</span><span style="font-size: 12.5px;">Під цей фільтр тікетів немає.</span></div>`}
    </div>
    <div class="tfoot" style="position: static;"><span>Розділ = тікет <span class="mono">kind: group</span>, дерево за полем <span class="mono">parent</span></span><span class="tnum">${count(total, W.tickets)}</span></div>
  </section>`;
}

/** Project progress: big percent, stacked status bar and legend (cancelled/frozen are outside the bar width). */
export function progressCard(data) {
  const c = data.counts || {};
  const n = (k) => c[k] || 0;
  const tot = BAR_ORDER.reduce((a, k) => a + n(k), 0);
  const legend = BAR_ORDER.concat(['frozen', 'cancelled']).filter(n);
  return html`<section class="card card-b stack" style="gap: 10px;">
    <div class="row between wrap" style="gap: 8px;"><span class="lbl">Прогрес проєкту</span><span class="tnum" style="font-size: 21px; font-weight: 650;">${pct(data.progress)}</span></div>
    <div class="stbar" role="img" aria-label="Розподіл тікетів за статусами">${BAR_ORDER.map((k) => (n(k) ? html`<span class="c-${k}" style="width: ${100 * n(k) / tot}%;"></span>` : ''))}</div>
    <div class="legend">${legend.map((k) => html`<span><i class="c-${k}"></i>${STATUS[k][1]} <span class="tnum" style="color: var(--foreground); font-weight: 600;">${num(n(k))}</span></span>`)}</div>
  </section>`;
}

// ---------------------------------------------------------------------------------------------- ticket card
function depLink(id, byId, path, hrefs) {
  const d = byId[id];
  return d
    ? html`<a class="mono" href="${hrefs.ticket(path, id)}">${id}</a> ${statusBadge(d.status)} <span class="muted">${inline(d.title)}</span>`
    : html`<span class="mono">${id}</span> ${warnIc('Тікета з таким id немає')}`;
}
const idList = (ids, byId, path, hrefs) => ids.map((id, i) => html`${i ? html`<br>` : ''}${depLink(id, byId, path, hrefs)}`);
const safeUrl = (u) => /^https?:\/\//i.test(u || '');

function ancestors(t, byId) {
  const out = [];
  const seen = new Set([t.id]);
  for (let p = t.parent; p && byId[p] && !seen.has(p); p = byId[p].parent) { seen.add(p); out.unshift(byId[p]); }
  return out;
}

/** Ticket (or group) card. t = /ticket payload (or a tree node while it loads); byId = id -> tree node; ui = { hrefs }. */
export function ticketCard(t, byId, ui) {
  const path = t.project_path || ui.path;
  const crumbs = ancestors(t, byId).map((a) => `${a.id} ${plain(a.title)}`).concat(t.id).join(' › ');
  if (isGroup(t)) {
    const nodes = Object.values(byId);
    const kids = (id) => nodes.filter((n) => n.parent === id);
    const under = (id, out = []) => { for (const c of kids(id)) { if (isGroup(c)) under(c.id, out); else out.push(c); } return out; };
    return html`<section class="card" aria-labelledby="mv-tk"><div class="card-h"><span class="mono muted" style="font-size: 12px;">${t.id}</span><h2 id="mv-tk" class="sec-t">${inline(t.title)}</h2></div>
      <div class="card-b stack" style="gap: 12px;">${progBar(t.progress)}
        <div class="kv"><span>Тікетів</span><span>${num(under(t.id).length)}</span><span>Файл</span><span class="mono" style="font-size: 12px; overflow-wrap: anywhere;">${t.path}</span></div></div></section>`;
  }
  const sec = t.sections;
  const goal = t.summary || (sec && sec.goal) || '';
  const dropped = t.body_dropped;
  const deps = t.deps || [], blocks = t.blocks || [];
  const ac = sec ? sec.ac : [];
  const acDone = ac.filter((x) => x.done).length;
  const log = sec ? sec.log.slice().reverse() : [];
  return html`
    <section class="card" aria-labelledby="mv-tk">
      <div class="card-h" style="align-items: flex-start;"><div class="stack" style="gap: 4px; min-width: 0;"><span class="mono muted" style="font-size: 12px; overflow-wrap: anywhere;">${crumbs}</span><h2 id="mv-tk" class="sec-t" style="font-size: 15px;">${inline(t.title)}</h2></div></div>
      <div class="card-b stack" style="gap: 14px;">
        <div class="row wrap" style="gap: 8px;">${statusBadge(t.status)}${t.size ? html`<span class="tag">розмір ${t.size}</span>` : ''}<span style="flex: 1; min-width: 140px;">${progBar(t.progress)}</span></div>
        ${goal ? html`<div class="stack" style="gap: 6px; font-size: 13px;">${sec && !t.summary && sec.goal ? markdown(sec.goal.split('\n')) : inline(goal)}</div>` : ''}
        <div class="kv">
          ${deps.length ? html`<span>Залежить від</span><span>${idList(deps, byId, path, ui.hrefs)}</span>` : ''}
          ${blocks.length ? html`<span>Блокує</span><span>${idList(blocks, byId, path, ui.hrefs)}</span>` : ''}
          ${t.branch ? html`<span>Гілка</span><span class="mono" style="font-size: 12px; overflow-wrap: anywhere;">${t.branch}</span>` : ''}
          ${t.pr ? html`<span>PR</span><span style="overflow-wrap: anywhere;">${safeUrl(t.pr) ? html`<a href="${t.pr}" target="_blank" rel="noopener noreferrer">${t.pr}</a>` : html`<span class="mono">${t.pr}</span>`}</span>` : ''}
          ${t.updated ? html`<span>Оновлено</span><span class="mono" style="font-size: 12px;">${isoDate(t.updated)}</span>` : ''}
        </div>
        ${(t.issues || []).length ? html`<ul class="health" aria-label="Здоров'я тікета">${t.issues.map((i) => html`<li>${warnIc(i.message)}<span>${inline(i.message)}</span></li>`)}</ul>` : ''}
        ${(t.sessions || []).map((x) => sessionBlock(x, { ...ui, project: path, ticket: t.id }))}
      </div>
    </section>
    ${dropped ? html`<section class="card"><div class="card-b" style="font-size: 12.5px;"><span class="muted">${icon('warn', 14, 1.8)} Тіло приховано: містить секрет</span></div></section>` : ''}
    ${!dropped && !sec ? skel(60, 80, 45) : ''}
    ${ac.length ? html`<section class="card" aria-labelledby="mv-ac"><div class="card-h"><h2 id="mv-ac" class="sec-t">Acceptance criteria</h2><span class="grow"></span><span class="hint tnum">${num(acDone)} з ${num(ac.length)}</span></div>
      <div class="card-b"><ul class="ac">${ac.map((x) => html`<li class="${x.done ? 'done' : ''}"><span class="cb ${x.done ? 'on' : ''}" aria-hidden="true"></span><span>${inline(x.text)}</span></li>`)}</ul></div></section>` : ''}
    ${ui.cardExtra || ''}
    ${log.length ? html`<section class="card" aria-labelledby="mv-log"><div class="card-h"><h2 id="mv-log" class="sec-t">Log</h2><span class="grow"></span><span class="hint">з файлу тікета</span></div>
      <div class="card-b log">${log.map((e) => html`<div><div class="h"><span class="mono">${isoDate(e.date)}</span>${e.agent ? html`<b>${e.agent}</b>` : ''}</div>${markdown((e.text || '').split('\n'))}</div>`)}</div></section>` : ''}`;
}

// ---------------------------------------------------------------------------------------------- comments (E3-01)
const cmtWhen = (date, time) => (date ? `${date.slice(8, 10)}.${date.slice(5, 7)}${time ? ' ' + time : ''}` : '');
const isoWhen = (s) => (s && /^\d{4}-\d{2}-\d{2}/.test(s) ? cmtWhen(s.slice(0, 10), /^.{10}[T ](\d{2}:\d{2})/.exec(s)?.[1]) : '');
const cmtHead = (who, when, badge, cls) => html`<div class="row" style="gap: 6px; font-size: 12px;"><strong>${who}</strong><span class="muted mono">${when}</span><span class="grow"></span><span class="bdg ${cls}">${badge}</span></div>`;
const cmtBody = (it) => html`${it.kind === 'status' ? html`<span>запит статусу: ${statusBadge(it.requested_status)}</span>` : ''}${it.text ? html`<span style="white-space: pre-wrap; overflow-wrap: anywhere;">${it.text}</span>` : ''}`;

/** Section «Коментарі» of the ticket card (mockup v3). comments = sections.comments (processed, from the body);
 * inboxItems = files still in inbox ("надіслано"); localDone = items whose file vanished before the body was re-read;
 * ui = { project, ticket, busy, draft: { text, status } }. */
export function commentsBlock(comments, inboxItems, localDone, ui) {
  const done = comments || [];
  const seen = done.map((c) => c.text || '');
  const local = (localDone || []).filter((x) => !(x.text && seen.some((t) => t.includes(x.text.trim().slice(0, 200)))));
  const sent = inboxItems || [];
  const draft = (ui && ui.draft) || {};
  const busy = ui && ui.busy;
  const fk = `${ui.project}\n${ui.ticket}`;
  return html`<section class="card" aria-labelledby="mv-cm"><div class="card-h"><h2 id="mv-cm" class="sec-t">Коментарі</h2><span class="grow"></span><span class="hint">йдуть оркестратору через inbox</span></div>
    <div class="card-b stack" style="gap: 10px;">
      ${done.map((c) => html`<div class="cmt">${cmtHead(c.author || 'Ти', cmtWhen(c.date, c.time), 'оброблено', 't-success')}<span style="white-space: pre-wrap; overflow-wrap: anywhere;">${c.text}</span></div>`)}
      ${local.map((x) => html`<div class="cmt">${cmtHead('Ти', isoWhen(x.created), 'оброблено', 't-success')}${cmtBody(x)}</div>`)}
      ${sent.map((x) => html`<div class="cmt pending">${cmtHead('Ти', isoWhen(x.created), 'надіслано', 't-warning')}${cmtBody(x)}</div>`)}
      <label for="mv-new" class="sr-only">Коментар до тікета</label>
      <textarea id="mv-new" class="inp" data-fk="cmt:${fk}" data-input="cmtext" placeholder="Коментар до тікета" maxlength="4000">${draft.text || ''}</textarea>
      <div class="row wrap" style="gap: 8px;"><button type="button" class="btn sm primary" data-act="inboxsend" data-project="${ui.project}" data-ticket="${ui.ticket}" ${busy ? raw('disabled') : ''}>Надіслати</button>
        <label for="mv-st" class="muted" style="font-size: 12px;">і змінити статус</label>
        <select id="mv-st" class="inp" data-fk="cmst:${fk}" data-change="cmstatus" style="height: 28px; font-size: 12.5px;"><option value="">не змінювати</option>${Object.keys(STATUS).map((k) => html`<option value="${k}" ${draft.status === k ? raw('selected') : ''}>${STATUS[k][0]} ${STATUS[k][1]}</option>`)}</select></div>
    </div></section>`;
}

// ---------------------------------------------------------------------------------------------- pages
/** Body of the "Тікети" tab. data = /tree payload or null (loading); ui = { path, filter, collapsed, sel, card, hrefs, err }. */
export function projectTicketsPage(data, ui) {
  if (ui.err && !data) {
    return html`<section class="card"><div class="empty"><span class="t">Не вдалося завантажити тікети</span><span style="font-size: 12.5px;">${ui.err}</span><a class="btn sm" href="${ui.hrefs.all()}">До всіх проєктів</a></div></section>`;
  }
  if (!data) return html`${skel(30, 100)}${skel(70, 90, 80, 60)}`;
  if (!data.nodes.length) {
    return html`<section class="card"><div class="empty"><span class="t">Тікетів ще немає</span><span style="font-size: 12.5px;">У <span class="mono">${data.dir}</span> нема тікетів за каноном. Перенести наявні: <span class="mono">myvec ticket migrate</span>.</span><a class="btn sm" href="${ui.hrefs.all()}">До всіх проєктів</a></div></section>`;
  }
  const byId = Object.fromEntries(data.nodes.map((n) => [n.id, n]));
  const selNode = ui.sel ? byId[ui.sel] : null;
  const card = ui.card && ui.card.id === ui.sel ? ui.card : selNode;
  let side = '';
  if (ui.card && ui.card.error && ui.card.id === ui.sel) {
    side = html`<section class="card"><div class="empty"><span class="t">Не вдалося завантажити тікет ${ui.sel}</span><span style="font-size: 12.5px;">${ui.card.error}</span></div></section>`;
  } else if (ui.sel && !selNode && !(ui.card && ui.card.id === ui.sel)) {
    side = html`<section class="card"><div class="empty"><span class="t">Тікета ${ui.sel} немає</span><span style="font-size: 12.5px;">Його могли видалити або перейменувати.</span></div></section>`;
  } else if (card) {
    side = ticketCard(card, byId, ui);
  }
  return html`
    ${progressCard(data)}
    <div class="cols">
      <div class="stack main" style="gap: 16px;">${treeCard(data, ui)}</div>
      ${side ? html`<aside class="stack side" id="mv-tkcard" style="gap: 16px;">${side}</aside>` : ''}
    </div>`;
}

/** "Усі проєкти": table over /api/tickets `projects`. summary = { projects } or null (loading); ui = { hrefs, err }. */
export function allProjectsPage(summary, ui) {
  const head = (sub) => html`<div class="row between wrap" style="align-items: flex-start; gap: 12px;"><div><h1 class="h1" style="margin: 0;">Тікети</h1><div class="sub">${sub}</div></div></div>`;
  if (ui.err && !summary) {
    return html`${head('Не вдалося завантажити')}<section class="card"><div class="empty"><span class="t">Не вдалося завантажити тікети</span><span style="font-size: 12.5px;">${ui.err}</span></div></section>`;
  }
  if (!summary) return html`${head('Завантажуємо…')}${skel(40, 80, 70, 60)}`;
  const ps = summary.projects;
  const legacy = ps.filter((p) => !p.n_tickets).length;
  const total = ps.reduce((a, p) => a + p.n_tickets, 0);
  const sub = ps.length
    ? `${count(ps.length, W.projects)}${legacy ? ` · ${num(legacy)} ще не на каноні` : ''}`
    : 'Проєктів із теками тікетів ще немає';
  const c = (p, k) => (p.counts && p.counts[k]) || 0;
  const WORST_COLOR = { error: 'var(--danger)', warning: 'var(--warning)', health: 'var(--info)' };
  const row = (p) => {
    const none = !p.n_tickets;
    const inWork = c(p, 'in_progress') + c(p, 'review') + c(p, 'partial');
    const wait = p.n_waiting != null ? p.n_waiting : c(p, 'waiting');
    const prob = (p.n_issues || 0) + (p.n_health || 0) + (p.n_process || 0);   // = the «Проблеми» tab count (problems.js problemCount)
    const probColor = WORST_COLOR[p.worst] || 'var(--danger)';   // no/unknown worst (old payload) with prob > 0: stay red
    return html`<div class="trow" style="grid-template-columns: ${PROJ_COLS};">
      <span class="row" style="gap: 6px; min-width: 0;"><a class="mono ell" href="${ui.hrefs.project(p.project_path)}">${p.project_path}</a></span>
      <span>${none ? html`<span class="tag">не на каноні</span>` : progBar(p.progress)}</span>
      <span class="num right c-x tnum">${none ? '—' : num(inWork)}</span>
      <span class="num right c-x tnum" style="${wait ? 'color: var(--warning); font-weight: 600;' : ''}">${none ? '—' : num(wait)}</span>
      <span class="num right c-x tnum" style="${prob ? `color: ${probColor}; font-weight: 600;` : ''}">${none ? '—' : prob && ui.hrefs.problems ? html`<a href="${ui.hrefs.problems(p.project_path)}" title="Відкрити вкладку «Проблеми»" style="color: inherit;">${num(prob)}</a>` : num(prob)}</span>
      <span class="muted c-x" style="font-size: 12px;">${p.updated_max ? isoDate(p.updated_max) : '—'}</span></div>`;
  };
  return html`
    ${head(sub)}
    ${waitingCard(ui.waiting, ui)}
    <section class="card projtbl" aria-labelledby="mv-pr" style="overflow: hidden;">
      <div class="card-h"><h2 id="mv-pr" class="sec-t">Проєкти</h2></div>
      ${ps.length ? html`<div class="scroll-x">
        <div class="thead" style="border-radius: 0; grid-template-columns: ${PROJ_COLS};"><span>Проєкт</span><span>Прогрес</span><span class="right c-x">В роботі</span><span class="right c-x">Чекають</span><span class="right c-x">Проблеми</span><span class="c-x">Оновлено</span></div>
        ${ps.map(row)}
      </div>` : html`<div class="empty"><span class="t">Тікетів ще немає</span><span style="font-size: 12.5px;">Створити тікет: <span class="mono">myvec ticket new</span>.</span></div>`}
      <div class="tfoot" style="position: static;"><span>${count(total, W.tickets)}</span><span>${legacy ? html`не на каноні: <span class="mono">myvec ticket migrate</span>` : ''}</span></div>
    </section>`;
}
