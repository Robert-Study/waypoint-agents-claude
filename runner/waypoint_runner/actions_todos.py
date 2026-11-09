"""To-do action handlers for the Waypoint runner.

Handles todo_merge, todo_click, todo_handoff, todo_add_to_project and brief_build over the data folder's
``todos/`` files. ``register(handlers)`` adds each action type as ``handler(action, ctx) -> dict``; the
runner's action loop discovers modules named ``actions_*.py`` and calls ``register`` (nothing wires this
module by name).

``ctx`` carries the data folder as ``ctx.store`` (a ``waypoint_data.Store``; the Store itself or a dict
with a "store" key also work) and may carry ``ctx.now`` (a callable returning epoch seconds). The data
folder is touched only through the data-pyclient (``waypoint_data``): every to-do write is a
compare-and-swap on ``rev`` (re-read and retry on a conflict), creates are exclusive.

Return value: ``{"status": "done"|"refused"|"failed", "result": {...}}`` plus ``reason`` and ``error`` (same
text) when not done. The caller writes the status onto the action file. A handler refuses an action whose
status is not ``queued``, so an action is processed once.

Rules kept here: a handler never changes a to-do's category or subtype (the producer sets them), a merge
never combines a Hands-on to-do with an AI to-do, and merging never deletes anything.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import time

from waypoint_data import io as wio
from waypoint_data.store import RevConflict, acquire_lock, iso, parse_iso, serialize

MAX_TRIES = 5
BRIEF_HOURS = 12
TITLE_LIMIT = 80
FIXED = ("category", "subtype")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
NOTE_MARK = "Result:"
CONSTRAINT_KEYS = ("project", "node", "line", "accepted", "derived", "lands_in", "source", "created_at")


class Refuse(Exception):
    """The action cannot be done as asked (nothing was changed)."""


# ---------------------------------------------------------------- helpers
def _store(ctx):
    s = ctx.get("store") if isinstance(ctx, dict) else getattr(ctx, "store", None)
    if s is None and hasattr(ctx, "read") and hasattr(ctx, "write"):
        s = ctx
    if s is None:
        raise Refuse("no data folder in ctx")
    return s


def _now(ctx, store):
    fn = ctx.get("now") if isinstance(ctx, dict) else getattr(ctx, "now", None)
    return (fn or getattr(store, "now", None) or time.time)()


def norm_text(text) -> str:
    """Normalised title: lower case, punctuation and spacing dropped."""
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def norm_link(link) -> str:
    return str(link or "").strip().lower().rstrip("/\\")


def _title(text) -> str:
    first = re.split(r"(?<=[.!?])\s|\n", str(text).strip(), maxsplit=1)[0].strip() or str(text).strip()
    return first if len(first) <= TITLE_LIMIT else first[:TITLE_LIMIT - 1].rstrip() + "..."


def _id(payload, key="todo_id"):
    v = payload.get(key)
    if not isinstance(v, str) or not ID_RE.match(v):
        raise Refuse(f"{key} is missing or not a valid id")
    return v


def _get_todo(store, tid):
    t = store.read(f"todos/{tid}.json")
    if not isinstance(t, dict):
        raise Refuse(f"to-do {tid} does not exist")
    return t


def _update_todo(store, tid, fn):
    """Read, let ``fn(copy)`` edit (return False for no change), write with the rev we read. Retries a conflict."""
    for _ in range(MAX_TRIES):
        cur = _get_todo(store, tid)
        new = dict(cur)
        if fn(new) is False or {k: v for k, v in new.items()} == cur:
            return cur, False
        if any(new.get(k) != cur.get(k) for k in FIXED):
            raise RuntimeError("a handler may not change category or subtype")
        try:
            return store.write(f"todos/{tid}.json", new, expected_rev=cur["rev"]), True
        except RevConflict:
            continue
    raise RuntimeError(f"to-do {tid} kept changing under us; try again")


def _event(store, kind, summary, data):
    if getattr(store, "writer", None):
        try:
            store.append_event(store.writer, {"kind": kind, "summary": summary, "data": data})
        except Exception:  # a log line never blocks the action
            pass


def _guarded(fn):
    def handler(action, ctx):
        try:
            if action.get("status", "queued") != "queued":
                raise Refuse(f"action already {action.get('status')}")
            payload = action.get("payload")
            if not isinstance(payload, dict):
                raise Refuse("payload must be an object")
            return {"status": "done", "result": fn(action, payload, ctx, _store(ctx))}
        except Refuse as exc:
            return {"status": "refused", "result": {}, "reason": str(exc), "error": str(exc)}
        except Exception as exc:  # unexpected: report, never crash the runner
            return {"status": "failed", "result": {}, "reason": f"{type(exc).__name__}: {exc}",
                    "error": f"{type(exc).__name__}: {exc}"}
    return handler


def _is_hands_on(t):
    return t.get("category") == "hands_on"


# ---------------------------------------------------------------- todo_merge
def _merge(action, p, ctx, store):
    ids = p.get("todo_ids")
    if not isinstance(ids, list) or len(set(ids)) < 2:
        raise Refuse("todo_ids needs at least two different to-dos")
    ids = list(dict.fromkeys(_id({"todo_id": i}) for i in ids))
    todos = {i: _get_todo(store, i) for i in ids}
    if len({_is_hands_on(t) for t in todos.values()}) > 1:
        raise Refuse("a Hands-on to-do is never merged with an AI to-do")
    keep = p.get("into")
    if keep is None:
        keep = min(ids, key=lambda i: (str(todos[i].get("created_at")), i))
    elif keep not in todos:
        raise Refuse("into must be one of todo_ids")
    k = todos[keep]
    if k.get("merged_into") or k.get("status") in ("done", "dropped"):
        raise Refuse(f"{keep} is {k.get('merged_into') and 'already merged' or k.get('status')} and cannot be kept")
    key = (norm_text(k["text"]), norm_link(k.get("link")))
    others = [i for i in ids if i != keep]
    for i in others:
        t = todos[i]
        if (norm_text(t["text"]), norm_link(t.get("link"))) != key:
            raise Refuse(f"{i} is not a duplicate of {keep} (title or link differs)")
        if t.get("merged_into") not in (None, keep):
            raise Refuse(f"{i} is already merged into {t['merged_into']}")
        if t.get("merged_into") is None and t.get("status") == "done":
            raise Refuse(f"{i} is done and cannot be merged")
    merged, already = [], []
    for i in others:
        def fold(t, keep=keep):
            if t.get("merged_into") == keep:
                return False
            t["merged_into"] = keep
            t["status"] = "dropped"
        _, changed = _update_todo(store, i, fold)
        (merged if changed else already).append(i)
    if merged:
        _event(store, "todo.merge", f"Merged {len(merged)} duplicate to-do(s) into {keep}.",
               {"into": keep, "merged": merged})
    return {"into": keep, "merged": merged, "already_merged": already}


# ---------------------------------------------------------------- todo_click
def _click(action, p, ctx, store):
    tid = _id(p)
    stamp = iso(_now(ctx, store))

    def bump(t):
        t["click_count"] = int(t.get("click_count") or 0) + 1
        if not t.get("first_clicked_at"):
            t["first_clicked_at"] = stamp
    t, _ = _update_todo(store, tid, bump)
    _event(store, "todo.click", f"To-do {tid} clicked ({t['click_count']}).", {"todo": tid, "at": stamp})
    return {"todo": tid, "click_count": t["click_count"], "first_clicked_at": t.get("first_clicked_at")}


# ---------------------------------------------------------------- project / node
def _check_project(store, pid):
    if not isinstance(pid, str) or not ID_RE.match(pid):
        raise Refuse("project is missing or not a valid id")
    proj = store.read(f"projects/{pid}/project.json")
    if not isinstance(proj, dict):
        raise Refuse(f"project {pid} does not exist")
    if proj.get("archived"):
        raise Refuse(f"project {pid} is archived")
    return proj


def _node_for(store, t, pid):
    """Create (or reuse) the project node for this to-do; returns its id."""
    base = ("todo-" + t["id"])[:60]
    title = _title(t["text"])
    name, n = base, 1
    while True:
        cur = store.read(f"projects/{pid}/nodes/{name}.json")
        if cur is None:
            break
        if cur.get("kind") == "todo" and cur.get("title") == title and cur.get("goal", "").startswith(t["text"]):
            return name  # an earlier attempt of this same action
        n += 1
        name = f"{base}-{n}"
    note = f"todo/{name}.md"
    node = {"schema": "waypoint.node.v1", "project": pid, "id": name, "title": title,
            "goal": f"{t['text']}\n\nWhen it is done, write a short note about the outcome to {note}; "
                    f"its first line must start with \"{NOTE_MARK}\".",
            "check": {"type": "text_present", "path": note, "text": NOTE_MARK},
            "deps": [], "parent": None, "estimate": 1, "slot": None,
            "location": {"where": "unset", "changed_at": None}, "files": [], "kind": "todo",
            "owner": "todo", "lane": "todo", "status": "not_started", "attempts": 0}
    try:
        store.write(f"projects/{pid}/nodes/{name}.json", node, expected_rev=0)
    except RevConflict:
        pass  # raced with ourselves; the file now exists
    return name


def _attach(store, tid, pid):
    """Put the to-do in project ``pid`` (sets project and node). Returns (todo, node id)."""
    t = _get_todo(store, tid)
    if t.get("project") not in (None, pid):
        raise Refuse(f"to-do {tid} is already in project {t['project']}")
    if t.get("status") in ("done", "dropped"):
        raise Refuse(f"to-do {tid} is {t['status']}")
    if t.get("merged_into"):
        raise Refuse(f"to-do {tid} was merged into {t['merged_into']}")
    node = t.get("node")
    if isinstance(node, dict) and node.get("project") == pid and t.get("project") == pid:
        return t, node["id"]
    nid = _node_for(store, t, pid)

    def put(x):
        x["project"] = pid
        x["node"] = {"project": pid, "id": nid}
    t, _ = _update_todo(store, tid, put)
    return t, nid


def _add_to_project(action, p, ctx, store):
    tid = _id(p)
    pid = p.get("project")
    _check_project(store, pid)
    _get_todo(store, tid)
    _, nid = _attach(store, tid, pid)
    _event(store, "todo.project", f"To-do {tid} added to {pid} as {nid}.", {"todo": tid, "project": pid, "node": nid})
    return {"todo": tid, "project": pid, "node": nid}


# ---------------------------------------------------------------- todo_handoff
def _record_handoff(store, tid, entry):
    def add(t):
        hist = list(t.get("handoffs") or [])
        if any(h.get("to") == entry["to"] and h.get("ref") == entry["ref"] for h in hist):
            return False
        t["handoffs"] = hist + [entry]
        t["status"] = "handed_off"
    return _update_todo(store, tid, add)[0]


def _handoff(action, p, ctx, store):
    tid = _id(p)
    to = p.get("to")
    if to not in ("plan", "project"):
        raise Refuse("to must be plan or project")
    t = _get_todo(store, tid)
    if t.get("status") in ("done", "dropped") or t.get("merged_into"):
        raise Refuse(f"to-do {tid} is {t.get('status')} and cannot be handed off")
    at = iso(_now(ctx, store))
    if to == "plan":
        plan_id = ("todo-" + tid)[:64]
        seed = {"schema": "waypoint.plan_stage.v1", "stage": "draft", "project": plan_id, "brief": t["text"],
                "created_at": at,
                "nodes": [{"id": "task", "title": _title(t["text"]), "goal": t["text"], "deps": [],
                           "lane": "todo", "owner": "todo", "files": []}]}
        try:
            wio.create_exclusive(store.path(f"plan_stages/{plan_id}/draft.json"), serialize(seed),
                                 writer=getattr(store, "writer", None) or "runner")
        except FileExistsError:
            pass  # an earlier attempt already planted the seed
        ref = {"plan": plan_id}
        result = {"todo": tid, "to": "plan", "plan": plan_id}
    else:
        pid = p.get("project")
        _check_project(store, pid)
        _, nid = _attach(store, tid, pid)
        ref = {"project": pid, "node": nid}
        result = {"todo": tid, "to": "project", "project": pid, "node": nid}
    _record_handoff(store, tid, {"to": to, "at": at, "by": action.get("by") or "runner", "ref": ref})
    _event(store, "todo.handoff", f"To-do {tid} handed off to {to}.", result)
    return result


# ---------------------------------------------------------------- brief_build
def _list_dir(store, rel):
    try:
        return sorted(os.listdir(store.path(rel)))
    except (FileNotFoundError, NotADirectoryError):
        return []


def _triage_merges(open_todos):
    groups = {}
    for t in open_todos:
        k = (_is_hands_on(t), norm_text(t["text"]), norm_link(t.get("link")))
        groups.setdefault(k, []).append(t)
    out = []
    for members in groups.values():
        if len(members) > 1:
            members.sort(key=lambda t: (str(t.get("created_at")), t["id"]))
            out.append({"keep": members[0]["id"], "merge": [m["id"] for m in members[1:]],
                        "text": members[0]["text"]})
    return sorted(out, key=lambda m: m["keep"])


def _constraints(store):
    out = []
    for e in store.list_entities("constraints"):
        if isinstance(e, dict) and e.get("project") and e.get("line"):
            out.append({k: e[k] for k in CONSTRAINT_KEYS if k in e})
    out.sort(key=lambda c: (c["project"], str(c.get("node") or ""), c["line"]))
    return out


def _node_items(store, since):
    done, failed = [], []
    for pid in _list_dir(store, "projects"):
        for name in _list_dir(store, f"projects/{pid}/nodes"):
            if not name.endswith(".json"):
                continue
            n = store.read(f"projects/{pid}/nodes/{name}")
            if not isinstance(n, dict):
                continue
            when = parse_iso(n.get("updated_at"))
            if when is None or when < since:
                continue
            item = {"project": pid, "node": n.get("id"), "title": n.get("title") or n.get("id")}
            if n.get("status") == "done":
                done.append(item)
            elif n.get("status") == "error":
                failed.append(item)
    return done, failed


def _paragraph(hours, done, failed, needs):
    span = "hour" if hours == 1 else f"{hours} hours"
    if not done and not failed:
        first = f"Nothing finished or failed in the last {span}."
    else:
        fin = f"{done} task{'' if done == 1 else 's'} finished" if done else "nothing finished"
        first = f"In the last {span}, {fin} and " + (f"{failed} failed." if failed else "nothing failed.")
    second = "Nothing needs you." if not needs else ("1 thing needs you." if needs == 1 else f"{needs} things need you.")
    return f"{first} {second}"


def _brief_build(action, p, ctx, store):
    now = _now(ctx, store)
    day = _dt.datetime.fromtimestamp(now, _dt.timezone.utc).strftime("%Y-%m-%d")
    open_todos = [t for t in store.list_entities("todos")
                  if t.get("status") == "open" and not t.get("merged_into")]
    questions = [q for q in store.list_entities("questions") if q.get("answered_at") in (None, "")]
    needs = len([t for t in open_todos if not _is_hands_on(t)]) + len(questions)
    since = now - BRIEF_HOURS * 3600
    done_items, failed_items = _node_items(store, since)
    brief = {"since": iso(since), "until": iso(now), "hours": BRIEF_HOURS,
             "paragraph": _paragraph(BRIEF_HOURS, len(done_items), len(failed_items), needs),
             "done": len(done_items), "failed": len(failed_items), "needs_you": needs,
             "done_items": done_items, "failed_items": failed_items, "usage": [],
             "triage": {"merges": _triage_merges(open_todos), "open": len(open_todos)}}
    constraints = _constraints(store)
    body = {"schema": "waypoint.brief.v1", "day": day, "brief": brief, "constraints": constraints}
    target = store.path(f"briefs/{day}.json")
    lock = acquire_lock(target + ".lock", owner=getattr(store, "writer", None) or "runner")
    try:
        wio.atomic_write(target, serialize(body), writer=getattr(store, "writer", None) or "runner")
    finally:
        lock.release()
    return {"day": day, "path": f"briefs/{day}.json", "needs_you": needs, "constraints": len(constraints)}


# ---------------------------------------------------------------- register
HANDLERS = {
    "todo_merge": _guarded(_merge),
    "todo_click": _guarded(_click),
    "todo_handoff": _guarded(_handoff),
    "todo_add_to_project": _guarded(_add_to_project),
    "brief_build": _guarded(_brief_build),
}


def register(handlers: dict) -> None:
    """Add this module's action types to the runner's handler table."""
    handlers.update(HANDLERS)
