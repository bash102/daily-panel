/**
 * X-Twilio-Signature validation (https://www.twilio.com/docs/usage/security):
 * base64(HMAC-SHA1(auth_token, full_url + concat(sorted(key + value)))).
 */

import { hmacSha1Base64, timingSafeEqual } from "./crypto";

export function signaturePayload(url: string, params: Iterable<[string, string]>): string {
  const pairs = [...params].sort((a, b) =>
    a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0,
  );
  let s = url;
  for (const [k, v] of pairs) s += k + v;
  return s;
}

export async function computeTwilioSignature(
  authToken: string,
  url: string,
  params: Iterable<[string, string]>,
): Promise<string> {
  return hmacSha1Base64(authToken, signaturePayload(url, params));
}

/** URLs Twilio may have signed: the URL as received, and the same path and
 * query under PUBLIC_BASE_URL (the URL configured in Twilio, in case a proxy
 * or custom domain changed what the worker sees). */
export function candidateUrls(requestUrl: string, publicBaseUrl?: string): string[] {
  const out = [requestUrl];
  if (publicBaseUrl) {
    const u = new URL(requestUrl);
    const alt = publicBaseUrl.replace(/\/+$/, "") + u.pathname + u.search;
    if (alt !== requestUrl) out.push(alt);
  }
  return out;
}

export interface ValidatedTwilioRequest {
  ok: boolean;
  params: URLSearchParams;
}

/** Reads the form body once and checks the signature against it. */
export async function validateTwilioRequest(
  req: Request,
  authToken: string,
  publicBaseUrl?: string,
): Promise<ValidatedTwilioRequest> {
  const ctype = req.headers.get("content-type") ?? "";
  const body = req.method === "POST" ? await req.text() : "";
  const params =
    req.method === "POST" && ctype.includes("application/x-www-form-urlencoded")
      ? new URLSearchParams(body)
      : new URLSearchParams();
  const sig = req.headers.get("x-twilio-signature");
  if (!sig || !authToken) return { ok: false, params };
  for (const url of candidateUrls(req.url, publicBaseUrl)) {
    const expected = await computeTwilioSignature(authToken, url, params);
    if (await timingSafeEqual(expected, sig)) return { ok: true, params };
  }
  return { ok: false, params };
}
