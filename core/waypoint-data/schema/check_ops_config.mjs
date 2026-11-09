// Check for data-schema-ops-config: validates every sample file against its schema,
// fails on any numeric floor, on any key/token in settings, and on a floor default in a schema.
// Dependency free: a small JSON Schema subset validator (type, enum, const, required, properties,
// additionalProperties, items, minimum, maximum, minLength, pattern).
import { readFileSync, readdirSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..");
const sample = join(root, "fixtures", "sample-folder");
const load = (p) => JSON.parse(readFileSync(p, "utf8"));
const errors = [];
const fail = (m) => errors.push(m);

const typeOf = (v) => (v === null ? "null" : Array.isArray(v) ? "array" : Number.isInteger(v) ? "integer" : typeof v);
const typeOk = (t, v) => (t === "number" ? typeof v === "number" : t === "integer" ? Number.isInteger(v) : typeOf(v) === t);

export function validate(schema, value, path = "$") {
  const out = [];
  if (schema.const !== undefined && JSON.stringify(schema.const) !== JSON.stringify(value)) out.push(`${path}: must equal ${JSON.stringify(schema.const)}`);
  if (schema.type) {
    const ts = Array.isArray(schema.type) ? schema.type : [schema.type];
    if (!ts.some((t) => typeOk(t, value))) { out.push(`${path}: expected ${ts.join("|")}, got ${typeOf(value)}`); return out; }
  }
  if (schema.enum && !schema.enum.some((e) => e === value)) out.push(`${path}: ${JSON.stringify(value)} not in enum`);
  if (typeof value === "number") {
    if (schema.minimum !== undefined && value < schema.minimum) out.push(`${path}: below minimum ${schema.minimum}`);
    if (schema.maximum !== undefined && value > schema.maximum) out.push(`${path}: above maximum ${schema.maximum}`);
  }
  if (typeof value === "string") {
    if (schema.minLength !== undefined && value.length < schema.minLength) out.push(`${path}: too short`);
    if (schema.pattern && !new RegExp(schema.pattern).test(value)) out.push(`${path}: does not match ${schema.pattern}`);
  }
  if (Array.isArray(value) && schema.items) value.forEach((v, i) => out.push(...validate(schema.items, v, `${path}[${i}]`)));
  if (typeOf(value) === "object") {
    for (const r of schema.required || []) if (!(r in value)) out.push(`${path}: missing required "${r}"`);
    const props = schema.properties || {};
    for (const [k, v] of Object.entries(value)) {
      if (k in props) out.push(...validate(props[k], v, `${path}.${k}`));
      else if (schema.additionalProperties === false) out.push(`${path}: unexpected property "${k}"`);
      else if (schema.additionalProperties && typeof schema.additionalProperties === "object") out.push(...validate(schema.additionalProperties, v, `${path}.${k}`));
    }
  }
  return out;
}

const schemaFiles = ["agent", "floor", "settings", "usage", "schedule", "health"];
const schemas = {};
for (const n of schemaFiles) {
  const p = join(here, `${n}.schema.json`);
  if (!existsSync(p)) { fail(`missing schema ${n}.schema.json`); continue; }
  schemas[n] = load(p);
}

const walk = (o, f, path = "$") => {
  if (o && typeof o === "object") for (const [k, v] of Object.entries(o)) { f(k, v, `${path}.${k}`); walk(v, f, `${path}.${k}`); }
};
// No default anywhere in a schema may carry a floor value: floors are never hard-coded.
for (const [n, s] of Object.entries(schemas)) {
  walk(s, (k, v, path) => { if (k === "default" && /min_percent|floor/i.test(path)) fail(`${n} schema: floor default at ${path}`); });
}

const listJson = (d) => (existsSync(d) ? readdirSync(d).filter((f) => f.endsWith(".json")).sort() : []);
const checkDir = (dir, schemaName, minCount) => {
  const files = listJson(join(sample, dir));
  if (files.length < minCount) fail(`${dir}/: expected at least ${minCount} sample file(s), found ${files.length}`);
  for (const f of files) {
    if (!schemas[schemaName]) continue;
    const data = load(join(sample, dir, f));
    for (const e of validate(schemas[schemaName], data)) fail(`${dir}/${f} ${e}`);
  }
};
checkDir("agents", "agent", 4);
checkDir("usage", "usage", 2);
checkDir("schedules", "schedule", 1);
checkDir("health", "health", 2);

{
  const kinds = new Set(listJson(join(sample, "agents")).map((f) => load(join(sample, "agents", f)).kind));
  for (const k of ["agent", "route", "model", "addon"]) if (!kinds.has(k)) fail(`agents/: no sample of kind ${k}`);
}

// Floors: every floor unset (null), no numeric value.
{
  const p = join(sample, "settings", "floors.json");
  if (!existsSync(p)) fail("settings/floors.json missing");
  else {
    const floors = load(p);
    if (schemas.floor) for (const e of validate(schemas.floor, floors)) fail(`settings/floors.json ${e}`);
    const pools = Object.entries(floors.pools || {});
    if (pools.length === 0) fail("settings/floors.json: no pools listed");
    for (const [pool, v] of pools) if (v.min_percent !== null) fail(`settings/floors.json: pool ${pool} has a numeric floor (${v.min_percent}); every sample floor must be null`);
    walk(floors, (k, v, path) => { if (/min_percent|floor/i.test(k) && typeof v === "number") fail(`floors.json: numeric ${path}`); });
  }
}

// Settings: valid, and no key or token anywhere.
const secretKey = /(^|_)(api_?key|secret|token|password|credential|authorization)s?$/i;
const secretVal = /(AIza[0-9A-Za-z_-]{20,}|sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abp]-|Bearer\s+[A-Za-z0-9._-]{12,}|eyJ[A-Za-z0-9_-]{20,}\.)/;
{
  const p = join(sample, "settings", "settings.json");
  if (!existsSync(p)) fail("settings/settings.json missing");
  else {
    const s = load(p);
    if (schemas.settings) for (const e of validate(schemas.settings, s)) fail(`settings/settings.json ${e}`);
    walk(s, (k, v, path) => {
      if (secretKey.test(k)) fail(`settings.json: secret-like key ${path}`);
      if (typeof v === "string" && secretVal.test(v)) fail(`settings.json: secret-like value at ${path}`);
    });
    if (s.python !== "~/workspace/second-repo/.venv/Scripts/python.exe") fail("settings.json: python must be the pinned venv interpreter");
    if (s.usage_fresh_minutes !== 30) fail("settings.json: usage_fresh_minutes sample must be the agent default 30");
  }
}
for (const d of ["agents", "usage", "schedules", "health", "settings"]) for (const f of listJson(join(sample, d))) {
  walk(load(join(sample, d, f)), (k, v, path) => { if (typeof v === "string" && secretVal.test(v)) fail(`${d}/${f}: secret-like value at ${path}`); });
}

// Self test: negative cases must be rejected.
if (schemas.floor) {
  const bad = { pools: { codex: { min_percent: 150, set_by: "owner", set_at: "2026-11-07T00:00:00Z" } } };
  if (validate(schemas.floor, bad).length === 0) fail("self-test: floor of 150 accepted");
  const noSetBy = { pools: { codex: { min_percent: null } } };
  if (validate(schemas.floor, noSetBy).length === 0) fail("self-test: floor without set_by/set_at accepted");
}
if (schemas.settings && existsSync(join(sample, "settings", "settings.json"))) {
  const s = load(join(sample, "settings", "settings.json"));
  if (validate(schemas.settings, { ...s, api_key: "x" }).length === 0) fail("self-test: settings with an api_key property accepted");
}

// Types file mentions every property of every schema.
{
  const p = join(root, "src", "types_ops_config.ts");
  if (!existsSync(p)) fail("src/types_ops_config.ts missing");
  else {
    const t = readFileSync(p, "utf8");
    const names = (s) => {
      const out = new Set();
      walk(s, (k, v) => { if (k === "properties" && v && typeof v === "object") Object.keys(v).forEach((x) => out.add(x)); });
      return out;
    };
    for (const [n, s] of Object.entries(schemas)) for (const prop of names(s)) if (!new RegExp(`\\b${prop}\\b`).test(t)) fail(`types_ops_config.ts: schema ${n} property "${prop}" not present`);
    if (/min_percent\s*[:?]\s*number\s*;/.test(t)) fail("types_ops_config.ts: min_percent must be number | null");
  }
}

if (errors.length) { console.error(`FAIL: ${errors.length} problem(s)`); for (const e of errors) console.error(" - " + e); process.exit(1); }
console.log(`OK: ${schemaFiles.length} schemas, sample folder valid, all floors unset, no secrets in settings`);
