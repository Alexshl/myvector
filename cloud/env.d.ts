declare namespace Cloudflare {
  interface Env {
    DB: D1Database;
  }
}

interface Env extends Cloudflare.Env {
  /** Local dev only: "1" lets the browser read API through from localhost. Never set in wrangler.toml. */
  ACCESS_DEV_BYPASS?: string;
}
