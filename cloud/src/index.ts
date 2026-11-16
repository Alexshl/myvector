import { err } from "./json";
import { handleRead } from "./read";
import { handleSnapshot } from "./snapshot";

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const path = new URL(req.url).pathname;
    try {
      if (path === "/node/v1/snapshot") return await handleSnapshot(req, env);
      if (path === "/node/v1/ws") return err("not_implemented", 501);
      if (path.startsWith("/api/v1/")) return await handleRead(req, env);
      return err("not_found", 404);
    } catch {
      return err("internal", 500);
    }
  },
} satisfies ExportedHandler<Env>;
