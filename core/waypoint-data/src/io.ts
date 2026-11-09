// Atomic whole-file writes and short file locks (core/docs/data/CONCURRENCY.md: Atomic write, Locks).
import { errCode, type FsAdapter } from "./fs-adapter.ts";

export const DEFAULT_MAX_BYTES = 1024 * 1024;

export class FileTooLarge extends Error {
  code = "file_too_large" as const;
  bytes: number;
  maxBytes: number;
  constructor(path: string, bytes: number, maxBytes: number) {
    super(`${path}: ${bytes} bytes exceeds the ${maxBytes} byte limit`);
    this.name = "FileTooLarge";
    this.bytes = bytes;
    this.maxBytes = maxBytes;
  }
}
export class WriteLocked extends Error {
  code = "write_locked" as const;
  constructor(path: string) {
    super(`${path}: could not replace the target (locked by another process)`);
    this.name = "WriteLocked";
  }
}
export class LockBusy extends Error {
  code = "lock_busy" as const;
  constructor(path: string) {
    super(`${path}: lock is held by another writer`);
    this.name = "LockBusy";
  }
}

export const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));
const byteLength = (s: string) => new TextEncoder().encode(s).length;
const rand8 = () => Math.random().toString(36).slice(2, 10).padEnd(8, "0");
export function splitPath(p: string): { dir: string; name: string } {
  const i = Math.max(p.lastIndexOf("/"), p.lastIndexOf("\\"));
  return { dir: i < 0 ? "" : p.slice(0, i), name: p.slice(i + 1) };
}
export const joinPath = (a: string, b: string) => (a === "" ? b : a.replace(/[\\/]+$/, "") + "/" + b.replace(/^[\\/]+/, ""));

const RENAME_RETRY = ["EPERM", "EACCES", "EBUSY"];
const BACKOFF = [10, 20, 40, 80, 160, 250, 250, 250];

export interface AtomicWriteOptions {
  maxBytes?: number;
  writer?: string;
  /** Test hook: replaces the real delay between rename retries. */
  sleep?: (ms: number) => Promise<void>;
}

/** Write `<dir>/.<name>.<writer>.<rand>.tmp` exclusively, then rename over the target. Never leaves a partial target. */
export async function atomicWrite(fs: FsAdapter, path: string, data: string, opts: AtomicWriteOptions = {}): Promise<void> {
  const max = opts.maxBytes ?? DEFAULT_MAX_BYTES;
  const bytes = byteLength(data);
  if (bytes > max) throw new FileTooLarge(path, bytes, max);
  const { dir, name } = splitPath(path);
  if (dir) await fs.mkdir(dir);
  const tmp = joinPath(dir, `.${name}.${opts.writer ?? "w"}.${rand8()}.tmp`);
  try {
    await fs.writeFile(tmp, data, { exclusive: true });
    const wait = opts.sleep ?? sleep;
    for (let i = 0; ; i++) {
      try {
        await fs.rename(tmp, path);
        return;
      } catch (e) {
        const c = errCode(e);
        if (c && RENAME_RETRY.includes(c)) {
          if (i >= BACKOFF.length) throw new WriteLocked(path);
          await wait(BACKOFF[i] + Math.floor(Math.random() * 20));
          continue;
        }
        throw e;
      }
    }
  } catch (e) {
    await fs.remove(tmp).catch(() => {});
    throw e;
  }
}

export interface LockOptions {
  owner?: string;
  staleMs?: number;
  waitMs?: number;
  now?: () => Date;
  sleep?: (ms: number) => Promise<void>;
}

/** Run fn while holding `<target>.lock` (exclusive create). Stale locks (older than staleMs, default 30 s) are taken over. */
export async function withLock<T>(fs: FsAdapter, target: string, fn: () => Promise<T>, o: LockOptions = {}): Promise<T> {
  const lock = target + ".lock";
  const now = o.now ?? (() => new Date());
  const stale = o.staleMs ?? 30_000;
  const wait = o.sleep ?? sleep;
  const deadline = Date.now() + (o.waitMs ?? 5_000);
  const token = rand8() + rand8();
  const body = () => JSON.stringify({ schema: "waypoint.lock.v1", owner: o.owner ?? "w", token, acquired_at: now().toISOString(), heartbeat_at: now().toISOString() });
  const { dir } = splitPath(target);
  if (dir) await fs.mkdir(dir);
  let delay = 25;
  for (;;) {
    try {
      await fs.writeFile(lock, body(), { exclusive: true });
      break;
    } catch (e) {
      if (errCode(e) !== "EEXIST") throw e;
    }
    let held = true;
    try {
      const cur = JSON.parse(await fs.readFile(lock)) as { heartbeat_at?: string; acquired_at?: string; at?: string };
      const t = Date.parse(cur.heartbeat_at ?? cur.acquired_at ?? cur.at ?? "");
      if (Number.isFinite(t) && now().getTime() - t > stale) held = false;
    } catch (e) {
      if (errCode(e) === "ENOENT") held = false; // released between our two calls
    }
    if (!held) {
      await fs.remove(lock).catch(() => {});
      continue;
    }
    if (Date.now() >= deadline) throw new LockBusy(lock);
    await wait(delay + Math.floor(Math.random() * 10));
    delay = Math.min(delay * 2, 500);
  }
  try {
    return await fn();
  } finally {
    try {
      const cur = JSON.parse(await fs.readFile(lock)) as { token?: string };
      if (cur.token === token) await fs.remove(lock);
    } catch {
      /* lock already gone or broken: nothing to release */
    }
  }
}
