// Done-check for data-schema-ops-sessions.
// Run from the repo root: node core/waypoint-data/schema/check_ops_sessions.mjs
// Validates every sample file under fixtures/sample-folder against its schema (dependency-free JSON Schema
// subset), proves the schemas reject a chat message without a provider and a snapshot without a revision,
// and checks that src/types_ops_sessions.ts declares every schema title with every property name.
import { readFileSync, readdirSync, existsSync, statSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const dataDir = dirname(here);
const folder = join(dataDir, "fixtures", "sample-folder");
const failures = [];
const fail = (m) => failures.push(m);

const readJson = (p) => JSON.parse(readFileSync(p, "utf8"));
const schemaOf = (name) => readJson(join(here, name));

// ---- minimal JSON Schema validator (subset the schemas here use) ----
function typeOk(t, v) {
  switch (t) {
    case "null": return v === null;
    case "string": return typeof v === "string";
    case "boolean": return typeof v === "boolean";
    case "number": return typeof v === "number" && Number.isFinite(v);
    case "integer": return Number.isInteger(v);
    case "array": return Array.isArray(v);
    case "object": return v !== null && typeof v === "object" && !Array.isArray(v);
    default: return false;
  }
}
const deepEq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

function validate(schema, value, root, path = "$") {
  const errs = [];
  if (schema === true || schema === undefined) return errs;
  if (schema.$ref) {
    const m = /^#\/\$defs\/(.+)$/.exec(schema.$ref);
    const target = m && root.$defs && root.$defs[m[1]];
    if (!target) return [`${path}: unresolved ${schema.$ref}`];
    return validate(target, value, root, path);
  }
  if (schema.type !== undefined) {
    const types = Array.isArray(schema.type) ? schema.type : [schema.type];
    if (!types.some((t) => typeOk(t, value))) {
      return [`${path}: expected ${types.join("|")}, got ${value === null ? "null" : Array.isArray(value) ? "array" : typeof value}`];
    }
  }
  if (schema.const !== undefined && !deepEq(schema.const, value)) errs.push(`${path}: expected const ${JSON.stringify(schema.const)}`);
  if (schema.enum && !schema.enum.some((e) => deepEq(e, value))) errs.push(`${path}: ${JSON.stringify(value)} not in enum ${JSON.stringify(schema.enum)}`);
  if (typeof value === "string") {
    if (schema.minLength !== undefined && value.length < schema.minLength) errs.push(`${path}: shorter than ${schema.minLength}`);
    if (schema.pattern && !new RegExp(schema.pattern).test(value)) errs.push(`${path}: does not match ${schema.pattern}`);
  }
  if (typeof value === "number" && schema.minimum !== undefined && value < schema.minimum) errs.push(`${path}: below ${schema.minimum}`);
  if (Array.isArray(value)) {
    if (schema.minItems !== undefined && value.length < schema.minItems) errs.push(`${path}: fewer than ${schema.minItems} items`);
    if (schema.items) value.forEach((v, i) => errs.push(...validate(schema.items, v, root, `${path}[${i}]`)));
  }
  if (typeOk("object", value)) {
    for (const k of schema.required || []) if (!(k in value)) errs.push(`${path}: missing required "${k}"`);
    const props = schema.properties || {};
    for (const [k, v] of Object.entries(value)) {
      if (k in props) errs.push(...validate(props[k], v, root, `${path}.${k}`));
      else if (schema.additionalProperties === false) errs.push(`${path}: unexpected property "${k}"`);
      else if (schema.additionalProperties && typeof schema.additionalProperties === "object") {
        errs.push(...validate(schema.additionalProperties, v, root, `${path}.${k}`));
      }
    }
  }
  if (schema.anyOf && !schema.anyOf.some((s) => validate(s, value, root, path).length === 0)) errs.push(`${path}: matches none of anyOf`);
  if (schema.oneOf) {
    const n = schema.oneOf.filter((s) => validate(s, value, root, path).length === 0).length;
    if (n !== 1) errs.push(`${path}: matches ${n} of oneOf (needs exactly 1)`);
  }
  return errs;
}

// ---- file discovery ----
const listFiles = (dir, pred) => (existsSync(dir) ? readdirSync(dir).filter((f) => pred(f, join(dir, f))).sort() : []);
const isFile = (p) => statSync(p).isFile();
const isDir = (p) => statSync(p).isDirectory();
const jsonIn = (dir) => listFiles(dir, (f, p) => f.endsWith(".json") && isFile(p)).map((f) => join(dir, f));
const subdirJson = (dir) => listFiles(dir, (_, p) => isDir(p)).flatMap((d) => jsonIn(join(dir, d)));

const groups = [
  { name: "sessions", schema: "session_event.schema.json", files: jsonIn(join(folder, "sessions")) },
  { name: "orchestrators", schema: "orchestrator.schema.json", files: jsonIn(join(folder, "orchestrators")) },
  { name: "brain", schema: "brain_index.schema.json", files: jsonIn(join(folder, "brain")) },
  { name: "chats", schema: "chat.schema.json", files: jsonIn(join(folder, "chats")) },
  { name: "snapshots", schema: "snapshot.schema.json", files: subdirJson(join(folder, "snapshots")) },
  { name: "briefs", schema: "brief.schema.json", files: jsonIn(join(folder, "briefs")) },
  { name: "learn", schema: "learn.schema.json", files: jsonIn(join(folder, "learn")) },
  { name: "plan_stages", schema: "plan_stage.schema.json", files: subdirJson(join(folder, "plan_stages")) },
];

let validated = 0;
for (const g of groups) {
  if (!g.files.length) { fail(`${g.name}: no sample files`); continue; }
  const schema = schemaOf(g.schema);
  for (const f of g.files) {
    let data;
    try { data = readJson(f); } catch (e) { fail(`${relative(folder, f)}: invalid JSON (${e.message})`); continue; }
    const errs = validate(schema, data, schema);
    validated++;
    for (const e of errs) fail(`${relative(folder, f)}: ${e}`);
  }
}

// brain/index.json must exist by name; learn needs all three kinds; plan_stages need all six stages
if (!existsSync(join(folder, "brain", "index.json"))) fail("brain/index.json missing");
{
  const kinds = new Set(groups.find((g) => g.name === "learn").files.map((f) => { try { return readJson(f).kind; } catch { return null; } }));
  for (const k of ["mastery", "cards", "log"]) if (!kinds.has(k)) fail(`learn: no sample of kind ${k}`);
  const stages = new Set(groups.find((g) => g.name === "plan_stages").files.map((f) => { try { return readJson(f).stage; } catch { return null; } }));
  for (const s of ["draft", "check_draft", "doctor", "research", "questions", "critique"]) if (!stages.has(s)) fail(`plan_stages: no sample of stage ${s}`);
}

// session cross-references: parent and fork ids and callbacks must point at sample sessions
{
  const sessions = groups[0].files.map((f) => { try { return readJson(f); } catch { return null; } }).filter(Boolean);
  const ids = new Set(sessions.map((s) => s.id));
  for (const s of sessions) {
    for (const k of ["parent", "fork_of"]) if (s[k] && !ids.has(s[k])) fail(`sessions/${s.id}: ${k} ${s[k]} is not a sample session`);
    for (const e of s.events || []) {
      if (e.callback && (!ids.has(e.callback.from_session) || !ids.has(e.callback.to_session))) fail(`sessions/${s.id}: callback points outside the samples`);
    }
  }
  if (!sessions.some((s) => s.parent)) fail("sessions: no sample with a parent");
  if (!sessions.some((s) => s.fork_of)) fail("sessions: no sample with fork_of");
  if (!sessions.some((s) => (s.events || []).some((e) => e.callback))) fail("sessions: no sample with a child-to-parent callback");
}

// chat messages.ndjson: every line is a ChatMessage
{
  const chatSchema = schemaOf("chat.schema.json");
  const msgSchema = { $ref: "#/$defs/message" };
  const chatsDir = join(folder, "chats");
  const dirs = listFiles(chatsDir, (_, p) => isDir(p));
  let lines = 0;
  const providers = new Set();
  for (const d of dirs) {
    const nd = join(chatsDir, d, "messages.ndjson");
    if (!existsSync(nd)) { fail(`chats/${d}: messages.ndjson missing`); continue; }
    if (!existsSync(join(chatsDir, `${d}.json`))) fail(`chats/${d}: no chats/${d}.json beside the folder`);
    readFileSync(nd, "utf8").split(/\r?\n/).forEach((ln, i) => {
      if (!ln.trim()) return;
      lines++;
      let m;
      try { m = JSON.parse(ln); } catch (e) { fail(`chats/${d}/messages.ndjson:${i + 1}: invalid JSON`); return; }
      providers.add(m.provider);
      for (const e of validate(msgSchema, m, chatSchema)) fail(`chats/${d}/messages.ndjson:${i + 1}: ${e}`);
    });
  }
  if (!lines) fail("chats: no messages.ndjson lines");
  for (const p of ["claude", "codex"]) if (!providers.has(p)) fail(`chats: no sample message with provider ${p}`);
  for (const f of groups.find((g) => g.name === "chats").files) {
    const c = readJson(f);
    const dChat = join(chatsDir, c.id);
    if (c.messages === undefined && !existsSync(dChat)) fail(`chats/${c.id}: neither inline messages nor a messages.ndjson folder`);
  }
}

// negative cases: the schemas must reject these
{
  const chat = schemaOf("chat.schema.json");
  const okMsg = { seq: 0, provider: "claude", role: "user", text: "SAMPLE", at: "2026-01-01T00:00:00Z" };
  const { provider, ...noProvider } = okMsg;
  if (validate({ $ref: "#/$defs/message" }, okMsg, chat).length) fail("negative setup: valid message rejected");
  if (!validate({ $ref: "#/$defs/message" }, noProvider, chat).length) fail("chat message without a provider was accepted");
  if (!validate({ $ref: "#/$defs/message" }, { ...okMsg, provider: "deepseek" }, chat).length) fail("chat message with provider deepseek was accepted");
  const inline = { schema: "waypoint.chat.v1", id: "x", provider: "codex", status: "open", messages: [noProvider] };
  if (!validate(chat, inline, chat).length) fail("chat file with an inline message lacking provider was accepted");

  const snap = schemaOf("snapshot.schema.json");
  const okSnap = { schema: "waypoint.state.v1", project: { id: "p", title: "P" }, revision: 1, nodes: [] };
  const { revision, ...noRev } = okSnap;
  if (validate(snap, okSnap, snap).length) fail("negative setup: valid snapshot rejected");
  if (!validate(snap, noRev, snap).length) fail("snapshot without a revision was accepted");
}

// types: every schema title and property name must appear in src/types_ops_sessions.ts
{
  const tsPath = join(dataDir, "src", "types_ops_sessions.ts");
  if (!existsSync(tsPath)) fail("src/types_ops_sessions.ts missing");
  else {
    const ts = readFileSync(tsPath, "utf8");
    const body = (name) => {
      const m = new RegExp(`export\\s+(?:interface|type)\\s+${name}\\b`).exec(ts);
      if (!m) return null;
      const rest = ts.slice(m.index + m[0].length);
      const end = rest.search(/\nexport\s/);
      return end < 0 ? rest : rest.slice(0, end);
    };
    const checkObj = (title, obj) => {
      const b = body(title);
      if (b === null) { fail(`types: ${title} not exported`); return; }
      for (const k of Object.keys(obj.properties || {})) {
        if (!new RegExp(`\\b${k}\\??\\s*:`).test(b)) fail(`types: ${title} lacks property ${k}`);
      }
    };
    for (const g of groups) {
      const s = schemaOf(g.schema);
      if (s.properties) checkObj(s.title, s);
      for (const d of Object.values(s.$defs || {})) if (d.title && d.properties) checkObj(d.title, d);
      if (!s.properties && body(s.title) === null) fail(`types: ${s.title} not exported`);
    }
  }
}

if (failures.length) {
  console.error(`check_ops_sessions: FAIL (${failures.length})`);
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log(`check_ops_sessions: OK, ${validated} sample files validated, negative cases rejected, types match`);
