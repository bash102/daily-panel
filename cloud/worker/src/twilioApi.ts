/** Twilio REST calls (SMS, recording download and delete) over an injected fetch. */

import type { FetchFn } from "./types";

export const TWILIO_API_HOST = "api.twilio.com";

export interface TwilioApi {
  sendSms(to: string, body: string): Promise<void>;
  /** Download a recording's audio as WAV. Only api.twilio.com URLs are fetched. */
  fetchRecording(recordingUrl: string): Promise<{ data: ArrayBuffer; contentType: string }>;
  /** Delete Twilio's copy. A recording that is already gone counts as deleted. */
  deleteRecording(recordingSid: string): Promise<void>;
}

export class TwilioError extends Error {}

export class TwilioRest implements TwilioApi {
  constructor(
    private readonly accountSid: string,
    private readonly authToken: string,
    private readonly fromNumber: string,
    private readonly fetchFn: FetchFn = (i, init) => fetch(i, init),
  ) {}

  private auth(): string {
    return "Basic " + btoa(`${this.accountSid}:${this.authToken}`);
  }

  private base(): string {
    return `https://${TWILIO_API_HOST}/2010-04-01/Accounts/${encodeURIComponent(this.accountSid)}`;
  }

  async sendSms(to: string, body: string): Promise<void> {
    if (!to || !this.fromNumber) throw new TwilioError("sms: missing to/from number");
    const res = await this.fetchFn(`${this.base()}/Messages.json`, {
      method: "POST",
      headers: { authorization: this.auth(), "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ To: to, From: this.fromNumber, Body: body }).toString(),
    });
    if (!res.ok) throw new TwilioError(`sms: HTTP ${res.status}`);
  }

  async fetchRecording(recordingUrl: string): Promise<{ data: ArrayBuffer; contentType: string }> {
    let u: URL;
    try {
      u = new URL(recordingUrl);
    } catch {
      throw new TwilioError("recording: bad URL");
    }
    // Never send our credentials anywhere but Twilio's API.
    if (u.protocol !== "https:" || u.hostname !== TWILIO_API_HOST) {
      throw new TwilioError("recording: URL is not on api.twilio.com");
    }
    if (!/\.(wav|mp3)$/.test(u.pathname)) u.pathname += ".wav";
    const res = await this.fetchFn(u.toString(), { headers: { authorization: this.auth() } });
    if (!res.ok) throw new TwilioError(`recording: HTTP ${res.status}`);
    const data = await res.arrayBuffer();
    if (data.byteLength === 0) throw new TwilioError("recording: empty body");
    return { data, contentType: res.headers.get("content-type") ?? "audio/wav" };
  }

  async deleteRecording(recordingSid: string): Promise<void> {
    if (!/^RE[0-9a-fA-F]{32}$/.test(recordingSid)) throw new TwilioError("recording: bad sid");
    const res = await this.fetchFn(`${this.base()}/Recordings/${recordingSid}.json`, {
      method: "DELETE",
      headers: { authorization: this.auth() },
    });
    if (!res.ok && res.status !== 404) throw new TwilioError(`delete recording: HTTP ${res.status}`);
  }
}
