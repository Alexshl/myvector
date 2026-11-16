export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}

export const err = (code: string, status: number, field?: string): Response =>
  json(field ? { error: code, field } : { error: code }, status);
