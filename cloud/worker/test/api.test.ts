import { beforeEach, describe, expect, it } from "vitest";
import { handleRequest } from "../src/app";
import { canGet, canPut, isSafeKey, parseControlKey } from "../src/keys";
import { makeWorld, type World } from "./fakes";
import { apiRequest } from "./helpers";

let w: World;
const call = (req: Request) => handleRequest(w.deps, req);
const putJson = (key: string, body: unknown, token?: string | null) =>
  call(apiRequest("PUT", `/api/objects/${key}`, JSON.stringify(body), { token, contentType: "application/json" }));

beforeEach(() => {
  w = makeWorld();
});

describe("auth", () => {
  it("requires the bearer token on every API route", async () => {
    for (const [m, p] of [
      ["GET", "/api/health"],
      ["GET", "/api/grandma/messages"],
      ["GET", "/api/objects/prints/a.png"],
      ["PUT", "/api/objects/prints/a.png"],
    ] as const) {
      expect((await call(apiRequest(m, p, m === "PUT" ? "x" : undefined, { token: null }))).status).toBe(401);
      expect((await call(apiRequest(m, p, m === "PUT" ? "x" : undefined, { token: "wrong" }))).status).toBe(401);
      expect((await call(apiRequest(m, p, m === "PUT" ? "x" : undefined, { token: "test-box-token-x" }))).status).toBe(401);
    }
    const ok = await call(apiRequest("GET", "/api/health"));
    expect(ok.status).toBe(200);
    expect(await ok.json()).toEqual({ ok: true, now: "2026-11-25T15:00:00.000Z" });
  });

  it("rejects a non-Bearer scheme", async () => {
    const r = new Request("https://box.example.test/api/health", { headers: { authorization: "Basic test-box-token" } });
    expect((await call(r)).status).toBe(401);
  });

  it("refuses everything when BOX_TOKEN is unset", async () => {
    w = makeWorld(undefined, { boxToken: "" });
    expect((await call(apiRequest("GET", "/api/health", undefined, { token: "" }))).status).toBe(401);
  });
});

describe("key validation", () => {
  it("accepts media-relative keys under allowed prefixes", () => {
    for (const k of [
      "recordings/zozo/2026/11/25/123456789-abcd1234.wav",
      "prints/2026/11/25/101500123-letter.png",
      "mood/2026/11/25/x.wav",
      "content/index/abc.json",
      "control/config.json",
    ]) {
      expect(canPut(k), k).toBe(true);
    }
    expect(canGet("recordings/grandma/2026/11/25/RE1.wav")).toBe(true);
  });

  it("rejects traversal, absolute paths and unknown prefixes", () => {
    for (const k of [
      "",
      "/prints/a.png",
      "prints/../control/config.json",
      "prints/./a.png",
      "prints//a.png",
      "prints/a.png/",
      "prints\\a.png",
      "prints/a b.png",
      "prints/a%2e.png",
      "secret/a.png",
      "recordings/grandma/2026/a.wav", // the Pi may not write grandma's slot
      "x".repeat(600),
    ]) {
      expect(canPut(k), k).toBe(false);
    }
    expect(isSafeKey("a/../b")).toBe(false);
    expect(canGet("../etc/passwd")).toBe(false);
  });

  it("returns 400 over HTTP for bad keys, including encoded traversal", async () => {
    expect((await call(apiRequest("PUT", "/api/objects/prints%2F..%2F..%2Fx", "x"))).status).toBe(400);
    expect((await call(apiRequest("PUT", "/api/objects/other/x.png", "x"))).status).toBe(400);
    expect((await call(apiRequest("GET", "/api/objects/%E0%A4%A", undefined))).status).toBe(400);
    expect((await call(apiRequest("PUT", "/api/objects/recordings/grandma/2026/11/25/x.wav", "x"))).status).toBe(400);
  });

  it("parses control keys", () => {
    expect(parseControlKey("control/config.json")).toEqual({ kind: "config" });
    expect(parseControlKey("control/zozo/abc123.json")).toEqual({ kind: "zozo_new", recId: "abc123" });
    expect(parseControlKey("control/zozo/abc123-withhold.json")).toEqual({ kind: "zozo_withhold", recId: "abc123" });
    expect(parseControlKey("control/other.json")).toEqual({ kind: "other" });
  });
});

describe("objects", () => {
  it("PUT stores raw bytes with the content type and is idempotent; GET downloads", async () => {
    const bytes = new Uint8Array([0, 1, 2, 250]);
    const key = "recordings/zozo/2026/11/25/101500123-abcd1234.wav";
    for (let i = 0; i < 2; i++) {
      const r = await call(apiRequest("PUT", `/api/objects/${key}`, bytes, { contentType: "audio/wav" }));
      expect(r.status).toBe(200);
      expect(await r.json()).toEqual({ ok: true, key, bytes: 4 });
    }
    expect(w.r2.objects.size).toBe(1);
    expect(w.r2.objects.get(key)?.contentType).toBe("audio/wav");
    const g = await call(apiRequest("GET", `/api/objects/${key}`));
    expect(g.status).toBe(200);
    expect(g.headers.get("content-type")).toBe("audio/wav");
    expect(new Uint8Array(await g.arrayBuffer())).toEqual(bytes);
    expect((await call(apiRequest("GET", "/api/objects/prints/missing.png"))).status).toBe(404);
  });
});

describe("control commands", () => {
  const NEW = {
    action: "new",
    rec_id: "r1",
    media_key: "recordings/zozo/2026/11/25/101500123-abcd1234.wav",
    recorded_at: "2026-11-25T15:15:00.123456+00:00",
    duration_s: 4.5,
  };

  it("config is validated, stored in R2 and applied", async () => {
    const cfg = { quiet_hours: { start: "20:00", end: "06:30" }, timezone: "Europe/Berlin", review_delay_s: 1800 };
    const r = await putJson("control/config.json", cfg);
    expect(r.status).toBe(200);
    expect(await r.json()).toMatchObject({ ok: true, applied: true, result: "config" });
    expect(JSON.parse(w.r2.text("control/config.json")!)).toEqual(cfg);
    expect(await w.deps.repo.getConfig()).toEqual(cfg);
    const g = await call(apiRequest("GET", "/api/config"));
    expect(await g.json()).toEqual(cfg);
  });

  it("bad config is refused with 422 and not stored", async () => {
    for (const bad of [
      { quiet_hours: { start: "25:00", end: "06:00" }, timezone: "UTC", review_delay_s: 1 },
      { quiet_hours: { start: "20:00", end: "06:00" }, timezone: "Mars/Olympus", review_delay_s: 1 },
      { quiet_hours: { start: "20:00", end: "06:00" }, timezone: "UTC", review_delay_s: -1 },
      { quiet_hours: { start: "20:00", end: "06:00" }, timezone: "UTC", review_delay_s: 1.5 },
      [],
    ]) {
      expect((await putJson("control/config.json", bad)).status).toBe(422);
    }
    const r = await call(apiRequest("PUT", "/api/objects/control/config.json", "{not json", { contentType: "application/json" }));
    expect(r.status).toBe(422);
    expect(w.r2.objects.size).toBe(0);
  });

  it("new registers a pending message; repeating it is idempotent", async () => {
    const r = await putJson("control/zozo/r1.json", NEW);
    expect(await r.json()).toMatchObject({ applied: true, result: "registered" });
    expect(await (await putJson("control/zozo/r1.json", NEW)).json()).toMatchObject({ result: "exists" });
    const row = await w.deps.repo.getZozo("r1");
    expect(row).toMatchObject({ status: "pending", recorded_at: "2026-11-25T15:15:00.123Z", duration_s: 4.5 });
    expect(w.r2.objects.has("control/zozo/r1.json")).toBe(true);
  });

  it("new is validated", async () => {
    expect((await putJson("control/zozo/r1.json", { ...NEW, rec_id: "other" })).status).toBe(422);
    expect((await putJson("control/zozo/r1.json", { ...NEW, action: "withhold" })).status).toBe(422);
    expect((await putJson("control/zozo/r1.json", { ...NEW, media_key: "prints/a.png" })).status).toBe(422);
    expect((await putJson("control/zozo/r1.json", { ...NEW, media_key: "recordings/zozo/../x.wav" })).status).toBe(422);
    expect((await putJson("control/zozo/r1.json", { ...NEW, recorded_at: "2026-11-25T15:15:00" })).status).toBe(422);
    expect((await putJson("control/zozo/r1-withhold.json", { ...NEW })).status).toBe(422);
  });

  it("withhold works before or after new", async () => {
    await putJson("control/zozo/r1.json", NEW);
    const r = await putJson("control/zozo/r1-withhold.json", { action: "withhold", rec_id: "r1" });
    expect(await r.json()).toMatchObject({ applied: true, result: "withheld" });
    expect((await w.deps.repo.getZozo("r1"))?.status).toBe("withheld");

    await putJson("control/zozo/r2-withhold.json", { action: "withhold", rec_id: "r2" });
    const late = await putJson("control/zozo/r2.json", { ...NEW, rec_id: "r2" });
    expect(await late.json()).toMatchObject({ result: "withheld" });
    expect(await w.deps.repo.getZozo("r2")).toMatchObject({ status: "withheld", media_key: NEW.media_key });
  });

  it("a newer message supersedes an older pending one; an older one arrives superseded", async () => {
    await putJson("control/zozo/a.json", { ...NEW, rec_id: "a", recorded_at: "2026-11-25T10:00:00Z" });
    await putJson("control/zozo/b.json", { ...NEW, rec_id: "b", recorded_at: "2026-11-25T11:00:00Z" });
    expect((await w.deps.repo.getZozo("a"))?.status).toBe("superseded");
    expect((await w.deps.repo.getZozo("b"))?.status).toBe("pending");
    const r = await putJson("control/zozo/c.json", { ...NEW, rec_id: "c", recorded_at: "2026-11-25T09:00:00Z" });
    expect(await r.json()).toMatchObject({ result: "superseded" });
  });

  it("other control keys are stored but not applied", async () => {
    const r = await putJson("control/notes.json", { hello: 1 });
    expect(await r.json()).toMatchObject({ ok: true, applied: false });
  });
});

describe("grandma messages", () => {
  beforeEach(async () => {
    for (const [id, t] of [
      ["RE2", "2026-11-25T12:00:00.000Z"],
      ["RE1", "2026-11-24T12:00:00.000Z"],
      ["RE3", "2026-11-25T13:00:00.000Z"],
    ] as const) {
      await w.deps.repo.insertGrandma(
        { id, key: `recordings/grandma/x/${id}.wav`, recorded_at: t, duration_s: 10, call_sid: "CA" },
        t,
      );
    }
  });

  it("lists all, oldest first (newest last)", async () => {
    const r = await call(apiRequest("GET", "/api/grandma/messages"));
    const list = (await r.json()) as { id: string }[];
    expect(list.map((m) => m.id)).toEqual(["RE1", "RE2", "RE3"]);
    expect(list[0]).toEqual({ id: "RE1", key: "recordings/grandma/x/RE1.wav", recorded_at: "2026-11-24T12:00:00.000Z", duration_s: 10 });
  });

  it("filters by since (inclusive), in any ISO offset", async () => {
    const r = await call(apiRequest("GET", "/api/grandma/messages?since=" + encodeURIComponent("2026-11-25T07:00:00-05:00")));
    expect(((await r.json()) as { id: string }[]).map((m) => m.id)).toEqual(["RE2", "RE3"]);
    expect((await call(apiRequest("GET", "/api/grandma/messages?since=yesterday"))).status).toBe(400);
  });
});
