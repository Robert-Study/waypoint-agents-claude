// Red tests for src/io.ts (does not exist yet). In-memory FsAdapter fake.
// Contract the library must meet:
//   FsAdapter { readFile, writeFile(path,data,{exclusive?}), rename, remove, exists, size, mkdir, listDir }
//   atomicWrite(fs, path, data, {maxBytes?}) writes `<path>.tmp-*` then renames; never leaves a partial target.
//   FileTooLarge error for data over the cap; DEFAULT_MAX_BYTES exported.
import { test } from "node:test";
import assert from "node:assert/strict";
import { atomicWrite, FileTooLarge, DEFAULT_MAX_BYTES } from "../src/io.ts";

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


test("atomicWrite writes the target and leaves no temp file", async () => {
  const fs = new MemFs();
  await atomicWrite(fs, "/d/a.json", '{"a":1}\n');
  assert.equal(await fs.readFile("/d/a.json"), '{"a":1}\n');
  assert.deepEqual([...fs.files.keys()], ["/d/a.json"]);
});

test("atomicWrite leaves the old file intact and no partial file when the write throws", async () => {
  const fs = new MemFs();
  await atomicWrite(fs, "/d/a.json", '{"v":1}\n');
  fs.failNextWrite = true;
  await assert.rejects(atomicWrite(fs, "/d/a.json", '{"v":2,"long":"xxxxxxxx"}\n'), /disk full/);
  assert.equal(await fs.readFile("/d/a.json"), '{"v":1}\n');
  assert.deepEqual([...fs.files.keys()], ["/d/a.json"], "temp file must be cleaned up");
});

test("atomicWrite on a new path leaves nothing behind when the write throws", async () => {
  const fs = new MemFs();
  fs.failNextWrite = true;
  await assert.rejects(atomicWrite(fs, "/d/new.json", '{"v":1}\n'), /disk full/);
  assert.equal(fs.files.size, 0);
});

test("a file over the size cap is refused with FileTooLarge and nothing is written", async () => {
  const fs = new MemFs();
  await assert.rejects(atomicWrite(fs, "/d/big.json", "x".repeat(101), { maxBytes: 100 }), FileTooLarge);
  assert.equal(fs.files.size, 0);
  assert.ok(Number.isInteger(DEFAULT_MAX_BYTES) && DEFAULT_MAX_BYTES > 0);
});

test("a file exactly at the size cap is accepted", async () => {
  const fs = new MemFs();
  await atomicWrite(fs, "/d/ok.json", "x".repeat(100), { maxBytes: 100 });
  assert.equal(fs.files.get("/d/ok.json")?.length, 100);
});
