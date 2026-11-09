// Red tests for src/store.ts (does not exist yet).
// Contract: createStore(fs, root) -> { read(rel), write(rel, entity, {expectedRev?}), serialize(entity) }.
//   write bumps `rev` (create -> 1, else previous+1) and returns the stored entity.
//   a write whose expectedRev differs from the stored rev rejects with StaleRevision.
//   serialize is canonical: JSON.stringify(entity, null, 2) + "\n", key order preserved.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, dirname, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { createStore, StaleRevision } from "../src/store.ts";

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


const here = dirname(fileURLToPath(import.meta.url));
const sample = join(here, "..", "fixtures", "sample-folder");

function walk(dir: string, out: string[] = []): string[] {
  for (const n of readdirSync(dir)) {
    const p = join(dir, n);
    if (statSync(p).isDirectory()) walk(p, out);
    else if (n.endsWith(".json")) out.push(p);
  }
  return out;
}

const node = () => ({ schema: "waypoint.node.v1", id: "n1", title: "T", rev: 0, updated_at: "2026-11-07T12:00:00Z", updated_by: "test" });

test("create sets rev 1 and read returns it", async () => {
  const store = createStore(new MemFs(), "/root");
  const w = await store.write("projects/p/nodes/n1.json", node());
  assert.equal(w.rev, 1);
  assert.equal((await store.read("projects/p/nodes/n1.json")).rev, 1);
});

test("a write with the current rev succeeds and bumps rev", async () => {
  const store = createStore(new MemFs(), "/root");
  await store.write("n.json", node());
  const w = await store.write("n.json", { ...node(), title: "T2" }, { expectedRev: 1 });
  assert.equal(w.rev, 2);
});

test("a stale rev rejects with StaleRevision and leaves the file unchanged", async () => {
  const fs = new MemFs();
  const store = createStore(fs, "/root");
  await store.write("n.json", node());
  await store.write("n.json", { ...node(), title: "T2" }, { expectedRev: 1 });
  const before = [...fs.files.entries()];
  await assert.rejects(store.write("n.json", { ...node(), title: "T3" }, { expectedRev: 1 }), StaleRevision);
  assert.deepEqual([...fs.files.entries()], before);
});

test("two writers racing on one node produce exactly one success", async () => {
  const store = createStore(new MemFs(), "/root");
  await store.write("n.json", node());
  const results = await Promise.allSettled([
    store.write("n.json", { ...node(), title: "A" }, { expectedRev: 1 }),
    store.write("n.json", { ...node(), title: "B" }, { expectedRev: 1 }),
  ]);
  const ok = results.filter((r) => r.status === "fulfilled");
  const bad = results.filter((r) => r.status === "rejected") as PromiseRejectedResult[];
  assert.equal(ok.length, 1);
  assert.equal(bad.length, 1);
  assert.ok(bad[0].reason instanceof StaleRevision);
  assert.equal((await store.read("n.json")).rev, 2);
});

test("every sample-folder entity round-trips byte-stable", async () => {
  const files = walk(sample);
  assert.ok(files.length > 20, "sample folder should have entities");
  for (const f of files) {
    const rel = relative(sample, f).split("\\").join("/");
    const entity = JSON.parse(readFileSync(f, "utf8"));
    const fs = new MemFs();
    const s = createStore(fs, "/root");
    assert.equal(s.serialize(entity), JSON.stringify(entity, null, 2) + "\n", rel);
    const written = await s.write(rel, entity);
    const back = await s.read(rel);
    assert.deepEqual(back, written, rel);
    const bytes = fs.files.get("/root/" + rel);
    assert.equal(s.serialize(back), bytes, rel + " bytes drifted");
    assert.equal(s.serialize(JSON.parse(s.serialize(back))), bytes, rel);
  }
});
