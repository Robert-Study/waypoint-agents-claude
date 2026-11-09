// Validates sample files against the ops-queue schemas. No dependencies (tiny draft-07 subset validator).
import { readFileSync, readdirSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..");
const load = (p) => JSON.parse(readFileSync(p, "utf8"));
const claimS = load(join(here, "claim.schema.json"));
const actionS = load(join(here, "action.schema.json"));
const dispS = load(join(here, "dispatch_request.schema.json"));

function resolve(rootS, ref) {
  if (!ref.startsWith("#/")) throw new Error("unsupported $ref " + ref);
  return ref.slice(2).split("/").reduce((o, k) => o[k], rootS);
}
const typeOk = (t, v) =>
  t === "object" ? !!v && typeof v === "object" && !Array.isArray(v)
  : t === "array" ? Array.isArray(v)
  : t === "string" ? typeof v === "string"
  : t === "integer" ? Number.isInteger(v)
  : t === "number" ? typeof v === "number"
  : t === "boolean" ? typeof v === "boolean"
  : t === "null" ? v === null : false;

export function validate(schema, v, rootS = schema, path = "$") {
  const errs = [];
  if (schema.$ref) return validate(resolve(rootS, schema.$ref), v, rootS, path);
  if (schema.type) {
    const ts = [].concat(schema.type);
    if (!ts.some((t) => typeOk(t, v))) return [`${path}: expected ${ts.join("|")}`];
  }
  if ("const" in schema && v !== schema.const) errs.push(`${path}: must equal ${JSON.stringify(schema.const)}`);
  if (schema.enum && !schema.enum.includes(v)) errs.push(`${path}: ${JSON.stringify(v)} not in [${schema.enum.join(", ")}]`);
  if (typeof v === "string") {
    if (schema.minLength && v.length < schema.minLength) errs.push(`${path}: too short`);
    if (schema.pattern && !new RegExp(schema.pattern).test(v)) errs.push(`${path}: does not match ${schema.pattern}`);
  }
  if (Array.isArray(v)) {
    if (schema.minItems && v.length < schema.minItems) errs.push(`${path}: too few items`);
    if (schema.items) v.forEach((x, i) => errs.push(...validate(schema.items, x, rootS, `${path}[${i}]`)));
  }
  if (v && typeof v === "object" && !Array.isArray(v)) {
    for (const k of schema.required || []) if (!(k in v)) errs.push(`${path}: missing ${k}`);
    const props = schema.properties || {};
    for (const k of Object.keys(v)) {
      if (schema.propertyNames && validate(schema.propertyNames, k, rootS, path).length)
        errs.push(`${path}: key "${k}" not allowed (keys, tokens and secrets never go in payloads)`);
      if (k in props) errs.push(...validate(props[k], v[k], rootS, `${path}.${k}`));
      else if (schema.additionalProperties === false) errs.push(`${path}: unknown key "${k}"`);
    }
  }
  for (const s of schema.allOf || []) errs.push(...validate(s, v, rootS, path));
  if (schema.if && validate(schema.if, v, rootS, path).length === 0 && schema.then)
    errs.push(...validate(schema.then, v, rootS, path));
  if (schema.not && validate(schema.not, v, rootS, path).length === 0) errs.push(`${path}: matches a forbidden shape`);
  return errs;
}

const files = (dir) =>
  existsSync(dir) ? readdirSync(dir).filter((f) => f.endsWith(".json")).map((f) => join(dir, f)) : [];
let failed = 0;
const fail = (m) => { failed++; console.log("FAIL " + m); };
const expectOk = (name, schema, doc, rootS) => {
  const e = validate(schema, doc, rootS);
  if (e.length) fail(`${name}: ${e.join("; ")}`);
};
const expectBad = (name, schema, doc, rootS) => {
  if (validate(schema, doc, rootS).length === 0) fail(`${name}: should have been rejected`);
};

const sf = join(root, "fixtures", "sample-folder");
const sets = [
  [files(join(sf, "claims")), claimS, claimS],
  [files(join(sf, "actions")), actionS, actionS],
  [files(join(sf, "dispatch", "requests")), dispS, dispS],
  [files(join(sf, "dispatch", "receipts")), { $ref: "#/$defs/receipt" }, dispS],
];
let n = 0;
for (const [fs, s, r] of sets) {
  if (!fs.length) fail("no sample files for a schema");
  for (const f of fs) { n++; expectOk(f, s, load(f), r); }
}

// Coverage: every action type has a sample; the types file matches the schema.
const types = actionS.$defs.actionTypes;
const seen = new Set(files(join(sf, "actions")).map((f) => load(f).type));
for (const t of types) if (!seen.has(t)) fail(`no sample action for type ${t}`);
const ts = readFileSync(join(root, "src", "types_ops_queue.ts"), "utf8");
for (const t of types) if (!ts.includes(`type: "${t}"`)) fail(`types_ops_queue.ts lacks action type ${t}`);
for (const m of actionS.$defs.runMode.enum) if (!ts.includes(`"${m}"`)) fail(`types_ops_queue.ts lacks run mode ${m}`);
for (const r of dispS.$defs.routes) if (!ts.includes(`"${r}"`)) fail(`types_ops_queue.ts lacks route ${r}`);
for (const t of new Set([...ts.matchAll(/ type: "([a-z_]+)";/g)].map((m) => m[1])))
  if (!types.includes(t)) fail(`types_ops_queue.ts has unknown action type ${t}`);
for (const t of actionS["x-owner-only-types"]) if (!ts.includes(`"${t}"`)) fail(`owner-only ${t} missing in TS`);

// Negative cases.
const base = load(files(join(sf, "actions")).find((f) => load(f).type === "run"));
expectBad("unknown action type", actionS, { ...base, type: "launch_missiles", payload: {} }, actionS);
expectBad("run unknown mode", actionS, { ...base, payload: { ...base.payload, mode: "turbo" } }, actionS);
expectBad("run missing mode", actionS, { ...base, payload: { node: "x" } }, actionS);
expectBad("payload carries a key", actionS, { ...base, payload: { ...base.payload, api_key: "x" } }, actionS);
expectBad("bad status", actionS, { ...base, status: "weird" }, actionS);
const acc = load(files(join(sf, "actions")).find((f) => load(f).type === "accept"));
expectBad("owner-only action by non-owner", actionS, { ...acc, by: "mod" }, actionS);
const req = load(files(join(sf, "dispatch", "requests"))[0]);
expectBad("bad route", dispS, { ...req, route: "carrier_pigeon" }, dispS);

console.log(failed ? `${failed} problem(s)` : `ok: ${n} sample files valid, ${types.length} action types covered, negative cases rejected`);
process.exit(failed ? 1 : 0);
