"""Scheduler for timed runs: which schedule slots are due now.

Rules: a slot is ``<schedule id>@<local YYYY-MM-DDTHH:MM>`` (the dedupe key). Only the
latest slot at or before ``now`` can be due, so a runner started after missed slots runs
the latest one and never replays the rest (no backfill). A slot already recorded as run
(``last_run.slot``, a ``ran`` key set, or a claim file) is skipped. A slot whose time is
outside the schedule's ``window`` is skipped, not deferred. Manual schedules (no ``start``)
are never due. Pure logic plus one exclusive claim file; all data access goes through
``waypoint_data`` (the Python data client). No spend or limit constants live here.
"""
from __future__ import annotations

import datetime as dt
import re
import sys
from dataclasses import dataclass
from pathlib import Path

_HM = re.compile(r"^(\d{1,2}):(\d{2})$")
SLOT_DIR = "schedules/slots"


@dataclass(frozen=True)
class DueSlot:
    schedule: dict
    slot: str  # local ISO minute, e.g. 2026-11-07T22:00

    @property
    def key(self) -> str:
        return slot_key(self.schedule["id"], self.slot)


def slot_key(schedule_id: str, slot: str) -> str:
    return f"{schedule_id}@{slot}"


def parse_hm(text):
    """Minutes since midnight for 'HH:MM', or None if not valid."""
    m = _HM.match(text.strip()) if isinstance(text, str) else None
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return h * 60 + mi if h < 24 and mi < 60 else None


def in_window(window, minute_of_day: int) -> bool:
    """True when the time is inside 'HH:MM-HH:MM' (may wrap midnight). No window = always."""
    if not window:
        return True
    parts = str(window).split("-")
    if len(parts) != 2:
        return True
    a, b = parse_hm(parts[0]), parse_hm(parts[1])
    if a is None or b is None or a == b:
        return True
    return a <= minute_of_day < b if a < b else (minute_of_day >= a or minute_of_day < b)


def latest_slot(start: str, now: dt.datetime):
    """The most recent slot datetime at or before ``now`` for a daily local start time."""
    m = parse_hm(start)
    if m is None:
        return None
    today = now.replace(hour=m // 60, minute=m % 60, second=0, microsecond=0)
    return today if today <= now else today - dt.timedelta(days=1)


def due_slots(now: dt.datetime, schedules, ran=None):
    """Each due slot once, at most one per schedule. ``now`` is naive local time."""
    ran = set(ran or ())
    out = []
    for s in schedules:
        slot_dt = latest_slot(s.get("start"), now)
        if slot_dt is None:
            continue
        slot = slot_dt.strftime("%Y-%m-%dT%H:%M")
        if slot == (s.get("last_run") or {}).get("slot") or slot_key(s["id"], slot) in ran:
            continue
        if not in_window(s.get("window"), now.hour * 60 + now.minute):
            continue
        out.append(DueSlot(s, slot))
    return out


def _store_types():
    p = str(Path(__file__).resolve().parents[2] / "core" / "waypoint-data" / "py")
    if p not in sys.path:
        sys.path.insert(0, p)
    from waypoint_data.store import RevConflict
    return RevConflict


def _slot_rel(due: DueSlot) -> str:
    return f"{SLOT_DIR}/{due.schedule['id']}.{due.slot.replace(':', '')}.json"


def claim_slot(store, due: DueSlot) -> bool:
    """Exclusively record that this slot is being run. False if another runner already has it."""
    RevConflict = _store_types()
    try:
        store.write(_slot_rel(due), {"key": due.key, "schedule": due.schedule["id"], "slot": due.slot}, expected_rev=0)
        return True
    except RevConflict:
        return False


def claimed_keys(store) -> set:
    """Keys of every slot already claimed."""
    return {e["key"] for e in store.list_entities(SLOT_DIR) if isinstance(e, dict) and "key" in e}
