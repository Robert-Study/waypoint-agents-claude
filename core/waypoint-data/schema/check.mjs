// Waypoint data check: parses every schema, validates the golden sample folder, and proves the schemas
// reject the mistakes that matter (missing rev, unknown todo category, deps without crosses, absolute cwd,
// stored float). Dependency free: a small JSON Schema subset validator lives here.
import { readFileSync, readdirSync, statSync, existsSync } from "node:fs";
import { join, dirname, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const sample = join(here, "..", "fixtures", "sample-folder");

// ---------------------------------------------------------------- schemas
const schemas = {};
const problems = [];
for (const f of readdirSync(here).filter((n) => n.endsWith(".schema.json")).sort()) {
  try {
    schemas[f] = JSON.parse(readFileSync(join(here, f), "utf8"));
  } catch (e) {
    problems.push(`${f}: does not parse (${e.message})`);
  }
}
const CORE = ["project", "version", "family", "node", "run", "todo", "question", "receipt", "event", "check"];
for (const n of CORE) if (!schemas[`${n}.schema.json`]) problems.push(`missing schema ${n}.schema.json`);

// ---------------------------------------------------------------- validator (JSON Schema subset)
function typeOf(v) {
  if (v === null) return "null";
  if (Array.isArray(v)) return "array";
  if (Number.isInteger(v)) return "integer";
  return typeof v;
}
function resolve(ref, file, rootSchema) {
  const [f, frag] = ref.split("#");
  const doc = f ? schemas[f] : rootSchema;
  if (!doc) throw new Error(`unresolved $ref ${ref}`);
  let node = doc;
  for (const part of (frag || "").split("/").filter(Boolean)) node = node?.[part];
  if (!node) throw new Error(`unresolved $ref ${ref}`);
  return [node, f || file, doc];
}
function validate(value, schema, file, rootSchema, path, errs) {
  if (schema === true || schema === undefined) return;
  if (schema === false) return void errs.push(`${path}: not allowed`);
  if (schema.$ref) {
    const [node, f, doc] = resolve(schema.$ref, file, rootSchema);
    return validate(value, node, f, doc, path, errs);
  }
  if (schema.allOf) for (const s of schema.allOf) validate(value, s, file, rootSchema, path, errs);
  if (schema.if) {
    const probe = [];
    validate(value, schema.if, file, rootSchema, path, probe);
    if (probe.length === 0 && schema.then) validate(value, schema.then, file, rootSchema, path, errs);
    if (probe.length > 0 && schema.else) validate(value, schema.else, file, rootSchema, path, errs);
  }
  if (schema.anyOf || schema.oneOf) {
    const alts = schema.anyOf || schema.oneOf;
    let ok = 0;
    for (const s of alts) {
      const probe = [];
      validate(value, s, file, rootSchema, path, probe);
      if (probe.length === 0) ok++;
    }
    if (schema.anyOf ? ok === 0 : ok !== 1) errs.push(`${path}: matches ${ok} of the ${schema.anyOf ? "anyOf" : "oneOf"} alternatives`);
  }
  const t = typeOf(value);
  if (schema.type) {
    const want = Array.isArray(schema.type) ? schema.type : [schema.type];
    const okType = want.some((w) => w === t || (w === "number" && t === "integer"));
    if (!okType) return void errs.push(`${path}: expected ${want.join("|")}, got ${t}`);
  }
  if ("const" in schema && JSON.stringify(schema.const) !== JSON.stringify(value)) errs.push(`${path}: must be ${JSON.stringify(schema.const)}`);
  if (schema.enum && !schema.enum.some((e) => JSON.stringify(e) === JSON.stringify(value))) errs.push(`${path}: ${JSON.stringify(value)} not in [${schema.enum.join(", ")}]`);
  if (t === "string") {
    if (schema.minLength !== undefined && value.length < schema.minLength) errs.push(`${path}: shorter than ${schema.minLength}`);
    if (schema.maxLength !== undefined && value.length > schema.maxLength) errs.push(`${path}: longer than ${schema.maxLength}`);
    if (schema.pattern && !new RegExp(schema.pattern).test(value)) errs.push(`${path}: does not match ${schema.pattern}`);
  }
  if ((t === "integer" || t === "number") && schema.minimum !== undefined && value < schema.minimum) errs.push(`${path}: below ${schema.minimum}`);
  if (t === "array") {
    if (schema.minItems !== undefined && value.length < schema.minItems) errs.push(`${path}: fewer than ${schema.minItems} items`);
    if (schema.maxItems !== undefined && value.length > schema.maxItems) errs.push(`${path}: more than ${schema.maxItems} items`);
    if (schema.items) value.forEach((v, i) => validate(v, schema.items, file, rootSchema, `${path}[${i}]`, errs));
  }
  if (t === "object") {
    for (const r of schema.required || []) if (!(r in value)) errs.push(`${path}: missing required "${r}"`);
    const props = schema.properties || {};
    for (const [k, v] of Object.entries(value)) {
      if (k in props) validate(v, props[k], file, rootSchema, `${path}.${k}`, errs);
      else if (schema.additionalProperties === false) errs.push(`${path}: unknown field "${k}"`);
      else if (schema.additionalProperties && typeof schema.additionalProperties === "object") validate(v, schema.additionalProperties, file, rootSchema, `${path}.${k}`, errs);
    }
  }
}
function check(entity, value) {
  const file = `${entity}.schema.json`;
  const errs = [];
  if (!schemas[file]) return [`no schema ${file}`];
  validate(value, schemas[file], file, schemas[file], "$", errs);
  return errs;
}

// ---------------------------------------------------------------- sample folder
function walk(dir) {
  if (!existsSync(dir)) return [];
  return readdirSync(dir).flatMap((n) => {
    const p = join(dir, n);
    return statSync(p).isDirectory() ? walk(p) : [p];
  });
}
const loaded = {}; // entity -> [{rel, value}]
const add = (entity, rel, value) => ((loaded[entity] ||= []).push({ rel, value }));
for (const p of walk(sample)) {
  const rel = relative(sample, p).split(sep).join("/");
  const parts = rel.split("/");
  let entity = null;
  if (parts[0] === "projects" && parts.length === 3 && parts[2] === "project.json") entity = "project";
  else if (parts[0] === "projects" && parts.length === 4 && parts[2] === "nodes") entity = "node";
  else if (parts[0] === "versions" && parts.length === 3) entity = "version";
  else if (parts[0] === "families" && parts.length === 2) entity = "family";
  else if (parts[0] === "runs" && parts.length === 2) entity = "run";
  else if (parts[0] === "runs" && parts.length === 3 && parts[2] === "events.ndjson") entity = "event";
  else if (parts[0] === "todos" && parts.length === 2) entity = "todo";
  else if (parts[0] === "questions" && parts.length === 2) entity = "question";
  else if (parts[0] === "receipts" && parts.length === 2) entity = "receipt";
  else continue; // other subfolders belong to other nodes
  try {
    if (entity === "event") {
      readFileSync(p, "utf8").split("\n").filter((l) => l.trim()).forEach((l, i) => add("event", `${rel}:${i + 1}`, JSON.parse(l)));
    } else add(entity, rel, JSON.parse(readFileSync(p, "utf8")));
  } catch (e) {
    problems.push(`${rel}: does not parse (${e.message})`);
  }
}
for (const n of CORE.filter((c) => c !== "check")) if (!(loaded[n] || []).length) problems.push(`sample folder has no ${n} entity`);

for (const [entity, items] of Object.entries(loaded)) {
  for (const { rel, value } of items) {
    for (const e of check(entity, value)) problems.push(`${rel}: ${e}`);
    if (entity === "node" && value.check) for (const e of check("check", value.check)) problems.push(`${rel}: check ${e}`);
  }
}
// cross references
const projectIds = new Set((loaded.project || []).map((x) => x.value.id));
for (const { rel, value } of loaded.node || []) if (!projectIds.has(value.project)) problems.push(`${rel}: project ${value.project} has no project.json`);
for (const { rel, value } of loaded.todo || []) if (value.project && !projectIds.has(value.project)) problems.push(`${rel}: unknown project ${value.project}`);

// ---------------------------------------------------------------- negative tests (the schemas must reject these)
const first = (e) => structuredClone(loaded[e][0].value);
const mutable = ["project", "node", "family", "run", "todo", "question", "version"];
const negatives = [];
if (problems.length === 0) {
  for (const e of mutable) {
    const v = first(e);
    delete v.rev;
    negatives.push([`${e} without rev`, e, v]);
    const u = first(e);
    delete u.updated_by;
    negatives.push([`${e} without updated_by`, e, u]);
  }
  const t1 = first("todo");
  t1.category = "chores";
  negatives.push(["todo with unknown category", "todo", t1]);
  const t2 = first("todo");
  t2.category = "review";
  t2.subtype = "own";
  negatives.push(["todo with a subtype of another category", "todo", t2]);
  const n1 = first("node");
  n1.deps = [{ id: "A1" }];
  negatives.push(["node whose dep lacks crosses", "node", n1]);
  const n2 = first("node");
  n2.deps = [{ id: "A1", crosses: "" }];
  negatives.push(["node whose dep has empty crosses", "node", n2]);
  const n3 = first("node");
  n3.float = 3;
  negatives.push(["node storing a computed float", "node", n3]);
  const p1 = first("project");
  p1.cwd = { machine: "pc", rel: "~\\x" };
  negatives.push(["project with an absolute drive path cwd", "project", p1]);
  const p2 = first("project");
  p2.cwd = "~\\x";
  negatives.push(["project with a plain-string cwd", "project", p2]);
  const r1 = first("receipt");
  r1.status = "great";
  negatives.push(["receipt with unknown status", "receipt", r1]);
}
let rejected = 0;
for (const [label, entity, value] of negatives) {
  if (check(entity, value).length === 0) problems.push(`negative test failed: ${label} was accepted`);
  else rejected++;
}

// ---------------------------------------------------------------- report
const counts = Object.entries(loaded).map(([k, v]) => `${k} ${v.length}`).join(", ");
if (problems.length) {
  console.error(`waypoint-data check FAILED (${problems.length} problems)`);
  for (const p of problems) console.error(` - ${p}`);
  process.exit(1);
}
console.log(`waypoint-data check OK: ${Object.keys(schemas).length} schemas parse; sample folder valid (${counts}); ${rejected}/${negatives.length} negative cases rejected`);
