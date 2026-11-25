import { describe, expect, it } from "vitest";
import { TwilioRest } from "../src/twilioApi";
import type { FetchFn } from "../src/types";

const SID = "AC" + "0".repeat(32);
const REC = "RE" + "b".repeat(32);

function mockFetch(responses: Response[] = []): FetchFn & { calls: { url: string; init?: RequestInit }[] } {
  const calls: { url: string; init?: RequestInit }[] = [];
  const f = (async (input: string | Request, init?: RequestInit) => {
    calls.push({ url: typeof input === "string" ? input : input.url, init });
    return responses.shift() ?? new Response("{}", { status: 200 });
  }) as FetchFn & { calls: typeof calls };
  f.calls = calls;
  return f;
}

const authOf = (init?: RequestInit) => new Headers(init?.headers).get("authorization");

describe("TwilioRest", () => {
  it("sends SMS with basic auth and form fields", async () => {
    const f = mockFetch([new Response("{}", { status: 201 })]);
    await new TwilioRest(SID, "tok", "+15550001111", f).sendSms("+15550002222", "Hi!");
    expect(f.calls[0]!.url).toBe(`https://api.twilio.com/2010-04-01/Accounts/${SID}/Messages.json`);
    expect(f.calls[0]!.init?.method).toBe("POST");
    expect(authOf(f.calls[0]!.init)).toBe("Basic " + btoa(`${SID}:tok`));
    const body = new URLSearchParams(String(f.calls[0]!.init?.body));
    expect(Object.fromEntries(body)).toEqual({ To: "+15550002222", From: "+15550001111", Body: "Hi!" });
  });

  it("throws on SMS errors", async () => {
    const f = mockFetch([new Response("no", { status: 400 })]);
    await expect(new TwilioRest(SID, "tok", "+1", f).sendSms("+2", "x")).rejects.toThrow("HTTP 400");
  });

  it("downloads recordings as WAV from api.twilio.com only", async () => {
    const f = mockFetch([new Response(new Uint8Array([1, 2, 3]), { status: 200, headers: { "content-type": "audio/x-wav" } })]);
    const t = new TwilioRest(SID, "tok", "+1", f);
    const r = await t.fetchRecording(`https://api.twilio.com/2010-04-01/Accounts/${SID}/Recordings/${REC}`);
    expect(f.calls[0]!.url).toBe(`https://api.twilio.com/2010-04-01/Accounts/${SID}/Recordings/${REC}.wav`);
    expect(authOf(f.calls[0]!.init)).toContain("Basic ");
    expect(new Uint8Array(r.data)).toEqual(new Uint8Array([1, 2, 3]));
    await expect(t.fetchRecording("https://evil.example/steal")).rejects.toThrow("api.twilio.com");
    await expect(t.fetchRecording("http://api.twilio.com/x")).rejects.toThrow("api.twilio.com");
    expect(f.calls).toHaveLength(1);
  });

  it("deletes recordings; 404 counts as already deleted", async () => {
    const f = mockFetch([new Response(null, { status: 204 }), new Response(null, { status: 404 }), new Response(null, { status: 500 })]);
    const t = new TwilioRest(SID, "tok", "+1", f);
    await t.deleteRecording(REC);
    expect(f.calls[0]!.init?.method).toBe("DELETE");
    expect(f.calls[0]!.url).toBe(`https://api.twilio.com/2010-04-01/Accounts/${SID}/Recordings/${REC}.json`);
    await t.deleteRecording(REC);
    await expect(t.deleteRecording(REC)).rejects.toThrow("HTTP 500");
    await expect(t.deleteRecording("../x")).rejects.toThrow("bad sid");
  });
});
