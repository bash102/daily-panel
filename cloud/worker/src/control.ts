/** Commands the Pi sends as JSON objects under control/ (see docs/SYNC.md). */

import { canGet, parseControlKey } from "./keys";
import type { Repo } from "./repo";
import { MAX_REVIEW_DELAY_S, iso, isHhmm, isValidTimeZone, parseIso, type BoxConfig } from "./time";

export class ControlError extends Error {}

export interface ControlOutcome {
  applied: boolean;
  result?: string;
}

function obj(v: unknown): Record<string, unknown> {
  if (typeof v !== "object" || v === null || Array.isArray(v)) throw new ControlError("expected a JSON object");
  return v as Record<string, unknown>;
}

export function parseConfig(v: unknown): BoxConfig {
  const o = obj(v);
  const q = obj(o.quiet_hours);
  if (!isHhmm(q.start) || !isHhmm(q.end)) throw new ControlError("quiet_hours.start/end must be HH:MM");
  if (!isValidTimeZone(o.timezone)) throw new ControlError("timezone must be an IANA zone name");
  const d = o.review_delay_s;
  if (typeof d !== "number" || !Number.isInteger(d) || d < 0 || d > MAX_REVIEW_DELAY_S) {
    throw new ControlError(`review_delay_s must be an integer 0..${MAX_REVIEW_DELAY_S}`);
  }
  return { quiet_hours: { start: q.start, end: q.end }, timezone: o.timezone, review_delay_s: d };
}

/** Validate a control object before it is stored. Throws ControlError. */
export function validateControl(key: string, body: unknown): void {
  const ck = parseControlKey(key);
  if (ck.kind === "other") return;
  if (ck.kind === "config") {
    parseConfig(body);
    return;
  }
  const o = obj(body);
  if (o.rec_id !== ck.recId) throw new ControlError("rec_id does not match the key");
  if (ck.kind === "zozo_withhold") {
    if (o.action !== "withhold") throw new ControlError('action must be "withhold"');
    return;
  }
  if (o.action !== "new") throw new ControlError('action must be "new"');
  if (typeof o.media_key !== "string" || !canGet(o.media_key) || !o.media_key.startsWith("recordings/zozo/")) {
    throw new ControlError("media_key must be a key under recordings/zozo/");
  }
  if (!parseIso(o.recorded_at)) throw new ControlError("recorded_at must be ISO 8601 with a zone");
  if (o.duration_s !== undefined && o.duration_s !== null && typeof o.duration_s !== "number") {
    throw new ControlError("duration_s must be a number");
  }
}

/** Apply a validated control object. Idempotent. */
export async function applyControl(repo: Repo, key: string, body: unknown, now: Date): Promise<ControlOutcome> {
  validateControl(key, body);
  const ck = parseControlKey(key);
  const nowIso = iso(now);
  switch (ck.kind) {
    case "config":
      await repo.setConfig(parseConfig(body), nowIso);
      return { applied: true, result: "config" };
    case "zozo_withhold":
      await repo.withholdZozo(ck.recId, nowIso);
      return { applied: true, result: "withheld" };
    case "zozo_new": {
      const o = body as Record<string, unknown>;
      const res = await repo.registerZozo(
        {
          rec_id: ck.recId,
          media_key: o.media_key as string,
          recorded_at: iso(parseIso(o.recorded_at)!),
          duration_s: typeof o.duration_s === "number" ? o.duration_s : null,
        },
        nowIso,
      );
      return { applied: true, result: res };
    }
    default:
      return { applied: false };
  }
}
