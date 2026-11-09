"""The ``sync_import`` action: import the files a cloud session pushed to ``sync/inbox`` (Waypoint 1.2, run-ingest).

Format: ``docs/v5/cloud_sync_format.md`` (``waypoint.sync.v1``). Reference for the rules: ``engine/cc_engine/cloud_sync.py``,
re-done here against the folder store (``core/waypoint-data/py``) instead of SQLite.

- ``node_status`` only moves a node forward (not_started/ready < needs_you/error < running < submitted < done); cloud
  ``done`` is applied as ``submitted``; a node changed after ``base_revision`` (its file ``rev`` is higher), a closed
  (skipped/carried) node, an archived project or a backward move is a conflict. Local state is never overwritten.
- ``todo_add`` is skipped when a to-do with the same text (case and spacing ignored) exists; else a Hands-on to-do.
- ``receipt`` writes a write-once ``receipts/`` file and an event; no status change.
- ``question`` (an extension: ``{"kind": "question", "project", "node", "question": {...}}``) writes a question and its
  Decisions to-do.
- Anything else is a conflict: an entry in ``sync/conflicts/<file>``, an event, and for status conflicts a Decisions
  to-do for Owner. A file that is not valid ``waypoint.sync.v1`` is moved to ``sync/invalid/``, reported, and gets a
  Review/failure to-do. Handled files are moved to ``sync/processed/`` once; the SHA-256 of the bytes is the ledger
  (``ingest/<digest>.json``), so the same file arriving again changes nothing.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import receipts_ingest as ri
from waypoint_data import io as wio  # noqa: E402  (receipts_ingest put core/waypoint-data/py on sys.path)
from waypoint_data.store import RevConflict  # noqa: E402

SCHEMA = "waypoint.sync.v1"
SCHEMA_CONFLICTS = "waypoint.sync_conflicts.v1"
ACTOR = "cloud-sync"
DEFAULT_SOURCE = "sync/inbox"
RANK = {"not_started": 0, "ready": 0, "needs_you": 1, "error": 1, "running": 2, "submitted": 3, "done": 4}
CLOSED = ("skipped", "carried")


def _norm(text):
    return " ".join(str(text or "").split()).lower()


def _event(store, kind, summary, project=None, node=None, data=None):
    ev = {"kind": kind, "summary": summary[:300], "data": data}
    if project and node and ri._known_node(store, project, node):
        ev["project"], ev["node"] = project, node
    try:
        store.append_event(ACTOR, ev)
    except Exception:
        pass


class _Ctx:
    def __init__(self, store, dig, name, session):
        self.store, self.dig, self.name, self.session = store, dig, name, session
        self.found = []
        self.todo_keys = 0

    def conflict(self, item, why, project=None, node=None, needs_robert=False):
        self.found.append({"item": item, "reason": why})
        _event(self.store, "sync.conflict", f"Cloud update not applied: {why}", project, node,
               {"file": self.name, "session": self.session})
        if needs_robert:
            self.todo_keys += 1
            ri.add_decision_todo(self.store, f"sync-{self.dig[:10]}-c{self.todo_keys}",
                                 f"Cloud sync conflict: {why}", project, node, link=f"sync/conflicts/{self.name}")
        return "conflict"


def _apply_status(cx, i, it):
    store = cx.store
    project, node, target, base = it.get("project"), it.get("id"), it.get("status"), it.get("base_revision")
    if (not project or not node or target not in RANK or isinstance(base, bool) or not isinstance(base, int)):
        return cx.conflict(it, "node_status item is missing project, id, status or base_revision", project, node)
    if not (isinstance(project, str) and isinstance(node, str) and ri.ID_RE.match(project) and ri.ID_RE.match(node)):
        return cx.conflict(it, "node_status item has a bad project or id", None, None)
    rel = f"projects/{project}/nodes/{node}.json"
    cur = store.read(rel)
    if not isinstance(cur, dict):
        return cx.conflict(it, f"no task {project}/{node}", project, node)
    status = cur.get("status")
    if status in CLOSED:
        return cx.conflict(it, f"{project}/{node} is {status} (closed by a version roll-over); cloud says "
                           f"{target}, which would reopen it", project, node)
    proj = store.read(f"projects/{project}/project.json")
    if isinstance(proj, dict) and proj.get("archived"):
        return cx.conflict(it, f"{project} is archived (an earlier version); cloud says {node} is {target}, "
                           "which cloud sync never applies", project, node)
    if target == "done":
        target = "submitted"  # only a passing check or Owner marks done
    if status == target:
        return "skipped"
    if RANK[target] < RANK.get(status, 0):
        return cx.conflict(it, f"{project}/{node} is already {status}; cloud says {target}, which would undo "
                           "local progress", project, node, needs_robert=True)
    if int(cur.get("rev") or 0) > base:
        return cx.conflict(it, f"{project}/{node} changed locally after revision {base}; cloud wanted {target}",
                           project, node, needs_robert=True)
    new = dict(cur, status=target)
    if it.get("evidence") is not None:
        new["evidence"] = str(it["evidence"])[:2000]
    try:
        store.write(rel, new, expected_rev=cur.get("rev"))
    except RevConflict:
        return cx.conflict(it, f"{project}/{node} changed while importing; cloud wanted {target}", project, node,
                           needs_robert=True)
    _event(store, "node.status", f"Cloud session {cx.session} reported {node} as {target.replace('_', ' ')}.",
           project, node, {"source": "cloud_sync", "base_revision": base, "file": cx.name})
    return "applied"


def _apply_item(cx, i, it):
    store = cx.store
    if not isinstance(it, dict):
        return cx.conflict(it, "item is not an object")
    kind = it.get("kind")
    if kind == "node_status":
        return _apply_status(cx, i, it)
    if kind == "todo_add":
        text = " ".join(str(it.get("text") or "").split())
        if not text:
            return cx.conflict(it, "todo_add without text")
        if any(_norm(t.get("text")) == _norm(text) for t in store.list_entities("todos") if isinstance(t, dict)):
            return "skipped"
        node = it.get("node") if isinstance(it.get("node"), dict) else {}
        ri.add_hands_on_todo(store, text, node.get("project"), node.get("id"), key=f"sync-{cx.dig[:10]}-{i}")
        return "applied"
    if kind == "receipt":
        project, node = it.get("project"), it.get("id")
        if not ri._known_node(store, project, node):
            return cx.conflict(it, f"receipt for unknown task {project}/{node}", None, None)
        summary = " ".join(str(it.get("summary") or "Cloud receipt").split())[:ri.MAX_SUMMARY] or "Cloud receipt"
        links = [str(x)[:ri.MAX_ITEM] for x in (it.get("links") or []) if isinstance(x, str)][:ri.MAX_LIST]
        body = {"schema": ri.SCHEMA_RECEIPT, "project": project, "node": node, "status": "done", "summary": summary,
                "evidence": [], "outputs": links, "todo": None, "questions": [], "metrics": {},
                "session": cx.session, "source": "cloud_sync"}
        rel = f"receipts/cloud.{ri._safe(cx.session)}.{project}.{node}.{cx.dig[:8]}-{i}.json"
        try:
            wio.create_exclusive(store.path(rel), store.serialize(body), writer=ACTOR)
        except FileExistsError:
            pass
        _event(store, "receipt", summary, project, node, {"links": links, "source": "cloud_sync", "receipt": rel})
        return "applied"
    if kind == "question":
        q, err = ri.validate_loose_question(it.get("question"))
        if err:
            return cx.conflict(it, f"question refused: {err}")
        ri.write_question(store, f"q-{cx.dig[:10]}-{i}", q, it.get("project") or q.get("project"),
                          it.get("node") or q.get("node"))
        return "applied"
    return cx.conflict(it, f"unknown item kind {kind!r}")


def _move(src, dest_dir):
    """Move a file into dest_dir without overwriting. Returns the new path or None."""
    try:
        Path(dest_dir).mkdir(parents=True, exist_ok=True)
        dest = Path(dest_dir) / Path(src).name
        n = 0
        while dest.exists():
            n += 1
            dest = Path(dest_dir) / f"{Path(src).stem}.{n}{Path(src).suffix}"
        os.replace(src, dest)
        return str(dest)
    except OSError:
        return None


def _source_of(action):
    if isinstance(action, str):
        return action
    if isinstance(action, dict):
        payload = action.get("payload") if isinstance(action.get("payload"), dict) else action
        return payload.get("source")
    return None


def sync_import(store, action=None, inbox=None):
    """Import every unhandled ``*.json`` in the inbox, in name order. Returns a summary dict.

    The inbox is ``sync/inbox`` in the data folder unless the action payload names another ``source`` (relative to the
    data folder; it can never point outside it).
    """
    rel_inbox = (inbox or _source_of(action) or DEFAULT_SOURCE).strip().replace("\\", "/").strip("/")
    inbox_dir = Path(store.path(rel_inbox))  # raises ValueError for .. or a drive letter
    base = inbox_dir.parent
    out = {"files": 0, "applied": 0, "skipped": 0, "conflicts": 0, "invalid": 0, "duplicate": 0, "details": []}
    for path in [inbox_dir / n for n in wio.list_names(str(inbox_dir)) if n.endswith(".json")][:ri.MAX_FILES]:
        if path.is_symlink():
            continue
        try:
            raw = path.read_bytes()
        except OSError as exc:
            out["details"].append({"file": path.name, "status": "unreadable", "reason": str(exc)})
            continue
        dig = ri.digest(raw)
        name = path.name
        if ri._ledger_has(store, dig):
            was_invalid = ri._was_invalid(store, dig)
            _move(path, base / ("invalid" if was_invalid else "processed"))
            out["duplicate"] += 1
            out["details"].append({"file": name, "status": "duplicate"})
            continue
        doc, reason = ri.parse(raw) if len(raw) <= 1024 * 1024 else (None, "the file is over 1 MB")
        if reason is None and (not isinstance(doc, dict) or doc.get("schema") != SCHEMA
                               or not isinstance(doc.get("items"), list)):
            reason = f"not a {SCHEMA} file"
        if reason:
            ri.add_review_todo(store, f"bad-{dig[:12]}", f"The cloud sync file {name} was rejected: {reason}. "
                               "It was moved to sync/invalid/.", link=f"sync/conflicts/{name}")
            store.write(f"{base.relative_to(store.root).as_posix()}/conflicts/{name}",
                        {"schema": SCHEMA_CONFLICTS, "file": name, "at": ri.iso(store.now()),
                         "conflicts": [{"item": {"file": name}, "reason": reason}]})
            _event(store, "sync.invalid", f"Cloud sync file {name} rejected: {reason}", None, None,
                   {"file": name, "digest": dig})
            ri._ledger_add(store, dig, "sync", name, "invalid", reason)
            _move(path, base / "invalid")
            out["invalid"] += 1
            out["details"].append({"file": name, "status": "invalid", "reason": reason})
            continue
        session = str(doc.get("session") or "cloud")
        cx = _Ctx(store, dig, name, session)
        counts = {"applied": 0, "skipped": 0, "conflict": 0}
        for i, it in enumerate(doc["items"]):
            try:
                result = _apply_item(cx, i, it)
            except Exception as exc:  # one bad item never blocks the rest
                result = cx.conflict(it, f"item failed: {type(exc).__name__}: {exc}")
            counts[result] += 1
        if cx.found:
            store.write(f"{base.relative_to(store.root).as_posix()}/conflicts/{name}",
                        {"schema": SCHEMA_CONFLICTS, "file": name, "at": ri.iso(store.now()),
                         "conflicts": cx.found})
        ri._ledger_add(store, dig, "sync", name, "imported")
        _move(path, base / "processed")
        out["files"] += 1
        for k in ("applied", "skipped"):
            out[k] += counts[k]
        out["conflicts"] += counts["conflict"]
        out["details"].append({"file": name, "status": "imported", **counts})
    if out["files"]:
        _event(store, "sync.import",
               f"Imported {out['files']} cloud sync file(s): {out['applied']} applied, {out['conflicts']} conflict(s).",
               None, None, {k: out[k] for k in ("files", "applied", "skipped", "conflicts")})
    return out


def register(handlers):
    """Install the ``sync_import`` action handler: ``handler(store, action_or_payload=None) -> summary dict``."""
    handlers["sync_import"] = sync_import
    return handlers
