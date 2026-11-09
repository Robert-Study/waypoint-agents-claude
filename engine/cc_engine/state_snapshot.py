"""Plan state travels with the repo: ``plans/state/<project>.json`` (R2).

After every board change a small snapshot is written (project, per task: status, attempts, actor,
last evidence line, claim, plus the plan itself) so ``Backup-Push`` carries it to the private GitHub
backup. On another machine ``resume`` imports it: done and failed tasks keep their state, and tasks that
were running or submitted are set to ready (or back to waiting when a dependency is unfinished), so at
most the in-progress work is lost. Claims never travel as live: the new machine's workers claim again.

- ``snapshot_project(store, project_id)`` -> dict (pure read).
- ``write_snapshot(store, project_id, root=None)`` -> path written, or ``None`` when nothing changed.
- ``Snapshotter(store, root)``: subscribes to ``store.subscribe`` and also polls the revision, so
  changes made by other processes (the scheduled tick, ``claim`` CLI) are picked up. It writes only
  when a file's content changes.
- ``attach(store, root=None)``: idempotent. Does nothing for a scratch database (anything but the live
  ``data/cc.sqlite``) unless ``root`` is given, so tests never write into ``plans/state``.
  Importing this module inside a running engine (api.py loads it as an extension) attaches to that
  engine's store; a route call does the same.
- ``resume(store, path_or_dict)`` -> summary. CLI: ``python -m cc_engine snapshot [--db P] [--out DIR]
  [--project ID]`` writes; ``snapshot --resume FILE [--db P]`` imports.

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
from pathlib import Path

from . import REPO_ROOT, default_db_path, graph, plan
from .store import now_iso

SCHEMA = "state_snapshot.v1"
DEFAULT_ROOT = REPO_ROOT / "plans" / "state"
EVIDENCE_MAX = 300
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
IN_PROGRESS = ("running", "submitted")
ACTOR = "state_snapshot"
_OPTIONAL = ("pool", "model", "effort", "recommended")


def default_root() -> Path:
    """``CC_SNAPSHOT_DIR``, else ``<repo>/plans/state`` when the repo has a ``plans`` folder (snapshots
    travel with that repo), else ``<data dir>/state`` (a product clone keeps them out of its tracked files)."""
    named = os.environ.get("CC_SNAPSHOT_DIR")
    if named:
        return Path(named)
    if DEFAULT_ROOT.parent.is_dir():
        return DEFAULT_ROOT
    from . import config
    return Path(config.data_dir()) / "state"


def snapshot_path(project_id, root=None) -> Path:
    if not isinstance(project_id, str) or not ID_RE.match(project_id):
        raise ValueError(f"unsafe project id {project_id!r}")
    return Path(root or default_root()) / f"{project_id}.json"


def _last_line(text):
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    return lines[-1][:EVIDENCE_MAX] if lines else None


def snapshot_project(store, project_id) -> dict:
    project = store.get_project(project_id)
    if project is None:
        raise KeyError(f"no project {project_id}")
    claims = store.claims(project_id)
    nodes = []
    for n in store.list_nodes(project_id):
        item = {"id": n["id"], "title": n["title"], "goal": n["goal"], "check": n["check"],
                "deps": n["deps"], "owner": n["owner"], "lane": n["lane"],
                "status": n["status"], "attempts": n["attempts"], "actor": n["actor"],
                "evidence": _last_line(n.get("evidence")), "claim": None}
        for k in _OPTIONAL:
            if n.get(k) is not None:
                item[k] = n[k]
        c = claims.get(n["id"])
        if c:
            item["claim"] = {k: c.get(k) for k in ("by", "worktree", "cwd", "environment", "since")}
        nodes.append(item)
    fam = project.get("family")
    frow = store._q("SELECT title FROM families WHERE id=?", (fam,)) if fam else []
    return {"schema": SCHEMA,
            "project": {"id": project["id"], "title": project["title"], "brief": project["brief"],
                        "cwd": project["cwd"], "created": project["created_at"],
                        "status": project.get("status") or "active",
                        "family": fam or None, "family_title": frow[0][0] if frow else None,
                        "version": project.get("version"), "archived": bool(int(project.get("archived") or 0))},
            "revision": store.revision(), "nodes": nodes}


def _dump(snap) -> str:
    return json.dumps(snap, indent=1, ensure_ascii=False, sort_keys=True) + "\n"


def write_snapshot(store, project_id, root=None):
    """Write the project's snapshot; ``None`` when the file already has this exact content."""
    path = snapshot_path(project_id, root)
    proj = store.get_project(project_id) or {}
    if proj.get("archived") and not path.exists():
        return None  # archived projects keep an existing snapshot current but never start a new one
    text = _dump(snapshot_project(store, project_id))
    try:
        if path.read_text(encoding="utf-8") == text:
            return None
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return path


def write_all(store, root=None) -> list:
    out = []
    for p in store.list_projects(include_archived=True):
        written = write_snapshot(store, p["id"], root)
        if written:
            out.append(written)
    return out


# ---------------------------------------------------------------- follow the board
class Snapshotter:
    """Writes snapshots when the board changes. ``start=False`` lets tests drive ``flush`` by hand."""

    def __init__(self, store, root=None, debounce=0.5, poll=2.0, start=True):
        self.store = store
        self.root = Path(root) if root else default_root()
        self.debounce, self.poll = debounce, poll
        self._dirty: set = set()
        self._all = False
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._closed = threading.Event()
        self._seen = store.revision()
        self._unsub = store.subscribe(self._on_store)
        self._thread = None
        if start:
            self._thread = threading.Thread(target=self._run, name="state-snapshot", daemon=True)
            self._thread.start()

    def _on_store(self, kind, payload):
        if kind != "state":
            return
        with self._lock:
            pid = (payload or {}).get("project")
            if pid:
                self._dirty.add(pid)
            else:
                self._all = True
        self._wake.set()

    def flush(self, check_revision=False) -> list:
        if check_revision:
            rev = self.store.revision()
            if rev != self._seen:
                with self._lock:
                    self._all = True
        with self._lock:
            ids, everything = set(self._dirty), self._all
            self._dirty.clear()
            self._all = False
        if everything:
            ids = {p["id"] for p in self.store.list_projects(include_archived=True)}
        self._seen = self.store.revision()
        written = []
        for pid in sorted(ids):
            try:
                path = write_snapshot(self.store, pid, self.root)
            except (KeyError, OSError, ValueError):
                continue
            if path:
                written.append(path)
        return written

    def _run(self):
        while not self._closed.is_set():
            self._wake.wait(self.poll)
            if self._closed.is_set():
                break
            if self._wake.is_set():
                self._wake.clear()
                self._closed.wait(self.debounce)  # coalesce a burst of changes
            try:
                self.flush(check_revision=True)
            except Exception:  # a snapshot problem must never hurt the engine
                pass

    def close(self):
        self._closed.set()
        self._wake.set()
        try:
            self._unsub()
        except Exception:
            pass
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)


_ATTACHED: dict = {}
_ATTACH_LOCK = threading.Lock()


def _is_live_store(store) -> bool:
    try:
        return Path(store.path).resolve() == Path(default_db_path()).resolve()
    except OSError:
        return False


def attach(store, root=None):
    """Start following ``store`` (idempotent). Returns the ``Snapshotter`` or ``None`` (scratch store)."""
    if root is None and not _is_live_store(store):
        return None
    with _ATTACH_LOCK:
        snapper = _ATTACHED.get(id(store))
        if snapper is None:
            snapper = _ATTACHED[id(store)] = Snapshotter(store, root)
            snapper._on_store("state", {"project": None})  # first pass writes whatever is missing
        return snapper


def _attach_to_calling_engine():
    """api.py imports this module from ``App.extension_routes``; find that App and follow its store."""
    try:
        frame = sys._getframe(2)
        for _ in range(12):
            if frame is None:
                return
            app = frame.f_locals.get("self")
            if app is not None and hasattr(app, "store") and hasattr(app, "import_module"):
                attach(app.store)
                return
            frame = frame.f_back
    except Exception:
        return


# ---------------------------------------------------------------- resume
def _load(path_or_dict) -> dict:
    if isinstance(path_or_dict, dict):
        snap = path_or_dict
    else:
        try:
            snap = json.loads(Path(path_or_dict).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"cannot read snapshot {path_or_dict}: {exc}") from exc
    if not isinstance(snap, dict) or snap.get("schema") != SCHEMA:
        raise ValueError(f"not a {SCHEMA} snapshot")
    project = snap.get("project")
    if not isinstance(project, dict) or not ID_RE.match(str(project.get("id") or "")):
        raise ValueError("snapshot needs a safe project id")
    if not isinstance(snap.get("nodes"), list):
        raise ValueError("snapshot needs a nodes list")
    return snap


def _plan_from(snap) -> dict:
    p = snap["project"]
    nodes = []
    for n in snap["nodes"]:
        item = {k: n.get(k) for k in ("id", "title", "goal", "check", "deps", "owner", "lane")}
        item["deps"] = list(item["deps"] or [])
        for k in _OPTIONAL:
            if n.get(k) is not None:
                item[k] = n[k]
        nodes.append(item)
    return {"id": p["id"], "title": p.get("title"), "brief": p.get("brief") or "", "cwd": p.get("cwd"),
            "created": p.get("created"), "nodes": nodes}


def _restore_family(store, pid, sp, archived):
    """Put a resumed project back into its family (and archive it when the snapshot says so)."""
    fam, title = str(sp["family"]), sp.get("family_title") or str(sp["family"])

    def fn(c):
        c.execute("INSERT OR IGNORE INTO families(id, title, created_at) VALUES(?,?,?)", (fam, title, now_iso()))
        c.execute("UPDATE projects SET family=?, version=? WHERE id=?", (fam, sp.get("version"), pid))
        if archived and sp.get("archived"):
            c.execute("UPDATE projects SET archived=1 WHERE id=?", (pid,))

    store.mutate(None, fn, ACTOR, f"Resumed '{pid}' as version {sp.get('version')} of '{title}'.", project_id=pid,
                 kind="resume")


def resume(store, path_or_dict) -> dict:
    """Import a snapshot. Creates the project when missing; never undoes local progress.

    Previously running or submitted tasks become ``ready`` (all dependencies done) or ``not_started``.
    """
    snap = _load(path_or_dict)
    pid = snap["project"]["id"]
    plan_dict = _plan_from(snap)
    plan.validate_plan(plan_dict)
    existed = store.get_project(pid) is not None
    if not existed:
        plan.import_plan(store, plan_dict, actor="import", draft=False)
        if snap["project"].get("status") == "draft":
            store.mutate(None, lambda c: c.execute("UPDATE projects SET status='draft' WHERE id=?", (pid,)),
                         ACTOR, f"Resumed '{pid}' as a draft; it waits for Owner to accept it.", project_id=pid)
    sp = snap["project"]
    if sp.get("family") and (not existed or not (store.get_project(pid) or {}).get("family")):
        _restore_family(store, pid, sp, archived=(not existed))
    local = {n["id"]: n for n in store.list_nodes(pid)}
    target = {n["id"]: n.get("status") or "not_started" for n in snap["nodes"]}

    def settled(nid):  # status after resume, for the dependency test
        s = target.get(nid)
        return s if s not in IN_PROGRESS else "pending"

    restore, ready, waiting, kept, missing = [], [], [], [], []
    by_id = {n["id"]: n for n in snap["nodes"]}
    for sn in snap["nodes"]:
        nid = sn["id"]
        cur = local.get(nid)
        if cur is None:
            missing.append(nid)
            continue
        status = target[nid]
        if cur["status"] not in ("not_started", "ready") and cur["status"] != status:
            kept.append(nid)  # local work already moved on: never undo it
            continue
        if status in IN_PROGRESS:
            deps_done = all(settled(d) == "done" for d in by_id[nid].get("deps") or [])
            status = "ready" if deps_done else "not_started"
            (ready if deps_done else waiting).append(nid)
        if status == cur["status"] and sn.get("evidence") == cur.get("evidence"):
            continue
        restore.append((nid, status, int(sn.get("attempts") or 0), sn.get("actor"), sn.get("evidence")))
    if restore:
        stamp = now_iso()

        def fn(c):
            for nid, status, attempts, actor, evidence in restore:
                c.execute("UPDATE nodes SET status=?, attempts=?, actor=?, evidence=?, updated_at=?"
                          " WHERE project_id=? AND id=?", (status, attempts, actor, evidence, stamp, pid, nid))

        store.mutate(None, fn, ACTOR,
                     f"Resumed '{pid}' from a snapshot: {len(restore)} tasks restored, "
                     f"{len(ready)} that were running are ready again.",
                     project_id=pid, kind="resume", data={"snapshot_revision": snap.get("revision")})
        for nid in ready + waiting:
            store.event(pid, nid, "resume", ACTOR,
                        f"Task {nid} was running when the snapshot was taken; it starts again.", {"node": nid})
    return {"project": pid, "created": not existed, "restored": [r[0] for r in restore],
            "reset_to_ready": ready, "reset_to_waiting": waiting, "kept_local": kept,
            "missing": missing, "snapshot_revision": snap.get("revision")}


# ---------------------------------------------------------------- routes
def _http_error(status, body):
    from .api import HttpError  # lazy: api imports extension modules
    return HttpError(status, body)


def g_state_snapshot(h, q):
    """Where the snapshots live and which projects have one."""
    store = h.app.store
    snapper = attach(store)
    root = snapper.root if snapper else default_root()
    files = sorted(p.name for p in root.glob("*.json")) if root.is_dir() else []
    return {"root": str(root), "following": snapper is not None, "files": files}


def p_state_snapshot_write(h, q):
    """{project?} -> writes now (owner or router; harmless: the repo copy of the board)."""
    store = h.app.store
    b = h._json_body()
    root = default_root()
    try:
        written = [write_snapshot(store, b["project"], root)] if b.get("project") else write_all(store, root)
    except KeyError:
        raise _http_error(404, {"error": "not_found", "detail": "no such project"})
    except ValueError as exc:
        raise _http_error(400, {"error": "bad_request", "detail": str(exc)})
    return {"ok": True, "written": [str(p) for p in written if p]}


def p_state_snapshot_resume(h, q):
    """{path} or {snapshot} -> import a snapshot (owner only: it restores task states)."""
    if getattr(h, "actor", None) != "owner":
        raise _http_error(403, {"error": "owner_only", "detail": "only Owner may resume from a snapshot"})
    b = h._json_body()
    src = b.get("snapshot") if isinstance(b.get("snapshot"), dict) else b.get("path")
    if not src:
        raise _http_error(400, {"error": "bad_request", "detail": "missing path or snapshot"})
    try:
        return {"ok": True, **resume(h.app.store, src)}
    except ValueError as exc:
        raise _http_error(400, {"error": "bad_request", "detail": str(exc)})


ROUTES = {
    "GET /api/state_snapshot": g_state_snapshot,
    "POST /api/state_snapshot/write": p_state_snapshot_write,
    "POST /api/state_snapshot/resume": p_state_snapshot_resume,
}

_attach_to_calling_engine()


# ---------------------------------------------------------------- CLI
def cli(argv) -> int:
    ap = argparse.ArgumentParser(prog="cc_engine snapshot",
                                 description="Write plans/state/<project>.json, or --resume a snapshot file.")
    ap.add_argument("--db", default=None)
    ap.add_argument("--out", default=None, help="snapshot folder (default plans/state)")
    ap.add_argument("--project", default=None, help="one project (default: all)")
    ap.add_argument("--resume", default=None, metavar="FILE", help="import this snapshot instead of writing")
    args = ap.parse_args(argv)
    from .store import Store

    store = Store(args.db or default_db_path())
    try:
        if args.resume:
            try:
                out = resume(store, args.resume)
            except ValueError as exc:
                print(f"cannot resume: {exc}", file=sys.stderr)
                return 1
            print(f"resumed {out['project']}: {len(out['restored'])} tasks restored, "
                  f"{len(out['reset_to_ready'])} running task(s) ready again, "
                  f"{len(out['kept_local'])} kept as is")
            return 0
        root = Path(args.out) if args.out else default_root()
        try:
            written = [write_snapshot(store, args.project, root)] if args.project else write_all(store, root)
        except (KeyError, ValueError) as exc:
            print(f"cannot snapshot: {exc}", file=sys.stderr)
            return 1
        written = [p for p in written if p]
        print(f"wrote {len(written)} snapshot(s) to {root}" if written else f"snapshots in {root} are up to date")
        return 0
    finally:
        store.close()
