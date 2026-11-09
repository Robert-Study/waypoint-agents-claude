"""Runner lifecycle: start on demand, tick while work exists, exit when idle.

Idle means: no queued actions, no ready nodes, no live jobs and no pending scheduled runs, continuously, for
``grace_s`` seconds.
Idle is read from the data folder (through the waypoint_data client), never from a web page or a lock url.

Exit codes: 0 idle (or --once done), 1 error, 2 bad usage, 3 busy (another runner holds the lock),
130 stopped by SIGINT/SIGTERM. The lock is always released on the way out.
"""
from __future__ import annotations

import datetime as dt
import os
import signal
import sys
import time
from pathlib import Path

from .lock import RunnerLock

_PY_CLIENT = Path(__file__).resolve().parents[2] / "core" / "waypoint-data" / "py"
if str(_PY_CLIENT) not in sys.path:
    sys.path.insert(0, str(_PY_CLIENT))

from waypoint_data import Claims, Store, resolve_data_root  # noqa: E402

EXIT_IDLE = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_BUSY = 3
EXIT_INTERRUPTED = 130

DEFAULT_GRACE_S = 30.0
DEFAULT_INTERVAL_S = 2.0
ACTIVE_RUN = frozenset(("running", "queued", "starting", "active"))


def default_data_dir(env=None, home=None) -> str:
    """The data folder: WAYPOINT_DATA_DIR, then ~/.waypoint/data-dir pointer, then ~/.waypoint/data."""
    env = os.environ if env is None else env
    home = home or str(Path.home())

    def read_file(p):
        try:
            return Path(p).read_text(encoding="utf-8")
        except OSError:
            return None

    return resolve_data_root(env, home, read_file, "win32" if os.name == "nt" else "posix")["root"]


class FolderWorkSource:
    """Reads work state from the shared data folder (only through waypoint_data.Store/Claims)."""

    def __init__(self, data_dir, now=time.time, horizon_s=DEFAULT_GRACE_S):
        self.store = Store(str(data_dir), writer="runner", now=now)
        self.claims = Claims(str(data_dir), now=now, writer="runner")
        self.now = now
        self.horizon_s = horizon_s

    def _safe_list(self, rel):
        try:
            return self.store.list_names(rel)
        except (OSError, ValueError):
            return []

    def _nodes(self, project):
        out = []
        for name in self._safe_list(f"projects/{project}/nodes"):
            if name.endswith(".json"):
                try:
                    n = self.store.read(f"projects/{project}/nodes/{name}")
                except Exception:
                    n = None
                if isinstance(n, dict):
                    out.append(n)
        return out

    def _active_projects(self):
        try:
            runs = self.store.list_entities("runs")
        except Exception:
            return []
        return sorted({r.get("project") for r in runs
                       if isinstance(r, dict) and r.get("status") in ACTIVE_RUN and r.get("project")})

    def queued_actions(self):
        """Action files still ``queued`` (the web page, the mod and the CLI write them; tick.Tick applies them)."""
        out = []
        for name in self._safe_list("actions"):
            if name.endswith(".json"):
                try:
                    a = self.store.read(f"actions/{name}")
                except Exception:  # noqa: BLE001 - a torn file waits for the next pass
                    a = None
                if isinstance(a, dict) and a.get("status") == "queued":
                    out.append(a)
        return out

    def ready_nodes(self):
        out = []
        for p in self._active_projects():
            out += [n for n in self._nodes(p) if n.get("status") == "ready"]
        return out

    def live_jobs(self):
        out = []
        for p in self._active_projects():
            out += [n for n in self._nodes(p) if n.get("status") == "running"]
        return out

    def unrouted_answers(self):
        """Answered questions the runner has not routed yet (resume.py): warm session or fresh worker."""
        from . import resume
        try:
            return resume.pending(self.store.list_entities("questions"))
        except Exception:
            return []

    def pending_scheduled(self):
        """Schedules with a start time that is due now (or within the horizon) and not yet run in that slot."""
        now = dt.datetime.fromtimestamp(self.now())
        horizon = now + dt.timedelta(seconds=self.horizon_s)
        try:
            schedules = self.store.list_entities("schedules")
        except Exception:
            return []
        out = []
        for s in schedules:
            start = s.get("start") if isinstance(s, dict) else None
            if not isinstance(start, str):
                continue
            try:
                hh, mm = (int(x) for x in start.split(":"))
            except ValueError:
                continue
            slot_time = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            slot = slot_time.strftime("%Y-%m-%dT%H:%M")
            if slot_time <= horizon and (s.get("last_run") or {}).get("slot") != slot:
                out.append(s)
        return out


class Runner:
    """Holds the single-instance lock and ticks while work exists; exits when idle for ``grace_s``."""

    def __init__(self, data_dir, source, tick, grace_s=DEFAULT_GRACE_S, interval_s=DEFAULT_INTERVAL_S,
                 once=False, clock=time.time, sleep=time.sleep, install_signals=True, log=None):
        self.data_dir = Path(data_dir)
        self.source = source
        self.tick = tick
        self.grace_s = grace_s
        self.interval_s = interval_s
        self.once = once
        self.clock = clock
        self.sleep = sleep
        self.install_signals = install_signals
        self.log = log or (lambda msg: None)
        self._stop = False
        self.lock = RunnerLock(self.data_dir, clock=clock)

    def request_stop(self):
        self._stop = True

    def _has_work(self) -> bool:
        s = self.source
        answers = getattr(s, "unrouted_answers", None)
        queued = getattr(s, "queued_actions", None)
        return bool((queued and queued()) or s.ready_nodes() or s.live_jobs() or s.pending_scheduled()
                    or (answers and answers()))

    def run(self) -> int:
        old = {}
        if self.install_signals:
            # SIGBREAK is what CTRL_BREAK_EVENT raises on Windows; handlers go in before the lock exists.
            for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
                sig = getattr(signal, name, None)
                if sig is not None:
                    try:
                        old[sig] = signal.signal(sig, lambda *_: self.request_stop())
                    except ValueError:  # not the main thread
                        pass

        def restore():
            for sig, handler in old.items():
                signal.signal(sig, handler)

        res = self.lock.acquire()
        if not res.ok:
            self.log(res.message or "runner already running")
            restore()
            return EXIT_BUSY
        try:
            return self._loop()
        except KeyboardInterrupt:
            return EXIT_INTERRUPTED
        except Exception as exc:  # the lock must not outlive a crash
            self.log(f"runner error: {exc!r}")
            return EXIT_ERROR
        finally:
            self.lock.release()
            restore()

    def _loop(self) -> int:
        idle_since = None
        while True:
            if self._stop:
                return EXIT_INTERRUPTED
            self.lock.heartbeat()
            if self.once:
                self.tick(self.source)
                return EXIT_IDLE
            if self._has_work():
                idle_since = None
                self.tick(self.source)
            else:
                now = self.clock()
                if idle_since is None:
                    idle_since = now
                if now - idle_since >= self.grace_s:
                    return EXIT_IDLE
            self.sleep(self.interval_s)


def noop_tick(source) -> None:
    """Placeholder tick: dispatch is wired in by later nodes; the lifecycle only needs a callable."""
    return None
