import { describe, expect, it } from "vitest";
import worker, { depsFromEnv } from "../src/index";
import { FakeD1, FakeR2 } from "./fakes";

function env(extra: Record<string, unknown> = {}) {
  return {
    ARCHIVE: new FakeR2(),
    DB: new FakeD1(),
    TWILIO_ACCOUNT_SID: "AC" + "0".repeat(32),
    TWILIO_AUTH_TOKEN: "tok",
    TWILIO_FROM_NUMBER: "+15550001111",
    GRANDMA_NUMBER: "+15550002222",
    PARENT_NUMBER: "+15550003333",
    GRANDMA_PIN: "1234",
    BOX_TOKEN: "box",
    MEDIA_SIGNING_KEY: "media",
    PUBLIC_BASE_URL: "https://box.example.test",
    ...extra,
  } as never;
}

describe("worker entry", () => {
  it("maps env to deps", () => {
    const d = depsFromEnv(env({ GLOBAL_PIN_FAILURE_LIMIT: "5" }));
    expect(d.settings.voice).toBe("Polly.Joanna");
    expect(d.pin.opts.globalLimit).toBe(5);
    expect(d.pin.isExempt("+15550002222")).toBe(true);
    expect(depsFromEnv(env({ GLOBAL_PIN_FAILURE_LIMIT: "0" })).pin.opts.globalLimit).toBe(0);
    expect(depsFromEnv(env({ GLOBAL_PIN_FAILURE_LIMIT: "" })).pin.opts.globalLimit).toBe(10);
    expect(depsFromEnv(env({ GLOBAL_PIN_FAILURE_LIMIT: "x" })).pin.opts.globalLimit).toBe(10);
    expect(depsFromEnv(env({ TWILIO_VOICE: "alice" })).settings.voice).toBe("alice");
  });

  it("serves the API and 404s elsewhere", async () => {
    const e = env();
    const ok = await worker.fetch(new Request("https://box.example.test/api/health", { headers: { authorization: "Bearer box" } }), e);
    expect(ok.status).toBe(200);
    expect((await worker.fetch(new Request("https://box.example.test/"), e)).status).toBe(404);
  });

  it("runs the cron through waitUntil", async () => {
    const waits: Promise<unknown>[] = [];
    await worker.scheduled({} as never, env(), { waitUntil: (p: Promise<unknown>) => waits.push(p) } as never);
    expect(waits).toHaveLength(1);
    await Promise.all(waits);
  });
});
