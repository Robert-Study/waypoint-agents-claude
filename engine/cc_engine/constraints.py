"""The learning edge: every accepted task leaves one line that later planning must respect.

When a node is accepted (status ``done``), ``derive`` writes one deterministic line from its goal,
check and evidence::

    ACCEPTED: <id> <title>: <goal> / DERIVED: <what must stay true> / LANDS IN: <files or area>

Lines are stored per project in their own ``constraints`` table (never the plan revision).
``sync`` records every done node that has no line yet, so nothing needs a hook in the check runner;
``record_accepted`` records one node at once. A model may phrase the DERIVED part
(``runner(node, draft) -> str``, injectable, off by default because code costs nothing); a failing
or empty model reply falls back to the template.

Planner and deep-plan prompts include ``relevant`` lines (keyword overlap with the goal), and task
briefs include ``for_task`` lines (from upstream tasks, then keyword matches), across projects.
``GET /api/constraints?project=`` lists them (Brain shows them as "Rules learned").
"""
from __future__ import annotations

import json
import re

from .api import HttpError
from .store import now_iso

SCHEMA = ("CREATE TABLE IF NOT EXISTS constraints(project_id TEXT NOT NULL, node_id TEXT NOT NULL, accepted TEXT,"
          " derived TEXT, lands_in TEXT, line TEXT, source TEXT, created_at TEXT, PRIMARY KEY(project_id, node_id))")
BRIEFS_PREFIX = "deep_plan:briefs:"  # deep_plan stores each task's owned files here
LINE_MAX = 400
PART_MAX = {"accepted": 160, "derived": 200, "lands_in": 120}
MIN_OVERLAP = 2
LIST_LIMIT = 200
PATH_EXT = re.compile(r"\.(py|mjs|js|ts|json|md|html|css|ps1|txt|csv|ya?ml|toml|sql|sh)$", re.I)
EVIDENCE = re.compile(r"\b\d+ passed\b[^\n]*", re.I)
WORD = re.compile(r"[a-z0-9]+")
STOP = frozenset("""
the and for with that this from into onto each every task tasks done pass passes passed passing check checks
keep must when then only will should have has are was were its their them they not all any one two new use
using used can run runs python pytest test tests exit accepted derived lands owner file files make makes now
out per via own than also more less after before later show shows project present contains does make sure
what which who where why how there here been being under over just very same other about
""".split())

# Injection point: callable(node, draft) -> str for the DERIVED part. None = template only.
MODEL_RUNNER = None


def ensure(store):
    with store.tx() as c:
        c.execute(SCHEMA)


def _one(text, limit):
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"


# ---------------------------------------------------------------- derive
def _paths_in(command) -> list:
    out = []
    for tok in re.split(r"\s+", str(command or "")):
        tok = tok.strip("\"'`,;()")
        if not tok or tok.startswith("-") or "://" in tok:
            continue
        if "/" in tok or "\\" in tok or PATH_EXT.search(tok):
            if tok not in out:
                out.append(tok)
    return out


def lands_in(node, project_id, files=None) -> str:
    check = node.get("check") or {}
    paths = [f for f in files or [] if isinstance(f, str) and f.strip()]
    if not paths:
        t = check.get("type")
        if t == "files_exist":
            paths = [str(p) for p in check.get("paths") or []]
        elif t == "text_present" and check.get("path"):
            paths = [str(check["path"])]
        elif t == "command":
            paths = _paths_in(check.get("run"))
    if paths:
        return ", ".join(paths[:6])
    if node.get("lane"):
        return f"lane {node['lane']} of {project_id}"
    return f"project {project_id}"


def _evidence(node) -> str:
    m = EVIDENCE.search(str(node.get("evidence") or ""))
    return f" (last run: {_one(m.group(0), 60)})" if m else ""


def derived_template(node) -> str:
    check = node.get("check") or {}
    t = check.get("type")
    if t == "command":
        return f"keep `{check.get('run')}` passing" + _evidence(node)
    if t == "files_exist":
        return "keep " + ", ".join(map(str, check.get("paths") or [])) + " in place"
    if t == "text_present":
        return f"keep {check.get('text')!r} in {check.get('path')}"
    if t == "human":
        return f"Owner approved \"{check.get('prompt')}\"; a change here needs his eye again"
    return "keep its accepted result true"


def derive(node, project_id, files=None, runner=None) -> dict:
    """The constraint for one accepted node (deterministic unless a model ``runner`` is given)."""
    nid = node.get("id")
    accepted = _one(f"{nid} {node.get('title') or nid}: {node.get('goal') or ''}", PART_MAX["accepted"])
    derived, source = _one(derived_template(node), PART_MAX["derived"]), "template"
    where = _one(lands_in(node, project_id, files), PART_MAX["lands_in"])
    runner = runner or MODEL_RUNNER
    if runner is not None:
        try:
            text = _one(runner(node, {"accepted": accepted, "derived": derived, "lands_in": where}),
                        PART_MAX["derived"])
            if text:
                derived, source = text, "model"
        except Exception:  # the learning edge never blocks on a model
            pass
    line = _one(f"ACCEPTED: {accepted} / DERIVED: {derived} / LANDS IN: {where}", LINE_MAX)
    return {"project": project_id, "node": nid, "accepted": accepted, "derived": derived, "lands_in": where,
            "line": line, "source": source}


# ---------------------------------------------------------------- store
def _files_for(store, project_id, node_id):
    raw = store.get_meta(BRIEFS_PREFIX + str(project_id))
    try:
        return list(((json.loads(raw) if raw else {}).get(node_id) or {}).get("files") or [])
    except (ValueError, AttributeError):
        return []


def _save(store, c, replace):
    sql = ("INSERT OR REPLACE" if replace else "INSERT OR IGNORE") + (
        " INTO constraints(project_id, node_id, accepted, derived, lands_in, line, source, created_at)"
        " VALUES(?,?,?,?,?,?,?,?)")
    with store.tx() as conn:
        conn.execute(SCHEMA)
        conn.execute(sql, (c["project"], c["node"], c["accepted"], c["derived"], c["lands_in"], c["line"],
                           c["source"], c["at"]))


def record_accepted(store, project_id, node_id, runner=None) -> dict:
    """Derive and store (or refresh) the constraint of one accepted node."""
    node = store.get_node(project_id, node_id)
    if node is None:
        raise KeyError(f"no node {project_id}/{node_id}")
    if node.get("status") != "done":
        raise ValueError(f"task {node_id} is not accepted yet")
    c = dict(derive(node, project_id, _files_for(store, project_id, node_id), runner=runner), at=now_iso())
    _save(store, c, replace=True)
    return c


def sync(store, project_id=None, runner=None) -> list:
    """Record a constraint for every done node that has none yet. Returns the new ones."""
    ensure(store)
    sql = ("SELECT n.project_id, n.id FROM nodes n LEFT JOIN constraints c"
           " ON c.project_id = n.project_id AND c.node_id = n.id WHERE n.status = 'done' AND c.node_id IS NULL")
    params = ()
    if project_id:
        sql += " AND n.project_id = ?"
        params = (project_id,)
    made = []
    for pid, nid in store._q(sql + " ORDER BY n.rowid", params):
        node = store.get_node(pid, nid)
        c = dict(derive(node, pid, _files_for(store, pid, nid), runner=runner), at=now_iso())
        _save(store, c, replace=False)
        made.append(c)
    return made


def _row(r) -> dict:
    return {"project": r[0], "node": r[1], "accepted": r[2], "derived": r[3], "lands_in": r[4], "line": r[5],
            "source": r[6], "at": r[7]}


def list_constraints(store, project_id=None, limit=LIST_LIMIT) -> list:
    ensure(store)
    sql = "SELECT project_id, node_id, accepted, derived, lands_in, line, source, created_at FROM constraints"
    params: tuple = ()
    if project_id:
        sql += " WHERE project_id = ?"
        params = (project_id,)
    rows = store._q(sql + " ORDER BY created_at DESC, rowid DESC LIMIT ?", params + (int(limit),))
    return [_row(r) for r in rows]


# ---------------------------------------------------------------- relevance
def keywords(text) -> set:
    return {w for w in WORD.findall(str(text or "").lower()) if len(w) >= 3 and w not in STOP}


def relevant(store, text, project=None, exclude=(), limit=5, min_overlap=MIN_OVERLAP) -> list:
    """Constraints sharing at least ``min_overlap`` keywords with ``text``, best first, across all
    projects (``project`` only breaks ties toward that project). ``exclude``: ``(project, node)``."""
    sync(store)
    want = keywords(text)
    if not want:
        return []
    skip = {tuple(x) for x in exclude or ()}
    scored = []
    for c in list_constraints(store, limit=LIST_LIMIT * 5):
        if (c["project"], c["node"]) in skip:
            continue
        hits = len(want & keywords(f"{c['accepted']} {c['derived']} {c['lands_in']}"))
        if hits >= min_overlap:
            scored.append((-hits, 0 if c["project"] == project else 1, c["at"] or "", c))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [t[3] for t in scored[:limit]]


def for_task(store, node, project=None, text=None, limit=6) -> list:
    """Constraints for one task's brief: its upstream tasks' first, then keyword matches."""
    sync(store)
    out, seen = [], set()
    if project:
        mine = {c["node"]: c for c in list_constraints(store, project)}
        for d in node.get("deps") or []:
            d = d.get("id") if isinstance(d, dict) else d
            if d in mine and mine[d]["line"] not in seen:
                seen.add(mine[d]["line"])
                out.append(mine[d])
    text = text if text is not None else f"{node.get('title') or ''} {node.get('goal') or ''}"
    for c in relevant(store, text, project=project, exclude=[(project, node.get("id"))], limit=limit):
        if c["line"] not in seen:
            seen.add(c["line"])
            out.append(c)
    return out[:limit]


# ---------------------------------------------------------------- routes
def g_constraints(h, q):
    store = h.app.store
    project = q.get("project") or None
    if project and store.get_project(project) is None:
        raise HttpError(404, {"error": "not_found", "detail": f"no project {project}"})
    sync(store, project)
    return {"project": project, "constraints": list_constraints(store, project)}


ROUTES = {"GET /api/constraints": g_constraints}
