"""Done-check evaluation for the Waypoint runner.

Check types: ``command`` (``run``, optional ``expect_stdout``), ``files_exist`` (``paths``),
``text_present`` (``path``, ``text``) and ``human``. Outcomes are ``pass``, ``fail`` or
``waiting-on-Owner``. A human check is never a pass. A timeout is a fail with the detail kept.
A passing command is evidence; the caller decides what a pass means for the node.
Ported from engine/cc_engine/checks.py (the engine's SQLite writes are not part of this module).
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

__all__ = ["CHECK_TYPES", "DETAIL_LIMIT", "PASS", "FAIL", "WAITING", "NodeCheckResult", "run_node_check"]

CHECK_TYPES = ("command", "files_exist", "text_present", "human")
DETAIL_LIMIT = 1500
PASS, FAIL, WAITING = "pass", "fail", "waiting-on-Owner"


@dataclass(frozen=True)
class NodeCheckResult:
    outcome: str
    detail: str

    @property
    def passed(self) -> bool:
        return self.outcome == PASS

    def __iter__(self):  # allows ``outcome, detail = result``
        return iter((self.outcome, self.detail))


def _trim(text, limit=DETAIL_LIMIT):
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit // 2] + " ... " + text[-limit // 2:]


def default_runner(cmd, cwd, timeout):
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
    p = subprocess.run(cmd, shell=True, cwd=str(cwd), capture_output=True, text=True, env=env,
                       timeout=timeout, encoding="utf-8", errors="replace")
    return p.returncode, p.stdout, p.stderr


def _resolve(cwd, p):
    path = Path(p)
    return path if path.is_absolute() else Path(cwd) / path


def run_node_check(check, cwd, runner=None, timeout=1800) -> NodeCheckResult:
    """Evaluate one done-check. ``runner(cmd, cwd, timeout) -> (code, stdout, stderr)`` is injectable."""
    check = check or {}
    kind = check.get("type")
    cwd = Path(cwd or ".")
    if kind == "human":
        return NodeCheckResult(WAITING, "Needs Owner: " + str(check.get("prompt") or "owner confirmation"))
    if kind == "command":
        cmd = check.get("run")
        if not cmd:
            return NodeCheckResult(FAIL, "command check has no 'run'")
        try:
            code, out, err = (runner or default_runner)(cmd, cwd, timeout)
        except subprocess.TimeoutExpired:
            return NodeCheckResult(FAIL, f"command timed out after {timeout}s: {cmd}")
        except OSError as exc:
            return NodeCheckResult(FAIL, f"command could not start: {exc}")
        if code != 0:
            return NodeCheckResult(FAIL, f"exit {code}: {cmd}\n{_trim(out)}\n{_trim(err)}".strip())
        want = check.get("expect_stdout")
        if want is not None and (out or "").strip() != str(want).strip():
            return NodeCheckResult(FAIL, f"expected stdout {str(want).strip()!r}, got {_trim(out)!r}")
        return NodeCheckResult(PASS, f"exit 0: {cmd}\n{_trim(out)}".strip())
    if kind == "files_exist":
        paths = check.get("paths") or []
        if not paths:
            return NodeCheckResult(FAIL, "files_exist check lists no paths")
        missing = [p for p in paths if not _resolve(cwd, p).exists()]
        if missing:
            return NodeCheckResult(FAIL, "missing: " + ", ".join(missing))
        return NodeCheckResult(PASS, "present: " + ", ".join(paths))
    if kind == "text_present":
        path, text = check.get("path"), check.get("text")
        if not path or not text:
            return NodeCheckResult(FAIL, "text_present check needs 'path' and 'text'")
        try:
            body = _resolve(cwd, path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return NodeCheckResult(FAIL, f"cannot read {path}")
        if text in body:
            return NodeCheckResult(PASS, f"{path} contains {text!r}")
        return NodeCheckResult(FAIL, f"{path} does not contain {text!r}")
    return NodeCheckResult(FAIL, f"unknown check type {kind!r}")
