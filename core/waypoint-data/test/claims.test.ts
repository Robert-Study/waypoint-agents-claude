// Red tests for src/claims.ts (does not exist yet).
// Contract: createClaims(fs, root, { staleMs, now }) -> { claim(req), heartbeat(project,node,token), release(project,node,token), read(project,node) }
//   claim({project,node,owner,location,token}) resolves { ok:true, claim } or { ok:false, reason:"held" }.
//   Claim files live at <root>/claims/<node>.json (waypoint.claim.v1) and are created exclusively;
//   the lock file is <root>/claims/<node>.json.lock holding {"at": ISO}.
//   A claim whose heartbeat_at is older than staleMs may be taken over; a fresh one never.
//   A lock file older than staleMs is taken over as well.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createClaims, STALE_MS } from "../src/claims.ts";

// In-memory FsAdapter fake (inlined: this node owns only the three tests and expect-red.mjs).
export class MemFs {
  files = new Map<string, string>();
  failNextWrite = false;
  async readFile(p: string): Promise<string> {
    const v = this.files.get(p);
    if (v === undefined) throw Object.assign(new Error("ENOENT " + p), { code: "ENOENT" });
    return v;
  }
  async writeFile(p: string, data: string, opts?: { exclusive?: boolean }): Promise<void> {
    await Promise.resolve();
    if (opts?.exclusive && this.files.has(p)) throw Object.assign(new Error("EEXIST " + p), { code: "EEXIST" });
    if (this.failNextWrite) {
      this.failNextWrite = false;
      this.files.set(p, data.slice(0, Math.floor(data.length / 2))); // partial bytes land, then throw
      throw new Error("disk full");
    }
    this.files.set(p, data);
  }
  async rename(a: string, b: string): Promise<void> {
    await Promise.resolve();
    const v = await this.readFile(a);
    this.files.delete(a);
    this.files.set(b, v);
  }
  async remove(p: string): Promise<void> {
    this.files.delete(p);
  }
  async exists(p: string): Promise<boolean> {
    return this.files.has(p);
  }
  async size(p: string): Promise<number> {
    return Buffer.byteLength(await this.readFile(p));
  }
  async mkdir(_p: string): Promise<void> {}
  async listDir(dir: string): Promise<string[]> {
    return [...this.files.keys()].filter((k) => k.startsWith(dir + "/")).map((k) => k.slice(dir.length + 1));
  }
}


function clock(start = Date.parse("2026-11-07T12:00:00Z")) {
  const c = { t: start, now: () => new Date(c.t) };
  return c;
}
const req = (token: string, owner = "runner") => ({ project: "p", node: "n1", owner, location: "/wt", token });

test("a claim writes a valid waypoint.claim.v1 file", async () => {
  const c = clock();
  const claims = createClaims(new MemFs(), "/root", { staleMs: 60_000, now: c.now });
  const r = await claims.claim(req("tok-aaaaaaaa"));
  assert.equal(r.ok, true);
  const stored = await claims.read("p", "n1");
  assert.equal(stored?.schema, "waypoint.claim.v1");
  assert.equal(stored?.heartbeat_at, "2026-11-07T12:00:00.000Z");
});

test("two claimants race on one node and exactly one wins", async () => {
  const c = clock();
  const claims = createClaims(new MemFs(), "/root", { staleMs: 60_000, now: c.now });
  const [a, b] = await Promise.all([claims.claim(req("tok-aaaaaaaa", "mod")), claims.claim(req("tok-bbbbbbbb", "runner"))]);
  assert.equal([a, b].filter((r) => r.ok).length, 1);
  const loser = [a, b].find((r) => !r.ok) as { reason: string };
  assert.equal(loser.reason, "held");
});

test("a claim with a fresh heartbeat is not reclaimable", async () => {
  const c = clock();
  const claims = createClaims(new MemFs(), "/root", { staleMs: 60_000, now: c.now });
  await claims.claim(req("tok-aaaaaaaa"));
  c.t += 59_000;
  assert.equal((await claims.claim(req("tok-bbbbbbbb"))).ok, false);
});

test("a heartbeat keeps the claim alive past the original timeout", async () => {
  const c = clock();
  const claims = createClaims(new MemFs(), "/root", { staleMs: 60_000, now: c.now });
  await claims.claim(req("tok-aaaaaaaa"));
  c.t += 50_000;
  await claims.heartbeat("p", "n1", "tok-aaaaaaaa");
  c.t += 50_000;
  assert.equal((await claims.claim(req("tok-bbbbbbbb"))).ok, false);
});

test("a claim with a stale heartbeat is reclaimable only after the timeout", async () => {
  const c = clock();
  const claims = createClaims(new MemFs(), "/root", { staleMs: 60_000, now: c.now });
  await claims.claim(req("tok-aaaaaaaa"));
  c.t += 60_000;
  assert.equal((await claims.claim(req("tok-bbbbbbbb"))).ok, false, "exactly at the window is still held");
  c.t += 1;
  const r = await claims.claim(req("tok-bbbbbbbb"));
  assert.equal(r.ok, true);
  assert.equal((await claims.read("p", "n1"))?.token, "tok-bbbbbbbb");
});

test("a stale lock file is taken over, a fresh one is respected", async () => {
  const fs = new MemFs();
  const c = clock();
  const claims = createClaims(fs, "/root", { staleMs: 60_000, now: c.now });
  await fs.writeFile("/root/claims/n1.json.lock", JSON.stringify({ at: c.now().toISOString() }));
  assert.equal((await claims.claim(req("tok-aaaaaaaa"))).ok, false, "fresh lock held by someone else");
  c.t += 61_000;
  assert.equal((await claims.claim(req("tok-aaaaaaaa"))).ok, true, "stale lock taken over");
});

test("only the token holder can release", async () => {
  const c = clock();
  const claims = createClaims(new MemFs(), "/root", { staleMs: 60_000, now: c.now });
  await claims.claim(req("tok-aaaaaaaa"));
  assert.equal(await claims.release("p", "n1", "tok-wrong-000"), false);
  assert.equal(await claims.release("p", "n1", "tok-aaaaaaaa"), true);
  assert.equal(await claims.read("p", "n1"), null);
  assert.ok(STALE_MS > 0);
});
