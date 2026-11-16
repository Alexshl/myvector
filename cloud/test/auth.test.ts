import { beforeEach, describe, expect, it } from "vitest";
import { env } from "cloudflare:workers";
import { hashToken } from "../src/auth";
import { call, genToken, mint, post, resetDb, snap } from "./helpers";

beforeEach(resetDb);

describe("hashToken", () => {
  it("matches the SHA-256 test vector", async () => {
    expect(await hashToken("abc")).toBe("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  });
});

describe("node auth", () => {
  it("rejects missing header, non-bearer, short token, query token and foreign token with 401", async () => {
    const token = await mint("dev-a");
    expect((await post(null, snap())).status).toBe(401);
    expect((await post(null, snap(), { Authorization: `Basic ${token}` })).status).toBe(401);
    expect((await post(null, snap(), { Authorization: "Bearer short" })).status).toBe(401);
    const q = await call(`/node/v1/snapshot?token=${token}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(snap()),
    });
    expect(q.status).toBe(401);
    expect((await post(genToken(), snap())).status).toBe(401);
    const body = await (await post(genToken(), snap())).json();
    expect(body).toEqual({ error: "unauthorized" });
  });

  it("returns 403 when body node differs from the bound node", async () => {
    const token = await mint("dev-a");
    expect((await post(token, snap({ node: "dev-b" }))).status).toBe(403);
    expect((await post(token, snap({ node: "dev-a" }))).status).toBe(200);
  });

  it("stores only a 64-hex hash, the raw token is nowhere in D1", async () => {
    const token = await mint("dev-a");
    await post(token, snap());
    const { results } = await env.DB.prepare("SELECT token_hash FROM node_tokens").all<{ token_hash: string }>();
    expect(results.length).toBe(1);
    expect(results[0]!.token_hash).toMatch(/^[0-9a-f]{64}$/);
    for (const t of ["nodes", "node_tokens", "node_projects", "tickets", "sessions"]) {
      const rows = await env.DB.prepare(`SELECT * FROM ${t}`).all();
      expect(JSON.stringify(rows.results)).not.toContain(token);
    }
  });

  it("method and content type are checked", async () => {
    const token = await mint("dev-a");
    const get = await call("/node/v1/snapshot", { headers: { Authorization: `Bearer ${token}` } });
    expect(get.status).toBe(405);
    const bad = await call("/node/v1/snapshot", {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "text/plain" },
      body: "x",
    });
    expect(bad.status).toBe(415);
  });
});
