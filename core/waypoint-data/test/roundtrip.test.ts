// Round-trip tests for the 1.1 SQLite -> files migration. They only ever see a COPY path that the test builds in a
// temp folder; the live DB is never opened (migrate() refuses a live-looking path, asserted below).
import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync, mkdirSync, existsSync, statSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { migrate, sha256File, defaultPython, pyDumps, isLiveDbPath, MANIFEST_REL } from "../migrate/migrate_1_1.ts";
import { roundtripCheck, liveReport, readManifest } from "../migrate/roundtrip_check.ts";

const here = path.dirname(fileURLToPath(import.meta.url));
const python = defaultPython();
const report = JSON.parse(readFileSync(path.join(here, "../spike/spike_report.json"), "utf8"));

const BUILD = String.raw`
import sqlite3, sys, json
spec = json.load(open(sys.argv[1], encoding="utf-8"))
con = sqlite3.connect(spec["db"])
if spec.get("wal"): con.execute("PRAGMA journal_mode=WAL")
for name, t in spec["tables"].items():
    con.execute('CREATE TABLE "%s" (%s)' % (name, ", ".join('"%s" %s' % (c["name"], c["type"] or "") for c in t["columns"])))
    for row in t["rows"]:
        ks = list(row)
        vals = [row[k] for k in ks]
        con.execute('INSERT INTO "%s" (%s) VALUES (%s)' % (name, ", ".join('"%s"' % k for k in ks), ", ".join("?" * len(ks))), vals)
con.commit(); con.close()
`;
function buildDb(dir: string, file: string, tables: Record<string, any[]>) {
  const spec = { db: file, tables: Object.fromEntries(Object.entries(tables).map(([n, rows]) => [n, { columns: report.tables[n].columns, rows }])) };
  const specPath = path.join(dir, `spec-${path.basename(file)}.json`);
  writeFileSync(specPath, JSON.stringify(spec));
  execFileSync(python, ["-I", "-c", BUILD, specPath]);
}

const ROWS = {
  families: [{ id: "fam", title: "Family", created_at: "2026-01-01" }],
  projects: [
    { id: "p1", title: "One", brief: "b", goal_hash: "h", cwd: "C:/Code/app", created_at: "2026-01-01", archived: 0, status: "active", family: "fam", version: 1 },
    { id: "p2", title: "Two", brief: null, goal_hash: null, cwd: null, created_at: "2026-01-02", archived: 1, status: "draft", family: "fam", version: 2 },
  ],
  nodes: [
    { project_id: "p1", id: "A", title: "Alpha", goal: "g", check_json: '{"type": "command", "run": "x"}', deps_json: '[{"id": "B", "crosses": "x"}]', files_json: '["a.py"]', status: "done", attempts: 2, updated_at: "2026-01-03T00:00:00Z", actor: "w" },
    { project_id: "p1", id: "B", title: "Beta", goal: "g2", check_json: null, deps_json: null, files_json: null, status: "todo", attempts: 0, updated_at: null, actor: null },
    { project_id: "p2", id: "A", title: "Alpha again", goal: "", check_json: "not json", deps_json: "[]", files_json: "[]", status: "todo", attempts: 0, updated_at: null, actor: null },
  ],
  node_loops: [{ project_id: "p1", node_id: "A", kind: "worker", script: "s", crosses_json: "[]", risk: "low", scope_json: "{}" }],
  events: [
    { seq: 1, at: "2026-01-01T00:00:00Z", project_id: "p1", node_id: null, kind: "run", actor: "engine", summary: "one", data_json: '{"run": "r1"}' },
    { seq: 1, at: "2026-01-01T00:00:01Z", project_id: "p2", node_id: "A", kind: "status", actor: "engine", summary: "same seq, other project", data_json: '{"caf\\u00e9": 1, "n": [1, 2]}' },
    { seq: 2, at: "2026-01-01T00:00:02Z", project_id: null, node_id: null, kind: "note", actor: "owner", summary: "global", data_json: null },
  ],
  runs: [{ id: "r1", project_id: "p1", settings_json: '{"preset": "frugal", "floors": {"codex": 0.75}, "pools": ["claude_code"]}', status: "running", created_at: "2026-01-01T00:00:00Z", started_at: "2026-01-01T00:00:00Z", ended_at: null }],
  sprints: [
    { id: "s1", run_id: "r1", pool: "claude_code", start_at: "2026-01-01T01:00:00Z", end_at: "2026-01-01T02:00:00Z", status: "cancelled" },
    { id: "s2", run_id: "gone", pool: "codex", start_at: null, end_at: null, status: "planned" },
  ],
  questions: [{ id: "q1", project_id: null, node_id: null, kind: "select", prompt: "Pick", options_json: '[{"value": "a", "label": "A"}]', scale: null, answer_json: '{"type": "answer", "value": ["a"]}', answered_at: "2026-01-02T00:00:00Z", captured: 1, allowed_json: '["ignore", "answer"]', proposal_json: null, purpose: "generic" }],
  claims: [
    { project_id: "p1", node_id: "A", claimed_by: "w1", worktree: "main", cwd: "C:\\Code\\app\\engine", environment: "env", since: "2026-01-01T00:00:00+00:00", refreshed_at: "2026-01-01T00:01:00+00:00" },
    { project_id: "p2", node_id: "A", claimed_by: "w2", worktree: null, cwd: null, environment: null, since: "2026-01-01T00:00:00+00:00", refreshed_at: "2026-01-01T00:00:00+00:00" },
  ],
  todos: [
    { id: "t1", text: "do it", status: "open", created_at: "2026-01-01T00:00:00+00:00", updated_at: "2026-01-01T00:00:00+00:00", created_by: "owner", handoffs_json: "[]", merged_into: null, section: "human", position: 3, link: null, node_json: '{"project": "p1", "id": "A"}', deliverables_json: "[]", first_clicked_at: null, archived_at: null, done_by: null },
    { id: "t2", text: "machine", status: "done", created_at: "2026-01-01T00:00:00+00:00", updated_at: null, created_by: "owner", handoffs_json: "[]", merged_into: null, section: "weird", position: null, link: null, node_json: null, deliverables_json: "[]", first_clicked_at: null, archived_at: null, done_by: "owner" },
  ],
  meta: [
    { key: "revision", value: "5" },
    { key: "floor:codex", value: "0.75" },
    { key: "autonomous:settings", value: '{"floors": {"c": 1}, "x": 2}' },
    { key: "packet:p:n", value: null },
  ],
  receipts: [{ id: "rc1", path: "p", name: "n", project: "p1", node: "A", session: null, status: "done", claimed: null, outcome: null, summary: "s", reason: null, data_json: "{}", ingested_at: "2026-01-01", content: "{}" }],
  agents: [{ id: "ag1", kind: "worker", cost_eq: 0.1, tokens_in: 5, title: "t" }],
  toggles: [{ id: "codex", enabled: 0, updated_at: "x", updated_by: "owner" }],
};
const LEARN_ROWS = { mastery: [{ concept_id: "c1", level: 2, attempts: 3, correct: 1, updated_at: "2026-01-01" }] };

function setup() {
  const dir = mkdtempSync(path.join(os.tmpdir(), "wp-migrate-"));
  const cc = path.join(dir, "cc_copy.sqlite");
  const learn = path.join(dir, "learn_copy.sqlite");
  buildDb(dir, cc, ROWS as any);
  buildDb(dir, learn, LEARN_ROWS as any);
  const files = path.join(dir, "inbox");
  mkdirSync(path.join(files, "sub"), { recursive: true });
  writeFileSync(path.join(files, "a.txt"), "hello");
  writeFileSync(path.join(files, "sub", "b.json"), "{}");
  return { dir, cc, learn, files, dest: path.join(dir, "folder") };
}
const rd = (p: string) => JSON.parse(readFileSync(p, "utf8"));

test("migrates every table, round trip shows zero mismatches, source copy untouched", async () => {
  const s = setup();
  const before = await sha256File(s.cc); const learnBefore = await sha256File(s.learn);
  const r = await migrate({ ccCopy: s.cc, learnCopy: s.learn, dest: s.dest, python, machine: "pc", fileState: { inbox: s.files } });
  assert.equal(r.skipped, false);
  const rt = await roundtripCheck({ ccCopy: s.cc, learnCopy: s.learn, dest: s.dest, python });
  assert.deepEqual(rt.problems, []);
  assert.equal(rt.mismatches, 0, JSON.stringify(rt.tables));
  assert.equal(rt.ok, true);
  assert.equal(rt.source_sha256_ok, true);
  for (const t of [...Object.keys(ROWS), "mastery"]) assert.ok(rt.tables[t], `table ${t} reported`);
  assert.equal(rt.tables.events.source, 3);
  assert.equal(rt.tables.events.rebuilt, 3);
  assert.equal(rt.tables.meta.source, 4);
  assert.equal(rt.tables.meta.dropped, 1, "one floor meta row dropped (a floors key inside another value is stripped in place)");
  assert.equal(rt.file_state.inbox.files, 2);
  assert.equal(rt.file_state.inbox.mismatches, 0);
  const m = await readManifest(s.dest);
  assert.equal(m.mismatches, 0);
  assert.equal(m.source_sha256, before);
  // the source copy is byte-identical afterwards
  assert.equal(await sha256File(s.cc), before);
  assert.equal(await sha256File(s.learn), learnBefore);
});

test("events with the same seq from different projects both survive; source seq kept in a field", async () => {
  const s = setup();
  await migrate({ ccCopy: s.cc, learnCopy: s.learn, dest: s.dest, python, machine: "pc" });
  const lines = (p: string) => readFileSync(path.join(s.dest, "events", p), "utf8").trim().split("\n").map((l) => JSON.parse(l));
  const e1 = lines("p1.ndjson"); const e2 = lines("p2.ndjson"); const e0 = lines("_none.ndjson");
  assert.equal(e1[0].src_seq, 1);
  assert.equal(e2[0].src_seq, 1);
  assert.equal(e0[0].src_seq, 2);
  assert.equal(e2[0].data["caf\u00e9"], 1);
  // two claims on node A from two projects do not overwrite each other
  assert.ok(existsSync(path.join(s.dest, "claims/A.json")));
  assert.ok(existsSync(path.join(s.dest, "claims/p2--A.json")));
});

test("no floor from 1.1 lands as a number", async () => {
  const s = setup();
  await migrate({ ccCopy: s.cc, learnCopy: s.learn, dest: s.dest, python, machine: "pc" });
  const floors = rd(path.join(s.dest, "settings/floors.json"));
  assert.ok(Object.keys(floors.pools).length >= 4);
  for (const p of Object.values<any>(floors.pools)) assert.equal(p.min_percent, null);
  const run = rd(path.join(s.dest, "runs/r1.json"));
  assert.equal("floors" in run.settings, false);
  assert.equal(run.settings.preset, "frugal");
  const meta = rd(path.join(s.dest, "settings/meta_1_1.json"));
  assert.equal(meta.entries.some((e: any) => /floor/i.test(e.key)), false);
  assert.equal(JSON.stringify(meta).includes("0.75"), false);
  const m = await readManifest(s.dest);
  assert.ok(m.floors_dropped.runs >= 1 && m.floors_dropped.meta >= 2);
  assert.equal(rd(path.join(s.dest, MANIFEST_REL)).floors_file, "settings/floors.json");
});

test("sprints become schedules and ride inside their run", async () => {
  const s = setup();
  const r = await migrate({ ccCopy: s.cc, learnCopy: s.learn, dest: s.dest, python, machine: "pc" });
  assert.equal(r.manifest.schedules.length, 1);
  const sched = rd(path.join(s.dest, r.manifest.schedules[0]));
  assert.equal(sched.id, "r1.s1");
  assert.equal(sched.project, "p1");
  assert.equal(sched.spend_ceiling, 0);
  assert.equal(rd(path.join(s.dest, "runs/r1.json")).sprints[0].id, "s1");
});

test("idempotent: a second run on the same source changes nothing", async () => {
  const s = setup();
  await migrate({ ccCopy: s.cc, learnCopy: s.learn, dest: s.dest, python, machine: "pc" });
  const mt = statSync(path.join(s.dest, "projects/p1/project.json")).mtimeMs;
  const again = await migrate({ ccCopy: s.cc, learnCopy: s.learn, dest: s.dest, python, machine: "pc" });
  assert.equal(again.skipped, true);
  assert.equal(statSync(path.join(s.dest, "projects/p1/project.json")).mtimeMs, mt);
});

test("a tampered file is caught by the round trip", async () => {
  const s = setup();
  await migrate({ ccCopy: s.cc, learnCopy: s.learn, dest: s.dest, python, machine: "pc" });
  const f = path.join(s.dest, "projects/p1/nodes/A.json");
  const e = rd(f); e.title = "changed"; writeFileSync(f, JSON.stringify(e));
  const rt = await roundtripCheck({ ccCopy: s.cc, learnCopy: s.learn, dest: s.dest, python, writeManifest: false });
  assert.equal(rt.ok, false);
  assert.ok(rt.tables.nodes.mismatches >= 1);
});

test("migrate never opens a live database path; it only receives a copy", async () => {
  assert.equal(isLiveDbPath("~/workspace/dsh-control-centre/data/cc.sqlite"), true);
  assert.equal(isLiveDbPath(path.join(os.tmpdir(), "x", "data", "cc.sqlite")), false);
  const dest = mkdtempSync(path.join(os.tmpdir(), "wp-migrate-live-"));
  // the path does not exist: a refusal (not a file-not-found from python) proves nothing was opened
  await assert.rejects(migrate({ ccCopy: "~/workspace/dsh-control-centre/data/cc.sqlite", dest, python }), /refusing to read a live database/);
  assert.equal(existsSync(path.join(dest, "migration")), false);
});

test("pyDumps matches Python json.dumps formatting", () => {
  assert.equal(pyDumps({ a: [1, 2], "caf\u00e9": null, s: "x\ny" }), '{"a": [1, 2], "caf\\u00e9": null, "s": "x\\ny"}');
});

test("--live-report prints per-table counts (run against a copy here)", async () => {
  const s = setup();
  const rep = await liveReport({ db: s.cc, learn: s.learn, python });
  assert.equal((rep.tables as any).events.source, 3);
  const out = spawnSync(process.execPath, ["--experimental-strip-types", "--no-warnings", path.join(here, "../migrate/roundtrip_check.ts"), "--live-report", "--db", s.cc, "--learn", s.learn, "--python", python], { encoding: "utf8" });
  assert.equal(out.status, 0, out.stderr);
  assert.equal(JSON.parse(out.stdout).tables.projects.source, 2);
});
