import { env } from "cloudflare:workers";
import worker from "../src/index";
import { hashToken } from "../src/auth";

export const UUID1 = "11111111-1111-4111-8111-111111111111";
export const UUID2 = "22222222-2222-4222-8222-222222222222";
export const PROJECT = "git:github.com/acme/repo";

export function genToken(): string {
  const raw = crypto.getRandomValues(new Uint8Array(32));
  return btoa(String.fromCharCode(...raw)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export async function resetDb(): Promise<void> {
  for (const t of ["tickets", "sessions", "node_projects", "node_tokens", "nodes"])
    await env.DB.prepare(`DELETE FROM ${t}`).run();
}

/** Registers a node with a fresh token (only the hash goes to D1) and returns the raw token. */
export async function mint(node: string): Promise<string> {
  const token = genToken();
  await env.DB.prepare("INSERT OR IGNORE INTO nodes (name, created_at) VALUES (?, ?)").bind(node, 1).run();
  await env.DB.prepare("INSERT INTO node_tokens (token_hash, node, created_at) VALUES (?, ?, ?)")
    .bind(await hashToken(token), node, 1)
    .run();
  return token;
}

export function call(path: string, init: RequestInit = {}, bypass?: string, host = "127.0.0.1"): Promise<Response> {
  const e = { DB: env.DB, ACCESS_DEV_BYPASS: bypass } as Env;
  return worker.fetch(new Request(`http://${host}${path}`, init), e);
}

export function post(token: string | null, body: unknown, extra: Record<string, string> = {}): Promise<Response> {
  const headers: Record<string, string> = { "Content-Type": "application/json", ...extra };
  if (token) headers.Authorization = `Bearer ${token}`;
  return call("/node/v1/snapshot", { method: "POST", headers, body: typeof body === "string" ? body : JSON.stringify(body) });
}

export function snap(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    v: 1,
    type: "snapshot",
    gen: 1000,
    ts: 1,
    projects: [
      {
        project: PROJECT,
        fingerprint: "abc",
        head: { sha: "deadbeef", time: 1700000000, branch: "main" },
        dirty: false,
        tickets_mtime: 1700000001,
        tickets: [
          { id: "E1-01", title: "One", kind: "ticket", status: "⬜", parent: "E1", deps: [], evil: "drop-me" },
          { id: "E1-02", title: "Two", status: "✅", progress: 50, inbox: 2 },
        ],
      },
    ],
    sessions: [{ session_id: UUID1, project: PROJECT, effective_state: "waiting", cwd: "~/proj", prompt: "drop-me" }],
    ...over,
  };
}

export const read = (path: string, host?: string) => call(path, {}, "1", host);
