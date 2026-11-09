"""Recovery states: what happens to a job that did not finish.

``classify`` maps one ended job to a state:
  retry      a transient failure; run again after an exponential backoff (counted against the bound)
  parked     a provider limit (usage, rate, 403/429); wait for the pool, never counted
  stopped    the owner stopped it; never retried
  needs_you  a person must act (login, permission, or an unsettled remote side effect)
  exhausted  the bound is spent; cloud-migrate consumes this state to move the node to the cloud

This module is the single owner of bounded retries. The bound is ``max_retries`` from settings/settings.json;
a missing or unreadable setting means 0 retries (fail safe, never retry forever). The cloud lane must not
count retries itself. No usage floor lives here. Data is touched only through core/waypoint-data/py.

A job whose remote side effect is unknown (``job["remote_effect"] == "unknown"``) is settled before any
repeat: the ``settle`` callable must return True, otherwise the node goes to ``needs_you``.
"""
from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_DATA_PY = _REPO / "core" / "waypoint-data" / "py"
if _DATA_PY.is_dir() and str(_DATA_PY) not in sys.path:
    sys.path.append(str(_DATA_PY))

from waypoint_data.store import RevConflict, Store  # noqa: E402

__all__ = ["STATES", "BACKOFF_BASE_S", "BACKOFF_CAP_S", "Decision", "Ledger", "classify", "max_retries"]

WRITER = "runner"
STATES = ("retry", "parked", "stopped", "needs_you", "exhausted")
BACKOFF_BASE_S = 30   # first retry waits this long, doubling each attempt (a delay)
BACKOFF_CAP_S = 1800

_PARK = re.compile(r"usage limit|limit reached|out of (?:extra )?usage|quota|rate.?limit|too many requests|"
                   r"\b429\b|\b403\b|forbidden", re.I)
_NEEDS_YOU = re.compile(r"needs_you|login|log in|sign.?in|permission|unauthori[sz]ed|credential|\b401\b", re.I)


@dataclass
class Decision:
    state: str
    reason: str | None = None
    delay_s: float | None = None
    settle: bool = False
    attempts: int = 0
    max_retries: int = 0


def classify(job, attempts=0, max_retries=0, settled=False):
    """Decision for an ended job, or None when it finished or still runs. ``attempts`` = retries already used."""
    state = job.get("state")
    reason = job.get("reason") or ""
    if state == "stopped":
        return Decision("stopped", reason or "stopped", attempts=attempts, max_retries=max_retries)
    if state not in ("failed", "recovery"):
        return None
    if _PARK.search(reason):
        return Decision("parked", reason, None, attempts=attempts, max_retries=max_retries)
    if _NEEDS_YOU.search(reason):
        return Decision("needs_you", reason, attempts=attempts, max_retries=max_retries)
    if attempts >= max_retries:
        return Decision("exhausted", reason or "retries_exhausted", attempts=attempts, max_retries=max_retries)
    delay = min(BACKOFF_BASE_S * (2 ** attempts), BACKOFF_CAP_S)
    unknown = job.get("remote_effect") == "unknown" and not settled
    return Decision("retry", reason, delay, settle=unknown, attempts=attempts, max_retries=max_retries)


def max_retries(root) -> int:
    """Bound from settings/settings.json; 0 when missing or invalid."""
    try:
        data = Store(root, writer=WRITER).read("settings/settings.json")
    except Exception:  # noqa: BLE001
        return 0
    value = data.get("max_retries") if isinstance(data, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


class Ledger:
    """Per-node retry record at recovery/<project>__<node>.json: attempts, not_before, last job, state."""

    def __init__(self, root, now=time.time, settle=None):
        self.root = Path(root)
        self.now = now
        self.settle = settle
        self.store = Store(self.root, writer=WRITER, now=now)

    @staticmethod
    def _rel(project, node):
        return f"recovery/{project}__{node}.json"

    def _read(self, project, node):
        return self.store.read(self._rel(project, node)) or {
            "schema": "waypoint.recovery.v1", "project": project, "node": node, "attempts": 0,
            "not_before": 0, "last_job": None, "state": None, "reason": None, "settled": False}

    def attempts(self, project, node) -> int:
        return int(self._read(project, node).get("attempts") or 0)

    def state(self, project, node):
        return self._read(project, node).get("state")

    def ready(self, project, node) -> bool:
        """True only when the node is in ``retry`` and its backoff has elapsed."""
        rec = self._read(project, node)
        return rec.get("state") == "retry" and self.now() >= (rec.get("not_before") or 0)

    def handle(self, job):
        """Classify an ended job, settle an unknown remote effect, count the retry once per job, persist."""
        project, node = job["project"], job["node"]
        for _ in range(5):
            rec = self._read(project, node)
            expected = rec.get("rev", 0)
            bound = max_retries(self.root)
            if rec.get("last_job") == job["id"] and rec.get("state"):
                return Decision(rec["state"], rec.get("reason"), rec.get("delay_s"), False, rec["attempts"], bound)
            settled = bool(rec.get("settled"))
            d = classify(job, rec["attempts"], bound, settled)
            if d is None:
                return None
            if d.state == "retry" and d.settle:
                if self.settle is not None and self.settle(job) is True:
                    settled = True
                    d.settle = False
                else:
                    d = Decision("needs_you", "unsettled_remote_effect", attempts=rec["attempts"], max_retries=bound)
            counted = d.state == "retry"
            new = dict(rec, last_job=job["id"], state=d.state, reason=d.reason, delay_s=d.delay_s,
                       attempts=rec["attempts"] + (1 if counted else 0),
                       not_before=self.now() + d.delay_s if counted else 0,
                       settled=settled and counted)
            try:
                self.store.write(self._rel(project, node), new, expected_rev=expected)
            except RevConflict:
                continue
            d.attempts = new["attempts"]
            return d
        raise RuntimeError("recovery ledger busy")
