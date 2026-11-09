// Entity store: one JSON file per entity, revision compare-and-swap (CONCURRENCY.md: Revisions).
import { errCode, type FsAdapter } from "./fs-adapter.ts";
import { atomicWrite, withLock, joinPath } from "./io.ts";

export type Entity = Record<string, any> & { rev?: number };

export class StaleRevision extends Error {
  code = "rev_conflict" as const;
  expected: number;
  current: Entity | null;
  constructor(path: string, expected: number, current: Entity | null) {
    super(`${path}: expected rev ${expected}, found ${current?.rev ?? 0}`);
    this.name = "StaleRevision";
    this.expected = expected;
    this.current = current;
  }
}

export interface WriteOptions {
  /** Rev the caller read; 0 means "create". Omitted means "whatever is there now" (single-writer files). */
  expectedRev?: number;
  /** When set, updated_by and updated_at are stamped, and a write that changes nothing else is skipped. */
  writer?: string;
}
export interface StoreOptions {
  now?: () => Date;
  maxBytes?: number;
  /** Take `<target>.lock` around the check and rename (default true). */
  useLock?: boolean;
}
export interface Store {
  read(rel: string): Promise<Entity>;
  tryRead(rel: string): Promise<Entity | null>;
  write(rel: string, entity: Entity, opts?: WriteOptions): Promise<Entity>;
  serialize(entity: unknown): string;
}

const serialize = (e: unknown) => JSON.stringify(e, null, 2) + "\n";
const strip = (e: Entity) => {
  const { rev: _r, updated_at: _a, updated_by: _b, ...rest } = e;
  return JSON.stringify(rest);
};

export function createStore(fs: FsAdapter, root: string, o: StoreOptions = {}): Store {
  const now = o.now ?? (() => new Date());
  const chains = new Map<string, Promise<unknown>>(); // in-process queue per path
  const abs = (rel: string) => joinPath(root, rel);

  async function tryRead(rel: string): Promise<Entity | null> {
    for (let i = 0; ; i++) {
      let text: string;
      try {
        text = await fs.readFile(abs(rel));
      } catch (e) {
        if (errCode(e) === "ENOENT") return null;
        throw e;
      }
      try {
        return JSON.parse(text) as Entity;
      } catch {
        if (i >= 4) throw Object.assign(new Error(`${rel}: unreadable JSON (read_torn)`), { code: "read_torn" });
        await new Promise((r) => setTimeout(r, 50));
      }
    }
  }

  async function doWrite(rel: string, entity: Entity, opts: WriteOptions): Promise<Entity> {
    const path = abs(rel);
    const critical = async (): Promise<Entity> => {
      const cur = await tryRead(rel);
      const curRev = cur?.rev ?? 0;
      if (opts.expectedRev !== undefined && opts.expectedRev !== curRev) throw new StaleRevision(rel, opts.expectedRev, cur);
      const next: Entity = { ...entity, rev: curRev + 1 };
      if (opts.writer) {
        next.updated_by = opts.writer;
        next.updated_at = now().toISOString();
        if (cur && strip(cur) === strip(next)) return cur;
      }
      await atomicWrite(fs, path, serialize(next), { maxBytes: o.maxBytes, writer: opts.writer });
      return next;
    };
    return o.useLock === false ? critical() : withLock(fs, path, critical, { owner: opts.writer, now });
  }

  return {
    read: async (rel) => {
      const e = await tryRead(rel);
      if (!e) throw Object.assign(new Error(`ENOENT ${rel}`), { code: "ENOENT" });
      return e;
    },
    tryRead,
    serialize,
    write(rel, entity, opts = {}) {
      const prev = chains.get(rel) ?? Promise.resolve();
      const run = prev.then(() => doWrite(rel, entity, opts));
      const tail = run.then(
        () => undefined,
        () => undefined,
      );
      chains.set(rel, tail);
      void tail.then(() => {
        if (chains.get(rel) === tail) chains.delete(rel);
      });
      return run;
    },
  };
}
