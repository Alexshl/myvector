// Tiny templating + formatting helpers (no dependencies).

// ---- html tagged template: every interpolated value is escaped unless it is itself html`` output ----
class Safe {
  constructor(s) { this.s = s; }
  toString() { return this.s; }
}
const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
export const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ESC[c]);
const part = (v) => {
  if (v == null || v === false || v === true) return '';
  if (v instanceof Safe) return v.s;
  if (Array.isArray(v)) return v.map(part).join('');
  return esc(v);
};
export function html(strings, ...vals) {
  let out = strings[0];
  vals.forEach((v, i) => { out += part(v) + strings[i + 1]; });
  return new Safe(out);
}
export const raw = (s) => new Safe(s);
export const toStr = (v) => part(v);

// ---- numbers: Ukrainian locale (non-breaking space thousands, decimal comma) ----
const NF = new Intl.NumberFormat('uk-UA');
const fixed = (d) => new Intl.NumberFormat('uk-UA', { minimumFractionDigits: d, maximumFractionDigits: d });
const NF1 = fixed(1);
const NF2 = fixed(2);
const NF3 = fixed(3);
const NF4 = fixed(4);
export const num = (v) => (v == null ? '—' : NF.format(v));
export const dec1 = (v) => NF1.format(v);
export const dec2 = (v) => NF2.format(v);
export const dec3 = (v) => NF3.format(v);
export const dec4 = (v) => NF4.format(v);

/** Ukrainian plural form: 1, 21, 31... -> one; 2-4, 22-24... -> few; the rest (and 11-14) -> many. */
export function plural(n, one, few, many) {
  const a = Math.abs(Math.trunc(n)) % 100, b = a % 10;
  if (a > 10 && a < 20) return many;
  if (b === 1) return one;
  if (b >= 2 && b <= 4) return few;
  return many;
}
/** "21 шматок": number + noun in the right form, forms = [one, few, many]. */
export const count = (n, forms) => `${num(n)} ${plural(n, ...forms)}`;
export const TOKENS = ['токен', 'токени', 'токенів'];

export function bytes(b) {
  if (b == null) return '—';
  if (b < 1024) return `${num(b)} Б`;
  const units = ['КБ', 'МБ', 'ГБ', 'ТБ'];
  let v = b / 1024, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v >= 100 || Number.isInteger(v) ? num(Math.round(v)) : dec1(v)} ${units[i]}`;
}

// ---- time: 24 h, dates dd.mm.yyyy ----
const p2 = (x) => String(x).padStart(2, '0');
const D = (ts) => new Date(ts * 1000);
export const hm = (ts) => { const d = D(ts); return `${p2(d.getHours())}:${p2(d.getMinutes())}`; };
export const hms = (ts) => { const d = D(ts); return `${hm(ts)}:${p2(d.getSeconds())}`; };
export const dmy = (ts) => { const d = D(ts); return `${p2(d.getDate())}.${p2(d.getMonth() + 1)}.${d.getFullYear()}`; };
export const dmyhm = (ts) => `${dmy(ts)} ${hm(ts)}`;
const sameDay = (a, b) => D(a).toDateString() === D(b).toDateString();
/** Time today, dd.mm otherwise. */
export const short = (ts, now = Date.now() / 1000) => {
  if (!ts) return '—';
  const d = D(ts);
  return sameDay(ts, now) ? hm(ts) : `${p2(d.getDate())}.${p2(d.getMonth() + 1)}`;
};
export const dayLabel = (ts, now = Date.now() / 1000) => {
  if (sameDay(ts, now)) return `Сьогодні, ${dmy(ts)}`;
  if (sameDay(ts, now - 86400)) return `Вчора, ${dmy(ts)}`;
  return dmy(ts);
};

/** Seconds -> "1 год 24 хв", "45 хв", "30 с". */
export function dur(sec) {
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return `${sec} с`;
  const m = Math.round(sec / 60), h = Math.floor(m / 60), mm = m % 60;
  if (!h) return `${mm} хв`;
  return mm ? `${h} год ${mm} хв` : `${h} год`;
}
/** Seconds with tenths: "41,8 с", "3 хв 12 с", "1 год 05 хв". */
export function durS(sec) {
  if (sec == null) return '—';
  if (sec < 60) return `${dec1(Math.max(0, sec))} с`;
  const t = Math.round(sec), h = Math.floor(t / 3600), m = Math.floor(t / 60) % 60, s = t % 60;
  return h ? `${h} год ${p2(m)} хв` : `${m} хв ${p2(s)} с`;
}
/** Cores of CPU time as "4,6 ядра". */
export const cores = (x) => (x == null ? '—' : `${dec1(x)} ядра`);
export const ms = (v) => (v == null ? '—' : v < 1000 ? `${num(Math.round(v))} мс` : `${dec1(v / 1000)} с`);
export const pct = (v) => (v == null ? '—' : `${new Intl.NumberFormat('uk-UA').format(Math.round(v))}%`);
export const hours = (h) => (h < 1 ? `${Math.round(h * 60)} хв` : Number.isInteger(h) ? `${h} год` : `${dec1(h)} год`);

// ---- icons (Lucide paths, as used in the mockups) ----
const ICONS = {
  mark: '<ellipse cx="12" cy="12" rx="10" ry="4.5" transform="rotate(-30 12 12)"/><circle cx="12" cy="12" r="2.4"/><circle cx="20.66" cy="7" r="1.3" fill="currentColor"/>',
  grid: '<path d="M3 3h7v9H3zM14 3h7v5h-7zM14 12h7v9h-7zM3 16h7v5H3z"/>',
  folders: '<path d="M20 10a1 1 0 0 0 1-1V6a1 1 0 0 0-1-1h-2.5a1 1 0 0 1-.8-.4l-.9-1.2A1 1 0 0 0 15 3h-2a1 1 0 0 0-1 1v5a1 1 0 0 0 1 1Z"/><path d="M20 21a1 1 0 0 0 1-1v-3a1 1 0 0 0-1-1h-2.9a1 1 0 0 1-.88-.55l-.42-.85a1 1 0 0 0-.92-.6H13a1 1 0 0 0-1 1v5a1 1 0 0 0 1 1Z"/><path d="M3 5a2 2 0 0 0 2 2h3"/><path d="M3 3v13a2 2 0 0 0 2 2h3"/>',
  activity: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
  book: '<path d="M10 2v8l3-3 3 3V2"/><path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H19a1 1 0 0 1 1 1v18a1 1 0 0 1-1 1H6.5a1 1 0 0 1 0-5H20"/>',
  search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
  settings: '<path d="M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0M12 1v3M12 20v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M1 12h3M20 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/>',
  moon: '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/>',
  refresh: '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>',
  download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/>',
  chevDown: '<path d="m6 9 6 6 6-6"/>',
  chevRight: '<path d="m9 18 6-6-6-6"/>',
  external: '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
  branch: '<line x1="6" x2="6" y1="3" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/>',
  pin: '<path d="M12 17v5"/><path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z"/>',
  promote: '<circle cx="12" cy="12" r="10"/><path d="m16 12-4-4-4 4"/><path d="M12 16V8"/>',
  archive: '<rect width="20" height="5" x="2" y="3" rx="1"/><path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8"/><path d="M10 12h4"/>',
  copy: '<rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
  alert: '<circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/>',
  warn: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4M12 17h.01"/>',
  info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>',
  folderEmpty: '<path d="m6 14 1.5-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.54 6a2 2 0 0 1-1.95 1.5H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.9a2 2 0 0 1 1.69.9l.81 1.2a2 2 0 0 0 1.67.9H18a2 2 0 0 1 2 2v2"/>',
  loader: '<path d="M21 12a9 9 0 1 1-6.219-8.56"/>',
  checkCircle: '<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>',
  send: '<path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/>',
  play: '<polygon points="6 3 20 12 6 21 6 3"/>',
  circle: '<circle cx="12" cy="12" r="10"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  gauge: '<path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/>',
  tickets: '<path d="M9 6h11M9 12h11M9 18h11"/><path d="m3 6 1 1 2-2M3 12l1 1 2-2M3 18l1 1 2-2"/>',
};
export function icon(name, size = 15, sw = 1.6, color = 'currentColor') {
  return raw(`<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="${color}" stroke-width="${sw}" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name]}</svg>`);
}

// ---- links ----
/** Project path -> route hash, slashes kept. */
export const projectHref = (path) => '#/project/' + path.split('/').map(encodeURIComponent).join('/');
export const obsidianFile = (abs) => 'obsidian://open?path=' + encodeURIComponent(abs);

// ---- paths: $HOME -> ~, long ones get a middle ellipsis (full path goes into a title attribute) ----
export const tilde = (p, home) => {
  if (!p || !home) return p || '';
  const h = home.replace(/\/+$/, '');
  return p === h ? '~' : p.startsWith(h + '/') ? '~' + p.slice(h.length) : p;
};
export function midEllipsis(p, max = 44) {
  if (p.length <= max) return p;
  const tail = Math.ceil((max - 1) * 0.65), head = max - 1 - tail;
  return p.slice(0, head) + '…' + p.slice(p.length - tail);
}
/** <span title="full path">~/short…path</span> */
export const pathEl = (p, home, max = 44) => {
  const t = tilde(p, home);
  return html`<span class="mono" title="${p}">${midEllipsis(t, max)}</span>`;
};

// ---- dates and markdown subset (shared by memory and tracker views) ----
export const isoDate = (s) => (s ? `${s.slice(8, 10)}.${s.slice(5, 7)}.${s.slice(0, 4)}` : '—');

/** Inline markdown subset: `code`, **bold**, [[wikilink]]. */
export function inline(s, memIds) {
  return (s || '').split(/(`[^`]+`|\*\*[^*]+\*\*|\[\[[^\]\n]+\]\])/).map((t) => {
    if (t.startsWith('`') && t.endsWith('`') && t.length > 2) return html`<span class="mono" style="font-size: 12.5px;">${t.slice(1, -1)}</span>`;
    if (t.startsWith('**') && t.endsWith('**') && t.length > 4) return html`<strong>${t.slice(2, -2)}</strong>`;
    if (t.startsWith('[[') && t.endsWith(']]')) return html`<span class="mono" style="font-size: 12.5px;">${t}</span>`;
    return t;
  });
}
export function markdown(lines) {
  const out = [];
  let list = null, para = [];
  const flush = () => { if (para.length) { out.push(html`<p>${inline(para.join(' '))}</p>`); para = []; } };
  const endList = () => { if (list) { out.push(html`<ul>${list.map((i) => html`<li>${inline(i)}</li>`)}</ul>`); list = null; } };
  for (const l of lines) {
    const m = /^\s*[-*]\s+(.*)$/.exec(l);
    if (m) { flush(); (list = list || []).push(m[1]); } else if (!l.trim()) { flush(); endList(); } else { endList(); para.push(l.trim()); }
  }
  flush(); endList();
  return out;
}
