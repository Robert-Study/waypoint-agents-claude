// Done-check for node cloud-01. Passes only when SPIKE_RESULT.md records PASS/FAIL/UNKNOWN
// with evidence for each item, names a RUN_URL, and that GitHub Actions run succeeded.
import { readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const fail = (m) => { console.error("FAIL: " + m); process.exit(1); };
let text;
try { text = readFileSync(join(here, "SPIKE_RESULT.md"), "utf8"); }
catch { fail("SPIKE_RESULT.md missing"); }

for (const item of ["auth", "secrets", "agent CLIs", "usage limits", "drive reachability"]) {
  const re = new RegExp(String.raw`^\s*[-*]?\s*\**` + item + String.raw`\**\s*[:\-]\s*\**(PASS|FAIL|UNKNOWN)\**\s*[-:—]\s*(\S.{9,})$`, "im");
  if (!re.test(text)) fail(`no "<${item}>: PASS|FAIL|UNKNOWN - evidence" line`);
}
const m = text.match(/^RUN_URL: https:\/\/github\.com\/([\w.-]+)\/([\w.-]+)\/actions\/runs\/(\d+)\s*$/m);
if (!m) fail("no RUN_URL line");
const [, owner, repo, id] = m;
if (!/prepared, not performed/i.test(text)) fail('credential proposal not marked "prepared, not performed"');
const r = spawnSync("gh", ["run", "view", id, "--repo", `${owner}/${repo}`, "--json", "conclusion"], { encoding: "utf8", shell: process.platform === "win32" });
if (r.status !== 0) fail("gh run view failed: " + (r.stderr || "").trim());
let c; try { c = JSON.parse(r.stdout).conclusion; } catch { fail("bad gh output"); }
if (c !== "success") fail("run conclusion is " + c);
console.log(`OK: 5 items recorded, run ${id} conclusion=success`);
