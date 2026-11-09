"""Memory floor: free physical memory and commit charge, read with the standard library only.

7 Nov 2026: Owner's PC (7.8 GB RAM) ran out of memory during the build. The dispatch gate (policy.allowed)
refuses to start a NEW worker while free RAM is below ``memory_min_free_gb`` or commit charge is above
``memory_max_commit_pct`` (settings/settings.json). Running workers are never touched. If memory cannot be read
the gate stays open (it cannot know) and the reading says so.

REVERSIBLE_AGENT_DEFAULT (by=agent; undo=set the two settings): 1.5 GB free and 85% commit.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_MIN_FREE_GB = 1.5
DEFAULT_MAX_COMMIT_PCT = 85.0
GB = 1024 ** 3


def _num(value, default, lo=0, hi=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < lo or (hi is not None and value > hi):
        return default
    return value


def thresholds(root) -> tuple[float, float]:
    """(min_free_gb, max_commit_pct) from settings/settings.json, defaults when missing or invalid."""
    try:
        data = json.loads((Path(root) / "settings" / "settings.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        data = None
    data = data if isinstance(data, dict) else {}
    return (_num(data.get("memory_min_free_gb"), DEFAULT_MIN_FREE_GB),
            _num(data.get("memory_max_commit_pct"), DEFAULT_MAX_COMMIT_PCT, 0, 100))


def _windows():
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]

    st = MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
        raise OSError("GlobalMemoryStatusEx failed")
    commit = (st.ullTotalPageFile - st.ullAvailPageFile) / st.ullTotalPageFile * 100 if st.ullTotalPageFile else None
    return st.ullAvailPhys / GB, st.ullTotalPhys / GB, commit, "GlobalMemoryStatusEx"


def _proc():
    info = {}
    with open("/proc/meminfo", encoding="utf-8") as fh:
        for line in fh:
            k, _, v = line.partition(":")
            parts = v.split()
            if parts:
                info[k] = int(parts[0]) * 1024
    free = info.get("MemAvailable", info.get("MemFree"))
    total = info["MemTotal"]
    limit, used = info.get("CommitLimit"), info.get("Committed_AS")
    commit = used / limit * 100 if limit and used is not None else None
    return free / GB, total / GB, commit, "proc_meminfo"


def _sysconf():
    page = os.sysconf("SC_PAGE_SIZE")
    return os.sysconf("SC_AVPHYS_PAGES") * page / GB, os.sysconf("SC_PHYS_PAGES") * page / GB, None, "sysconf"


def read_system() -> dict:
    """{free_gb, total_gb, commit_pct, source}; every field None and source 'unavailable' if unreadable."""
    readers = (_windows,) if sys.platform == "win32" else (_proc, _sysconf)
    for fn in readers:
        try:
            free, total, commit, source = fn()
            return {"free_gb": round(free, 2), "total_gb": round(total, 2),
                    "commit_pct": None if commit is None else round(commit, 1), "source": source}
        except Exception:  # noqa: BLE001 - next reader, then unavailable
            continue
    return {"free_gb": None, "total_gb": None, "commit_pct": None, "source": "unavailable"}


def evaluate(reading, min_free_gb, max_commit_pct, now=None) -> dict:
    """Build the memory.json record from a raw reading and the two settings."""
    now = now or datetime.now(timezone.utc)
    free, commit = reading.get("free_gb"), reading.get("commit_pct")
    reasons = []
    if free is not None and free < min_free_gb:
        reasons.append(f"free memory is {free:.1f} GB, below the {min_free_gb:g} GB minimum")
    if commit is not None and commit > max_commit_pct:
        reasons.append(f"commit charge is {commit:.0f}%, above the {max_commit_pct:g}% limit")
    if reasons:
        reason = "Not starting a new worker: " + " and ".join(reasons) + "."
    elif free is None and commit is None:
        reason = "Memory could not be read; not blocking."
    else:
        reason = "ok"
    return {"schema": "waypoint.memory.v1", "free_gb": free, "total_gb": reading.get("total_gb"),
            "commit_pct": commit, "min_free_gb": min_free_gb, "max_commit_pct": max_commit_pct,
            "ok": not reasons, "reason": reason, "read_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": reading.get("source", "unavailable")}


def record(root, rec) -> bool:
    """Write usage/memory.json (atomic replace). False if the folder cannot be written."""
    try:
        d = Path(root) / "usage"
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "memory.json.tmp"
        tmp.write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, d / "memory.json")
        return True
    except OSError:
        return False


def gate_enabled() -> bool:
    """WAYPOINT_MEMORY_GATE=on|off overrides. Unset: on, except inside pytest, so other tests do not depend on
    how much RAM the machine has at that moment (test_memory_floor.py sets it on)."""
    v = os.environ.get("WAYPOINT_MEMORY_GATE", "").strip().lower()
    if v in ("on", "off"):
        return v == "on"
    return "PYTEST_CURRENT_TEST" not in os.environ


def check(root, reader=None, now=None) -> dict:
    """Read memory, judge it against the settings, record it, return the record."""
    lo, hi = thresholds(root)
    rec = evaluate((reader or read_system)(), lo, hi, now)
    record(root, rec)
    return rec
