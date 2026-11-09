"""Cloud sync inbox (waypoint-v5 Y1). Format: ``docs/v5/cloud_sync_format.md``.

A cloud session cannot reach the local engine, so it pushes one JSON file per update to
``sync/inbox/`` in the private repo. ``import_inbox(store, repo_dir)`` applies the items that are
revision-consistent and never undoes local progress; everything else becomes a conflict event, a
``sync/conflicts/<file>.json`` entry and (when Owner must decide) a human to-do. Processed files
move to ``sync/processed/``, so a re-run does nothing new. No git here; the CLI pulls only on --pull.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import todo
from .store import VERSION_ONLY_STATUSES, NotAllowed, StaleRevision, now_iso

SCHEMA = "waypoint.sync.v1"
ACTOR = "cloud-sync"
# How far along a status is. Cloud may only move a node forward; done needs a check or Owner.
# ``skipped`` and ``carried`` are deliberately absent: they are closed by a version roll-over and cloud sync
# never changes such a task (see ``_apply_status``), so they have no rank to compare against.
RANK = {"not_started": 0, "ready": 0, "needs_you": 1, "error": 1, "running": 2, "submitted": 3, "done": 4}
DEFAULT_REPO = Path(__file__).resolve().parents[2]


def _dirs(repo_dir):
    base = Path(repo_dir) / "sync"
    return base / "inbox", base / "processed", base / "conflicts"


def _conflict(store, item, why, project=None, node=None, needs_robert=True):
    entry = {"item": item, "reason": why}
    store.event(project_id=project, node_id=node, kind="sync_conflict", actor=ACTOR,
                summary=f"Cloud update not applied: {why}", data=entry)
    if needs_robert:
        try:
            todo.add_todo(store, f"Cloud sync conflict: {why}", actor=ACTOR, section="human",
                          node={"project": project, "id": node} if project and node else None)
        except Exception:  # a to-do failure must never stop the import
            pass
    return entry


def _changed_since(store, project, node, base):
    """True if a revision-bumping event touched the node after ``base`` revision, or a version event
    (carry or skip) did. One indexed query; the event log is never loaded."""
    return store.node_changed_since(project, node, base)


def _apply_status(store, session, it):
    project, node, target = it.get("project"), it.get("id"), it.get("status")
    base = it.get("base_revision")
    if not project or not node or target not in RANK or isinstance(base, bool) or not isinstance(base, int):
        return "conflict", _conflict(store, it, "node_status item is missing project, id, status or base_revision",
                                     project, node, needs_robert=False)
    cur = store.get_node(project, node)
    if cur is None:
        return "conflict", _conflict(store, it, f"no task {project}/{node}", project, node, needs_robert=False)
    status = cur["status"]
    if status in VERSION_ONLY_STATUSES:  # skipped or carried: closed by a version roll-over
        return "conflict", _conflict(store, it, f"{project}/{node} is {status} (closed by a version roll-over); "
                                     f"cloud says {target}, which would reopen it", project, node,
                                     needs_robert=False)
    proj = store.get_project(project)
    if proj is not None and int(proj.get("archived") or 0):
        return "conflict", _conflict(store, it, f"{project} is archived (an earlier version); cloud says "
                                     f"{node} is {target}, which cloud sync never applies", project, node,
                                     needs_robert=False)
    if target == "done":
        target = "submitted"  # only a passing check or Owner marks done
    if status == target:
        return "skipped", None
    if RANK[target] < RANK.get(status, 0):
        return "conflict", _conflict(store, it, f"{project}/{node} is already {status}; cloud says {target}, "
                                     "which would undo local progress", project, node)
    if _changed_since(store, project, node, base):
        return "conflict", _conflict(store, it, f"{project}/{node} changed locally after revision {base}; "
                                     f"cloud wanted {target}", project, node)
    try:
        store.set_node_status(project, node, target, f"{ACTOR}:{session}", evidence=it.get("evidence"),
                              summary=f"Cloud session {session} reported {node} as {target.replace('_', ' ')}.",
                              expected_revision=None, data={"source": "cloud_sync", "base_revision": base})
    except (NotAllowed, StaleRevision, ValueError, KeyError) as exc:
        return "conflict", _conflict(store, it, f"could not apply: {exc}", project, node)
    return "applied", None


def _apply_item(store, session, it):
    if not isinstance(it, dict):
        return "conflict", _conflict(store, it, "item is not an object", needs_robert=False)
    kind = it.get("kind")
    if kind == "node_status":
        return _apply_status(store, session, it)
    if kind == "todo_add":
        text = " ".join(str(it.get("text") or "").split())
        if not text:
            return "conflict", _conflict(store, it, "todo_add without text", needs_robert=False)
        for t in todo.list_todos(store, archived=True):
            if " ".join(str(t["text"]).split()).lower() == text.lower():
                return "skipped", None
        try:
            todo.add_todo(store, text, actor=ACTOR, section=it.get("section") or "human", node=it.get("node"))
        except ValueError as exc:
            return "conflict", _conflict(store, it, f"todo_add refused: {exc}", needs_robert=False)
        return "applied", None
    if kind == "receipt":
        project, node = it.get("project"), it.get("id")
        if not project or not node or store.get_node(project, node) is None:
            return "conflict", _conflict(store, it, f"receipt for unknown task {project}/{node}",
                                         project, node, needs_robert=False)
        store.event(project_id=project, node_id=node, kind="receipt", actor=f"{ACTOR}:{session}",
                    summary=str(it.get("summary") or "Cloud receipt")[:300],
                    data={"links": list(it.get("links") or []), "source": "cloud_sync"})
        return "applied", None
    return "conflict", _conflict(store, it, f"unknown item kind {kind!r}", needs_robert=False)


def import_inbox(store, repo_dir=None) -> dict:
    """Import every unprocessed inbox file in name order. Returns a summary dict."""
    inbox, processed, conflicts = _dirs(repo_dir or DEFAULT_REPO)
    out = {"files": 0, "applied": 0, "skipped": 0, "conflicts": 0, "details": []}
    if not inbox.is_dir():
        return out
    for path in sorted(p for p in inbox.iterdir() if p.is_file() and p.suffix == ".json"):
        counts = {"applied": 0, "skipped": 0, "conflict": 0}
        found = []
        session = "cloud"
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(doc, dict) or doc.get("schema") != SCHEMA or not isinstance(doc.get("items"), list):
                raise ValueError(f"not a {SCHEMA} file")
            session = str(doc.get("session") or "cloud")
            items = doc["items"]
        except (ValueError, OSError) as exc:
            found.append(_conflict(store, {"file": path.name}, f"unreadable sync file {path.name}: {exc}",
                                   needs_robert=False))
            counts["conflict"] += 1
            items = []
        for it in items:
            try:
                result, entry = _apply_item(store, session, it)
            except Exception as exc:  # one bad item never blocks the rest
                result, entry = "conflict", _conflict(store, it, f"item failed: {type(exc).__name__}: {exc}",
                                                      needs_robert=False)
            counts[result] += 1
            if entry:
                found.append(entry)
        if found:
            conflicts.mkdir(parents=True, exist_ok=True)
            (conflicts / path.name).write_text(
                json.dumps({"file": path.name, "at": now_iso(), "conflicts": found}, indent=2, default=str),
                encoding="utf-8")
        processed.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(processed / path.name))
        out["files"] += 1
        out["applied"] += counts["applied"]
        out["skipped"] += counts["skipped"]
        out["conflicts"] += counts["conflict"]
        out["details"].append({"file": path.name, **counts})
    if out["files"]:
        store.event(kind="sync_import", actor=ACTOR,
                    summary=(f"Imported {out['files']} cloud sync file(s): {out['applied']} applied, "
                             f"{out['conflicts']} conflict(s)."),
                    data={k: out[k] for k in ("files", "applied", "skipped", "conflicts")})
    return out


def p_import(h, q):
    from .api import _owner_only
    _owner_only(h, "import cloud sync files")
    body = h._json_body() or {}
    return import_inbox(h.app.store, body.get("repo") or DEFAULT_REPO)


def cli(argv):
    import argparse
    import subprocess
    import sys

    from . import default_db_path
    from .store import Store

    ap = argparse.ArgumentParser(prog="cc_engine sync-import")
    ap.add_argument("--repo", default=str(DEFAULT_REPO))
    ap.add_argument("--db", default=None)
    ap.add_argument("--pull", action="store_true", help="run git pull --ff-only in the repo first")
    args = ap.parse_args(argv)
    if args.pull:
        r = subprocess.run(["git", "-C", args.repo, "pull", "--ff-only"], capture_output=True, text=True)
        if r.returncode:
            print(f"git pull failed: {r.stderr.strip()}", file=sys.stderr)
            return 1
    store = Store(args.db or default_db_path())
    try:
        res = import_inbox(store, args.repo)
    finally:
        store.close()
    print(f"sync-import: {res['files']} file(s), {res['applied']} applied, {res['skipped']} skipped, "
          f"{res['conflicts']} conflict(s)")
    return 0


ROUTES = {"POST /api/sync/import": p_import}
