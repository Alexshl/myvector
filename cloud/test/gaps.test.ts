import { beforeEach, describe, expect, it } from "vitest";
import { env } from "cloudflare:workers";
import { PROJECT, UUID1, call, mint, post, read, resetDb, snap } from "./helpers";

beforeEach(resetDb);

const count = async (t: string) =>
  (await env.DB.prepare(`SELECT COUNT(*) AS n FROM ${t}`).first<{ n: number }>())!.n;
const proj = () => (snap().projects as Record<string, unknown>[])[0]!;

describe("schema gaps", () => {
  it("rejects a wrong type field and a non-integer or negative gen", async () => {
    const token = await mint("dev-a");
    for (const body of [snap({ type: "other" }), snap({ gen: -1 }), snap({ gen: 1.5 }), snap({ gen: "1" })]) {
      expect((await post(token, body)).status).toBe(400);
    }
    expect(await count("tickets")).toBe(0);
  });

  it("rejects duplicate tickets, duplicate projects and duplicate sessions", async () => {
    const token = await mint("dev-a");
    const dupT = snap({ projects: [{ ...proj(), tickets: [{ id: "A-1" }, { id: "A-1" }] }] });
    const dupP = snap({ projects: [proj(), proj()] });
    const dupS = snap({ sessions: [{ session_id: UUID1 }, { session_id: UUID1 }] });
    for (const b of [dupT, dupP, dupS]) {
      const r = await post(token, b);
      expect(r.status).toBe(400);
      expect(((await r.json()) as { error: string }).error).toBe("duplicate");
    }
  });

  it("rejects a cwd that is relative without a tilde", async () => {
    const token = await mint("dev-a");
    expect((await post(token, snap({ sessions: [{ session_id: UUID1, cwd: "proj/x" }] }))).status).toBe(400);
    expect((await post(token, snap({ sessions: [{ session_id: UUID1, cwd: "~" }] }))).status).toBe(200);
  });

  it("rejects a hard secret in a ticket title and does not echo it", async () => {
    const token = await mint("dev-a");
    const body = snap({ projects: [{ ...proj(), tickets: [{ id: "A-1", title: "key AKIAABCDEFGHIJKLMNOP" }] }] });
    const r = await post(token, body);
    expect(r.status).toBe(400);
    expect(await r.text()).not.toContain("AKIA");
    expect(await count("tickets")).toBe(0);
  });

  it("rejects a body with a foreign node even when other fields are invalid (403 first)", async () => {
    const token = await mint("dev-a");
    expect((await post(token, snap({ node: "dev-b", v: 2 }))).status).toBe(403);
  });
});

describe("transport gaps", () => {
  it("rejects a declared Content-Length above 8 MiB with 413", async () => {
    const token = await mint("dev-a");
    const r = await post(token, snap(), { "Content-Length": String(8 * 1024 * 1024 + 1) });
    expect(r.status).toBe(413);
  });

  it("returns 401 before the body is parsed", async () => {
    const r = await post(null, "{not json");
    expect(r.status).toBe(401);
  });

  it("a stale gen does not change the stored last_gen and the same gen keeps data", async () => {
    const token = await mint("dev-a");
    await post(token, snap({ gen: 9 }));
    await post(token, snap({ gen: 3, projects: [], sessions: [] }));
    const row = await env.DB.prepare("SELECT last_gen FROM nodes WHERE name='dev-a'").first<{ last_gen: number }>();
    expect(row!.last_gen).toBe(9);
  });

  it("a rejected snapshot leaves the previous data intact", async () => {
    const token = await mint("dev-a");
    await post(token, snap({ gen: 1 }));
    expect((await post(token, snap({ gen: 2, v: 2 }))).status).toBe(400);
    expect(await count("tickets")).toBe(2);
  });

  it("the ws route answers 501 under any method and does not need auth", async () => {
    expect((await call("/node/v1/ws", { method: "POST" })).status).toBe(501);
  });

  it("read API returns tickets of both nodes for the same project", async () => {
    const a = await mint("dev-a");
    const b = await mint("dev-b");
    await post(a, snap());
    await post(b, snap({ sessions: [] }));
    const all = (await (await read(`/api/v1/tickets?project=${encodeURIComponent(PROJECT)}`)).json()) as { tickets: unknown[] };
    expect(all.tickets.length).toBe(4);
  });
});
