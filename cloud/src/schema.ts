// Hand-written validator for the node snapshot (ADR-047 + ADR-050). Errors name the field, never the value.

export const MAX_BODY_BYTES = 8 * 1024 * 1024;
export const MAX_PROJECTS = 200;
export const MAX_TICKETS_PER_PROJECT = 5000;
export const MAX_TICKETS = 20000;
export const MAX_SESSIONS = 2000;
export const MAX_STR = 16384;
export const MAX_JSON_FIELD = 262144;
/** D1 caps a row at ~2 MB; stay well below it for the serialized `data` of a ticket or a session. */
export const MAX_DATA_BYTES = 1048576;

export class SchemaError extends Error {
  constructor(
    public code: string,
    public field = "",
  ) {
    super(code);
  }
}

export interface TicketIn {
  id: string;
  kind: string | null;
  status: string | null;
  parent: string | null;
  data: Record<string, unknown>;
}
export interface ProjectIn {
  project: string;
  fingerprint: string | null;
  head_sha: string | null;
  head_time: number | null;
  head_branch: string | null;
  dirty: number;
  tickets_mtime: number | null;
  tickets: TicketIn[];
}
export interface SessionIn {
  session_id: string;
  project: string | null;
  data: Record<string, unknown>;
}
export interface SnapshotIn {
  gen: number;
  ts: number | null;
  node: string | null;
  myvec_version: string | null;
  projects: ProjectIn[];
  sessions: SessionIn[];
}

type Kind = "str" | "strnull" | "num" | "numnull" | "strnum" | "strlist" | "json" | "int";
const TICKET_FIELDS: Record<string, Kind> = {
  id: "str", title: "str", kind: "strnull", status: "strnull", parent: "strnull", size: "strnull",
  deps: "strlist", blocks: "strlist", branch: "strnull", pr: "strnum", summary: "strnull",
  updated: "strnum", progress: "numnull", body_sections: "json", inbox: "int",
};
const SESSION_FIELDS: Record<string, Kind> = {
  session_id: "str", project: "strnull", effective_state: "strnull", last_seen: "numnull",
  last_state_event_at: "numnull", wait_s: "numnull", ticket_id: "strnull", ticket_source: "strnull",
  title: "strnull", question: "strnull", question_at: "strnum", question_options: "json",
  last_message: "strnull", mode: "strnull", answer_kind: "strnull", cwd: "strnull",
};

const PROJECT_KEY = /^(?:git:\S+|local:[^:\s]+:.+)$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const PEM = /-----BEGIN [A-Z ]*PRIVATE KEY-----/;
const AKIA = /AKIA[0-9A-Z]{16}/;

/** Defensive check only; it does not replace the node's `filters.py`. */
export function hasHardSecret(text: string): boolean {
  return PEM.test(text) || AKIA.test(text);
}

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

function checkValue(kind: Kind, v: unknown, field: string): unknown {
  const bad = () => new SchemaError("invalid_field", field);
  const str = (x: unknown) => {
    if (typeof x !== "string" || x.length > MAX_STR) throw bad();
    return x;
  };
  const num = (x: unknown) => {
    if (typeof x !== "number" || !Number.isFinite(x)) throw bad();
    return x;
  };
  switch (kind) {
    case "str": return str(v);
    case "strnull": return v === null ? null : str(v);
    case "num": return num(v);
    case "numnull": return v === null ? null : num(v);
    case "strnum": return v === null || typeof v === "number" ? (v === null ? null : num(v)) : str(v);
    case "int":
      if (!Number.isInteger(v) || (v as number) < 0) throw bad();
      return v;
    case "strlist":
      if (!Array.isArray(v) || v.length > 1000) throw bad();
      return v.map(str);
    case "json":
      if (v === null) return null;
      if (typeof v !== "object" || JSON.stringify(v).length > MAX_JSON_FIELD) throw bad();
      return v;
  }
}

function checkRowSize(data: Record<string, unknown>, field: string): void {
  if (new TextEncoder().encode(JSON.stringify(data)).length > MAX_DATA_BYTES) throw new SchemaError("invalid_field", field);
}

function pick(obj: Record<string, unknown>, spec: Record<string, Kind>, path: string): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, kind] of Object.entries(spec)) {
    if (obj[k] === undefined) continue;
    out[k] = checkValue(kind, obj[k], `${path}.${k}`);
  }
  return out;
}

function optStr(v: unknown, field: string): string | null {
  return v === undefined || v === null ? null : (checkValue("str", v, field) as string);
}
function optNum(v: unknown, field: string): number | null {
  return v === undefined || v === null ? null : (checkValue("num", v, field) as number);
}

function projectKey(v: unknown, field: string): string {
  if (typeof v !== "string" || v.length > 512 || !PROJECT_KEY.test(v)) throw new SchemaError("invalid_field", field);
  return v;
}

export function validateSnapshot(body: unknown): SnapshotIn {
  if (!isObj(body)) throw new SchemaError("invalid_field", "body");
  if (body.v !== 1) throw new SchemaError("unsupported_version", "v");
  if (body.type !== "snapshot") throw new SchemaError("invalid_field", "type");
  if (typeof body.gen !== "number" || !Number.isSafeInteger(body.gen) || body.gen < 0)
    throw new SchemaError("invalid_field", "gen");
  if (!Array.isArray(body.projects) || body.projects.length > MAX_PROJECTS) throw new SchemaError("invalid_field", "projects");
  const sessionsRaw = body.sessions ?? [];
  if (!Array.isArray(sessionsRaw) || sessionsRaw.length > MAX_SESSIONS) throw new SchemaError("invalid_field", "sessions");

  const seen = new Set<string>();
  let total = 0;
  const projects = body.projects.map((p: unknown, i: number): ProjectIn => {
    const at = `projects[${i}]`;
    if (!isObj(p)) throw new SchemaError("invalid_field", at);
    const key = projectKey(p.project, `${at}.project`);
    if (seen.has(key)) throw new SchemaError("duplicate", `${at}.project`);
    seen.add(key);
    const head = p.head === undefined || p.head === null ? {} : p.head;
    if (!isObj(head)) throw new SchemaError("invalid_field", `${at}.head`);
    if (p.dirty !== undefined && typeof p.dirty !== "boolean") throw new SchemaError("invalid_field", `${at}.dirty`);
    if (!Array.isArray(p.tickets) || p.tickets.length > MAX_TICKETS_PER_PROJECT)
      throw new SchemaError("invalid_field", `${at}.tickets`);
    total += p.tickets.length;
    if (total > MAX_TICKETS) throw new SchemaError("invalid_field", "tickets");
    const ids = new Set<string>();
    const tickets = p.tickets.map((t: unknown, j: number): TicketIn => {
      const tp = `${at}.tickets[${j}]`;
      if (!isObj(t)) throw new SchemaError("invalid_field", tp);
      const data = pick(t, TICKET_FIELDS, tp);
      checkRowSize(data, tp);
      if (typeof data.id !== "string" || !data.id) throw new SchemaError("invalid_field", `${tp}.id`);
      if (ids.has(data.id)) throw new SchemaError("duplicate", `${tp}.id`);
      ids.add(data.id);
      return {
        id: data.id,
        kind: (data.kind as string | null | undefined) ?? null,
        status: (data.status as string | null | undefined) ?? null,
        parent: (data.parent as string | null | undefined) ?? null,
        data,
      };
    });
    return {
      project: key,
      fingerprint: optStr(p.fingerprint, `${at}.fingerprint`),
      head_sha: optStr(head.sha, `${at}.head.sha`),
      head_time: optNum(head.time, `${at}.head.time`),
      head_branch: optStr(head.branch, `${at}.head.branch`),
      dirty: p.dirty ? 1 : 0,
      tickets_mtime: optNum(p.tickets_mtime, `${at}.tickets_mtime`),
      tickets,
    };
  });

  const sids = new Set<string>();
  const sessions = sessionsRaw.map((s: unknown, i: number): SessionIn => {
    const at = `sessions[${i}]`;
    if (!isObj(s)) throw new SchemaError("invalid_field", at);
    const data = pick(s, SESSION_FIELDS, at);
    checkRowSize(data, at);
    const sid = data.session_id;
    if (typeof sid !== "string" || !UUID.test(sid)) throw new SchemaError("invalid_field", `${at}.session_id`);
    if (sids.has(sid)) throw new SchemaError("duplicate", `${at}.session_id`);
    sids.add(sid);
    if (typeof data.project === "string") projectKey(data.project, `${at}.project`);
    if (typeof data.cwd === "string" && !(data.cwd === "~" || data.cwd.startsWith("~/")))
      throw new SchemaError("invalid_field", `${at}.cwd`);
    return { session_id: sid, project: (data.project as string | null | undefined) ?? null, data };
  });

  return {
    gen: body.gen,
    ts: optNum(body.ts, "ts"),
    node: body.node === undefined || body.node === null ? null : optStr(body.node, "node"),
    myvec_version: optStr(body.myvec_version, "myvec_version"),
    projects,
    sessions,
  };
}
