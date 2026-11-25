/**
 * Short-lived signed media URLs for Twilio <Play>: /media/<payload>.<sig>,
 * payload = base64url(JSON {k: r2 key, e: expiry ms}), sig = base64url(HMAC-SHA256).
 */

import { base64UrlDecode, base64UrlEncode, hmacSha256, timingSafeEqual } from "./crypto";

export const MEDIA_TOKEN_TTL_MS = 10 * 60 * 1000;

export async function signMediaToken(key: string, expiresAtMs: number, secret: string): Promise<string> {
  if (!secret) throw new Error("MEDIA_SIGNING_KEY is not set");
  const payload = base64UrlEncode(new TextEncoder().encode(JSON.stringify({ k: key, e: expiresAtMs })));
  const sig = base64UrlEncode(await hmacSha256(secret, payload));
  return `${payload}.${sig}`;
}

/** Returns the R2 key, or null for a bad, tampered or expired token. */
export async function verifyMediaToken(token: string, secret: string, nowMs: number): Promise<string | null> {
  if (!secret) return null;
  const parts = token.split(".");
  if (parts.length !== 2) return null;
  const [payload, sig] = parts as [string, string];
  const expected = base64UrlEncode(await hmacSha256(secret, payload));
  if (!(await timingSafeEqual(expected, sig))) return null;
  const raw = base64UrlDecode(payload);
  if (!raw) return null;
  try {
    const obj = JSON.parse(new TextDecoder().decode(raw)) as { k?: unknown; e?: unknown };
    if (typeof obj.k !== "string" || typeof obj.e !== "number") return null;
    if (nowMs > obj.e) return null;
    return obj.k;
  } catch {
    return null;
  }
}
