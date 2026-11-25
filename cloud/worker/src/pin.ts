/**
 * PIN check with lockouts (SPEC 7.2):
 * - 3 wrong PINs from one caller (within an hour) lock that caller out for 1
 *   hour and text the parent once for that lockout.
 * - A line-wide limit (beyond the SPEC, against brute force from many
 *   numbers): `globalLimit` wrong PINs from anyone within an hour lock the
 *   line for 1 hour and text the parent once. The line lock never applies to
 *   `lineLockExempt` numbers (GRANDMA_NUMBER), so strangers cannot lock
 *   grandma out; those numbers still have their own 3-try lock. Set
 *   `globalLimit` to 0 to turn the line-wide lock off.
 */

import { timingSafeEqual } from "./crypto";
import type { CallerLock, Repo } from "./repo";
import { iso, parseIso } from "./time";
import type { Clock, Logger } from "./types";

export type PinStatus = "ok" | "locked" | "line_locked";
export type PinResult = "ok" | "retry" | "locked";

export interface PinOptions {
  maxTries: number;
  lockoutMs: number;
  windowMs: number;
  globalLimit: number;
}

export const DEFAULT_PIN_OPTIONS: PinOptions = {
  maxTries: 3,
  lockoutMs: 3600_000,
  windowMs: 3600_000,
  globalLimit: 10,
};

export type Notify = (body: string) => Promise<void>;

/** Mask a phone number for logs and texts: keep the last 4 digits. */
export function maskCaller(caller: string): string {
  if (!caller) return "an unknown number";
  return caller.length <= 4 ? caller : "…" + caller.slice(-4);
}

/** Compare caller ids loosely: digits only (and a leading +1 is optional). */
export function sameNumber(a: string, b: string): boolean {
  const d = (s: string) => s.replace(/\D/g, "").replace(/^1(?=\d{10}$)/, "");
  const x = d(a);
  return x.length > 0 && x === d(b);
}

export class PinGuard {
  readonly opts: PinOptions;

  constructor(
    private readonly repo: Repo,
    private readonly pin: string,
    private readonly notifyParent: Notify,
    private readonly now: Clock,
    opts: Partial<PinOptions> = {},
    private readonly log: Logger = console,
    private readonly lineLockExempt: string[] = [],
  ) {
    this.opts = { ...DEFAULT_PIN_OPTIONS, ...opts };
  }

  private active(until: string | null, now: Date): boolean {
    const t = parseIso(until);
    return t !== null && t.getTime() > now.getTime();
  }

  isExempt(caller: string): boolean {
    return this.lineLockExempt.some((n) => n && sameNumber(caller, n));
  }

  async status(caller: string): Promise<PinStatus> {
    const now = this.now();
    if (!this.isExempt(caller)) {
      const g = await this.repo.getGlobalLock();
      if (this.active(g.locked_until, now)) return "line_locked";
    }
    const c = await this.repo.getCallerLock(caller);
    if (c && this.active(c.locked_until, now)) return "locked";
    return "ok";
  }

  async check(caller: string, digits: string): Promise<PinResult> {
    if ((await this.status(caller)) !== "ok") return "locked";
    const good = /^\d{4}$/.test(this.pin) && (await timingSafeEqual(digits, this.pin));
    const now = this.now();
    const nowIso = iso(now);
    const prev: CallerLock = (await this.repo.getCallerLock(caller)) ?? {
      caller,
      failures: 0,
      first_failure_at: null,
      locked_until: null,
      notified_at: null,
    };
    if (good) {
      await this.repo.saveCallerLock({ ...prev, failures: 0, first_failure_at: null });
      return "ok";
    }

    // Wrong PIN: count it for this caller ...
    const first = parseIso(prev.first_failure_at);
    const fresh = !first || now.getTime() - first.getTime() > this.opts.windowMs;
    const failures = (fresh ? 0 : prev.failures) + 1;
    let result: PinResult = "retry";
    const next: CallerLock = { ...prev, failures, first_failure_at: fresh ? nowIso : prev.first_failure_at };
    if (failures >= this.opts.maxTries) {
      next.failures = 0;
      next.first_failure_at = null;
      next.locked_until = iso(new Date(now.getTime() + this.opts.lockoutMs));
      next.notified_at = nowIso;
      result = "locked";
      this.log.warn(`pin: ${this.opts.maxTries} wrong tries from ${maskCaller(caller)}; locked out`);
      await this.safeNotify(
        `Zozo box: ${this.opts.maxTries} wrong PIN tries on Grandma's line from ${caller || "an unknown number"}. ` +
          `That number is locked out for 1 hour.`,
      );
    }
    await this.repo.saveCallerLock(next);

    // ... and toward the line-wide limit.
    if (this.opts.globalLimit <= 0) return result;
    const g = await this.repo.getGlobalLock();
    const ws = parseIso(g.window_start);
    const gFresh = !ws || now.getTime() - ws.getTime() > this.opts.windowMs;
    const gFailures = (gFresh ? 0 : g.failures) + 1;
    if (gFailures >= this.opts.globalLimit) {
      this.log.warn("pin: line-wide wrong-PIN limit reached; line locked");
      await this.repo.saveGlobalLock({
        window_start: null,
        failures: 0,
        locked_until: iso(new Date(now.getTime() + this.opts.lockoutMs)),
        notified_at: nowIso,
      });
      await this.safeNotify(
        "Zozo box: too many wrong PINs on Grandma's line. The line is locked for 1 hour " +
          "(Grandma's own number can still call).",
      );
      if (!this.isExempt(caller)) result = "locked";
    } else {
      await this.repo.saveGlobalLock({
        ...g,
        window_start: gFresh ? nowIso : g.window_start,
        failures: gFailures,
      });
    }
    return result;
  }

  private async safeNotify(body: string): Promise<void> {
    try {
      await this.notifyParent(body);
    } catch (e) {
      this.log.error(`pin: could not text the parent: ${String(e)}`);
    }
  }
}
