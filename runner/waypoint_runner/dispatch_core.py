"""Dispatch core: pick an enabled adapter within its floor, write a job spec, record job state.

``Dispatcher.dispatch`` is the only way a job starts. It always asks ``policy.allowed`` (toggle, floor, fresh
reading) for every candidate adapter and starts nothing when none is allowed (status ``held`` plus the reason
and any Decisions to-do text). No floor or percentage lives here: floors are settings Owner sets in Waypoint.

Run modes (from the run action) only order and filter candidates, they never bypass a toggle or a floor:
  frugal    free adapters only (free pools first; a metered adapter is never started)
  balanced  free adapters first, then metered
  push/max  metered adapters first, then free
Within a group adapters sort by ``priority`` then name.
REVERSIBLE_AGENT_DEFAULT (by=agent; undo=edit ``_order`` below; affects=run-actions): the plan text says only
that frugal "tries free pools first" and that push and max "may use metered routes", so frugal here never
falls back to a metered route.

Data folder layout (all Python writes go through core/waypoint-data/py):
  jobs/<job_id>/spec.json        immutable job spec; embeds the brief, run mode, parent and fork ids, claim token
  jobs/<job_id>/state.json       job state with rev: running | finished | failed | recovery | stopped
  jobs/<job_id>/heartbeat.json   {"at": iso}, rewritten by ``Dispatcher.heartbeat`` (and by the worker wrapper)
  sessions/<job_id>.json         waypoint.session.v1: started (parent_id, fork_id), ended; callback to the parent
  claims/<node>.json             shared claim protocol, owner ``runner``

The claim is released when a job reaches any end state (the engine releases it again on receipt); a
``recovery`` job leaves the node claimable so recovery can dispatch it again.
"""
from __future__ import annotations

import os
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_DATA_PY = _REPO / "core" / "waypoint-data" / "py"
if _DATA_PY.is_dir() and str(_DATA_PY) not in sys.path:
    sys.path.append(str(_DATA_PY))

from waypoint_data import io as wio  # noqa: E402
from waypoint_data.claims import Claims  # noqa: E402
from waypoint_data.store import RevConflict, Store, iso, parse_iso, serialize  # noqa: E402

from . import brief as brief_mod  # noqa: E402
from . import policy  # noqa: E402
from . import resume  # noqa: E402

__all__ = ["MODES", "Adapter", "AdapterRegistry", "DispatchResult", "Dispatcher", "pid_alive", "kill_tree",
           "write_heartbeat"]

WRITER = "runner"
MODES = ("frugal", "balanced", "push", "max")
END_STATES = ("finished", "failed", "recovery", "stopped")
# Claim-protocol window for runner owners (docs/data/CONCURRENCY.md); a time window, not a floor.
DEFAULT_STALE_S = 5 * 60
_SESSION_STATUS = {"finished": "done", "failed": "error", "recovery": "error", "stopped": "idle"}


# ---------------------------------------------------------------- processes

def pid_alive(pid, started_at=None) -> bool:
    """True while process ``pid`` runs. Reuses the engine's Windows-safe check (stdlib fallback)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        engine = str(_REPO / "engine")
        if engine not in sys.path:
            sys.path.append(engine)
        from cc_engine import dispatch as eng  # noqa: PLC0415
        return bool(eng.pid_alive(pid, started_at))
    except ImportError:
        pass
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
        return str(pid) in out.stdout
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def kill_tree(pid) -> int:
    """Kill ``pid`` and all its children (taskkill /T /F on Windows, the process group elsewhere)."""
    pid = int(pid)
    if os.name == "nt":
        return subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], check=False, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode
    import signal
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
        return 0
    except OSError:
        return 1


def write_heartbeat(job_dir, now=time.time) -> None:
    wio.atomic_write(str(Path(job_dir) / "heartbeat.json"), serialize({"at": iso(now())}), writer=WRITER)


# ---------------------------------------------------------------- adapters

class Adapter:
    """One worker type. Subclass and set ``name`` (registry key), ``route`` (agent id or route name that
    ``policy`` decides on) and optionally ``priority`` (lower first inside a mode group).

    ``start(spec, job_dir)`` launches the work and returns ``{"pid": int|None, ...}``.
    ``poll(job, job_dir)`` returns ``{"state": "running"|"finished"|"failed", "exit_code"?, "reason"?}``.
    ``stop(job, job_dir)`` ends the work; the default kills the whole process tree of ``job["pid"]``.
    """

    name: str = ""
    route: str = ""
    priority: int = 0

    def start(self, spec, job_dir):  # pragma: no cover - interface
        raise NotImplementedError

    def poll(self, job, job_dir):  # pragma: no cover - interface
        raise NotImplementedError

    def stop(self, job, job_dir):
        pid = job.get("pid")
        if pid:
            kill_tree(pid)


class AdapterRegistry:
    def __init__(self):
        self._adapters: dict[str, Adapter] = {}

    def register(self, adapter) -> Adapter:
        if not getattr(adapter, "name", "") or not getattr(adapter, "route", ""):
            raise ValueError("an adapter needs a name and a route")
        if adapter.name in self._adapters:
            raise ValueError(f"adapter {adapter.name!r} is already registered")
        self._adapters[adapter.name] = adapter
        return adapter

    def get(self, name) -> Adapter:
        return self._adapters[name]

    def names(self) -> list:
        return sorted(self._adapters)

    def adapters(self) -> list:
        return [self._adapters[n] for n in self.names()]


# ---------------------------------------------------------------- dispatcher

@dataclass
class DispatchResult:
    status: str  # started | held
    reason: str = "ok"
    job_id: str | None = None
    adapter: str | None = None
    spec_path: str | None = None
    todos: list = field(default_factory=list)
    considered: list = field(default_factory=list)  # [(adapter name, reason)]


def _order(adapters, metered_of, mode):
    free = sorted((a for a in adapters if not metered_of(a)), key=lambda a: (a.priority, a.name))
    metered = sorted((a for a in adapters if metered_of(a)), key=lambda a: (a.priority, a.name))
    if mode == "frugal":
        return free
    if mode == "balanced":
        return free + metered
    return metered + free  # push, max


def _read(path):
    return wio.read_json(str(path))


class Dispatcher:
    """``root`` is the Waypoint data folder. ``now`` returns epoch seconds. ``pid_alive`` and ``kill_tree`` are
    injectable so tests never touch real processes."""

    def __init__(self, root, registry, now=time.time, stale_s=DEFAULT_STALE_S, pid_alive=pid_alive,
                 kill_tree=kill_tree, id_factory=None):
        self.root = Path(root)
        self.registry = registry
        self.now = now
        self.stale_s = stale_s
        self._pid_alive = pid_alive
        self._kill_tree = kill_tree
        self._id = id_factory or (lambda node: f"{str(node)[:60]}-{time.strftime('%Y%m%d%H%M%S', time.gmtime(self.now()))}"
                                  f"-{secrets.token_hex(3)}")
        self.store = Store(self.root, writer=WRITER, now=now)
        self.claims = Claims(self.root, stale_s=stale_s, now=now, writer=WRITER)

    # -- paths
    def job_dir(self, job_id) -> Path:
        return self.root / "jobs" / job_id

    def _utc(self):
        return datetime.fromtimestamp(self.now(), timezone.utc)

    # -- dispatch
    def dispatch(self, node, project, mode="balanced", graph=None, todos=(), parent_id=None, fork_id=None,
                 cwd=None, requested_by=WRITER) -> DispatchResult:
        if mode not in MODES:
            return DispatchResult("held", "bad_mode")
        node = dict(node)
        node.setdefault("project", project)
        node["files"] = node.get("files") or []
        node["deps"] = node.get("deps") or []
        adapters = self.registry.adapters()
        if not adapters:
            return DispatchResult("held", "no_adapter")

        def metered_of(a):
            agent = policy.find_route(self.root, a.route)
            return True if agent is None else not policy.is_free_pool(agent.get("pool"))

        candidates = _order(adapters, metered_of, mode)
        if not candidates:
            return DispatchResult("held", "mode_frugal_metered",
                                  considered=[(a.name, "metered") for a in adapters])

        allowed, considered, todo_text = [], [], []
        for a in candidates:  # the floor check: every candidate, every dispatch
            decision = policy.allowed(self.root, a.route, self._utc())
            considered.append((a.name, decision.reason))
            if decision.allowed:
                allowed.append(a)
            elif decision.todo and decision.todo not in todo_text:
                todo_text.append(decision.todo)
        if not allowed:
            return DispatchResult("held", considered[0][1], todos=todo_text, considered=considered)

        token = secrets.token_hex(8)
        got = self.claims.claim(project, node["id"], "runner", cwd or "pc", token, claimed_by=requested_by)
        if not got.ok:
            return DispatchResult("held", "claim_held", considered=considered)

        job_id = self._id(node["id"])
        jdir = self.job_dir(job_id)
        jdir.mkdir(parents=True, exist_ok=True)
        errors = []
        brief = resume.resume_header(self.store, project, node["id"]) + brief_mod.build_worker_brief(node, graph, todos)
        for a in allowed:
            spec = {"schema": "waypoint.job_spec.v1", "job_id": job_id, "project": project, "node": node["id"],
                    "adapter": a.name, "route": a.route, "mode": mode,
                    "brief": brief,
                    "cwd": cwd, "parent_id": parent_id, "fork_id": fork_id, "session_id": job_id,
                    "claim_token": token, "requested_by": requested_by, "created_at": iso(self.now())}
            spec_path = jdir / "spec.json"
            wio.atomic_write(str(spec_path), serialize(spec), writer=WRITER)
            try:
                started = a.start(spec, jdir) or {}
            except Exception as exc:  # noqa: BLE001 - recorded, next adapter tried
                errors.append(f"{a.name}: {type(exc).__name__}: {exc}")
                continue
            write_heartbeat(jdir, self.now)
            self.store.write(f"jobs/{job_id}/state.json", {
                "schema": "waypoint.job.v1", "id": job_id, "project": project, "node": node["id"],
                "adapter": a.name, "route": a.route, "mode": mode, "state": "running",
                "pid": started.get("pid"), "session_id": job_id, "parent_id": parent_id, "fork_id": fork_id,
                "claim_token": token, "started_at": iso(self.now()), "ended_at": None, "reason": None,
                "exit_code": None}, expected_rev=0)
            self._open_session(spec, node)
            return DispatchResult("started", "ok", job_id, a.name, str(spec_path), considered=considered)

        self.claims.release(project, node["id"], token)
        return DispatchResult("held", "start_failed", considered=considered + [(e, "start_failed") for e in errors])

    # -- sessions
    def _session_path(self, sid) -> Path:
        return self.root / "sessions" / f"{sid}.json"

    def _append_event(self, sid, event, status=None) -> bool:
        path = self._session_path(sid)
        sess = _read(path)
        if not isinstance(sess, dict):
            return False
        events = sess.setdefault("events", [])
        events.append(dict({"seq": len(events), "at": iso(self.now())}, **event))
        sess["updated_at"] = iso(self.now())
        if status:
            sess["status"] = status
        wio.atomic_write(str(path), serialize(sess), writer=WRITER)
        return True

    def _open_session(self, spec, node):
        sid = spec["session_id"]
        t = iso(self.now())
        sess = {"schema": "waypoint.session.v1", "id": sid, "agent": spec["adapter"], "parent": spec["parent_id"],
                "fork_of": spec["fork_id"], "orchestrator": None, "project": spec["project"], "node": spec["node"],
                "cwd": spec["cwd"], "title": f"{node['id']}: {node.get('title') or node['id']}", "status": "running",
                "started_at": t, "updated_at": t,
                "events": [{"seq": 0, "at": t, "kind": "started", "actor": WRITER,
                            "summary": f"{spec['adapter']} started ({spec['mode']})",
                            "parent_id": spec["parent_id"], "fork_id": spec["fork_id"],
                            "data": {"job_id": sid, "mode": spec["mode"]}}]}
        path = self._session_path(sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        wio.atomic_write(str(path), serialize(sess), writer=WRITER)
        if spec["parent_id"] and spec["fork_id"]:
            self._append_event(spec["parent_id"], {"kind": "fork", "actor": WRITER,
                                                   "summary": f"forked {sid}", "parent_id": spec["parent_id"],
                                                   "fork_id": spec["fork_id"], "data": {"job_id": sid}})

    # -- state
    def state(self, job_id):
        return self.store.read(f"jobs/{job_id}/state.json")

    def _update(self, job, **changes):
        new = dict(job, **changes)
        return self.store.write(f"jobs/{job['id']}/state.json", new, expected_rev=job.get("rev"))

    def _end(self, job, state, reason=None, exit_code=None):
        done = self._update(job, state=state, reason=reason, exit_code=exit_code, ended_at=iso(self.now()))
        self.claims.release(job["project"], job["node"], job.get("claim_token"))
        sid = job.get("session_id")
        self._append_event(sid, {"kind": "ended", "actor": WRITER, "summary": f"{state}" + (f": {reason}" if reason else "")},
                           status=_SESSION_STATUS[state])
        if job.get("parent_id"):
            self._append_event(job["parent_id"], {
                "kind": "callback", "actor": sid, "summary": f"{sid} {state}",
                "callback": {"from_session": sid, "to_session": job["parent_id"],
                             "outcome": "done" if state == "finished" else "error",
                             "summary": reason, "receipt": None}})
        return done

    # -- heartbeat, poll, stop
    def heartbeat(self, job_id) -> bool:
        """Beat for a running job: refresh heartbeat.json and the claim. False once the job has ended."""
        job = self.state(job_id)
        if not job or job["state"] != "running":
            return False
        write_heartbeat(self.job_dir(job_id), self.now)
        return bool(self.claims.heartbeat(job["project"], job["node"], job.get("claim_token")))

    def _heartbeat_age(self, job):
        beat = _read(self.job_dir(job["id"]) / "heartbeat.json")
        at = parse_iso(beat.get("at")) if isinstance(beat, dict) else None
        if at is None:
            at = parse_iso(job.get("started_at"))
        return float("inf") if at is None else self.now() - at

    def poll(self, job_id):
        """Advance one job: adapter result first, then liveness. A dead heartbeat or vanished process
        moves a running job to ``recovery``. Ended jobs are returned unchanged."""
        job = self.state(job_id)
        if job is None:
            return None
        if job["state"] != "running":
            return job
        adapter = self.registry.get(job["adapter"])
        res = adapter.poll(job, self.job_dir(job_id)) or {}
        if res.get("state") == "finished":
            return self._end(job, "finished", None, res.get("exit_code"))
        if res.get("state") == "failed":
            return self._end(job, "failed", res.get("reason") or "failed", res.get("exit_code"))
        pid = job.get("pid")
        if pid and not self._pid_alive(pid):
            return self._end(job, "recovery", "process_gone")
        if self._heartbeat_age(job) > self.stale_s:
            if pid:
                self._kill_tree(pid)  # a hung leftover must not run beside the recovery attempt
            return self._end(job, "recovery", "heartbeat_dead")
        return job

    def stop(self, job_id, reason="stopped"):
        """Stop a running job: the adapter ends it (default: kill the process tree), state becomes ``stopped``."""
        job = self.state(job_id)
        if job is None or job["state"] != "running":
            return job
        adapter = self.registry.get(job["adapter"])
        adapter.stop(job, self.job_dir(job_id))
        return self._end(job, "stopped", reason)

    def live_jobs(self) -> list:
        out = []
        for name in self.store.list_names("jobs"):
            job = self.store.read(f"jobs/{name}/state.json") if "." not in name else None
            if job and job.get("state") == "running":
                out.append(job)
        return out
