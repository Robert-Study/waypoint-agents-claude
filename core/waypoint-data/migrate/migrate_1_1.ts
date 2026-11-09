// One-time Waypoint 1.1 SQLite -> 1.2 folder migration (node: wp12 data-migrate).
//
// Reader: Python sqlite3 (spike_report.json: sqlite_reader = "python"), opened read-only (mode=ro) on a COPY.
// This module never opens the live DB: migrate() refuses a path that looks like the live data/cc.sqlite outside the
// system temp folder, and cutover.ts is the only code that ever touches the live file (backup API).
//
// Mapped tables get one schema-shaped file each; every other table lands losslessly under legacy/1.1/.
// Columns a mapped file cannot reproduce byte for byte are kept in that file's `legacy_cols` (counted in the manifest).
// Floors from 1.1 are never migrated as numbers: keys named *floor* are stripped and counted, and
// settings/floors.json is created with every pool unset (never overwritten).
import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import { promises as fsp } from "node:fs";
import os from "node:os";
import nodePath from "node:path";
import type { FsAdapter } from "../src/fs-adapter.ts";
import { createNodeFsAdapter } from "../src/node-fs-adapter.ts";
import { atomicWrite } from "../src/io.ts";
import { toCwdRef } from "../src/paths.ts";

export type Cell = null | number | string | { $float: string } | { $blob: string };
export type Row = Record<string, Cell>;
export interface TableData { database: "cc" | "learn"; columns: string[]; rows: Row[] }
export type Tables = Record<string, TableData>;

export const MIGRATION_DIR = "migration";
export const MANIFEST_REL = `${MIGRATION_DIR}/manifest-1.1.json`;
export const FLOOR_POOLS = ["claude_5h", "claude_week", "codex_5h", "codex_week"];
const WRITER = "migrate-1.1";
const MAX_BYTES = 256 * 1024 * 1024;

// ---------------------------------------------------------------- python helper (embedded, run with -I -c)
export const PY_HELPER = String.raw`
import sqlite3, sys, json, pathlib, hashlib
def enc(v):
    if isinstance(v, (bytes, bytearray, memoryview)): return {"$blob": bytes(v).hex()}
    if isinstance(v, float): return {"$float": repr(v)}
    if isinstance(v, int) and abs(v) > 2**53: raise SystemExit("integer beyond 2^53 not supported")
    return v
def ro(path):
    con = sqlite3.connect(pathlib.Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    con.text_factory = lambda b: b.decode("utf-8", "surrogateescape")
    return con
def tables(con):
    return [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
mode = sys.argv[1]
if mode == "read":
    con = ro(sys.argv[2]); out = {}
    for t in tables(con):
        cols = [c[1] for c in con.execute('PRAGMA table_info("%s")' % t)]
        try: rows = con.execute('SELECT * FROM "%s" ORDER BY rowid' % t).fetchall()
        except sqlite3.OperationalError: rows = con.execute('SELECT * FROM "%s"' % t).fetchall()
        out[t] = {"columns": cols, "rows": [[enc(v) for v in r] for r in rows]}
    print(json.dumps(out, ensure_ascii=True))
elif mode == "counts":
    con = ro(sys.argv[2])
    print(json.dumps({t: con.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0] for t in tables(con)}))
elif mode == "backup":
    # Source is opened read-only (mode=ro) and never written: no checkpoint, no journal change. The backup API reads the
    # committed WAL contents too. Only the destination is written.
    src = ro(sys.argv[2])
    dst_path = pathlib.Path(sys.argv[3])
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    for sfx in ("", "-wal", "-shm", "-journal"):
        pathlib.Path(str(dst_path) + sfx).unlink(missing_ok=True)
    dst = sqlite3.connect(str(dst_path))
    src.backup(dst); dst.commit()
    dst.execute("PRAGMA journal_mode=DELETE")
    integ = dst.execute("PRAGMA integrity_check").fetchone()[0]
    dst.close(); src.close()
    print(json.dumps({"integrity_check": integ}))
else:
    raise SystemExit("unknown mode")
`;

export function defaultPython(): string {
  return process.env.WAYPOINT_PYTHON || "python"; // set WAYPOINT_PYTHON where bare python is not a real interpreter
}

export function runPython(python: string, args: string[]): Promise<string> {
  return new Promise((resolve, reject) => {
    const p = spawn(python, ["-I", "-c", PY_HELPER, ...args], { stdio: ["ignore", "pipe", "pipe"] });
    const out: Buffer[] = [];
    let err = "";
    p.stdout.on("data", (d) => out.push(d));
    p.stderr.on("data", (d) => (err += d));
    p.on("error", reject);
    p.on("close", (code) => (code === 0 ? resolve(Buffer.concat(out).toString("utf8")) : reject(new Error(`python ${args[0]} failed (${code}): ${err.slice(-400)}`))));
  });
}

export async function readSqlite(python: string, dbPath: string, database: "cc" | "learn"): Promise<Tables> {
  const raw = JSON.parse(await runPython(python, ["read", dbPath])) as Record<string, { columns: string[]; rows: Cell[][] }>;
  const out: Tables = {};
  for (const [name, t] of Object.entries(raw)) {
    out[name] = { database, columns: t.columns, rows: t.rows.map((r) => Object.fromEntries(t.columns.map((c, i) => [c, r[i]])) as Row) };
  }
  return out;
}

export async function countTables(python: string, dbPath: string): Promise<Record<string, number>> {
  return JSON.parse(await runPython(python, ["counts", dbPath]));
}

// ---------------------------------------------------------------- helpers
export const sha256 = (b: Buffer | string) => createHash("sha256").update(b).digest("hex");
export async function sha256File(p: string): Promise<string> {
  return sha256(await fsp.readFile(p));
}
export function canon(c: Cell | undefined): string {
  if (c === null || c === undefined) return "n";
  if (typeof c === "number") return "i" + c;
  if (typeof c === "string") return "t" + c;
  if ("$float" in c) return "f" + c.$float;
  return "b" + c.$blob;
}
const rowKey = (cols: string[], r: Row) => JSON.stringify(cols.map((c) => canon(r[c])));

/** JSON text exactly as Python's json.dumps (default separators, ensure_ascii) would write it. */
export function pyDumps(v: unknown): string {
  if (v === null || v === undefined) return "null";
  if (typeof v === "string") return JSON.stringify(v).replace(/[\u007f-\uffff]/g, (ch) => (ch === "\u007f" ? ch : "\\u" + ch.charCodeAt(0).toString(16).padStart(4, "0")));
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  if (Array.isArray(v)) return "[" + v.map(pyDumps).join(", ") + "]";
  return "{" + Object.entries(v as Record<string, unknown>).map(([k, x]) => pyDumps(k) + ": " + pyDumps(x)).join(", ") + "}";
}
const tryParse = (c: Cell): { ok: true; v: unknown } | { ok: false } => {
  if (typeof c !== "string") return { ok: false };
  try { return { ok: true, v: JSON.parse(c) }; } catch { return { ok: false }; }
};
const plain = (c: Cell): string | number | null => (c === null || typeof c === "string" || typeof c === "number" ? c : null);
const str = (c: Cell): string | null => (typeof c === "string" ? c : null);
const jsonField = (c: Cell): unknown => { const p = tryParse(c); return p.ok ? p.v : null; };
const jsonBack = (v: unknown): Cell => (v === null || v === undefined ? null : pyDumps(v));
const safeName = (s: string) => encodeURIComponent(s).replace(/%/g, "_");
const FLOOR_KEY = /floor/i;

/** Strip every key named *floor* (recursively). Returns the cleaned value and how many keys went. */
export function dropFloors(v: unknown): { v: unknown; dropped: number } {
  let dropped = 0;
  const walk = (x: unknown): unknown => {
    if (Array.isArray(x)) return x.map(walk);
    if (x && typeof x === "object") {
      const o: Record<string, unknown> = {};
      for (const [k, val] of Object.entries(x)) {
        if (FLOOR_KEY.test(k)) { dropped++; continue; }
        o[k] = walk(val);
      }
      return o;
    }
    return x;
  };
  const out = walk(v);
  return { v: out, dropped };
}

/** The source as the migration promises to carry it: floors stripped. Used by migrate AND by the round-trip check. */
export function normalizeSource(tables: Tables): { tables: Tables; floorsDropped: Record<string, number> } {
  const out: Tables = {};
  const floorsDropped: Record<string, number> = {};
  const bump = (t: string, n: number) => { if (n) floorsDropped[t] = (floorsDropped[t] ?? 0) + n; };
  for (const [name, t] of Object.entries(tables)) {
    let rows = t.rows;
    if (name === "meta") {
      rows = [];
      for (const r of t.rows) {
        if (typeof r.key === "string" && FLOOR_KEY.test(r.key)) { bump(name, 1); continue; }
        const p = tryParse(r.value);
        if (p.ok && p.v && typeof p.v === "object") {
          const d = dropFloors(p.v);
          if (d.dropped) { bump(name, d.dropped); rows.push({ ...r, value: pyDumps(d.v) }); continue; }
        }
        rows.push(r);
      }
    } else if (name === "runs") {
      rows = t.rows.map((r) => {
        const p = tryParse(r.settings_json);
        if (!p.ok) return r;
        const d = dropFloors(p.v);
        if (!d.dropped) return r;
        bump(name, d.dropped);
        return { ...r, settings_json: pyDumps(d.v) };
      });
    }
    out[name] = rows === t.rows ? t : { ...t, rows };
  }
  return { tables: out, floorsDropped };
}

export function isLiveDbPath(p: string): boolean {
  const abs = nodePath.resolve(p);
  const tmp = nodePath.resolve(os.tmpdir()).toLowerCase();
  if (abs.toLowerCase().startsWith(tmp + nodePath.sep)) return false;
  return nodePath.basename(abs).toLowerCase() === "cc.sqlite" && nodePath.basename(nodePath.dirname(abs)).toLowerCase() === "data";
}

// ---------------------------------------------------------------- mapped tables
export interface MapCtx { machine: string; codeRoot: string }
const isoOr = (c: Cell, fallback = "1970-01-01T00:00:00Z") => (typeof c === "string" && c.length >= 10 ? c : fallback);
const asBool = (c: Cell) => (c === 1 ? true : c === 0 ? false : null);
const fromBool = (b: unknown): Cell => (b === true ? 1 : b === false ? 0 : null);

const projectToEntity = (r: Row, x: MapCtx) => {
  let cwd: { machine: string; rel: string } | null = null;
  if (typeof r.cwd === "string") {
    try { cwd = toCwdRef(x.machine, x.codeRoot, r.cwd, "win32"); } catch { cwd = null; }
  }
  return { schema: "waypoint.project.v1", id: plain(r.id), title: plain(r.title), brief: plain(r.brief), goal_hash: plain(r.goal_hash), cwd,
    created_at: plain(r.created_at), archived: asBool(r.archived), status: plain(r.status), family: plain(r.family), version: plain(r.version),
    rev: 1, updated_at: isoOr(r.created_at), updated_by: WRITER };
};
const projectFrom = (e: any, x: MapCtx): Row => ({
  id: e.id, title: e.title, brief: e.brief, goal_hash: e.goal_hash,
  cwd: e.cwd && typeof e.cwd.rel === "string" ? (e.cwd.rel === "" ? x.codeRoot : `${x.codeRoot}/${e.cwd.rel}`) : null,
  created_at: e.created_at, archived: fromBool(e.archived), status: e.status, family: e.family, version: e.version });

const nodeToEntity = (r: Row) => ({ schema: "waypoint.node.v1", project: plain(r.project_id), id: plain(r.id), title: plain(r.title), goal: plain(r.goal),
  goal_hash: plain(r.goal_hash), check_hash: plain(r.check_hash), check: jsonField(r.check_json), deps: jsonField(r.deps_json),
  estimate: 1, slot: null, location: { where: "unset", changed_at: null }, files: jsonField(r.files_json), kind: "worker",
  owner: plain(r.owner), lane: plain(r.lane), pool: plain(r.pool), model: plain(r.model), effort: plain(r.effort), recommended: plain(r.recommended),
  status: plain(r.status), attempts: plain(r.attempts), actor: plain(r.actor), evidence: plain(r.evidence),
  rev: 1, updated_at: isoOr(r.updated_at), updated_by: str(r.actor) ?? WRITER });
const nodeFrom = (e: any): Row => ({ project_id: e.project, id: e.id, title: e.title, goal: e.goal, goal_hash: e.goal_hash, check_json: jsonBack(e.check),
  deps_json: jsonBack(e.deps), owner: e.owner, lane: e.lane, pool: e.pool, model: e.model, effort: e.effort, status: e.status, attempts: e.attempts,
  actor: e.actor, evidence: e.evidence, updated_at: e.updated_at === "1970-01-01T00:00:00Z" ? null : e.updated_at, check_hash: e.check_hash,
  recommended: e.recommended, files_json: jsonBack(e.files) });

const familyToEntity = (r: Row, members: string[]) => ({ schema: "waypoint.family.v1", id: plain(r.id), title: plain(r.title), created_at: plain(r.created_at),
  projects: members, rev: 1, updated_at: isoOr(r.created_at), updated_by: WRITER });
const familyFrom = (e: any): Row => ({ id: e.id, title: e.title, created_at: e.created_at });

const runToEntity = (r: Row, sprints: Row[]) => ({ schema: "waypoint.run.v1", id: plain(r.id), project: plain(r.project_id), settings: jsonField(r.settings_json),
  status: plain(r.status), created_at: plain(r.created_at), started_at: plain(r.started_at), ended_at: plain(r.ended_at),
  sprints: sprints.map((s) => ({ id: plain(s.id), pool: plain(s.pool), start_at: plain(s.start_at), end_at: plain(s.end_at), status: plain(s.status) })),
  rev: 1, updated_at: isoOr(r.ended_at ?? r.started_at ?? r.created_at), updated_by: WRITER });
const runFrom = (e: any): Row => ({ id: e.id, project_id: e.project, settings_json: jsonBack(e.settings), status: e.status, created_at: e.created_at,
  started_at: e.started_at, ended_at: e.ended_at });
const sprintsFrom = (e: any): Row[] => (e.sprints ?? []).map((s: any) => ({ id: s.id, run_id: e.id, pool: s.pool, start_at: s.start_at, end_at: s.end_at, status: s.status }));
const scheduleOf = (run: Row, s: Row) => ({ id: `${plain(run.id)}.${plain(s.id)}`, project: plain(run.project_id) ?? "", mode: "balanced", model: null, effort: null,
  spend_ceiling: 0, start: plain(s.start_at), window: `${plain(s.start_at) ?? ""}/${plain(s.end_at) ?? ""}`, order: [],
  last_run: { slot: plain(s.id), started_at: plain(s.start_at), ended_at: plain(s.end_at), result: plain(s.status) } });

const questionToEntity = (r: Row) => ({ schema: "waypoint.question.v1", id: plain(r.id), project: plain(r.project_id), node: plain(r.node_id), kind: plain(r.kind),
  prompt: plain(r.prompt), options: jsonField(r.options_json), scale: plain(r.scale), answer: jsonField(r.answer_json), answered_at: plain(r.answered_at),
  captured: asBool(r.captured), allowed: jsonField(r.allowed_json), proposal: jsonField(r.proposal_json), purpose: plain(r.purpose),
  created_at: str(r.answered_at) ?? "1970-01-01T00:00:00Z", rev: 1, updated_at: isoOr(r.answered_at), updated_by: WRITER });
const questionFrom = (e: any): Row => ({ id: e.id, project_id: e.project, node_id: e.node, kind: e.kind, prompt: e.prompt, options_json: jsonBack(e.options),
  scale: e.scale, answer_json: jsonBack(e.answer), answered_at: e.answered_at, captured: fromBool(e.captured), allowed_json: jsonBack(e.allowed),
  proposal_json: jsonBack(e.proposal), purpose: e.purpose });

const claimToEntity = (r: Row) => {
  const since = isoOr(r.since);
  return { schema: "waypoint.claim.v1", project: plain(r.project_id), node: plain(r.node_id), owner: "mod", location: str(r.cwd) ?? str(r.worktree) ?? "unknown",
    token: sha256(`${canon(r.project_id)}/${canon(r.node_id)}/${since}`).slice(0, 16), heartbeat_at: isoOr(r.refreshed_at, since), since,
    claimed_by: plain(r.claimed_by), environment: plain(r.environment) };
};
const claimFrom = (e: any): Row => ({ project_id: e.project, node_id: e.node, claimed_by: e.claimed_by, worktree: null, cwd: e.location, environment: e.environment,
  since: e.since, refreshed_at: e.heartbeat_at });

const todoToEntity = (r: Row) => {
  const human = r.section === "human";
  return { schema: "waypoint.todo.v1", id: plain(r.id), text: plain(r.text), status: plain(r.status), category: human ? "hands_on" : "decisions",
    subtype: human ? "own" : "approval", link: plain(r.link), node: jsonField(r.node_json), project: null, order: plain(r.position), merged_into: plain(r.merged_into),
    click_count: 0, next_click: null, created_at: plain(r.created_at), created_by: plain(r.created_by), first_clicked_at: plain(r.first_clicked_at),
    archived_at: plain(r.archived_at), done_by: plain(r.done_by), handoffs: jsonField(r.handoffs_json), deliverables: jsonField(r.deliverables_json),
    rev: 1, updated_at: isoOr(r.updated_at), updated_by: str(r.created_by) ?? WRITER };
};
const todoFrom = (e: any): Row => ({ id: e.id, text: e.text, status: e.status, created_at: e.created_at, updated_at: e.updated_at === "1970-01-01T00:00:00Z" ? null : e.updated_at,
  created_by: e.created_by, handoffs_json: jsonBack(e.handoffs), merged_into: e.merged_into, section: e.category === "hands_on" ? "human" : e.category === "decisions" ? "computer" : null,
  position: e.order, link: e.link, node_json: jsonBack(e.node), deliverables_json: jsonBack(e.deliverables), first_clicked_at: e.first_clicked_at,
  archived_at: e.archived_at, done_by: e.done_by });

const eventToEntity = (r: Row, ordinal: number) => ({ seq: ordinal, src_seq: plain(r.seq), at: plain(r.at), project: plain(r.project_id), node: plain(r.node_id),
  kind: plain(r.kind), actor: plain(r.actor), summary: plain(r.summary), data: jsonField(r.data_json) });
const eventFrom = (e: any): Row => ({ seq: e.src_seq, at: e.at, project_id: e.project, node_id: e.node, kind: e.kind, actor: e.actor, summary: e.summary, data_json: jsonBack(e.data) });

/** Columns of `src` that `back` does not reproduce exactly: kept raw so nothing is lost. */
function overflow(cols: string[], src: Row, back: Row): Record<string, Cell> | null {
  const o: Record<string, Cell> = {};
  for (const c of cols) {
    const b = back[c] === undefined ? null : back[c];
    if (canon(b) !== canon(src[c])) o[c] = src[c] === undefined ? null : src[c];
  }
  return Object.keys(o).length ? o : null;
}
const withOverflow = (e: any, cols: string[], src: Row, back: Row) => {
  const o = overflow(cols, src, back);
  return o ? { ...e, legacy_cols: o } : e;
};
const rebuild = (e: any, from: (e: any) => Row, cols: string[]): Row => {
  const r = from(e);
  const out: Row = {};
  for (const c of cols) out[c] = (r[c] === undefined ? null : r[c]) as Cell;
  if (e.legacy_cols) for (const [k, v] of Object.entries(e.legacy_cols)) out[k] = v as Cell;
  return out;
};

// ---------------------------------------------------------------- manifest
export interface TableReport { source: number; migrated: number; dropped: number; fallback_columns: number; columns: string[]; files: string[] }
export interface Manifest {
  schema: "waypoint.migration_manifest.v1";
  migration: "1.1-sqlite-to-files";
  source_sha256: string;
  learn_sha256: string | null;
  machine: string;
  code_root: string;
  complete: boolean;
  tables: Record<string, TableReport>;
  floors_dropped: Record<string, number>;
  floors_file: string;
  file_state: Record<string, { source_dir: string; files: Record<string, string> }>;
  schedules: string[];
  mismatches: number;
}

export interface MigrateOptions {
  /** A COPY of cc.sqlite. Never the live file. */
  ccCopy: string;
  learnCopy?: string | null;
  dest: string;
  python?: string;
  machine?: string;
  codeRoot?: string;
  /** name -> source folder (receipts, questions, inbox, plans, config); copied under legacy/1.1/files/<name>/. */
  fileState?: Record<string, string>;
  fs?: FsAdapter;
}
export interface MigrateResult { manifest: Manifest; skipped: boolean; manifestPath: string }

function commonAncestor(paths: string[]): string {
  const segs = paths.map((p) => p.replace(/\\/g, "/").replace(/\/+$/, "").split("/"));
  if (!segs.length) return "";
  let n = segs[0].length;
  for (const s of segs) {
    let i = 0;
    while (i < n && i < s.length && s[i].toLowerCase() === segs[0][i].toLowerCase()) i++;
    n = i;
  }
  return segs[0].slice(0, n).join("/");
}

async function walkFiles(dir: string, base = ""): Promise<string[]> {
  let names: import("node:fs").Dirent[];
  try { names = await fsp.readdir(nodePath.join(dir, base), { withFileTypes: true }); } catch { return []; }
  const out: string[] = [];
  for (const d of names.sort((a, b) => (a.name < b.name ? -1 : 1))) {
    const rel = base ? `${base}/${d.name}` : d.name;
    if (d.isDirectory()) out.push(...(await walkFiles(dir, rel)));
    else if (d.isFile()) out.push(rel);
  }
  return out;
}

export async function sourceFileHashes(dir: string): Promise<Record<string, string>> {
  const out: Record<string, string> = {};
  for (const rel of await walkFiles(dir)) out[rel] = await sha256File(nodePath.join(dir, rel));
  return out;
}

export async function loadSource(python: string, ccCopy: string, learnCopy?: string | null): Promise<Tables> {
  const cc = await readSqlite(python, ccCopy, "cc");
  const learn = learnCopy ? await readSqlite(python, learnCopy, "learn") : {};
  for (const n of Object.keys(learn)) if (cc[n]) throw new Error(`table name appears in both databases: ${n}`);
  return { ...cc, ...learn };
}

export function mapContext(tables: Tables, o: { machine?: string; codeRoot?: string }): MapCtx {
  const cwds = (tables.projects?.rows ?? []).map((r) => r.cwd).filter((c): c is string => typeof c === "string" && /^[A-Za-z]:|^\//.test(c));
  return { machine: o.machine ?? os.hostname().toLowerCase(), codeRoot: (o.codeRoot ?? commonAncestor(cwds)).replace(/\\/g, "/") };
}

const MAPPED = ["projects", "nodes", "families", "runs", "sprints", "questions", "claims", "todos", "events", "meta"];

/** Where the claim for (project,node) lands: claims/<node>.json, or claims/<project>--<node>.json when two projects share a node id. */
function claimPaths(rows: Row[]): string[] {
  const seen = new Map<string, string>();
  return rows.map((r) => {
    const node = safeName(String(r.node_id));
    const proj = String(r.project_id);
    const holder = seen.get(node);
    if (holder === undefined) { seen.set(node, proj); return `claims/${node}.json`; }
    return holder === proj ? `claims/${node}.json` : `claims/${safeName(proj)}--${node}.json`;
  });
}

export async function migrate(o: MigrateOptions): Promise<MigrateResult> {
  for (const p of [o.ccCopy, o.learnCopy]) if (p && isLiveDbPath(p)) throw new Error(`refusing to read a live database path: ${p} (pass a copy)`);
  const python = o.python ?? defaultPython();
  const fs = o.fs ?? (await createNodeFsAdapter());
  const dest = nodePath.resolve(o.dest);
  const ccSha = await sha256File(o.ccCopy);
  const learnSha = o.learnCopy ? await sha256File(o.learnCopy) : null;
  const fileHashes: Record<string, Record<string, string>> = {};
  for (const [name, dir] of Object.entries(o.fileState ?? {})) fileHashes[name] = await sourceFileHashes(dir);

  const manifestPath = nodePath.join(dest, MANIFEST_REL);
  try {
    const old = JSON.parse(await fsp.readFile(manifestPath, "utf8")) as Manifest;
    const sameFiles = JSON.stringify(Object.fromEntries(Object.entries(old.file_state ?? {}).map(([k, v]) => [k, v.files]))) === JSON.stringify(fileHashes);
    if (old.complete && old.source_sha256 === ccSha && old.learn_sha256 === learnSha && sameFiles) return { manifest: old, skipped: true, manifestPath };
  } catch { /* first run, or an unreadable manifest: migrate again */ }

  const raw = await loadSource(python, o.ccCopy, o.learnCopy);
  const { tables, floorsDropped } = normalizeSource(raw);
  const ctx = mapContext(tables, o);
  const report: Record<string, TableReport> = {};
  const droppedRows = (t: string) => raw[t].rows.length - tables[t].rows.length;
  const put = async (rel: string, data: unknown) => {
    await atomicWrite(fs, nodePath.join(dest, rel), JSON.stringify(data, null, 2) + "\n", { maxBytes: MAX_BYTES, writer: WRITER });
    return rel;
  };
  const putText = async (rel: string, text: string) => {
    await atomicWrite(fs, nodePath.join(dest, rel), text, { maxBytes: MAX_BYTES, writer: WRITER });
    return rel;
  };
  const rep = (t: string, files: string[], fallback: number) => {
    report[t] = { source: raw[t].rows.length, migrated: tables[t].rows.length, dropped: droppedRows(t), fallback_columns: fallback, columns: tables[t].columns, files };
  };
  const rowsOf = (t: string) => tables[t]?.rows ?? [];
  const colsOf = (t: string) => tables[t]?.columns ?? [];
  let fb = 0;
  const wrap = (e: any, t: string, src: Row, from: (e: any) => Row) => {
    const out = withOverflow(e, colsOf(t), src, rebuild(e, from, colsOf(t)) as Row);
    if (out.legacy_cols) fb += Object.keys(out.legacy_cols).length;
    return out;
  };
  const schedules: string[] = [];

  // projects, nodes, families
  if (tables.projects) {
    fb = 0; const files: string[] = [];
    for (const r of rowsOf("projects")) files.push(await put(`projects/${safeName(String(r.id))}/project.json`, wrap(projectToEntity(r, ctx), "projects", r, (e) => projectFrom(e, ctx))));
    rep("projects", files, fb);
  }
  if (tables.nodes) {
    fb = 0; const files: string[] = [];
    for (const r of rowsOf("nodes")) files.push(await put(`projects/${safeName(String(r.project_id))}/nodes/${safeName(String(r.id))}.json`, wrap(nodeToEntity(r), "nodes", r, nodeFrom)));
    rep("nodes", files, fb);
  }
  if (tables.families) {
    fb = 0; const files: string[] = [];
    const projects = rowsOf("projects");
    for (const r of rowsOf("families")) {
      const members = projects.filter((p) => p.family === r.id).sort((a, b) => Number(a.version ?? 0) - Number(b.version ?? 0) || (String(a.created_at) < String(b.created_at) ? -1 : 1)).map((p) => String(p.id));
      files.push(await put(`families/${safeName(String(r.id))}.json`, wrap(familyToEntity(r, members), "families", r, familyFrom)));
    }
    rep("families", files, fb);
  }
  // runs and sprints (sprints ride inside their run and also become schedules)
  if (tables.runs || tables.sprints) {
    fb = 0; const files: string[] = [];
    const runs = rowsOf("runs"); const sprints = rowsOf("sprints");
    const runIds = new Set(runs.map((r) => r.id));
    for (const r of runs) {
      const mine = sprints.filter((s) => s.run_id === r.id);
      const e = runToEntity(r, mine);
      const back = { ...runFrom(e) } as Row;
      const out = withOverflow(e, colsOf("runs"), r, back);
      if (out.legacy_cols) fb += Object.keys(out.legacy_cols).length;
      // sprints that do not rebuild exactly are kept raw too
      const sprintBack = sprintsFrom(e);
      const sprintOver = mine.map((s, i) => overflow(colsOf("sprints"), s, sprintBack[i] ?? {}));
      if (sprintOver.some(Boolean)) { (out as any).legacy_sprint_cols = sprintOver; fb += sprintOver.filter(Boolean).length; }
      files.push(await put(`runs/${safeName(String(r.id))}.json`, out));
      for (const s of mine) schedules.push(await put(`schedules/${safeName(String(r.id))}.${safeName(String(s.id))}.json`, scheduleOf(r, s)));
    }
    if (tables.runs) rep("runs", files, fb);
    if (tables.sprints) {
      const orphans = sprints.filter((s) => !runIds.has(s.run_id));
      const sf: string[] = sprints.length - orphans.length ? [...files] : [];
      if (orphans.length) sf.push(await put("legacy/1.1/cc/sprints_orphan.json", { schema: "waypoint.legacy_table.v1", table: "sprints", columns: colsOf("sprints"), rows: orphans.map((s) => colsOf("sprints").map((c) => s[c])) }));
      rep("sprints", sf, 0);
    }
  }
  if (tables.questions) {
    fb = 0; const files: string[] = [];
    for (const r of rowsOf("questions")) files.push(await put(`questions/${safeName(String(r.id))}.json`, wrap(questionToEntity(r), "questions", r, questionFrom)));
    rep("questions", files, fb);
  }
  if (tables.claims) {
    fb = 0; const files: string[] = [];
    const rows = rowsOf("claims"); const paths = claimPaths(rows);
    for (let i = 0; i < rows.length; i++) files.push(await put(paths[i], wrap(claimToEntity(rows[i]), "claims", rows[i], claimFrom)));
    rep("claims", files, fb);
  }
  if (tables.todos) {
    fb = 0; const files: string[] = [];
    for (const r of rowsOf("todos")) files.push(await put(`todos/${safeName(String(r.id))}.json`, wrap(todoToEntity(r), "todos", r, todoFrom)));
    rep("todos", files, fb);
  }
  if (tables.events) {
    // events from different projects share seq values: group per project, keep the source seq in src_seq.
    fb = 0; const groups = new Map<string, string[]>();
    for (const r of rowsOf("events")) {
      const k = typeof r.project_id === "string" ? safeName(r.project_id) : "_none";
      const g = groups.get(k) ?? [];
      const e = wrap(eventToEntity(r, g.length + 1), "events", r, eventFrom);
      g.push(JSON.stringify(e));
      groups.set(k, g);
    }
    const files: string[] = [];
    for (const [k, lines] of [...groups].sort()) files.push(await putText(`events/${k}.ndjson`, lines.join("\n") + "\n"));
    rep("events", files, fb);
  }
  if (tables.meta) {
    fb = 0;
    const cols = colsOf("meta");
    const entries = rowsOf("meta").map((r) => {
      const e = { key: plain(r.key), value: plain(r.value) };
      const out = withOverflow(e, cols, r, e as any);
      if (out.legacy_cols) fb += Object.keys(out.legacy_cols).length;
      return out;
    });
    rep("meta", [await put("settings/meta_1_1.json", { schema: "waypoint.meta_1_1.v1", entries })], fb);
  }
  // every other table: lossless
  for (const [name, t] of Object.entries(tables)) {
    if (MAPPED.includes(name)) continue;
    const rel = `legacy/1.1/${t.database}/${safeName(name)}.json`;
    await put(rel, { schema: "waypoint.legacy_table.v1", table: name, database: t.database, columns: t.columns, rows: t.rows.map((r) => t.columns.map((c) => r[c])) });
    rep(name, [rel], 0);
  }
  // file-side state
  const fileState: Manifest["file_state"] = {};
  for (const [name, dir] of Object.entries(o.fileState ?? {})) {
    for (const rel of Object.keys(fileHashes[name])) {
      const target = nodePath.join(dest, "legacy/1.1/files", safeName(name), rel);
      await fsp.mkdir(nodePath.dirname(target), { recursive: true });
      await fsp.copyFile(nodePath.join(dir, rel), target);
    }
    fileState[name] = { source_dir: nodePath.resolve(dir), files: fileHashes[name] };
  }
  // floors: created unset, never overwritten
  const floorsRel = "settings/floors.json";
  if (!(await fs.exists(nodePath.join(dest, floorsRel)))) {
    await put(floorsRel, { pools: Object.fromEntries(FLOOR_POOLS.map((p) => [p, { min_percent: null, set_by: null, set_at: null }])) });
  }
  const manifest: Manifest = { schema: "waypoint.migration_manifest.v1", migration: "1.1-sqlite-to-files", source_sha256: ccSha, learn_sha256: learnSha,
    machine: ctx.machine, code_root: ctx.codeRoot, complete: true, tables: report, floors_dropped: floorsDropped, floors_file: floorsRel, file_state: fileState,
    schedules, mismatches: 0 };
  await put(MANIFEST_REL, manifest);
  return { manifest, skipped: false, manifestPath };
}

// ---------------------------------------------------------------- reading the folder back (used by roundtrip_check.ts)
export async function rebuildTables(dest: string, m: Manifest): Promise<Record<string, Row[]>> {
  const out: Record<string, Row[]> = {};
  const readJson = async (rel: string) => JSON.parse(await fsp.readFile(nodePath.join(dest, rel), "utf8"));
  const ctx: MapCtx = { machine: m.machine, codeRoot: m.code_root };
  for (const [name, t] of Object.entries(m.tables)) {
    const rows: Row[] = [];
    const cols = t.columns;
    if (name === "projects") for (const f of t.files) rows.push(rebuild(await readJson(f), (e) => projectFrom(e, ctx), cols));
    else if (name === "nodes") for (const f of t.files) rows.push(rebuild(await readJson(f), nodeFrom, cols));
    else if (name === "families") for (const f of t.files) rows.push(rebuild(await readJson(f), familyFrom, cols));
    else if (name === "runs") for (const f of t.files) rows.push(rebuild(await readJson(f), runFrom, cols));
    else if (name === "sprints") {
      for (const f of t.files) {
        if (f.startsWith("legacy/")) { const d = await readJson(f); for (const r of d.rows) rows.push(Object.fromEntries(d.columns.map((c: string, i: number) => [c, r[i]])) as Row); continue; }
        const e = await readJson(f);
        const back = sprintsFrom(e);
        (e.sprints ?? []).forEach((_: unknown, i: number) => {
          const r: Row = {}; for (const c of cols) r[c] = (back[i][c] === undefined ? null : back[i][c]) as Cell;
          for (const [k, v] of Object.entries(e.legacy_sprint_cols?.[i] ?? {})) r[k] = v as Cell;
          rows.push(r);
        });
      }
    } else if (name === "questions") for (const f of t.files) rows.push(rebuild(await readJson(f), questionFrom, cols));
    else if (name === "claims") for (const f of t.files) rows.push(rebuild(await readJson(f), claimFrom, cols));
    else if (name === "todos") for (const f of t.files) rows.push(rebuild(await readJson(f), todoFrom, cols));
    else if (name === "events") {
      for (const f of t.files) for (const line of (await fsp.readFile(nodePath.join(dest, f), "utf8")).split("\n")) if (line.trim()) rows.push(rebuild(JSON.parse(line), eventFrom, cols));
    } else if (name === "meta") {
      const d = await readJson(t.files[0]);
      for (const e of d.entries) rows.push(rebuild(e, (x) => ({ key: x.key, value: x.value }), cols));
    } else {
      for (const f of t.files) { const d = await readJson(f); for (const r of d.rows) rows.push(Object.fromEntries(d.columns.map((c: string, i: number) => [c, r[i]])) as Row); }
    }
    out[name] = rows;
  }
  return out;
}
export { rowKey };

// ---------------------------------------------------------------- CLI
export async function main(argv: string[]): Promise<number> {
  const a = new Map<string, string>();
  const files: Record<string, string> = {};
  for (let i = 0; i < argv.length; i++) {
    if (!argv[i].startsWith("--")) continue;
    const k = argv[i].slice(2); const v = argv[i + 1]?.startsWith("--") || argv[i + 1] === undefined ? "true" : argv[++i];
    if (k === "files") { const [n, ...d] = v.split("="); files[n] = d.join("="); } else a.set(k, v);
  }
  if (!a.get("source") || !a.get("dest")) { console.error("usage: migrate_1_1.ts --source <cc copy> [--learn <learn copy>] --dest <folder> [--python p] [--machine m] [--code-root r] [--files name=dir]"); return 2; }
  const r = await migrate({ ccCopy: a.get("source")!, learnCopy: a.get("learn") ?? null, dest: a.get("dest")!, python: a.get("python"), machine: a.get("machine"), codeRoot: a.get("code-root"), fileState: files });
  console.log(JSON.stringify({ skipped: r.skipped, manifest: r.manifestPath, tables: Object.fromEntries(Object.entries(r.manifest.tables).map(([k, v]) => [k, v.migrated])) }));
  return 0;
}
if (import.meta.main) {
  main(process.argv.slice(2)).then((c) => process.exit(c), (e) => { console.error(String(e?.message ?? e)); process.exit(1); });
}
