/**
 * Every-5-minutes job: publish Zozo's messages whose review window is over,
 * text grandma once per published message, retry recording ingests, prune.
 */

import type { Deps } from "./deps";
import { MAX_INGEST_ATTEMPTS, ingestRecording } from "./ivr";
import { computePublishAt, iso, isQuiet, parseIso } from "./time";

export const MAX_SMS_ATTEMPTS = 5;

export interface CronReport {
  published: string[];
  texted: string[];
  waitingForMedia: string[];
  ingestsRetried: number;
}

export function grandmaSmsBody(fromNumber: string): string {
  return `New message from Zozo! Call ${fromNumber} to listen.`;
}

export async function publishDue(d: Deps, report: CronReport): Promise<void> {
  const cfg = await d.repo.getConfig();
  const now = d.now();
  const quietNow = isQuiet(now, cfg);
  for (const m of await d.repo.pendingZozo()) {
    try {
      const recordedAt = parseIso(m.recorded_at);
      if (!recordedAt || !m.media_key) continue;
      // Recomputed every run, so a config change applies to pending messages.
      const publishAt = computePublishAt(recordedAt, cfg);
      if (m.publish_at !== iso(publishAt)) await d.repo.setPublishAt(m.rec_id, iso(publishAt));
      if (quietNow || now.getTime() < publishAt.getTime()) continue;
      if (!(await d.archive.head(m.media_key))) {
        // The "new" command can arrive before the audio upload; wait for it.
        report.waitingForMedia.push(m.rec_id);
        continue;
      }
      if (await d.repo.markZozoPublished(m.rec_id, iso(now))) {
        report.published.push(m.rec_id);
        d.log.info(`cron: published Zozo message ${m.rec_id}`);
      }
    } catch (e) {
      d.log.error(`cron: publishing ${m.rec_id} failed: ${String(e)}`);
    }
  }
}

export async function sendGrandmaTexts(d: Deps, report: CronReport): Promise<void> {
  if (!d.settings.grandmaNumber) {
    d.log.warn("cron: GRANDMA_NUMBER not set; not texting");
    return;
  }
  for (const m of await d.repo.zozoNeedingSms(MAX_SMS_ATTEMPTS)) {
    try {
      await d.twilio.sendSms(d.settings.grandmaNumber, grandmaSmsBody(d.settings.fromNumber));
      await d.repo.markSmsSent(m.rec_id, iso(d.now()));
      report.texted.push(m.rec_id);
    } catch (e) {
      d.log.error(`cron: texting grandma about ${m.rec_id} failed: ${String(e)}`);
      await d.repo.bumpSmsAttempt(m.rec_id, iso(d.now()));
    }
  }
}

export async function runCron(d: Deps): Promise<CronReport> {
  const report: CronReport = { published: [], texted: [], waitingForMedia: [], ingestsRetried: 0 };
  const steps: [string, () => Promise<void>][] = [
    ["publish", () => publishDue(d, report)],
    ["sms", () => sendGrandmaTexts(d, report)],
    [
      "ingest",
      async () => {
        for (const row of await d.repo.ingestsToRetry(MAX_INGEST_ATTEMPTS)) {
          report.ingestsRetried++;
          await ingestRecording(d, row);
        }
      },
    ],
    ["prune", () => d.repo.pruneCalls(iso(new Date(d.now().getTime() - 86400_000)))],
  ];
  for (const [name, step] of steps) {
    try {
      await step();
    } catch (e) {
      d.log.error(`cron: ${name} step failed: ${String(e)}`);
    }
  }
  return report;
}
