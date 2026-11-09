// Round-trip check for the 1.1 -> files migration: rebuild every table's rows from the folder (the manifest names the
// files) and compare, per table, with the source copy. Also: --live-report prints per-table counts for ship-cutover.
//
//   node --experimental-strip-types roundtrip_check.ts --source <cc copy> [--learn <learn copy>] --folder <dest> [--python p]
//   node --experimental-strip-types roundtrip_check.ts --live-report [--db <cc.sqlite>] [--learn <learn.sqlite>] [--folder <dest>]
//
// The comparison only reads: the source copy (read-only through the Python reader) and the folder.
import { promises as fsp, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import nodePath from "node:path";
import { execFileSync } from "node:child_process";
import { createNodeFsAdapter } from "../src/node-fs-adapter.ts";
import { atomicWrite } from "../src/io.ts";
import { MANIFEST_REL, FLOOR_POOLS, canon, countTables, defaultPython, loadSource, normalizeSource, rebuildTables, sha256File, sourceFileHashes,
  type Manifest, type Row } from "./migrate_1_1.ts";

export interface TableCheck { source: number; rebuilt: number; dropped: number; mismatches: number; missing: number; extra: number; examples: string[] }
export interface RoundtripResult {
  ok: boolean;
  mismatches: number;
  tables: Record<string, TableCheck>;
  source_sha256_ok: boolean;
  floors_unset: boolean;
  file_state: Record<string, { files: number; mismatches: number }>;
  problems: string[];
}

export interface RoundtripOptions {
  ccCopy: string;
  learnCopy?: string | null;
  dest: string;
  python?: string;
  /** Write the result into the manifest (mismatches, roundtrip block). Default true. */
  writeManifest?: boolean;
}

export async function readManifest(dest: string): Promise<Manifest> {
  return JSON.parse(await fsp.readFile(nodePath.join(dest, MANIFEST_REL), "utf8")) as Manifest;
}

export async function floorsAllUnset(dest: string, m?: Manifest): Promise<boolean> {
  try {
    const f = JSON.parse(await fsp.readFile(nodePath.join(dest, m?.floors_file ?? "settings/floors.json"), "utf8"));
    const pools = f?.pools ?? {};
    return Object.keys(pools).length > 0 && Object.values(pools).every((p: any) => p && p.min_percent === null);
  } catch {
    return false;
  }
}

export async function roundtripCheck(o: RoundtripOptions): Promise<RoundtripResult> {
  const python = o.python ?? defaultPython();
  const dest = nodePath.resolve(o.dest);
  const m = await readManifest(dest);
  const problems: string[] = [];
  const srcSha = await sha256File(o.ccCopy);
  const source_sha256_ok = srcSha === m.source_sha256;
  if (!source_sha256_ok) problems.push("source copy changed since the migration (sha256 differs from the manifest)");

  const raw = await loadSource(python, o.ccCopy, o.learnCopy);
  const { tables: src } = normalizeSource(raw);
  const back = await rebuildTables(dest, m);
  const tables: Record<string, TableCheck> = {};
  let total = 0;
  for (const name of new Set([...Object.keys(src), ...Object.keys(m.tables)])) {
    const s = src[name]; const b = back[name];
    if (!s || !b) {
      const n = (s?.rows.length ?? 0) + (b?.length ?? 0);
      tables[name] = { source: raw[name]?.rows.length ?? 0, rebuilt: b?.length ?? 0, dropped: 0, mismatches: n || 1, missing: s?.rows.length ?? 0, extra: b?.length ?? 0, examples: [s ? "table missing from folder" : "table not in source"] };
      total += tables[name].mismatches;
      continue;
    }
    const cols = s.columns;
    const key = (r: Row) => JSON.stringify(cols.map((c) => canon(r[c])));
    const want = new Map<string, number>();
    for (const r of s.rows) want.set(key(r), (want.get(key(r)) ?? 0) + 1);
    let extra = 0; const examples: string[] = [];
    for (const r of b) {
      const k = key(r); const n = want.get(k) ?? 0;
      if (n > 0) want.set(k, n - 1);
      else { extra++; if (examples.length < 3) examples.push("unexpected row " + k.slice(0, 160)); }
    }
    let missing = 0;
    for (const [k, n] of want) if (n > 0) { missing += n; if (examples.length < 3) examples.push("missing row " + k.slice(0, 160)); }
    const mism = missing + extra + (s.columns.join() === m.tables[name]?.columns.join() ? 0 : 1);
    tables[name] = { source: raw[name].rows.length, rebuilt: b.length, dropped: raw[name].rows.length - s.rows.length, mismatches: mism, missing, extra, examples };
    total += mism;
  }
  // every file the manifest names must exist; sprint schedules too
  for (const [name, t] of Object.entries(m.tables)) for (const f of t.files) {
    try { await fsp.access(nodePath.join(dest, f)); } catch { problems.push(`${name}: missing file ${f}`); total++; }
  }
  for (const f of m.schedules) {
    try { await fsp.access(nodePath.join(dest, f)); } catch { problems.push(`missing schedule ${f}`); total++; }
  }
  const expectedSprints = (src.sprints?.rows ?? []).filter((s) => (src.runs?.rows ?? []).some((r) => r.id === s.run_id)).length;
  if (m.schedules.length !== expectedSprints) { problems.push(`schedules: ${m.schedules.length} files for ${expectedSprints} sprints`); total++; }

  // file-side state: hash of every file in the folder copy against the source folder
  const file_state: RoundtripResult["file_state"] = {};
  for (const [name, fs] of Object.entries(m.file_state)) {
    let bad = 0;
    const nowSrc = await sourceFileHashes(fs.source_dir);
    if (JSON.stringify(nowSrc) !== JSON.stringify(fs.files)) bad++;
    for (const [rel, h] of Object.entries(fs.files)) {
      try { if ((await sha256File(nodePath.join(dest, "legacy/1.1/files", encodeURIComponent(name).replace(/%/g, "_"), rel))) !== h) bad++; } catch { bad++; }
    }
    file_state[name] = { files: Object.keys(fs.files).length, mismatches: bad };
    total += bad;
  }
  const floors_unset = await floorsAllUnset(dest, m);
  if (!floors_unset) problems.push("a floor in the folder is set (or settings/floors.json is missing)");
  if (!source_sha256_ok) total++;
  const result: RoundtripResult = { ok: total === 0 && floors_unset, mismatches: total, tables, source_sha256_ok, floors_unset, file_state, problems };
  if (o.writeManifest !== false) {
    const fs = await createNodeFsAdapter();
    const next = { ...m, mismatches: total, roundtrip: { at_source_sha256: srcSha, tables: Object.fromEntries(Object.entries(tables).map(([k, v]) => [k, { source: v.source, rebuilt: v.rebuilt, dropped: v.dropped, mismatches: v.mismatches }])) } };
    await atomicWrite(fs, nodePath.join(dest, MANIFEST_REL), JSON.stringify(next, null, 2) + "\n", { writer: "roundtrip-check", maxBytes: 64 * 1024 * 1024 });
  }
  return result;
}

/** Per-table counts of a database (read-only) next to what the folder holds, for ship-cutover. */
export async function liveReport(o: { db: string; learn?: string | null; folder?: string | null; python?: string }) {
  const python = o.python ?? defaultPython();
  const counts = { ...(await countTables(python, o.db)), ...(o.learn ? await countTables(python, o.learn) : {}) };
  let folder: Record<string, number> | null = null;
  if (o.folder) {
    try { const m = await readManifest(o.folder); folder = Object.fromEntries(Object.entries(m.tables).map(([k, v]) => [k, v.migrated + v.dropped])); } catch { folder = null; }
  }
  const tables = Object.fromEntries(Object.entries(counts).map(([k, n]) => [k, { source: n, folder: folder ? (folder[k] ?? null) : null }]));
  return { schema: "waypoint.live_report.v1", db: o.db, tables, total_rows: Object.values(counts).reduce((a, b) => a + b, 0) };
}

function locateLiveDb(): string {
  if (process.env.CC_DB) return process.env.CC_DB;
  const here = nodePath.dirname(fileURLToPath(import.meta.url));
  const cands = [nodePath.resolve(here, "../../../data/cc.sqlite")];
  try {
    const common = execFileSync("git", ["-C", here, "rev-parse", "--git-common-dir"], { encoding: "utf8" }).trim();
    if (common) cands.push(nodePath.resolve(here, common, "..", "data", "cc.sqlite"));
  } catch { /* not a git checkout */ }
  const found = cands.find((c) => { try { return !!statSync(c); } catch { return false; } });
  return found ?? cands[cands.length - 1];
}

export { FLOOR_POOLS };

async function main(argv: string[]): Promise<number> {
  const a = new Map<string, string>();
  for (let i = 0; i < argv.length; i++) if (argv[i].startsWith("--")) a.set(argv[i].slice(2), argv[i + 1] && !argv[i + 1].startsWith("--") ? argv[++i] : "true");
  if (a.has("live-report")) {
    const db = a.get("db") ?? locateLiveDb();
    const learn = a.get("learn") ?? (() => { const l = nodePath.join(nodePath.dirname(db), "learn.sqlite"); try { return statSync(l) && l; } catch { return null; } })();
    console.log(JSON.stringify(await liveReport({ db, learn, folder: a.get("folder") ?? null, python: a.get("python") }), null, 2));
    return 0;
  }
  if (!a.get("source") || !a.get("folder")) { console.error("usage: roundtrip_check.ts --source <cc copy> [--learn <copy>] --folder <dest>  |  --live-report [--db p] [--folder f]"); return 2; }
  const r = await roundtripCheck({ ccCopy: a.get("source")!, learnCopy: a.get("learn") ?? null, dest: a.get("folder")!, python: a.get("python") });
  console.log(JSON.stringify(r, null, 2));
  return r.ok ? 0 : 1;
}
if (import.meta.main) main(process.argv.slice(2)).then((c) => process.exit(c), (e) => { console.error(String(e?.message ?? e)); process.exit(1); });
