"""Power off when a run is finished (10-minute cancel window).

Only ``shutdown /s /t 600 /c "Control Centre run finished"`` and ``shutdown /a``
are ever issued, and only through the injected ``runner``. The PC is shut down
only when the finished run asked for it, every node of its project is done, no
run in ANY project is still active, no node anywhere is running and no job
wrapper is alive. ``cancel()`` runs ``shutdown /a``.
"""
from __future__ import annotations

import json
import subprocess

from . import scheduler as S
from .store import CLOSED_STATUSES

SHUTDOWN_ARGV = ["shutdown", "/s", "/t", "600", "/c", "Control Centre run finished"]
CANCEL_ARGV = ["shutdown", "/a"]
CANCEL_WINDOW_SECONDS = 600


def default_runner(argv):
    return subprocess.run(argv, check=False).returncode


def _marker(run_id):
    return f"power_off:{run_id}"


def _all_projects(store):
    try:
        return [p["id"] for p in store.list_projects(include_archived=True)]
    except TypeError:
        return [p["id"] for p in store.list_projects()]


def other_work(store, run, dispatcher=None):
    """Plain-English reason the PC must stay on, or None when nothing else is working."""
    others = [r for r in S.active_runs(store) if r["id"] != run["id"]]
    if others:
        return "another run is still active"
    for pid in _all_projects(store):
        if any(n["status"] == "running" for n in store.list_nodes(pid)):
            return "a task is still running"
    if dispatcher is not None and hasattr(dispatcher, "live_jobs"):
        if dispatcher.live_jobs():
            return "a job is still running"
    return None


def power_off_if_done(store, run, runner=None, settings=None, dispatcher=None):
    """Schedule shutdown if the run's setting is on, its project is done and nothing else works.

    Returns True when the shutdown command was issued. Issues it at most once per run.
    """
    runner = runner or default_runner
    if settings is None:
        settings = S.expand_settings(S.run_settings(run))
    if not settings.get("power_off"):
        return False
    pid = run["project_id"]
    nodes = S.list_nodes(store, pid)
    if not nodes or any(n["status"] not in CLOSED_STATUSES for n in nodes):
        return False
    if S.meta_get(store, _marker(run["id"])):
        return False
    busy = other_work(store, run, dispatcher)
    if busy:
        key = f"power_wait:{run['id']}"
        if S.meta_get(store, key) != busy:
            S.meta_set(store, key, busy)
            S.emit(store, pid, None, "power",
                   f"Every task is done, but {busy}, so the PC stays on for now.",
                   data={"run": run["id"], "reason": busy})
        return False
    code = runner(list(SHUTDOWN_ARGV))
    S.meta_set(store, _marker(run["id"]), json.dumps({"code": code}))
    if code == 0:
        S.emit(store, pid, None, "power",
               "Every task is done, so the PC will shut down in 10 minutes unless you cancel.",
               data={"run": run["id"], "argv": SHUTDOWN_ARGV})
        return True
    S.emit(store, pid, None, "power",
           f"Every task is done, but the shutdown command failed (exit {code}), so the PC stays on.",
           data={"run": run["id"], "code": code})
    return False


def cancel(runner=None, store=None, project_id=None):
    """Abort a planned shutdown (``shutdown /a``)."""
    runner = runner or default_runner
    code = runner(list(CANCEL_ARGV))
    if store is not None:
        S.emit(store, project_id, None, "power",
               "The planned shutdown was cancelled." if code == 0
               else f"Cancelling the shutdown failed (exit {code}).",
               actor="owner", data={"code": code})
    return code == 0
