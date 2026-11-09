// Done-check for node cloud-where. Passes only when core/docs/WHAT_LIVES_WHERE.md holds one
// linking table (thing | lives in | local access | cloud access | handover) with at most 12 rows,
// and the text names git, Drive for desktop, connectors and "never localhost".
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const MAX_ROWS = 12;
const HEADER = ["thing", "lives in", "local access", "cloud access", "handover"];
const NEEDED = ["git", "Drive for desktop", "connectors", "never localhost"];

const here = dirname(fileURLToPath(import.meta.url));
const fail = (m) => { console.error("FAIL: " + m); process.exit(1); };

let text;
try { text = readFileSync(join(here, "WHAT_LIVES_WHERE.md"), "utf8"); }
catch { fail("core/docs/WHAT_LIVES_WHERE.md missing"); }

const lines = text.split(/\r?\n/);
const cells = (l) => l.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim().toLowerCase());
const headerAt = lines.findIndex((l) => {
  const c = cells(l);
  return l.trim().startsWith("|") && c.length === HEADER.length && c.every((v, i) => v === HEADER[i]);
});
if (headerAt < 0) fail("no table with header: " + HEADER.join(" | "));

let rows = 0;
for (let i = headerAt + 1; i < lines.length; i++) {
  const l = lines[i].trim();
  if (!l.startsWith("|")) break;
  if (/^\|[\s:|-]+\|$/.test(l)) continue; // separator row
  rows++;
}
if (rows < 1) fail("table has no rows");
if (rows > MAX_ROWS) fail(`table has ${rows} rows, limit is ${MAX_ROWS}`);

const lower = text.toLowerCase();
for (const word of NEEDED) {
  if (!lower.includes(word.toLowerCase())) fail(`text does not name "${word}"`);
}

console.log(`OK: table with ${rows} rows; names git, Drive for desktop, connectors, never localhost`);
