import { authNode } from "./auth";
import { err, json } from "./json";
import { MAX_BODY_BYTES, SchemaError, hasHardSecret, validateSnapshot } from "./schema";

async function readBody(req: Request): Promise<string | null> {
  const declared = Number(req.headers.get("Content-Length") ?? 0);
  if (declared > MAX_BODY_BYTES) return null;
  if (!req.body) return "";
  const reader = req.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > MAX_BODY_BYTES) {
      await reader.cancel();
      return null;
    }
    chunks.push(value);
  }
  const all = new Uint8Array(size);
  let off = 0;
  for (const c of chunks) {
    all.set(c, off);
    off += c.byteLength;
  }
  return new TextDecoder().decode(all);
}

export async function handleSnapshot(req: Request, env: Env): Promise<Response> {
  if (req.method !== "POST") return err("method_not_allowed", 405);
  if (!(req.headers.get("Content-Type") ?? "").toLowerCase().startsWith("application/json"))
    return err("unsupported_media_type", 415);
  const auth = await authNode(req, env);
  if (!auth.ok) return err("unauthorized", auth.status);

  const text = await readBody(req);
  if (text === null) return err("payload_too_large", 413);
  let body: unknown;
  try {
    body = JSON.parse(text);
  } catch {
    return err("invalid_json", 400);
  }
  if (hasHardSecret(text) || hasHardSecret(JSON.stringify(body))) return err("rejected_secret", 400);
  if (typeof body === "object" && body !== null && "node" in body) {
    const n = (body as { node: unknown }).node;
    if (n !== undefined && n !== null && n !== auth.node) return err("forbidden", 403);
  }
  let snap;
  try {
    snap = validateSnapshot(body);
  } catch (e) {
    if (e instanceof SchemaError) return err(e.code, 400, e.field || undefined);
    throw e;
  }

  const node = auth.node;
  const now = Date.now();
  const ticketCount = snap.projects.reduce((n, p) => n + p.tickets.length, 0);
  const ack = { v: 1, type: "snapshot.ack", gen: snap.gen, projects: snap.projects.length, tickets: ticketCount, sessions: snap.sessions.length };

  // Fast path (also keeps an equal-gen retry write-free); the authoritative guard is the one in the batch below.
  const cur = await env.DB.prepare("SELECT last_gen FROM nodes WHERE name = ?").bind(node).first<{ last_gen: number | null }>();
  if (!cur) return err("unauthorized", 403);
  if (cur.last_gen !== null && snap.gen < cur.last_gen) return err("stale_gen", 409);
  if (cur.last_gen !== null && snap.gen === cur.last_gen) return json(ack);

  // The gen guard lives inside the atomic batch: the first UPDATE moves last_gen only forward, and every
  // other statement is conditioned on last_gen == this gen, so a stale concurrent batch changes nothing.
  const q = (sql: string) => env.DB.prepare(sql);
  const guard = "WHERE (SELECT last_gen FROM nodes WHERE name = ?) = ?";
  const stmts: D1PreparedStatement[] = [
    q(
      "UPDATE nodes SET last_gen = ?, last_seen_at = ?, last_snapshot_at = ?, myvec_version = ? WHERE name = ? AND (last_gen IS NULL OR last_gen < ?)",
    ).bind(snap.gen, now, now, snap.myvec_version, node, snap.gen),
    q(`DELETE FROM tickets WHERE node = ? AND (SELECT last_gen FROM nodes WHERE name = ?) = ?`).bind(node, node, snap.gen),
    q(`DELETE FROM sessions WHERE node = ? AND (SELECT last_gen FROM nodes WHERE name = ?) = ?`).bind(node, node, snap.gen),
    q(`DELETE FROM node_projects WHERE node = ? AND (SELECT last_gen FROM nodes WHERE name = ?) = ?`).bind(node, node, snap.gen),
  ];
  for (const p of snap.projects) {
    stmts.push(
      q(
        `INSERT INTO node_projects (node, project, fingerprint, head_sha, head_time, head_branch, dirty, tickets_mtime, received_at) SELECT ?,?,?,?,?,?,?,?,? ${guard}`,
      ).bind(node, p.project, p.fingerprint, p.head_sha, p.head_time, p.head_branch, p.dirty, p.tickets_mtime, now, node, snap.gen),
    );
    for (const t of p.tickets)
      stmts.push(
        q(`INSERT INTO tickets (node, project, id, kind, status, parent, data) SELECT ?,?,?,?,?,?,? ${guard}`).bind(
          node, p.project, t.id, t.kind, t.status, t.parent, JSON.stringify(t.data), node, snap.gen,
        ),
      );
  }
  for (const s of snap.sessions)
    stmts.push(
      q(`INSERT INTO sessions (node, session_id, project, data) SELECT ?,?,?,? ${guard}`).bind(
        node, s.session_id, s.project, JSON.stringify(s.data), node, snap.gen,
      ),
    );
  const results = await env.DB.batch(stmts);
  if (results[0].meta.changes === 0) {
    const row = await env.DB.prepare("SELECT last_gen FROM nodes WHERE name = ?").bind(node).first<{ last_gen: number | null }>();
    if (!row) return err("unauthorized", 403);
    if (row.last_gen !== null && snap.gen < row.last_gen) return err("stale_gen", 409);
    // Equal gen: a concurrent twin of the same gen won; its data is already there.
  }
  return json(ack);
}
