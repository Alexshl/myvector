import { beforeEach, describe, expect, it } from "vitest";
import { env } from "cloudflare:workers";
import { PROJECT, UUID1, UUID2, mint, post, read, resetDb, snap } from "./helpers";

beforeEach(resetDb);

const count = async (t: string) =>
  (await env.DB.prepare(`SELECT COUNT(*) AS n FROM ${t}`).first<{ n: number }>())!.n;

describe("snapshot", () => {
  it("accepts a valid snapshot and reads it back, dropping unknown fields", async () => {
    const token = await mint("dev-a");
    const r = await post(token, snap());
    expect(r.status).toBe(200);
    expect(await r.json()).toEqual({ v: 1, type: "snapshot.ack", gen: 1000, projects: 1, tickets: 2, sessions: 1 });

    const tickets = (await (await read(`/api/v1/tickets?project=${encodeURIComponent(PROJECT)}`)).json()) as {
      tickets: Record<string, unknown>[];
    };
    expect(tickets.tickets.map((t) => t.id)).toEqual(["E1-01", "E1-02"]);
    expect(tickets.tickets[0]).toMatchObject({ node: "dev-a", title: "One", status: "⬜", parent: "E1" });
    expect(tickets.tickets[1]).toMatchObject({ progress: 50, inbox: 2 });
    expect(JSON.stringify(tickets)).not.toContain("drop-me");

    const sessions = (await (await read("/api/v1/sessions?node=dev-a")).json()) as { sessions: Record<string, unknown>[] };
    expect(sessions.sessions[0]).toMatchObject({ session_id: UUID1, cwd: "~/proj", effective_state: "waiting" });
    expect(JSON.stringify(sessions)).not.toContain("drop-me");

    const projects = (await (await read("/api/v1/projects")).json()) as { projects: Record<string, unknown>[] };
    expect(projects.projects[0]).toMatchObject({ node: "dev-a", head_sha: "deadbeef", head_branch: "main", ticket_count: 2, dirty: 0 });
    const nodes = (await (await read("/api/v1/nodes")).json()) as { nodes: Record<string, unknown>[] };
    expect(nodes.nodes[0]).toMatchObject({ name: "dev-a", last_gen: 1000 });
    expect(JSON.stringify(nodes)).not.toContain("token_hash");
  });

  it("rejects bad input with 400 and no echo of values", async () => {
    const token = await mint("dev-a");
    const proj = (snap().projects as Record<string, unknown>[])[0]!;
    const cases: [string, unknown, string][] = [
      ["version", snap({ v: 2 }), "unsupported_version"],
      ["absolute cwd", snap({ sessions: [{ session_id: UUID1, cwd: "/home/zzsecretuser/x" }] }), "invalid_field"],
      ["non-uuid sid", snap({ sessions: [{ session_id: "nope-zz" }] }), "invalid_field"],
      ["bad project key", snap({ projects: [{ ...proj, project: "bogus key zz" }] }), "invalid_field"],
      ["bad ticket type", snap({ projects: [{ ...proj, tickets: [{ id: "A-1", deps: "x" }] }] }), "invalid_field"],
      ["pem", snap({ sessions: [{ session_id: UUID2, last_message: "-----BEGIN RSA PRIVATE KEY-----\nzz" }] }), "rejected_secret"],
      ["akia", snap({ sessions: [{ session_id: UUID2, last_message: "AKIAABCDEFGHIJKLMNOP" }] }), "rejected_secret"],
    ];
    for (const [name, body, code] of cases) {
      const r = await post(token, body);
      expect(r.status, name).toBe(400);
      const text = await r.text();
      expect(JSON.parse(text).error, name).toBe(code);
      expect(text, name).not.toMatch(/zzsecretuser|AKIA|PRIVATE KEY|bogus key/);
    }
    expect(await post(token, "{not json").then((r) => r.status)).toBe(400);
    expect(await count("tickets")).toBe(0);
  });

  it("returns 413 above 8 MiB", async () => {
    const token = await mint("dev-a");
    const r = await post(token, "x".repeat(8 * 1024 * 1024 + 1));
    expect(r.status).toBe(413);
  });

  it("replaces node data atomically: a dropped project disappears with tickets and sessions", async () => {
    const token = await mint("dev-a");
    await post(token, snap());
    const second = snap({ gen: 1001, projects: [], sessions: [] });
    expect((await post(token, second)).status).toBe(200);
    expect(await count("tickets")).toBe(0);
    expect(await count("node_projects")).toBe(0);
    expect(await count("sessions")).toBe(0);
  });

  it("does not touch other nodes when one replaces its snapshot", async () => {
    const a = await mint("dev-a");
    const b = await mint("dev-b");
    await post(a, snap());
    await post(b, snap({ sessions: [{ session_id: UUID2 }] }));
    await post(a, snap({ gen: 2000, projects: [], sessions: [] }));
    expect(await count("tickets")).toBe(2);
    expect(await count("sessions")).toBe(1);
  });

  it("gen: lower -> 409, same -> 200 without changes, higher -> applied", async () => {
    const token = await mint("dev-a");
    await post(token, snap({ gen: 5 }));
    expect((await post(token, snap({ gen: 4, projects: [], sessions: [] }))).status).toBe(409);
    expect(await count("tickets")).toBe(2);
    const same = await post(token, snap({ gen: 5, projects: [], sessions: [] }));
    expect(same.status).toBe(200);
    expect(await count("tickets")).toBe(2);
    expect((await post(token, snap({ gen: 6, projects: [], sessions: [] }))).status).toBe(200);
    expect(await count("tickets")).toBe(0);
  });

  it("concurrent gen 6 and gen 5 never leave gen 5 data (atomic guard)", async () => {
    const token = await mint("dev-a");
    await post(token, snap({ gen: 4, projects: [], sessions: [] }));
    const mk = (gen: number, id: string) =>
      snap({
        gen,
        sessions: [],
        projects: [{ project: PROJECT, tickets: [{ id, title: `g${gen}` }] }],
      });
    const [r6, r5] = await Promise.all([post(token, mk(6, "E9-06")), post(token, mk(5, "E9-05"))]);
    expect(r6.status).toBe(200);
    expect([200, 409]).toContain(r5.status);
    const rows = (await env.DB.prepare("SELECT id FROM tickets").all<{ id: string }>()).results;
    expect(rows.map((r) => r.id)).toEqual(["E9-06"]);
    const n = await env.DB.prepare("SELECT last_gen FROM nodes WHERE name = 'dev-a'").first<{ last_gen: number }>();
    expect(n?.last_gen).toBe(6);
  });

  it("a stale batch that slips past the fast path changes nothing", async () => {
    const token = await mint("dev-a");
    await post(token, snap({ gen: 9 }));
    await env.DB.prepare("UPDATE nodes SET last_gen = 3 WHERE name = 'dev-a'").run();
    // Simulate the race: last_gen is read as 3 by the fast path, then moves to 10 before the batch.
    const orig = env.DB.prepare.bind(env.DB);
    let hijacked = false;
    const spy = { ...env, DB: new Proxy(env.DB, { get(t, k) {
      if (k === "batch") return async (s: D1PreparedStatement[]) => {
        if (!hijacked) { hijacked = true; await orig("UPDATE nodes SET last_gen = 10 WHERE name = 'dev-a'").run(); }
        return t.batch(s);
      };
      const v = (t as unknown as Record<string | symbol, unknown>)[k];
      return typeof v === "function" ? (v as (...a: unknown[]) => unknown).bind(t) : v;
    } }) } as Env;
    const { handleSnapshot } = await import("../src/snapshot");
    const req = new Request("http://x/node/v1/snapshot", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify(snap({ gen: 5, projects: [], sessions: [] })),
    });
    expect((await handleSnapshot(req, spy)).status).toBe(409);
    expect(await count("tickets")).toBe(2);
  });

  it("rejects an oversized ticket/session row (400 invalid_field, not 500)", async () => {
    const token = await mint("dev-a");
    const deps = Array.from({ length: 100 }, () => "x".repeat(16000));
    const big = snap({
      projects: [{ project: PROJECT, tickets: [{ id: "E1-01", deps }] }],
    });
    const r = await post(token, big);
    expect(r.status).toBe(400);
    expect(await r.json()).toMatchObject({ error: "invalid_field", field: "projects[0].tickets[0]" });
    expect(await count("tickets")).toBe(0);
  });

  it("catches a JSON-escaped AKIA key", async () => {
    const token = await mint("dev-a");
    const body = snap({ projects: [{ project: PROJECT, tickets: [{ id: "E1-01", summary: "k AKIAABCDEFGHIJKLMNOP" }] }] });
    const raw = JSON.stringify(body).replace("AKIAABCD", "\\u0041KIAABCD");
    expect((await post(token, raw)).status).toBe(400);
  });
});
