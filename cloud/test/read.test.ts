import { hashToken } from "../src/auth";
import { beforeEach, describe, expect, it } from "vitest";
import { call, mint, post, read, resetDb, snap } from "./helpers";

beforeEach(resetDb);

describe("read API guard", () => {
  it("is fail-closed without the bypass (403)", async () => {
    for (const p of ["/api/v1/nodes", "/api/v1/projects", "/api/v1/tickets?project=x", "/api/v1/sessions"]) {
      expect((await call(p)).status, p).toBe(403);
      expect((await call(p, {}, "0")).status, p).toBe(403);
    }
  });

  it("is closed for a non-localhost host even with the bypass", async () => {
    expect((await read("/api/v1/nodes", "manager.example")).status).toBe(403);
    expect((await read("/api/v1/nodes", "127.0.0.1")).status).toBe(200);
    expect((await read("/api/v1/nodes", "localhost")).status).toBe(200);
  });

  it("never exposes token_hash", async () => {
    const t = await mint("dev-a");
    await post(t, snap());
    for (const p of ["/api/v1/nodes", "/api/v1/projects", "/api/v1/sessions", "/api/v1/tickets?project=git:github.com/acme/repo"]) {
      const body = await (await read(p)).text();
      expect(body, p).not.toContain("token_hash");
      expect(body, p).not.toContain(await hashToken(t));
    }
  });

  it("validates routes and does not close /node/* with the browser guard", async () => {
    expect((await read("/api/v1/tickets")).status).toBe(400);
    expect((await read("/api/v1/nope")).status).toBe(404);
    expect((await read("/elsewhere")).status).toBe(404);
    expect((await call("/node/v1/ws")).status).toBe(501);
    expect((await call("/node/v1/snapshot", { method: "POST" })).status).toBe(415);
  });
});
