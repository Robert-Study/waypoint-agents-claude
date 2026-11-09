"""Owner's own Run click for Codex: ``POST /api/node/run_owner``.

This is the Inspector's Codex Run button. It starts ``codex exec`` for one task only when:
- the request comes from the dsh page (allowlisted browser ``Origin`` -> actor ``owner``) and
  carries no engine token (the Router may never start Codex, even posing as the page);
- the body says ``route: "codex"`` and ``confirm: true`` (the page shows a confirm click first);
- Codex is not switched off in the Agents tab (``toggles.is_enabled('codex')`` when present, else
  the models registry) and its usage is above the floor.
The scheduler, failure handler, MCP server and agents never import this module; at module level
``start_owner_run`` refuses every actor except ``owner``.

The task's Copy Prompt packet goes to ``codex exec --json --skip-git-repo-check -C <cwd> -`` on
stdin, with metered API keys and ``CODEX_HOME`` removed from the environment so it runs on
Owner's Codex plan. Output lands in ``<data>/owner_runs/<run_id>/``. When Codex exits 0 the
task's done-check runs (only a passing check sets done); any other exit marks the task error.

The process runner is injectable (``runner(argv, cwd, stdin_text, out_dir, env, on_exit)``), so
tests never call real Codex. On the server, ``h.app.owner_runner``, ``h.app.codex_exe``,
``h.app.owner_disabled`` and ``h.app.check_runner`` override the defaults.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from .pick import floor_for
from .store import OWNER

ROUTE = "codex"
USAGE_POOLS = ("codex_5h", "codex_week")
USAGE_FLOOR_PERCENT = 0.0  # note 146: unlisted pools have a 0% floor
TOKEN_HEADER = "X-CC-Token"
METERED_ENV = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE", "CODEX_API_KEY",
               "AZURE_OPENAI_API_KEY", "ANTHROPIC_API_KEY")
DROP_ENV = METERED_ENV + ("CODEX_HOME",)
RUNNING = ("running", "submitted")


class OwnerRunRefused(Exception):
    def __init__(self, status, error, detail, **extra):
        super().__init__(detail)
        self.status = status
        self.body = dict({"error": error, "detail": detail}, **extra)


# ---------------------------------------------------------------- defaults
def default_codex_exe():
    return shutil.which("codex")


TOGGLE_IDS = ("codex", "agent:codex")


def default_disabled() -> bool:
    """True when Owner switched Codex off with its Agents-tab switch.

    Uses ``toggles.is_enabled(id)`` when that module exists; otherwise the models registry
    (``agent:codex`` in its disabled list, or the whole ``codex`` route switched off).
    A broken toggle source counts as off: Codex is never run on a guess."""
    import importlib
    try:
        toggles = importlib.import_module("cc_engine.toggles")
    except ImportError:
        toggles = None
    fn = getattr(toggles, "is_enabled", None)
    if callable(fn):
        try:
            return fn(ROUTE) is False
        except Exception:
            return True
    try:
        from . import models
        disabled = {str(x) for x in (models.load_state().get("disabled") or [])}
        return bool(disabled & set(TOGGLE_IDS)) or bool(models.is_disabled(route=ROUTE))
    except Exception:
        return True


def child_env(base=None):
    env = dict(os.environ if base is None else base)
    for k in DROP_ENV:
        env.pop(k, None)
    return env


def default_runner(argv, cwd, stdin_text, out_dir, env, on_exit):
    """Start ``argv`` detached from the request; ``on_exit(code)`` runs in a watcher thread."""
    out_dir = Path(out_dir)
    out = open(out_dir / "out.jsonl", "wb")
    err = open(out_dir / "err.txt", "wb")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(argv, cwd=cwd or None, stdin=subprocess.PIPE, stdout=out, stderr=err,
                                env=env, creationflags=flags)
    except OSError:
        out.close()
        err.close()
        raise

    def watch():
        try:
            try:
                proc.stdin.write(stdin_text.encode("utf-8"))
                proc.stdin.close()
            except OSError:
                pass
            code = proc.wait()
        finally:
            out.close()
            err.close()
        (out_dir / "exit.json").write_text(json.dumps({"exit_code": code}), encoding="utf-8")
        on_exit(code)

    threading.Thread(target=watch, daemon=True, name="owner-run-" + out_dir.name).start()
    return {"pid": proc.pid}


# ---------------------------------------------------------------- core
def _usage_floor(rows):
    mine = [r for r in rows or [] if isinstance(r, dict) and (r.get("pool") in USAGE_POOLS
                                                               or str(r.get("pool") or "").startswith("codex_"))]
    known = [r for r in mine if isinstance(r.get("remaining_percent"), (int, float))
             and r.get("status") in ("FRESH", "STALE")]
    if not known:
        return None
    return min(known, key=lambda r: r["remaining_percent"] - floor_for(r["pool"], USAGE_FLOOR_PERCENT))


def codex_argv(exe, cwd):
    argv = [exe, "exec", "--json", "--skip-git-repo-check"]
    if cwd:
        argv += ["-C", str(cwd)]
    return argv + ["-"]


def start_owner_run(store, project_id, node_id, route, actor, confirm, runner=None, codex_exe=None,
                    usage_rows=None, disabled=None, check_runner=None, data_dir=None) -> dict:
    """Start Owner's Codex run on one task. Raises ``OwnerRunRefused`` (HTTP status + body)."""
    if actor != OWNER:
        raise OwnerRunRefused(403, "owner_only", "only Owner's own click may run Codex")
    if route != ROUTE:
        raise OwnerRunRefused(400, "bad_route", "this route runs Codex only; other routes use Send in the Agents route")
    if confirm is not True:
        raise OwnerRunRefused(409, "confirm", "Codex draws on your Codex plan; confirm to run it")
    project = store.get_project(project_id)
    node = store.get_node(project_id, node_id) if project else None
    if node is None:
        raise OwnerRunRefused(404, "not_found", f"no task {node_id}")
    if node["status"] in RUNNING:
        raise OwnerRunRefused(409, "busy", f"task {node_id} is already running")
    if node["status"] == "done":
        raise OwnerRunRefused(409, "done", f"task {node_id} is done; reopen it first")
    if (disabled or default_disabled)():
        raise OwnerRunRefused(403, "disabled", "Codex is switched off on the Agents tab")
    low = _usage_floor(usage_rows)
    if low is not None:
        floor = floor_for(low["pool"], USAGE_FLOOR_PERCENT)
        if low["remaining_percent"] <= max(floor, 0.0):
            raise OwnerRunRefused(409, "usage_floor", f"{low.get('label') or low['pool']} has "
                                  f"{low['remaining_percent']:g}% left; Codex runs stop at {floor:g}%",
                                  usage_percent=low["remaining_percent"])
    exe = codex_exe if codex_exe is not None else default_codex_exe()
    if not exe:
        raise OwnerRunRefused(503, "unavailable", "the codex CLI was not found")

    from .api import build_prompt_packet
    prompt = build_prompt_packet(store, project_id, node_id)
    cwd = project.get("cwd") or None
    run_id = "or-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(3)
    out_dir = Path(data_dir or Path(store.path).parent) / "owner_runs" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    argv = codex_argv(exe, cwd)

    store.set_node_status(project_id, node_id, "running", OWNER,
                          summary=f"Owner started Codex on task {node_id}.",
                          data={"route": ROUTE, "run_id": run_id})

    def on_exit(code):
        _finish(store, project_id, node_id, code, check_runner, cwd)

    try:
        launch = (runner or default_runner)(argv, cwd, prompt, str(out_dir), child_env(), on_exit) or {}
    except OSError as e:
        store.set_node_status(project_id, node_id, "error", "codex",
                              summary=f"Codex could not start on task {node_id}: {e}.")
        raise OwnerRunRefused(503, "unavailable", f"codex could not start: {e}")
    return {"ok": True, "run_id": run_id, "route": ROUTE, "status": "running", "pid": launch.get("pid"),
            "output": str(out_dir)}


def _finish(store, project_id, node_id, code, check_runner=None, cwd=None):
    """Codex exited: a 0 exit runs the done-check; anything else marks the task error."""
    node = store.get_node(project_id, node_id)
    if node is None or node["status"] not in RUNNING:
        return  # Owner reopened or finished it meanwhile
    if code != 0:
        store.set_node_status(project_id, node_id, "error", "codex",
                              summary=f"Codex stopped on task {node_id} with exit code {code}.")
        return
    from . import checks
    res = checks.complete_node(store, project_id, node_id, runner=check_runner, cwd=cwd)
    node = store.get_node(project_id, node_id)
    if not res.ok and node is not None and node["status"] in RUNNING:  # e.g. waiting on a dependency
        store.set_node_status(project_id, node_id, "error", "codex", res.detail,
                              summary=f"Codex finished task {node_id} but its check could not pass yet.")


# ---------------------------------------------------------------- HTTP
def _http_error(status, body):
    from .api import HttpError  # lazy: api imports extension modules
    return HttpError(status, body)


def p_run_owner(h, q):
    """{project, id, route: "codex", confirm: true} -> {ok, run_id, route, status}."""
    if getattr(h, "actor", None) != OWNER or h.headers.get(TOKEN_HEADER):
        raise _http_error(403, {"error": "owner_only", "detail": "only Owner's own click on the page may run Codex"})
    b = h._json_body()
    pid = b.get("project")
    nid = b.get("id") or b.get("node")
    if not pid or not nid:
        raise _http_error(400, {"error": "bad_request", "detail": "missing project or id"})
    app = h.app
    try:
        rows = app.usage_reader() or []
    except Exception:
        rows = []
    try:
        return start_owner_run(app.store, pid, nid, b.get("route"), actor=h.actor, confirm=b.get("confirm"),
                               runner=getattr(app, "owner_runner", None),
                               codex_exe=getattr(app, "codex_exe", None),
                               usage_rows=rows, disabled=getattr(app, "owner_disabled", None),
                               check_runner=getattr(app, "check_runner", None))
    except OwnerRunRefused as e:
        raise _http_error(e.status, e.body)


def set_archived(store, project_id, archived, actor=OWNER):
    """Archive (or bring back) a project. Archiving cancels its active runs and closes its open
    questions, so nothing keeps running or asking for a project Owner set aside."""
    from . import scheduler
    flag = 1 if archived else 0

    def fn(c):
        c.execute("UPDATE projects SET archived=? WHERE id=?", (flag, project_id))

    store.mutate(None, fn, actor, f"Owner {'archived' if archived else 'brought back'} the project.",
                 project_id=project_id, kind="project", data={"archived": bool(archived)})
    out = {"ok": True, "project": project_id, "archived": bool(archived), "cancelled_runs": [],
           "closed_questions": []}
    if archived:
        for run in store.list_runs(limit=10000):
            if run["project_id"] == project_id and run["status"] in scheduler.ACTIVE_RUN_STATUSES:
                scheduler.cancel_run(store, run["id"], actor=actor)
                store.notify("run", {"id": run["id"], "project": project_id, "status": "cancelled"})
                out["cancelled_runs"].append(run["id"])
        out["closed_questions"] = store.close_questions(project_id, actor=actor, why="the project is archived")
    return out


def _p_archive(archived):
    def handler(h, q):
        """{project} -> {ok, project, archived, cancelled_runs, closed_questions}; Owner only."""
        if getattr(h, "actor", None) != OWNER:  # api.py's owner rule (``_owner_only``)
            raise _http_error(403, {"error": "owner_only",
                                    "detail": f"only Owner may {'archive' if archived else 'unarchive'} a project"})
        b = h._json_body()
        pid = b.get("project")
        if not pid:
            raise _http_error(400, {"error": "bad_request", "detail": "missing project"})
        if h.app.store.get_project(pid) is None:
            raise _http_error(404, {"error": "not_found", "detail": "no such project"})
        return set_archived(h.app.store, pid, archived, actor=h.actor)
    return handler


p_project_archive = _p_archive(True)
p_project_unarchive = _p_archive(False)

ROUTES = {"POST /api/node/run_owner": p_run_owner,
          "POST /api/project/archive": p_project_archive,
          "POST /api/project/unarchive": p_project_unarchive}
