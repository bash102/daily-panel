/** D1 data access. All SQL lives here; everything else talks to Repo. */

import type { SqlDb } from "./types";
import { DEFAULT_CONFIG, type BoxConfig } from "./time";

export type ZozoStatus = "pending" | "published" | "withheld" | "superseded";

export interface ZozoRow {
  rec_id: string;
  media_key: string | null;
  recorded_at: string | null;
  duration_s: number | null;
  status: ZozoStatus;
  registered_at: string;
  publish_at: string | null;
  published_at: string | null;
  withheld_at: string | null;
  superseded_by: string | null;
  sms_sent_at: string | null;
  sms_attempts: number;
  updated_at: string;
}

export interface GrandmaRow {
  id: string;
  key: string;
  recorded_at: string;
  duration_s: number | null;
  call_sid: string | null;
  created_at: string;
}

export type IngestStatus = "pending" | "stored" | "done" | "failed" | "rejected";

export interface IngestRow {
  recording_sid: string;
  recording_url: string;
  call_sid: string | null;
  duration_s: number | null;
  recorded_at: string;
  status: IngestStatus;
  attempts: number;
  last_error: string | null;
  updated_at: string;
}

export interface CallerLock {
  caller: string;
  failures: number;
  first_failure_at: string | null;
  locked_until: string | null;
  notified_at: string | null;
}

export interface GlobalLock {
  window_start: string | null;
  failures: number;
  locked_until: string | null;
  notified_at: string | null;
}

export interface NewZozo {
  rec_id: string;
  media_key: string;
  recorded_at: string;
  duration_s: number | null;
}

export type RegisterResult = "registered" | "superseded" | "exists" | "withheld";

export class Repo {
  constructor(private readonly db: SqlDb) {}

  // -- config -----------------------------------------------------------------
  async getConfig(): Promise<BoxConfig> {
    const r = await this.db
      .prepare("SELECT quiet_start, quiet_end, timezone, review_delay_s FROM config WHERE id = 1")
      .first<{ quiet_start: string; quiet_end: string; timezone: string; review_delay_s: number }>();
    if (!r) return { ...DEFAULT_CONFIG, quiet_hours: { ...DEFAULT_CONFIG.quiet_hours } };
    return {
      quiet_hours: { start: r.quiet_start, end: r.quiet_end },
      timezone: r.timezone,
      review_delay_s: Number(r.review_delay_s),
    };
  }

  async setConfig(cfg: BoxConfig, now: string): Promise<void> {
    await this.db
      .prepare(
        "INSERT INTO config(id, quiet_start, quiet_end, timezone, review_delay_s, updated_at) " +
          "VALUES (1, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET quiet_start = excluded.quiet_start, " +
          "quiet_end = excluded.quiet_end, timezone = excluded.timezone, " +
          "review_delay_s = excluded.review_delay_s, updated_at = excluded.updated_at",
      )
      .bind(cfg.quiet_hours.start, cfg.quiet_hours.end, cfg.timezone, cfg.review_delay_s, now)
      .run();
  }

  // -- Zozo's messages --------------------------------------------------------
  async getZozo(recId: string): Promise<ZozoRow | null> {
    return this.db.prepare("SELECT * FROM zozo_messages WHERE rec_id = ?").bind(recId).first<ZozoRow>();
  }

  /**
   * Register a reviewed-then-published Zozo message. Idempotent. A message
   * the parent already withheld (withhold arrived first) stays withheld. The
   * single live slot: older pending messages are superseded by a newer one,
   * and a message older than an existing pending/published one is registered
   * as superseded straight away.
   */
  async registerZozo(m: NewZozo, now: string): Promise<RegisterResult> {
    const cur = await this.getZozo(m.rec_id);
    if (cur) {
      if (cur.media_key === null || cur.recorded_at === null) {
        await this.db
          .prepare(
            "UPDATE zozo_messages SET media_key = ?, recorded_at = ?, duration_s = ?, updated_at = ? WHERE rec_id = ?",
          )
          .bind(m.media_key, m.recorded_at, m.duration_s, now, m.rec_id)
          .run();
      }
      return cur.status === "withheld" ? "withheld" : "exists";
    }
    const newer = await this.db
      .prepare(
        "SELECT rec_id FROM zozo_messages WHERE status IN ('pending', 'published') AND recorded_at > ? " +
          "ORDER BY recorded_at DESC LIMIT 1",
      )
      .bind(m.recorded_at)
      .first<{ rec_id: string }>();
    const status: ZozoStatus = newer ? "superseded" : "pending";
    await this.db
      .prepare(
        "INSERT INTO zozo_messages(rec_id, media_key, recorded_at, duration_s, status, registered_at, " +
          "superseded_by, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
      )
      .bind(m.rec_id, m.media_key, m.recorded_at, m.duration_s, status, now, newer?.rec_id ?? null, now)
      .run();
    if (!newer) {
      await this.db
        .prepare(
          "UPDATE zozo_messages SET status = 'superseded', superseded_by = ?, updated_at = ? " +
            "WHERE status = 'pending' AND rec_id <> ? AND recorded_at <= ?",
        )
        .bind(m.rec_id, now, m.rec_id, m.recorded_at)
        .run();
    }
    return status === "pending" ? "registered" : "superseded";
  }

  /** Never publish (or unpublish). Unknown ids get a tombstone so a late "new" stays withheld. */
  async withholdZozo(recId: string, now: string): Promise<void> {
    await this.db
      .prepare(
        "INSERT INTO zozo_messages(rec_id, status, registered_at, withheld_at, updated_at) " +
          "VALUES (?, 'withheld', ?, ?, ?) ON CONFLICT(rec_id) DO UPDATE SET status = 'withheld', " +
          "withheld_at = COALESCE(zozo_messages.withheld_at, excluded.withheld_at), updated_at = excluded.updated_at",
      )
      .bind(recId, now, now, now)
      .run();
  }

  async pendingZozo(): Promise<ZozoRow[]> {
    const r = await this.db
      .prepare(
        "SELECT * FROM zozo_messages WHERE status = 'pending' AND media_key IS NOT NULL " +
          "AND recorded_at IS NOT NULL ORDER BY recorded_at",
      )
      .all<ZozoRow>();
    return r.results;
  }

  async setPublishAt(recId: string, publishAt: string): Promise<void> {
    await this.db
      .prepare("UPDATE zozo_messages SET publish_at = ? WHERE rec_id = ? AND status = 'pending'")
      .bind(publishAt, recId)
      .run();
  }

  /** Returns false when the message stopped being pending (e.g. withheld meanwhile). */
  async markZozoPublished(recId: string, now: string): Promise<boolean> {
    const r = await this.db
      .prepare(
        "UPDATE zozo_messages SET status = 'published', published_at = ?, updated_at = ? " +
          "WHERE rec_id = ? AND status = 'pending'",
      )
      .bind(now, now, recId)
      .run();
    return (r.meta.changes ?? 0) > 0;
  }

  async zozoNeedingSms(maxAttempts: number): Promise<ZozoRow[]> {
    const r = await this.db
      .prepare(
        "SELECT * FROM zozo_messages WHERE status = 'published' AND sms_sent_at IS NULL " +
          "AND sms_attempts < ? ORDER BY published_at",
      )
      .bind(maxAttempts)
      .all<ZozoRow>();
    return r.results;
  }

  async markSmsSent(recId: string, now: string): Promise<void> {
    await this.db
      .prepare(
        "UPDATE zozo_messages SET sms_sent_at = ?, sms_attempts = sms_attempts + 1, updated_at = ? WHERE rec_id = ?",
      )
      .bind(now, now, recId)
      .run();
  }

  async bumpSmsAttempt(recId: string, now: string): Promise<void> {
    await this.db
      .prepare("UPDATE zozo_messages SET sms_attempts = sms_attempts + 1, updated_at = ? WHERE rec_id = ?")
      .bind(now, recId)
      .run();
  }

  /** What grandma hears on "press 1": the newest published, non-withheld message. */
  async latestPublishedZozo(): Promise<ZozoRow | null> {
    return this.db
      .prepare(
        "SELECT * FROM zozo_messages WHERE status = 'published' AND media_key IS NOT NULL " +
          "ORDER BY recorded_at DESC LIMIT 1",
      )
      .first<ZozoRow>();
  }

  async listZozo(limit: number): Promise<ZozoRow[]> {
    const r = await this.db
      .prepare("SELECT * FROM zozo_messages ORDER BY registered_at DESC LIMIT ?")
      .bind(limit)
      .all<ZozoRow>();
    return r.results;
  }

  // -- grandma's messages -----------------------------------------------------
  async insertGrandma(g: Omit<GrandmaRow, "created_at">, now: string): Promise<void> {
    await this.db
      .prepare(
        "INSERT OR IGNORE INTO grandma_messages(id, key, recorded_at, duration_s, call_sid, created_at) " +
          "VALUES (?, ?, ?, ?, ?, ?)",
      )
      .bind(g.id, g.key, g.recorded_at, g.duration_s, g.call_sid, now)
      .run();
  }

  /** Messages with recorded_at >= since (inclusive), oldest first. */
  async grandmaSince(since: string | null, limit: number): Promise<GrandmaRow[]> {
    const r = await this.db
      .prepare(
        "SELECT * FROM grandma_messages WHERE recorded_at >= ? ORDER BY recorded_at, id LIMIT ?",
      )
      .bind(since ?? "", limit)
      .all<GrandmaRow>();
    return r.results;
  }

  // -- Twilio recording ingest -----------------------------------------------
  async getIngest(sid: string): Promise<IngestRow | null> {
    return this.db.prepare("SELECT * FROM twilio_ingest WHERE recording_sid = ?").bind(sid).first<IngestRow>();
  }

  async insertIngest(
    row: Pick<IngestRow, "recording_sid" | "recording_url" | "call_sid" | "duration_s" | "recorded_at" | "status">,
    now: string,
  ): Promise<void> {
    await this.db
      .prepare(
        "INSERT OR IGNORE INTO twilio_ingest(recording_sid, recording_url, call_sid, duration_s, recorded_at, " +
          "status, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
      )
      .bind(row.recording_sid, row.recording_url, row.call_sid, row.duration_s, row.recorded_at, row.status, now)
      .run();
  }

  async updateIngest(
    sid: string,
    status: IngestStatus,
    now: string,
    opts: { error?: string | null; bump?: boolean } = {},
  ): Promise<void> {
    await this.db
      .prepare(
        "UPDATE twilio_ingest SET status = ?, last_error = ?, attempts = attempts + ?, updated_at = ? " +
          "WHERE recording_sid = ?",
      )
      .bind(status, opts.error ?? null, opts.bump ? 1 : 0, now, sid)
      .run();
  }

  async ingestsToRetry(maxAttempts: number): Promise<IngestRow[]> {
    const r = await this.db
      .prepare(
        "SELECT * FROM twilio_ingest WHERE status IN ('pending', 'stored', 'rejected') AND attempts < ? " +
          "ORDER BY recorded_at LIMIT 20",
      )
      .bind(maxAttempts)
      .all<IngestRow>();
    return r.results;
  }

  // -- PIN lockouts -----------------------------------------------------------
  async getCallerLock(caller: string): Promise<CallerLock | null> {
    return this.db.prepare("SELECT * FROM pin_callers WHERE caller = ?").bind(caller).first<CallerLock>();
  }

  async saveCallerLock(l: CallerLock): Promise<void> {
    await this.db
      .prepare(
        "INSERT INTO pin_callers(caller, failures, first_failure_at, locked_until, notified_at) VALUES (?, ?, ?, ?, ?) " +
          "ON CONFLICT(caller) DO UPDATE SET failures = excluded.failures, first_failure_at = excluded.first_failure_at, " +
          "locked_until = excluded.locked_until, notified_at = excluded.notified_at",
      )
      .bind(l.caller, l.failures, l.first_failure_at, l.locked_until, l.notified_at)
      .run();
  }

  async getGlobalLock(): Promise<GlobalLock> {
    const r = await this.db.prepare("SELECT * FROM pin_global WHERE id = 1").first<GlobalLock>();
    return r ?? { window_start: null, failures: 0, locked_until: null, notified_at: null };
  }

  async saveGlobalLock(g: GlobalLock): Promise<void> {
    await this.db
      .prepare(
        "INSERT INTO pin_global(id, window_start, failures, locked_until, notified_at) VALUES (1, ?, ?, ?, ?) " +
          "ON CONFLICT(id) DO UPDATE SET window_start = excluded.window_start, failures = excluded.failures, " +
          "locked_until = excluded.locked_until, notified_at = excluded.notified_at",
      )
      .bind(g.window_start, g.failures, g.locked_until, g.notified_at)
      .run();
  }

  // -- authenticated calls ----------------------------------------------------
  async markCallAuthed(callSid: string, caller: string, now: string): Promise<void> {
    await this.db
      .prepare(
        "INSERT INTO calls(call_sid, caller, authed_at) VALUES (?, ?, ?) " +
          "ON CONFLICT(call_sid) DO UPDATE SET authed_at = excluded.authed_at",
      )
      .bind(callSid, caller, now)
      .run();
  }

  async isCallAuthed(callSid: string, notBefore: string): Promise<boolean> {
    if (!callSid) return false;
    const r = await this.db
      .prepare("SELECT 1 AS ok FROM calls WHERE call_sid = ? AND authed_at >= ?")
      .bind(callSid, notBefore)
      .first<{ ok: number }>();
    return r !== null;
  }

  async pruneCalls(before: string): Promise<void> {
    await this.db.prepare("DELETE FROM calls WHERE authed_at < ?").bind(before).run();
  }
}
