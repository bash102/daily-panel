import { beforeEach, describe, expect, it } from "vitest";
import { grandmaSmsBody, runCron } from "../src/cron";
import { makeWorld, TEST_SETTINGS, type World } from "./fakes";

let w: World;
const MEDIA = "recordings/zozo/2026/11/25/a.wav";

async function newMessage(recId: string, recordedAt: string, media = MEDIA, withMedia = true) {
  await w.deps.repo.registerZozo({ rec_id: recId, media_key: media, recorded_at: recordedAt, duration_s: 5 }, recordedAt);
  if (withMedia) await w.r2.put(media, new Uint8Array([1]), { httpMetadata: { contentType: "audio/wav" } });
}

beforeEach(() => {
  w = makeWorld("2026-11-25T15:00:00.000Z"); // 10:00 New York
});

describe("publishing", () => {
  it("publishes after the review delay and texts grandma exactly once", async () => {
    await newMessage("m1", "2026-11-25T15:00:00.000Z");
    w.clock.set("2026-11-25T15:55:00.000Z");
    expect((await runCron(w.deps)).published).toEqual([]);
    expect((await w.deps.repo.getZozo("m1"))?.publish_at).toBe("2026-11-25T16:00:00.000Z");

    w.clock.set("2026-11-25T16:00:00.000Z");
    const r = await runCron(w.deps);
    expect(r.published).toEqual(["m1"]);
    expect(r.texted).toEqual(["m1"]);
    expect(w.twilio.sms).toEqual([{ to: TEST_SETTINGS.grandmaNumber, body: grandmaSmsBody(TEST_SETTINGS.fromNumber) }]);
    expect(w.twilio.sms[0]!.body).toBe("New message from Zozo! Call +15550001111 to listen.");

    w.clock.advance(5 * 60_000);
    await runCron(w.deps);
    expect(w.twilio.sms).toHaveLength(1);
    expect((await w.deps.repo.latestPublishedZozo())?.rec_id).toBe("m1");
  });

  it("pauses the review window over quiet hours across midnight", async () => {
    // 18:30 New York; quiet 19:00-07:00 by default
    w.clock.set("2026-11-25T23:30:00.000Z");
    await newMessage("m1", "2026-11-25T23:30:00.000Z");
    for (const t of ["2026-11-26T00:30:00.000Z", "2026-11-26T05:00:00.000Z", "2026-11-26T12:25:00.000Z"]) {
      w.clock.set(t);
      expect((await runCron(w.deps)).published, t).toEqual([]);
    }
    w.clock.set("2026-11-26T12:30:00.000Z"); // 07:30 NY
    expect((await runCron(w.deps)).published).toEqual(["m1"]);
    expect(w.twilio.sms).toHaveLength(1);
  });

  it("follows the configured timezone and delay", async () => {
    await w.deps.repo.setConfig(
      { quiet_hours: { start: "19:00", end: "07:00" }, timezone: "Europe/Berlin", review_delay_s: 600 },
      "2026-11-25T00:00:00.000Z",
    );
    // 15:00Z = 16:00 Berlin; +10 min
    await newMessage("m1", "2026-11-25T15:00:00.000Z");
    w.clock.set("2026-11-25T15:10:00.000Z");
    expect((await runCron(w.deps)).published).toEqual(["m1"]);
    // 17:55Z = 18:55 Berlin -> 5 min before quiet, 5 min after 07:00 -> 06:05Z
    await newMessage("m2", "2026-11-25T17:55:00.000Z", "recordings/zozo/2026/11/25/b.wav");
    w.clock.set("2026-11-26T06:04:00.000Z");
    expect((await runCron(w.deps)).published).toEqual([]);
    w.clock.set("2026-11-26T06:05:00.000Z");
    expect((await runCron(w.deps)).published).toEqual(["m2"]);
  });

  it("never publishes a withheld message", async () => {
    await newMessage("m1", "2026-11-25T15:00:00.000Z");
    await w.deps.repo.withholdZozo("m1", "2026-11-25T15:30:00.000Z");
    w.clock.set("2026-11-25T17:00:00.000Z");
    const r = await runCron(w.deps);
    expect(r.published).toEqual([]);
    expect(w.twilio.sms).toHaveLength(0);
    expect(await w.deps.repo.latestPublishedZozo()).toBeNull();
  });

  it("withholding after publishing unpublishes; grandma hears the previous one; no SMS", async () => {
    await newMessage("m1", "2026-11-25T15:00:00.000Z");
    w.clock.set("2026-11-25T16:00:00.000Z");
    await runCron(w.deps);
    await newMessage("m2", "2026-11-25T16:30:00.000Z", "recordings/zozo/2026/11/25/b.wav");
    w.clock.set("2026-11-25T17:30:00.000Z");
    await runCron(w.deps);
    expect((await w.deps.repo.latestPublishedZozo())?.rec_id).toBe("m2");
    expect(w.twilio.sms).toHaveLength(2);
    await w.deps.repo.withholdZozo("m2", "2026-11-25T17:40:00.000Z");
    expect((await w.deps.repo.latestPublishedZozo())?.rec_id).toBe("m1");
    await runCron(w.deps);
    expect(w.twilio.sms).toHaveLength(2);
  });

  it("waits for the audio upload before publishing", async () => {
    await newMessage("m1", "2026-11-25T15:00:00.000Z", MEDIA, false);
    w.clock.set("2026-11-25T16:10:00.000Z");
    const r = await runCron(w.deps);
    expect(r.published).toEqual([]);
    expect(r.waitingForMedia).toEqual(["m1"]);
    await w.r2.put(MEDIA, new Uint8Array([1]));
    expect((await runCron(w.deps)).published).toEqual(["m1"]);
  });

  it("does not publish during quiet hours even when overdue (e.g. config changed)", async () => {
    await newMessage("m1", "2026-11-25T15:00:00.000Z");
    w.clock.set("2026-11-26T03:00:00.000Z"); // 22:00 NY: quiet, overdue since 11:00
    expect((await runCron(w.deps)).published).toEqual([]);
    w.clock.set("2026-11-26T12:00:00.000Z");
    expect((await runCron(w.deps)).published).toEqual(["m1"]);
  });

  it("only the newest of several pending messages publishes", async () => {
    await newMessage("m1", "2026-11-25T15:00:00.000Z");
    await newMessage("m2", "2026-11-25T15:20:00.000Z", "recordings/zozo/2026/11/25/b.wav");
    w.clock.set("2026-11-25T17:00:00.000Z");
    expect((await runCron(w.deps)).published).toEqual(["m2"]);
    expect(w.twilio.sms).toHaveLength(1);
  });

  it("retries a failed SMS on the next run, up to a limit", async () => {
    await newMessage("m1", "2026-11-25T15:00:00.000Z");
    w.clock.set("2026-11-25T16:00:00.000Z");
    w.twilio.failSms = true;
    const r = await runCron(w.deps);
    expect(r.published).toEqual(["m1"]);
    expect(r.texted).toEqual([]);
    w.twilio.failSms = false;
    expect((await runCron(w.deps)).texted).toEqual(["m1"]);
    expect(w.twilio.sms).toHaveLength(1);

    await newMessage("m2", "2026-11-25T16:30:00.000Z", "recordings/zozo/2026/11/25/b.wav");
    w.clock.set("2026-11-25T18:00:00.000Z");
    w.twilio.failSms = true;
    for (let i = 0; i < 7; i++) await runCron(w.deps);
    expect((await w.deps.repo.getZozo("m2"))?.sms_attempts).toBe(5);
  });

  it("skips texting when GRANDMA_NUMBER is unset", async () => {
    w = makeWorld("2026-11-25T15:00:00.000Z", { grandmaNumber: "" });
    await newMessage("m1", "2026-11-25T15:00:00.000Z");
    w.clock.set("2026-11-25T16:00:00.000Z");
    expect((await runCron(w.deps)).published).toEqual(["m1"]);
    expect(w.twilio.sms).toHaveLength(0);
  });
});
