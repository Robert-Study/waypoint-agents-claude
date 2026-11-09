"""Headless workers and the worker context (Waypoint Core 1.1).

Two routes:

``GET /api/worker/context?session=<id>&cwd=<path>``
    Which node is this session working on? Finds the live claim whose ``by`` equals the session, else
    the one whose cwd or worktree equals ``cwd``. Answers ``{node: {project, id, title, goal, check,
    files}, receipt_path, claimed_by}`` or ``{node: null, receipt_path: null, claimed_by: null}``.
    Read only and cheap: one pass over the live claims.

``POST /api/dispatch/headless {project, node, engine: "claude"|"codex", parent?}`` (owner only)
    Starts ``claude -p --output-format stream-json`` or ``codex exec --json`` in the project folder with
    the Copy Prompt packet on stdin, claims the node as ``headless-<id>`` and feeds an agent row
    (kind ``headless``) in the ``agents`` table from the output stream. Refused with 409 and a plain
    reason when the engine is switched off in Models or its usage is below the floor.

The worker writes its own receipt (``FILE_CONTRACT.md``). When the process ends without one, nothing is
written to the plan: the node stays as the done-check decides, and one event says so. No model is ever
called by this module itself, and tests inject the process runner.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import claims, dispatch, providers, scheduler
from .store import now_iso

ENGINES = ("claude", "codex")
ROUTE = {"claude": "claude_code", "codex": "codex"}
LABEL = {"claude": "Claude", "codex": "Codex"}
FLUSH_SECONDS = 0.5
LAST_LINE_MAX = 200
AGENT_COLUMNS = ("id", "kind", "parent", "session_id", "title", "project", "node", "cwd", "status", "tool",
                 "tokens_in", "tokens_out", "cache_read", "context", "context_window", "cost_eq", "model",
                 "last_line", "transcript", "started_at", "updated_at")
AGENT_SCHEMA = ("CREATE TABLE IF NOT EXISTS agents(id TEXT PRIMARY KEY, kind TEXT, parent TEXT, session_id TEXT,"
                " title TEXT, project TEXT, node TEXT, cwd TEXT, status TEXT, tool TEXT, tokens_in INT,"
                " tokens_out INT, cache_read INT, context INT, context_window INT, cost_eq REAL, model TEXT,"
                " last_line TEXT, transcript TEXT, started_at TEXT, updated_at TEXT)")


def _http_error(status, body):
    from .api import HttpError  # lazy: api imports the extension modules
    return HttpError(status, body)


# ---------------------------------------------------------------- worker context
def _norm(path):
    return os.path.normcase(os.path.normpath(str(path))) if path else None


def receipt_path(base, project, node) -> str:
    return str(Path(str(base)) / ".waypoint" / "receipts" / f"{project}.{node}.json")


def context(store, session=None, cwd=None, node=None, by=None) -> dict:
    """The node claimed by ``session`` (claim ``by``) or whose claim cwd/worktree equals ``cwd``.

    ``node`` (``<project>/<node>``) asks for that node only, and only when its live claim is held by
    ``by`` or by ``session``. The folder match skips headless claims and needs exactly one live claim."""
    session = (session or "").strip()
    by = (by or "").strip()
    none = {"node": None, "receipt_path": None, "claimed_by": None}
    if node and str(node).strip():
        project_id, _, node_id = str(node).strip().partition("/")
        project = store.get_project(project_id) if project_id and node_id else None
        c = store.claims(project_id).get(node_id) if project else None
        if c and c.get("by") and c.get("by") in (by, session):
            return _context_for(store, project, node_id, c, cwd)
        return none
    want = _norm(cwd.strip()) if cwd and cwd.strip() else None
    matches = []
    for project in store.list_projects():
        for node_id, c in store.claims(project["id"]).items():
            if session and c.get("by") == session:
                return _context_for(store, project, node_id, c, cwd)
            if want and not str(c.get("by") or "").startswith("headless-") \
                    and want in (_norm(c.get("cwd")), _norm(c.get("worktree"))):
                matches.append((project, node_id, c))
    if len(matches) == 1:
        return _context_for(store, *matches[0], cwd)
    if matches:
        return dict(none, ambiguous=True)
    return none


def _context_for(store, project, node_id, claim, cwd):
    node = store.get_node(project["id"], node_id)
    base = cwd or claim.get("cwd") or claim.get("worktree") or project.get("cwd")
    return {"node": {"project": project["id"], "id": node_id, "title": node.get("title"), "goal": node.get("goal"),
                     "check": node.get("check") or {}, "files": list(node.get("files") or [])},
            "receipt_path": receipt_path(base, project["id"], node_id) if base else None,
            "claimed_by": claim.get("by")}


def g_context(h, q):
    return context(h.app.store, q.get("session"), q.get("cwd"), q.get("node"), q.get("by"))


# ---------------------------------------------------------------- agent rows
def ensure_agents(store):
    with store.tx() as c:
        c.execute(AGENT_SCHEMA)


def upsert_agent(store, row):
    """Write one agent row, through ``trace.upsert_agent`` when that module has it, else locally
    (only the columns the table has), then push SSE ``trace``."""
    try:
        from . import trace
        fn = getattr(trace, "upsert", None)
    except ImportError:
        fn = None
    if callable(fn):
        fields = {k: v for k, v in row.items() if k != "id"}
        if fields.get("parent"):
            fields["hook_parent"] = fields["parent"]  # trace keeps parents by source; dispatch is a hook-level fact
        view = fn(store, row["id"], **fields)
        store.notify("trace", {"agents": [view]})
        return
    ensure_agents(store)
    with store.tx() as c:
        have = {r[1] for r in c.execute("PRAGMA table_info(agents)")}
        cols = [k for k in AGENT_COLUMNS if k in have and k in row]
        c.execute(f"INSERT INTO agents({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})"
                  f" ON CONFLICT(id) DO UPDATE SET {', '.join(k + '=excluded.' + k for k in cols if k != 'id')}",
                  [row[k] for k in cols])
    store.notify("trace", {"id": row["id"], "kind": "headless", "status": row.get("status")})


# ---------------------------------------------------------------- stream parsing
def _first_line(text):
    for ln in str(text or "").splitlines():
        if ln.strip():
            return " ".join(ln.split())[:LAST_LINE_MAX]
    return None


class StreamState:
    """Folds stream-json lines (Claude or Codex) into the numbers an agent row shows."""

    def __init__(self, engine):
        self.engine = engine
        self.tool = None
        self.last_line = None
        self.session_id = None
        self.model = None
        self.cost = None
        self.context = 0
        self.window = None
        self.usages = {}   # message id -> usage dict (one request is streamed as several lines)
        self.extra = [0, 0, 0]  # codex style totals: in, out, cache read

    def feed(self, line) -> bool:
        """Fold one stdout line. True when something a row shows changed."""
        try:
            ev = json.loads(line)
        except ValueError:
            return False
        if not isinstance(ev, dict):
            return False
        kind = ev.get("type")
        if ev.get("session_id") and not self.session_id:
            self.session_id = str(ev["session_id"])
        if kind == "system" and ev.get("model"):
            self.model = str(ev["model"])
        if kind == "assistant":
            return self._assistant(ev.get("message") or {})
        if kind == "result":
            if ev.get("total_cost_usd") is not None:
                self.cost = float(ev["total_cost_usd"])
            if ev.get("result"):
                self.last_line = _first_line(ev["result"]) or self.last_line
            return True
        return self._codex(ev, kind)

    def _assistant(self, msg):
        changed = False
        if msg.get("model"):
            self.model = str(msg["model"])
        for part in msg.get("content") or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "tool_use" and part.get("name"):
                self.tool = str(part["name"])
                changed = True
            elif part.get("type") == "text":
                line = _first_line(part.get("text"))
                if line:
                    self.last_line = line
                    changed = True
        u = msg.get("usage")
        if isinstance(u, dict):
            self.usages[msg.get("id") or f"m{len(self.usages)}"] = u
            self.context = (int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0)
                            + int(u.get("cache_creation_input_tokens") or 0))
            changed = True
        return changed

    def _codex(self, ev, kind):
        item = ev.get("item") if isinstance(ev.get("item"), dict) else None
        msg = ev.get("msg") if isinstance(ev.get("msg"), dict) else None
        changed = False
        if item:
            t = item.get("type")
            if t == "command_execution" and kind == "item.started":
                self.tool = "shell"
                changed = True
            elif t == "agent_message":
                self.last_line = _first_line(item.get("text")) or self.last_line
                changed = True
        if msg and msg.get("type") == "agent_message":
            self.last_line = _first_line(msg.get("message")) or self.last_line
            changed = True
        usage = None
        if kind == "turn.completed" and isinstance(ev.get("usage"), dict):
            usage = ev["usage"]
        elif msg and msg.get("type") == "token_count" and isinstance(msg.get("info"), dict):
            usage = msg["info"].get("total_token_usage")
        if isinstance(usage, dict):
            self.extra = [int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0),
                          int(usage.get("cached_input_tokens") or usage.get("cache_read_input_tokens") or 0)]
            changed = True
        return changed

    def tokens(self):
        t_in = sum(int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0)
                   for u in self.usages.values()) + self.extra[0]
        t_out = sum(int(u.get("output_tokens") or 0) for u in self.usages.values()) + self.extra[1]
        cache = sum(int(u.get("cache_read_input_tokens") or 0) for u in self.usages.values()) + self.extra[2]
        return t_in, t_out, cache


# ---------------------------------------------------------------- the process
class _Popen:
    def __init__(self, argv, cwd, stdin_text, env):
        self.proc = subprocess.Popen(argv, cwd=cwd or None, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                     env=env)
        try:
            self.proc.stdin.write(stdin_text)
            self.proc.stdin.close()
        except OSError:
            pass
        self.stdout = self.proc.stdout

    def wait(self):
        return self.proc.wait()

    def kill(self):
        dispatch.kill_tree(self.proc.pid)


def default_runner(argv, cwd, stdin_text, env):
    return _Popen(argv, cwd, stdin_text, env)


def build_argv(engine, node, cwd, claude_exe=None, codex_exe=None):
    model = node.get("model")
    if engine == "claude":
        argv = [claude_exe or providers.claude_exe(), "-p", "--output-format", "stream-json", "--verbose",
                "--model", str(model or "sonnet")]
        return argv + list(dispatch.CLAUDE_WORKER_ARGS)
    argv = [codex_exe or shutil.which("codex") or "codex", "exec", "--json", "--skip-git-repo-check",
            "--sandbox", dispatch.CODEX_SANDBOX]
    if cwd:
        argv += ["-C", str(cwd)]
    if model:
        argv += ["-m", str(model)]
    return argv + ["-"]


MODEL_OK = re.compile(r"^[A-Za-z0-9._:\[\]-]{1,80}$")
IS_WINDOWS = os.name == "nt"
CMD_UNSAFE = "&|<>^%!\""
DEFAULT_TIMEOUT = 2 * 60 * 60
RUNNING = {}  # headless id -> (process, killer): every worker still running
_RUN_LOCK = threading.Lock()


def cmd_shim_refusal(argv):
    """On Windows a .cmd/.bat shim runs through cmd.exe: refuse arguments cmd.exe would act on."""
    if not (IS_WINDOWS and argv and str(argv[0]).lower().endswith((".cmd", ".bat"))):
        return None
    for a in argv[1:]:
        if any(ch in str(a) for ch in CMD_UNSAFE):
            return "an argument has a character that is not allowed with a .cmd launcher"
    return None


def build_env(engine):
    env = dispatch.child_env()
    if engine == "codex":
        for k in dispatch.CODEX_DROP_ENV:
            env.pop(k, None)
    return env


def transcript_for(cwd, session_id):
    if not (cwd and session_id):
        return None
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    return str(Path.home() / ".claude" / "projects" / slug / f"{session_id}.jsonl")


# ---------------------------------------------------------------- refusals
def refusal(store, engine, node, usage_reader=None, now=None):
    """Plain-English reason this engine may not start now, else None."""
    route = ROUTE[engine]
    model = node.get("model")
    why = dispatch.toggle_refusal(route, model, store)
    if why:
        return why
    if dispatch.is_disabled(route, model, toggles=store):
        return f"{model or LABEL[engine]} is turned off in Models"
    try:
        rows = list((usage_reader or scheduler.default_usage_reader)() or [])
    except Exception:
        rows = []
    by_pool = {r.get("pool"): r for r in rows if isinstance(r, dict)}
    state, info = scheduler.usage_gate(route, by_pool, None, now if now is not None else time.time())
    if state == "low":
        return f"{LABEL[engine]} usage is low: {info.get('reason')}"
    if state == "hold":
        floors = [scheduler.floor_for(u, scheduler.DEFAULT_RESERVE_PERCENT) for u in scheduler.USAGE_POOLS[route]]
        if max(floors) > 0:  # a pool with no floor may run on a reading it cannot confirm
            return f"{LABEL[engine]} usage cannot be confirmed: {info.get('reason')}"
    return None


# ---------------------------------------------------------------- dispatch
def prompt_for(store, project, node, path):
    from .api import build_prompt_packet  # the Copy Prompt packet builder
    text = build_prompt_packet(store, project, node).rstrip()
    return (text + "\n\nWhen you finish, write your receipt to " + path
            + " (schema waypoint.receipt.v1, see FILE_CONTRACT.md)\n")


def launch(store, project, node, engine, parent=None, *, runner=None, usage_reader=None, claude_exe=None,
           codex_exe=None, now=None, timeout=None, killer=None):
    """Claim the node and start the worker. Returns ``{ok, id, thread}``; raises ``HttpError`` (409/404/400)."""
    if engine not in ENGINES:
        raise _http_error(400, {"error": "bad_request", "detail": "engine must be claude or codex"})
    proj = store.get_project(project)
    n = store.get_node(project, node) if proj else None
    if n is None:
        raise _http_error(404, {"error": "not_found", "detail": f"no node {project}/{node}"})
    cwd = n.get("cwd") or proj.get("cwd")
    if not cwd:
        raise _http_error(400, {"error": "bad_request", "detail": "the project has no working folder"})
    if n.get("model") is not None and not MODEL_OK.match(str(n["model"])):
        raise _http_error(409, {"error": "refused", "detail": "model name not allowed"})
    why = refusal(store, engine, n, usage_reader, now)
    if why:
        raise _http_error(409, {"error": "refused", "detail": why})
    argv = build_argv(engine, n, cwd, claude_exe, codex_exe)
    why = cmd_shim_refusal(argv)
    if why:
        raise _http_error(409, {"error": "refused", "detail": why})
    hid = secrets.token_hex(3)
    agent_id = f"headless-{hid}"
    try:
        claims.claim(store, project, node, agent_id, cwd=str(cwd), environment=f"headless {LABEL[engine]}")
    except claims.ClaimConflict as e:
        raise _http_error(409, {"error": "claimed", "detail": str(e)})
    except Exception as e:  # NotAllowed (draft project, done node) and the like
        raise _http_error(409, {"error": "refused", "detail": str(e).strip("'\"")})
    path = receipt_path(cwd, project, node)
    stdin = prompt_for(store, project, node, path)
    env = build_env(engine)
    env["WAYPOINT_NODE"] = f"{project}/{node}"
    env["WAYPOINT_CLAIM_BY"] = agent_id
    row = {"id": agent_id, "kind": "headless", "parent": parent or None, "session_id": None,
           "title": f"{project}/{node}", "project": project, "node": node, "cwd": str(cwd), "status": "running",
           "tool": None, "tokens_in": 0, "tokens_out": 0, "cache_read": 0, "context": 0, "context_window": None,
           "cost_eq": None, "model": n.get("model"), "last_line": None, "transcript": None,
           "started_at": now_iso(), "updated_at": now_iso()}
    started = time.time()
    try:
        proc = (runner or default_runner)(argv, str(cwd), stdin, env)
    except Exception as e:
        claims.release(store, project, node, by=agent_id)
        raise _http_error(409, {"error": "refused", "detail": f"could not start {LABEL[engine]}: {e}"})
    killer = killer or kill_process
    with _RUN_LOCK:
        RUNNING[agent_id] = (proc, killer)
    upsert_agent(store, row)
    th = threading.Thread(target=run_stream, args=(store, row, proc, engine, path),
                          kwargs={"started": started, "timeout": DEFAULT_TIMEOUT if timeout is None else timeout,
                                  "killer": killer},
                          daemon=True, name=f"waypoint-{agent_id}")
    th.start()
    return {"ok": True, "id": agent_id, "thread": th}


def kill_process(proc):
    """Kill a worker and everything it started (``taskkill /T /F`` on Windows)."""
    pid = getattr(getattr(proc, "proc", None), "pid", None)
    if pid is not None:
        dispatch.kill_tree(pid)
    elif hasattr(proc, "kill"):
        proc.kill()


def run_stream(store, row, proc, engine, path, started=0.0, timeout=None, killer=None):
    """Read the worker's stdout to the end, keeping the agent row current; mark it done or error."""
    state = StreamState(engine)
    last_flush = 0.0
    stopped = []  # why this run was cut short, if it was
    killer = killer or kill_process

    def stop(why):
        stopped.append(why)
        try:
            killer(proc)
        except Exception:
            pass

    def push(final=False):
        t_in, t_out, cache = state.tokens()
        row.update(tool=None if final else state.tool, tokens_in=t_in, tokens_out=t_out, cache_read=cache,
                   context=state.context, cost_eq=state.cost, last_line=state.last_line,
                   model=state.model or row.get("model"), session_id=state.session_id,
                   transcript=transcript_for(row["cwd"], state.session_id) if engine == "claude" else None,
                   updated_at=now_iso())
        if final and stopped:
            row["last_line"] = stopped[0]
        upsert_agent(store, row)

    timer = threading.Timer(timeout, stop, args=("Stopped after the time limit",)) if timeout else None
    if timer:
        timer.daemon = True
        timer.start()
    code = None
    try:
        for line in proc.stdout:
            if state.feed(line) and time.monotonic() - last_flush >= FLUSH_SECONDS:
                last_flush = time.monotonic()
                push()
        code = proc.wait()
    except Exception:
        code = -1
    finally:
        if timer:
            timer.cancel()
        with _RUN_LOCK:
            was_stopped_by_close = RUNNING.pop(row["id"], None) is None
    if was_stopped_by_close and not stopped:
        stopped.append("Stopped when the engine closed")
    row["status"] = "error" if stopped or code != 0 else "done"
    try:
        push(final=True)
        if not _receipt_arrived(path, started):
            # No receipt for this run will ever release this claim: release it now so the task is free again.
            try:
                claims.release(store, row["project"], row["node"], by=row["id"])
            except Exception:
                pass
            why = stopped[0][0].lower() + stopped[0][1:] if stopped else "ended without writing a receipt"
            store.event(row["project"], row["node"], "headless", row["id"],
                        f"{LABEL[engine]} headless worker {row['id']} {why}, "
                        "so its claim was released and the task stays as its done-check decides.",
                        {"exit": code, "receipt": path})
    except Exception:
        pass


def _receipt_arrived(path, started):
    """True when the receipt file was written during this run (its mtime is not before the start)."""
    try:
        return os.stat(path).st_mtime >= started
    except OSError:
        return False


class _Handle:
    def stop(self):
        """Kill every running worker (the engine is closing). Each one's thread then ends the row."""
        with _RUN_LOCK:
            running = list(RUNNING.items())
            RUNNING.clear()
        for _id, (proc, killer) in running:
            try:
                killer(proc)
            except Exception:
                pass


def start(app):
    """Background hook (``api.BACKGROUND``): the handle's ``stop()`` kills every running worker when the
    engine closes. No thread is needed."""
    return _Handle()


def p_dispatch(h, q):
    from .api import _owner_only, _need
    _owner_only(h, "start a headless worker")
    b = h._json_body()
    _need(b, "project", "node")
    out = launch(h.app.store, b["project"], b["node"], str(b.get("engine") or "claude").lower(), b.get("parent"),
                 runner=getattr(h.app, "headless_runner", None), usage_reader=h.app.usage_reader,
                 timeout=getattr(h.app, "headless_timeout", None), killer=getattr(h.app, "headless_killer", None))
    return {"ok": True, "id": out["id"]}


ROUTES = {"GET /api/worker/context": g_context, "POST /api/dispatch/headless": p_dispatch}
