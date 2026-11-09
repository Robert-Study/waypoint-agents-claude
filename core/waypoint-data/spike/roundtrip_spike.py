"""Spike: prove every table of the live Waypoint 1.1 SQLite DB exports to JSON files and reads back identically.

Python is used only inside this spike. The live DB is opened read-only (never for write); the work happens on a copy
made with the sqlite backup API under <system temp>/waypoint-spike/.

    python -I roundtrip_spike.py            build the copy, export, read back, compare, write spike_report.json
    python -I roundtrip_spike.py --check    validate spike_report.json (exit 0 only when complete, fresh and green)
"""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPORT = HERE / "spike_report.json"
WORK = Path(tempfile.gettempdir()) / "waypoint-spike"
COPY = WORK / "cc_copy.sqlite"
LEARN_COPY = WORK / "learn_copy.sqlite"
EXPORT = WORK / "export"
REQUIRED_TOP = ("copy_path", "wal_checkpoint", "copy_failures", "tables", "sqlite_reader", "node_sqlite_reads_copy")
REQUIRED_WAL = ("mode", "busy", "log_frames", "checkpointed_frames", "ok")
EXPECTED_CORE = ("projects", "nodes", "events", "runs", "sprints", "questions", "families", "claims", "meta")


def _repo_roots():
    for p in [HERE] + list(HERE.parents):
        if (p / "engine" / "cc_engine").is_dir():
            yield p
            break
    try:
        common = subprocess.run(["git", "-C", str(HERE), "rev-parse", "--git-common-dir"], capture_output=True,
                                text=True, timeout=20).stdout.strip()
        if common:
            yield (HERE / common).resolve().parent
    except Exception:  # noqa: BLE001
        pass


def locate_live(name, env):
    if os.environ.get(env):
        return Path(os.environ[env])
    if name == "learn.sqlite" and os.environ.get("CC_DATA_DIR"):
        return Path(os.environ["CC_DATA_DIR"]) / name
    for root in _repo_roots():
        cand = root / "data" / name
        if cand.exists():
            return cand
    raise FileNotFoundError(f"live {name} not found (set {env})")


def backup_copy(src, dst):
    """Read-only source connection, sqlite backup API, then checkpoint the copy so no WAL sidecar is needed."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    for sfx in ("", "-wal", "-shm", "-journal"):
        Path(str(dst) + sfx).unlink(missing_ok=True)
    s = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True, timeout=30)
    d = sqlite3.connect(str(dst))
    try:
        s.backup(d)
        d.commit()
        busy, log, ckpt = d.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        d.execute("PRAGMA journal_mode=DELETE")  # copy becomes a single self-contained file
        integrity = d.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        d.close()
        s.close()
    sidecars = [x for x in ("-wal", "-shm") if Path(str(dst) + x).exists() and Path(str(dst) + x).stat().st_size]
    return {"mode": "TRUNCATE", "busy": busy, "log_frames": log, "checkpointed_frames": ckpt,
            "ok": busy == 0 and log == ckpt and not sidecars and integrity == "ok", "integrity_check": integrity}


def tables_of(con):
    return list(con.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"))


def enc(v):
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"$blob": bytes(v).hex()}
    if isinstance(v, float):
        return {"$float": repr(v)}  # lossless, and survives values JSON cannot hold
    return v


def dec(v):
    if isinstance(v, dict):
        if "$blob" in v:
            return bytes.fromhex(v["$blob"])
        if "$float" in v:
            return float(v["$float"])
    return v


def row_hash(row):
    """Type-aware: (tag, value) per column, so 1, 1.0 and '1' all differ."""
    parts = []
    for v in row:
        if v is None:
            parts.append(["n"])
        elif isinstance(v, int):
            parts.append(["i", str(v)])
        elif isinstance(v, float):
            parts.append(["f", repr(v)])
        elif isinstance(v, str):
            parts.append(["t", v])
        else:
            parts.append(["b", bytes(v).hex()])
    return hashlib.sha256(json.dumps(parts, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def table_hash(rows):
    h = hashlib.sha256()
    for r in rows:
        h.update(row_hash(r).encode())
    return h.hexdigest()


def export_table(con, db_label, name, out_dir):
    cols = [{"name": c[1], "type": c[2]} for c in con.execute(f'PRAGMA table_info("{name}")')]
    try:
        rows = con.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()
    except sqlite3.OperationalError:  # WITHOUT ROWID table
        rows = con.execute(f'SELECT * FROM "{name}"').fetchall()
    path = out_dir / f"{name}.json"
    doc = {"table": name, "database": db_label, "columns": cols, "rows": [[enc(v) for v in r] for r in rows]}
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return cols, rows, path


def read_back(path):
    doc = json.loads(path.read_text(encoding="utf-8"))
    return [tuple(dec(v) for v in r) for r in doc["rows"]]


NODE_SCRIPT = """
const { DatabaseSync } = require('node:sqlite');
const db = new DatabaseSync(process.argv[2], { readOnly: true });
const out = {};
for (const t of db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").all()) {
  out[t.name] = db.prepare('SELECT COUNT(*) AS n FROM "' + t.name + '"').get().n;
}
console.log(JSON.stringify(out));
"""


def node_reads(path, expected):
    node = shutil.which("node")
    if not node:
        return False, "node not found"
    script = WORK / "node_reader.cjs"
    script.write_text(NODE_SCRIPT, encoding="utf-8")
    try:
        r = subprocess.run([node, "--no-warnings", str(script), str(path)], capture_output=True, text=True, timeout=60)
        if r.returncode:
            return False, r.stderr.strip()[-300:]
        got = json.loads(r.stdout.strip().splitlines()[-1])
        return got == expected, "" if got == expected else "row counts differ"
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:300]


def build():
    WORK.mkdir(parents=True, exist_ok=True)
    if EXPORT.exists():
        shutil.rmtree(EXPORT)
    EXPORT.mkdir(parents=True)
    live = locate_live("cc.sqlite", "CC_DB")
    learn_live = locate_live("learn.sqlite", "CC_LEARN_DB")
    wal = backup_copy(live, COPY)
    learn_wal = backup_copy(learn_live, LEARN_COPY)

    failures, tables, counts = [], {}, {}
    src = sqlite3.connect(f"file:{live.as_posix()}?mode=ro", uri=True, timeout=30)
    src_tables = dict(tables_of(src))
    src.close()
    for label, path, expect in (("cc", COPY, src_tables), ("learn", LEARN_COPY, None)):
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        got = tables_of(con)
        if expect is not None:
            diff = set(expect) ^ {n for n, _ in got}
            if diff:
                failures.append(f"table list differs: {sorted(diff)}")
            failures += [f"schema differs: {n}" for n, sql in got if expect.get(n) != sql]
        out_dir = EXPORT / label
        out_dir.mkdir()
        for name, _sql in got:
            cols, rows, p = export_table(con, label, name, out_dir)
            back = read_back(p)
            h_db, h_back = table_hash(rows), table_hash(back)
            rowwise = len(rows) == len(back) and all(row_hash(a) == row_hash(b) for a, b in zip(rows, back))
            if name in tables:
                failures.append(f"duplicate table name across databases: {name}")
            tables[name] = {"database": label, "rows": len(rows), "columns": cols, "hash": h_db,
                            "hash_match": bool(h_db == h_back and rowwise)}
            if label == "cc":
                counts[name] = len(rows)
        con.close()
    for t in EXPECTED_CORE:
        if t not in tables:
            failures.append(f"expected table missing: {t}")
    ok_node, node_err = node_reads(COPY, counts)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "script_mtime_ns": Path(__file__).stat().st_mtime_ns,
        "live_source_opened": "read-only (mode=ro)",
        "copy_path": str(COPY),
        "learn_copy_path": str(LEARN_COPY),
        "export_dir": str(EXPORT),
        "wal_checkpoint": wal,
        "learn_wal_checkpoint": learn_wal,
        "copy_failures": failures,
        "tables": tables,
        "sqlite_reader": "python",
        "node_sqlite_reads_copy": bool(ok_node),
        "node_note": node_err or "node:sqlite (built in) read the copy read-only and row counts matched",
    }
    REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def check():
    errs = []
    if not REPORT.exists():
        print("FAIL: spike_report.json missing; run without --check first")
        return 1
    try:
        r = json.loads(REPORT.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: report unreadable: {e}")
        return 1
    miss = [k for k in REQUIRED_TOP if k not in r]
    if miss:
        print("FAIL: missing field " + ", ".join(miss))
        return 1
    errs += [f"missing wal_checkpoint.{k}" for k in REQUIRED_WAL if k not in r["wal_checkpoint"]]
    cp = Path(r["copy_path"])
    try:
        cp.resolve().relative_to(Path(tempfile.gettempdir()).resolve())
    except ValueError:
        errs.append("copy_path not under system temp")
    if not cp.exists():
        errs.append("copy_path file missing (stale report)")
    if r.get("script_mtime_ns") != Path(__file__).stat().st_mtime_ns:
        errs.append("report older than script (stale); rerun")
    if r["copy_failures"]:
        errs.append(f"copy_failures: {r['copy_failures']}")
    if r["wal_checkpoint"].get("ok") is not True:
        errs.append("wal_checkpoint.ok is not true")
    if r["sqlite_reader"] not in ("python", "node"):
        errs.append("sqlite_reader invalid")
    if not isinstance(r["node_sqlite_reads_copy"], bool):
        errs.append("node_sqlite_reads_copy not bool")
    if not r["tables"]:
        errs.append("no tables")
    errs += [f"table {n} absent" for n in EXPECTED_CORE if n not in r["tables"]]
    for n, t in r["tables"].items():
        if not (isinstance(t.get("rows"), int) and t.get("columns") and "hash_match" in t):
            errs.append(f"table {n} incomplete")
        elif t["hash_match"] is not True:
            errs.append(f"table {n} hash_match false")
    if errs:
        print("FAIL: " + "; ".join(errs))
        return 1
    print(f"OK: {len(r['tables'])} tables, {sum(t['rows'] for t in r['tables'].values())} rows, all hash_match; "
          f"wal ok; node_sqlite_reads_copy={r['node_sqlite_reads_copy']}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    if a.check:
        return check()
    rep = build()
    print(f"built report: {len(rep['tables'])} tables, failures={len(rep['copy_failures'])}")
    return check()


if __name__ == "__main__":
    sys.exit(main())
