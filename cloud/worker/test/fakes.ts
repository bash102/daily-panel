/** In-memory stand-ins for D1 (node:sqlite running the real migrations) and R2. */

import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { DatabaseSync } from "node:sqlite";
import { makeDeps, type Deps, type Settings } from "../src/deps";
import type { TwilioApi } from "../src/twilioApi";
import type { Logger, ObjectMeta, ObjectStore, PutOptions, SqlDb, SqlStatement, StoredObject } from "../src/types";

type Val = string | number | bigint | null | Uint8Array;

function norm(v: unknown): Val {
  if (v === undefined || v === null) return null;
  if (typeof v === "boolean") return v ? 1 : 0;
  if (typeof v === "string" || typeof v === "number" || typeof v === "bigint" || v instanceof Uint8Array) return v;
  throw new Error(`FakeD1: cannot bind ${typeof v}`);
}

class FakeStatement implements SqlStatement {
  constructor(
    private readonly db: DatabaseSync,
    private readonly sql: string,
    private readonly values: Val[] = [],
  ) {}

  bind(...values: unknown[]): SqlStatement {
    return new FakeStatement(this.db, this.sql, values.map(norm));
  }

  async first<T>(): Promise<T | null> {
    const r = this.db.prepare(this.sql).get(...this.values);
    return (r ? { ...r } : null) as T | null;
  }

  async all<T>(): Promise<{ results: T[] }> {
    return { results: this.db.prepare(this.sql).all(...this.values).map((r) => ({ ...r })) as T[] };
  }

  async run(): Promise<{ meta: { changes?: number } }> {
    const r = this.db.prepare(this.sql).run(...this.values);
    return { meta: { changes: Number(r.changes) } };
  }
}

export class FakeD1 implements SqlDb {
  readonly raw: DatabaseSync;

  constructor() {
    this.raw = new DatabaseSync(":memory:");
    const dir = fileURLToPath(new URL("../migrations/", import.meta.url));
    for (const f of readdirSync(dir).filter((n) => n.endsWith(".sql")).sort()) {
      this.raw.exec(readFileSync(join(dir, f), "utf8"));
    }
  }

  prepare(sql: string): SqlStatement {
    return new FakeStatement(this.raw, sql);
  }
}

export class FakeR2 implements ObjectStore {
  readonly objects = new Map<string, { data: Uint8Array; contentType?: string; custom?: Record<string, string> }>();

  async put(key: string, value: ArrayBuffer | Uint8Array | string, options?: PutOptions): Promise<unknown> {
    const data =
      typeof value === "string"
        ? new TextEncoder().encode(value)
        : value instanceof Uint8Array
          ? new Uint8Array(value)
          : new Uint8Array(value.slice(0));
    this.objects.set(key, { data, contentType: options?.httpMetadata?.contentType, custom: options?.customMetadata });
    return {};
  }

  async head(key: string): Promise<ObjectMeta | null> {
    const o = this.objects.get(key);
    return o ? { size: o.data.byteLength, httpMetadata: { contentType: o.contentType } } : null;
  }

  async get(key: string): Promise<StoredObject | null> {
    const o = this.objects.get(key);
    if (!o) return null;
    const bytes = o.data;
    return {
      size: bytes.byteLength,
      httpMetadata: { contentType: o.contentType },
      body: new Blob([new Uint8Array(bytes)]).stream(),
      arrayBuffer: async () => bytes.slice().buffer,
    };
  }

  text(key: string): string | undefined {
    const o = this.objects.get(key);
    return o ? new TextDecoder().decode(o.data) : undefined;
  }
}

export class FakeTwilio implements TwilioApi {
  sms: { to: string; body: string }[] = [];
  deleted: string[] = [];
  fetched: string[] = [];
  failSms = false;
  failFetch = false;
  audio = new Uint8Array([82, 73, 70, 70, 1, 2, 3, 4]);

  async sendSms(to: string, body: string): Promise<void> {
    if (this.failSms) throw new Error("sms down");
    this.sms.push({ to, body });
  }

  async fetchRecording(url: string): Promise<{ data: ArrayBuffer; contentType: string }> {
    if (this.failFetch) throw new Error("twilio down");
    this.fetched.push(url);
    return { data: this.audio.slice().buffer, contentType: "audio/x-wav" };
  }

  async deleteRecording(sid: string): Promise<void> {
    this.deleted.push(sid);
  }
}

export class FakeClock {
  constructor(public t: Date) {}
  now = (): Date => new Date(this.t.getTime());
  set(iso: string): void {
    this.t = new Date(iso);
  }
  advance(ms: number): void {
    this.t = new Date(this.t.getTime() + ms);
  }
}

export const quietLog: Logger & { lines: string[] } = {
  lines: [],
  info(m) {
    this.lines.push(m);
  },
  warn(m) {
    this.lines.push(m);
  },
  error(m) {
    this.lines.push(m);
  },
};

export const TEST_SETTINGS: Settings = {
  publicBaseUrl: "https://box.example.test",
  voice: "Polly.Joanna",
  grandmaPin: "4321",
  boxToken: "test-box-token",
  mediaSigningKey: "test-media-key",
  twilioAuthToken: "test-twilio-token",
  fromNumber: "+15550001111",
  grandmaNumber: "+15550002222",
  parentNumber: "+15550003333",
  pin: { globalLimit: 10 },
};

export interface World {
  deps: Deps;
  db: FakeD1;
  r2: FakeR2;
  twilio: FakeTwilio;
  clock: FakeClock;
}

export function makeWorld(start = "2026-11-25T15:00:00.000Z", settings: Partial<Settings> = {}): World {
  const db = new FakeD1();
  const r2 = new FakeR2();
  const twilio = new FakeTwilio();
  const clock = new FakeClock(new Date(start));
  const log: Logger = { info() {}, warn() {}, error() {} };
  const deps = makeDeps({
    db,
    archive: r2,
    twilio,
    twilioAccountSid: "AC" + "0".repeat(32),
    settings: { ...TEST_SETTINGS, ...settings },
    now: clock.now,
    log,
  });
  return { deps, db, r2, twilio, clock };
}
