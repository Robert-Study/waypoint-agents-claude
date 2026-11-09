"""Unattended-run helpers the tick calls: parked runs, needs-you to-dos and run receipts.

Owner's standard for overnight work (Claude memory ``overseer-keep-dispatching``): press Run and
sleep; nothing stops to wait for him.

- **Parked run** (``run_parked``): every task still open waits on Owner (``needs_you`` or
  ``error``) and nothing is running, submitted or ready. A parked run stays active, so his answer
  resumes it, but it no longer holds back the Autonomous rows below it (``settings.after_runs``).
- **Needs-you to-dos** (``sync_needs_you_todos``): each ``needs_you`` task of an active run gets
  one open to-do in the human section, linked to the task. ``todo.housekeep`` ticks it once the
  task is done; this module also ticks it once the task leaves ``needs_you`` (answered, retried).
- **Receipts** (``write_receipt``): a short Markdown receipt per run at
  ``<data dir>/receipts/<run id>.md``, written when the run finishes and when it parks, plus one
  ``receipt`` event naming the file. Nothing here makes a model call or bumps the plan revision.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

NEEDS_YOU_ACTOR = "engine:needs-you"
WAITING = ("needs_you", "error")
MOVING = ("running", "submitted")
APP_URL = "http://127.0.0.1:8751/app/"


def _nodes(store, project_id):
    from . import scheduler as S
    return S.list_nodes(store, project_id) if project_id else []


def run_parked(store, run):
    """True when the run's open tasks all wait on Owner and nothing else can move."""
    from . import graph
    nodes = _nodes(store, run.get("project_id"))
    from .store import CLOSED_STATUSES
    open_nodes = [n for n in nodes if n["status"] not in CLOSED_STATUSES]
    if not open_nodes or any(n["status"] in MOVING for n in open_nodes):
        return False
    if graph.ready_nodes(nodes):
        return False
    return any(n["status"] in WAITING for n in open_nodes)


# ---------------------------------------------------------------- needs-you to-dos

def _question_for(store, project_id, node_id):
    try:
        from . import needs
        qs = [q for q in needs.list_questions(store, open_only=True, project_id=project_id)
              if q.get("node_id") == node_id]
    except Exception:
        return None
    return qs[-1] if qs else None


def _todo_text(store, run, node):
    project = store.get_project(run["project_id"]) or {}
    q = _question_for(store, run["project_id"], node["id"])
    ask = (q or {}).get("prompt") or node.get("title") or node["id"]
    return (f"Waypoint needs you: {project.get('title') or run['project_id']} / {node['id']}: "
            f"{ask}")[:1900]


def sync_needs_you_todos(store, runs=None):
    """Open one human to-do per needs-you task of an active run; tick ones that are settled.

    Returns the number of to-dos added or ticked.
    """
    from . import scheduler as S
    from . import todo
    todo.ensure_schema(store)
    runs = S.active_runs(store) if runs is None else runs
    mine = [t for t in todo.list_todos(store) if t.get("created_by") == NEEDS_YOU_ACTOR and t["node"]]
    open_for = {(t["node"]["project"], t["node"]["id"]): t for t in mine
                if t["status"] in ("open", "handed_off")}
    changed = 0
    for run in runs:
        for n in _nodes(store, run["project_id"]):
            key = (run["project_id"], n["id"])
            if n["status"] != "needs_you" or key in open_for:
                continue
            open_for[key] = todo.add_todo(
                store, _todo_text(store, run, n), actor=NEEDS_YOU_ACTOR, section="human",
                link=f"{APP_URL}?project={run['project_id']}",
                node={"project": run["project_id"], "id": n["id"]})
            S.emit(store, run["project_id"], n["id"], "todo",
                   f"Task {n['id']} needs Owner, so it is on his to-do list; the rest of the run "
                   f"carries on.", data={"todo": open_for[key]["id"], "run": run["id"]})
            changed += 1
    for (pid, nid), t in open_for.items():
        node = store.get_node(pid, nid)
        if node is None or node.get("status") != "needs_you":
            todo._save(store, t["id"], status="done", done_by="engine")
            changed += 1
    return changed


# ---------------------------------------------------------------- receipts

def receipts_dir(store):
    return Path(store.path).resolve().parent / "receipts"


def _line(n):
    bits = [n["status"]]
    if n.get("pool"):
        bits.append(n["pool"] + (f" / {n['model']}" if n.get("model") else ""))
    bits.append(f"{n['attempts']} attempt{'s' if n['attempts'] != 1 else ''}")
    return f"- {n['id']}: {n.get('title') or ''} ({', '.join(bits)})"


def receipt_text(store, run, why, now):
    from . import scheduler as S
    pid = run["project_id"]
    project = store.get_project(pid) or {}
    nodes = _nodes(store, pid)
    since = run.get("created_at") or ""
    events = [e for e in store.events(pid, latest=2000) if (e.get("at") or "") >= since]
    kinds = Counter(e.get("kind") for e in events)
    counts = Counter(n["status"] for n in nodes)
    todos = []
    try:
        from . import todo
        todos = [t for t in todo.list_todos(store) if t.get("created_by") == NEEDS_YOU_ACTOR
                 and t["node"] and t["node"]["project"] == pid and t["status"] != "done"]
    except Exception:
        pass
    lines = [f"# Run receipt: {project.get('title') or pid}",
             "",
             f"- Run: {run['id']} ({why})",
             f"- Written: {S.iso(now)}",
             f"- Started: {run.get('started_at') or run.get('created_at') or 'unknown'}; "
             f"ended: {run.get('ended_at') or 'still active'}",
             f"- Tasks: {len(nodes)} total; "
             + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in sorted(counts.items())),
             f"- Events: {kinds.get('transient', 0)} early stops, {kinds.get('escalate', 0)} "
             f"escalations, {kinds.get('report', 0)} reports",
             "",
             "## Tasks"] + [_line(n) for n in nodes]
    if todos:
        lines += ["", "## Waiting for Owner (human to-dos)"]
        lines += [f"- {t['id']}: {t['text']}" for t in todos]
    return "\n".join(lines) + "\n"


def write_receipt(store, run, why, now):
    """Write ``receipts/<run id>.md`` and one ``receipt`` event. Returns the path."""
    from . import scheduler as S
    run = store.get_run(run["id"]) or run
    folder = receipts_dir(store)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{run['id']}.md"
    path.write_text(receipt_text(store, run, why, now), encoding="utf-8")
    S.emit(store, run["project_id"], None, "receipt", f"The run receipt is at {path}.",
           data={"run": run["id"], "path": str(path), "why": why})
    return path


def note_parked(store, run, now):
    """Write a receipt once per parked episode. Returns True while the run is parked."""
    from . import scheduler as S
    key = f"parked:{run['id']}"
    if not run_parked(store, run):
        if S.meta_get(store, key):
            S.meta_set(store, key, None)
        return False
    if not S.meta_get(store, key):
        S.meta_set(store, key, json.dumps({"at": now}))
        S.emit(store, run["project_id"], None, "run",
               "Every open task now waits on Owner, so this run is parked and the next "
               "projects may start.", data={"run": run["id"]})
        sync_needs_you_todos(store, [run])  # the receipt lists them
        write_receipt(store, run, "parked: waiting on Owner", now)
    return True
