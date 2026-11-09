"""Todo list with one-click AI hand-off (T1).

A todo is a line Owner (or the Router) jots down. One owner click sends it on:

- ``plan``: starts a Plan session through ``planner.start`` (Claude Code asks a few questions, then
  drafts; the draft stays unrunnable until Owner accepts it).
- ``project``: picks the best active project by keyword scoring over project titles and briefs
  (deterministic first; an injectable ``MODEL_RUNNER(prompt) -> text`` only breaks ties or names a
  project when nothing matches) and adds one task with a machine-checkable done-check.
- ``run``: makes a one-task project (``todo-<id>``) that the Autonomous tab can start. Nothing is
  started here; a run begins only when Owner starts it from Autonomous.

Every hand-off is recorded on the todo (``handoffs``: where it went, when, by whom) and as one
plain-English ``todo`` event. Todos live in their own ``todos`` table, created with
``CREATE TABLE IF NOT EXISTS`` through the store connection; ``store.py`` is untouched. Adding is
open to either credential; changing, merging and handing off are owner only. Nothing here bumps
the plan revision except the task added by a project hand-off (``store.add_node``), which honours
an optional ``revision`` in the request (409 when stale).

Injection points (tests replace these; none makes a provider call by default): ``PLAN_HOOK(store,
title, goal, cwd=None, actor=None, app=None) -> session view``, ``MODEL_RUNNER(prompt) -> str``
(also ``app.todo_model_runner``) and ``CHECK_HOOK(goal, cwd=None) -> check`` (default
``failure_doctor.draft_check`` when that module exists).
"""
from __future__ import annotations

import inspect
import json
import re
import secrets
from datetime import datetime, timedelta, timezone

from . import config, plan
from .api import HttpError, ROUTER, _owner_only
from .store import OWNER, now_iso

STATUSES = ("open", "handed_off", "done", "dropped")
TARGETS = ("plan", "project", "run")
TARGET_WORD = {"plan": "Plan", "project": "a project", "run": "Autonomous"}
MAX_TEXT = 2000
TITLE_LIMIT = 80
RUN_PREFIX = "todo-"
AUTO_CHECKS = ("command", "files_exist", "text_present")
NOTE_MARK = "Result:"

# Project-matching knobs: a title hit is worth more than a brief hit.
TITLE_WEIGHT, BRIEF_WEIGHT = 3, 1
CONFIDENT_MIN, CONFIDENT_GAP = 3, 2
MODEL_CANDIDATES = 12

STOP = frozenset("""
a about above after again all also am an and any are as at be been before being below between both but by
can could did do does doing down during each few for from further had has have having her here hers him his
how i if in into is it its just me more most my no nor not now of off on once only or other our out over own
same she should so some such than that the their them then there these they this those through to too under
until up very was we were what when where which while who whom why will with would you your
add make new get use please need needs want todo
""".split())

# Contract: computer items are triaged on add. Kept False at module level so the pre-existing tests (which
# hand off by hand) stay green; the engine turns it on with ``app.todo_auto_triage = True``.
AUTO_TRIAGE = True  # computer to-dos are triaged to a project or a run on add (Owner, 5 Nov 2026)
PLAN_HOOK = None
MODEL_RUNNER = None
CHECK_HOOK = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS todos(
  id TEXT PRIMARY KEY, text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT, updated_at TEXT, created_by TEXT, handoffs_json TEXT DEFAULT '[]', merged_into TEXT)
"""


# ---------------------------------------------------------------- storage
SECTIONS = ("computer", "human")
ARCHIVE_AFTER = timedelta(hours=24)
NEW_COLUMNS = (("section", "TEXT DEFAULT 'computer'"), ("position", "INTEGER"), ("link", "TEXT"),
               ("node_json", "TEXT"), ("deliverables_json", "TEXT DEFAULT '[]'"),
               ("first_clicked_at", "TEXT"), ("archived_at", "TEXT"), ("done_by", "TEXT"))


def ensure_schema(store):
    if getattr(store, "_todo_schema_ok", False):
        return
    with store.tx() as c:
        c.execute(SCHEMA)
        have = {r[1] for r in c.execute("PRAGMA table_info(todos)").fetchall()}
        for name, decl in NEW_COLUMNS:
            if name not in have:
                c.execute(f"ALTER TABLE todos ADD COLUMN {name} {decl}")
    store._todo_schema_ok = True


def _view(row) -> dict:
    d = dict(row)
    d["handoffs"] = json.loads(d.pop("handoffs_json", None) or "[]")
    d["done"] = d["status"] == "done"
    d["section"] = d.get("section") or "computer"
    d["node"] = _loads(d.pop("node_json", None), None)
    d["deliverables"] = _loads(d.pop("deliverables_json", None), [])
    fc = d.get("first_clicked_at")
    due = None
    if fc:
        t = _parse(fc)
        due = (t + ARCHIVE_AFTER).isoformat(timespec="seconds") if t else None
    d["archive_due_at"] = due
    return d


def _loads(raw, default):
    try:
        v = json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default
    return default if v is None else v


def _parse(stamp):
    try:
        t = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _notify(store, d):
    fn = getattr(store, "notify", None)
    if callable(fn):
        try:
            fn("todo", {"id": d.get("id"), "section": d.get("section"), "status": d.get("status")})
        except Exception:
            pass


def list_todos(store, archived=False) -> list:
    ensure_schema(store)
    where = "" if archived else "WHERE archived_at IS NULL"
    return [_view(r) for r in store._q(
        f"SELECT * FROM todos {where} ORDER BY (position IS NULL), position, created_at, rowid")]


def get_todo(store, todo_id):
    ensure_schema(store)
    rows = store._q("SELECT * FROM todos WHERE id=?", (todo_id,))
    if not rows:
        raise KeyError(f"no todo {todo_id}")
    return _view(rows[0])


def _check_section(section):
    section = section or "computer"
    if section not in SECTIONS:
        raise ValueError(f"section must be one of {', '.join(SECTIONS)}")
    return section


def _clean_node(node):
    if node in (None, ""):
        return None
    if not isinstance(node, dict) or not node.get("project") or not node.get("id"):
        raise ValueError('node must be {"project", "id"}')
    return {"project": str(node["project"]), "id": str(node["id"])}


def _clean_deliverables(items):
    if items in (None, ""):
        return []
    if not isinstance(items, list) or not all(isinstance(i, dict) and i.get("label") for i in items):
        raise ValueError("deliverables must be a list of objects with a label")
    return items


def add_todo(store, text, actor=OWNER, section=None, position=None, link=None, node=None,
             deliverables=None) -> dict:
    text = " ".join(str(text or "").split()) if "\n" not in str(text or "") else str(text).strip()
    if not text:
        raise ValueError("a todo needs some text")
    if len(text) > MAX_TEXT:
        raise ValueError(f"keep a todo under {MAX_TEXT} characters")
    section = _check_section(section)
    node = _clean_node(node)
    deliverables = _clean_deliverables(deliverables)
    position = None if position in ("", None) else int(position)
    ensure_schema(store)
    if position is None and section == "human":
        row = store._q("SELECT MAX(position) FROM todos WHERE section='human'")
        position = int(row[0][0] or 0) + 1
    tid = "td-" + secrets.token_hex(3)
    stamp = now_iso()
    with store.tx() as c:
        c.execute("INSERT INTO todos(id, text, status, created_at, updated_at, created_by, handoffs_json,"
                  " section, position, link, node_json, deliverables_json)"
                  " VALUES(?,?, 'open', ?, ?, ?, '[]', ?,?,?,?,?)",
                  (tid, text, stamp, stamp, actor, section, position, link or None,
                   json.dumps(node) if node else None, json.dumps(deliverables)))
    out = get_todo(store, tid)
    _notify(store, out)
    return out


def _save(store, tid, **fields):
    cols = sorted(fields)
    with store.tx() as c:
        c.execute(f"UPDATE todos SET {', '.join(k + '=?' for k in cols)}, updated_at=? WHERE id=?",
                  (*[fields[k] for k in cols], now_iso(), tid))


_UNSET = object()


def update_todo(store, tid, text=None, status=None, done=None, section=_UNSET, position=_UNSET,
                link=_UNSET, node=_UNSET, deliverables=_UNSET) -> dict:
    cur = get_todo(store, tid)
    fields = {}
    if text is not None:
        text = str(text).strip()
        if not text or len(text) > MAX_TEXT:
            raise ValueError(f"a todo needs 1-{MAX_TEXT} characters of text")
        fields["text"] = text
    if status is not None:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        fields["status"] = status
    if done is not None and "status" not in fields:
        if done:
            fields["status"] = "done"
        elif cur["status"] == "done":
            fields["status"] = "handed_off" if cur["handoffs"] else "open"
    if section is not _UNSET:
        fields["section"] = _check_section(section)
    if position is not _UNSET:
        fields["position"] = None if position in ("", None) else int(position)
    if link is not _UNSET:
        fields["link"] = link or None
    if node is not _UNSET:
        n = _clean_node(node)
        fields["node_json"] = json.dumps(n) if n else None
    if deliverables is not _UNSET:
        fields["deliverables_json"] = json.dumps(_clean_deliverables(deliverables))
    if not fields:
        raise ValueError("nothing to change; send text, status or done")
    if fields.get("status") == "done" and cur["status"] != "done":
        fields["done_by"] = OWNER
    _save(store, tid, **fields)
    out = get_todo(store, tid)
    _notify(store, out)
    return out


# ---------------------------------------------------------------- matching
def _stem(word):
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[:-len(suffix)]
            break
    return word[:-1] if len(word) > 3 and word.endswith("e") else word


def tokens(text) -> list:
    """Lower-case content words with a light stem, in order, without stop words."""
    out = []
    for w in re.findall(r"[a-z0-9]+", str(text or "").lower()):
        if len(w) < 3 or w in STOP:
            continue
        out.append(_stem(w))
    return out


def destinations(store) -> list:
    """Active, unarchived projects a todo may be added to (not drafts, not one-task todo runs)."""
    out = []
    for p in store.list_projects():
        if (p.get("status") or "active") != "active" or str(p["id"]).startswith(RUN_PREFIX):
            continue
        out.append(p)
    return out


def rank_projects(store, text) -> list:
    """``[{project, title, score, words}]``, best first; only projects that share a word."""
    want = set(tokens(text))
    ranked = []
    for p in destinations(store):
        title = set(tokens(p.get("title"))) | set(tokens(str(p["id"]).replace("-", " ").replace("_", " ")))
        brief = set(tokens(p.get("brief")))
        hit_t, hit_b = sorted(want & title), sorted((want & brief) - title)
        score = TITLE_WEIGHT * len(hit_t) + BRIEF_WEIGHT * len(hit_b)
        if score:
            ranked.append({"project": p["id"], "title": p.get("title") or p["id"], "score": score,
                           "words": hit_t + hit_b})
    ranked.sort(key=lambda r: (-r["score"], r["project"]))
    return ranked


def propose_project(store, text):
    """The keyword-scored best project for a todo, or None when nothing fits clearly (no model call)."""
    ranked = rank_projects(store, text)
    if not ranked:
        return None
    best = ranked[0]
    second = ranked[1]["score"] if len(ranked) > 1 else 0
    if best["score"] >= CONFIDENT_MIN and best["score"] - second >= CONFIDENT_GAP:
        return best
    return None


def _model_runner(h):
    return MODEL_RUNNER or getattr(getattr(h, "app", None), "todo_model_runner", None)


def _ask_model(runner, text, projects):
    lines = "\n".join(f"- {p['id']}: {p.get('title') or p['id']}. {str(p.get('brief') or '')[:240]}" for p in projects)
    prompt = ("Pick the one project that best fits this todo. Answer with the project id only.\n"
              f"Todo: {text}\nProjects:\n{lines}\n")
    try:
        reply = str(runner(prompt) or "")
    except Exception:
        return None
    for p in projects:
        if re.search(r"(?<![\w-])" + re.escape(p["id"]) + r"(?![\w-])", reply, re.I):
            return p["id"]
    return None


def choose_project(store, text, explicit=None, runner=None) -> dict:
    """``{project, title, why}`` or raises 404/409 ``HttpError``."""
    options = {p["id"]: p for p in destinations(store)}
    if explicit:
        p = store.get_project(explicit)
        if p is None:
            raise HttpError(404, {"error": "not_found", "detail": f"no project {explicit}"})
        if explicit not in options:
            raise HttpError(409, {"error": "not_runnable", "detail": "that project is a draft or archived; accept it first"})
        return {"project": explicit, "title": p.get("title") or explicit, "why": "You chose this project."}
    ranked = rank_projects(store, text)
    if ranked:
        best = ranked[0]
        second = ranked[1]["score"] if len(ranked) > 1 else 0
        if best["score"] >= CONFIDENT_MIN and best["score"] - second >= CONFIDENT_GAP:
            return {"project": best["project"], "title": best["title"],
                    "why": "Matches " + ", ".join(best["words"][:4]) + "."}
    if runner is not None and options:
        pool = [options[r["project"]] for r in ranked[:MODEL_CANDIDATES]] or list(options.values())[:MODEL_CANDIDATES]
        pick = _ask_model(runner, text, pool)
        if pick:
            p = options[pick]
            return {"project": pick, "title": p.get("title") or pick, "why": "The model judged it the best fit."}
    if ranked and (len(ranked) == 1 or ranked[0]["score"] > ranked[1]["score"]):
        best = ranked[0]
        return {"project": best["project"], "title": best["title"],
                "why": "Closest match: " + ", ".join(best["words"][:4]) + "."}
    cands = ranked or [{"project": p["id"], "title": p.get("title") or p["id"], "score": 0, "words": []}
                       for p in options.values()]
    raise HttpError(409, {"error": "no_match", "detail": "No project fits this clearly; pick one.",
                          "candidates": cands[:MODEL_CANDIDATES]})


# ---------------------------------------------------------------- done-checks
def valid_auto_check(check):
    """The check dict when it is machine-checkable and complete, else None. Accepts ``{"check": {...}}``."""
    if isinstance(check, dict) and isinstance(check.get("check"), dict):
        check = check["check"]
    if not isinstance(check, dict):
        return None
    kind = check.get("type")
    if kind == "command" and str(check.get("run") or "").strip():
        return check
    if kind == "files_exist" and isinstance(check.get("paths"), list) and check["paths"]:
        return check
    if kind == "text_present" and check.get("path") and check.get("text"):
        return check
    return None


def _check_function():
    if CHECK_HOOK is not None:
        return CHECK_HOOK
    try:
        from . import failure_doctor
    except Exception:  # not built yet or broken: the fallback check covers it
        return None
    fn = getattr(failure_doctor, "draft_check", None)
    return fn if callable(fn) else None


def drafted_check(goal, cwd=None):
    """A check from ``failure_doctor.draft_check`` when it is importable and returns a usable one."""
    fn = _check_function()
    if fn is None:
        return None
    try:
        try:
            takes_cwd = "cwd" in inspect.signature(fn).parameters
        except (TypeError, ValueError):
            takes_cwd = False
        result = fn(goal, cwd=cwd) if takes_cwd else fn(goal)
    except Exception:
        return None
    return valid_auto_check(result)


def note_path(node_id):
    return f"todo/{node_id}.md"


def fallback_check(node_id):
    return {"type": "text_present", "path": note_path(node_id), "text": NOTE_MARK}


def task_for(node_id, text, cwd=None) -> dict:
    """``{goal, check, check_source}`` for a todo turned into a task."""
    check = drafted_check(text, cwd)
    if check is not None:
        return {"goal": text, "check": check, "check_source": "failure_doctor"}
    goal = (f"{text}\n\nWhen it is done, write a short note about the outcome to {note_path(node_id)}; "
            f"its first line must start with \"{NOTE_MARK}\".")
    return {"goal": goal, "check": fallback_check(node_id), "check_source": "fallback"}


def title_of(text) -> str:
    first = re.split(r"(?<=[.!?])\s|\n", str(text).strip(), maxsplit=1)[0].strip() or str(text).strip()
    return first if len(first) <= TITLE_LIMIT else first[:TITLE_LIMIT - 1].rstrip() + "…"


# ---------------------------------------------------------------- hand-off
def _record(store, tid, to, ref, actor, summary):
    todo = get_todo(store, tid)
    entry = {"to": to, "at": now_iso(), "by": actor, "ref": ref, "summary": summary}
    status = "done" if todo["status"] == "done" else "handed_off"
    _save(store, tid, handoffs_json=json.dumps(todo["handoffs"] + [entry]), status=status)
    store.event(ref.get("project"), ref.get("node"), "todo", actor, summary, {"todo": tid, "to": to})
    return entry


def _default_plan(store, title, goal, cwd=None, actor=None, app=None):
    from . import planner
    return planner.start(store, title, goal, cwd, actor=actor, app=app)


def _unique(taken, base):
    name, n = base, 1
    while name in taken:
        n += 1
        name = f"{base}-{n}"
    return name


def hand_off_plan(h, todo):
    hook = PLAN_HOOK or _default_plan
    title = title_of(todo["text"])
    try:
        view = hook(h.app.store, title, todo["text"], None, actor=h.actor, app=h.app)
    except (ImportError, AttributeError) as exc:
        raise HttpError(503, {"error": "unavailable", "detail": f"Plan mode is not available: {exc}"})
    sid = (view or {}).get("id") or (view or {}).get("session")
    if not sid:
        raise HttpError(503, {"error": "unavailable", "detail": "Plan mode did not start a session"})
    ref = {"session": sid}
    result = {"to": "plan", "session": sid, "status": (view or {}).get("status"), "title": title}
    return ref, result, f"{config.OWNER_NAME} sent the todo '{title}' to Plan to be drafted."


def hand_off_project(h, todo, body):
    store = h.app.store
    pick = choose_project(store, todo["text"], explicit=body.get("project"), runner=_model_runner(h))
    project = store.get_project(pick["project"])
    node_id = _unique({n["id"] for n in store.list_nodes(pick["project"])}, RUN_PREFIX + todo["id"].split("-", 1)[-1])
    task = task_for(node_id, todo["text"], project.get("cwd"))
    node = {"id": node_id, "title": title_of(todo["text"]), "goal": task["goal"], "check": task["check"],
            "deps": [], "owner": "todo", "lane": "todo"}
    revision = body.get("revision")
    rev = store.add_node(pick["project"], node, h.actor,
                         f"{config.OWNER_NAME} added the todo '{node['title']}' to {pick['title']} as task {node_id}.",
                         expected_revision=int(revision) if revision not in (None, "") else None)
    ref = {"project": pick["project"], "node": node_id, "title": pick["title"], "check": task["check"]}
    result = {"to": "project", "project": pick["project"], "project_title": pick["title"], "node": node_id,
              "check": task["check"], "check_source": task["check_source"], "why": pick["why"], "revision": rev}
    return ref, result, f"{config.OWNER_NAME} added the todo '{node['title']}' to {pick['title']} as task {node_id}."


def hand_off_run(h, todo):
    store = h.app.store
    existing = {p["id"] for p in store.list_projects(include_archived=True)}
    pid = _unique(existing, RUN_PREFIX + todo["id"].split("-", 1)[-1])
    node_id = "task"
    title = title_of(todo["text"])
    task = task_for(node_id, todo["text"], None)
    plan.import_plan(store, {
        "id": pid, "title": title, "brief": todo["text"], "cwd": None,
        "nodes": [{"id": node_id, "title": title, "goal": task["goal"], "check": task["check"], "deps": [],
                   "owner": "todo", "lane": "todo"}]}, actor=OWNER, draft=False)
    ref = {"project": pid, "node": node_id, "check": task["check"]}
    result = {"to": "run", "project": pid, "node": node_id, "title": title, "check": task["check"],
              "check_source": task["check_source"]}
    return ref, result, f"{config.OWNER_NAME} queued the todo '{title}' as a one-task run; start it from Autonomous."


def hand_off(h, tid, to, body=None) -> dict:
    body = body or {}
    if to not in TARGETS:
        raise HttpError(400, {"error": "bad_request", "detail": f"to must be one of {', '.join(TARGETS)}"})
    store = h.app.store
    todo = get_todo(store, tid)
    if todo["status"] in ("done", "dropped"):
        raise HttpError(409, {"error": "closed", "detail": f"this todo is {todo['status']}; reopen it first"})
    prior = [e for e in todo["handoffs"] if e["to"] == to]
    if prior and not body.get("again"):
        raise HttpError(409, {"error": "already_handed_off", "detail": f"already sent to {TARGET_WORD[to]}",
                              "handoff": prior[-1]})
    if to == "plan":
        ref, result, summary = hand_off_plan(h, todo)
    elif to == "project":
        ref, result, summary = hand_off_project(h, todo, body)
    else:
        ref, result, summary = hand_off_run(h, todo)
    entry = _record(store, tid, to, ref, h.actor, summary)
    result["handoff"] = entry
    return result


# ---------------------------------------------------------------- routes
def _need_id(b):
    tid = str(b.get("id") or "").strip()
    if not tid:
        raise HttpError(400, {"error": "bad_request", "detail": "missing id"})
    return tid


def _guard(fn):
    """Routes called directly (tests, other modules) raise ``HttpError`` rather than Value/KeyError."""
    def wrapped(h, q):
        try:
            return fn(h, q)
        except KeyError as exc:
            raise HttpError(404, {"error": "not_found", "detail": str(exc).strip("'\"")})
        except ValueError as exc:
            raise HttpError(400, {"error": "bad_request", "detail": str(exc)})
    wrapped.__name__ = fn.__name__
    return wrapped


AUTO_TICK_SECTIONS = ("computer",)


def may_auto_tick(t) -> bool:
    """Only computer items are ticked when their linked task is done. A human ("you") item asks
    Owner to decide or approve something; a worker finishing the task must never tick it, or the
    approval is lost. Owner ticks those himself."""
    return (t.get("section") or "computer") in AUTO_TICK_SECTIONS


def housekeep(store, now=None) -> int:
    """Auto-tick computer items whose node is done; archive items first clicked 24h or more ago."""
    ensure_schema(store)
    stamp = now if isinstance(now, str) else (now.isoformat(timespec="seconds") if now else now_iso())
    now_t = _parse(stamp)
    changed = 0
    for t in list_todos(store):
        fields = {}
        n = t["node"]
        if n and t["status"] in ("open", "handed_off") and may_auto_tick(t):
            try:
                node = store.get_node(n["project"], n["id"])
            except Exception:
                node = None
            if node and node.get("status") == "done":
                fields.update(status="done", done_by="engine")
                if not t["deliverables"]:
                    fields["deliverables_json"] = json.dumps(
                        [{"label": "Task result", "project": n["project"], "node": n["id"]}])
        fc = _parse(t["first_clicked_at"]) if t["first_clicked_at"] else None
        if fc and now_t and now_t - fc >= ARCHIVE_AFTER:
            fields["archived_at"] = stamp
        if fields:
            _save(store, t["id"], **fields)
            _notify(store, get_todo(store, t["id"]))
            changed += 1
    return changed


def _flag(q, key):
    v = (q or {}).get(key)
    if isinstance(v, (list, tuple)):
        v = v[0] if v else None
    return str(v or "").lower() in ("1", "true", "yes")


def g_list(h, q):
    store = h.app.store
    try:
        housekeep(store)
    except Exception:
        pass
    show_archived = _flag(q, "archived")
    todos = list_todos(store, archived=show_archived)
    live = [t for t in todos if not t["archived_at"]]
    sections = {s: [t for t in live if t["section"] == s] for s in SECTIONS}

    def is_open(t):
        return t["status"] in ("open", "handed_off")

    archived_n = store._q("SELECT COUNT(*) FROM todos WHERE archived_at IS NOT NULL")[0][0]
    ordered = sections["computer"] + sections["human"]
    return {"todos": todos if show_archived else ordered, "sections": sections,
            "counts": {"open": sum(1 for t in live if is_open(t)), "total": len(live),
                       "human_open": sum(1 for t in sections["human"] if is_open(t)),
                       "computer_open": sum(1 for t in sections["computer"] if is_open(t)),
                       "archived": archived_n}}


def triage(h, todo):
    """Hand a computer item off to a project (clear best match) or a one-task run. Never raises."""
    store = h.app.store
    try:
        best = propose_project(store, todo["text"])
        to, body = ("project", {"project": best["project"]}) if best else ("run", {})
        result = hand_off(h, todo["id"], to, body)
        _save(store, todo["id"], node_json=json.dumps({"project": result["project"], "id": result["node"]}))
    except Exception as exc:
        detail = getattr(exc, "body", None) or str(exc)
        if isinstance(detail, dict):
            detail = detail.get("detail") or detail.get("error") or str(detail)
        try:
            entry = {"to": "error", "at": now_iso(), "by": h.actor, "ref": {},
                     "summary": "Hand-off failed: " + str(detail)}
            _save(store, todo["id"], handoffs_json=json.dumps(get_todo(store, todo["id"])["handoffs"] + [entry]))
        except Exception:
            pass
    out = get_todo(store, todo["id"])
    _notify(store, out)
    return out


def p_add(h, q):
    b = h._json_body()
    todo = add_todo(h.app.store, b.get("text"), actor=h.actor, section=b.get("section"),
                    position=b.get("position"), link=b.get("link"), node=b.get("node"),
                    deliverables=b.get("deliverables"))
    want = b.get("triage")
    if want is None:
        want = getattr(h.app, "todo_auto_triage", AUTO_TRIAGE)
    if todo["section"] == "computer" and want and not todo["node"]:
        todo = triage(h, todo)
    return {"ok": True, "todo": todo}


def p_update(h, q):
    _owner_only(h, "change a todo")
    b = h._json_body()
    extra = {k: b[k] for k in ("section", "position", "link", "node", "deliverables") if k in b}
    return {"ok": True, "todo": update_todo(h.app.store, _need_id(b), text=b.get("text"),
                                            status=b.get("status"), done=b.get("done"), **extra)}


def p_click(h, q):
    _owner_only(h, "open a todo")
    store = h.app.store
    tid = _need_id(h._json_body())
    cur = get_todo(store, tid)
    if not cur["first_clicked_at"]:
        _save(store, tid, first_clicked_at=now_iso())
        cur = get_todo(store, tid)
        _notify(store, cur)
    return {"ok": True, "todo": cur}


def p_reorder(h, q):
    if getattr(h, "actor", None) not in (OWNER, ROUTER):
        raise HttpError(403, {"error": "forbidden", "detail": "not allowed to reorder todos"})
    b = h._json_body()
    section = _check_section(b.get("section"))
    ids = b.get("ids")
    if not isinstance(ids, list):
        raise HttpError(400, {"error": "bad_request", "detail": "ids must be a list"})
    store = h.app.store
    ensure_schema(store)
    ids = [str(i) for i in dict.fromkeys(ids)]
    for i in ids:
        get_todo(store, i)
    with store.tx() as c:
        for n, i in enumerate(ids, 1):
            c.execute("UPDATE todos SET position=?, section=?, updated_at=? WHERE id=?", (n, section, now_iso(), i))
    _notify(store, {"id": None, "section": section, "status": "reordered"})
    return {"ok": True, "section": section, "todos": [get_todo(store, i) for i in ids]}


def p_handoff(h, q):
    _owner_only(h, "hand a todo off")
    b = h._json_body()
    tid = _need_id(b)
    result = hand_off(h, tid, b.get("to"), b)
    return {"ok": True, "result": result, "todo": get_todo(h.app.store, tid)}


def p_merge(h, q):
    _owner_only(h, "merge todos")
    b = h._json_body()
    store = h.app.store
    keep = get_todo(store, str(b.get("keep") or ""))
    ids = b.get("merge")
    if not isinstance(ids, list) or not ids:
        raise HttpError(400, {"error": "bad_request", "detail": "merge needs a list of todo ids"})
    merged = []
    for tid in dict.fromkeys(str(i) for i in ids):
        if tid == keep["id"]:
            continue
        get_todo(store, tid)
        _save(store, tid, status="dropped", merged_into=keep["id"])
        merged.append(get_todo(store, tid))
    if merged:
        store.event(None, None, "todo", h.actor,
                    f"{config.OWNER_NAME} merged {len(merged)} duplicate todo{'s' if len(merged) != 1 else ''} into '{title_of(keep['text'])}'.",
                    {"todo": keep["id"], "merged": [m["id"] for m in merged]})
    return {"ok": True, "todo": get_todo(store, keep["id"]), "merged": merged}


ROUTES = {
    "GET /api/todos": g_list,
    "POST /api/todos": _guard(p_add),
    "POST /api/todos/update": _guard(p_update),
    "POST /api/todos/click": _guard(p_click),
    "POST /api/todos/reorder": _guard(p_reorder),
    "POST /api/todos/handoff": _guard(p_handoff),
    "POST /api/todos/merge": _guard(p_merge),
}
