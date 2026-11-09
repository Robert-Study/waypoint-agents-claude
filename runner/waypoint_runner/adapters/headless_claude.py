"""Headless ``claude -p`` adapter, plus the shared CLI-adapter base the Codex adapter reuses.

Reference: engine/cc_engine/headless.py (argv, env). No floor or percentage lives here. Before anything
starts, ``start`` asks ``policy.allowed`` (toggle, Owner's editable floor, a FRESH reading). A disabled
route, an unset floor on a metered pool, a below-floor, stale or unknown reading all raise ``AdapterRefused``
with the reason and any Decisions to-do text; nothing is launched.

A frontier-tier model (opus) is never a child swarm worker: a spec with a ``parent_id`` that names one is
refused.

Output goes to ``<job_dir>/output.log``; the exit code to ``<job_dir>/exit.json``. ``exec`` is injectable:
``exec(argv, cwd, stdin_text, env, out_path, exit_path)`` returns a handle with ``.pid``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .. import dispatch_core as _dc  # first: puts core/waypoint-data/py on sys.path
from .. import policy

Adapter = _dc.Adapter

__all__ = ["AdapterRefused", "CliAdapter", "HeadlessClaudeAdapter", "default_exec"]

METERED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")
CLAUDE_WORKER_ARGS = ("--permission-mode", "acceptEdits")
FRONTIER_MARKERS = ("opus",)
OUTPUT_FILE = "output.log"
EXIT_FILE = "exit.json"


class AdapterRefused(Exception):
    """The adapter will not start. ``reason`` is a policy reason or ``frontier_child``."""

    def __init__(self, reason, todo=None, detail=""):
        super().__init__(f"{reason}" + (f": {detail}" if detail else ""))
        self.reason, self.todo = reason, todo


class _Handle:
    def __init__(self, proc, thread):
        self.proc, self.thread, self.pid = proc, thread, proc.pid

    def join(self, timeout=None):
        self.thread.join(timeout)


def default_exec(argv, cwd, stdin_text, env, out_path, exit_path):
    """Run argv with the brief on stdin; stdout and stderr to ``out_path``; a thread writes the exit file."""
    out = open(out_path, "wb")
    try:
        proc = subprocess.Popen(argv, cwd=cwd or None, stdin=subprocess.PIPE, stdout=out,
                                stderr=subprocess.STDOUT, env=env)
    except Exception:
        out.close()
        raise

    def feed_and_wait():
        try:
            try:
                proc.stdin.write((stdin_text or "").encode("utf-8"))
                proc.stdin.close()
            except OSError:
                pass
            code = proc.wait()
        finally:
            out.close()
        Path(exit_path).write_text(json.dumps({"exit_code": code}), encoding="utf-8")

    t = threading.Thread(target=feed_and_wait, daemon=True)
    t.start()
    return _Handle(proc, t)


def _is_frontier(model) -> bool:
    return bool(model) and any(m in str(model).lower() for m in FRONTIER_MARKERS)


class CliAdapter(Adapter):
    """Common start/poll for a metered CLI worker. Subclasses set name, route, ``drop_env``, ``build_argv``."""

    drop_env: tuple = ()
    default_exe = ""

    def __init__(self, root=None, exec=None, exe=None, now=time.time):
        self.root = Path(root) if root else None
        self._exec = exec or default_exec
        self.exe = exe
        self.now = now

    def build_argv(self, spec, exe):  # pragma: no cover - interface
        raise NotImplementedError

    def _root(self, job_dir):
        return self.root or Path(job_dir).parent.parent

    def build_env(self):
        env = dict(os.environ)
        for k in self.drop_env:
            env.pop(k, None)
        return env

    def check(self, spec, job_dir):
        """Raise AdapterRefused unless the route may run now."""
        if spec.get("parent_id") and _is_frontier(spec.get("model")):
            raise AdapterRefused("frontier_child", detail=f"{spec.get('model')} is frontier tier")
        now = datetime.fromtimestamp(self.now(), timezone.utc)
        d = policy.allowed(self._root(job_dir), self.route, now)
        if not d.allowed:
            raise AdapterRefused(d.reason, d.todo)

    def start(self, spec, job_dir):
        job_dir = Path(job_dir)
        self.check(spec, job_dir)
        exe = self.exe or shutil.which(self.default_exe) or self.default_exe
        job_dir.mkdir(parents=True, exist_ok=True)
        exit_path = job_dir / EXIT_FILE
        if exit_path.exists():
            exit_path.unlink()
        handle = self._exec(self.build_argv(spec, exe), spec.get("cwd"), str(spec.get("brief") or ""),
                            self.build_env(), job_dir / OUTPUT_FILE, exit_path)
        return {"pid": getattr(handle, "pid", None)}

    def poll(self, job, job_dir):
        try:
            data = json.loads((Path(job_dir) / EXIT_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"state": "running"}
        code = data.get("exit_code") if isinstance(data, dict) else None
        if code == 0:
            return {"state": "finished", "exit_code": 0}
        return {"state": "failed", "exit_code": code, "reason": f"exit code {code}"}


class HeadlessClaudeAdapter(CliAdapter):
    name = "headless_claude"
    route = "claude_code"
    drop_env = METERED_ENV
    default_exe = "claude"

    def build_argv(self, spec, exe):
        model = spec.get("model") or "sonnet"
        return [exe, "-p", "--output-format", "stream-json", "--verbose", "--model", str(model),
                *CLAUDE_WORKER_ARGS]
