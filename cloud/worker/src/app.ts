/** Request router. Never throws: unexpected errors become a logged 500. */

import { handleApi } from "./api";
import type { Deps } from "./deps";
import {
  handleMenu,
  handlePin,
  handleRecorded,
  handleRecordingStatus,
  handleVoice,
} from "./ivr";
import { verifyMediaToken } from "./mediaToken";
import { validateTwilioRequest } from "./twilioSignature";

const TWILIO_ROUTES: Record<string, (d: Deps, p: URLSearchParams, url: URL) => Promise<Response>> = {
  "/twilio/voice": (d, p) => handleVoice(d, p),
  "/twilio/pin": (d, p) => handlePin(d, p),
  "/twilio/menu": (d, p, u) => handleMenu(d, p, u),
  "/twilio/recorded": (d, p) => handleRecorded(d, p),
  "/twilio/recording-status": (d, p) => handleRecordingStatus(d, p),
};

export async function handleRequest(d: Deps, req: Request): Promise<Response> {
  try {
    const url = new URL(req.url);
    const path = url.pathname;

    if (path.startsWith("/twilio/")) {
      const route = TWILIO_ROUTES[path];
      if (!route) return new Response("not found", { status: 404 });
      if (req.method !== "POST") return new Response("method not allowed", { status: 405 });
      const v = await validateTwilioRequest(req, d.settings.twilioAuthToken, d.settings.publicBaseUrl);
      if (!v.ok) {
        d.log.warn(`twilio: rejected request with a bad signature on ${path}`);
        return new Response("forbidden", { status: 403 });
      }
      return await route(d, v.params, url);
    }

    if (path.startsWith("/media/")) {
      if (req.method !== "GET" && req.method !== "HEAD") return new Response("method not allowed", { status: 405 });
      const key = await verifyMediaToken(path.slice("/media/".length), d.settings.mediaSigningKey, d.now().getTime());
      if (!key || !key.startsWith("recordings/")) return new Response("forbidden", { status: 403 });
      const obj = await d.archive.get(key);
      if (!obj) return new Response("not found", { status: 404 });
      return new Response(req.method === "HEAD" ? null : obj.body, {
        headers: {
          "content-type": obj.httpMetadata?.contentType ?? "audio/wav",
          "content-length": String(obj.size),
          "cache-control": "private, no-store",
        },
      });
    }

    if (path.startsWith("/api/")) return await handleApi(d, req, url);

    return new Response("not found", { status: 404 });
  } catch (e) {
    d.log.error(`worker: unhandled error: ${String(e)}`);
    return new Response("internal error", { status: 500 });
  }
}
