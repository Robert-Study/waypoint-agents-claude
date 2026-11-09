"""Chat action handlers for the Waypoint runner: chat_send, chat_stop, chat_resume.

Covers Claude and Codex chats, direct and subagent (chats/<id>.json plus chats/<id>/messages.ndjson). A direct
DeepSeek chat belongs to DSH and is not handled here. ``register(handlers)`` adds each action type as
``handler(action, ctx) -> dict``; the runner discovers ``actions_*.py`` modules (nothing wires this by name).

The provider runs through the CLI adapters (headless ``claude -p``, Codex CLI) behind the toggle and the floor
decision (``policy.allowed``). A refusal (disabled route, unset floor on a metered pool, below floor, stale or
unknown reading, a frontier model as a child worker) is written into the chat as a system message and the
action is ``refused`` with the same reason. No floor or percentage lives here.

State beside the chat: ``chats/<id>/state.json`` holds the provider's own session id (read from the CLI's
output), the pid and the turn. Stop kills the process tree; resume continues from the stored session id.

ctx: ``store`` (waypoint_data.Store, or ctx itself / a dict with "store"), optional ``now`` (epoch seconds),
``exec`` and ``exe`` (adapter injection, for tests), ``kill`` (default dispatch_core.kill_tree), ``sync``
(True: wait for the turn to end inside the handler), ``poll_s`` and ``max_wait_s``. Every write goes through
the data-pyclient with revision checks; an action whose status is not ``queued`` is refused (processed once).
Return value: ``{"status": "done"|"refused"|"failed", "result": {...}}`` plus ``reason`` and ``error``.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

from . import dispatch_core as _dc  # first: puts core/waypoint-data/py on sys.path
from .adapters.codex_cli import CodexCliAdapter
from .adapters.headless_claude import AdapterRefused, HeadlessClaudeAdapter
from waypoint_data import io as wio
from waypoint_data.store import RevConflict, acquire_lock, iso

MAX_TRIES = 5
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
POLL_S = 0.5
MAX_WAIT_S = 6 * 3600
DEFAULT_MODEL = "sonnet"
CONTINUE_TEXT = "Continue where you left off."


class Refuse(Exception):
    """The action cannot be done as asked."""

    def __init__(self, reason, todo=None):
        super().__init__(reason)
        self.todo = todo


# ---------------------------------------------------------------- adapters with resume
class _ResumeClaude(HeadlessClaudeAdapter):
    def build_argv(self, spec, exe):
        argv = super().build_argv(spec, exe)
        return argv + ["--resume", str(spec["resume_id"])] if spec.get("resume_id") else argv


class _ResumeCodex(CodexCliAdapter):
    def build_argv(self, spec, exe):
        argv = super().build_argv(spec, exe)
        if spec.get("resume_id"):
            return argv[:-1] + ["resume", str(spec["resume_id"]), "-"]
        return argv


ADAPTERS = {"claude": _ResumeClaude, "codex": _ResumeCodex}


# ---------------------------------------------------------------- helpers
def _get(ctx, key):
    return ctx.get(key) if isinstance(ctx, dict) else getattr(ctx, key, None)


def _store(ctx):
    s = _get(ctx, "store")
    if s is None and hasattr(ctx, "read") and hasattr(ctx, "write"):
        s = ctx
    if s is None:
        raise Refuse("no data folder in ctx")
    return s


def _now(ctx, store):
    return (_get(ctx, "now") or getattr(store, "now", None) or time.time)()


def _sid(payload):
    v = payload.get("session_id")
    if not isinstance(v, str) or not ID_RE.match(v):
        raise Refuse("session_id is missing or not a valid id")
    return v


def _event(store, summary, data):
    if getattr(store, "writer", None):
        try:
            store.append_event(store.writer, {"kind": "chat", "summary": summary, "data": data})
        except Exception:  # a log line never blocks the action
            pass


def _update(store, rel, fn, create=None):
    """Read, edit with ``fn(copy)``, write with the rev we read; retry a conflict. Creates from ``create``."""
    for _ in range(MAX_TRIES):
        cur = store.read(rel)
        if cur is None:
            if create is None:
                raise Refuse(f"{rel} does not exist")
            new = dict(create)
            fn(new)
            try:
                return store.write(rel, new, expected_rev=0)
            except RevConflict:
                continue
        new = dict(cur)
        fn(new)
        try:
            return store.write(rel, new, expected_rev=cur.get("rev", 0))
        except RevConflict:
            continue
    raise RuntimeError(f"{rel} kept changing under us; try again")


def _chat(store, cid):
    c = store.read(f"chats/{cid}.json")
    if not isinstance(c, dict):
        raise Refuse(f"chat {cid} does not exist")
    if c.get("provider") not in ADAPTERS:
        raise Refuse(f"chat {cid} is not a Claude or Codex chat (DeepSeek chats belong to DSH)")
    return c


def _set_status(store, cid, status):
    _update(store, f"chats/{cid}.json", lambda c: c.update(status=status))


def _state(store, cid):
    return store.read(f"chats/{cid}/state.json") or {}


def _set_state(store, cid, **kw):
    _update(store, f"chats/{cid}/state.json", lambda s: s.update(kw),
            create={"schema": "waypoint.chat_state.v1", "id": cid})


def add_message(store, cid, provider, role, text, turn_id=None):
    """Append one line to chats/<id>/messages.ndjson (seq = line count, assigned under a lock)."""
    rel = f"chats/{cid}/messages.ndjson"
    path = store.path(rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lock = acquire_lock(path + ".lock", owner=store.writer or "py", now=store.now)
    try:
        msg = {"seq": len(store.read_log(rel)), "provider": provider, "role": role, "text": text,
               "at": iso(store.now()), "turn_id": turn_id}
        wio.append_line(path, json.dumps(msg, ensure_ascii=False))
        return msg
    finally:
        lock.release()


# ---------------------------------------------------------------- reading CLI output
def parse_output(provider, text):
    """(provider session id or None, [assistant texts]) from the CLI's JSON lines; raw text as a fallback."""
    sid, parts, result, raw = None, [], None, []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            raw.append(line)
            continue
        if not isinstance(ev, dict):
            continue
        sid = ev.get("session_id") or ev.get("thread_id") or sid
        kind = ev.get("type")
        if provider == "claude":
            if kind == "assistant":
                for blk in (ev.get("message") or {}).get("content") or []:
                    if isinstance(blk, dict) and blk.get("type") == "text" and blk.get("text"):
                        parts.append(blk["text"])
            elif kind == "result" and isinstance(ev.get("result"), str):
                result = ev["result"]
        elif kind == "item.completed":
            item = ev.get("item") or {}
            if item.get("type") in ("agent_message", "assistant_message") and item.get("text"):
                parts.append(item["text"])
    if not parts and result:
        parts = [result]
    if not parts and raw:
        parts = ["\n".join(raw)]
    return sid, parts


def _read_log(job_dir):
    try:
        with open(os.path.join(job_dir, "output.log"), "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except (OSError, TypeError):
        return ""


# ---------------------------------------------------------------- running a turn
def _adapter(ctx, store, provider):
    return ADAPTERS[provider](root=store.root, exec=_get(ctx, "exec"), exe=_get(ctx, "exe"),
                              now=_get(ctx, "now") or time.time)


def _finish(store, cid, provider, turn, job_dir, outcome):
    """Ingest the turn's output once and set the chat status. ``outcome`` is the adapter poll result or stopped."""
    st = _state(store, cid)
    if st.get("ingested_turn") == turn:
        return
    sid, parts = parse_output(provider, _read_log(job_dir))
    tid = f"t{turn}"
    for p in parts:
        add_message(store, cid, provider, "assistant", p, tid)
    state = outcome.get("state")
    if state == "failed":
        add_message(store, cid, provider, "system",
                    f"The {provider} process failed: {outcome.get('reason') or 'unknown'}", tid)
    fields = {"ingested_turn": turn, "pid": None,
              "state": {"stopped": "stopped", "failed": "failed"}.get(state, "finished")}
    if sid:
        fields["provider_session_id"] = sid
    _set_status(store, cid, {"stopped": "open", "failed": "error"}.get(state, "done"))
    _set_state(store, cid, **fields)


def _watch(ctx, store, cid, provider, turn, job_dir, adapter, pid):
    poll_s = _get(ctx, "poll_s")
    poll_s = POLL_S if poll_s is None else poll_s
    deadline = time.time() + (_get(ctx, "max_wait_s") or MAX_WAIT_S)
    while time.time() < deadline:
        st = _state(store, cid)
        if st.get("turn") != turn or st.get("state") != "running":
            return  # stopped (the stop handler ingested) or superseded
        r = adapter.poll({"pid": pid}, job_dir)
        if r.get("state") != "running":
            _finish(store, cid, provider, turn, job_dir, r)
            return
        time.sleep(poll_s)
    _finish(store, cid, provider, turn, job_dir, {"state": "failed", "reason": "timed out"})


def _run_turn(ctx, store, chat, text):
    cid, provider = chat["id"], chat["provider"]
    st = _state(store, cid)
    turn = int(st.get("turn") or 0) + 1
    spec = {"brief": text, "model": DEFAULT_MODEL, "cwd": st.get("cwd")}
    if chat.get("kind") == "subagent":
        spec["parent_id"] = chat.get("parent_session") or cid
    if st.get("provider_session_id"):
        spec["resume_id"] = st["provider_session_id"]
    job_dir = store.path(f"chats/{cid}/turn-{turn}")
    adapter = _adapter(ctx, store, provider)
    try:
        info = adapter.start(spec, job_dir)
    except AdapterRefused as exc:
        reason = exc.reason + (f" ({exc.todo})" if exc.todo else "")
        add_message(store, cid, provider, "system", f"Refused: {reason}", f"t{turn}")
        raise Refuse(exc.reason, exc.todo)
    pid = info.get("pid")
    _set_state(store, cid, turn=turn, state="running", pid=pid, job_dir=job_dir, route=adapter.route)
    _set_status(store, cid, "running")
    if _get(ctx, "sync"):
        _watch(ctx, store, cid, provider, turn, job_dir, adapter, pid)
    else:
        threading.Thread(target=_watch, args=(ctx, store, cid, provider, turn, job_dir, adapter, pid),
                         daemon=True).start()
    return {"chat": cid, "provider": provider, "turn": turn, "pid": pid}


# ---------------------------------------------------------------- handlers
def _send(action, p, ctx, store):
    cid = _sid(p)
    text = p.get("text")
    if not isinstance(text, str) or not text.strip():
        raise Refuse("text is missing")
    chat = _chat(store, cid)
    add_message(store, cid, chat["provider"], "user", text)
    if _state(store, cid).get("state") == "running":
        add_message(store, cid, chat["provider"], "system", "Refused: the chat is already running; stop it first")
        raise Refuse("chat is already running")
    out = _run_turn(ctx, store, chat, text)
    _event(store, f"chat_send {cid}", out)
    return out


def _stop(action, p, ctx, store):
    cid = _sid(p)
    chat = _chat(store, cid)
    st = _state(store, cid)
    if st.get("state") != "running":
        raise Refuse("chat is not running")
    # flip the state first so the watcher stands down, then kill and ingest what was said so far
    _set_state(store, cid, state="stopping")
    kill = _get(ctx, "kill") or _dc.kill_tree
    if st.get("pid"):
        kill(st["pid"])
    _finish(store, cid, chat["provider"], st.get("turn"), st.get("job_dir"), {"state": "stopped"})
    add_message(store, cid, chat["provider"], "system", "Stopped", f"t{st.get('turn')}")
    _event(store, f"chat_stop {cid}", {"chat": cid})
    return {"chat": cid, "stopped": True, "provider_session_id": _state(store, cid).get("provider_session_id")}


def _resume(action, p, ctx, store):
    cid = _sid(p)
    chat = _chat(store, cid)
    st = _state(store, cid)
    if st.get("state") == "running":
        raise Refuse("chat is already running")
    if not st.get("provider_session_id"):
        add_message(store, cid, chat["provider"], "system", "Refused: no stored session id to resume")
        raise Refuse("no stored session id to resume")
    out = _run_turn(ctx, store, chat, CONTINUE_TEXT)
    out["provider_session_id"] = st["provider_session_id"]
    _event(store, f"chat_resume {cid}", out)
    return out


def _guarded(fn):
    def handler(action, ctx):
        try:
            if action.get("status", "queued") != "queued":
                raise Refuse(f"action already {action.get('status')}")
            payload = action.get("payload")
            if not isinstance(payload, dict):
                raise Refuse("payload must be an object")
            return {"status": "done", "result": fn(action, payload, ctx, _store(ctx))}
        except Refuse as exc:
            return {"status": "refused", "result": {}, "reason": str(exc), "error": str(exc)}
        except Exception as exc:  # unexpected: report, never crash the runner
            msg = f"{type(exc).__name__}: {exc}"
            return {"status": "failed", "result": {}, "reason": msg, "error": msg}
    return handler


def register(handlers: dict) -> None:
    handlers["chat_send"] = _guarded(_send)
    handlers["chat_stop"] = _guarded(_stop)
    handlers["chat_resume"] = _guarded(_resume)
