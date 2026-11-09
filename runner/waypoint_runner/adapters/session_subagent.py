"""Adapter: run a node as a subagent inside the user's open Claude session, through the Waypoint mod.

The runner never calls a model. ``start`` only prepares a dispatch request (``dispatch/requests/<job_id>.json``,
schema ``waypoint.dispatch_request.v1``) plus the brief file; the mod (claude_mod hooks, dispatch.ts) picks the
request up inside an open session and writes ``dispatch/receipts/<job_id>.json`` (``waypoint.dispatch_receipt.v1``).
``poll`` maps that receipt back to job states. The two sides agree through the schema only.

Phases, kept in ``jobs/<job_id>/adapter.json`` (the runner's own note, never read by the mod):
  waiting-for-session  no open session to take the job: nothing is requested, no new chat is opened
  requested            request file written, the mod has not answered yet
  started              receipt says a session took it; ``session_id`` records which one
  finished | failed    receipt result (``refused`` counts as failed)
  stopped              stopped by the runner before pickup

The toggle and floor are checked by the dispatcher at dispatch and again here right before the request is
written, so a job that waited is never released after Owner turned the route off or raised a floor.
No floor value lives here.

``ModBridgeAdapter`` is the shared bridge; ``SessionSubagentAdapter`` is the subagent route. The side session
route is a separate adapter in ``side_session.py``.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from .. import dispatch_core as dc  # first: puts core/waypoint-data/py on sys.path
from .. import policy
from waypoint_data import io as wio  # noqa: E402
from waypoint_data.store import iso, serialize  # noqa: E402

__all__ = ["ModBridgeAdapter", "SessionSubagentAdapter", "WAITING", "open_claude_sessions"]

WAITING = "waiting-for-session"
REQUEST_SCHEMA = "waypoint.dispatch_request.v1"
RECEIPT_SCHEMA = "waypoint.dispatch_receipt.v1"
LIVE_SESSION_STATUS = ("running", "idle", "waiting")


def _read(path):
    return wio.read_json(str(path))


def open_claude_sessions(root) -> list:
    """Default presence probe: ids of live Claude sessions recorded in ``sessions/*.json``.

    Replaceable through the adapter's ``sessions_open`` argument (tests pass a fake)."""
    out = []
    sdir = Path(root) / "sessions"
    if not sdir.is_dir():
        return out
    for name in wio.list_names(str(sdir)):
        if not name.endswith(".json"):
            continue
        sess = _read(sdir / name)
        if isinstance(sess, dict) and sess.get("status") in LIVE_SESSION_STATUS \
                and str(sess.get("agent") or "").startswith("claude"):
            out.append(sess.get("id") or name[:-5])
    return out


class ModBridgeAdapter(dc.Adapter):
    """Request-file bridge to the mod. Subclasses set ``name``/``route`` and may override ``_eligible``."""

    def __init__(self, sessions_open=None, now=time.time):
        self.sessions_open = sessions_open
        self.now = now

    # -- helpers
    @staticmethod
    def _root(job_dir) -> Path:
        return Path(job_dir).parent.parent

    def _sessions(self, root) -> list:
        probe = self.sessions_open or open_claude_sessions
        return [s for s in (probe(root) or []) if self._eligible(s)]

    def _eligible(self, session_id) -> bool:  # noqa: ARG002
        return True

    def _note_path(self, job_dir) -> Path:
        return Path(job_dir) / "adapter.json"

    def _note(self, job_dir) -> dict:
        data = _read(self._note_path(job_dir))
        return data if isinstance(data, dict) else {}

    def _save_note(self, job_dir, **changes) -> dict:
        note = dict(self._note(job_dir), **changes)
        wio.atomic_write(str(self._note_path(job_dir)), serialize(note), writer=dc.WRITER)
        return note

    def request_path(self, root, job_id) -> Path:
        return Path(root) / "dispatch" / "requests" / f"{job_id}.json"

    def receipt_path(self, root, job_id) -> Path:
        return Path(root) / "dispatch" / "receipts" / f"{job_id}.json"

    def _write_request(self, spec, job_dir, root) -> str:
        job_id = spec["job_id"]
        brief_file = Path(job_dir) / "brief.md"
        brief = spec.get("brief")
        text = brief if isinstance(brief, str) else serialize(brief)
        wio.atomic_write(str(brief_file), text, writer=dc.WRITER)
        req = {"schema": REQUEST_SCHEMA, "id": job_id, "project": spec["project"], "node": spec["node"],
               "route": self.route, "mode": spec["mode"], "brief_path": str(brief_file),
               "created_at": iso(self.now()), "requested_by": spec.get("requested_by") or dc.WRITER}
        path = self.request_path(root, job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        wio.atomic_write(str(path), serialize(req), writer=dc.WRITER)
        return str(path)

    def _release(self, spec, job_dir, root) -> str:
        """Write the request if a session is open and the route is still allowed. Returns the phase."""
        decision = policy.allowed(root, self.route, datetime.fromtimestamp(self.now(), timezone.utc))
        if not decision.allowed:
            self._save_note(job_dir, phase="failed", reason=f"route_not_allowed:{decision.reason}")
            return "failed"
        if not self._sessions(root):
            self._save_note(job_dir, phase=WAITING)
            return WAITING
        path = self._write_request(spec, job_dir, root)
        self._save_note(job_dir, phase="requested", request=path)
        return "requested"

    # -- adapter interface
    def start(self, spec, job_dir):
        root = self._root(job_dir)
        self._save_note(job_dir, phase=WAITING, route=self.route, job_id=spec["job_id"])
        phase = self._release(spec, job_dir, root)
        return {"pid": None, "phase": phase}

    def poll(self, job, job_dir):
        root = self._root(job_dir)
        phase = self._note(job_dir).get("phase")
        if phase == "failed":
            return {"state": "failed", "reason": self._note(job_dir).get("reason") or "failed"}
        if phase == WAITING:
            spec = _read(Path(job_dir) / "spec.json")
            if isinstance(spec, dict):
                phase = self._release(spec, job_dir, root)
            if phase == "failed":
                return {"state": "failed", "reason": self._note(job_dir).get("reason")}
            if phase == WAITING:
                dc.write_heartbeat(job_dir, self.now)  # the runner is alive and waiting, not dead
                return {"state": "running", "phase": WAITING}
        rec = _read(self.receipt_path(root, job["id"]))
        if not isinstance(rec, dict) or rec.get("schema") != RECEIPT_SCHEMA:
            return {"state": "running", "phase": self._note(job_dir).get("phase")}
        status = rec.get("status")
        sid = rec.get("session_id")
        if status == "started":
            self._save_note(job_dir, phase="started", session_id=sid, receipt_at=rec.get("at"))
            return {"state": "running", "phase": "started", "session_id": sid}
        if status == "finished":
            self._save_note(job_dir, phase="finished", session_id=sid, receipt_at=rec.get("at"))
            return {"state": "finished", "exit_code": 0, "session_id": sid}
        reason = rec.get("detail") or status or "failed"
        self._save_note(job_dir, phase="failed", session_id=sid, reason=reason, receipt_at=rec.get("at"))
        return {"state": "failed", "reason": reason, "session_id": sid}

    def stop(self, job, job_dir):
        """Withdraw a request the mod has not picked up. A session already running the job is not killed here."""
        root = self._root(job_dir)
        if self._note(job_dir).get("phase") in (WAITING, "requested"):
            wio.remove_if_exists(str(self.request_path(root, job["id"])))
        self._save_note(job_dir, phase="stopped")


class SessionSubagentAdapter(ModBridgeAdapter):
    """Subagent in the user's open session."""

    name = "session_subagent"
    route = "session_subagent"
