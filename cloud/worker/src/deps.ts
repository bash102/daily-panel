/** Everything a handler needs, injected (fakes in tests, bindings in index.ts). */

import { PinGuard, type PinOptions } from "./pin";
import { Repo } from "./repo";
import { TwilioRest, type TwilioApi } from "./twilioApi";
import type { Clock, FetchFn, Logger, ObjectStore, SqlDb } from "./types";

export interface Settings {
  publicBaseUrl: string;
  voice: string;
  grandmaPin: string;
  boxToken: string;
  mediaSigningKey: string;
  twilioAuthToken: string;
  fromNumber: string;
  grandmaNumber: string;
  parentNumber: string;
  pin: Partial<PinOptions>;
}

export interface Deps {
  repo: Repo;
  archive: ObjectStore;
  twilio: TwilioApi;
  pin: PinGuard;
  now: Clock;
  log: Logger;
  settings: Settings;
}

export const DEFAULT_VOICE = "Polly.Joanna";

export interface DepsInput {
  db: SqlDb;
  archive: ObjectStore;
  settings: Settings;
  twilioAccountSid: string;
  now?: Clock;
  fetchFn?: FetchFn;
  log?: Logger;
  twilio?: TwilioApi;
}

export function makeDeps(i: DepsInput): Deps {
  const now = i.now ?? (() => new Date());
  const log = i.log ?? console;
  const repo = new Repo(i.db);
  const twilio =
    i.twilio ?? new TwilioRest(i.twilioAccountSid, i.settings.twilioAuthToken, i.settings.fromNumber, i.fetchFn);
  const notifyParent = async (body: string) => {
    if (!i.settings.parentNumber) {
      log.warn("pin: PARENT_NUMBER not set; not texting");
      return;
    }
    await twilio.sendSms(i.settings.parentNumber, body);
  };
  const pin = new PinGuard(repo, i.settings.grandmaPin, notifyParent, now, i.settings.pin, log, [
    i.settings.grandmaNumber,
  ]);
  return { repo, archive: i.archive, twilio, pin, now, log, settings: i.settings };
}
