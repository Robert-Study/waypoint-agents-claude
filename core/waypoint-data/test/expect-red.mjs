// expect-red.mjs: node core/waypoint-data/test/expect-red.mjs
// Runs the io, store and claims tests (each in its own node process) and exits 0 only when
// every outcome is acceptable:
//   - all tests pass (stays valid after data-lib turns them green), or
//   - the only failure is the missing library module for that test (src/io.ts, src/store.ts, src/claims.ts), or
//   - the failures are assertion failures (ERR_ASSERTION).
// Anything else exits nonzero: SyntaxError, TypeScript parse error, unresolved import of anything
// but the missing library module, a missing/renamed test file, TypeError/ReferenceError and so on.
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const pkg = resolve(here, "..");
const SUITES = [
  { test: "io.test.ts", lib: "io.ts" },
  { test: "store.test.ts", lib: "store.ts" },
  { test: "claims.test.ts", lib: "claims.ts" },
];

const norm = (s) => s.replace(/\\\\/g, "\\").replace(/\\/g, "/").toLowerCase();

function classify(suite, out, status) {
  const text = out;
  if (status === 0) return { ok: true, kind: "green" };

  if (/SyntaxError/.test(text) && !/does not provide an export named/.test(text)) {
    return { ok: false, why: "SyntaxError in test or library" };
  }
  if (/ERR_INVALID_TYPESCRIPT_SYNTAX|ERR_UNSUPPORTED_TYPESCRIPT_SYNTAX|ERR_UNKNOWN_FILE_EXTENSION/.test(text)) {
    return { ok: false, why: "TypeScript parse/transform error" };
  }
  if (/does not provide an export named/.test(text)) {
    return { ok: false, why: "test imports a name the library does not export" };
  }

  const missing = [...text.matchAll(/Cannot find (?:module|package) '([^']+)'/g)].map((m) => norm(m[1]));
  const wantedLib = norm(join(pkg, "src", suite.lib));
  if (missing.length) {
    const other = missing.filter((m) => m !== wantedLib);
    if (other.length) return { ok: false, why: "unresolved import other than the library module: " + other[0] };
    if (!/ERR_MODULE_NOT_FOUND/.test(text)) return { ok: false, why: "missing module without ERR_MODULE_NOT_FOUND" };
    return { ok: true, kind: "red: missing module src/" + suite.lib };
  }
  if (/ERR_MODULE_NOT_FOUND|ERR_PACKAGE_PATH_NOT_EXPORTED/.test(text)) {
    return { ok: false, why: "module resolution error not naming the library module" };
  }

  // Tests ran and some failed: every failure must be an assertion.
  const codes = [...text.matchAll(/\bcode: '([A-Z_]+)'/g)].map((m) => m[1]);
  const badNames = [...text.matchAll(/\b(TypeError|ReferenceError|RangeError|EvalError|URIError)\b/g)].map((m) => m[1]);
  if (badNames.length) return { ok: false, why: "non-assertion failure: " + badNames[0] };
  const unexpected = codes.filter((c) => !["ERR_ASSERTION", "ERR_TEST_FAILURE"].includes(c));
  if (unexpected.length) return { ok: false, why: "non-assertion failure code: " + unexpected[0] };
  if (!codes.includes("ERR_ASSERTION")) return { ok: false, why: "failed without an assertion or a missing module" };
  return { ok: true, kind: "red: assertion failures" };
}

let failed = false;
for (const suite of SUITES) {
  const file = join(here, suite.test);
  if (!existsSync(file)) {
    console.log(`FAIL ${suite.test}: test file missing`);
    failed = true;
    continue;
  }
  const r = spawnSync(process.execPath, [file], { cwd: pkg, encoding: "utf8", timeout: 120_000 });
  const out = (r.stdout || "") + "\n" + (r.stderr || "");
  if (r.error) {
    console.log(`FAIL ${suite.test}: could not run (${r.error.message})`);
    failed = true;
    continue;
  }
  const v = classify(suite, out, r.status);
  if (v.ok) console.log(`ok   ${suite.test}: ${v.kind}`);
  else {
    failed = true;
    console.log(`FAIL ${suite.test}: ${v.why}`);
    console.log(out.split("\n").slice(0, 15).map((l) => "  | " + l).join("\n"));
  }
}
console.log(failed ? "expect-red: FAIL" : "expect-red: OK");
process.exit(failed ? 1 : 0);
