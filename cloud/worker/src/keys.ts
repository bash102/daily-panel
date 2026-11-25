/** Archive key rules for the Pi object API. */

/** Prefixes the Pi may write (PUT /api/objects/<key>). */
export const PUT_PREFIXES: readonly string[] = [
  "recordings/zozo/",
  "prints/",
  "mood/",
  "content/",
  "phrases/",
  "events/",
  "logs/",
  "control/",
];

/** Prefixes the Pi may read (GET /api/objects/<key>): its own plus grandma's messages. */
export const GET_PREFIXES: readonly string[] = [...PUT_PREFIXES, "recordings/grandma/"];

export const MAX_KEY_LENGTH = 512;

const SAFE_CHARS = /^[A-Za-z0-9._~+=@,\-\/]+$/;

/** A relative, normalized key: no "..", no ".", no empty segments, no
 * absolute path, no backslashes or control characters, safe characters only. */
export function isSafeKey(key: string): boolean {
  if (!key || key.length > MAX_KEY_LENGTH) return false;
  if (key.startsWith("/") || key.endsWith("/")) return false;
  if (!SAFE_CHARS.test(key)) return false;
  for (const seg of key.split("/")) {
    if (seg === "" || seg === "." || seg === "..") return false;
  }
  return true;
}

export function hasPrefix(key: string, prefixes: readonly string[]): boolean {
  return prefixes.some((p) => key.startsWith(p));
}

export function canPut(key: string): boolean {
  return isSafeKey(key) && hasPrefix(key, PUT_PREFIXES);
}

export function canGet(key: string): boolean {
  return isSafeKey(key) && hasPrefix(key, GET_PREFIXES);
}

export const REC_ID_RE = /^[A-Za-z0-9_]{1,100}$/;

export type ControlKey =
  | { kind: "config" }
  | { kind: "zozo_new"; recId: string }
  | { kind: "zozo_withhold"; recId: string }
  | { kind: "other" };

export function parseControlKey(key: string): ControlKey {
  if (key === "control/config.json") return { kind: "config" };
  const m = /^control\/zozo\/([A-Za-z0-9_]{1,100})(-withhold)?\.json$/.exec(key);
  if (m) return m[2] ? { kind: "zozo_withhold", recId: m[1]! } : { kind: "zozo_new", recId: m[1]! };
  return { kind: "other" };
}
