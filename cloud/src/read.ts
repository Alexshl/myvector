import { err, json } from "./json";

/**
 * Browser guard. Fail-closed: 403 until Access JWT verification exists (E4-05, replaces this body).
 * Local bypass needs BOTH ACCESS_DEV_BYPASS="1" (not in wrangler.toml) and a localhost hostname.
 */
export function requireBrowser(req: Request, env: Env): Response | null {
  const host = new URL(req.url).hostname;
  if (env.ACCESS_DEV_BYPASS === "1" && (host === "localhost" || host === "127.0.0.1")) return null;
  return err("forbidden", 403);
}

type Row = Record<string, unknown>;
const unpack = (r: Row): Row => ({ ...(JSON.parse(String(r.data)) as Row) });

export async function handleRead(req: Request, env: Env): Promise<Response> {
  const denied = requireBrowser(req, env);
  if (denied) return denied;
  if (req.method !== "GET") return err("method_not_allowed", 405);
  const url = new URL(req.url);
  const node = url.searchParams.get("node");
  const project = url.searchParams.get("project");

  switch (url.pathname) {
    case "/api/v1/nodes": {
      const { results } = await env.DB.prepare(
        "SELECT name, last_seen_at, last_gen, last_snapshot_at, myvec_version FROM nodes ORDER BY name",
      ).all<Row>();
      return json({ nodes: results });
    }
    case "/api/v1/projects": {
      const { results } = await env.DB.prepare(
        `SELECT p.node, p.project, p.fingerprint, p.head_sha, p.head_time, p.head_branch, p.dirty, p.tickets_mtime, p.received_at,
                (SELECT COUNT(*) FROM tickets t WHERE t.node = p.node AND t.project = p.project) AS ticket_count
         FROM node_projects p ORDER BY p.project, p.node`,
      ).all<Row>();
      return json({ projects: results });
    }
    case "/api/v1/tickets": {
      if (!project) return err("missing_project", 400);
      const stmt = node
        ? env.DB.prepare("SELECT node, project, data FROM tickets WHERE project = ? AND node = ? ORDER BY node, id").bind(project, node)
        : env.DB.prepare("SELECT node, project, data FROM tickets WHERE project = ? ORDER BY node, id").bind(project);
      const { results } = await stmt.all<Row>();
      return json({ tickets: results.map((r) => ({ node: r.node, project: r.project, ...unpack(r) })) });
    }
    case "/api/v1/sessions": {
      const { results } = await env.DB.prepare(
        "SELECT node, data FROM sessions WHERE (?1 IS NULL OR node = ?1) AND (?2 IS NULL OR project = ?2) ORDER BY node, session_id",
      )
        .bind(node, project)
        .all<Row>();
      return json({ sessions: results.map((r) => ({ node: r.node, ...unpack(r) })) });
    }
  }
  return err("not_found", 404);
}
