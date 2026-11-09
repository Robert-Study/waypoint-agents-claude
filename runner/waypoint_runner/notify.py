"""Desktop notifications (Waypoint 1.2, node run-callbacks-notify).

This is the notification source when no Claude window is open. The mod covers in-window signals.

``notify(store, event_id, kind, title, body, notifier=None)`` shows one toast per event id.
- Kinds: done, blocked, asking, failed, question. Any other kind is refused.
- A kind listed in ``settings/settings.json`` ``mute_kinds`` is silent. A muted event leaves no ledger entry,
  so unmuting later does not lose it.
- Each event id is recorded in ``notified/<digest>.json`` before the toast is shown (create_exclusive), so the
  same event toasts once even across runner runs. A failed toast is recorded as failed and not retried.
- A notification never changes job, node or receipt state. It is a side effect only.

``notifier(title, body)`` is injectable. Tests always inject a fake. The default shows a Windows balloon tip
through PowerShell and never waits for it.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

_PY = Path(__file__).resolve().parents[2] / "core" / "waypoint-data" / "py"
if str(_PY) not in sys.path and _PY.is_dir():
    sys.path.append(str(_PY))

from waypoint_data import io as wio  # noqa: E402
from waypoint_data.store import iso, serialize  # noqa: E402

__all__ = ["KINDS", "event_id", "event_for_job", "muted_kinds", "notify", "windows_toast"]

WRITER = "runner"
KINDS = ("done", "blocked", "asking", "failed", "question")
SETTINGS_REL = "settings/settings.json"
LEDGER_DIR = "notified"
MAX_TITLE = 120
MAX_BODY = 400


def event_id(kind, ref) -> str:
    """Deterministic id for one event: the same kind and reference always give the same id."""
    return f"{kind}:{ref}"


def event_for_job(job):
    """``(kind, event reference)`` for a job that just reached a notifiable state, else None.

    finished -> done, failed -> failed, recovery -> blocked. A stopped or running job raises no toast.
    """
    state = job.get("state")
    kind = {"finished": "done", "failed": "failed", "recovery": "blocked"}.get(state)
    if kind is None:
        return None
    return kind, job["id"]


def muted_kinds(store) -> set:
    """Kinds Owner muted in settings. A missing or unreadable settings file mutes nothing."""
    try:
        settings = store.read(SETTINGS_REL)
    except Exception:  # noqa: BLE001 - a bad settings read must not stop notifications
        return set()
    if not isinstance(settings, dict):
        return set()
    raw = settings.get("mute_kinds")
    if not isinstance(raw, list):
        return set()
    return {str(k) for k in raw}


def _ledger_rel(eid) -> str:
    return f"{LEDGER_DIR}/{hashlib.sha256(eid.encode('utf-8')).hexdigest()}.json"


def windows_toast(title, body) -> None:
    """Show a balloon tip on Windows. Starts PowerShell and returns without waiting for it."""
    if os.name != "nt":
        raise RuntimeError("desktop notifications need Windows")
    script = ("Add-Type -AssemblyName System.Windows.Forms; "
              "$n = New-Object System.Windows.Forms.NotifyIcon; "
              "$n.Icon = [System.Drawing.SystemIcons]::Information; $n.Visible = $true; "
              "$n.ShowBalloonTip(6000, $env:WP_TOAST_TITLE, $env:WP_TOAST_BODY, 'Info'); "
              "Start-Sleep -Seconds 7; $n.Dispose()")
    env = dict(os.environ, WP_TOAST_TITLE=str(title)[:MAX_TITLE], WP_TOAST_BODY=str(body)[:MAX_BODY])
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                     env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=flags)


def notify(store, eid, kind, title, body, notifier=None) -> dict:
    """Show one toast for ``eid``. Returns ``{"status": sent|duplicate|muted|failed|unknown_kind, ...}``."""
    if kind not in KINDS:
        return {"status": "unknown_kind", "kind": kind}
    if kind in muted_kinds(store):
        return {"status": "muted", "kind": kind}
    rel = _ledger_rel(eid)
    path = store.path(rel)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    entry = {"schema": "waypoint.notified.v1", "event_id": eid, "kind": kind, "outcome": "sent",
             "created_at": iso(store.now())}
    try:
        wio.create_exclusive(path, serialize(entry), writer=WRITER)
    except FileExistsError:
        return {"status": "duplicate", "kind": kind}
    show = notifier or windows_toast
    try:
        show(title, body)
    except Exception as exc:  # noqa: BLE001 - recorded, never raised into the runner tick
        entry["outcome"] = "failed"
        entry["reason"] = f"{type(exc).__name__}: {exc}"[:200]
        wio.atomic_write(path, serialize(entry), writer=WRITER)
        return {"status": "failed", "kind": kind, "reason": entry["reason"]}
    return {"status": "sent", "kind": kind}
