"""Action queue consumer for the Waypoint runner (Waypoint 1.2, node run-actions).

The web page and the mod write ``actions/<id>.json`` (``waypoint.action.v1``, status ``queued``). This module
carries each one out exactly once and records the outcome in the same file: ``done``, ``failed`` (with ``error``)
or ``refused`` (with ``error``), plus ``finished_at`` and an optional ``result``.

Handler contract (shared with every ``actions_*.py`` module)
- ``HANDLERS`` maps an action type to ``handler(action: dict, ctx) -> dict``. The dict is
  ``{"status": "done"|"failed"|"refused", "result"?: {...}, "error"?: str}``.
- ``load_handlers()`` imports every ``actions_*.py`` module of the package (default ``waypoint_runner``) that has
  ``register(handlers: dict)`` and calls it, so each handler node adds its types without editing this file. A module
  cannot replace a built-in type here; a module that fails to import is recorded in ``load_handlers.errors`` and
  the rest still load.
- ``ctx`` is a dict that also answers attribute reads (``ctx.store``). Keys: ``store`` (``waypoint_data.Store``,
  writer ``runner``), ``root``, ``now`` (epoch seconds), ``claims``, ``dispatcher``, ``check_runner``,
  ``cwd_for(project, node) -> path|None``, ``machines`` and ``handlers``. Build one with ``make_ctx``.

Rules
- Each action file is processed once: a ``<id>.processing`` lock is taken, the file is re-read under it and only a
  ``queued`` action runs. A locked action is skipped, never waited for.
- Owner-only types (accept, reject, owner_status) are refused unless the action says ``by: owner``.
- Node revision check: when the payload names ``rev`` and the node is at another revision the action is refused
  as stale and nothing is written. ``owner_status`` requires ``rev``.
- accept and reject never mark a node done from a receipt. accept runs the node's done-check and only a pass makes
  the node ``done`` (a human check is passed by the owner's accept itself). reject sends the node to ``error``.
- ``run`` and ``retry`` carry a mode (frugal, balanced, push, max) to the dispatcher, which applies toggles and
  floors. This module holds no floor and no percentage.
- Python touches the data folder only through core/waypoint-data/py.
"""
from __future__ import annotations

import importlib
import pkgutil
import sys
from pathlib import Path

_DATA_PY = Path(__file__).resolve().parents[2] / "core" / "waypoint-data" / "py"
if _DATA_PY.is_dir() and str(_DATA_PY) not in sys.path:
    sys.path.insert(0, str(_DATA_PY))

import re  # noqa: E402
import time  # noqa: E402
import secrets  # noqa: E402

from waypoint_data import paths as wpaths  # noqa: E402
from waypoint_data.claims import Claims  # noqa: E402
from waypoint_data.store import LockBusy, RevConflict, Store, acquire_lock, iso  # noqa: E402

from . import node_checks  # noqa: E402
from .dispatch_core import MODES  # noqa: E402
from .ready import dep_ids  # noqa: E402

__all__ = ["HANDLERS", "OWNER_ONLY", "Ctx", "make_ctx", "load_handlers", "process_action", "process_queue"]

WRITER = "runner"
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
NODE_STATUSES = ("not_started", "ready", "running", "submitted", "done", "error", "needs_you", "skipped", "carried")
STARTABLE = ("not_started", "ready")
RETRYABLE = ("error", "needs_you")
AWAITING = ("submitted", "error", "needs_you")  # accept: work finished, or failed and the owner overrules
REJECTABLE = ("submitted", "needs_you")
OWNER_ONLY = ("accept", "reject", "owner_status")
RESULT_STATUSES = ("done", "failed", "refused")
EVIDENCE_MAX = 400
_VOLATILE = ("rev", "updated_at", "updated_by")


class Refuse(Exception):
    """The action may not run now (owner marker missing, stale revision, node in the wrong state)."""


class Fail(Exception):
    """The action cannot run (bad input, unknown node)."""


class Ctx(dict):
    """A dict whose keys can also be read as attributes (``ctx.store``)."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


def make_ctx(root, dispatcher=None, now=time.time, check_runner=None, cwd_for=None, machines=None, handlers=None,
             **extra) -> Ctx:
    """Context for the handlers. ``now`` returns epoch seconds; ``check_runner(cmd, cwd, timeout)`` and
    ``cwd_for(project, node)`` are injectable so tests never run real commands."""
    root = Path(root)
    ctx = Ctx(root=root, store=Store(root, writer=WRITER, now=now), claims=Claims(root, now=now, writer=WRITER),
              dispatcher=dispatcher, now=now, check_runner=check_runner, cwd_for=cwd_for, machines=machines or {},
              handlers=handlers)
    ctx.update(extra)
    return ctx


# ------------------------------------------------------------------ helpers
def _need_owner(action):
    if action.get("by") != "owner":
        raise Refuse(f"{action.get('type')} is an owner action; it needs the owner marker (by=owner), "
                     f"not by={action.get('by')!r}")


def _id(payload, key):
    v = payload.get(key)
    if not isinstance(v, str) or not ID_RE.match(v):
        raise Fail(f"{key} must be a short id (letters, digits, . _ -)")
    return v


def _subdirs(store, rel):
    import os
    try:
        names = os.listdir(store.path(rel))
    except (FileNotFoundError, NotADirectoryError):
        return []
    return sorted(n for n in names if os.path.isdir(os.path.join(store.path(rel), n)) and not n.startswith("."))


def _find_project(store, payload):
    """The project named in the payload, or the one project holding the node."""
    node_id = _id(payload, "node")
    if payload.get("project") is not None:
        pid = _id(payload, "project")
        if not store.exists(f"projects/{pid}/nodes/{node_id}.json"):
            raise Fail(f"no node {node_id} in project {pid}")
        return pid, node_id
    hits = [p for p in _subdirs(store, "projects") if store.exists(f"projects/{p}/nodes/{node_id}.json")]
    if not hits:
        raise Fail(f"no node {node_id} in any project")
    if len(hits) > 1:
        raise Fail(f"node {node_id} is in several projects ({', '.join(hits)}); name the project")
    return hits[0], node_id


def _node_rel(project, node):
    return f"projects/{project}/nodes/{node}.json"


def _load(ctx, payload):
    """(project, node dict) with the revision check applied."""
    store = ctx["store"]
    project, node_id = _find_project(store, payload)
    node = store.read(_node_rel(project, node_id))
    if not isinstance(node, dict):
        raise Fail(f"node {node_id} is unreadable")
    if "rev" in payload and payload["rev"] != node.get("rev"):
        raise Refuse(f"stale revision: the action was made at rev {payload['rev']}, node {node_id} is at rev "
                     f"{node.get('rev')}; refresh and act again")
    return project, node


def _update_node(ctx, node, **changes):
    """Compare-and-swap the node at the rev it was read at; a changed node is a stale revision."""
    new = {k: v for k, v in node.items() if k not in _VOLATILE}
    new.update(changes)
    try:
        return ctx["store"].write(_node_rel(node["project"], node["id"]), new, expected_rev=node["rev"])
    except RevConflict:
        raise Refuse(f"stale revision: node {node['id']} changed while the action ran") from None


def _evidence(text):
    text = " ".join(str(text).split())
    return text if len(text) <= EVIDENCE_MAX else text[: EVIDENCE_MAX - 3] + "..."


def _mode(payload):
    mode = payload.get("mode", "balanced")
    if mode not in MODES:
        raise Fail(f"mode must be one of {', '.join(MODES)}")
    return mode


def _dispatcher(ctx):
    d = ctx.get("dispatcher")
    if d is None:
        raise Fail("no dispatcher configured in the runner")
    return d


def _live_jobs(ctx, project, node_id):
    """Running jobs of a node. Read here because Dispatcher.live_jobs lists files and jobs/ holds one folder per job."""
    store = ctx["store"]
    out = []
    for name in _subdirs(store, "jobs"):
        job = store.read(f"jobs/{name}/state.json")
        if isinstance(job, dict) and job.get("state") == "running" and job.get("node") == node_id                 and job.get("project") == project:
            out.append(job)
    return out


def _project_nodes(ctx, project):
    store = ctx["store"]
    out = []
    for n in store.list_names(f"projects/{project}/nodes"):
        if n.endswith(".json"):
            e = store.read(f"projects/{project}/nodes/{n}")
            if isinstance(e, dict):
                out.append(e)
    return out


def _default_cwd(ctx, project, node_id):
    store = ctx["store"]
    proj = store.read(f"projects/{project}/project.json") or {}
    cwd = proj.get("cwd")
    machines = ctx.get("machines") or {}
    if isinstance(cwd, dict) and machines:
        try:
            return wpaths.to_local_path(cwd, machines)
        except Exception:  # noqa: BLE001 - an unknown machine means no folder
            return None
    return None


def _cwd(ctx, project, node_id):
    fn = ctx.get("cwd_for")
    return fn(project, node_id) if fn else _default_cwd(ctx, project, node_id)


# ------------------------------------------------------------------ run and retry
def _start(ctx, node, mode, requested_by):
    """Dispatch a startable node. Returns the handler result: running node on start, refused with reason on hold."""
    disp = _dispatcher(ctx)
    project = node["project"]
    nodes = _project_nodes(ctx, project)
    res = disp.dispatch(node, project, mode=mode, graph={"nodes": nodes}, cwd=_cwd(ctx, project, node["id"]),
                        requested_by=requested_by)
    if res.status != "started":
        return {"status": "refused", "error": f"held: {res.reason}",
                "result": {"held": res.reason, "todos": list(res.todos), "mode": mode}}
    fresh = ctx["store"].read(_node_rel(project, node["id"])) or node
    _update_node(ctx, fresh, status="running", attempts=int(fresh.get("attempts") or 0) + 1, actor=res.adapter)
    return {"status": "done", "result": {"job_id": res.job_id, "adapter": res.adapter, "mode": mode}}


def h_run(action, ctx):
    payload = action.get("payload") or {}
    mode = _mode(payload)
    project, node = _load(ctx, payload)
    _dispatcher(ctx)
    if node.get("status") not in STARTABLE:
        raise Refuse(f"node {node['id']} is {node.get('status')}, not ready to run")
    by_id = {n["id"]: n for n in _project_nodes(ctx, project)}
    open_deps = [d for d in dep_ids(node) if by_id.get(d, {}).get("status") != "done"]
    if open_deps:
        raise Refuse(f"node {node['id']} waits for {', '.join(open_deps)}")
    return _start(ctx, node, mode, WRITER)


def h_retry(action, ctx):
    payload = action.get("payload") or {}
    mode = _mode(payload)
    project, node = _load(ctx, payload)
    _dispatcher(ctx)
    if node.get("status") not in RETRYABLE:
        raise Refuse(f"node {node['id']} is {node.get('status')}; only an error or needs_you node can be retried")
    reset = _update_node(ctx, node, status="ready", evidence=_evidence(f"retry requested ({node.get('status')})"))
    out = _start(ctx, reset, mode, WRITER)
    if out["status"] == "refused":
        out["error"] += "; the node was reset to ready"
        out["result"]["reset"] = True
    return out


# ------------------------------------------------------------------ stop
def h_stop(action, ctx):
    payload = action.get("payload") or {}
    project, node = _load(ctx, payload)
    disp = _dispatcher(ctx)
    jobs = _live_jobs(ctx, project, node["id"])
    if not jobs and node.get("status") != "running":
        raise Refuse(f"nothing is running for node {node['id']}")
    stopped = []
    for j in jobs:
        disp.stop(j["id"], reason=f"stopped by {action.get('by')}")
        stopped.append(j["id"])
    fresh = ctx["store"].read(_node_rel(project, node["id"])) or node
    if fresh.get("status") == "running":
        _update_node(ctx, fresh, status="ready", evidence=_evidence(f"stopped by {action.get('by')}"))
    return {"status": "done", "result": {"stopped": stopped}}


# ------------------------------------------------------------------ claim and release
def h_claim(action, ctx):
    payload = action.get("payload") or {}
    project, node = _load(ctx, payload)
    who = payload.get("by") or action.get("by") or "worker"
    location = payload.get("location") or "pc"
    got = ctx["claims"].claim(project, node["id"], "runner", str(location), secrets.token_hex(8),
                              claimed_by=str(who)[:64])
    if not got.ok:
        holder = (got.holder or {}).get("claimed_by") or (got.holder or {}).get("owner")
        raise Refuse(f"node {node['id']} is held" + (f" by {holder}" if holder else ""))
    return {"status": "done", "result": {"node": node["id"], "claimed_by": str(who)}}


def h_release(action, ctx):
    payload = action.get("payload") or {}
    project, node = _load(ctx, payload)
    claim = ctx["claims"].read(project, node["id"])
    if not isinstance(claim, dict):
        raise Refuse(f"node {node['id']} is not claimed")
    if not ctx["claims"].release(project, node["id"], claim.get("token")):
        raise Refuse(f"the claim on node {node['id']} changed; try again")
    return {"status": "done", "result": {"node": node["id"], "released_from": claim.get("claimed_by")}}


# ------------------------------------------------------------------ accept, reject, owner status
def h_accept(action, ctx):
    _need_owner(action)
    payload = action.get("payload") or {}
    project, node = _load(ctx, payload)
    if node.get("status") not in AWAITING:
        raise Refuse(f"node {node['id']} is {node.get('status')}; accept needs a submitted node")
    check = node.get("check") or {}
    if check.get("type") == "human":
        # The owner's accept IS the confirmation a human check waits for.
        _update_node(ctx, node, status="done", evidence=_evidence("accepted by owner (human check)"))
        return {"status": "done", "result": {"node": node["id"], "check": "human"}}
    cwd = _cwd(ctx, project, node["id"])
    if not cwd:
        raise Fail(f"no folder known to run the check of node {node['id']} in")
    res = node_checks.run_node_check(check, cwd, runner=ctx.get("check_runner"))
    if not res.passed:
        raise Fail(f"check {res.outcome} for node {node['id']}: {_evidence(res.detail)}")
    _update_node(ctx, node, status="done", evidence=_evidence(f"accepted by owner; check passed: {res.detail}"))
    return {"status": "done", "result": {"node": node["id"], "check": "pass"}}


def h_reject(action, ctx):
    _need_owner(action)
    payload = action.get("payload") or {}
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise Fail("reject needs a reason")
    project, node = _load(ctx, payload)
    if node.get("status") not in REJECTABLE:
        raise Refuse(f"node {node['id']} is {node.get('status')}; reject needs a submitted node")
    _update_node(ctx, node, status="error", evidence=_evidence(f"rejected by owner: {reason.strip()}"))
    return {"status": "done", "result": {"node": node["id"]}}


def h_owner_status(action, ctx):
    _need_owner(action)
    payload = action.get("payload") or {}
    if "rev" not in payload:
        raise Refuse("owner_status needs the node rev it was decided at (payload.rev)")
    status = payload.get("status")
    if status not in NODE_STATUSES:
        raise Fail(f"status must be one of {', '.join(NODE_STATUSES)}")
    project, node = _load(ctx, payload)
    if status != "running":
        if _live_jobs(ctx, project, node["id"]):
            raise Refuse(f"a job is running for node {node['id']}; stop it first")
    note = payload.get("evidence")
    text = f"set to {status} by owner" + (f": {note}" if isinstance(note, str) and note.strip() else "")
    _update_node(ctx, node, status=status, evidence=_evidence(text))
    return {"status": "done", "result": {"node": node["id"], "status": status}}


HANDLERS = {"run": h_run, "retry": h_retry, "stop": h_stop, "claim": h_claim, "release": h_release,
            "accept": h_accept, "reject": h_reject, "owner_status": h_owner_status}


# ------------------------------------------------------------------ discovery
def load_handlers(package="waypoint_runner", handlers=None) -> dict:
    """Built-in handlers plus whatever every ``actions_*`` module of ``package`` registers.

    Returns a new dict (or fills ``handlers``). Problems go to ``load_handlers.errors`` (cleared on each call)."""
    errors = []
    load_handlers.errors = errors
    table = HANDLERS.copy() if handlers is None else handlers
    for k, v in HANDLERS.items():
        table.setdefault(k, v)
    try:
        pkg = importlib.import_module(package)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{package}: {type(exc).__name__}: {exc}")
        return table
    for info in sorted(pkgutil.iter_modules(getattr(pkg, "__path__", [])), key=lambda i: i.name):
        if info.ispkg or not info.name.startswith("actions_"):
            continue
        name = f"{package}.{info.name}"
        try:
            mod = importlib.import_module(name)
            reg = getattr(mod, "register", None)
            if not callable(reg):
                continue
            extra = {}
            reg(extra)
        except Exception as exc:  # noqa: BLE001 - one bad module must not stop the others
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        for typ, fn in extra.items():
            if typ in HANDLERS:
                errors.append(f"{name}: cannot replace built-in action type {typ}")
            elif callable(fn):
                table[typ] = fn
            else:
                errors.append(f"{name}: handler for {typ} is not callable")
    return table


load_handlers.errors = []


# ------------------------------------------------------------------ processing
def _handlers(ctx):
    table = ctx.get("handlers")
    if table is None:
        table = ctx["handlers"] = load_handlers()
    return table


def _run_handler(action, ctx):
    fn = _handlers(ctx).get(action.get("type"))
    if fn is None:
        return {"status": "failed", "error": f"unknown action type {action.get('type')!r}"}
    try:
        out = fn(action, ctx)
    except Refuse as exc:
        return {"status": "refused", "error": str(exc)}
    except Fail as exc:
        return {"status": "failed", "error": str(exc)}
    except RevConflict as exc:
        return {"status": "refused", "error": f"stale revision: {exc}"}
    except Exception as exc:  # noqa: BLE001 - recorded on the action, never raised to the loop
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(out, dict) or out.get("status") not in RESULT_STATUSES:
        return {"status": "failed", "error": f"handler for {action.get('type')} returned no valid status"}
    return out


def process_action(ctx, action_id):
    """Carry out one queued action and record the outcome in its file. Returns the outcome dict
    (``{"id", "status", ...}``), or None when the action was not queued or is being handled elsewhere."""
    store = ctx["store"]
    rel = f"actions/{action_id}.json"
    try:
        lock = acquire_lock(store.path(f"actions/{action_id}.processing"), owner=WRITER, wait_s=0, now=ctx["now"])
    except LockBusy:
        return None
    try:
        action = store.read(rel)
        if not isinstance(action, dict) or action.get("status") != "queued":
            return None
        if not isinstance(action.get("type"), str):
            out = {"status": "failed", "error": "action has no type"}
        else:
            out = _run_handler(action, ctx)
        body = {k: v for k, v in action.items() if k not in _VOLATILE}
        body.update(status=out["status"], finished_at=iso(ctx["now"]()))
        body.pop("error", None)
        body.pop("result", None)
        if out.get("error"):
            body["error"] = str(out["error"])
        if out.get("result") is not None:
            body["result"] = out["result"]
        store.write(rel, body, expected_rev=action.get("rev"))
        return {"id": action_id, "type": action.get("type"), "status": out["status"], "error": out.get("error")}
    finally:
        lock.release()


def process_queue(ctx):
    """Process every queued action, oldest first. Returns the outcomes of the actions that ran."""
    store = ctx["store"]
    queued = []
    for name in store.list_names("actions"):
        if not name.endswith(".json"):
            continue
        try:
            a = store.read(f"actions/{name}")
        except Exception:  # noqa: BLE001 - a torn or bad file waits for the next pass
            continue
        if isinstance(a, dict) and a.get("status") == "queued":
            queued.append((str(a.get("created_at") or ""), name[:-5]))
    out = []
    for _, aid in sorted(queued):
        res = process_action(ctx, aid)
        if res is not None:
            out.append(res)
    return out
