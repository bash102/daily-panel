// Ambient Cloudflare names used by src/env.ts and src/index.ts, for type-checking
// the tests under Node types (@cloudflare/workers-types conflicts with @types/node).
type R2Bucket = unknown;
type D1Database = unknown;
type ScheduledController = unknown;
interface ExecutionContext {
  waitUntil(p: Promise<unknown>): void;
}
interface ExportedHandler<E> {
  fetch?(req: Request, env: E): Promise<Response>;
  scheduled?(c: ScheduledController, env: E, ctx: ExecutionContext): Promise<void>;
}
