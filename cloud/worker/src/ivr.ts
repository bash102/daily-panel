/**
 * Grandma's phone line (SPEC 7.2): PIN, then a two-option menu.
 *
 *   POST /twilio/voice             greeting + <Gather> 4-digit PIN
 *   POST /twilio/pin               check PIN -> menu | retry | hang up
 *   POST /twilio/menu              menu prompt; Digits=1 play, Digits=2 record
 *   POST /twilio/recorded          <Record> action: thank you + goodbye
 *   POST /twilio/recording-status  recordingStatusCallback: archive to R2, delete from Twilio
 *
 * Every request has already passed X-Twilio-Signature validation (app.ts).
 */

import type { Deps } from "./deps";
import { MEDIA_TOKEN_TTL_MS, signMediaToken } from "./mediaToken";
import { maskCaller } from "./pin";
import type { IngestRow } from "./repo";
import { iso, localDatePath, parseIso } from "./time";
import { gather, hangup, pause, play, record, redirect, say, twiml, twimlResponse } from "./twiml";

export const MAX_RECORD_S = 120;
export const CALL_AUTH_TTL_MS = 2 * 3600_000;
export const MAX_INGEST_ATTEMPTS = 12;

const RECORDING_SID_RE = /^RE[0-9a-fA-F]{32}$/;

function base(d: Deps): string {
  return d.settings.publicBaseUrl.replace(/\/+$/, "");
}

function pinGather(d: Deps, prompt: string): string {
  const v = d.settings.voice;
  return twiml(
    gather(
      { input: "dtmf", numDigits: 4, timeout: 10, action: "/twilio/pin", method: "POST" },
      say(prompt, v),
    ),
    say("I didn't hear any numbers. Please call again soon. Goodbye!", v),
    hangup(),
  );
}

function goodbyeLocked(d: Deps): Response {
  return twimlResponse(
    twiml(say("Sorry, this line isn't available right now. Please try again later. Goodbye.", d.settings.voice), hangup()),
  );
}

export function menuTwiml(d: Deps, intro?: string, repeat = false): string {
  const v = d.settings.voice;
  const verbs: string[] = [];
  if (intro) verbs.push(say(intro, v));
  verbs.push(
    gather(
      { input: "dtmf", numDigits: 1, timeout: 8, action: "/twilio/menu", method: "POST" },
      say("To hear Zozo's message, press 1. To record a message for Zozo, press 2.", v),
    ),
  );
  if (repeat) verbs.push(say("Thank you for calling. Goodbye!", v), hangup());
  else verbs.push(redirect("/twilio/menu?again=1"));
  return twiml(...verbs);
}

export async function handleVoice(d: Deps, p: URLSearchParams): Promise<Response> {
  const caller = p.get("From") ?? "";
  if ((await d.pin.status(caller)) !== "ok") {
    d.log.info(`ivr: locked-out call from ${maskCaller(caller)}`);
    return goodbyeLocked(d);
  }
  return twimlResponse(pinGather(d, "Hello! This is Zozo's phone line. Please enter your four digit code."));
}

export async function handlePin(d: Deps, p: URLSearchParams): Promise<Response> {
  const caller = p.get("From") ?? "";
  const callSid = p.get("CallSid") ?? "";
  const digits = p.get("Digits") ?? "";
  if (!digits) {
    if ((await d.pin.status(caller)) !== "ok") return goodbyeLocked(d);
    return twimlResponse(pinGather(d, "Please enter your four digit code."));
  }
  const result = await d.pin.check(caller, digits);
  if (result === "ok") {
    await d.repo.markCallAuthed(callSid, caller, iso(d.now()));
    return twimlResponse(menuTwiml(d, "Hi Grandma! Welcome."));
  }
  if (result === "retry") {
    return twimlResponse(pinGather(d, "Hmm, that code didn't work. Please try again."));
  }
  return twimlResponse(
    twiml(say("Sorry, that code didn't work. Goodbye.", d.settings.voice), hangup()),
  );
}

async function requireAuthed(d: Deps, p: URLSearchParams): Promise<boolean> {
  const notBefore = iso(new Date(d.now().getTime() - CALL_AUTH_TTL_MS));
  return d.repo.isCallAuthed(p.get("CallSid") ?? "", notBefore);
}

export async function handleMenu(d: Deps, p: URLSearchParams, url: URL): Promise<Response> {
  const v = d.settings.voice;
  if (!(await requireAuthed(d, p))) {
    d.log.warn("ivr: menu request from a call that did not enter the PIN");
    return twimlResponse(twiml(hangup()));
  }
  const digit = p.get("Digits") ?? "";
  if (digit === "1") {
    const msg = await d.repo.latestPublishedZozo();
    if (!msg || !msg.media_key) {
      return twimlResponse(menuTwiml(d, "There's no message from Zozo yet. Please check back soon!"));
    }
    const token = await signMediaToken(
      msg.media_key,
      d.now().getTime() + MEDIA_TOKEN_TTL_MS,
      d.settings.mediaSigningKey,
    );
    return twimlResponse(
      twiml(
        say("Here's Zozo!", v),
        play(`${base(d)}/media/${token}`),
        pause(1),
        redirect("/twilio/menu"),
      ),
    );
  }
  if (digit === "2") {
    return twimlResponse(
      twiml(
        say("After the beep, say your message for Zozo. Press the pound key when you're done.", v),
        record({
          maxLength: MAX_RECORD_S,
          finishOnKey: "#",
          playBeep: true,
          timeout: 10,
          action: "/twilio/recorded",
          method: "POST",
          recordingStatusCallback: `${base(d)}/twilio/recording-status`,
          recordingStatusCallbackMethod: "POST",
          recordingStatusCallbackEvent: "completed",
        }),
        say("I didn't hear anything.", v),
        redirect("/twilio/menu"),
      ),
    );
  }
  const again = url.searchParams.get("again") === "1" && !digit;
  return twimlResponse(menuTwiml(d, digit ? "Sorry, I didn't get that." : undefined, again));
}

export async function handleRecorded(d: Deps, p: URLSearchParams): Promise<Response> {
  const v = d.settings.voice;
  if (!(await requireAuthed(d, p))) return twimlResponse(twiml(hangup()));
  const dur = Number(p.get("RecordingDuration") ?? "0");
  if (!(dur > 0)) {
    return twimlResponse(menuTwiml(d, "I didn't hear anything."));
  }
  return twimlResponse(
    twiml(say("Thank you! Zozo will hear your message soon. Goodbye!", v), hangup()),
  );
}

/** recordingStatusCallback: queue the ingest, then try it right away. */
export async function handleRecordingStatus(d: Deps, p: URLSearchParams): Promise<Response> {
  const sid = p.get("RecordingSid") ?? "";
  const url = p.get("RecordingUrl") ?? "";
  const status = p.get("RecordingStatus") ?? "";
  if (status !== "completed" || !RECORDING_SID_RE.test(sid) || !url) {
    return new Response("ignored", { status: 200 });
  }
  const now = d.now();
  const authed = await requireAuthed(d, p);
  const dur = Number(p.get("RecordingDuration") ?? "");
  await d.repo.insertIngest(
    {
      recording_sid: sid,
      recording_url: url,
      call_sid: p.get("CallSid"),
      duration_s: Number.isFinite(dur) ? dur : null,
      // The callback comes when the recording ends; it started `dur` seconds earlier.
      recorded_at: iso(new Date(now.getTime() - (Number.isFinite(dur) ? dur * 1000 : 0))),
      // A recording from a call that never entered the PIN is only deleted.
      status: authed ? "pending" : "rejected",
    },
    iso(now),
  );
  const row = await d.repo.getIngest(sid);
  if (row) await ingestRecording(d, row);
  return new Response("ok", { status: 200 });
}

/**
 * Copy one Twilio recording into R2 + D1, then delete Twilio's copy.
 * Resumable: 'pending' -> 'stored' -> 'done'. Failures are retried by the cron
 * up to MAX_INGEST_ATTEMPTS. Never throws.
 */
export async function ingestRecording(d: Deps, row: IngestRow): Promise<void> {
  const sid = row.recording_sid;
  try {
    if (row.status === "rejected") {
      await d.twilio.deleteRecording(sid);
      await d.repo.updateIngest(sid, "done", iso(d.now()), { error: "rejected: call not authenticated" });
      return;
    }
    if (row.status === "pending") {
      const cfg = await d.repo.getConfig();
      const recordedAt = parseIso(row.recorded_at) ?? d.now();
      const key = `recordings/grandma/${localDatePath(recordedAt, cfg.timezone)}/${sid}.wav`;
      const { data } = await d.twilio.fetchRecording(row.recording_url);
      await d.archive.put(key, data, {
        httpMetadata: { contentType: "audio/wav" },
        customMetadata: { recording_sid: sid, recorded_at: iso(recordedAt) },
      });
      await d.repo.insertGrandma(
        { id: sid, key, recorded_at: iso(recordedAt), duration_s: row.duration_s, call_sid: row.call_sid },
        iso(d.now()),
      );
      await d.repo.updateIngest(sid, "stored", iso(d.now()));
      d.log.info(`ivr: archived grandma message ${sid} at ${key}`);
    }
    // Twilio's copy goes only once ours is confirmed archived (SPEC 7.2).
    await d.twilio.deleteRecording(sid);
    await d.repo.updateIngest(sid, "done", iso(d.now()));
  } catch (e) {
    const fresh = await d.repo.getIngest(sid);
    const attempts = (fresh?.attempts ?? row.attempts) + 1;
    const status = attempts >= MAX_INGEST_ATTEMPTS ? "failed" : (fresh?.status ?? row.status);
    d.log.error(`ivr: ingest of ${sid} failed (attempt ${attempts}): ${String(e)}`);
    await d.repo.updateIngest(sid, status, iso(d.now()), { error: String(e).slice(0, 500), bump: true });
  }
}
