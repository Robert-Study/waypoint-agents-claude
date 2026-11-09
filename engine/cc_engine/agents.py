"""Agents: chat with Owner's coding agents through their own CLIs or local bridges.

Owner talks to each agent directly from the Agents page (owner only). Each send runs one
non-interactive turn and streams it to the browser as SSE ``agent`` events
(``{agent, session_id, turn_id, delta|tool|done|error}``).

Routes (all zero extra paid spend; metered API keys are removed from every child env):

- ``claude_code``: ``claude -p --output-format stream-json --verbose --include-partial-messages``
  with ``--session-id <uuid>`` for a new chat and ``--resume <id>`` after; prompt on stdin.
- ``codex``: ``codex exec --json --skip-git-repo-check -`` / ``codex exec resume --json
  --skip-git-repo-check <thread_id> -``. Codex is a separate, explicit owner invocation:
  every send needs ``confirm: true`` after the page has shown current Codex usage.
- ``freebuff``: OpenAI-compatible streaming POST to ``127.0.0.1:1455/v1/chat/completions``
  with the session's earlier messages as history.
- ``antigravity``: ``agy -p <message> --output-format stream-json [--conversation <id>]``.

A send is refused when a pool the agent draws on is known to be at or below
``USAGE_FLOOR_PERCENT``. Unknown usage stays unknown and is shown, never invented.

Sessions live in ``agent_sessions`` and messages in ``agent_messages`` inside data/cc.sqlite
(created here; never bump the plan revision). Every side effect is injectable through
``Runtime`` (``which``, ``port_open``, ``popen``, ``http_stream``, ``killer``, ``usage_reader``,
``clock``, ``sleep``); tests use fakes only and never call a real model.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import dispatch
from . import config
from .pick import floor_for

REPO = Path(__file__).resolve().parents[2]
DRIVE_ROOT = Path(config.DRIVE_ROOT)
FOLDERS = (
    {"id": "drive", "label": "Drive", "path": str(DRIVE_ROOT)},
    {"id": "dsh-control-centre", "label": "dsh-control-centre", "path": str(REPO)},
    # The optional second repo (config key ``extra_repo``); id and label are its folder name.
    {"id": Path(config.EXTRA_REPO).name, "label": Path(config.EXTRA_REPO).name, "path": config.EXTRA_REPO},
)
# A folder that does not exist on this machine is not offered (a fresh install has no second repo); the
# first two (drive and this repo) always stay. Owner's install has all three, so nothing changes there.
FOLDERS = FOLDERS[:2] + tuple(f for f in FOLDERS[2:] if Path(f["path"]).exists())

FREEBUFF_URL = dispatch.FREEBUFF_URL
FREEBUFF_PORT = 1455
USAGE_FLOOR_PERCENT = 0.0  # note 146: unlisted pools have a 0% floor
HISTORY_MESSAGES = 30
HISTORY_CHARS = 60000
MAX_MESSAGE_CHARS = 30000
CLAUDE_EXTRA_ARGS: tuple = ()  # e.g. ("--permission-mode", "acceptEdits") if Owner wants edits

# Environment variables that would turn a subscription route into metered API spend.
METERED_ENV = dispatch.METERED_ENV + (
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE", "CODEX_API_KEY",
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI")

AGENTS = {
    "claude_code": {"name": "Claude Code", "glyph": "claude", "route": "claude CLI", "exe": "claude",
                    "pools": ("claude_5h", "claude_week"), "note": "Max subscription"},
    "codex": {"name": "Codex", "glyph": "codex", "route": "codex CLI", "exe": "codex",
              "pools": ("codex_5h", "codex_week"), "confirm": True,
              "note": "Plus subscription; confirm usage before each send"},
    "freebuff": {"name": "Freebuff", "glyph": "deepseek", "route": "local proxy", "port": FREEBUFF_PORT,
                 "pool_prefixes": ("freebuff",), "note": "DeepSeek V4.1 Flash via Freebuff, free; text only"},
    "antigravity": {"name": "Antigravity", "glyph": "gemini", "route": "agy CLI", "exe": "agy",
                    "pool_prefixes": ("antigravity_",), "note": "Antigravity subscription"},
}
AGENT_ORDER = ("claude_code", "codex", "freebuff", "antigravity")
# Effort levels each agent's CLI accepts (claude --effort; codex -c model_reasoning_effort=). Others have none.
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
CODEX_EFFORTS = ("low", "medium", "high", "xhigh")
AGENT_EFFORTS = {"claude_code": CLAUDE_EFFORTS, "codex": CODEX_EFFORTS}

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS agent_sessions(id TEXT PRIMARY KEY, agent TEXT NOT NULL, cwd TEXT,"
    " title TEXT, created_at TEXT, last_at TEXT, ext_id TEXT)",
    "CREATE INDEX IF NOT EXISTS agent_sessions_agent ON agent_sessions(agent, last_at)",
    "CREATE TABLE IF NOT EXISTS agent_messages(seq INTEGER PRIMARY KEY, session_id TEXT NOT NULL,"
    " turn_id TEXT, role TEXT, text TEXT, at TEXT)",
    "CREATE INDEX IF NOT EXISTS agent_messages_session ON agent_messages(session_id, seq)",
)
UUID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _http_error(status, body):
    from .api import HttpError  # lazy: api imports this module
    return HttpError(status, body)


def child_env(base=None, drop=()):
    env = dict(os.environ if base is None else base)
    for k in METERED_ENV + tuple(drop):
        env.pop(k, None)
    return env


def _short(text, limit=140):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------- parsers
class ClaudeParser:
    """``claude -p --output-format stream-json --verbose --include-partial-messages`` lines.

    ``feed(line)`` returns events: ``("delta", text)``, ``("tool", line)``, ``("ext_id", id)``,
    ``("final", text)`` (result text, used only if nothing streamed) and ``("error", msg)``.
    """

    def __init__(self):
        self.partial = False      # text of the current message already arrived as deltas
        self.had_text = False
        self.pending_break = False

    def _text(self, text):
        if not text:
            return []
        if self.pending_break:
            text, self.pending_break = "\n\n" + text, False
        self.had_text = True
        return [("delta", text)]

    def feed(self, line):
        line = (line or "").strip()
        if not line:
            return []
        try:
            ev = json.loads(line)
        except ValueError:
            return self.feed_other(line)
        if not isinstance(ev, dict):
            return []
        return self.feed_event(ev)

    def feed_other(self, line):
        return []

    def feed_event(self, ev):
        t = ev.get("type")
        out = []
        if t == "system":
            if ev.get("session_id"):
                out.append(("ext_id", ev["session_id"]))
        elif t == "stream_event":
            e = ev.get("event") or {}
            et = e.get("type")
            if et == "message_start":
                self.partial = False
                self.pending_break = self.had_text
            elif et == "content_block_delta":
                d = e.get("delta") or {}
                if d.get("type") == "text_delta" and d.get("text"):
                    self.partial = True
                    out += self._text(d["text"])
        elif t == "assistant":
            for block in ((ev.get("message") or {}).get("content") or []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and not self.partial:
                    out += self._text(block.get("text"))
                elif block.get("type") == "tool_use":
                    out.append(("tool", describe_tool(block.get("name"), block.get("input"))))
        elif t == "result":
            if ev.get("session_id"):
                out.append(("ext_id", ev["session_id"]))
            if ev.get("is_error") or (ev.get("subtype") not in (None, "success")):
                out.append(("error", _short(ev.get("result") or ev.get("subtype") or "the turn failed", 600)))
            elif ev.get("result"):
                out.append(("final", ev["result"]))
        return out


def describe_tool(name, args):
    """One muted line for a tool call, e.g. ``Read engine/cc_engine/api.py``."""
    name = str(name or "tool")
    args = args if isinstance(args, dict) else {}
    for key in ("command", "file_path", "path", "pattern", "url", "query", "description", "prompt"):
        if args.get(key):
            return _short(f"{name} {args[key]}")
    return name


class CodexParser:
    """``codex exec --json`` JSONL events (current ``thread.*``/``item.*`` and legacy ``msg``)."""

    def __init__(self):
        self.seen = set()
        self.had_text = False

    def _text(self, text):
        if not text:
            return []
        if self.had_text:
            text = "\n\n" + text
        self.had_text = True
        return [("delta", text)]

    def feed(self, line):
        line = (line or "").strip()
        if not line.startswith("{"):
            return []
        try:
            ev = json.loads(line)
        except ValueError:
            return []
        if not isinstance(ev, dict):
            return []
        t = ev.get("type")
        if t == "thread.started" and ev.get("thread_id"):
            return [("ext_id", ev["thread_id"])]
        if t in ("item.started", "item.completed", "item.updated"):
            return self._item(t, ev.get("item") or {})
        if t == "turn.failed":
            err = ev.get("error") or {}
            return [("error", _short(err.get("message") if isinstance(err, dict) else err, 600))]
        if t == "error":  # transient (e.g. reconnecting); a real failure ends in turn.failed / exit code
            return [("tool", _short("Codex: " + str(ev.get("message") or "error")))]
        msg = ev.get("msg")
        if isinstance(msg, dict):  # legacy event stream
            mt = msg.get("type")
            if mt == "session_configured" and msg.get("session_id"):
                return [("ext_id", msg["session_id"])]
            if mt == "agent_message":
                return self._text(msg.get("message"))
            if mt == "exec_command_begin":
                cmd = msg.get("command")
                return [("tool", _short("Ran " + (" ".join(cmd) if isinstance(cmd, list) else str(cmd))))]
            if mt == "error":
                return [("error", _short(msg.get("message"), 600))]
        return []

    def _item(self, t, item):
        it = item.get("type") or item.get("item_type")
        iid = item.get("id")
        if t == "item.updated":
            return []
        if it in ("agent_message", "assistant_message"):
            return self._text(item.get("text")) if t == "item.completed" else []
        if it == "reasoning" or it == "todo_list":
            return []
        if iid is not None and iid in self.seen:
            return []
        if iid is not None:
            self.seen.add(iid)
        if it == "command_execution":
            return [("tool", _short("Ran " + str(item.get("command") or "")))]
        if it == "file_change":
            paths = [c.get("path") for c in (item.get("changes") or []) if isinstance(c, dict)]
            return [("tool", _short("Edited " + ", ".join(p for p in paths if p)))]
        if it == "mcp_tool_call":
            return [("tool", _short(f"{item.get('server') or 'mcp'}.{item.get('tool') or 'tool'}"))]
        if it == "web_search":
            return [("tool", _short("Searched " + str(item.get("query") or "")))]
        if it == "error":
            return [("tool", _short("Codex: " + str(item.get("message") or "error")))]
        return [("tool", _short(str(it or "step")))] if it else []


class AgyParser(ClaudeParser):
    """``agy -p --output-format stream-json``: Claude-shaped events when present, else lenient."""

    ID_KEYS = ("session_id", "sessionId", "conversation_id", "conversationId")

    def feed_other(self, line):  # plain text output
        return self._text(line + "\n")

    def feed_event(self, ev):
        out = []
        for k in self.ID_KEYS:
            if isinstance(ev.get(k), str) and ev.get(k):
                out.append(("ext_id", ev[k]))
                break
        t = str(ev.get("type") or "")
        if t in ("system", "stream_event", "assistant", "result"):
            known = super().feed_event(ev)
            return out + [e for e in known if e[0] != "ext_id"]
        if "tool" in t:
            return out + [("tool", describe_tool(ev.get("name") or ev.get("tool_name") or ev.get("tool"),
                                                 ev.get("input") or ev.get("args")))]
        if t == "error":
            return out + [("error", _short(ev.get("message") or ev.get("error") or "error", 600))]
        text = ev.get("text")
        d = ev.get("delta")
        if text is None and isinstance(d, str):
            text = d
        elif text is None and isinstance(d, dict):
            text = d.get("text")
        if text is None and isinstance(ev.get("content"), str):
            text = ev["content"]
        if isinstance(text, str):
            out += self._text(text)
        return out


# ---------------------------------------------------------------- default side effects
class SubprocessProc:
    """A child whose stdout is read line by line; stderr is kept as a short tail."""

    def __init__(self, argv, cwd, env, stdin_text):
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.p = subprocess.Popen(argv, cwd=cwd or None, env=env, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                  encoding="utf-8", errors="replace", bufsize=1, creationflags=flags)
        self.pid = self.p.pid
        self._err = deque(maxlen=40)
        threading.Thread(target=self._drain, daemon=True).start()
        try:
            if stdin_text is not None:
                self.p.stdin.write(stdin_text)
            self.p.stdin.close()
        except OSError:
            pass
        self.stdout = self.p.stdout

    def _drain(self):
        try:
            for line in self.p.stderr:
                self._err.append(line.rstrip())
        except (OSError, ValueError):
            pass

    def wait(self):
        return self.p.wait()

    def stderr_tail(self):
        return "\n".join(self._err)


def default_popen(argv, cwd, env, stdin_text):
    return SubprocessProc(argv, cwd, env, stdin_text)


def default_http_stream(url, body, cancel, timeout=900):
    """POST an OpenAI-compatible chat request with ``stream: true``; yield text deltas."""
    data = json.dumps(dict(body, stream=True)).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "text/event-stream"})
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)  # loopback only
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from None
    with resp:
        if "text/event-stream" not in (resp.headers.get("Content-Type") or ""):
            payload = json.loads(resp.read().decode("utf-8", "replace"))
            yield payload["choices"][0]["message"]["content"] or ""
            return
        for raw in resp:
            if cancel.is_set():
                return
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk == "[DONE]":
                return
            try:
                obj = json.loads(chunk)
                text = (obj["choices"][0].get("delta") or {}).get("content")
            except (ValueError, KeyError, IndexError, TypeError):
                continue
            if text:
                yield text


def default_port_open(port, host="127.0.0.1", timeout=0.3):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------- runtime
class Turn:
    def __init__(self, turn_id, agent, session_id):
        self.id = turn_id
        self.agent = agent
        self.session_id = session_id
        self.items = []       # [{"kind": "text"|"tool"|"error", "text": ...}] in arrival order
        self.cancel = threading.Event()
        self.proc = None
        self.thread = None
        self.started_at = now_iso()
        self.state = "running"
        self.model = None
        self.effort = None

    def add(self, kind, text):
        if kind == "text" and self.items and self.items[-1]["kind"] == "text":
            self.items[-1]["text"] += text
        else:
            self.items.append({"kind": kind, "text": text})

    def snapshot(self):
        return {"turn_id": self.id, "agent": self.agent, "session_id": self.session_id,
                "started_at": self.started_at, "state": self.state,
                "items": [dict(i) for i in self.items]}


class Runtime:
    def __init__(self, which=None, port_open=None, popen=None, http_stream=None, killer=None,
                 usage_reader=None, clock=time.time, sleep=time.sleep, folders=None,
                 freebuff_url=FREEBUFF_URL,
                 freebuff_models=None, powershell_exe="powershell",
                 threaded=True, availability_ttl=10.0, env=None, models_reader=None,
                 disabled_check=None):
        self.which = which or shutil.which
        self.port_open = port_open or default_port_open
        self.popen = popen or default_popen
        self.http_stream = http_stream or default_http_stream
        self.killer = killer or dispatch.kill_tree
        self.usage_reader = usage_reader
        self.clock = clock
        self.sleep = sleep
        self.folders = [dict(f) for f in (folders or FOLDERS)]
        self.freebuff_url = freebuff_url
        self.freebuff_models = freebuff_models or dispatch.freebuff_models
        self.powershell_exe = powershell_exe
        self.threaded = threaded
        self.availability_ttl = availability_ttl
        self.env = env
        self.models_reader = models_reader
        self.disabled_check = disabled_check
        self.turns = {}
        self.lock = threading.Lock()
        self._avail = None
        self._avail_at = 0.0

    # -- availability
    def availability(self, force=False):
        now = self.clock()
        if not force and self._avail is not None and now - self._avail_at < self.availability_ttl:
            return self._avail
        out = {}
        for aid in AGENT_ORDER:
            a = AGENTS[aid]
            if "port" in a:
                ok = bool(self.port_open(a["port"]))
                out[aid] = {"available": ok, "detail": ("listening on port %d" % a["port"]) if ok
                            else "nothing listening on port %d" % a["port"], "path": None}
            else:
                path = self.which(a["exe"])
                out[aid] = {"available": bool(path), "detail": path or f"`{a['exe']}` not on PATH",
                            "path": path}
        self._avail, self._avail_at = out, now
        return out

    def exe(self, agent):
        info = self.availability().get(agent) or {}
        return info.get("path") or AGENTS[agent].get("exe") or agent

    # -- folders
    def resolve_cwd(self, value):
        """A folder id or a path equal to / under one of ``folders``; default the first."""
        if not value:
            return self.folders[0]["path"]
        for f in self.folders:
            if value == f["id"]:
                return f["path"]
        try:
            target = Path(value).resolve()
        except (OSError, ValueError):
            target = None
        if target is not None:
            for f in self.folders:
                root = Path(f["path"]).resolve()
                if target == root or root in target.parents:
                    return str(target)
        raise _http_error(400, {"error": "bad_folder", "detail": "working folder must be one of the listed projects"})

    def busy_session(self, session_id):
        with self.lock:
            for t in self.turns.values():
                if t.session_id == session_id and t.state == "running":
                    return t
        return None

    def active_turns(self):
        with self.lock:
            return [t for t in self.turns.values() if t.state == "running"]


RUNTIME = Runtime()


def set_runtime(rt):
    """Swap the runtime (tests); returns the previous one."""
    global RUNTIME
    prev, RUNTIME = RUNTIME, rt
    return prev


# ---------------------------------------------------------------- storage
def ensure_schema(store):
    if getattr(store, "_agents_schema", False):
        return
    with store.tx() as c:
        for stmt in SCHEMA:
            c.execute(stmt)
    store._agents_schema = True


def _rows(store, sql, params=()):
    ensure_schema(store)
    return [dict(r) for r in store._q(sql, params)]


def get_session(store, session_id):
    rows = _rows(store, "SELECT * FROM agent_sessions WHERE id=?", (session_id,))
    return rows[0] if rows else None


def create_session(store, agent, cwd, title):
    ensure_schema(store)
    sid = "as-" + secrets.token_hex(6)
    at = now_iso()
    with store.tx() as c:
        c.execute("INSERT INTO agent_sessions(id, agent, cwd, title, created_at, last_at, ext_id)"
                  " VALUES(?,?,?,?,?,?,NULL)", (sid, agent, cwd, title, at, at))
    return get_session(store, sid)


def add_messages(store, session_id, turn_id, items, ext_id=None):
    ensure_schema(store)
    at = now_iso()
    with store.tx() as c:
        for role, text in items:
            c.execute("INSERT INTO agent_messages(session_id, turn_id, role, text, at) VALUES(?,?,?,?,?)",
                      (session_id, turn_id, role, text, at))
        if ext_id:
            c.execute("UPDATE agent_sessions SET last_at=?, ext_id=? WHERE id=?", (at, ext_id, session_id))
        else:
            c.execute("UPDATE agent_sessions SET last_at=? WHERE id=?", (at, session_id))


def list_messages(store, session_id):
    return _rows(store, "SELECT role, text, at, turn_id FROM agent_messages WHERE session_id=? ORDER BY seq",
                 (session_id,))


def history(store, session_id, exclude_turn=None, limit=HISTORY_MESSAGES, max_chars=HISTORY_CHARS):
    """Earlier user/assistant messages as OpenAI chat messages (newest kept within the caps)."""
    rows = [r for r in list_messages(store, session_id)
            if r["role"] in ("user", "assistant") and (exclude_turn is None or r["turn_id"] != exclude_turn)]
    out, total = [], 0
    for r in reversed(rows[-limit:]):
        total += len(r["text"] or "")
        if total > max_chars and out:
            break
        out.append({"role": r["role"], "content": r["text"] or ""})
    return list(reversed(out))


def title_for(message):
    first = next((ln.strip() for ln in message.splitlines() if ln.strip()), "New chat")
    return _short(first, 60)


# ---------------------------------------------------------------- usage
def _usage_rows(h=None, rt=None):
    rt = rt or RUNTIME
    reader = rt.usage_reader or getattr(getattr(h, "app", None), "usage_reader", None)
    if reader is None:
        from . import usage
        reader = usage.read_usage
    try:
        rows = reader() or []
    except Exception:
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("pool")]


def usage_floor_hit(u):
    """``(row, floor)`` for the first known usage row at or below its pool's floor, else None.

    Floors come from ``pick.DEFAULT_FLOORS`` (0 for Claude/Antigravity, 20 for Codex), falling
    back to ``USAGE_FLOOR_PERCENT``. A floor of 0 lets work run until 0% left, never below.
    """
    for r in u.get("usage") or []:
        left = r.get("remaining_percent")
        if isinstance(left, (int, float)) and r.get("status") in ("FRESH", "STALE"):
            floor = floor_for(r.get("pool"), USAGE_FLOOR_PERCENT)
            if left <= max(floor, 0.0):
                return r, floor
    return None


def agent_usage(agent, rows):
    a = AGENTS[agent]
    pools = a.get("pools") or ()
    prefixes = a.get("pool_prefixes") or ()
    mine = [r for r in rows if r["pool"] in pools or any(str(r["pool"]).startswith(p) for p in prefixes)]
    known = [r for r in mine if isinstance(r.get("remaining_percent"), (int, float))
             and r.get("status") in ("FRESH", "STALE")]
    low = min(known, key=lambda r: r["remaining_percent"]) if known else None
    return {
        "usage_percent": low["remaining_percent"] if low else None,
        "usage_status": low.get("status") if low else "UNKNOWN",
        "usage_pool": low.get("label") or low["pool"] if low else None,
        "usage": [{k: r.get(k) for k in ("pool", "label", "remaining_percent", "status", "resets_at", "observed_at")}
                  for r in mine],
    }


# ---------------------------------------------------------------- models offered per agent
def _registry_rows(h, rt):
    """Rows of GET /api/models (injectable through ``Runtime.models_reader``); [] when it is unreadable."""
    try:
        if rt.models_reader is not None:
            return list(rt.models_reader() or [])
        from . import models
        return models.build_registry(models._sources(h), _usage_rows(h, rt))
    except Exception:
        return []


def offered_models(agent, rows):
    """Enabled registry models Owner may pick for ``agent`` (route match; Codex via its own provider)."""
    out = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("model") or r.get("kind") not in ("chat", "subagent"):
            continue
        if r.get("enabled") is False or r.get("access") is False:
            continue
        mine = r.get("provider") == "openai-codex" if agent == "codex" else r.get("route") == agent
        if mine:
            out.append({"id": r.get("id"), "model": r["model"], "label": r.get("label") or r["model"],
                        "elo": r.get("elo"), "context_window": r.get("context_window")})
    return out


def _agent_disabled(rt, agent):
    """True when the agent's switch is off (Agents tab). ``Runtime.disabled_check`` is injectable."""
    try:
        if rt.disabled_check is not None:
            return bool(rt.disabled_check(agent))
        from . import models
        return bool(models.is_disabled(route=agent))
    except Exception:
        return False


def _check_choice(h, agent, body):
    """Validated ``(model, effort)`` from a send body; anything the registry does not offer is refused."""
    model = str(body.get("model") or "").strip() or None
    effort = str(body.get("effort") or "").strip().lower() or None
    if model and model not in {m["model"] for m in offered_models(agent, _registry_rows(h, RUNTIME))}:
        raise _http_error(400, {"error": "model_not_offered",
                                "detail": f"{model} is not an enabled model for {AGENTS[agent]['name']}"})
    if effort and effort not in AGENT_EFFORTS.get(agent, ()):
        raise _http_error(400, {"error": "bad_effort",
                                "detail": f"{AGENTS[agent]['name']} does not take effort {effort!r}"})
    return model, effort


# ---------------------------------------------------------------- commands
_CLAUDE_SHORT = re.compile(r"^(opus|sonnet|haiku)-(\d+)\.(\d+)$")


def claude_model_id(model):
    """The id the claude CLI accepts: 'opus-5.5' -> 'claude-opus-5-5'; anything else passes through."""
    m = _CLAUDE_SHORT.match(str(model or "").strip())
    return "claude-%s-%s-%s" % m.groups() if m else model


def claude_argv(exe, session, model=None, effort=None):
    argv = [exe, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
    if session.get("ext_id"):
        argv += ["--resume", session["ext_id"]]
    else:
        argv += ["--session-id", session["_new_ext_id"]]
    if model:
        argv += ["--model", claude_model_id(model)]
    if effort:
        argv += ["--effort", effort]
    return argv + list(CLAUDE_EXTRA_ARGS)


def codex_argv(exe, session, model=None, effort=None):
    argv = [exe, "exec"] + (["resume"] if session.get("ext_id") else []) + ["--json", "--skip-git-repo-check"]
    if model:
        argv += ["-m", model]
    if effort:
        argv += ["-c", "model_reasoning_effort=" + effort]
    if session.get("ext_id"):
        argv.append(session["ext_id"])
    return argv + ["-"]


def agy_argv(exe, session, message, model=None):
    argv = [exe, "-p", message, "--output-format", "stream-json"]
    if model:
        argv += ["--model", model]
    if session.get("ext_id"):
        argv += ["--conversation", session["ext_id"]]
    return argv


# ---------------------------------------------------------------- turn execution
def _emit(store, turn, **payload):
    store.notify("agent", dict({"agent": turn.agent, "session_id": turn.session_id, "turn_id": turn.id}, **payload))


def _line_files(agent, line):
    """``[{path, added, removed}]`` for the edits one claude stream-json / codex JSONL line records."""
    if agent not in ("claude_code", "codex", "antigravity") or '"' not in line:
        return []
    try:
        from . import subagent_chats  # lazy: subagent_chats imports this module
        return subagent_chats.stream_summary("codex" if agent == "codex" else "claude_code", line)["files"]
    except Exception:  # a line it cannot read just has no counts
        return []


def _add_files(state, files):
    """Fold ``files`` into the turn's running per-file totals; returns the totals as a list."""
    total = state.setdefault("files", {})
    for f in files:
        t = total.setdefault(f["path"], {"path": f["path"], "added": 0, "removed": 0})
        t["added"] += f["added"]
        t["removed"] += f["removed"]
    return list(total.values())


def _handle(store, turn, state, events):
    for kind, value in events:
        if kind == "delta":
            turn.add("text", value)
            state["streamed"] = True
            _emit(store, turn, delta=value)
        elif kind == "tool":
            turn.add("tool", value)
            extra = {"files": state.pop("line_files")} if state.get("line_files") else {}
            _emit(store, turn, tool=value, **extra)
        elif kind == "ext_id":
            state["ext_id"] = value
        elif kind == "final":
            state["final"] = value
        elif kind == "error":
            state["error"] = value


def run_turn(store, rt, turn, session, message):
    agent = turn.agent
    state = {"ext_id": session.get("ext_id") or session.get("_new_ext_id"), "streamed": False,
             "final": None, "error": None, "exit_code": None}
    cwd = session.get("cwd")
    base_env = rt.env
    try:
        if agent == "freebuff":
            models = rt.freebuff_models() or list(dispatch.FREEBUFF_FALLBACK_MODELS)
            msgs = [{"role": "system", "content": "You are chatting with Owner in his Control Centre. "
                     f"Working folder: {cwd}. You cannot run tools or read files; answer in text."}]
            msgs += history(store, turn.session_id, exclude_turn=turn.id)
            msgs.append({"role": "user", "content": message})
            body = {"model": turn.model or models[0], "messages": msgs}
            for delta in rt.http_stream(rt.freebuff_url, body, turn.cancel):
                if turn.cancel.is_set():
                    break
                _handle(store, turn, state, [("delta", delta)])
        else:
            stdin_text, reply_path = None, None
            started = rt.clock()
            if agent == "claude_code":
                argv, parser, stdin_text = claude_argv(rt.exe(agent), session, turn.model, turn.effort), ClaudeParser(), message
                env = child_env(base_env)
            elif agent == "codex":
                argv, parser, stdin_text = codex_argv(rt.exe(agent), session, turn.model, turn.effort), CodexParser(), message
                env = child_env(base_env, drop=("CODEX_HOME",))
            elif agent == "antigravity":
                argv, parser = agy_argv(rt.exe(agent), session, message, turn.model), AgyParser()
                env = child_env(base_env)
            else:
                raise ValueError(f"unknown agent {agent!r}")
            proc = rt.popen(argv, cwd, env, stdin_text)
            turn.proc = proc
            if turn.cancel.is_set():
                rt.killer(proc.pid)
            for line in proc.stdout:
                if turn.cancel.is_set():
                    break
                seen = _line_files(agent, line)
                if seen:
                    _add_files(state, seen)
                    state["line_files"] = seen
                _handle(store, turn, state, parser.feed(line))
                state.pop("line_files", None)
            state["exit_code"] = proc.wait()
            if state["final"] and not state["streamed"] and not state["error"]:
                _handle(store, turn, state, [("delta", state["final"])])
            if not turn.cancel.is_set() and state["exit_code"] not in (0, None) and not state["error"]:
                tail = getattr(proc, "stderr_tail", lambda: "")() or ""
                state["error"] = _short(tail.strip().splitlines()[-1] if tail.strip() else
                                        f"{AGENTS[agent]['name']} exited with code {state['exit_code']}", 600)
    except Exception as exc:  # reported to Owner, never raised past the turn
        state["error"] = _short(f"{type(exc).__name__}: {exc}", 600)
    finish_turn(store, rt, turn, state)


def finish_turn(store, rt, turn, state):
    stopped = turn.cancel.is_set()
    files = list((state.get("files") or {}).values())
    extra = {"files": files} if files else {}
    items = [("assistant" if i["kind"] == "text" else i["kind"], i["text"]) for i in turn.items if i["text"]]
    if stopped:
        items.append(("error", "Stopped by Owner."))
    elif state.get("error"):
        items.append(("error", state["error"]))
    try:
        add_messages(store, turn.session_id, turn.id, items, ext_id=state.get("ext_id"))
    finally:
        turn.state = "stopped" if stopped else ("error" if state.get("error") else "done")
        with rt.lock:
            rt.turns.pop(turn.id, None)
        if state.get("error") and not stopped:
            _emit(store, turn, error=state["error"], done=True, **extra)
        else:
            _emit(store, turn, done=True, stopped=stopped, exit_code=state.get("exit_code"), **extra)


# ---------------------------------------------------------------- handlers
def _owner(h, what):
    if getattr(h, "actor", None) != "owner":
        raise _http_error(403, {"error": "owner_only", "detail": f"only Owner may {what}"})


# ---------------------------------------------------------------- connections (v3)
# One row per model connection Owner has. ``id`` is the toggle id (toggles.py); ``agent_id`` is the
# chat agent that shares the switch, when there is one.
CONNECTIONS = (
    ("claude_code", "Claude Code", "claude_code"), ("codex", "Codex", "codex"),
    ("freebuff", "Freebuff", "freebuff"),
    ("groq", "Groq", None), ("gemini_studio", "Gemini / AI Studio", None),
    ("gemini_app", "Gemini App", None), ("chatgpt_web", "ChatGPT Web", None), ("copilot", "GitHub Copilot", None),
    ("deepseek_dsh", "Built-in agent (dsh)", None), ("antigravity_manual", "Antigravity", "antigravity"),
)


def connections(rt=None, rows=None, db=None, tools_dir=None):
    """Status of every connection: ``[{id, name, agent_id, state, line, ok, enabled, dispatchable}]``.

    ``state``: ready | out_of_credits | not_installed | bridge_down | signed_out. Probes come from the
    ``Runtime`` (injectable ``which`` / ``port_open``); ``dispatchable`` is on AND ready.
    """
    from . import toggles, usage
    rt = rt or RUNTIME
    rows = rows if rows is not None else _usage_rows(None, rt)
    snap = toggles.snapshot(db)
    out = []
    for cid, name, agent_id in CONNECTIONS:
        try:
            st = dispatch.route_state(cid if cid != "antigravity_manual" else "agy_cli", which=rt.which,
                                      usage_rows=rows, port_open=rt.port_open,
                                      signed_in=usage.agy_signed_in, tools_dir=tools_dir,
                                      # live HTTP probe of the bridges only when the port probe is live too
                                      bridge_ok=(dispatch.default_bridge_ok if rt.port_open is default_port_open
                                                 else None))
        except Exception as exc:  # a probe that breaks reads as down, never as a crash of the tab
            st = {"state": "bridge_down", "line": "Status unavailable (%s)" % type(exc).__name__, "ok": False}
        on = snap.is_enabled(cid)
        out.append({"id": cid, "name": name, "agent_id": agent_id, "state": st["state"], "line": st["line"],
                    "ok": st["ok"], "enabled": on, "dispatchable": bool(on and st["ok"])})
    return out


def g_agents(h, q):
    rt = RUNTIME
    avail = rt.availability(force=q.get("refresh") == "1")
    rows = _usage_rows(h, rt)
    busy = {t.agent for t in rt.active_turns()}
    registry = _registry_rows(h, rt)
    agents = []
    for aid in AGENT_ORDER:
        a = AGENTS[aid]
        info = avail.get(aid) or {}
        entry = {"id": aid, "name": a["name"], "glyph": a["glyph"], "route": a["route"], "note": a["note"],
                 "available": bool(info.get("available")), "availability_detail": info.get("detail"),
                 "needs_confirm": bool(a.get("confirm")), "busy": aid in busy,
                 # floor_percent is the fallback; per-pool floors live in pick.DEFAULT_FLOORS.
                 "floor_percent": USAGE_FLOOR_PERCENT, "models": offered_models(aid, registry),
                 "efforts": list(AGENT_EFFORTS.get(aid, ()))}
        entry.update(agent_usage(aid, rows))
        agents.append(entry)
    store = getattr(getattr(h, "app", None), "store", None)
    return {"agents": agents, "connections": connections(rt, rows, store),
            "folders": rt.folders, "default_folder": rt.folders[0]["id"]}


def g_sessions(h, q):
    agent = q.get("agent")
    if agent:
        rows = _rows(h.app.store, "SELECT * FROM agent_sessions WHERE agent=? ORDER BY last_at DESC, id LIMIT 200",
                     (agent,))
    else:
        rows = _rows(h.app.store, "SELECT * FROM agent_sessions ORDER BY last_at DESC, id LIMIT 200")
    busy = {t.session_id: t.id for t in RUNTIME.active_turns()}
    for r in rows:
        r["active_turn"] = busy.get(r["id"])
    return {"sessions": rows}


def g_messages(h, q):
    sid = q.get("session")
    if not sid:
        raise _http_error(400, {"error": "bad_request", "detail": "missing session"})
    session = get_session(h.app.store, sid)
    if session is None:
        raise _http_error(404, {"error": "not_found", "detail": "no such session"})
    turn = RUNTIME.busy_session(sid)
    return {"session": session, "messages": list_messages(h.app.store, sid),
            "active": turn.snapshot() if turn else None}


def p_send(h, q):
    _owner(h, "talk to an agent")
    body = h._json_body()
    store, rt = h.app.store, RUNTIME
    agent = body.get("agent")
    message = str(body.get("message") or "").strip()
    if agent not in AGENTS:
        raise _http_error(400, {"error": "bad_request", "detail": "unknown agent"})
    if not message:
        raise _http_error(400, {"error": "bad_request", "detail": "missing message"})
    if len(message) > MAX_MESSAGE_CHARS:
        raise _http_error(413, {"error": "too_large", "detail": f"messages are limited to {MAX_MESSAGE_CHARS} characters"})
    if _agent_disabled(rt, agent):
        raise _http_error(409, {"error": "agent_disabled", "agent": agent,
                                "detail": f"{AGENTS[agent]['name']} is turned off in Agents"})
    model, effort = _check_choice(h, agent, body)
    sid = body.get("session_id")
    session = None
    if sid:
        session = get_session(store, sid)
        if session is None or session["agent"] != agent:
            raise _http_error(404, {"error": "not_found", "detail": "no such session for this agent"})
        running = rt.busy_session(sid)
        if running:
            raise _http_error(409, {"error": "busy", "turn_id": running.id,
                                    "detail": "this chat is still answering; stop it or wait"})
    info = rt.availability().get(agent) or {}
    if not info.get("available"):
        info = rt.availability(force=True).get(agent) or {}
    if not info.get("available"):
        raise _http_error(503, {"error": "unavailable", "agent": agent, "detail": info.get("detail")})
    u = agent_usage(agent, _usage_rows(h, rt))
    hit = usage_floor_hit(u)
    if hit is not None:
        raise _http_error(409, {"error": "usage_floor", "agent": agent, **u,
                                "detail": f"{hit[0].get('label') or hit[0]['pool']} has "
                                          f"{hit[0]['remaining_percent']:g}% left; "
                                          f"sends stop at {hit[1]:g}% to avoid extra spend"})
    if AGENTS[agent].get("confirm") and body.get("confirm") is not True:
        raise _http_error(409, {"error": "confirm_usage", "agent": agent, **u,
                                "detail": "Codex draws on your Codex plan; confirm after checking usage"})
    if session is None:
        cwd = rt.resolve_cwd(body.get("cwd"))
        session = create_session(store, agent, cwd, title_for(message))
    session = dict(session)
    if agent == "claude_code" and not session.get("ext_id"):
        session["_new_ext_id"] = str(uuid.uuid4())
    turn = Turn("at-" + secrets.token_hex(6), agent, session["id"])
    turn.model, turn.effort = model, effort
    with rt.lock:
        rt.turns[turn.id] = turn
    add_messages(store, session["id"], turn.id, [("user", message)])
    _emit(store, turn, started=True)
    if rt.threaded:
        turn.thread = threading.Thread(target=run_turn, args=(store, rt, turn, session, message), daemon=True,
                                       name=f"agent-{turn.id}")
        turn.thread.start()
    else:
        run_turn(store, rt, turn, session, message)
    return {"turn_id": turn.id, "session_id": session["id"], "agent": agent}


def p_stop(h, q):
    _owner(h, "stop an agent")
    body = h._json_body()
    tid = body.get("turn_id")
    if not tid:
        raise _http_error(400, {"error": "bad_request", "detail": "missing turn_id"})
    rt = RUNTIME
    with rt.lock:
        turn = rt.turns.get(tid)
    if turn is None:
        return {"stopped": False, "detail": "that turn has already finished"}
    turn.cancel.set()
    proc = turn.proc
    if proc is not None and getattr(proc, "pid", None):
        try:
            rt.killer(proc.pid)
        except Exception:
            pass
    return {"stopped": True, "turn_id": tid}


ROUTES = {
    "GET /api/agents": g_agents,
    "GET /api/agents/sessions": g_sessions,
    "GET /api/agents/messages": g_messages,
    "POST /api/agents/send": p_send,
    "POST /api/agents/stop": p_stop,
}
