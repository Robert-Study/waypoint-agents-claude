import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { resolveDataRoot, toLocalPath, toCwdRef, parseMachineFile, machineFilePath, PathError } from "../src/paths.ts";

const here = dirname(fileURLToPath(import.meta.url));
const fx = JSON.parse(readFileSync(join(here, "..", "fixtures", "paths.json"), "utf8"));

for (const c of fx.resolve_data_root) {
  test(`resolveDataRoot: ${c.name}`, () => {
    const got = resolveDataRoot({ env: c.env, home: c.home, readFile: (p: string) => (p in c.fs ? c.fs[p] : null), platform: "win32" });
    assert.deepEqual(got, c.expect);
  });
}

for (const c of fx.to_local_path) {
  test(`toLocalPath: ${c.name}`, () => {
    const run = () => toLocalPath(c.cwd, c.machines, "win32");
    if (c.expect_error) assert.throws(run, (e: any) => e instanceof PathError && e.code === c.expect_error);
    else assert.equal(run(), c.expect);
  });
}

for (const c of fx.to_cwd_ref) {
  test(`toCwdRef: ${c.name}`, () => {
    const run = () => toCwdRef(c.machine, c.code_root, c.abs, "win32");
    if (c.expect_error) assert.throws(run, (e: any) => e instanceof PathError && e.code === c.expect_error);
    else assert.deepEqual(run(), c.expect);
  });
}

test("a stored ref never contains a drive letter", () => {
  const ref = toCwdRef("desktop", "C:\\code", "C:\\code\\a\\b", "win32");
  assert.doesNotMatch(JSON.stringify(ref), /[A-Za-z]:/);
});

test("machines file lives under the data root and parses to a code root", () => {
  assert.equal(machineFilePath("D:\\Data Root", "desktop", "win32"), "D:\\Data Root\\machines\\desktop.json");
  assert.equal(parseMachineFile('{"id":"desktop","code_root":"C:\\\\code"}'), "C:\\code");
  assert.throws(() => parseMachineFile("{}"), (e: any) => e instanceof PathError && e.code === "bad_machine_file");
});

test("posix platform works too", () => {
  assert.equal(toLocalPath({ machine: "m", rel: "a b/c" }, { m: "/home/x/code" }, "posix"), "/home/x/code/a b/c");
});
