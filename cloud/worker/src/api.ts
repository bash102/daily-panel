/**
 * Pi sync API (docs/SYNC.md). Every request needs
 * "Authorization: Bearer <BOX_TOKEN>" (constant-time check).
 *
 *   PUT  /api/objects/<key>            store bytes in R2 (control/ keys are also applied)
 *   GET  /api/objects/<key>            download
 *   GET  /api/grandma/messages?since=  grandma's messages, oldest first
 *   GET  /api/zozo/messages            review/publish state of Zozo's messages
 *   GET  /api/config                   the config the worker is using
 *   GET  /api/health                   auth + liveness check
 */

import { ControlError, applyControl, validateControl } from "./control";
import { timingSafeEqual } from "./crypto";
import type { Deps } from "./deps";
import { canGet, canPut } from "./keys";
import { iso, parseIso } from "./time";

export const MAX_OBJECT_BYTES = 50 * 1024 * 1024;

export function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });
}

function err(status: number, message: string): Response {
  return json({ error: message }, status);
}

export async function authorized(req: Request, boxToken: string): Promise<boolean> {
  const h = req.headers.get("authorization") ?? "";
  const m = /^Bearer (.+)$/.exec(h);
  if (!m || !boxToken) return false;
  return timingSafeEqual(m[1]!, boxToken);
}

function objectKey(pathname: string): string | null {
  const raw = pathname.slice("/api/objects/".length);
  try {
    return decodeURIComponent(raw);
  } catch {
    return null;
  }
}

export async function handleApi(d: Deps, req: Request, url: URL): Promise<Response> {
  if (!(await authorized(req, d.settings.boxToken))) return err(401, "unauthorized");
  const path = url.pathname;

  if (path.startsWith("/api/objects/")) {
    const key = objectKey(path);
    if (req.method === "PUT") {
      if (key === null || !canPut(key)) return err(400, "bad key");
      return putObject(d, req, key);
    }
    if (req.method === "GET" || req.method === "HEAD") {
      if (key === null || !canGet(key)) return err(400, "bad key");
      const obj = await d.archive.get(key);
      if (!obj) return err(404, "not found");
      return new Response(req.method === "HEAD" ? null : obj.body, {
        headers: {
          "content-type": obj.httpMetadata?.contentType ?? "application/octet-stream",
          "content-length": String(obj.size),
          "cache-control": "no-store",
        },
      });
    }
    return err(405, "method not allowed");
  }

  if (req.method !== "GET") return err(405, "method not allowed");

  if (path === "/api/grandma/messages") {
    const s = url.searchParams.get("since");
    let since: string | null = null;
    if (s) {
      const t = parseIso(s);
      if (!t) return err(400, "since must be ISO 8601 with a zone");
      since = iso(t);
    }
    const limit = Math.min(Math.max(Number(url.searchParams.get("limit") ?? "50") || 50, 1), 200);
    const rows = await d.repo.grandmaSince(since, limit);
    return json(rows.map((r) => ({ id: r.id, key: r.key, recorded_at: r.recorded_at, duration_s: r.duration_s })));
  }

  if (path === "/api/zozo/messages") {
    const rows = await d.repo.listZozo(50);
    return json(
      rows.map((r) => ({
        rec_id: r.rec_id,
        status: r.status,
        recorded_at: r.recorded_at,
        publish_at: r.publish_at,
        published_at: r.published_at,
        withheld_at: r.withheld_at,
        sms_sent_at: r.sms_sent_at,
      })),
    );
  }

  if (path === "/api/config") return json(await d.repo.getConfig());
  if (path === "/api/health") return json({ ok: true, now: iso(d.now()) });
  return err(404, "not found");
}

async function putObject(d: Deps, req: Request, key: string): Promise<Response> {
  const declared = Number(req.headers.get("content-length") ?? "0");
  if (declared > MAX_OBJECT_BYTES) return err(413, "too large");
  const data = await req.arrayBuffer();
  if (data.byteLength > MAX_OBJECT_BYTES) return err(413, "too large");
  const contentType = req.headers.get("content-type") ?? "application/octet-stream";

  let control: unknown = undefined;
  if (key.startsWith("control/")) {
    try {
      control = JSON.parse(new TextDecoder().decode(data));
      validateControl(key, control);
    } catch (e) {
      const msg = e instanceof ControlError ? e.message : "body is not valid JSON";
      return err(422, msg);
    }
  }

  await d.archive.put(key, data, { httpMetadata: { contentType } });

  if (control !== undefined) {
    const outcome = await applyControl(d.repo, key, control, d.now());
    return json({ ok: true, key, ...outcome });
  }
  return json({ ok: true, key, bytes: data.byteLength });
}
