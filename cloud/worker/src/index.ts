/** Cloudflare Worker entry: maps bindings to Deps and dispatches. */

import { handleRequest } from "./app";
import { runCron } from "./cron";
import { DEFAULT_VOICE, makeDeps, type Deps } from "./deps";
import type { Env } from "./env";
import type { ObjectStore, SqlDb } from "./types";

export function depsFromEnv(env: Env): Deps {
  const limit = Number(env.GLOBAL_PIN_FAILURE_LIMIT ?? "");
  return makeDeps({
    db: env.DB as unknown as SqlDb,
    archive: env.ARCHIVE as unknown as ObjectStore,
    twilioAccountSid: env.TWILIO_ACCOUNT_SID,
    settings: {
      publicBaseUrl: env.PUBLIC_BASE_URL ?? "",
      voice: env.TWILIO_VOICE || DEFAULT_VOICE,
      grandmaPin: env.GRANDMA_PIN ?? "",
      boxToken: env.BOX_TOKEN ?? "",
      mediaSigningKey: env.MEDIA_SIGNING_KEY ?? "",
      twilioAuthToken: env.TWILIO_AUTH_TOKEN ?? "",
      fromNumber: env.TWILIO_FROM_NUMBER ?? "",
      grandmaNumber: env.GRANDMA_NUMBER ?? "",
      parentNumber: env.PARENT_NUMBER ?? "",
      pin: Number.isInteger(limit) && limit >= 0 && (env.GLOBAL_PIN_FAILURE_LIMIT ?? "").trim() !== "" ? { globalLimit: limit } : {},
    },
  });
}

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    return handleRequest(depsFromEnv(env), req);
  },

  async scheduled(_event: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil(
      runCron(depsFromEnv(env)).then(
        (r) => console.log(`cron: ${JSON.stringify(r)}`),
        (e) => console.error(`cron failed: ${String(e)}`),
      ),
    );
  },
} satisfies ExportedHandler<Env>;
