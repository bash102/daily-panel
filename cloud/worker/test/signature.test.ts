import { createHmac } from "node:crypto";
import { describe, expect, it } from "vitest";
import { timingSafeEqual } from "../src/crypto";
import { computeTwilioSignature, signaturePayload, validateTwilioRequest } from "../src/twilioSignature";

const DOC_PARAMS: [string, string][] = [
  ["CallSid", "CA1234567890ABCDE"],
  ["Caller", "+12349013030"],
  ["Digits", "1234"],
  ["From", "+12349013030"],
  ["To", "+18005551212"],
];

function req(url: string, body: URLSearchParams, sig: string | null): Request {
  const headers: Record<string, string> = { "content-type": "application/x-www-form-urlencoded" };
  if (sig !== null) headers["x-twilio-signature"] = sig;
  return new Request(url, { method: "POST", headers, body: body.toString() });
}

describe("Twilio signature", () => {
  it("matches Twilio's documented example", async () => {
    const sig = await computeTwilioSignature("12345", "https://mycompany.com/myapp.php?foo=1&bar=2", DOC_PARAMS);
    expect(sig).toBe("0/KCTR6DLpKmkAf8muzZqo1nDgQ=");
  });

  it("sorts params by key and matches an independent HMAC", async () => {
    const params: [string, string][] = [["b", "2"], ["a", "1"], ["a", "0"]];
    const payload = signaturePayload("https://x.test/p", params);
    expect(payload).toBe("https://x.test/pa0a1b2");
    const oracle = createHmac("sha1", "tok").update(payload).digest("base64");
    expect(await computeTwilioSignature("tok", "https://x.test/p", params)).toBe(oracle);
  });

  it("accepts a valid request and returns its params", async () => {
    const url = "https://box.example.test/twilio/voice";
    const body = new URLSearchParams(DOC_PARAMS);
    const sig = await computeTwilioSignature("tok", url, body);
    const v = await validateTwilioRequest(req(url, body, sig), "tok");
    expect(v.ok).toBe(true);
    expect(v.params.get("Digits")).toBe("1234");
  });

  it("rejects a missing, wrong or tampered signature", async () => {
    const url = "https://box.example.test/twilio/voice";
    const body = new URLSearchParams(DOC_PARAMS);
    const sig = await computeTwilioSignature("tok", url, body);
    expect((await validateTwilioRequest(req(url, body, null), "tok")).ok).toBe(false);
    expect((await validateTwilioRequest(req(url, body, sig), "other-token")).ok).toBe(false);
    const tampered = new URLSearchParams(DOC_PARAMS);
    tampered.set("Digits", "0000");
    expect((await validateTwilioRequest(req(url, tampered, sig), "tok")).ok).toBe(false);
    expect((await validateTwilioRequest(req(url + "?x=1", body, sig), "tok")).ok).toBe(false);
    expect((await validateTwilioRequest(req(url, body, sig), "")).ok).toBe(false);
  });

  it("also accepts the URL as signed under PUBLIC_BASE_URL", async () => {
    const body = new URLSearchParams(DOC_PARAMS);
    const sig = await computeTwilioSignature("tok", "https://public.example/twilio/pin?again=1", body);
    const r = req("https://internal.workers.dev/twilio/pin?again=1", body, sig);
    expect((await validateTwilioRequest(r, "tok", "https://public.example/")).ok).toBe(true);
  });
});

describe("timingSafeEqual", () => {
  it("compares exactly", async () => {
    expect(await timingSafeEqual("abc", "abc")).toBe(true);
    expect(await timingSafeEqual("abc", "abd")).toBe(false);
    expect(await timingSafeEqual("abc", "abcd")).toBe(false);
    expect(await timingSafeEqual("", "")).toBe(true);
  });
});
