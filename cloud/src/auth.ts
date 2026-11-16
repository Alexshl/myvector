export type AuthResult = { ok: true; node: string } | { ok: false; status: 401 };

const BEARER = /^Bearer ([A-Za-z0-9_-]{43})$/;

export async function hashToken(token: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(token));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function hexBytes(hex: string): Uint8Array {
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  return out;
}

/** Constant-time comparison of two hex SHA-256 digests (lengths are fixed, 64). */
function sameHash(a: string, b: string): boolean {
  if (!/^[0-9a-f]{64}$/.test(a) || !/^[0-9a-f]{64}$/.test(b)) return false;
  const x = hexBytes(a);
  const y = hexBytes(b);
  const ts = (crypto.subtle as unknown as { timingSafeEqual?: (a: ArrayBufferView, b: ArrayBufferView) => boolean })
    .timingSafeEqual;
  if (ts) return ts.call(crypto.subtle, x, y);
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x[i]! ^ y[i]!;
  return diff === 0;
}

/**
 * Node auth: only `Authorization: Bearer <43 base64url>`; the token is never read from the URL.
 * D1 holds only SHA-256; after the lookup the stored hash is compared in constant time.
 * The token and its hash are never logged or returned.
 */
export async function authNode(req: Request, env: Env): Promise<AuthResult> {
  const m = BEARER.exec(req.headers.get("Authorization") ?? "");
  if (!m) return { ok: false, status: 401 };
  const hash = await hashToken(m[1]!);
  const row = await env.DB.prepare("SELECT token_hash, node FROM node_tokens WHERE token_hash = ?")
    .bind(hash)
    .first<{ token_hash: string; node: string }>();
  if (!row || !sameHash(row.token_hash, hash)) return { ok: false, status: 401 };
  return { ok: true, node: row.node };
}
