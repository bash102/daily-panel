import { describe, expect, it } from "vitest";
import {
  computePublishAt,
  isQuiet,
  localDatePath,
  nextTransition,
  parseIso,
  type BoxConfig,
} from "../src/time";

const NY: BoxConfig = { quiet_hours: { start: "19:00", end: "07:00" }, timezone: "America/New_York", review_delay_s: 3600 };
const at = (s: string) => new Date(s);
const pub = (rec: string, cfg: BoxConfig) => computePublishAt(at(rec), cfg).toISOString();

describe("parseIso", () => {
  it("accepts Z, offsets and microseconds", () => {
    expect(parseIso("2026-11-25T10:00:00Z")?.toISOString()).toBe("2026-11-25T10:00:00.000Z");
    expect(parseIso("2026-11-25T10:00:00.123456+00:00")?.toISOString()).toBe("2026-11-25T10:00:00.123Z");
    expect(parseIso("2026-11-25T05:00:00-05:00")?.toISOString()).toBe("2026-11-25T10:00:00.000Z");
  });
  it("rejects naive or garbage values", () => {
    expect(parseIso("2026-11-25T10:00:00")).toBeNull();
    expect(parseIso("yesterday")).toBeNull();
    expect(parseIso(42)).toBeNull();
    expect(parseIso("2026-13-01T00:00:00Z")).toBeNull();
  });
});

describe("quiet hours", () => {
  it("spans midnight in the configured timezone", () => {
    // 23:30 New York (EST, UTC-5) = 04:30Z
    expect(isQuiet(at("2026-11-26T04:30:00Z"), NY)).toBe(true);
    // 06:59 NY quiet, 07:00 not (end is exclusive)
    expect(isQuiet(at("2026-11-26T11:59:00Z"), NY)).toBe(true);
    expect(isQuiet(at("2026-11-26T12:00:00Z"), NY)).toBe(false);
    // noon NY
    expect(isQuiet(at("2026-11-25T17:00:00Z"), NY)).toBe(false);
  });
  it("uses the timezone, not UTC", () => {
    const berlin = { ...NY, timezone: "Europe/Berlin" };
    // 20:00Z is 15:00 in New York (awake) and 21:00 in Berlin (quiet)
    expect(isQuiet(at("2026-11-25T20:00:00Z"), NY)).toBe(false);
    expect(isQuiet(at("2026-11-25T20:00:00Z"), berlin)).toBe(true);
  });
  it("start == end means never quiet", () => {
    const off = { ...NY, quiet_hours: { start: "07:00", end: "07:00" } };
    expect(isQuiet(at("2026-11-26T04:30:00Z"), off)).toBe(false);
  });
  it("finds the next transition", () => {
    expect(nextTransition(at("2026-11-25T17:00:00Z"), NY).toISOString()).toBe("2026-11-26T00:00:00.000Z");
    expect(nextTransition(at("2026-11-26T04:30:00Z"), NY).toISOString()).toBe("2026-11-26T12:00:00.000Z");
  });
});

describe("computePublishAt", () => {
  it("is recording time + delay outside quiet hours", () => {
    expect(pub("2026-11-25T15:00:00Z", NY)).toBe("2026-11-25T16:00:00.000Z"); // 10:00 -> 11:00 NY
  });
  it("pauses the review clock across quiet hours over midnight", () => {
    // 18:30 NY: 30 min of review before 19:00, the rest after 07:00 -> 07:30 next day
    expect(pub("2026-11-25T23:30:00Z", NY)).toBe("2026-11-26T12:30:00.000Z");
  });
  it("starts the clock at the end of quiet hours for a message recorded during them", () => {
    // 23:00 NY -> 07:00 + 1h = 08:00 NY = 13:00Z
    expect(pub("2026-11-26T04:00:00Z", NY)).toBe("2026-11-26T13:00:00.000Z");
    // just after midnight, same answer
    expect(pub("2026-11-26T05:10:00Z", NY)).toBe("2026-11-26T13:00:00.000Z");
  });
  it("depends on the timezone", () => {
    const tokyo: BoxConfig = { ...NY, timezone: "Asia/Tokyo" };
    // 18:30 Tokyo (09:30Z) -> 07:30 Tokyo next day (22:30Z)
    expect(pub("2026-11-25T09:30:00Z", tokyo)).toBe("2026-11-25T22:30:00.000Z");
  });
  it("handles a DST change during quiet hours", () => {
    // US DST ends 2026-11-01 02:00. 18:30 EDT Oct 31 (22:30Z) -> 07:30 EST Nov 1 (12:30Z)
    expect(pub("2026-10-31T22:30:00Z", NY)).toBe("2026-11-01T12:30:00.000Z");
  });
  it("a window ending exactly at quiet start waits until quiet hours end", () => {
    // 18:00 NY + 1h = 19:00 exactly -> wait till 07:00
    expect(pub("2026-11-25T23:00:00Z", NY)).toBe("2026-11-26T12:00:00.000Z");
  });
  it("zero delay publishes immediately, or at the end of quiet hours", () => {
    const zero = { ...NY, review_delay_s: 0 };
    expect(pub("2026-11-25T15:00:00Z", zero)).toBe("2026-11-25T15:00:00.000Z");
    expect(pub("2026-11-26T04:00:00Z", zero)).toBe("2026-11-26T12:00:00.000Z");
  });
  it("a long delay spans several nights", () => {
    const long = { ...NY, review_delay_s: 13 * 3600 }; // 12h awake per day
    // 07:00 NY day 1: 12h until 19:00, 1h more after 07:00 next day -> 08:00 NY day 2
    expect(pub("2026-11-25T12:00:00Z", long)).toBe("2026-11-26T13:00:00.000Z");
  });
  it("handles quiet hours that do not span midnight", () => {
    const nap = { ...NY, quiet_hours: { start: "13:00", end: "15:00" } };
    // 12:30 NY + 1h -> 30 min before nap, 30 after -> 15:30 NY = 20:30Z
    expect(pub("2026-11-25T17:30:00Z", nap)).toBe("2026-11-25T20:30:00.000Z");
  });
  it("with quiet hours off it is plain addition", () => {
    const off = { ...NY, quiet_hours: { start: "00:00", end: "00:00" } };
    expect(pub("2026-11-26T04:00:00Z", off)).toBe("2026-11-26T05:00:00.000Z");
  });
});

describe("localDatePath", () => {
  it("uses the local date", () => {
    expect(localDatePath(at("2026-11-26T03:00:00Z"), "America/New_York")).toBe("2026/11/25");
    expect(localDatePath(at("2026-11-26T03:00:00Z"), "UTC")).toBe("2026/11/26");
  });
});
