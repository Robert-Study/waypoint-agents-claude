"""Dispatchers: turn a ready node into a detached job on a zero-extra-spend route.

Routes
- ``claude_code``: ``claude -p --model <m> --output-format json`` in the node cwd
  (prompt on stdin; ``--effort`` added when the node has one). Metered API keys and
  base URLs are removed from the child environment.
- ``freebuff``: OpenAI-compatible POST to ``http://127.0.0.1:1455/v1/chat/completions``.
  Model ids come from the freebuff provider block of ``~/.dsh/cordis.patch.yml``
  (read-only, with a fallback). Freebuff writes text only, so it takes only nodes
  whose check is ``text_present``/``human`` or whose brief says text-only, unless the
  run sheet's ``freebuff_file_work`` flag allows more.
- ``gemini_app`` / ``chatgpt_web`` / ``antigravity_manual`` / ``manual``: write a Copy Prompt packet
  (``prompt.md`` plus ``packet.json`` with the prompt, cwd and done-check) and raise a
  needs_you question. Never automated. ``antigravity_manual`` is Antigravity (``agy``) as a
  manual agentic coder: Owner opens it in the packet's folder himself; it never runs unattended.
- ``codex``: ``codex exec --json --skip-git-repo-check --sandbox workspace-write -C <cwd> [-m <model>]
  [-c model_reasoning_effort=<effort>] -`` with the prompt on stdin, as a job like ``claude_code``.
  Metered OpenAI keys and ``CODEX_HOME`` are dropped in the job wrapper, so it runs on Owner's
  Codex plan. The scheduler gates it on the ``codex_5h``/``codex_week`` usage exactly like Claude.
- Anything Owner switched off in Agents is refused (``refusal``): ``cc_engine.toggles`` (the one
  switch per agent, route and model; Codex is off until he switches it on) decides, plus the
  guarded ``cc_engine.models.is_disabled(route, model)`` for registry aliases.

Every command and request comes from the shared adapters in ``cc_engine.providers``, the same
module the Super-Agent ticket server (``dsh_subagent_mcp_server.py``) imports, so both build
identical commands for the same inputs.

Every automated route runs through a small job wrapper (``python -m
cc_engine.dispatch job <spec.json>``) so a later tick, in a different process,
can see whether the job is still alive, finished (``exit.json``) or lost. The
wrapper's pid and start time are kept in ``launch.json``; a job is lost only when
that pid is no longer alive (``pid_alive``: OpenProcess/GetExitCodeProcess on
Windows), never because its heartbeat went quiet during sleep. A job whose
active run time passes its timeout (default 90 min) has its process tree killed
(``taskkill /T /F``) and is reported as ``timeout``.

All side effects are injectable: ``launcher`` starts the wrapper,
``exec_runner`` / ``http_post`` run inside it, ``clock`` and ``sleep`` drive the
waits, ``pid_alive`` / ``killer`` check and stop processes. Tests use fakes only.
"""
from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import config, env_policy, node_kinds
from . import providers

# v3: every model connection is a real route. ``agy_cli`` is Antigravity run natively through
# ``agy --print``; it shares the one Antigravity switch (``antigravity_manual``) in Agents.
CLI_ROUTES = ("groq", "gemini_studio", "copilot", "deepseek_dsh", "agy_cli")
ROUTES = ("claude_code", "codex", "freebuff", "gemini_app", "chatgpt_web", "antigravity_manual", "manual") + CLI_ROUTES
AUTOMATED_ROUTES = ("claude_code", "codex", "freebuff") + CLI_ROUTES
MANUAL_ROUTES = ("gemini_app", "chatgpt_web", "antigravity_manual", "manual")
# LIVE2: with no permission mode ``claude -p`` denies Write, exits 0 and the check fails. Edits
# only (never bypassPermissions); run settings' ``claude_extra_args`` replace this when given.
CLAUDE_WORKER_ARGS = ("--permission-mode", "acceptEdits")

FREEBUFF_URL = providers.FREEBUFF_CHAT_URL
FREEBUFF_CONFIG = Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".dsh" / "cordis.patch.yml"
FREEBUFF_FALLBACK_MODELS = (providers.FREEBUFF_DEFAULT_MODEL,)
FREEBUFF_TEXT_CHECKS = ("text_present", "human")
CLAUDE_EFFORTS = providers.CLAUDE_EFFORTS

HEARTBEAT_SECONDS = 30
DEFAULT_JOB_TIMEOUT_MINUTES = 90
# A legacy job dir with no recorded pid counts as lost only after a whole timeout.
LOST_AFTER_SECONDS = DEFAULT_JOB_TIMEOUT_MINUTES * 60
# Environment variables that would turn a subscription route into metered spend.
METERED_ENV = providers.METERED_ENV
# Codex runs on Owner's Codex plan: no OpenAI API keys, and the default ~/.codex home.
CODEX_DROP_ENV = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE", "CODEX_API_KEY",
                  "AZURE_OPENAI_API_KEY", "CODEX_HOME")
CODEX_SANDBOX = "workspace-write"

DEFAULT_DATA_DIR = Path(config.DATA_DIR)  # the configured data folder (settings file / WAYPOINT_DATA_DIR)

# Pool name (as ticked on the run sheet / stored on the node) -> route.
POOL_ROUTES = {
    "claude": "claude_code",
    "claude_code": "claude_code",
    "claude_max": "claude_code",
    "codex": "codex",
    "codex_cli": "codex",
    "chatgpt": "chatgpt_web",
    "chatgpt_web": "chatgpt_web",
    "freebuff": "freebuff",
    "deepseek": "freebuff",
    "gemini": "gemini_app",
    "gemini_app": "gemini_app",
    "antigravity": "antigravity_manual",
    "antigravity_manual": "antigravity_manual",
    "agy": "antigravity_manual",
    "manual": "manual",
    "groq": "groq",
    "gemini_studio": "gemini_studio",
    "ai_studio": "gemini_studio",
    "gemini_api": "gemini_studio",
    "copilot": "copilot",
    "github_copilot": "copilot",
    "deepseek_dsh": "deepseek_dsh",
    "dsh": "deepseek_dsh",
    "agy_cli": "agy_cli",
    "antigravity_cli": "agy_cli",
}


def route_for_pool(pool):
    """Route for a pool name; unknown pools never get an automated route."""
    name = (pool or "").strip().lower()
    return POOL_ROUTES.get(name, "manual")


# ---------------------------------------------------------------- route state (ready / out of credits / ...)
FREEBUFF_PORT = 1455
QUOTA_RE = re.compile(r"(?i)(out of (weekly |five.?hour |daily )?(credits|quota)|quota (exceeded|exhausted|reached)"
                      r"|resource[_ ]exhausted|usage limit|rate.?limit|limit (reached|exceeded)|exceeded (your|the)"
                      r"|no (remaining )?credits|insufficient (credits|quota)|\b429\b)")
SIGNIN_RE = re.compile(r"(?i)(sign[ -]?in|log[ -]?in|logged in|authenticat|oauth|unauthori[sz]ed|accounts\.google)")
SIGNED_OUT_LINE = "Not signed in: Owner signs in with agy"
TOOL_SCRIPTS = {"groq": "groq_subagent.py", "gemini_studio": "gemini_subagent.py"}
USAGE_POOLS = {"claude_code": ("claude_5h", "claude_week"), "codex": ("codex_5h", "codex_week"),
               "antigravity_manual": ("antigravity_week",), "agy_cli": ("antigravity_week",)}


def default_port_open(port, host="127.0.0.1", timeout=0.3):
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def default_bridge_ok(port, host="127.0.0.1", timeout=1.5):
    """``(ok, detail)``: does the local bridge answer HTTP? 5xx or no answer = down; 2xx-4xx = up
    (a 404 on /v1/models still proves the bridge is serving)."""
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/v1/models", timeout=timeout) as r:
            return True, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return (e.code < 500), f"HTTP {e.code}"
    except Exception as e:  # timeout, reset, refused
        return False, type(e).__name__


def reset_date(value):
    """``Mon 09 Nov`` from an ISO reset time, else None."""
    try:
        from .usage import _parse_time
        dt = _parse_time(value)
        return dt.astimezone().strftime("%a %d %b") if dt else None
    except Exception:
        return None


def _drained(rows, route):
    """The usage row (dict) that says ``route`` has no credits left, else None. FRESH or a known-future reset."""
    for r in rows or []:
        if not isinstance(r, dict) or r.get("pool") not in USAGE_POOLS.get(route, ()):
            continue
        rem = r.get("remaining_percent")
        if not (isinstance(rem, (int, float)) and rem <= 0):
            continue
        if r.get("status") == "FRESH" or (r.get("status") == "STALE" and not _reset_passed(r)):
            return r
    return None


def _reset_passed(row):
    """True only when the row's reset time is known and already behind us (its 0% is last period's)."""
    try:
        from datetime import datetime, timezone
        from .usage import _parse_time
        dt = _parse_time(row.get("resets_at"))
        return dt is not None and dt <= datetime.now(timezone.utc)
    except Exception:
        return False


def _known_usage(rows, route):
    """True when a FRESH or STALE usage row with a number exists for one of ``route``'s pools."""
    return any(isinstance(r, dict) and r.get("pool") in USAGE_POOLS.get(route, ())
               and isinstance(r.get("remaining_percent"), (int, float)) and r.get("status") in ("FRESH", "STALE")
               for r in rows or [])


def credits_line(row):
    when = reset_date(row.get("resets_at")) if row else None
    return "Out of weekly credits, resets " + when if when else "Out of weekly credits"


def route_state(route, which=None, usage_rows=None, port_open=None, signed_in=None, tools_dir=None,
                agy_exe=None, bridge_ok=None):
    """``{state, line, ok}`` for one route. state: ready | out_of_credits | usage_unknown | not_installed | bridge_down | signed_out.

    Pure apart from the injected probes (``which``, ``port_open``, ``signed_in``) and the ``usage_rows``
    list, so tests and the Agents payload use the same answer the dispatcher acts on.
    """
    import shutil
    which = which or shutil.which
    if bridge_ok is None and port_open in (None, default_port_open):
        bridge_ok = default_bridge_ok  # live probe only when the port probe is live too (tests inject)
    port_open = port_open or default_port_open

    def out(state, line):
        return {"state": state, "line": line, "ok": state == "ready"}

    if route in ("claude_code", "codex"):
        exe = "claude" if route == "claude_code" else "codex"
        if not which(exe):
            return out("not_installed", f"{exe} CLI not installed")
        if _drained(usage_rows, route):
            return out("out_of_credits", credits_line(_drained(usage_rows, route)))
        return out("ready", "Ready")
    if route == "freebuff":
        port = FREEBUFF_PORT
        if not port_open(port):
            return out("bridge_down", "Bridge down (nothing on port %d)" % port)
        if bridge_ok is not None:
            ok, detail = bridge_ok(port)
            if not ok:
                return out("bridge_down", "Bridge not answering (%s)" % detail)
        return out("ready", "Ready")
    if route in TOOL_SCRIPTS:
        script = Path(tools_dir or providers.TOOLS_DIR) / TOOL_SCRIPTS[route]
        if not script.is_file():
            return out("not_installed", f"{TOOL_SCRIPTS[route]} not found")
        return out("ready", "Ready")
    if route == "copilot":
        return out("ready", "Ready") if which("copilot") else out("not_installed", "GitHub Copilot CLI not installed")
    if route == "deepseek_dsh":
        return out("ready", "Ready") if which("dsh") else out("not_installed", "dsh not installed")
    if route in ("agy_cli", "antigravity_manual"):
        exe = agy_exe or which("agy") or (providers.AGY_EXE if Path(providers.AGY_EXE).is_file() else None)
        if not exe:
            return out("not_installed", "agy CLI not installed")
        if signed_in is not None and not signed_in():
            return out("signed_out", SIGNED_OUT_LINE)
        row = _drained(usage_rows, route)
        if row:
            return out("out_of_credits", credits_line(row))
        if usage_rows is not None and not _known_usage(usage_rows, route):
            # usage probes failed: unknown evidence stays unknown, so never treat it as credits left
            return out("usage_unknown", "Usage unknown, not started")
        return out("ready", "Ready")
    return out("ready", "Manual: Copy Prompt")


def agy_failure_reason(text, code=None, resets=None, timed_out=False, timeout=None):
    """One plain line for why an ``agy`` run failed, from its output. Never multi-line."""
    text = str(text or "")
    if timed_out:
        return "agy timed out after %d min" % max(1, int((timeout or 0) // 60))
    if QUOTA_RE.search(text):
        return credits_line({"resets_at": resets}) if resets else "Out of weekly credits"
    if SIGNIN_RE.search(text):
        return SIGNED_OUT_LINE
    for line in text.splitlines():
        line = line.strip()
        if line:
            return ("agy failed: " + line)[:140]
    return "agy exited with code %s" % code


def toggle_refusal(route, model, toggles=None):
    """Plain-English reason when Agents has ``route`` (or ``model`` on it) switched off, else None.

    ``toggles``: a ``toggles.Snapshot``, a Store, a database path, or None for the live database.
    """
    from . import toggles as T
    try:
        return T.resolve(toggles).refusal(route, model)
    except Exception:  # unreadable switches: only the defaults (Codex off) apply
        return T.Snapshot({}).refusal(route, model)


def is_disabled(route, model, toggles=None):
    """True when Owner switched ``model`` (or the whole ``route``) off.

    ``cc_engine.toggles`` first; then the guarded ``cc_engine.models.is_disabled`` (registry
    aliases). A missing models module disables nothing more.
    """
    if toggle_refusal(route, model, toggles):
        return True
    import importlib
    try:
        mod = importlib.import_module("cc_engine.models")
    except ImportError:
        return False
    fn = getattr(mod, "is_disabled", None)
    if not callable(fn):
        return False
    try:
        return bool(fn(route, model))
    except Exception:  # a broken registry never stops work it cannot judge
        return False


def _node_get(node, key, default=None):
    if isinstance(node, dict):
        return node.get(key, default)
    return getattr(node, key, default)


def _as_check(check):
    if isinstance(check, str):
        try:
            return json.loads(check)
        except ValueError:
            return {"type": "human", "prompt": check}
    return check or {}


# ---------------------------------------------------------------- freebuff

def freebuff_models(path=None):
    """Model ids in the ``freebuff`` provider block of cordis.patch.yml (read-only).

    A small indentation-aware reader (stdlib only). Falls back to
    ``FREEBUFF_FALLBACK_MODELS`` when the file or the block is missing.
    """
    path = Path(path) if path else Path(FREEBUFF_CONFIG)
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return list(FREEBUFF_FALLBACK_MODELS)
    ids, block_indent, models_indent = [], None, None
    for raw in lines:
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if block_indent is None:
            if text == "freebuff:":
                block_indent = indent
            continue
        if indent <= block_indent:
            break  # left the freebuff provider block
        if text == "models:":
            models_indent = indent
            continue
        if models_indent is not None:
            if indent <= models_indent and not text.startswith("-"):
                models_indent = None
                continue
            m = re.match(r"^-\s*id:\s*['\"]?([^'\"#\s]+)", text)
            if m:
                ids.append(m.group(1))
    return ids or list(FREEBUFF_FALLBACK_MODELS)


def freebuff_eligible(node, settings=None):
    """True if Freebuff may take ``node``: text-only work, or file work explicitly allowed."""
    settings = settings or {}
    if settings.get("freebuff_file_work"):
        return True
    check = _as_check(_node_get(node, "check") or _node_get(node, "check_json"))
    if (check or {}).get("type") in FREEBUFF_TEXT_CHECKS:
        return True
    words = " ".join(str(_node_get(node, k) or "") for k in ("brief", "goal", "title", "lane"))
    return bool(re.search(r"\btext[- ]only\b", words.lower()))


# ---------------------------------------------------------------- processes

def pid_alive(pid, started_at=None):
    """True while process ``pid`` is running (stdlib only).

    Windows: OpenProcess + GetExitCodeProcess (STILL_ACTIVE) and, when
    ``started_at`` is given, GetProcessTimes to reject a pid reused by a newer process.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except PermissionError:
            return True
        except OSError:
            return False
        return True
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() == 5  # access denied: it exists but is not ours
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        if code.value != 259:  # STILL_ACTIVE
            return False
        if started_at is not None:
            times = [wintypes.FILETIME() for _ in range(4)]
            if k32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
                ft = times[0]
                created = ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7 - 11644473600
                if created > float(started_at) + 120:
                    return False  # the pid now belongs to a newer process
        return True
    finally:
        k32.CloseHandle(handle)


def kill_tree(pid):
    """Kill ``pid`` and all its children (``taskkill /T /F`` on Windows). Returns exit code."""
    if os.name == "nt":
        return subprocess.run(["taskkill", "/T", "/F", "/PID", str(int(pid))], check=False,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
    import signal
    try:
        os.killpg(os.getpgid(int(pid)), signal.SIGKILL)
        return 0
    except OSError:
        return 1


def build_prompt(node, project=None):
    """Plain-text Copy Prompt packet for one node. The goal is quoted verbatim."""
    project = project or {}
    check = _as_check(_node_get(node, "check") or _node_get(node, "check_json"))
    deps = _node_get(node, "deps")
    if deps is None:
        raw = _node_get(node, "deps_json") or "[]"
        deps = json.loads(raw) if isinstance(raw, str) else raw
    lines = [
        f"# Task {_node_get(node, 'id')}: {_node_get(node, 'title') or ''}".rstrip(),
        "",
    ]
    if project.get("title"):
        lines += [f"Project: {project.get('title')}"]
    if project.get("brief"):
        lines += [f"Brief: {project.get('brief')}"]
    cwd = _node_get(node, "cwd") or project.get("cwd")
    if cwd:
        lines += [f"Working folder: {cwd}"]
    lines += [
        "",
        "## Goal (fixed; do not change it)",
        str(_node_get(node, "goal") or ""),
        "",
        "## Done check (written before the work; only this check or Owner marks it done)",
        json.dumps(check, indent=1, sort_keys=True),
        "",
        f"Dependencies already done: {', '.join(deps) if deps else 'none'}",
        "",
        "## Rules",
        "- Do only this task. Write only the files it needs.",
        "- No paid API routes. Do not commit unless the brief says so.",
        "- Finish with a short plain-English summary of what changed and how the check is met.",
    ]
    return "\n".join(lines) + "\n"


EARLIER_TAIL_CHARS = 2500
OUTPUT_FILES = ("out.txt",)
STOPPED_FILE = "stopped.json"


def mark_stopped(job, reason):
    """Record in the job folder that this attempt stopped before it finished (scheduler call)."""
    if job and Path(job).is_dir():
        _write_json(Path(job) / STOPPED_FILE, {"reason": reason})


def earlier_attempt(job):
    """Prompt section for a node whose last job stopped before it finished, else ''.

    A job stopped early when the scheduler marked it so (``mark_stopped``: a usage limit, 403 or
    crash, whatever the exit code), it has no exit file (lost, cut off by a restart), it was killed
    for running too long, or it exited non-zero. The next
    attempt, on any model or pool, is told where that attempt's output is and to continue from the
    working folder as it stands instead of starting over. A clean exit adds nothing: a failed check
    already travels as the W7 return packet.
    """
    if job is None or not Path(job).is_dir():
        return ""
    job = Path(job)
    ended = _read_json(job / "exit.json")
    killed = (job / "killed.json").exists()
    stopped = _read_json(job / STOPPED_FILE)
    if (isinstance(ended, dict) and int(ended.get("exit_code") or 0) == 0 and not killed
            and not stopped):
        return ""
    why = (f"it stopped because {stopped.get('reason')}" if isinstance(stopped, dict) and stopped.get("reason")
           else "it was stopped for running too long" if killed
           else f"it exited with code {ended.get('exit_code')}" if isinstance(ended, dict)
           else "it was cut off before it finished")
    out = next((job / f for f in OUTPUT_FILES if (job / f).exists()), None)
    tail = _tail(out, EARLIER_TAIL_CHARS).strip() if out else ""
    lines = ["## Continue from an earlier attempt (it stopped before it finished)",
             f"Attempt folder: {job} ({why}).",
             "The working folder may already hold part of this change. Look at what is there, keep "
             "what is right and finish the task from it; do not start over or undo good work."]
    if tail:
        lines += ["", f"Last output of that attempt ({out.name}):", "```", tail, "```"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- job wrapper

def env_task(node, cwd):
    """The ``env_policy.choose`` task for a dispatched node."""
    scoped = bool(_node_get(node, "scope")) or (_node_get(node, "risk") or "contained") == "contained"
    return {"repo": cwd, "read_only": bool(_node_get(node, "read_only")), "bounded": scoped,
            "needs_conversation": False}


def _write_json(path, data):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def child_env(base=None):
    """The job's environment without metered API keys or base URLs."""
    env = dict(os.environ if base is None else base)
    for k in METERED_ENV:
        env.pop(k, None)
    return env


def default_exec_runner(argv, cwd, stdin_text, stdout_path, stderr_path):
    with open(stdout_path, "w", encoding="utf-8") as out, open(stderr_path, "w", encoding="utf-8") as err:
        proc = subprocess.run(argv, cwd=cwd or None, input=stdin_text, stdout=out, stderr=err,
                              text=True, encoding="utf-8", env=child_env())
    return proc.returncode


def agy_exec_runner(argv, cwd, stdin_text, stdout_path, stderr_path, timeout=3600):
    """Run ``agy`` with a closed stdin and a hard time limit; 124 on timeout (process tree killed)."""
    with open(stdout_path, "w", encoding="utf-8") as out, open(stderr_path, "w", encoding="utf-8") as err:
        proc = subprocess.Popen(list(argv), cwd=cwd or None, stdin=subprocess.PIPE, stdout=out, stderr=err,
                                text=True, encoding="utf-8", env=child_env(),
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            proc.communicate(stdin_text or "", timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_tree(proc.pid)
            try:
                proc.communicate(timeout=10)
            except Exception:
                pass
            return 124
    return proc.returncode


def _agy_resets():
    try:
        from . import usage
        for r in usage.read_usage():
            if r.get("pool") == "antigravity_week":
                return r.get("resets_at")
    except Exception:
        pass
    return None


def default_http_post(url, body, timeout=3600):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # loopback only
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # 403 / 429 / 5xx: recorded, classified transient
        try:
            payload = json.loads(exc.read().decode("utf-8", "replace"))
        except ValueError:
            payload = {"error": str(exc)}
        return exc.code, payload


def run_job(spec_path, exec_runner=None, http_post=None, clock=time.time, heartbeat=True):
    """Execute one job spec (inside the detached wrapper). Writes exit.json."""
    spec_path = Path(spec_path)
    job = spec_path.parent
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    is_agy = spec.get("route") == "agy_cli"
    if exec_runner is None:
        exec_runner = (functools.partial(agy_exec_runner, timeout=spec.get("timeout_seconds") or 3600)
                       if is_agy else default_exec_runner)
    if http_post is None:
        http_post = functools.partial(default_http_post,
                                      timeout=spec.get("timeout_seconds") or 3600)
    hb = job / "heartbeat"
    state = {"at": clock(), "active": 0.0}
    hb.write_text(json.dumps(state), encoding="utf-8")
    stop = threading.Event()
    if heartbeat:
        def beat():
            # ``active`` counts run time only: a gap longer than two beats (sleep) adds at
            # most two beats, so a suspended machine never times a job out.
            while not stop.wait(HEARTBEAT_SECONDS):
                t = clock()
                state["active"] += max(0.0, min(t - state["at"], 2 * HEARTBEAT_SECONDS))
                state["at"] = t
                try:
                    hb.write_text(json.dumps(state), encoding="utf-8")
                except OSError:
                    pass
        threading.Thread(target=beat, daemon=True).start()
    started = clock()
    result = {"started_at": started, "route": spec.get("route"), "attempt": spec.get("attempt"),
              "pid": os.getpid()}
    if spec.get("env"):
        result["env"] = spec["env"]
    for key in spec.get("env_drop") or []:  # the wrapper process runs this one job only
        os.environ.pop(str(key), None)
    try:
        if spec["kind"] == "exec":
            stdin_text = None
            if spec.get("stdin_file"):
                stdin_text = Path(spec["stdin_file"]).read_text(encoding="utf-8")
            code = exec_runner(spec["argv"], spec.get("cwd"), stdin_text,
                               str(job / "out.txt"), str(job / "err.txt"))
            result["exit_code"] = int(code)
            if is_agy and int(code) != 0:  # a clean one-line reason, never a hang or a traceback
                text = ""
                for name in ("err.txt", "out.txt"):
                    try:
                        text += (job / name).read_text(encoding="utf-8", errors="replace") + "\n"
                    except OSError:
                        pass
                reason = agy_failure_reason(text, int(code), _agy_resets(), timed_out=int(code) == 124,
                                            timeout=spec.get("timeout_seconds"))
                result["reason"] = result["error"] = reason
        elif spec["kind"] == "http":
            status, payload = http_post(spec["url"], spec["body"])
            text = ""
            try:
                text = payload["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                text = json.dumps(payload)
            (job / "out.txt").write_text(text, encoding="utf-8")
            result["http_status"] = status
            result["exit_code"] = 0 if 200 <= int(status) < 300 else 1
        else:
            raise ValueError(f"unknown job kind {spec['kind']!r}")
    except Exception as exc:  # recorded, never raised past the wrapper
        result["exit_code"] = 1
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        stop.set()
    result["ended_at"] = clock()
    _write_json(job / "exit.json", result)
    return result


def default_launcher(spec_path):
    """Start the job wrapper detached so it survives the tick process."""
    engine_dir = Path(__file__).resolve().parents[1]
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008  # DETACHED_PROCESS
        flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen([sys.executable, "-m", "cc_engine.dispatch", "job", str(spec_path)],
                            cwd=str(engine_dir), creationflags=flags, close_fds=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, env=child_env())
    return {"pid": proc.pid}


def _tail(path, limit=4000):
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-limit:]


# ---------------------------------------------------------------- dispatcher

class Dispatcher:
    """``route`` picks a route; ``dispatch`` starts it; ``poll`` reports job state."""

    def __init__(self, data_dir=None, launcher=None, asker=None, clock=time.time,
                 sleep=time.sleep, claude_exe=None, powershell_exe=None,
                 freebuff_url=FREEBUFF_URL,
                 pid_alive=None, killer=None,
                 freebuff_config=None, codex_exe=None, toggles_db=None, which=None,
                 usage_reader=None, port_open=None, agy_signed_in=None, agy_exe=None,
                 tools_dir=None, copilot_exe=None, dsh_exe=None):
        self.data_dir = Path(data_dir) if data_dir else config.data_dir()
        self.codex_exe = codex_exe
        self.which = which
        self.usage_reader = usage_reader
        self.port_open = port_open
        self.agy_signed_in = agy_signed_in
        self.agy_exe = agy_exe
        self.tools_dir = tools_dir
        self.copilot_exe = copilot_exe
        self.dsh_exe = dsh_exe
        # The engine database holding the Agents switches. The scheduler points this at its own
        # store each tick; otherwise ``<data_dir>/cc.sqlite`` (the live database by default).
        if toggles_db is None:
            if data_dir:
                toggles_db = self.data_dir / "cc.sqlite"
            else:
                from . import default_db_path
                toggles_db = default_db_path()
        self.toggles_db = toggles_db
        self.launcher = launcher or default_launcher
        self.asker = asker
        self.clock = clock
        self.sleep = sleep
        self.claude_exe = claude_exe
        self.powershell_exe = powershell_exe
        self.freebuff_url = freebuff_url
        self.pid_alive = pid_alive or globals()["pid_alive"]
        self.killer = killer or kill_tree
        self.freebuff_config = freebuff_config

    # -- routing
    def route(self, node, settings=None):
        settings = settings or {}
        pool = _node_get(node, "pool") or settings.get("default_pool")
        return route_for_pool(pool)

    def refusal(self, node, settings=None):
        """Plain-English reason ``node`` may not be dispatched as it stands, else None."""
        route = self.route(node, settings)
        model = _node_get(node, "model")
        reason = toggle_refusal(route, model, self.toggles_db)
        if reason:
            return reason
        if is_disabled(route, model, toggles=self.toggles_db):
            what = model or route.replace("_", " ")
            return f"{what} is turned off in Models"
        return None

    def freebuff_models(self):
        return freebuff_models(self.freebuff_config)

    def _usage_rows(self):
        try:
            if self.usage_reader is not None:
                return list(self.usage_reader() or [])
            from . import usage
            return usage.read_usage()
        except Exception:
            return []

    def _signed_in(self):
        if self.agy_signed_in is not None:
            return self.agy_signed_in
        from . import usage
        return usage.agy_signed_in

    def state(self, route):
        """``route_state`` with this dispatcher's probes (the Agents tab shows the same answer)."""
        return route_state(route, which=self.which, usage_rows=self._usage_rows(), port_open=self.port_open,
                           signed_in=self._signed_in(), tools_dir=self.tools_dir, agy_exe=self.agy_exe)

    def unavailable(self, route):
        """One-line reason a v3 CLI route cannot run right now (not installed, no credits, signed out), else None.

        Checked before anything starts, so ``agy`` is never launched when it would open a browser
        sign-in or fail on empty credits, and an absent ``copilot`` is simply not dispatchable.
        """
        if route not in CLI_ROUTES:
            return None
        st = self.state(route)
        return None if st["ok"] else st["line"]

    # -- paths
    def node_dir(self, project_id, node_id):
        return self.data_dir / "jobs" / str(project_id) / str(node_id)

    def latest_job(self, project_id, node_id):
        base = self.node_dir(project_id, node_id)
        if not base.is_dir():
            return None
        nums = [int(p.name) for p in base.iterdir() if p.is_dir() and p.name.isdigit()]
        return base / str(max(nums)) if nums else None

    def _new_job_dir(self, project_id, node_id):
        latest = self.latest_job(project_id, node_id)
        n = int(latest.name) + 1 if latest else 1
        job = self.node_dir(project_id, node_id) / str(n)
        job.mkdir(parents=True, exist_ok=False)
        return job

    # -- command builders (pure; exposed for tests and the inspector), all via cc_engine.providers
    def claude_argv(self, node, settings=None):
        """``claude -p``; a worker may edit files in its cwd (``acceptEdits``) unless the run's
        settings give their own ``claude_extra_args`` (the planner passes read-only ones)."""
        extra = (settings or {}).get("claude_extra_args")
        if extra is None:
            extra = CLAUDE_WORKER_ARGS
        launch = providers.claude_code(model=_node_get(node, "model") or "sonnet",
                                       effort=_node_get(node, "effort"), output_format="json",
                                       extra_args=extra, exe=self.claude_exe)
        return list(launch.argv)

    def codex_argv(self, node, settings=None, cwd=None):
        """``codex exec --json`` for one task; the prompt goes on stdin (``-``)."""
        import shutil
        exe = self.codex_exe or shutil.which("codex") or "codex"
        argv = [exe, "exec", "--json", "--skip-git-repo-check", "--sandbox", CODEX_SANDBOX]
        if cwd:
            argv += ["-C", str(cwd)]
        model = _node_get(node, "model")
        if model:
            argv += ["-m", str(model)]
        effort = _node_get(node, "effort")
        if effort:
            argv += ["-c", f"model_reasoning_effort={effort}"]
        argv += [str(a) for a in (settings or {}).get("codex_extra_args") or []]
        return argv + ["-"]

    def cli_argv(self, route, node, brief):
        """Command for a v3 CLI route; the full brief is the file the short prompt names."""
        short = (f"Complete task {_node_get(node, 'id')} exactly as described in the file {brief}. "
                 f"Do only that task, then give a short summary.")
        model = _node_get(node, "model")
        if route == "agy_cli":
            return list(providers.agy_cli(short, model=model, exe=self.agy_exe).argv)
        if route == "copilot":
            return list(providers.copilot(short, model=model, exe=self.copilot_exe).argv)
        if route == "deepseek_dsh":
            return list(providers.deepseek_dsh(short, exe=self.dsh_exe).argv)
        return list(getattr(providers, route)(short, file_path=brief, model=model,
                                              tools_dir=self.tools_dir).argv)

    def freebuff_body(self, node, prompt):
        return providers.freebuff(prompt=prompt, model=_node_get(node, "model"),
                                  models=self.freebuff_models(), url=self.freebuff_url).body

    @staticmethod
    def timeout_seconds(settings=None):
        settings = settings or {}
        try:
            minutes = float(settings.get("job_timeout_minutes") or DEFAULT_JOB_TIMEOUT_MINUTES)
        except (TypeError, ValueError):
            minutes = DEFAULT_JOB_TIMEOUT_MINUTES
        return max(60.0, minutes * 60)

    # -- dispatch
    def dispatch(self, node, settings=None, project=None):
        """Start ``node``. Returns ``{route, status, job?, packet?, question_id?, summary}``.

        ``status`` is ``running`` for automated routes, ``needs_you`` for Gemini App /
        Antigravity / manual routes and ``refused`` for a model turned off in Models.
        """
        project = project or {}
        project_id = project.get("id") or _node_get(node, "project_id")
        node_id = _node_get(node, "id")
        # W1: a code node runs its script and never a model, so model switches do not apply to it.
        if node_kinds.is_code(node, self.toggles_db):
            return node_kinds.dispatch_code(self, node, project, settings)
        route = self.route(node, settings)
        refused = self.refusal(node, settings)
        if refused:
            return {"route": route, "status": "refused", "reason": refused,
                    "summary": f"Task {node_id} was not started because {refused}."}
        down = self.unavailable(route)
        if down:
            return {"route": route, "status": "refused", "reason": down, "unavailable": True,
                    "summary": f"Task {node_id} was not started: {down}."}
        prompt = build_prompt(node, project)
        # W7: this node's own return packet goes back verbatim; a sibling's output never does.
        from . import loops  # lazy: loops imports scheduler, which imports this module
        pk = self._packet(project_id, node_id)
        if pk and pk.get("unit") in (None, node_id):
            prompt = loops.rework_prompt(prompt, pk)
        earlier = earlier_attempt(self.latest_job(project_id, node_id))
        if earlier:  # resume from the partial work, whatever model or pool picks it up now
            prompt = prompt.rstrip() + "\n\n" + earlier
        cwd = _node_get(node, "cwd") or project.get("cwd")
        job = self._new_job_dir(project_id, node_id)
        brief = job / "prompt.md"
        brief.write_text(prompt, encoding="utf-8")
        where = self._record_env(job, node, project_id, node_id, cwd)

        if route not in AUTOMATED_ROUTES:
            adapter = route if route in MANUAL_ROUTES else "manual"
            label = getattr(providers, adapter)().label  # module attribute, so callers can wrap it
            check = _as_check(_node_get(node, "check") or _node_get(node, "check_json"))
            _write_json(job / "packet.json", {
                "route": route, "label": label, "node": node_id, "project": project_id,
                "prompt_path": str(brief), "prompt": prompt, "cwd": cwd, "check": check,
                "model": _node_get(node, "model"), "effort": _node_get(node, "effort"),
                "created_at": self.clock()})
            if route == "antigravity_manual":
                ask = (f"Task {node_id} needs an Antigravity run: open Antigravity in {cwd or 'the project folder'}, "
                       f"paste the prompt packet at {brief}, let it make the change and run the done-check, "
                       f"then paste the result back.")
            else:
                ask = (f"Task {node_id} needs a {label} run: copy the prompt packet at "
                       f"{brief} into {label} and paste the result back.")
            question_id = None
            if self.asker is not None:
                question_id = self.asker(
                    project_id=project_id, node_id=node_id, kind="text",
                    purpose="manual_result", prompt=ask, options=None)
            return {"route": route, "status": "needs_you", "job": str(job), "env": where,
                    "packet": str(brief), "packet_json": str(job / "packet.json"),
                    "question_id": question_id,
                    "summary": f"Task {node_id} is ready for a {label} run; its Copy Prompt packet is waiting for you."}

        if route == "claude_code":
            spec = {"kind": "exec", "route": route, "argv": self.claude_argv(node, settings),
                    "cwd": cwd, "stdin_file": str(brief)}
        elif route == "codex":
            spec = {"kind": "exec", "route": route, "argv": self.codex_argv(node, settings, cwd),
                    "cwd": cwd, "stdin_file": str(brief), "env_drop": list(CODEX_DROP_ENV)}
        elif route in CLI_ROUTES:
            spec = {"kind": "exec", "route": route, "argv": self.cli_argv(route, node, brief),
                    "cwd": cwd, "stdin_file": None}
        else:  # freebuff
            spec = {"kind": "http", "route": route, "url": self.freebuff_url,
                    "body": self.freebuff_body(node, prompt)}
        timeout = self.timeout_seconds(settings)
        spec["node"] = node_id
        spec["project"] = project_id
        spec["attempt"] = _node_get(node, "attempts")
        spec["timeout_seconds"] = timeout
        spec["created_at"] = self.clock()
        spec["env"] = where
        spec_path = job / "spec.json"
        _write_json(spec_path, spec)
        (job / "heartbeat").write_text(json.dumps({"at": self.clock(), "active": 0.0}),
                                       encoding="utf-8")
        started = self.clock()
        info = self.launcher(spec_path) or {}
        _write_json(job / "launch.json", {"pid": info.get("pid"), "started_at": started,
                                          "timeout_seconds": timeout, "route": route,
                                          "attempt": spec["attempt"]})
        return {"route": route, "status": "running", "job": str(job), "launch": info,
                "pid": info.get("pid"), "env": where,
                "summary": f"Task {node_id} started on {route.replace('_', ' ')}."}

    # -- W7 packet and T2 environment
    def _with_store(self, fn, default=None):
        """Run ``fn(store)`` on the Store handed in, or on a short-lived one for a database path."""
        db = self.toggles_db
        if db is None:
            return default
        try:
            if hasattr(db, "get_meta"):
                return fn(db)
            if not Path(db).exists():
                return default
            from .store import Store
            store = Store(db)
            try:
                return fn(store)
            finally:
                store.close()
        except Exception:
            return default

    def _packet(self, project_id, node_id):
        if not project_id or not node_id:
            return None
        from . import loops
        return self._with_store(lambda st: loops.latest_packet(st, project_id, node_id))

    def _record_env(self, job, node, project_id, node_id, cwd):
        """T2: ask ``env_policy.choose`` where this job runs; write it to the job dir and an event."""
        task = env_task(node, cwd)

        def run(store):
            claims = store.claims(project_id) if project_id else {}
            claims = {k: v for k, v in claims.items() if k != node_id}
            return env_policy.choose(task, {"writers": env_policy.writers_from_claims(claims, cwd)})

        choice = self._with_store(run) or env_policy.choose(task, {})
        where = {"env": choice["env"], "workdir": choice["workdir"], "why": choice["why"]}
        _write_json(job / "meta.json", {"node": node_id, "project": project_id, "cwd": cwd, "where": where,
                                        "created_at": self.clock()})
        line = f"Task {node_id} runs on {where['env'].replace('_', ' ')}: {where['why']}"
        self._with_store(lambda st: st.event(project_id, node_id, "env", "dispatcher", line, where))
        return where

    # -- polling
    def _active_seconds(self, job, launch, now):
        hb = _read_json(job / "heartbeat")
        if isinstance(hb, dict) and "active" in hb:
            gap = max(0.0, min(now - float(hb.get("at") or now), 2 * HEARTBEAT_SECONDS))
            return float(hb["active"]) + gap
        return now - float(launch.get("started_at") or now)

    def poll(self, project_id, node_id, now=None):
        """``{'state': 'running'|'exited'|'lost'|'timeout'|'manual'|'none', ...}`` for the latest job.

        ``lost`` means the recorded pid is no longer alive and no exit file exists;
        ``timeout`` means the job ran past its timeout and its process tree was killed.
        """
        now = self.clock() if now is None else now
        job = self.latest_job(project_id, node_id)
        if job is None:
            return {"state": "none"}
        exit_file = job / "exit.json"
        if exit_file.exists():
            data = _read_json(exit_file) or {"exit_code": 1, "error": "unreadable exit.json"}
            data["state"] = "exited"
            data["job"] = str(job)
            out = job / "out.txt"
            data["output_path"] = str(out) if out.exists() else None
            data["stderr_tail"] = _tail(job / "err.txt")
            data["output_tail"] = _tail(out) if out.exists() else ""
            return data
        if not (job / "spec.json").exists():
            return {"state": "manual", "job": str(job)}
        killed = _read_json(job / "killed.json")
        if killed:
            return dict(killed, state=killed.get("state", "timeout"), job=str(job))
        launch = _read_json(job / "launch.json") or {}
        spec = _read_json(job / "spec.json") or {}
        pid = launch.get("pid")
        started = launch.get("started_at") or spec.get("created_at") or now
        timeout = float(launch.get("timeout_seconds") or spec.get("timeout_seconds")
                        or LOST_AFTER_SECONDS)
        base = {"job": str(job), "pid": pid, "started_at": started,
                "attempt": launch.get("attempt", spec.get("attempt"))}
        if pid is None:
            # No pid recorded (legacy job): never judged by heartbeat age, only by timeout.
            if now - float(started) > timeout:
                return dict(base, state="lost")
            return dict(base, state="running")
        if not self.pid_alive(pid, started):
            if exit_file.exists():  # it finished between the two checks
                return self.poll(project_id, node_id, now)
            return dict(base, state="lost")
        active = self._active_seconds(job, launch, now)
        if active > timeout:
            self.killer(pid)
            info = {"state": "timeout", "killed_at": now, "active_seconds": active,
                    "reason": f"it ran for more than {int(timeout // 60)} minutes"}
            _write_json(job / "killed.json", info)
            return dict(base, **info)
        return dict(base, state="running", active_seconds=active)

    def kill_job(self, project_id, node_id, reason="cancelled", now=None):
        """Kill the latest job's process tree if it is alive. Returns True if one was killed."""
        now = self.clock() if now is None else now
        job = self.latest_job(project_id, node_id)
        if job is None or (job / "exit.json").exists():
            return False
        launch = _read_json(job / "launch.json") or {}
        pid = launch.get("pid")
        alive = pid is not None and self.pid_alive(pid, launch.get("started_at"))
        if alive:
            self.killer(pid)
        if (job / "spec.json").exists():
            _write_json(job / "killed.json", {"state": "cancelled", "killed_at": now,
                                              "reason": reason})
        return alive

    def live_jobs(self):
        """Every job under ``data/jobs`` whose wrapper pid is still alive."""
        out = []
        base = self.data_dir / "jobs"
        if not base.is_dir():
            return out
        for launch_file in base.glob("*/*/*/launch.json"):
            job = launch_file.parent
            if (job / "exit.json").exists() or (job / "killed.json").exists():
                continue
            launch = _read_json(launch_file) or {}
            pid = launch.get("pid")
            if pid is not None and self.pid_alive(pid, launch.get("started_at")):
                out.append({"project": job.parent.parent.name, "node": job.parent.name,
                            "job": str(job), "pid": pid})
        return out


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) == 2 and argv[0] == "job":
        result = run_job(argv[1])
        return 0 if result.get("exit_code") == 0 else 1
    print("usage: python -m cc_engine.dispatch job <spec.json>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
