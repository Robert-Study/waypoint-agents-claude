"""Child-to-parent callbacks (Waypoint 1.2, node run-callbacks-notify).

When a job reaches a terminal state the runner writes ONE callback file to the parent orchestrator's inbox:
``inbox/<parent_id>/<job_id>.json`` in the data folder (``inbox/orchestrator/`` when the job has no parent).
The file carries the receipt summary, so the parent reads the outcome without opening the job.

Rules
- Only terminal jobs. A running job never writes a callback, and a receipt alone never does either.
- Only three outcomes reach a parent: ``done``, ``blocked`` and ``asking``. Never "still waiting".
  - finished job, receipt done (or no receipt)            -> done
  - finished job, receipt error                           -> blocked
  - receipt needs_you (any terminal state)                -> asking
  - failed or recovery job (the job cannot finish)        -> blocked
  - stopped job (Owner stopped it on purpose)            -> no callback
- Write once: ``create_exclusive`` never overwrites, so a re-run of the same job adds nothing ("duplicate").
- Writes go through the folder store (``core/waypoint-data/py``), never SQLite.
- A callback never marks a node done. The node check decides, as before.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_PY = Path(__file__).resolve().parents[2] / "core" / "waypoint-data" / "py"
if str(_PY) not in sys.path and _PY.is_dir():
    sys.path.append(str(_PY))

from waypoint_data import io as wio  # noqa: E402
from waypoint_data.store import iso  # noqa: E402

__all__ = ["SCHEMA", "CALLBACK_KINDS", "callback_kind", "build_callback", "callback_rel", "write_callback"]

SCHEMA = "waypoint.callback.v1"
WRITER = "runner"
CALLBACK_KINDS = ("done", "blocked", "asking")
TERMINAL_STATES = ("finished", "failed", "recovery", "stopped")
DEFAULT_PARENT = "orchestrator"
_SAFE = re.compile(r"[^A-Za-z0-9._-]")
MAX_SUMMARY = 300


def _safe(name) -> str:
    return _SAFE.sub("_", str(name))[:120] or "x"


def callback_kind(state, receipt_status=None):
    """The callback kind for a terminal job, or None when no callback is written."""
    if state not in TERMINAL_STATES:
        return None
    if receipt_status == "needs_you":
        return "asking"
    if state in ("failed", "recovery"):
        return "blocked"
    if state == "finished":
        return "blocked" if receipt_status == "error" else "done"
    return None  # stopped


def callback_rel(job) -> str:
    """Relative path of the callback file in the data folder."""
    parent = job.get("parent_id") or DEFAULT_PARENT
    return f"inbox/{_safe(parent)}/{_safe(job['id'])}.json"


def build_callback(store, job, receipt=None, receipt_name=None):
    """The callback object for a terminal job, or None when no callback applies."""
    status = receipt.get("status") if isinstance(receipt, dict) else None
    kind = callback_kind(job.get("state"), status)
    if kind is None:
        return None
    summary = None
    if isinstance(receipt, dict) and receipt.get("summary"):
        summary = str(receipt["summary"])[:MAX_SUMMARY]
    elif job.get("reason"):
        summary = str(job["reason"])[:MAX_SUMMARY]
    return {
        "schema": SCHEMA,
        "job_id": job["id"],
        "project": job.get("project"),
        "node": job.get("node"),
        "parent_id": job.get("parent_id"),
        "kind": kind,
        "state": job.get("state"),
        "summary": summary,
        "receipt": receipt_name,
        "created_at": iso(store.now()),
    }


def write_callback(store, job, receipt=None, receipt_name=None) -> str:
    """Write the callback once. Returns ``written``, ``duplicate`` (already there) or ``skipped`` (no callback)."""
    if job.get("state") not in TERMINAL_STATES:
        return "skipped"
    body = build_callback(store, job, receipt, receipt_name)
    if body is None:
        return "skipped"
    path = store.path(callback_rel(job))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    try:
        wio.create_exclusive(path, store.serialize(body), writer=WRITER)
    except FileExistsError:
        return "duplicate"
    return "written"
