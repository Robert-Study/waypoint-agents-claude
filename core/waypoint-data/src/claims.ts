// Node claims: claims/<node>.json (waypoint.claim.v1) created exclusively, with a heartbeat and a stale window
// (CONCURRENCY.md: Claims). A short `<node>.json.lock` file serialises the read-decide-write on one machine.
import type { Claim, ClaimOwner } from "./types_ops_queue.ts";
import { errCode, type FsAdapter } from "./fs-adapter.ts";
import { atomicWrite, joinPath } from "./io.ts";

export const STALE_MS = 5 * 60_000;
export const CLOUD_STALE_MS = 10 * 60_000;

export interface ClaimRequest {
  project: string;
  node: string;
  owner: ClaimOwner;
  location: string;
  token: string;
  claimed_by?: string;
  environment?: string;
}
export type ClaimResult = { ok: true; claim: Claim } | { ok: false; reason: "held"; holder?: Claim };
export interface ClaimsOptions {
  staleMs?: number;
  /** Stale window for `cloud:` owners; never shorter than staleMs. */
  cloudStaleMs?: number;
  now?: () => Date;
}
export interface Claims {
  claim(req: ClaimRequest): Promise<ClaimResult>;
  heartbeat(project: string, node: string, token: string): Promise<boolean>;
  release(project: string, node: string, token: string): Promise<boolean>;
  read(project: string, node: string): Promise<Claim | null>;
}

const text = (c: Claim) => JSON.stringify(c, null, 2) + "\n";

export function createClaims(fs: FsAdapter, root: string, o: ClaimsOptions = {}): Claims {
  const staleMs = o.staleMs ?? STALE_MS;
  const cloudMs = Math.max(o.cloudStaleMs ?? CLOUD_STALE_MS, staleMs);
  const now = o.now ?? (() => new Date());
  const file = (node: string) => joinPath(root, `claims/${node}.json`);

  async function load(node: string): Promise<Claim | null> {
    try {
      return JSON.parse(await fs.readFile(file(node))) as Claim;
    } catch (e) {
      if (errCode(e) === "ENOENT" || e instanceof SyntaxError) return null;
      throw e;
    }
  }
  const isStale = (c: Claim) => {
    const t = Date.parse(c.heartbeat_at);
    const win = c.owner.startsWith("cloud:") ? cloudMs : staleMs;
    return !Number.isFinite(t) || now().getTime() - t > win;
  };

  /** Exclusive create of the lock; a lock older than staleMs is taken over. Returns false when held. */
  async function lock(node: string): Promise<boolean> {
    const path = file(node) + ".lock";
    await fs.mkdir(joinPath(root, "claims"));
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        await fs.writeFile(path, JSON.stringify({ at: now().toISOString() }), { exclusive: true });
        return true;
      } catch (e) {
        if (errCode(e) !== "EEXIST") throw e;
      }
      let age = 0; // an unreadable lock counts as fresh: its writer may still be mid-write
      try {
        age = now().getTime() - Date.parse((JSON.parse(await fs.readFile(path)) as { at: string }).at);
      } catch {
        /* torn or just released */
      }
      if (!(age > staleMs)) return false;
      await fs.remove(path).catch(() => {});
    }
    return false;
  }

  async function locked<T>(node: string, held: T, fn: () => Promise<T>): Promise<T> {
    if (!(await lock(node))) return held;
    try {
      return await fn();
    } finally {
      await fs.remove(file(node) + ".lock").catch(() => {});
    }
  }

  return {
    read: (_project, node) => load(node),
    claim: (req) =>
      locked<ClaimResult>(req.node, { ok: false, reason: "held" }, async () => {
        const cur = await load(req.node);
        if (cur && cur.token !== req.token && !isStale(cur)) return { ok: false, reason: "held", holder: cur };
        const t = now().toISOString();
        const claim: Claim = {
          schema: "waypoint.claim.v1",
          project: req.project,
          node: req.node,
          owner: req.owner,
          location: req.location,
          token: req.token,
          heartbeat_at: t,
          since: cur && cur.token === req.token ? (cur.since ?? t) : t,
          ...(req.claimed_by ? { claimed_by: req.claimed_by } : {}),
          ...(req.environment ? { environment: req.environment } : {}),
        };
        if (cur) await atomicWrite(fs, file(req.node), text(claim), { writer: req.owner });
        else await fs.writeFile(file(req.node), text(claim), { exclusive: true });
        return { ok: true, claim };
      }),
    heartbeat: (_project, node, token) =>
      locked(node, false, async () => {
        const cur = await load(node);
        if (!cur || cur.token !== token) return false;
        await atomicWrite(fs, file(node), text({ ...cur, heartbeat_at: now().toISOString() }));
        return true;
      }),
    release: (_project, node, token) =>
      locked(node, false, async () => {
        const cur = await load(node);
        if (!cur || cur.token !== token) return false;
        await fs.remove(file(node));
        return true;
      }),
  };
}
