"""Waypoint Claude learning tools: topics with a dependency plan, propositions with premises, a simple review schedule.

A topic holds the goal, a short note on where understanding starts, and steps with dependencies. A proposition is one
claim the learner now holds, with the claims it rests on (premises). Practice uses a plain SM-2-style interval.
Only ideas are stored: no ability scores, no summaries, no search index.
Data folder: learn/topics/<id>.json and learn/props/<id>.json (same Store, revisions and events as ops_tools).
"""
from __future__ import annotations

import math

try:
    from . import mcp_board as mb
except ImportError:  # started as a script from this folder
    import mcp_board as mb

from waypoint_data import RevConflict  # noqa: E402
from waypoint_data.store import iso, parse_iso  # noqa: E402

ToolError, _id = mb.ToolError, mb._id

STEP_STATUS = ("todo", "taught", "repair")
TOPIC_STATUS = ("active", "done", "paused")
OUTCOMES = ("again", "hard", "good", "easy")
START_EASE, MIN_EASE = 2.5, 1.3
DAY = 86400.0


def _text(v, what, required=True):
    if v is None and not required:
        return ""
    if not isinstance(v, str) or (required and not v.strip()):
        raise ToolError("bad_argument", f"{what} must be a non-empty string" if required else f"{what} must be a string")
    return v.strip()


def _topic(board, tid):
    t = board.store.read(f"learn/topics/{tid}.json")
    if not isinstance(t, dict):
        raise ToolError("not_found", f"topic {tid} does not exist")
    return t


def _save(board, rel, body, expected_rev, what):
    try:
        return board.store.write(rel, body, expected_rev=expected_rev)
    except RevConflict as exc:
        raise board._conflict(exc, what)


def _check_plan(steps):
    """Return clean steps; refuse bad ids, duplicates, unknown deps and cycles."""
    if not isinstance(steps, list):
        raise ToolError("bad_argument", "steps must be a list")
    out, ids = [], set()
    for s in steps:
        if not isinstance(s, dict):
            raise ToolError("bad_argument", "each step must be an object")
        sid = _id(s.get("id"), "step id")
        if sid in ids:
            raise ToolError("bad_argument", f"step id {sid} is used twice")
        ids.add(sid)
        status = s.get("status", "todo")
        if status not in STEP_STATUS:
            raise ToolError("bad_argument", f"step status must be one of {', '.join(STEP_STATUS)}")
        deps = s.get("deps") or []
        if not isinstance(deps, list):
            raise ToolError("bad_argument", "deps must be a list of step ids")
        out.append({"id": sid, "title": _text(s.get("title"), "step title"), "deps": [_id(d, "dep") for d in deps],
                    "status": status, "note": s.get("note") or ""})
    for s in out:
        for d in s["deps"]:
            if d not in ids:
                raise ToolError("bad_argument", f"step {s['id']} depends on unknown step {d}")
    graph, state = {s["id"]: s["deps"] for s in out}, {}

    def visit(n):
        if state.get(n) == 1:
            raise ToolError("bad_argument", f"steps form a cycle at {n}")
        if state.get(n) == 2:
            return
        state[n] = 1
        for d in graph[n]:
            visit(d)
        state[n] = 2
    for n in graph:
        visit(n)
    return out


def learn_topic_set(board, args):
    """Create or update a topic. Omitted fields keep their value; steps, when given, replace the plan."""
    tid = _id(args.get("id"), "id")
    cur = board.store.read(f"learn/topics/{tid}.json")
    cur = cur if isinstance(cur, dict) else None
    now = iso(board.now())
    if cur is None and not args.get("title"):
        raise ToolError("bad_argument", "title is required to create a topic")
    body = {"id": tid, "title": (cur or {}).get("title", ""), "goal": (cur or {}).get("goal", ""),
            "start": (cur or {}).get("start", ""), "steps": (cur or {}).get("steps", []),
            "status": (cur or {}).get("status", "active"), "created_at": (cur or {}).get("created_at", now)}
    for k in ("title", "goal", "start"):
        if args.get(k) is not None:
            body[k] = _text(args[k], k)
    if args.get("status") is not None:
        if args["status"] not in TOPIC_STATUS:
            raise ToolError("bad_argument", f"status must be one of {', '.join(TOPIC_STATUS)}")
        body["status"] = args["status"]
    if args.get("steps") is not None:
        if not isinstance(args["steps"], list):
            raise ToolError("bad_argument", "steps must be a list")
        old = {s["id"]: s for s in body["steps"]}
        merged = []
        for s in args["steps"]:
            if isinstance(s, dict) and s.get("id") in old:  # a step the plan keeps keeps its progress
                s = {"status": old[s["id"]]["status"], "note": old[s["id"]]["note"], **s}
            merged.append(s)
        body["steps"] = _check_plan(merged)
    body["updated_at"] = now
    out = _save(board, f"learn/topics/{tid}.json", body, cur.get("rev") if cur else 0, "topic")
    board.store.append_event(board.by, {"kind": "learn.topic", "summary": f"Topic {tid} {'updated' if cur else 'created'}.",
                                        "data": {"id": tid, "steps": len(body["steps"])}})
    return out


def learn_step_set(board, args):
    """Set one step's status (todo, taught, repair) and optional note."""
    tid, sid = _id(args.get("topic"), "topic"), _id(args.get("step"), "step")
    status = args.get("status")
    if status not in STEP_STATUS:
        raise ToolError("bad_argument", f"status must be one of {', '.join(STEP_STATUS)}")
    t = _topic(board, tid)
    steps = [dict(s) for s in t.get("steps") or []]
    hit = next((s for s in steps if s["id"] == sid), None)
    if hit is None:
        raise ToolError("not_found", f"step {sid} is not in topic {tid}")
    hit["status"] = status
    if args.get("note") is not None:
        hit["note"] = _text(args["note"], "note", required=False)
    out = _save(board, f"learn/topics/{tid}.json", dict(t, steps=steps, updated_at=iso(board.now())), t.get("rev"), "topic")
    board.store.append_event(board.by, {"kind": "learn.step", "summary": f"Step {sid} of {tid} is {status}.",
                                        "data": {"topic": tid, "step": sid, "status": status}})
    return out


def _props(board):
    return [p for p in board.store.list_entities("learn/props") if isinstance(p, dict) and p.get("id")]


def learn_prop_add(board, args):
    """Record one claim the learner now holds, with the claims it rests on."""
    tid = _id(args.get("topic"), "topic")
    _topic(board, tid)
    text = _text(args.get("text"), "text")
    prem = args.get("premises") or []
    if not isinstance(prem, list):
        raise ToolError("bad_argument", "premises must be a list of proposition ids")
    existing = {p["id"] for p in _props(board)}
    for pid in prem:
        if _id(pid, "premise") not in existing:
            raise ToolError("bad_argument", f"premise {pid} does not exist")
    n = 1
    while f"p{n}" in existing:
        n += 1
    now = board.now()
    body = {"id": f"p{n}", "topic": tid, "text": text, "premises": list(dict.fromkeys(prem)),
            "session": args.get("session") or iso(now)[:10], "created_at": iso(now),
            "practice": {"due": iso(now), "interval_days": 0, "ease": START_EASE, "reps": 0, "last_outcome": None}}
    out = _save(board, f"learn/props/p{n}.json", body, 0, "proposition")
    board.store.append_event(board.by, {"kind": "learn.prop", "summary": f"Claim p{n} added to {tid}.",
                                        "data": {"id": f"p{n}", "topic": tid, "premises": body["premises"]}})
    return out


def _days(x):
    return max(1, int(math.floor(x + 0.5)))


def learn_practice_record(board, args):
    """Record one practice outcome (again, hard, good, easy) and schedule the next review."""
    pid = _id(args.get("id"), "id")
    outcome = args.get("outcome")
    if outcome not in OUTCOMES:
        raise ToolError("bad_argument", f"outcome must be one of {', '.join(OUTCOMES)}")
    p = board.store.read(f"learn/props/{pid}.json")
    if not isinstance(p, dict):
        raise ToolError("not_found", f"proposition {pid} does not exist")
    pr = p.get("practice") or {}
    ease, reps, iv = float(pr.get("ease") or START_EASE), int(pr.get("reps") or 0), float(pr.get("interval_days") or 0)
    if outcome == "again":
        ease, reps, iv = max(MIN_EASE, ease - 0.2), 0, 0
    else:
        if outcome == "hard":
            ease, iv = max(MIN_EASE, ease - 0.15), (1 if reps == 0 else max(_days(iv * 1.2), int(iv) or 1))
        elif outcome == "good":
            iv = 1 if reps == 0 else max(_days(iv * ease), int(iv) + 1)  # a pass always lengthens the gap
        else:
            ease, iv = ease + 0.15, (3 if reps == 0 else max(_days(iv * ease * 1.3), int(iv) + 2))
        reps += 1
    now = board.now()
    pr = {"due": iso(now + iv * DAY), "interval_days": iv, "ease": round(ease, 2), "reps": reps, "last_outcome": outcome}
    out = _save(board, f"learn/props/{pid}.json", dict(p, practice=pr), p.get("rev"), "proposition")
    board.store.append_event(board.by, {"kind": "learn.practice", "summary": f"Practice of {pid}: {outcome}.",
                                        "data": {"id": pid, "outcome": outcome, "interval_days": iv}})
    return out


def learn_read(board, args):
    """Topics, claims, premise links (from premise to claim) and due claims (oldest due first). Read only."""
    tid = args.get("topic")
    topics = sorted((t for t in board.store.list_entities("learn/topics") if isinstance(t, dict) and t.get("id")),
                    key=lambda t: t["id"])
    props = sorted(_props(board), key=lambda p: (len(p["id"]), p["id"]))
    if tid is not None:
        topics, props = [t for t in topics if t["id"] == tid], [p for p in props if p.get("topic") == tid]
    ids = {p["id"] for p in props}
    links = [{"from": q, "to": p["id"]} for p in props for q in p.get("premises") or [] if q in ids]
    now, due = board.now(), []
    for p in props:
        try:
            t = parse_iso((p.get("practice") or {}).get("due"))
        except (TypeError, ValueError, AttributeError):
            t = None
        if t is not None and t <= now:
            due.append((t, p))
    due.sort(key=lambda x: x[0])
    return {"topics": topics, "props": props, "links": links, "due": [p for _, p in due]}


STEP_SCHEMA = {"type": "object", "properties": {
    "id": {"type": "string"}, "title": {"type": "string"}, "deps": {"type": "array", "items": {"type": "string"}},
    "status": {"type": "string", "enum": list(STEP_STATUS)}, "note": {"type": "string"}}, "required": ["id", "title"]}
TOOLS = [
    ("learn_read", "Read learning topics, claims (propositions), premise links and the claims due for practice.",
     {"topic": {"type": "string"}}, []),
    ("learn_topic_set", "Create or update a learning topic and its plan (steps with dependencies; no cycles).",
     {"id": {"type": "string"}, "title": {"type": "string"}, "goal": {"type": "string"},
      "start": {"type": "string", "description": "Where understanding starts, from the measure phase."},
      "status": {"type": "string", "enum": list(TOPIC_STATUS)},
      "steps": {"type": "array", "description": "Plan: steps with deps (ids of steps needed first).", "items": STEP_SCHEMA}},
     ["id"]),
    ("learn_step_set", "Set one plan step to todo, taught or repair, with an optional note.",
     {"topic": {"type": "string"}, "step": {"type": "string"}, "status": {"type": "string", "enum": list(STEP_STATUS)},
      "note": {"type": "string"}}, ["topic", "step", "status"]),
    ("learn_prop_add", "Record one claim the learner now holds, with the ids of the claims it rests on. Returns its id.",
     {"topic": {"type": "string"}, "text": {"type": "string"}, "premises": {"type": "array", "items": {"type": "string"}},
      "session": {"type": "string"}}, ["topic", "text"]),
    ("learn_practice_record", "Record a practice outcome (again, hard, good, easy) for one claim and schedule its next review.",
     {"id": {"type": "string"}, "outcome": {"type": "string", "enum": list(OUTCOMES)}}, ["id", "outcome"]),
]
READ_ONLY = {"learn_read"}
FUNCS = {"learn_read": learn_read, "learn_topic_set": learn_topic_set, "learn_step_set": learn_step_set,
         "learn_prop_add": learn_prop_add, "learn_practice_record": learn_practice_record}


def tools_list():
    return [{"name": n, "description": d, "inputSchema": {"type": "object", "properties": p, "required": r},
             "annotations": {"readOnlyHint": n in READ_ONLY}} for n, d, p, r in TOOLS]


def handle(board, message):
    params = message.get("params") or {}
    name, args = str(params.get("name", "")), params.get("arguments") or {}
    try:
        result = mb._text(FUNCS[name](board, args if isinstance(args, dict) else {}), False)
    except ToolError as exc:
        result = mb._text({"error": exc.code, "message": str(exc), **exc.data}, True)
    except Exception as exc:  # noqa: BLE001
        result = mb._text({"error": "internal", "message": str(exc)}, True)
    return {"jsonrpc": "2.0", "id": message.get("id"), "result": result}
