"""Single-instance lock for the Waypoint runner.

The lock is a JSON file ``{pid, host, heartbeat_at, url|null}`` inside the data
folder passed in (never hard-coded). Creation uses O_CREAT|O_EXCL, which is
atomic on Windows and POSIX. Windows does not drop a file on crash, so a lock is
also recovered when its PID is dead or its heartbeat is older than ``stale_after``.
"""
from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path

LOCK_NAME = "runner.lock.json"
DEFAULT_STALE_AFTER = 30.0  # seconds without a heartbeat


def pid_alive(pid) -> bool:
    """True if a process with this PID exists."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.GetLastError() == 5  # access denied: process exists
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
        k32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass
class AcquireResult:
    ok: bool
    status: str  # "acquired" | "recovered" | "busy"
    holder: dict | None = None
    message: str = ""

    def __bool__(self) -> bool:
        return self.ok


@dataclass
class RunnerLock:
    data_dir: Path
    stale_after: float = DEFAULT_STALE_AFTER
    clock: object = field(default=time.time, repr=False)
    pid: int = field(default_factory=os.getpid)
    held: bool = False

    def __post_init__(self):
        self.data_dir = Path(self.data_dir)

    @property
    def path(self) -> Path:
        return self.data_dir / LOCK_NAME

    # -- reading -------------------------------------------------------
    def read(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return {}  # unreadable or half-written: treated as stale

    def is_stale(self, info: dict) -> bool:
        if info.get("host") == socket.gethostname() and not pid_alive(info.get("pid")):
            return True
        hb = info.get("heartbeat_at")
        if not isinstance(hb, (int, float)):
            return True
        return self.clock() - hb > self.stale_after

    # -- acquiring -----------------------------------------------------
    def _record(self, url=None) -> dict:
        return {"pid": self.pid, "host": socket.gethostname(),
                "heartbeat_at": self.clock(), "url": url}

    def _create(self) -> bool:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self._record(), fh)
        return True

    def acquire(self) -> AcquireResult:
        recovered = False
        for _ in range(3):
            if self._create():
                self.held = True
                return AcquireResult(True, "recovered" if recovered else "acquired")
            info = self.read()
            if info is None:
                continue  # vanished between create and read: retry
            if info.get("pid") == self.pid and info.get("host") == socket.gethostname():
                self.held = True
                return AcquireResult(True, "acquired", info)
            if not self.is_stale(info):
                return AcquireResult(False, "busy", info,
                                     f"runner already running (pid {info.get('pid')})")
            try:
                os.remove(self.path)
                recovered = True
            except FileNotFoundError:
                pass
            except OSError:
                return AcquireResult(False, "busy", info, "stale lock could not be removed")
        return AcquireResult(False, "busy", self.read(), "lock contended")

    # -- holding -------------------------------------------------------
    def update(self, fields: dict | None = None) -> dict:
        """Merge fields (e.g. url) into the lock and refresh the heartbeat."""
        if not self.held:
            raise RuntimeError("lock not held")
        info = self.read() or {}
        if info.get("pid") != self.pid:
            self.held = False
            raise RuntimeError("lock lost to another process")
        info.update(fields or {})
        info["heartbeat_at"] = self.clock()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(info), encoding="utf-8")
        os.replace(tmp, self.path)
        return info

    def heartbeat(self) -> dict:
        return self.update({})

    def release(self) -> None:
        if not self.held:
            return
        info = self.read()
        if info and info.get("pid") == self.pid:
            try:
                os.remove(self.path)
            except FileNotFoundError:
                pass
        self.held = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
