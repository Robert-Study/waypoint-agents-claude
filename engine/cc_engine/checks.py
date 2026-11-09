"""Done-check runner. Only ``complete_node`` (actor ``check:<type>``) or the owner sets ``done``.

Check types: ``command`` (``run``, optional ``expect_stdout``), ``files_exist`` (``paths``),
``text_present`` (``path``, ``text``) and ``human`` (never auto-passes).
"""
from __future__ import annotations

import subprocess
import sys
import os
from dataclasses import dataclass
from pathlib import Path

CHECK_TYPES = ("command", "files_exist", "text_present", "human")
DETAIL_LIMIT = 1500


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    detail: str

    def __iter__(self):  # allows ``ok, detail = result``
        return iter((self.ok, self.detail))


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


def run_check(check: dict, cwd, runner=None, timeout=1800) -> CheckResult:
    check = check or {}
    kind = check.get("type")
    cwd = Path(cwd or ".")
    if kind == "human":
        return CheckResult(False, "Needs Owner: " + str(check.get("prompt") or "owner confirmation"))
    if kind == "command":
        cmd = check.get("run")
        if not cmd:
            return CheckResult(False, "command check has no 'run'")
        try:
            code, out, err = (runner or default_runner)(cmd, cwd, timeout)
        except subprocess.TimeoutExpired:
            return CheckResult(False, f"command timed out after {timeout}s: {cmd}")
        except OSError as exc:
            return CheckResult(False, f"command could not start: {exc}")
        if code != 0:
            return CheckResult(False, f"exit {code}: {cmd}\n{_trim(out)}\n{_trim(err)}".strip())
        want = check.get("expect_stdout")
        if want is not None and (out or "").strip() != str(want).strip():
            return CheckResult(False, f"expected stdout {str(want).strip()!r}, got {_trim(out)!r}")
        return CheckResult(True, f"exit 0: {cmd}\n{_trim(out)}".strip())
    if kind == "files_exist":
        paths = check.get("paths") or []
        if not paths:
            return CheckResult(False, "files_exist check lists no paths")
        missing = [p for p in paths if not _resolve(cwd, p).exists()]
        if missing:
            return CheckResult(False, "missing: " + ", ".join(missing))
        return CheckResult(True, "present: " + ", ".join(paths))
    if kind == "text_present":
        path, text = check.get("path"), check.get("text")
        if not path or not text:
            return CheckResult(False, "text_present check needs 'path' and 'text'")
        f = _resolve(cwd, path)
        try:
            body = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return CheckResult(False, f"cannot read {path}")
        if text in body:
            return CheckResult(True, f"{path} contains {text!r}")
        return CheckResult(False, f"{path} does not contain {text!r}")
    return CheckResult(False, f"unknown check type {kind!r}")


def complete_node(store, project_id, node_id, runner=None, cwd=None, timeout=1800,
                  record_failure=True, owner_action=False) -> CheckResult:
    """Run the node's done-check and record the truthful outcome.

    pass -> ``done`` (actor ``check:<type>``); ``human`` -> ``needs_you``; fail -> ``error``.
    A node whose dependencies are not all done is left alone. ``record_failure=False`` probes
    without turning an untouched node red.
    """
    node = store.get_node(project_id, node_id)
    if node is None:
        raise KeyError(f"no node {project_id}/{node_id}")
    status = {n["id"]: n["status"] for n in store.list_nodes(project_id)}
    waiting = [d for d in node.get("deps") or [] if status.get(d) != "done"]
    if waiting:
        return CheckResult(False, "waiting on " + ", ".join(waiting))
    project = store.get_project(project_id) or {}
    check = node.get("check") or {}
    kind = check.get("type") or "unknown"
    actor = f"check:{kind}"
    res = run_check(check, cwd or project.get("cwd") or ".", runner=runner, timeout=timeout)
    if kind == "human":
        if node["status"] not in ("needs_you", "done"):
            store.set_node_status(project_id, node_id, "needs_you", actor, res.detail,
                                  f"Task {node_id} is waiting for Owner to confirm it.")
        return res
    # The check hash on the result event lets the board count only results for the current check.
    stamp = {"check_hash": node.get("check_hash")}
    if res.ok:
        from . import loops  # lane: closed needs an owner action, wide a deterministic check
        allowed, why = loops.can_complete(dict(node, project_id=project_id), kind, True, owner_action, store)
        if not allowed:
            if node["status"] != "needs_you":
                store.set_node_status(project_id, node_id, "needs_you", actor, res.detail,
                                      f"Task {node_id} passed its {kind} check but {why}.", data=stamp)
            return CheckResult(False, f"{res.detail}; {why}")
        loops.clear_reason(store, project_id, node_id)
        if node["status"] != "done":
            store.set_node_status(project_id, node_id, "done", actor, res.detail,
                                  f"Task {node_id} passed its {kind} check and is done.", data=stamp)
    elif record_failure:
        store.set_node_status(project_id, node_id, "error", actor, res.detail,
                              f"Task {node_id} failed its {kind} check.", data=stamp)
    return res
