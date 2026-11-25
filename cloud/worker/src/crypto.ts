/** WebCrypto helpers (available in Workers and Node 20+). */

const enc = new TextEncoder();

export function bytesToBase64(bytes: Uint8Array): string {
  let s = "";
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]!);
  return btoa(s);
}

export function base64UrlEncode(bytes: Uint8Array): string {
  return bytesToBase64(bytes).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function base64UrlDecode(s: string): Uint8Array | null {
  if (!/^[A-Za-z0-9_-]*$/.test(s)) return null;
  const b64 = s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4);
  try {
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  } catch {
    return null;
  }
}

async function hmac(hash: "SHA-1" | "SHA-256", key: string, msg: string | Uint8Array): Promise<Uint8Array> {
  const k = await crypto.subtle.importKey("raw", enc.encode(key), { name: "HMAC", hash }, false, ["sign"]);
  const data = new Uint8Array(typeof msg === "string" ? enc.encode(msg) : msg);
  return new Uint8Array(await crypto.subtle.sign("HMAC", k, data));
}

export async function hmacSha1Base64(key: string, msg: string): Promise<string> {
  return bytesToBase64(await hmac("SHA-1", key, msg));
}

export async function hmacSha256(key: string, msg: string | Uint8Array): Promise<Uint8Array> {
  return hmac("SHA-256", key, msg);
}

/**
 * Constant-time string comparison: both sides are hashed first, so neither
 * the content nor the length of the secret leaks through timing.
 */
export async function timingSafeEqual(a: string, b: string): Promise<boolean> {
  const [da, db] = await Promise.all([
    crypto.subtle.digest("SHA-256", enc.encode(a)),
    crypto.subtle.digest("SHA-256", enc.encode(b)),
  ]);
  const x = new Uint8Array(da);
  const y = new Uint8Array(db);
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x[i]! ^ y[i]!;
  return diff === 0 && a.length === b.length;
}
