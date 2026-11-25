import { beforeEach, describe, expect, it } from "vitest";
import { handleRequest } from "../src/app";
import { verifyMediaToken } from "../src/mediaToken";
import { makeWorld, TEST_SETTINGS, type World } from "./fakes";
import { CALL, SID, twilioPost } from "./helpers";

let w: World;
const send = async (path: string, params: Record<string, string>, opts = {}) =>
  handleRequest(w.deps, await twilioPost(path, params, opts));
const text = async (path: string, params: Record<string, string>) => (await send(path, params)).text();

async function login(from = CALL.From, callSid = CALL.CallSid): Promise<string> {
  return text("/twilio/pin", { ...CALL, From: from, CallSid: callSid, Digits: TEST_SETTINGS.grandmaPin });
}

beforeEach(() => {
  w = makeWorld();
});

describe("signature enforcement", () => {
  it("rejects unsigned or badly signed Twilio requests", async () => {
    const r = await send("/twilio/voice", CALL, { badSig: true });
    expect(r.status).toBe(403);
    const unsigned = new Request("https://box.example.test/twilio/voice", {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams(CALL).toString(),
    });
    expect((await handleRequest(w.deps, unsigned)).status).toBe(403);
  });
  it("rejects a request signed with another account's token", async () => {
    expect((await send("/twilio/pin", { ...CALL, Digits: "4321" }, { token: "nope" })).status).toBe(403);
    expect(await w.deps.repo.isCallAuthed(CALL.CallSid, "")).toBe(false);
  });
  it("404s unknown twilio paths and refuses GET", async () => {
    expect((await send("/twilio/nope", CALL)).status).toBe(404);
    const g = await handleRequest(w.deps, new Request("https://box.example.test/twilio/voice"));
    expect(g.status).toBe(405);
  });
});

describe("call flow", () => {
  it("greets with a 4-digit PIN gather", async () => {
    const r = await send("/twilio/voice", CALL);
    expect(r.headers.get("content-type")).toContain("text/xml");
    const xml = await r.text();
    expect(xml).toContain('<Gather input="dtmf" numDigits="4"');
    expect(xml).toContain('action="/twilio/pin"');
    expect(xml).toContain('voice="Polly.Joanna"');
    expect(xml).toContain("four digit code");
  });

  it("right PIN opens the menu", async () => {
    const xml = await login();
    expect(xml).toContain('numDigits="1"');
    expect(xml).toContain('action="/twilio/menu"');
    expect(xml).toContain("press 1");
    expect(await w.deps.repo.isCallAuthed(CALL.CallSid, "")).toBe(true);
  });

  it("wrong PIN asks again", async () => {
    const xml = await text("/twilio/pin", { ...CALL, Digits: "0000" });
    expect(xml).toContain("didn't work. Please try again");
    expect(xml.indexOf('numDigits="4"')).toBeLessThan(xml.indexOf("<Hangup/>")); // hang up only if no input
  });

  it("an empty PIN entry re-prompts without counting", async () => {
    await text("/twilio/pin", { ...CALL, Digits: "" });
    expect(await w.deps.repo.getCallerLock(CALL.From)).toBeNull();
  });

  it("menu needs an authenticated call", async () => {
    const xml = await text("/twilio/menu", { ...CALL, Digits: "1" });
    expect(xml).toBe('<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>');
  });

  it("press 1 with no published message says so", async () => {
    await login();
    const xml = await text("/twilio/menu", { ...CALL, Digits: "1" });
    expect(xml).toContain("no message from Zozo yet");
    expect(xml).not.toContain("<Play>");
  });

  it("press 1 plays the latest published message through a signed short-lived URL", async () => {
    const now = "2026-11-25T14:00:00.000Z";
    await w.deps.repo.registerZozo({ rec_id: "old", media_key: "recordings/zozo/2026/11/24/a.wav", recorded_at: "2026-11-24T14:00:00.000Z", duration_s: 3 }, now);
    await w.deps.repo.markZozoPublished("old", now);
    await w.deps.repo.registerZozo({ rec_id: "new", media_key: "recordings/zozo/2026/11/25/b.wav", recorded_at: "2026-11-25T13:00:00.000Z", duration_s: 3 }, now);
    await w.deps.repo.markZozoPublished("new", now);
    await w.r2.put("recordings/zozo/2026/11/25/b.wav", new Uint8Array([1, 2, 3]), { httpMetadata: { contentType: "audio/wav" } });
    await login();
    const xml = await text("/twilio/menu", { ...CALL, Digits: "1" });
    const m = /<Play>https:\/\/box\.example\.test\/media\/([^<]+)<\/Play>/.exec(xml);
    expect(m).not.toBeNull();
    const token = m![1]!;
    expect(await verifyMediaToken(token, TEST_SETTINGS.mediaSigningKey, w.clock.now().getTime())).toBe(
      "recordings/zozo/2026/11/25/b.wav",
    );
    expect(xml).toContain('<Redirect method="POST">/twilio/menu</Redirect>');

    const media = await handleRequest(w.deps, new Request(`https://box.example.test/media/${token}`));
    expect(media.status).toBe(200);
    expect(media.headers.get("content-type")).toBe("audio/wav");
    expect(new Uint8Array(await media.arrayBuffer())).toEqual(new Uint8Array([1, 2, 3]));

    // Withholding the newest falls back to the previous published one.
    await w.deps.repo.withholdZozo("new", now);
    const xml2 = await text("/twilio/menu", { ...CALL, Digits: "1" });
    const t2 = /\/media\/([^<]+)<\/Play>/.exec(xml2)![1]!;
    expect(await verifyMediaToken(t2, TEST_SETTINGS.mediaSigningKey, w.clock.now().getTime())).toBe(
      "recordings/zozo/2026/11/24/a.wav",
    );

    // Tokens expire and cannot be forged.
    w.clock.advance(11 * 60_000);
    expect((await handleRequest(w.deps, new Request(`https://box.example.test/media/${token}`))).status).toBe(403);
    expect((await handleRequest(w.deps, new Request(`https://box.example.test/media/abc.def`))).status).toBe(403);
  });

  it("press 2 records up to 120 s with a status callback", async () => {
    await login();
    const xml = await text("/twilio/menu", { ...CALL, Digits: "2" });
    expect(xml).toContain('maxLength="120"');
    expect(xml).toContain('finishOnKey="#"');
    expect(xml).toContain('recordingStatusCallback="https://box.example.test/twilio/recording-status"');
    expect(xml).toContain('action="/twilio/recorded"');
  });

  it("an unknown key or silence repeats the menu once, then says goodbye", async () => {
    await login();
    expect(await text("/twilio/menu", { ...CALL, Digits: "7" })).toContain("didn't get that");
    const first = await text("/twilio/menu", { ...CALL });
    expect(first).toContain("/twilio/menu?again=1");
    const again = await handleRequest(
      w.deps,
      await twilioPost("/twilio/menu?again=1", { ...CALL }),
    );
    const xml = await again.text();
    expect(xml).toContain("Goodbye");
    expect(xml).toContain("<Hangup/>");
  });

  it("after recording thanks grandma and hangs up", async () => {
    await login();
    const xml = await text("/twilio/recorded", { ...CALL, RecordingDuration: "12", RecordingSid: SID });
    expect(xml).toContain("Thank you");
    expect(xml).toContain("<Hangup/>");
    const empty = await text("/twilio/recorded", { ...CALL, RecordingDuration: "0", RecordingSid: SID });
    expect(empty).toContain("didn't hear anything");
  });
});

describe("PIN lockout", () => {
  it("3 wrong tries hang up, lock the caller for an hour and text the parent once", async () => {
    expect(await text("/twilio/pin", { ...CALL, Digits: "1111" })).toContain("try again");
    expect(await text("/twilio/pin", { ...CALL, Digits: "2222" })).toContain("try again");
    const third = await text("/twilio/pin", { ...CALL, Digits: "3333" });
    expect(third).toContain("<Hangup/>");
    expect(w.twilio.sms).toHaveLength(1);
    expect(w.twilio.sms[0]!.to).toBe(TEST_SETTINGS.parentNumber);
    expect(w.twilio.sms[0]!.body).toContain("locked out for 1 hour");

    // Locked: even the right PIN is refused, the greeting hangs up, no more texts.
    expect(await login()).toContain("<Hangup/>");
    const voice = await text("/twilio/voice", CALL);
    expect(voice).toContain("<Hangup/>");
    expect(voice).not.toContain("<Gather");
    expect(w.twilio.sms).toHaveLength(1);

    // Other callers are unaffected.
    expect(await login("+15559998888", "CA" + "2".repeat(32))).toContain("press 1");

    // After an hour the caller may try again.
    w.clock.advance(3600_000 + 1000);
    expect(await login()).toContain("press 1");
  });

  it("a right PIN resets the count", async () => {
    await text("/twilio/pin", { ...CALL, Digits: "1111" });
    await text("/twilio/pin", { ...CALL, Digits: "2222" });
    await login();
    expect(await text("/twilio/pin", { ...CALL, Digits: "1111" })).toContain("try again");
    expect(w.twilio.sms).toHaveLength(0);
  });

  it("wrong tries older than an hour do not count", async () => {
    await text("/twilio/pin", { ...CALL, Digits: "1111" });
    await text("/twilio/pin", { ...CALL, Digits: "2222" });
    w.clock.advance(2 * 3600_000);
    expect(await text("/twilio/pin", { ...CALL, Digits: "3333" })).toContain("try again");
  });

  it("the line-wide limit locks every other caller and texts once", async () => {
    // 10 wrong PINs from 10 different numbers (2 each from 5 numbers would be the same).
    for (let i = 0; i < 9; i++) {
      await text("/twilio/pin", { ...CALL, From: `+1555000${1000 + i}`, Digits: "0000" });
    }
    expect(w.twilio.sms).toHaveLength(0);
    const last = await text("/twilio/pin", { ...CALL, From: "+15550009999", Digits: "0000" });
    expect(last).toContain("<Hangup/>");
    expect(w.twilio.sms).toHaveLength(1);
    expect(w.twilio.sms[0]!.body).toContain("line is locked");
    // Another number is refused while the line is locked ...
    const other = { ...CALL, From: "+15550008888", CallSid: "CA" + "8".repeat(32) };
    expect(await text("/twilio/voice", other)).not.toContain("<Gather");
    expect(await login(other.From, other.CallSid)).toContain("<Hangup/>");
    w.clock.advance(3600_000 + 1000);
    expect(await login(other.From, other.CallSid)).toContain("press 1");
    expect(w.twilio.sms).toHaveLength(1);
  });

  it("strangers cannot lock grandma's own number out of the line", async () => {
    for (let i = 0; i < 10; i++) {
      await text("/twilio/pin", { ...CALL, From: `+1555000${1000 + i}`, Digits: "0000" });
    }
    expect(w.twilio.sms[0]!.body).toContain("line is locked");
    // Grandma (GRANDMA_NUMBER, also without the +1) still gets in.
    expect(await text("/twilio/voice", CALL)).toContain("<Gather");
    expect(await login()).toContain("press 1");
    expect(await login("5550002222", "CA" + "7".repeat(32))).toContain("press 1");
  });

  it("grandma's number keeps its own 3-try lock during a line lock", async () => {
    for (let i = 0; i < 10; i++) {
      await text("/twilio/pin", { ...CALL, From: `+1555000${1000 + i}`, Digits: "0000" });
    }
    expect(await text("/twilio/pin", { ...CALL, Digits: "0000" })).toContain("try again");
    expect(await text("/twilio/pin", { ...CALL, Digits: "1111" })).toContain("try again");
    expect(await text("/twilio/pin", { ...CALL, Digits: "2222" })).toContain("<Hangup/>");
    expect(await login()).toContain("<Hangup/>");
  });

  it("globalLimit 0 turns the line-wide lock off", async () => {
    w = makeWorld(undefined, { pin: { globalLimit: 0 } });
    for (let i = 0; i < 30; i++) {
      await text("/twilio/pin", { ...CALL, From: `+1555000${1000 + i}`, Digits: "0000" });
    }
    expect(w.twilio.sms).toHaveLength(0);
    const other = { ...CALL, From: "+15550008888", CallSid: "CA" + "8".repeat(32) };
    expect(await login(other.From, other.CallSid)).toContain("press 1");
  });

  it("a failing SMS does not break the call", async () => {
    w.twilio.failSms = true;
    for (const d of ["1", "2", "3"]) await text("/twilio/pin", { ...CALL, Digits: d.repeat(4) });
    expect((await w.deps.repo.getCallerLock(CALL.From))?.locked_until).not.toBeNull();
  });

  it("an unset or malformed PIN never matches", async () => {
    w = makeWorld(undefined, { grandmaPin: "" });
    expect(await text("/twilio/pin", { ...CALL, Digits: "" + "" })).toContain("four digit code");
    expect(await text("/twilio/pin", { ...CALL, Digits: "0000" })).toContain("try again");
  });
});

describe("recording ingest", () => {
  const status = (extra: Record<string, string> = {}) => ({
    ...CALL,
    RecordingSid: SID,
    RecordingUrl: `https://api.twilio.com/2010-04-01/Accounts/AC${"0".repeat(32)}/Recordings/${SID}`,
    RecordingStatus: "completed",
    RecordingDuration: "15",
    ...extra,
  });

  it("archives to R2 under recordings/grandma/YYYY/MM/DD, registers it, then deletes Twilio's copy", async () => {
    await login();
    const r = await send("/twilio/recording-status", status());
    expect(r.status).toBe(200);
    // 15:00Z is 10:00 in New York (default timezone); started 15 s earlier.
    const key = `recordings/grandma/2026/11/25/${SID}.wav`;
    expect(w.r2.objects.get(key)?.contentType).toBe("audio/wav");
    expect(w.twilio.deleted).toEqual([SID]);
    const rows = await w.deps.repo.grandmaSince(null, 10);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ id: SID, key, recorded_at: "2026-11-25T14:59:45.000Z", duration_s: 15 });
    expect((await w.deps.repo.getIngest(SID))?.status).toBe("done");

    // A repeated callback is harmless.
    await send("/twilio/recording-status", status());
    expect(await w.deps.repo.grandmaSince(null, 10)).toHaveLength(1);
  });

  it("keeps Twilio's copy when the download fails and the cron retries", async () => {
    await login();
    w.twilio.failFetch = true;
    await send("/twilio/recording-status", status());
    expect(w.twilio.deleted).toEqual([]);
    const row = await w.deps.repo.getIngest(SID);
    expect(row).toMatchObject({ status: "pending", attempts: 1 });
    w.twilio.failFetch = false;
    const { runCron } = await import("../src/cron");
    const rep = await runCron(w.deps);
    expect(rep.ingestsRetried).toBe(1);
    expect(w.twilio.deleted).toEqual([SID]);
    expect(await w.deps.repo.grandmaSince(null, 10)).toHaveLength(1);
  });

  it("only deletes recordings from calls that never entered the PIN", async () => {
    await send("/twilio/recording-status", status());
    expect(w.r2.objects.size).toBe(0);
    expect(w.twilio.deleted).toEqual([SID]);
    expect(await w.deps.repo.grandmaSince(null, 10)).toHaveLength(0);
  });

  it("ignores non-completed statuses and bad sids", async () => {
    await login();
    await send("/twilio/recording-status", status({ RecordingStatus: "in-progress" }));
    await send("/twilio/recording-status", status({ RecordingSid: "RE123" }));
    expect(w.twilio.fetched).toEqual([]);
  });
});
