/**
 * Time helpers: ISO parsing, local wall-clock in an IANA zone, quiet hours
 * and the review-window computation (SPEC 2.2 / 6: the 1-hour review clock
 * runs from recording time and pauses during quiet hours).
 */

export interface QuietHours {
  start: string; // "HH:MM" local
  end: string; // "HH:MM" local; start == end means never quiet
}

export interface BoxConfig {
  quiet_hours: QuietHours;
  timezone: string;
  review_delay_s: number;
}

/** Same defaults as the Pi's store settings. */
export const DEFAULT_CONFIG: BoxConfig = {
  quiet_hours: { start: "19:00", end: "07:00" },
  timezone: "America/New_York",
  review_delay_s: 3600,
};

export const MAX_REVIEW_DELAY_S = 7 * 86400;

const ISO_RE =
  /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?(Z|[+-]\d{2}:?\d{2})$/;

/** Parse an ISO 8601 timestamp with an explicit zone (Z or +HH:MM). Any
 * number of fractional digits is accepted (Python emits microseconds). */
export function parseIso(s: unknown): Date | null {
  if (typeof s !== "string") return null;
  const m = ISO_RE.exec(s.trim());
  if (!m) return null;
  const [, y, mo, d, h, mi, sec, frac, zone] = m;
  const ms = frac ? Math.floor(Number((frac + "000").slice(0, 3))) : 0;
  let t = Date.UTC(+y!, +mo! - 1, +d!, +h!, +mi!, sec ? +sec : 0, ms);
  if (zone !== "Z") {
    const sign = zone![0] === "-" ? -1 : 1;
    const digits = zone!.slice(1).replace(":", "");
    t -= sign * (Number(digits.slice(0, 2)) * 60 + Number(digits.slice(2))) * 60000;
  }
  const out = new Date(t);
  // Reject roll-overs such as month 13 or Feb 30.
  if (Number.isNaN(out.getTime()) || +mo! < 1 || +mo! > 12 || +d! < 1 || +d! > 31 || +h! > 23 || +mi! > 59) {
    return null;
  }
  return out;
}

/** Canonical form stored in D1 and returned by the API: fixed-width UTC, sorts lexically. */
export function iso(d: Date): string {
  return d.toISOString();
}

const HHMM_RE = /^([01]\d|2[0-3]):([0-5]\d)$/;

export function isHhmm(s: unknown): s is string {
  return typeof s === "string" && HHMM_RE.test(s);
}

export function hhmmToMin(s: string): number {
  const m = HHMM_RE.exec(s);
  if (!m) throw new Error(`bad HH:MM ${s}`);
  return Number(m[1]) * 60 + Number(m[2]);
}

export function isValidTimeZone(tz: unknown): tz is string {
  if (typeof tz !== "string" || !tz) return false;
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: tz });
    return true;
  } catch {
    return false;
  }
}

export interface LocalParts {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
  second: number;
}

const fmtCache = new Map<string, Intl.DateTimeFormat>();

function formatter(tz: string): Intl.DateTimeFormat {
  let f = fmtCache.get(tz);
  if (!f) {
    f = new Intl.DateTimeFormat("en-US", {
      timeZone: tz,
      hourCycle: "h23",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
    fmtCache.set(tz, f);
  }
  return f;
}

export function localParts(d: Date, tz: string): LocalParts {
  const out: Record<string, number> = {};
  for (const p of formatter(tz).formatToParts(d)) {
    if (p.type !== "literal") out[p.type] = Number(p.value);
  }
  return {
    year: out.year!,
    month: out.month!,
    day: out.day!,
    hour: out.hour! % 24,
    minute: out.minute!,
    second: out.second!,
  };
}

/** Local minus UTC, in ms, at instant d. */
export function tzOffsetMs(d: Date, tz: string): number {
  const p = localParts(d, tz);
  const asUtc = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second);
  return asUtc - (d.getTime() - d.getUTCMilliseconds());
}

export function quietEnabled(q: QuietHours): boolean {
  return q.start !== q.end;
}

/** Same rule as the Pi (zozobox.store.settings.QuietHours.contains). */
export function isQuiet(d: Date, cfg: Pick<BoxConfig, "quiet_hours" | "timezone">): boolean {
  const q = cfg.quiet_hours;
  if (!quietEnabled(q)) return false;
  const p = localParts(d, cfg.timezone);
  const m = p.hour * 60 + p.minute;
  const s = hhmmToMin(q.start);
  const e = hhmmToMin(q.end);
  if (s < e) return m >= s && m < e;
  return m >= s || m < e;
}

/** Next instant after d at which the quiet state flips. Requires quiet hours enabled. */
export function nextTransition(d: Date, cfg: Pick<BoxConfig, "quiet_hours" | "timezone">): Date {
  const q = cfg.quiet_hours;
  const target = hhmmToMin(isQuiet(d, cfg) ? q.end : q.start);
  const p = localParts(d, cfg.timezone);
  const cur = p.hour * 60 + p.minute;
  let deltaMin = (((target - cur) % 1440) + 1440) % 1440;
  if (deltaMin === 0) deltaMin = 1440;
  const deltaMs = deltaMin * 60000 - p.second * 1000 - d.getUTCMilliseconds();
  const naive = new Date(d.getTime() + deltaMs);
  // Wall-clock delta -> real delta: correct for a DST change in between.
  const fixed = new Date(naive.getTime() - (tzOffsetMs(naive, cfg.timezone) - tzOffsetMs(d, cfg.timezone)));
  return fixed.getTime() > d.getTime() ? fixed : naive;
}

/**
 * When a Zozo message recorded at `recordedAt` publishes: after `review_delay_s`
 * seconds of review clock, where the clock only runs outside quiet hours. A
 * window that runs out exactly as quiet hours start waits for them to end, and
 * a message recorded during quiet hours starts its clock when they end.
 */
export function computePublishAt(recordedAt: Date, cfg: BoxConfig): Date {
  let remaining = Math.max(0, cfg.review_delay_s) * 1000;
  let t = recordedAt;
  if (!quietEnabled(cfg.quiet_hours)) return new Date(t.getTime() + remaining);
  for (let i = 0; i < 2000; i++) {
    const nt = nextTransition(t, cfg);
    if (isQuiet(t, cfg)) {
      t = nt;
      continue;
    }
    const left = nt.getTime() - t.getTime();
    if (remaining < left) return new Date(t.getTime() + remaining);
    remaining -= left;
    t = nt;
  }
  return new Date(t.getTime() + remaining); // unreachable in practice
}

/** "YYYY/MM/DD" for d in tz (archive key folders). */
export function localDatePath(d: Date, tz: string): string {
  const p = localParts(d, tz);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${p.year}/${pad(p.month)}/${pad(p.day)}`;
}
