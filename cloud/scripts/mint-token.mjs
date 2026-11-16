#!/usr/bin/env node
// Local token mint (E4-02). The token goes only to a 0600 file, never to stdout; D1 gets only the SHA-256.
import { execFileSync } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import { existsSync, unlinkSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { parseArgs } from "node:util";

const NAME = /^[a-z0-9][a-z0-9-]{0,62}$/;
const MAX_ACTIVE = 2;

function fail(msg, code = 2) {
  console.error(`mint-token: ${msg}`);
  process.exit(code);
}

let args;
try {
  args = parseArgs({
    options: {
      node: { type: "string" },
      out: { type: "string" },
      label: { type: "string" },
      "revoke-oldest": { type: "boolean", default: false },
      remote: { type: "boolean", default: false },
    },
    strict: true,
  }).values;
} catch (e) {
  fail(e.message);
}

if (args.remote) fail("--remote is not supported (E4-05)");
if (!args.node || !NAME.test(args.node)) fail("--node must match ^[a-z0-9][a-z0-9-]{0,62}$");
if (!args.out) fail("--out PATH is required (the token is never printed)");
const label = args.label ?? "";
if (!/^[A-Za-z0-9 ._-]{0,64}$/.test(label)) fail("--label: only letters, digits, space, . _ - (max 64)");
const out = resolve(args.out);
if (existsSync(out)) fail("--out already exists, refusing to overwrite");

function d1(sql, json = false) {
  const a = ["--no-install", "wrangler", "d1", "execute", "DB", "--local", "--command", sql];
  if (json) a.push("--json");
  return execFileSync("npx", a, {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
    env: { ...process.env, WRANGLER_SEND_METRICS: "false" },
  });
}

function activeTokens() {
  const res = JSON.parse(
    d1(`SELECT token_hash, created_at FROM node_tokens WHERE node = '${args.node}' ORDER BY created_at, rowid`, true),
  );
  return res[0]?.results ?? [];
}

try {
  const active = activeTokens();
  let victims = [];
  if (active.length >= MAX_ACTIVE) {
    if (!args["revoke-oldest"]) fail(`node already has ${active.length} active tokens (max ${MAX_ACTIVE}); use --revoke-oldest`, 3);
    victims = active.slice(0, active.length - MAX_ACTIVE + 1);
  }

  const token = randomBytes(32).toString("base64url");
  const hash = createHash("sha256").update(token, "utf8").digest("hex");
  writeFileSync(out, token + "\n", { mode: 0o600, flag: "wx" });
  try {
    const now = Date.now();
    d1(
      `INSERT OR IGNORE INTO nodes (name, created_at) VALUES ('${args.node}', ${now}); ` +
        `INSERT INTO node_tokens (token_hash, node, created_at, label) VALUES ('${hash}', '${args.node}', ${now}, '${label}')`,
    );
  } catch (e) {
    unlinkSync(out);
    throw e;
  }
  // Revoke only after the new token is stored, so a failed INSERT never leaves the node with fewer tokens.
  for (const v of victims) d1(`DELETE FROM node_tokens WHERE token_hash = '${v.token_hash}'`);
  console.log(`node: ${args.node}\ntoken file: ${out}\nhash: ${hash.slice(0, 8)}...`);
} catch (e) {
  fail(`failed (${e?.status ?? "error"}); is the local D1 migrated? (npm run db:migrate:local)`, 1);
}
