"""The runner tick (Waypoint 1.2, node run-tick): one pass that advances every active run.

``Tick(root, ...)`` is a callable. ``Tick(...)(source=None)`` runs one pass and returns a summary dict, so it plugs
into ``lifecycle.Runner(tick=...)`` (which passes its work source, unused here: state is read from the data folder).

One pass, in order
  1. take the tick lock (``tick.processing``); a second tick while one holds it returns ``{"status": "busy"}`` and
     touches nothing
  2. actions: ``actions.process_queue`` (handlers loaded once with ``actions.load_handlers()``, never by name),
     then answered questions are routed once (``resume.process``: a warm session gets the answer, a cold one is
     resumed from Waypoint by a fresh worker)
  3. runs: a queued run becomes running
  4. jobs: poll every running job; settle every ended job once (receipt ingest, callback, notification, node
     status from the receipt, the node check or the recovery ledger), recorded in ``jobs/<id>/tick.json``
  5. schedules: claim each due slot once and queue a run for it
  6. dispatch: ready nodes of every running run, once per node per tick. A held dispatch is reported in
     ``held`` and is not tried again in the same tick.
  7. runs whose project has nothing left to do are finished

Small on purpose: adapter, policy, recovery, callback and notify logic live in their own modules. No floor or
percentage lives here; floors are Waypoint settings applied by the dispatcher. Data access only through
core/waypoint-data/py.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import time
from pathlib import Path

_PY = Path(__file__).resolve().parents[2] / "core" / "waypoint-data" / "py"
if _PY.is_dir() and str(_PY) not in sys.path:
    sys.path.insert(0, str(_PY))

from waypoint_data.store import LockBusy, RevConflict, acquire_lock, iso  # noqa: E402

from . import actions, callbacks, node_checks, receipts_ingest, recovery, resume, schedule  # noqa: E402
from . import notify as notify_mod  # noqa: E402
from .dispatch_core import AdapterRegistry, Dispatcher  # noqa: E402
from .ready import ready_nodes  # noqa: E402

__all__ = ["Tick", "default_registry", "tick"]

WRITER = "runner"
ACTIVE = ("running", "queued", "starting", "active")
END_STATES = ("finished", "failed", "recovery", "stopped")


def default_registry(root, now=time.time) -> AdapterRegistry:
    """Every shipped adapter, each behind its own toggle and floor (the dispatcher applies both)."""
    from .adapters import codex_cli, free_pools, headless_claude, session_subagent, side_session
    reg = AdapterRegistry()
    reg.register(headless_claude.HeadlessClaudeAdapter(root=root, now=now))
    reg.register(codex_cli.CodexCliAdapter(root=root, now=now))
    reg.register(session_subagent.SessionSubagentAdapter(now=now))
    reg.register(side_session.SideSessionAdapter(now=now))
    free_pools.register_free_pools(reg, free_pools.FreePools(root, now=now))
    return reg


def _subdirs(store, rel):
    try:
        names = os.listdir(store.path(rel))
    except (FileNotFoundError, NotADirectoryError):
        return []
    return sorted(n for n in names if os.path.isdir(os.path.join(store.path(rel), n)) and not n.startswith("."))


class Tick:
    def __init__(self, root, registry=None, dispatcher=None, now=time.time, check_runner=None, cwd_for=None,
                 machines=None, notifier=None, handlers=None, settle=None, desktop=resume.desktop_activity):
        self.root = Path(root)
        self.now = now
        self.desktop = desktop  # last activity from the desktop app's session files; tests pass a fake
        self.notifier = notifier
        self.dispatcher = dispatcher or Dispatcher(
            self.root, registry if registry is not None else default_registry(self.root, now), now=now)
        self.ctx = actions.make_ctx(self.root, dispatcher=self.dispatcher, now=now, check_runner=check_runner,
                                    cwd_for=cwd_for, machines=machines, handlers=handlers)
        self.store = self.ctx["store"]
        self.ledger = recovery.Ledger(self.root, now=now, settle=settle)
        self._handlers_loaded = handlers is not None

    # -- entry
    def __call__(self, source=None) -> dict:
        return self.run()

    def run(self) -> dict:
        try:
            lock = acquire_lock(self.store.path("tick.processing"), owner=WRITER, wait_s=0, now=self.now)
        except LockBusy:
            return {"status": "busy"}
        try:
            return self._pass()
        finally:
            lock.release()

    # -- the pass
    def _pass(self) -> dict:
        if not self._handlers_loaded:  # once per Tick, never per pass
            self.ctx["handlers"] = actions.load_handlers()
            self._handlers_loaded = True
        s = {"status": "ok", "actions": [], "ingested": 0, "polled": 0, "settled": [], "scheduled": [],
             "started": [], "held": [], "finished_runs": [], "resumed": []}
        s["actions"] = [a["id"] for a in actions.process_queue(self.ctx)]
        s["resumed"] = resume.process(self.store, now=self.now, desktop=self.desktop)
        self._runs_start()
        jobs = self._jobs()
        s["polled"] = sum(1 for j in jobs if j.get("state") == "running")
        jobs = [(self.dispatcher.poll(j["id"]) or j) if j.get("state") == "running" else j for j in jobs]
        pending = [j for j in jobs if j.get("state") in END_STATES and not self.store.exists(self._marker(j))]
        s["ingested"] = len(self._ingest(jobs, pending))
        for j in pending:
            self._settle(j)
            s["settled"].append(j["id"])
        s["scheduled"] = self._schedules()
        runs = self._runs_start()
        self._dispatch(runs, s)
        self._runs_finish(runs, s)
        return s

    # -- runs
    def _runs_start(self) -> list:
        out = []
        for r in self.store.list_entities("runs"):
            if not isinstance(r, dict) or r.get("status") not in ACTIVE or not r.get("project"):
                continue
            if r.get("status") == "queued":
                try:
                    r = self.store.write(f"runs/{r['id']}.json",
                                         dict(r, status="running", started_at=iso(self.now())),
                                         expected_rev=r.get("rev"))
                except RevConflict:
                    continue
            out.append(r)
        return out

    def _runs_finish(self, runs, s):
        for r in runs:
            nodes = self._nodes(r["project"])
            busy = any(n.get("status") in ("ready", "running") for n in nodes) or any(
                self.ledger.state(r["project"], n["id"]) == "retry" for n in nodes)
            if busy:
                continue
            cur = self.store.read(f"runs/{r['id']}.json") or r
            try:
                self.store.write(f"runs/{r['id']}.json", dict(cur, status="finished", ended_at=iso(self.now())),
                                 expected_rev=cur.get("rev"))
                s["finished_runs"].append(r["id"])
            except RevConflict:
                pass

    def _nodes(self, project):
        return actions._project_nodes(self.ctx, project)

    # -- jobs
    @staticmethod
    def _marker(job):
        return f"jobs/{job['id']}/tick.json"

    def _jobs(self) -> list:
        out = []
        for name in _subdirs(self.store, "jobs"):
            j = self.store.read(f"jobs/{name}/state.json")
            if isinstance(j, dict) and j.get("id"):
                out.append(j)
        return out

    def _cwd(self, job):
        spec = self.store.read(f"jobs/{job['id']}/spec.json")
        return spec.get("cwd") if isinstance(spec, dict) else None

    def _ingest(self, jobs, pending):
        ids = {j["id"] for j in pending}
        roots = sorted({c for c in (self._cwd(j) for j in jobs if j.get("state") == "running" or j["id"] in ids)
                        if c and Path(c).is_dir()})
        return receipts_ingest.scan_inbox(self.store, roots) if roots else []

    def _receipt(self, job):
        prefix = f"{job['project']}.{job['node']}."
        names = [n for n in self.store.list_names("receipts") if n.startswith(prefix) and n.endswith(".json")]
        names.sort(key=lambda n: os.path.getmtime(self.store.path(f"receipts/{n}")))
        return (self.store.read(f"receipts/{names[-1]}"), names[-1]) if names else (None, None)

    def _settle(self, job):
        project, node_id = job["project"], job["node"]
        receipt, rname = self._receipt(job) if job["state"] == "finished" else (None, None)
        rstatus = receipt.get("status") if isinstance(receipt, dict) else None
        callbacks.write_callback(self.store, job, receipt, rname)
        ev = notify_mod.event_for_job(job)
        if ev or rstatus == "needs_you":
            kind = "asking" if rstatus == "needs_you" else ev[0]
            body = (receipt or {}).get("summary") or job.get("reason") or job["state"]
            notify_mod.notify(self.store, notify_mod.event_id(kind, job["id"]), kind, f"{project}/{node_id}: {kind}",
                              str(body), notifier=self.notifier)
        node = self.store.read(f"projects/{project}/nodes/{node_id}.json")
        if isinstance(node, dict) and node.get("status") == "running":
            self._node_after(job, node, rstatus)
        self.store.write(self._marker(job), {"job": job["id"], "state": job["state"], "settled_at": iso(self.now())},
                         expected_rev=None)

    def _set(self, node, status, text):
        body = {k: v for k, v in node.items() if k not in actions._VOLATILE}
        body.update(status=status, evidence=actions._evidence(text))
        try:
            self.store.write(f"projects/{node['project']}/nodes/{node['id']}.json", body, expected_rev=node["rev"])
        except RevConflict:
            pass  # changed under us (owner or an action): the other change wins

    def _node_after(self, job, node, rstatus):
        state = job["state"]
        if state == "stopped":
            return self._set(node, "ready", f"stopped: {job.get('reason') or 'stopped'}")
        if state == "finished":
            if rstatus == "needs_you":
                return self._set(node, "needs_you", "the worker asked for you")
            if rstatus == "error":
                return self._set(node, "error", "the worker reported an error")
            check = node.get("check") or {}
            if check.get("type") == "human":
                return self._set(node, "submitted", "waiting for owner confirmation (human check)")
            res = node_checks.run_node_check(check, self._cwd(job) or ".", runner=self.ctx.get("check_runner"))
            if res.passed:
                return self._set(node, "done", f"check passed: {res.detail}")
            receipts_ingest.report_check_failure(self.store, job["project"], node["id"], actions._evidence(res.detail))
            return self._set(node, "error", f"check {res.outcome}: {res.detail}")
        d = self.ledger.handle(job)  # failed or recovery
        if d is None:
            return None
        if d.state == "retry":
            return self._set(node, "ready", f"retry {d.attempts} of {d.max_retries} after {d.reason}")
        if d.state == "parked":
            return self._set(node, "ready", f"parked: {d.reason}")
        if d.state == "needs_you":
            return self._set(node, "needs_you", f"needs you: {d.reason}")
        return self._set(node, "error", f"{d.state}: {d.reason}")

    # -- schedules
    def _schedules(self) -> list:
        now = dt.datetime.fromtimestamp(self.now())
        scheds = [x for x in self.store.list_entities("schedules") if isinstance(x, dict) and x.get("id")]
        out = []
        for due in schedule.due_slots(now, scheds, schedule.claimed_keys(self.store)):
            if not schedule.claim_slot(self.store, due):
                continue
            sc = due.schedule
            rid = f"run-{due.slot.replace('-', '').replace(':', '').replace('T', '')}-{sc['id']}"[:64]
            try:
                self.store.write(f"runs/{rid}.json", {
                    "schema": "waypoint.run.v1", "id": rid, "project": sc["project"], "status": "queued",
                    "settings": {"mode": sc.get("mode") or "balanced", "schedule": sc["id"], "slot": due.slot},
                    "created_at": iso(self.now()), "started_at": None, "ended_at": None, "sprints": []},
                    expected_rev=0)
            except RevConflict:
                continue
            body = {k: v for k, v in sc.items() if k not in actions._VOLATILE}
            body["last_run"] = {"slot": due.slot, "started_at": iso(self.now()), "ended_at": None, "result": None}
            try:
                self.store.write(f"schedules/{sc['id']}.json", body, expected_rev=sc.get("rev"))
            except RevConflict:
                pass
            out.append(due.key)
        return out

    # -- dispatch
    def _dispatch(self, runs, s):
        tried = set()
        for r in runs:
            project = r["project"]
            nodes = self._nodes(project)
            waiting = {n["id"] for n in nodes if self.ledger.state(project, n["id"]) == "retry"
                       and not self.ledger.ready(project, n["id"])}
            claimed = {n["id"] for n in nodes if self.ctx["claims"].read(project, n["id"])}
            mode = (r.get("settings") or {}).get("mode") or "balanced"
            for n in ready_nodes(nodes, claimed=claimed, parked=waiting):
                key = (project, n["id"])
                if key in tried:
                    continue
                tried.add(key)
                try:
                    out = actions._start(self.ctx, n, mode, WRITER)
                except (actions.Refuse, actions.Fail) as exc:
                    s["held"].append({"project": project, "node": n["id"], "reason": str(exc)})
                    continue
                if out["status"] == "done":
                    s["started"].append({"project": project, "node": n["id"], **out["result"]})
                    continue
                res = out.get("result") or {}
                s["held"].append({"project": project, "node": n["id"], "reason": res.get("held") or out.get("error")})
                for text in res.get("todos") or []:
                    receipts_ingest.add_decision_todo(self.store, f"held-{project}-{n['id']}-{res.get('held')}",
                                                      text, project, n["id"])


def tick(root, **kw) -> dict:
    """One pass with a fresh ``Tick`` (tests and ``--once``); a long-lived runner keeps one ``Tick``."""
    return Tick(root, **kw)()
