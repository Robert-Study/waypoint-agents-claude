// Cutover tests: --apply and --verify run against a temp copy only (a fake data/ folder under the system temp dir).
// The live DB is never touched; verify's failure modes are exercised by tampering with the temp files.
import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync, mkdirSync, existsSync, rmSync, statSync, readdirSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { apply, verify, runnerDir } from "../migrate/cutover.ts";
import { sha256File, defaultPython } from "../migrate/migrate_1_1.ts";

const here = path.dirname(fileURLToPath(import.meta.url));
const python = defaultPython();
const report = JSON.parse(readFileSync(path.join(here, "../spike/spike_report.json"), "utf8"));

const BUILD = String.raw`
import sqlite3, sys, json
spec = json.load(open(sys.argv[1], encoding="utf-8"))
import shutil, os
con = sqlite3.connect(spec["build"])
con.execute("PRAGMA journal_mode=WAL")
con.execute("PRAGMA wal_autocheckpoint=0")
for name, t in spec["tables"].items():
    con.execute('CREATE TABLE "%s" (%s)' % (name, ", ".join('"%s" %s' % (c["name"], c["type"] or "") for c in t["columns"])))
    for row in t["rows"]:
        ks = list(row)
        con.execute('INSERT INTO "%s" (%s) VALUES (%s)' % (name, ", ".join('"%s"' % k for k in ks), ", ".join("?" * len(ks))), [row[k] for k in ks])
con.commit()
# the last write stays in the WAL; copy db + wal while the writer is open so the fixture keeps un-checkpointed frames
con.execute("INSERT INTO meta (key, value) VALUES ('late', '1')"); con.commit()
for sfx in ("", "-wal"):
    shutil.copyfile(spec["build"] + sfx, spec["db"] + sfx)
    os.utime(spec["db"] + sfx, (1700000000, 1700000000))
con.close()
`;
const PY_EXEC = (spec: string) => execFileSync(python, ["-I", "-c", BUILD, spec]);
const PY_SQL = String.raw`
import sqlite3, sys
c = sqlite3.connect(sys.argv[1]); c.execute(sys.argv[2]); c.commit(); c.close()
`;

function setup() {
  const dir = mkdtempSync(path.join(os.tmpdir(), "wp-cutover-"));
  const data = path.join(dir, "data");
  mkdirSync(data, { recursive: true });
  const db = path.join(data, "cc.sqlite");
  const tables: Record<string, any[]> = {
    projects: [{ id: "p1", title: "One", brief: "b", goal_hash: "h", cwd: "C:/Code/app", created_at: "2026-01-01", archived: 0, status: "active", family: null, version: null }],
    nodes: [{ project_id: "p1", id: "A", title: "Alpha", goal: "g", check_json: null, deps_json: "[]", files_json: "[]", status: "todo", attempts: 0 }],
    events: [
      { seq: 1, at: "2026-01-01T00:00:00Z", project_id: "p1", node_id: null, kind: "run", actor: "e", summary: "s", data_json: null },
      { seq: 1, at: "2026-01-01T00:00:00Z", project_id: null, node_id: null, kind: "run", actor: "e", summary: "t", data_json: null },
    ],
    runs: [{ id: "r1", project_id: "p1", settings_json: '{"floors": {"codex": 0.75}}', status: "done", created_at: "2026-01-01T00:00:00Z" }],
    sprints: [], questions: [], families: [], claims: [], todos: [],
    meta: [{ key: "revision", value: "1" }],
  };
  const spec = { db, build: path.join(dir, "build.sqlite"), tables: Object.fromEntries(Object.entries(tables).map(([n, rows]) => [n, { columns: report.tables[n].columns, rows }])) };
  const sp = path.join(dir, "spec.json");
  writeFileSync(sp, JSON.stringify(spec));
  PY_EXEC(sp);
  const folder = path.join(dir, "folder");
  return { dir, data, db, folder, backups: path.join(folder, "backups") };
}

const snap = (f: string) => { const st = statSync(f); return { size: st.size, mtimeMs: st.mtimeMs }; };

test("--apply never writes the source DB, puts backup/manifest/receipt under <folder>/backups; --verify passes", async () => {
  const s = setup();
  const wal = s.db + "-wal";
  assert.ok(existsSync(wal) && statSync(wal).size > 0, "fixture has un-checkpointed WAL frames");
  const before = { db: readFileSync(s.db), dbStat: snap(s.db), wal: readFileSync(wal), walStat: snap(wal), names: readdirSync(s.data).sort() };
  const r = await apply({ db: s.db, folder: s.folder, python });
  assert.equal(r.receipt.ok, true);
  assert.equal(r.receipt.mismatches, 0);
  // the source is untouched: bytes, mtime, and no WAL checkpoint (the -wal still holds its frames)
  assert.ok(readFileSync(s.db).equals(before.db), "source DB bytes unchanged");
  assert.deepEqual(snap(s.db), before.dbStat, "source DB mtime unchanged");
  assert.ok(existsSync(wal) && readFileSync(wal).equals(before.wal), "source -wal unchanged (no checkpoint)");
  assert.deepEqual(snap(wal), before.walStat, "source -wal mtime unchanged");
  // nothing written beside the DB (no backups dir; only sqlite's own -shm may appear)
  assert.ok(!existsSync(path.join(s.data, "backups")), "no backups folder beside the DB");
  assert.deepEqual(readdirSync(s.data).filter((n) => !before.names.includes(n) && !n.endsWith("-shm")), []);
  // everything lands under <folder>/backups
  const backup = path.join(s.backups, "cc-pre-1.2.sqlite");
  assert.ok(existsSync(backup));
  assert.ok(existsSync(path.join(s.backups, "cutover-receipt.json")));
  assert.equal(r.manifest, path.join(s.backups, "cutover-manifest.json"));
  const man = JSON.parse(readFileSync(path.join(s.backups, "cutover-manifest.json"), "utf8"));
  assert.equal(man.source_sha256, await sha256File(s.db), "source hash recorded for information");
  assert.equal(man.backup.integrity_check, "ok");
  const rec = JSON.parse(readFileSync(path.join(s.backups, "cutover-receipt.json"), "utf8"));
  assert.equal(rec.tables.events.source, 2);
  assert.equal(rec.tables.meta.source, 2, "the row still in the WAL is in the backup");
  assert.equal(rec.tables.runs.dropped, 0);
  assert.ok(rec.floors_dropped.runs >= 1);
  const v = await verify({ db: s.db, folder: s.folder, python });
  assert.deepEqual(v.problems, []);
  assert.equal(v.ok, true);
});

test("a second --apply changes nothing (idempotent) and the receipt is not rewritten", async () => {
  const s = setup();
  await apply({ db: s.db, folder: s.folder, python });
  const files = () => { const out: Record<string, string> = {}; const walk = (d: string) => { for (const e of readdirSync(d, { withFileTypes: true })) { const f = path.join(d, e.name); if (e.isDirectory()) walk(f); else out[path.relative(s.dir, f)] = readFileSync(f).toString("base64"); } }; walk(s.folder); walk(s.data); return out; };
  const before = files();
  const again = await apply({ db: s.db, folder: s.folder, python }).then(() => "ran", (e) => String(e.message));
  assert.match(again, /already applied/);
  assert.deepEqual(files(), before);
});

test("--verify fails when a floor is set, the folder disagrees with the backup or the backup is missing; it ignores later writes to the live DB", async () => {
  const s = setup();
  await apply({ db: s.db, folder: s.folder, python });
  // 1. a floor is set in the folder
  const fl = path.join(s.folder, "settings/floors.json");
  const floors = JSON.parse(readFileSync(fl, "utf8"));
  floors.pools.codex_5h.min_percent = 75;
  writeFileSync(fl, JSON.stringify(floors));
  let v = await verify({ db: s.db, folder: s.folder, python });
  assert.equal(v.ok, false);
  assert.ok(v.problems.some((p) => /floor/.test(p)));
  floors.pools.codex_5h.min_percent = null;
  writeFileSync(fl, JSON.stringify(floors));
  assert.equal((await verify({ db: s.db, folder: s.folder, python })).ok, true);
  // 2. the folder no longer matches the backup (a migrated row file is removed)
  const mf = JSON.parse(readFileSync(path.join(s.folder, "migration/manifest-1.1.json"), "utf8"));
  const victim = path.join(s.folder, mf.tables.nodes.files[0]);
  const keep = readFileSync(victim);
  rmSync(victim);
  v = await verify({ db: s.db, folder: s.folder, python });
  assert.equal(v.ok, false);
  writeFileSync(victim, keep);
  assert.equal((await verify({ db: s.db, folder: s.folder, python })).ok, true);
  // 3. the live DB is written to after the cutover: the engine legitimately keeps writing, verify still passes
  execFileSync(python, ["-I", "-c", PY_SQL, s.db, "INSERT INTO meta (key, value) VALUES ('after', '1')"]);
  v = await verify({ db: s.db, folder: s.folder, python });
  assert.deepEqual(v.problems, []);
  // 4. the backup is gone
  rmSync(path.join(s.backups, "cc-pre-1.2.sqlite"));
  v = await verify({ db: s.db, folder: s.folder, python });
  assert.equal(v.ok, false);
  assert.match(v.problems[0], /backup missing/);
});

test("--apply refuses to run twice without --force", async () => {
  const s = setup();
  await apply({ db: s.db, folder: s.folder, python });
  await assert.rejects(apply({ db: s.db, folder: s.folder, python }), /already applied/);
  const again = await apply({ db: s.db, folder: s.folder, python, force: true });
  assert.equal(again.receipt.ok, true);
});

test("cutover CLI: --apply then --verify exit codes", () => {
  const s = setup();
  const run = (flag: string) => {
    try {
      execFileSync(process.execPath, ["--experimental-strip-types", "--no-warnings", path.join(here, "../migrate/cutover.ts"), flag, "--db", s.db, "--folder", s.folder, "--python", python], { encoding: "utf8", stdio: "pipe" });
      return 0;
    } catch (e: any) { return e.status as number; }
  };
  assert.equal(run("--verify"), 1, "verify before apply fails");
  assert.equal(run("--apply"), 0);
  assert.equal(run("--verify"), 0);
});

test("--apply seeds a schema-valid settings file (required keys, python, runner_dir) only for absent keys; --verify still passes", async () => {
  const s = setup();
  const sf = path.join(s.folder, "settings/settings.json");
  assert.ok(!existsSync(sf));
  const r = await apply({ db: s.db, folder: s.folder, python });
  assert.deepEqual(r.receipt.settings_seeded, ["usage_fresh_minutes", "max_retries", "sync_mode", "python", "runner_dir"]);
  const seeded = JSON.parse(readFileSync(sf, "utf8"));
  const schema = JSON.parse(readFileSync(new URL("../schema/settings.schema.json", import.meta.url), "utf8"));
  for (const k of schema.required) assert.ok(k in seeded, `seeded settings has required key ${k}`);
  for (const k of Object.keys(seeded)) assert.ok(k in schema.properties, `seeded key ${k} is in the schema`);
  assert.equal(seeded.python, python);
  assert.equal(seeded.runner_dir, runnerDir().split(path.sep).join("/"));
  assert.ok(existsSync(path.join(seeded.runner_dir, "waypoint_runner")), "runner_dir is the repo's runner folder");
  assert.equal(JSON.parse(readFileSync(path.join(s.backups, "cutover-receipt.json"), "utf8")).settings_seeded.length, 5);
  assert.equal((await verify({ db: s.db, folder: s.folder, python })).ok, true);
  // present -> untouched: a user's python stays, only the missing key is filled
  writeFileSync(sf, JSON.stringify({ usage_fresh_minutes: 10, python: "C:/mine/python.exe" }));
  const again = await apply({ db: s.db, folder: s.folder, python, force: true });
  assert.deepEqual(again.receipt.settings_seeded, ["max_retries", "sync_mode", "runner_dir"]);
  const kept = JSON.parse(readFileSync(sf, "utf8"));
  assert.equal(kept.python, "C:/mine/python.exe");
  assert.equal(kept.usage_fresh_minutes, 10);
  const full = readFileSync(sf, "utf8");
  const third = await apply({ db: s.db, folder: s.folder, python, force: true });
  assert.deepEqual(third.receipt.settings_seeded, []);
  assert.equal(readFileSync(sf, "utf8"), full, "a complete settings file is not rewritten");
  assert.equal((await verify({ db: s.db, folder: s.folder, python })).ok, true);
});
