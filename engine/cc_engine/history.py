"""Clean task history for the Inspector: one plain line per moment that matters.

Kinds (oldest first): ``prereqs_met`` (every dependency became done), ``started`` (the task went
to running), ``interrupted`` (it stopped running without finishing, or a finished task was
reopened), ``failed`` (its check failed or the failure handler gave up), ``completed`` (done),
``returned`` (a correction return from a loop node: an event whose kind, or whose data's
``kind``, is ``returned``; shown as a single line) and ``replanned`` (a ``replan`` event for the task,
by node id or ``data.unit``; also one line).
Each row is ``{"at", "kind", "text", "by"}``. Internal events (model swaps, field updates,
submitted-for-check, retries queued after a failure, reports, plan edits) are left out; a
failure doctor ``diagnosis`` event replaces the text of the failure it explains with its cause.

``build_history`` is a pure function over the project's events, so it works without dsh or a
server. ``GET /api/node/history?project=&id=`` serves it.
"""
from __future__ import annotations

import re

KINDS = ("prereqs_met", "started", "interrupted", "failed", "completed", "returned", "replanned")
RUNNING = ("running", "submitted")
WAITING = ("not_started", "ready")
TEXT_LIMIT = 140
EVENT_SCAN_LIMIT = 100000


def who(actor) -> str:
    """A short plain name for an event actor."""
    a = str(actor or "").strip()
    low = a.lower()
    if low == "owner":
        return "Owner"
    if low.startswith("check:") or low == "check":
        return "Check"
    if low in ("router", "scheduler", "failure", "engine", "planner"):
        return {"failure": "Engine"}.get(low, low.capitalize())
    return a or "Engine"


def _plain(summary, node_id, fallback) -> str:
    """One line from an event summary, without the leading ``Task <id>``."""
    text = " ".join(str(summary or "").split())
    if node_id:
        text = re.sub(rf"^Task {re.escape(str(node_id))}\b[ ,:]*", "", text)
    if not text:
        return fallback
    text = text[0].upper() + text[1:]
    if len(text) > TEXT_LIMIT:
        text = text[: TEXT_LIMIT - 1].rstrip() + "…"
    return text


def is_return(ev) -> bool:
    data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
    return ev.get("kind") == "returned" or data.get("kind") == "returned"


def _data(ev) -> dict:
    return ev.get("data") if isinstance(ev.get("data"), dict) else {}


def is_replan(ev) -> bool:
    return ev.get("kind") == "replan" or _data(ev).get("kind") == "replan"


def _about(ev, nid) -> bool:
    return ev.get("node_id") == nid or (ev.get("node_id") in (None, "") and _data(ev).get("unit") == nid)


def _status_of(ev):
    if ev.get("kind") != "status":
        return None
    data = ev.get("data") or {}
    return data.get("status") if isinstance(data, dict) else None


def build_history(node, events, statuses=None) -> list:
    """History rows for ``node`` from ``events`` (the project's events, any order).

    ``statuses`` maps node ids to their current stored status; a dependency with no status
    events is assumed to have held that status throughout (e.g. imported as done). A dependency
    added later (an ``add_node`` event, e.g. a fix-up) counts only from when it was added.
    """
    nid = node["id"]
    deps = [d for d in (node.get("deps") or []) if d != nid]
    statuses = statuses or {}
    evs = sorted(events or [], key=lambda e: (e.get("seq") or 0, e.get("at") or ""))
    dep_has_events = {d: any(e.get("node_id") == d and _status_of(e) for e in evs) for d in deps}
    added_later = {e.get("node_id") for e in evs if e.get("kind") == "add_node" and e.get("node_id") in deps}
    dep_state = {d: ("not_started" if dep_has_events[d] else statuses.get(d, "not_started"))
                 for d in deps if d not in added_later}
    met = bool(dep_state) and all(s == "done" for s in dep_state.values())
    prev = "not_started"
    rows = []

    def add(ev, kind, text, by=None):
        rows.append({"at": ev.get("at"), "kind": kind, "text": text, "by": by or who(ev.get("actor"))})

    for ev in evs:
        if ev.get("node_id") == nid and ev.get("kind") == "diagnosis":
            cause = (ev.get("data") or {}).get("cause") if isinstance(ev.get("data"), dict) else None
            if cause and rows and rows[-1]["kind"] == "failed":  # the failure doctor's one-line cause
                rows[-1]["text"] = _plain("Failed: " + str(cause), nid, rows[-1]["text"])
            continue
        if ev.get("node_id") == nid and is_return(ev):
            data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
            add(ev, "returned", _plain(ev.get("summary") or data.get("reason") or data.get("text"), nid,
                                       "Returned for a fix"))
            continue
        if is_replan(ev) and _about(ev, nid):
            add(ev, "replanned", _plain(ev.get("summary") or _data(ev).get("reason"), nid, "Replanned"))
            continue
        target = ev.get("node_id")
        if ev.get("kind") == "add_node" and target in added_later:
            dep_state[target] = "not_started"  # a new prerequisite: not met until it is done
            met = False
            continue
        status = _status_of(ev)
        if status is None:
            continue
        if target in dep_state:
            dep_state[target] = status
            now_met = all(s == "done" for s in dep_state.values())
            if now_met and not met:
                add(ev, "prereqs_met", "Prerequisites done: " + ", ".join(d for d in deps if d in dep_state))
            met = now_met
            continue
        if target != nid or status == prev:
            continue
        actor = ev.get("actor")
        if status == "running":
            add(ev, "started", f"Started by {who(actor)}")
        elif status == "done":
            low = str(actor or "").lower()
            if low.startswith("check:"):
                text = f"Done: {low.split(':', 1)[1]} check passed"
            elif low == "owner":
                text = "Marked done by Owner"
            else:
                text = f"Done ({who(actor)})"
            add(ev, "completed", text)
        elif status == "error":
            add(ev, "failed", _plain(ev.get("summary"), nid, "Failed"))
        elif status == "needs_you" and prev in RUNNING and str(actor or "").lower() == "failure":
            add(ev, "failed", _plain(ev.get("summary"), nid, "Failed and needs Owner"))
        elif status in WAITING and prev in RUNNING:
            add(ev, "interrupted", _plain(ev.get("summary"), nid, "Interrupted"))
        elif status in WAITING and prev == "done":
            add(ev, "interrupted", f"Reopened by {who(actor)}")
        prev = status
    return rows


def node_history(store, project_id, node_id) -> list:
    """History rows for one task, read from the store."""
    node = store.get_node(project_id, node_id)
    if node is None:
        raise KeyError(f"no node {project_id}/{node_id}")
    statuses = {n["id"]: n["status"] for n in store.list_nodes(project_id)}
    wanted = {node_id, *(node.get("deps") or [])}
    events = [e for e in store.events(project_id=project_id, since=0, limit=EVENT_SCAN_LIMIT)
              if (e.get("node_id") in wanted and (e.get("kind") in ("status", "diagnosis", "add_node") or is_return(e)))
              or (is_replan(e) and _about(e, node_id))]
    return build_history(node, events, statuses)


def _http_error(status, body):
    from .api import HttpError  # lazy: api imports extension modules
    return HttpError(status, body)


def g_node_history(h, q):
    """``?project=&id=`` -> ``{"project", "node", "history": [...]}``."""
    store = h.app.store
    pid = q.get("project") or store.default_project_id()
    if not pid or store.get_project(pid) is None:
        raise _http_error(404, {"error": "not_found", "detail": "no such project"})
    nid = q.get("id") or q.get("node")
    if not nid or store.get_node(pid, nid) is None:
        raise _http_error(404, {"error": "not_found", "detail": f"no node {nid}"})
    return {"project": pid, "node": nid, "history": node_history(store, pid, nid)}


ROUTES = {"GET /api/node/history": g_node_history}
