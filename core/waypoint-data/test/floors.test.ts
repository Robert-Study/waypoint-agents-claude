import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { decide, classifyReading } from "../src/floors.ts";

const fx = JSON.parse(readFileSync(new URL("../fixtures/floors.json", import.meta.url), "utf8"));
const now = new Date(fx.now);

function settingsFor(floors: Record<string, number | null>) {
  const pools: Record<string, any> = {};
  for (const [k, v] of Object.entries(floors)) pools[k] = { min_percent: v, set_by: v === null ? null : "owner", set_at: null };
  return { ...fx.settings_base, floors: { pools } };
}

for (const c of fx.cases) {
  test(c.name, () => {
    const r = decide(c.route, settingsFor(c.floors), c.reading, now);
    assert.deepEqual(r, c.expect);
  });
}

test("decide does not mutate its inputs and is repeatable", () => {
  const c = fx.cases[1];
  const s = settingsFor(c.floors);
  const before = JSON.stringify([c.route, s, c.reading]);
  const a = decide(c.route, s, c.reading, now);
  const b = decide(c.route, s, c.reading, now);
  assert.deepEqual(a, b);
  assert.equal(JSON.stringify([c.route, s, c.reading]), before);
});

test("classifyReading treats unparseable timestamps as UNKNOWN", () => {
  assert.equal(
    classifyReading({ pool: "p", remaining_percent: 50, resets_at: null, read_at: "garbage", source: "x" }, 30, now),
    "UNKNOWN",
  );
});

test("floors.ts holds no numeric floor constant and no I/O or clock", () => {
  const src = readFileSync(new URL("../src/floors.ts", import.meta.url), "utf8");
  const code = src
    .split("\n")
    .filter((l) => !l.trim().startsWith("//") && !l.trim().startsWith("*") && !l.trim().startsWith("/*"))
    .join("\n");
  const stripped = code.replace(/"[^"]*"/g, '""').replace(/\b[01]\b/g, "").replace(/\b60000\b/g, "");
  assert.equal(/\d/.test(stripped), false, "no numeric constants in floors.ts");
  assert.equal(/Date\.now|new Date\(\)|readFile|fetch\(|process\.|Math\.random/.test(code), false, "decide must be pure");
});
