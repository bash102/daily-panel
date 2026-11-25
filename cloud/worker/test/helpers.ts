import { computeTwilioSignature } from "../src/twilioSignature";
import { TEST_SETTINGS } from "./fakes";

export const BASE = TEST_SETTINGS.publicBaseUrl;

/** A Twilio webhook POST signed the way Twilio signs it. */
export async function twilioPost(
  path: string,
  params: Record<string, string>,
  opts: { token?: string; badSig?: boolean; url?: string } = {},
): Promise<Request> {
  const url = opts.url ?? BASE + path;
  const sp = new URLSearchParams(params);
  let sig = await computeTwilioSignature(opts.token ?? TEST_SETTINGS.twilioAuthToken, url, sp);
  if (opts.badSig) sig = sig.slice(0, -2) + "xx";
  return new Request(url, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded", "x-twilio-signature": sig },
    body: sp.toString(),
  });
}

export function apiRequest(
  method: string,
  path: string,
  body?: BodyInit,
  opts: { token?: string | null; contentType?: string } = {},
): Request {
  const headers: Record<string, string> = {};
  const token = opts.token === undefined ? TEST_SETTINGS.boxToken : opts.token;
  if (token !== null) headers.authorization = `Bearer ${token}`;
  if (opts.contentType) headers["content-type"] = opts.contentType;
  return new Request(BASE + path, { method, headers, body });
}

export const CALL = { CallSid: "CA" + "1".repeat(32), From: "+15550002222", To: "+15550001111" };
export const SID = "RE" + "a".repeat(32);
