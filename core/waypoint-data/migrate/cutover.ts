// The real 1.1 -> 1.2 cutover, run by node ship-cutover. Never run it from a test against the live DB.
//
//   --apply   read-only open of cc.sqlite (SQLite URI mode=ro, never written, no WAL checkpoint), sqlite backup API copy (includes
//             WAL contents) to <folder>/backups/cc-pre-1.2.sqlite, migrate FROM THE BACKUP into <folder>, round trip against the
//             backup, write cutover-manifest.json (source SHA-256 at backup time, information only) and cutover-receipt.json
//             under <folder>/backups. Nothing is written beside the source DB, so only the folder you name can switch the CLI.
//   --verify --folder F  exit 0 only when: F/backups/cc-pre-1.2.sqlite exists, the per-table counts in F equal the backup's counts,
//             the round trip against the backup has zero mismatches, and every floor in F is unset. The live cc.sqlite hash is NOT
//             checked: the engine legitimately keeps writing to it.
//
// Options: --db <cc.sqlite> --folder <data folder> --learn <learn.sqlite> --python <p> --files name=dir (repeatable) --force
import { promises as fsp, statSync, readFileSync } from "node:fs";
import os from "node:os";
import nodePath from "node:path";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";
import { resolveDataRoot } from "../src/paths.ts";
import { MANIFEST_REL, countTables, defaultPython, migrate, runPython, sha256File } from "./migrate_1_1.ts";
import { roundtripCheck } from "./roundtrip_check.ts";

export interface CutoverOptions {
  db: string;
  folder: string;
  learn?: string | null;
  python?: string;
  fileState?: Record<string, string>;
  force?: boolean;
}
const BACKUP_NAME = "cc-pre-1.2.sqlite";
const LEARN_BACKUP_NAME = "learn-pre-1.2.sqlite";

const paths = (folder: string) => {
  const dir = nodePath.join(nodePath.resolve(folder), "backups");
  return { dir, backup: nodePath.join(dir, BACKUP_NAME), learnBackup: nodePath.join(dir, LEARN_BACKUP_NAME),
    manifest: nodePath.join(dir, "cutover-manifest.json"), receipt: nodePath.join(dir, "cutover-receipt.json") };
};
const exists = async (p: string) => { try { await fsp.access(p); return true; } catch { return false; } };
const writeJson = async (p: string, v: unknown) => {
  await fsp.mkdir(nodePath.dirname(p), { recursive: true });
  const tmp = `${p}.${process.pid}.tmp`;
  await fsp.writeFile(tmp, JSON.stringify(v, null, 2) + "\n", "utf8");
  await fsp.rename(tmp, p);
};

/** The repo's runner folder, derived from this file's own location (core/waypoint-data/migrate -> repo root). */
export const runnerDir = () => nodePath.resolve(fileURLToPath(new URL("../../../runner", import.meta.url)));

/**
 * Machine paths live in the data folder's settings, not in shipped code: seed settings/settings.json with
 * {python, runner_dir} for keys not already present. Never overwrites a user's value. Returns the keys seeded.
 */
export async function seedSettings(folder: string, python: string): Promise<string[]> {
  const file = nodePath.join(folder, "settings", "settings.json");
  let cur: Record<string, unknown> = {};
  if (await exists(file)) {
    try { cur = JSON.parse(await fsp.readFile(file, "utf8")); } catch { return []; } // unreadable: leave it alone
    if (!cur || typeof cur !== "object" || Array.isArray(cur)) return [];
  }
  // A new file also gets the schema's required keys (settings.schema.json), so it is valid from the start.
  const want: Record<string, unknown> = { usage_fresh_minutes: 30, max_retries: 2, sync_mode: {}, python, runner_dir: runnerDir().split(nodePath.sep).join("/") };
  const seeded = Object.keys(want).filter((k) => !(k in cur));
  if (seeded.length) await writeJson(file, { ...cur, ...Object.fromEntries(seeded.map((k) => [k, want[k]])) });
  return seeded;
}

export async function apply(o: CutoverOptions) {
  const python = o.python ?? defaultPython();
  const p = paths(o.folder);
  if (!o.force && (await exists(p.manifest)) && (await exists(p.backup))) throw new Error(`cutover already applied (${p.manifest}); pass --force to redo it`);
  await fsp.mkdir(p.dir, { recursive: true });
  const learn = o.learn && (await exists(o.learn)) ? o.learn : null;

  const sha_source = await sha256File(o.db);
  const learn_sha_source = learn ? await sha256File(learn) : null;
  const backup = JSON.parse(await runPython(python, ["backup", o.db, p.backup]));
  const learnBackup = learn ? JSON.parse(await runPython(python, ["backup", learn, p.learnBackup])) : null;
  if (backup.integrity_check !== "ok" || (learnBackup && learnBackup.integrity_check !== "ok")) throw new Error("backup failed its integrity check");
  const backup_sha256 = await sha256File(p.backup);
  await writeJson(p.manifest, { schema: "waypoint.cutover_manifest.v2", at: new Date().toISOString(), db: nodePath.resolve(o.db), learn_db: learn ? nodePath.resolve(learn) : null,
    source_sha256: sha_source, learn_source_sha256: learn_sha_source, source_sha256_note: "information only; the engine keeps writing to the live DB",
    backup: { path: p.backup, sha256: backup_sha256, integrity_check: backup.integrity_check }, learn_backup: learnBackup ? { path: p.learnBackup, sha256: await sha256File(p.learnBackup) } : null });

  const folder = nodePath.resolve(o.folder);
  const mig = await migrate({ ccCopy: p.backup, learnCopy: learnBackup ? p.learnBackup : null, dest: folder, python, fileState: o.fileState });
  const rt = await roundtripCheck({ ccCopy: p.backup, learnCopy: learnBackup ? p.learnBackup : null, dest: folder, python });
  const settings_seeded = rt.ok ? await seedSettings(folder, python) : [];
  const receipt = { schema: "waypoint.cutover_receipt.v1", at: new Date().toISOString(), backup: p.backup, backup_sha256, folder, skipped_migration: mig.skipped,
    ok: rt.ok, mismatches: rt.mismatches, floors_unset: rt.floors_unset,
    tables: Object.fromEntries(Object.entries(rt.tables).map(([k, v]) => [k, { source: v.source, migrated: v.rebuilt, dropped: v.dropped, mismatches: v.mismatches }])),
    settings_seeded, floors_dropped: mig.manifest.floors_dropped, problems: rt.problems };
  await writeJson(p.receipt, receipt);
  return { receipt, manifest: p.manifest };
}

export async function verify(o: CutoverOptions): Promise<{ ok: boolean; problems: string[] }> {
  const python = o.python ?? defaultPython();
  const p = paths(o.folder);
  if (!(await exists(p.backup))) return { ok: false, problems: [`backup missing: ${p.backup}`] };
  const problems: string[] = [];
  const hasLearn = await exists(p.learnBackup);
  let man: any = null;
  try { man = JSON.parse(await fsp.readFile(p.manifest, "utf8")); } catch { /* the manifest is informational */ }
  if (man?.backup?.sha256 && man.backup.sha256 !== (await sha256File(p.backup))) problems.push("backup file SHA-256 differs from the manifest");
  try {
    const counts = { ...(await countTables(python, p.backup)), ...(hasLearn ? await countTables(python, p.learnBackup) : {}) };
    const rt = await roundtripCheck({ ccCopy: p.backup, learnCopy: hasLearn ? p.learnBackup : null, dest: o.folder, python, writeManifest: false });
    for (const [t, n] of Object.entries(counts)) {
      const r = rt.tables[t];
      if (!r) { problems.push(`folder has no counts for table ${t}`); continue; }
      if (r.source !== n) problems.push(`${t}: folder source count ${r.source} != backup ${n}`);
      if (r.rebuilt + r.dropped !== n) problems.push(`${t}: folder rows ${r.rebuilt} + dropped ${r.dropped} != backup ${n}`);
    }
    for (const t of Object.keys(rt.tables)) if (!(t in counts)) problems.push(`folder has an unknown table ${t}`);
    if (rt.mismatches !== 0) problems.push(`round trip against the backup: ${rt.mismatches} mismatches`);
    problems.push(...rt.problems);
    if (!rt.floors_unset && !rt.problems.some((x) => /floor/.test(x))) problems.push("a floor in the folder is set");
  } catch (e: any) { problems.push(`folder manifest missing or unreadable: ${nodePath.join(o.folder, MANIFEST_REL)} (${String(e?.message ?? e).slice(0, 200)})`); }
  return { ok: problems.length === 0, problems };
}

function locateLiveDb(): string {
  if (process.env.CC_DB) return process.env.CC_DB;
  const here = nodePath.dirname(fileURLToPath(import.meta.url));
  const cands = [nodePath.resolve(here, "../../../data/cc.sqlite")];
  try {
    const common = execFileSync("git", ["-C", here, "rev-parse", "--git-common-dir"], { encoding: "utf8" }).trim();
    if (common) cands.push(nodePath.resolve(here, common, "..", "data", "cc.sqlite"));
  } catch { /* not a git checkout */ }
  return cands.find((c) => { try { return !!statSync(c); } catch { return false; } }) ?? cands[0];
}

export async function main(argv: string[]): Promise<number> {
  const a = new Map<string, string>(); const files: Record<string, string> = {};
  for (let i = 0; i < argv.length; i++) {
    if (!argv[i].startsWith("--")) continue;
    const k = argv[i].slice(2); const v = argv[i + 1] && !argv[i + 1].startsWith("--") ? argv[++i] : "true";
    if (k === "files") { const [n, ...d] = v.split("="); files[n] = d.join("="); } else a.set(k, v);
  }
  const db = a.get("db") ?? locateLiveDb();
  const sibling = nodePath.join(nodePath.dirname(db), "learn.sqlite");
  const folder = a.get("folder") ?? resolveDataRoot({ env: process.env, home: os.homedir(), readFile: (f) => { try { return readFileSync(f, "utf8"); } catch { return null; } } }).root;
  const o: CutoverOptions = { db, folder, learn: a.get("learn") ?? sibling, python: a.get("python"), fileState: files, force: a.has("force") };
  if (a.has("apply")) {
    const r = await apply(o);
    console.log(JSON.stringify({ ok: r.receipt.ok, mismatches: r.receipt.mismatches, tables: r.receipt.tables, manifest: r.manifest }, null, 2));
    return r.receipt.ok ? 0 : 1;
  }
  if (a.has("verify")) {
    const r = await verify(o);
    console.log(JSON.stringify(r, null, 2));
    return r.ok ? 0 : 1;
  }
  console.error("usage: cutover.ts --apply | --verify [--db p] [--folder f] [--learn p] [--python p] [--files name=dir] [--force]");
  return 2;
}
if (import.meta.main) main(process.argv.slice(2)).then((c) => process.exit(c), (e) => { console.error(String(e?.message ?? e)); process.exit(1); });
