/**
 * Minimal structural interfaces for the Cloudflare bindings the worker uses.
 *
 * D1Database and R2Bucket satisfy these, and the tests back them with
 * in-memory fakes (node:sqlite for D1, a Map for R2), so every module
 * except src/index.ts runs without Cloudflare.
 */

export interface SqlStatement {
  bind(...values: unknown[]): SqlStatement;
  first<T = Record<string, unknown>>(): Promise<T | null>;
  all<T = Record<string, unknown>>(): Promise<{ results: T[] }>;
  run(): Promise<{ meta: { changes?: number } }>;
}

export interface SqlDb {
  prepare(sql: string): SqlStatement;
}

export interface ObjectMeta {
  size: number;
  httpMetadata?: { contentType?: string };
}

export interface StoredObject extends ObjectMeta {
  body: ReadableStream | null;
  arrayBuffer(): Promise<ArrayBuffer>;
}

export interface PutOptions {
  httpMetadata?: { contentType?: string };
  customMetadata?: Record<string, string>;
}

export interface ObjectStore {
  put(key: string, value: ArrayBuffer | Uint8Array | string, options?: PutOptions): Promise<unknown>;
  get(key: string): Promise<StoredObject | null>;
  head(key: string): Promise<ObjectMeta | null>;
}

/** Wall-clock source, injected so tests control time. */
export type Clock = () => Date;

/** Same shape as the global fetch; injected so tests never hit the network. */
export type FetchFn = (input: string | Request, init?: RequestInit) => Promise<Response>;

/** Logger; defaults to console. Never pass secrets or PIN digits to it. */
export interface Logger {
  info(msg: string, ...rest: unknown[]): void;
  warn(msg: string, ...rest: unknown[]): void;
  error(msg: string, ...rest: unknown[]): void;
}
