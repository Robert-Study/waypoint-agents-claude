"""Loopback HTTP API for the Waypoint engine (stdlib ``ThreadingHTTPServer``).

Waypoint API ``API_VERSION`` ("1.0"): ``GET /health`` reports ``api`` (major, int) and ``api_version``,
and every answer carries the header ``X-Waypoint-API``. A minor bump is additive; a major bump breaks.

Every request must name this engine in its ``Host`` header: ``127.0.0.1:<port>``, ``localhost:<port>``
or ``[::1]:<port>`` (the bound port). Anything else gets 403 ``{"error": "bad_host"}``, so a web page on
another name that resolves to 127.0.0.1 (DNS rebinding) cannot read or write.

CORS echoes ``Access-Control-Allow-Origin`` only for the dsh web origins.

Every state-changing request (POST/PUT/PATCH/DELETE) needs a credential, and the actor comes
from that credential, never from the body:
- an allowlisted browser ``Origin`` (the dsh page) -> actor ``owner`` (Owner);
- the ``X-CC-Owner-Token`` header matching ``owner.token`` (random, user-only; ``default_owner_token_path``:
  beside ``CC_TOKEN_FILE`` when that is set, else ``<data dir>/owner.token``; ``start()`` makes it before
  the port is bound) -> actor ``owner``, credential ``claude-mod`` (Owner's clicks in the Claude
  desktop mod, which has no page Origin);
- otherwise the ``X-CC-Token`` header matching ``data/engine.token`` (random, written at startup,
  readable only by this Windows user) -> actor ``router``. The router may never set a task done,
  answer questions, change goals/checks/brief, accept plans, cancel a shutdown or turn power-off on;
  plans it imports land as ``draft`` until Owner accepts them (``POST /api/plan/accept``).
A non-allowlisted Origin or no credential at all gets 403. GETs with a good Host stay open.

``/api/board`` is row-encoded to stay under 50 KB at 300 nodes: ``fields`` names the columns
and each entry of ``nodes`` is a list in that order (see ``decode_nodes``). The optional trailing
columns ``checks`` (``[passed, failed, last]`` done-check results; null when the check never ran),
``recommended`` (the planned model id) and ``claim`` (``{by, worktree, cwd, environment, since,
refreshed_at, stale}`` while a lead session or worker is on the task) are left off a row when
they and everything after them are null. SSE ``state`` events carry the same payload.
"""
from __future__ import annotations

import hmac
import importlib
import json
import os
import queue
import secrets
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import DATA_DIR, __version__, config, graph, plan, versions
# needs, runsheet and usage are imported where they are used: they are not needed to answer /health,
# and every import before the port is bound delays start-up on a busy machine.
from .store import OWNER, STATUSES, VERSION_ONLY_STATUSES, GoalLocked, NotAllowed, StaleRevision

VERSION = __version__
API_VERSION = "1.1"  # Waypoint API: major.minor; minor = additive, major = breaking
API_MAJOR = int(API_VERSION.split(".")[0])
API_HEADER = "X-Waypoint-API"
DEFAULT_PORT = 8751
ALLOWED_ORIGINS = frozenset({"http://127.0.0.1:3080", "http://localhost:3080"})
BOARD_FIELDS = ("id", "title", "status", "display_status", "deps", "wave", "pool", "model", "effort",
                "attempts", "actor", "lane", "checks", "recommended", "claim")
OPTIONAL_TAIL = 3  # checks, recommended, claim: trailing nulls are left off each row
TITLE_LIMIT = 80
MAX_BODY = 1 << 20
RULES = ("Do not change the goal.", "Make the check pass.",
         "Report evidence: the exact command output or files that prove the check passes.")
TOKEN_HEADER = "X-CC-Token"
OWNER_TOKEN_HEADER = "X-CC-Owner-Token"
MOD_CREDENTIAL = "claude-mod"  # who presented the owner token; the actor stays OWNER
# Optional engine modules exposing ``ROUTES = {"GET /api/x": handler, "POST /api/y": handler}``
# with the same ``handler(h, q)`` signature as the built-in routes. Imported lazily; a missing
# module (or one without ROUTES) is skipped. Built-in routes always win on a clash. Extension
# POSTs pass the same credential rule first, so ``h.actor`` is ``owner`` or ``router``.
EXTENSIONS = ["brain_overlaps", "planner", "agents", "sprints", "learn", "models", "claims",
              "history", "owner_runs", "failure_doctor", "sync", "model_health", "pick", "subagent_chats",
              "todo", "brief", "deep_plan", "state_snapshot", "toggles", "constraints", "app_shell", "cloud_sync",
              "receipts", "trace", "headless", "wpc_bridge"]
# An extension may also expose ``start(app)`` for background work (a file watcher, a transcript tailer). The
# warm-up thread calls it once after the routes load; it returns an object with ``stop()`` (or None).
BACKGROUND = ("receipts", "trace", "headless")
EXTENSION_TTL = 10.0
ROUTER = "router"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")


# ---------------------------------------------------------------- engine token
def default_token_path() -> Path:
    """``$CC_TOKEN_FILE`` or ``<data dir>/engine.token`` (``config.data_dir()``, read at call time)."""
    return Path(os.environ.get("CC_TOKEN_FILE") or config.data_dir() / "engine.token")


def default_owner_token_path() -> Path:
    """The one rule for ``owner.token`` (the CLI and the Claude mod use the same): beside ``CC_TOKEN_FILE``
    when that is set, else ``<data dir>/owner.token``."""
    named = os.environ.get("CC_TOKEN_FILE")
    return Path(named).with_name("owner.token") if named else config.data_dir() / "owner.token"


def _restrict_to_user(path: Path):
    """Make ``path`` readable only by the current user (best effort; never raises)."""
    try:
        import subprocess
        if os.name == "nt":
            import getpass
            user = getpass.getuser()
            domain = os.environ.get("USERDOMAIN")
            who = f"{domain}\\{user}" if domain else user
            subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{who}:F"],
                           check=False, capture_output=True, timeout=15)
        else:
            os.chmod(path, 0o600)
    except Exception:
        pass


def read_token(path=None):
    """The engine token text, or None when the file is missing/empty/unreadable."""
    try:
        text = Path(path or default_token_path()).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def ensure_token(path=None, background=False) -> str:
    """Return the engine token, creating ``path`` with a fresh random token if missing.

    A new file is made user-only before it is used. For an existing file the permissions are
    re-applied too; ``background=True`` does that in a daemon thread (``icacls`` takes ~0.5 s, more on
    a busy machine) so engine start-up never waits on it."""
    path = Path(path or default_token_path())
    existing = read_token(path)
    if existing and len(existing) >= 32:
        if background:
            threading.Thread(target=_restrict_to_user, args=(path,), daemon=True, name="token-acl").start()
        else:
            _restrict_to_user(path)
        return existing
    return _write_new_token(path)


def _write_new_token(path: Path, harden=True) -> str:
    """Write a fresh random token to ``path`` (atomic rename), made user-only before the rename.
    ``harden=False`` skips ``icacls``; the caller must then restrict the file itself (``start()``'s
    warm-up thread does, so binding the port never waits on it)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token)
    if harden:
        _restrict_to_user(tmp)
    os.replace(tmp, path)  # a rename in the same folder keeps the user-only ACL set on tmp
    return token


def prepare_owner_token(path) -> str | None:
    """Make sure ``path`` holds an owner token before the port is bound; cheap (no ``icacls`` wait:
    the warm-up thread makes the file user-only right after)."""
    path = Path(path)
    existing = read_token(path)
    if existing and len(existing) >= 32:
        return existing
    try:
        return _write_new_token(path, harden=False)
    except OSError as exc:
        print(f"engine start: owner token not written: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return None


class HttpError(Exception):
    def __init__(self, status, body):
        super().__init__(body)
        self.status = status
        self.body = body


def decode_nodes(board) -> list:
    """Expand row-encoded board nodes into dicts (reference decoder for clients/tests).

    A row may be shorter than ``fields``: missing trailing columns are null.
    """
    fields = board["fields"]
    return [{f: (row[i] if i < len(row) else None) for i, f in enumerate(fields)} for row in board["nodes"]]


def build_board(store, project_id):
    project = store.get_project(project_id) if project_id else None
    revision = store.revision()
    if project is None:
        return {"project": None, "revision": revision, "fields": list(BOARD_FIELDS), "nodes": [],
                "edges": [], "counts": {"total": 0}, "integrity_ok": True, "integrity": []}
    nodes = store.list_nodes(project_id)
    display = graph.readiness(nodes)
    draft = (project.get("status") or "active") == "draft"
    if draft:  # nothing in a draft is runnable, so nothing shows as ready
        display = {k: ("not_started" if v == "ready" else v) for k, v in display.items()}
    mismatches = store.verify_integrity(project_id)
    try:
        waves = graph.waves(nodes)
        edges = [list(e) for e in graph.transitive_reduction(nodes)]
    except ValueError:  # a cycle introduced after import: show flat rather than fail
        waves = {n["id"]: 0 for n in nodes}
        edges = [[d, n["id"]] for n in nodes for d in n["deps"]]
    rows, counts = [], {s: 0 for s in STATUSES}
    check_counts = store.check_counts(project_id)  # {id: [passed, failed, last]}; absent = never run
    live_claims = store.claims(project_id)  # {id: claim}; only tasks someone is working on now
    for n in nodes:
        ds = display[n["id"]]
        counts[ds] = counts.get(ds, 0) + 1
        title = n.get("title") or n["id"]
        if len(title) > TITLE_LIMIT:
            title = title[: TITLE_LIMIT - 1] + "…"
        row = [n["id"], title, n["status"], ds, n["deps"], waves[n["id"]], n.get("pool"),
               n.get("model"), n.get("effort"), n.get("attempts", 0), n.get("actor"), n.get("lane"),
               check_counts.get(n["id"]), n.get("recommended"), live_claims.get(n["id"])]
        keep = len(BOARD_FIELDS) - OPTIONAL_TAIL
        while len(row) > keep and row[-1] is None:  # trailing nulls left off to stay small
            row.pop()
        rows.append(row)
    counts["total"] = len(nodes)
    fam = versions.family_info(store, project)
    return {"project": {"id": project["id"], "title": project["title"], "cwd": project["cwd"],
                        "family": fam["family"], "family_title": fam["family_title"], "version": fam["version"],
                        "status": "archived" if int(project.get("archived") or 0) else (project.get("status") or "active"),
                        "draft": draft, "archived": bool(int(project.get("archived") or 0))},
            "revision": revision, "fields": list(BOARD_FIELDS), "nodes": rows, "edges": edges,
            "counts": counts, "integrity_ok": not mismatches, "integrity": mismatches[:50]}


def build_prompt_packet(store, project_id, node_id) -> str:
    """Copy Prompt packet: goal, brief, done-check, deps' evidence and the rules."""
    project = store.get_project(project_id)
    node = store.get_node(project_id, node_id)
    if project is None or node is None:
        raise HttpError(404, {"error": "not_found"})
    check = node.get("check") or {}
    lines = [
        f"# Task {node['id']}: {node.get('title') or ''}".rstrip(),
        f"Project: {project['title']} ({project['id']})",
        f"Working directory: {project.get('cwd') or '(not set)'}",
        "",
        "## Goal (locked)",
        node["goal"],
        "",
        "## Brief",
        project.get("brief") or "(no brief)",
        "",
        "## Done-check (must pass)",
        _describe_check(check),
        "```json",
        json.dumps(check, indent=1),
        "```",
        "",
        "## Dependencies and their evidence",
    ]
    if not node["deps"]:
        lines.append("(none)")
    for d in node["deps"]:
        dep = store.get_node(project_id, d)
        if dep is None:
            lines.append(f"- {d}: missing")
            continue
        ev = (dep.get("evidence") or "no evidence recorded").strip()
        if len(ev) > 1200:
            ev = ev[:1200] + " ..."
        lines.append(f"- {d} ({dep.get('title') or ''}), status {dep['status']}, by {dep.get('actor') or 'nobody'}:")
        lines.extend("    " + ln for ln in ev.splitlines() or [ev])
    lines += ["", "## Rules"] + [f"- {r}" for r in RULES]
    return "\n".join(lines) + "\n"


def _describe_check(check):
    kind = check.get("type")
    if kind == "command":
        s = f"Run `{check.get('run')}`; it must exit 0"
        if check.get("expect_stdout") is not None:
            s += f" and print exactly `{check['expect_stdout']}`"
        return s + "."
    if kind == "files_exist":
        return "These files must exist: " + ", ".join(check.get("paths") or []) + "."
    if kind == "text_present":
        return f"`{check.get('path')}` must contain `{check.get('text')}`."
    if kind == "human":
        return f"{config.OWNER_NAME} confirms: {check.get('prompt') or 'the result'} (never automatic)."
    return f"Unknown check type {kind!r}."


def _read_usage():
    from . import usage
    return usage.read_usage()


def _call(fn, **available):
    """Call ``fn`` with only the keyword arguments it accepts."""
    import inspect
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return fn()
    if any(p.kind == p.VAR_KEYWORD for p in params.values()):
        return fn(**available)
    return fn(**{k: v for k, v in available.items() if k in params})


def _iso_or_400(value, field):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat(timespec="seconds")
    if isinstance(value, str):
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            datetime.fromisoformat(text)
            return value
        except ValueError:
            pass
    raise HttpError(400, {"error": "bad_request", "detail": f"{field} must be an ISO time or epoch seconds"})


class App:
    def __init__(self, store, usage_reader=None, capture_runner=None, capture_dir=None, heartbeat=15.0,
                 poll_interval=1.0, module_loader=None, token=None, token_path=None, power_runner=None,
                 owner_token=None, owner_token_path=None):
        self.store = store
        # The router credential. Default file sits beside the database (data/engine.token live).
        self.token_path = Path(token_path) if token_path else Path(store.path).parent / "engine.token"
        self.token = token or ensure_token(self.token_path, background=True)
        # The Claude mod's owner credential: a second file beside engine.token, made on first use.
        self.owner_token_path = (Path(owner_token_path) if owner_token_path
                                 else self.token_path.with_name("owner.token"))
        self._owner_token = owner_token
        self._owner_lock = threading.Lock()
        self.power_runner = power_runner
        self._ext_routes = None
        self._ext_loaded_at = 0.0
        self._ext_lock = threading.Lock()
        self.usage_reader = usage_reader or _read_usage
        self.capture_runner = capture_runner
        self.capture_dir = capture_dir
        self.heartbeat = heartbeat
        self.poll_interval = poll_interval
        self.import_module = module_loader or importlib.import_module
        self.closing = threading.Event()
        self._queues: set = set()
        self._qlock = threading.Lock()
        self._seen_revision = store.revision()
        store.subscribe(self._on_store)

    def attach_snapshots(self):
        """R2: board snapshots travel with the repo (only attaches to the live db). Called from the
        start-up warm-up thread, not the constructor, so binding the port never waits on it."""
        try:
            from . import state_snapshot
            state_snapshot.attach(self.store)
        except Exception:
            pass

    # --- fan-out to SSE clients
    def _on_store(self, kind, payload):
        if kind == "state":
            self._seen_revision = max(self._seen_revision, int(payload.get("revision") or 0))
        if kind == "event":
            return
        with self._qlock:
            qs = list(self._queues)
        for q in qs:
            q.put((kind, payload))

    def add_queue(self):
        q = queue.Queue()
        with self._qlock:
            self._queues.add(q)
        return q

    def drop_queue(self, q):
        with self._qlock:
            self._queues.discard(q)

    def close(self):
        self.closing.set()
        for handle in list(getattr(self, "background_handles", ())):
            try:
                handle.stop()
            except Exception:
                pass
        with self._qlock:
            qs = list(self._queues)
        for q in qs:
            q.put(None)

    def poll_external(self):
        """Notice mutations made by other processes (the scheduled tick) and fan them out."""
        while not self.closing.wait(self.poll_interval):
            try:
                rev = self.store.revision()
            except Exception:
                continue
            if rev != self._seen_revision:
                self._seen_revision = rev
                self._on_store("state", {"project": None, "revision": rev})

    def extension_routes(self) -> dict:
        """``{"METHOD /path": handler}`` merged from ``EXTENSIONS``; re-scanned every few seconds so a
        module added while the engine runs is picked up without a restart."""
        import time
        with self._ext_lock:
            now = time.monotonic()
            if self._ext_routes is not None and now - self._ext_loaded_at < EXTENSION_TTL:
                return self._ext_routes
            merged = {}
            for name in EXTENSIONS:
                try:
                    mod = self.import_module(f"cc_engine.{name}")
                except Exception:  # missing or broken module: skip it, never break the engine
                    continue
                routes = getattr(mod, "ROUTES", None)
                if not isinstance(routes, dict):
                    continue
                for key, fn in routes.items():
                    if not callable(fn) or not isinstance(key, str):
                        continue
                    method, _, path = key.strip().partition(" ")
                    method, path = method.upper(), path.strip()
                    if method not in ("GET", "POST") or not path.startswith("/"):
                        continue
                    builtin = GET_ROUTES if method == "GET" else POST_ROUTES
                    if path in builtin:
                        continue
                    merged.setdefault(f"{method} {path}", fn)
            self._ext_routes, self._ext_loaded_at = merged, now
            return merged

    def owner_token(self):
        """The owner-scoped mod token; creates ``owner_token_path`` (user-only) on first use."""
        if not self._owner_token:
            with self._owner_lock:  # the warm-up thread and a first request may race here
                if not self._owner_token:
                    try:
                        self._owner_token = ensure_token(self.owner_token_path)
                    except OSError:
                        return None
        return self._owner_token

    def optional(self, name):
        try:
            return self.import_module(f"cc_engine.{name}")
        except ImportError:
            raise HttpError(503, {"error": "unavailable", "module": name})


class CCServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, addr, app):
        self.app = app
        self.answered = threading.Event()  # set once the first answer has gone out (start-up warm-up waits)
        super().__init__(addr, Handler)
        self._poller = None
        if app.poll_interval and app.poll_interval > 0:
            self._poller = threading.Thread(target=app.poll_external, daemon=True, name="cc-poll")
            self._poller.start()

    def shutdown(self):
        self.app.close()
        super().shutdown()


def make_server(store, host="127.0.0.1", port=DEFAULT_PORT, **app_kwargs) -> CCServer:
    return CCServer((host, port), App(store, **app_kwargs))


class Handler(BaseHTTPRequestHandler):
    server_version = "cc-engine/" + VERSION
    actor = None  # set by do_POST from the request's credential ("owner" | "router")
    credential = None  # "claude-mod" when the owner token was presented

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    # ------------------------------------------------------------ plumbing
    @property
    def app(self) -> App:
        return self.server.app

    def _origin(self):
        return self.headers.get("Origin")

    def end_headers(self):
        self.send_header(API_HEADER, API_VERSION)  # every answer: JSON, SSE, preflight and errors
        super().end_headers()
        answered = getattr(self.server, "answered", None)
        if answered is not None:
            answered.set()

    def _host_ok(self):
        """DNS-rebinding guard: Host must be a loopback name plus the port this server is bound to."""
        host = (self.headers.get("Host") or "").strip().lower()
        try:
            port = int(self.server.server_address[1])
        except (AttributeError, IndexError, TypeError, ValueError):
            return False
        return host in {f"{name}:{port}" for name in LOOPBACK_HOSTS}

    def _guard_host(self):
        """True when the request may go on; otherwise the 403 answer has been sent."""
        if self._host_ok():
            return True
        self._send(403, {"error": "bad_host"})
        return False

    def _cors(self):
        o = self._origin()
        if o and o in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", o)
            self.send_header("Vary", "Origin")

    def _send(self, status, body):
        data = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _read_raw(self):
        """Read the request body once, up front, so early errors never reset the socket."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._body_error = HttpError(400, {"error": "bad_request", "detail": "bad Content-Length"})
            return b""
        if length > MAX_BODY:
            self._body_error = HttpError(413, {"error": "too_large"})
            return b""
        return self.rfile.read(length) if length > 0 else b""

    def _json_body(self):
        if getattr(self, "_body_error", None):
            raise self._body_error
        raw = getattr(self, "_raw", b"")
        if not raw:
            return {}
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HttpError(400, {"error": "bad_request", "detail": "body must be JSON"})
        if not isinstance(body, dict):
            raise HttpError(400, {"error": "bad_request", "detail": "body must be a JSON object"})
        return body

    def _dispatch(self, routes, method="GET"):
        url = urlparse(self.path)
        q = {k: v[-1] for k, v in parse_qs(url.query).items()}
        fn = routes.get(url.path)
        try:
            if fn is None:
                fn = self.app.extension_routes().get(f"{method} {url.path}")
            if fn is None:
                raise HttpError(404, {"error": "not_found", "path": url.path})
            result = fn(self, q)
            if result is not None:
                self._send(200, result)
        except HttpError as e:
            self._send(e.status, e.body)
        except StaleRevision as e:
            self._send(409, {"error": "stale", "revision": e.revision})
        except GoalLocked as e:
            self._send(403, {"error": "goal_locked", "detail": str(e)})
        except NotAllowed as e:
            self._send(403, {"error": "not_allowed", "detail": str(e)})
        except plan.PlanError as e:
            self._send(400, {"error": "bad_plan", "detail": str(e)})
        except KeyError as e:
            self._send(404, {"error": "not_found", "detail": str(e).strip("'\"")})
        except ValueError as e:
            self._send(400, {"error": "bad_request", "detail": str(e)})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:  # last resort: report, never crash the server thread silently
            try:
                self._send(500, {"error": "internal", "detail": f"{type(e).__name__}: {e}"})
            except OSError:
                pass

    # ------------------------------------------------------------ verbs
    def do_OPTIONS(self):
        if not self._guard_host():
            return
        o = self._origin()
        if o and o not in ALLOWED_ORIGINS:
            return self._send(403, {"error": "origin_not_allowed"})
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, " + TOKEN_HEADER)
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if not self._guard_host():
            return
        self._dispatch(GET_ROUTES)

    def _authenticate(self):
        """The actor for a state-changing request, from its credential; raises 403 HttpError."""
        if not self._host_ok():
            raise HttpError(403, {"error": "bad_host"})
        o = self._origin()
        if o is not None:
            if o in ALLOWED_ORIGINS:
                return OWNER
            raise HttpError(403, {"error": "origin_not_allowed"})
        owner_token = self.headers.get(OWNER_TOKEN_HEADER)
        if owner_token:
            expected = self.app.owner_token()
            if expected and hmac.compare_digest(owner_token.encode("utf-8"), expected.encode("utf-8")):
                self.credential = MOD_CREDENTIAL
                return OWNER
            raise HttpError(403, {"error": "forbidden", "detail": "the owner token does not match"})
        token = self.headers.get(TOKEN_HEADER)
        if token and self.app.token and hmac.compare_digest(token.encode("utf-8"), self.app.token.encode("utf-8")):
            return ROUTER
        raise HttpError(403, {"error": "forbidden",
                              "detail": "state-changing requests need the dsh page Origin or the engine token"})

    def do_POST(self):
        self._raw = self._read_raw()
        if not self._guard_host():
            return
        try:
            self.actor = self._authenticate()
        except HttpError as e:
            return self._send(e.status, e.body)
        self._dispatch(POST_ROUTES, "POST")

    def do_PUT(self):
        self.do_POST()

    do_DELETE = do_PATCH = do_PUT

    # ------------------------------------------------------------ helpers
    def _project(self, q, required=True):
        pid = q.get("project") or self.app.store.default_project_id()
        if required and (not pid or self.app.store.get_project(pid) is None):
            raise HttpError(404, {"error": "not_found", "detail": "no such project"})
        return pid


def _owner_only(h, what):
    if getattr(h, "actor", None) != OWNER:
        raise HttpError(403, {"error": "owner_only", "detail": f"only {config.OWNER_NAME} may {what}"})


def _who(actor):
    return config.OWNER_NAME if actor == OWNER else "The Router"


def _need(body, *keys):
    missing = [k for k in keys if body.get(k) in (None, "")]
    if missing:
        raise HttpError(400, {"error": "bad_request", "detail": "missing " + ", ".join(missing)})


def _revision(body):
    _need(body, "revision")
    rev = body["revision"]
    if isinstance(rev, bool) or not isinstance(rev, int):
        raise HttpError(400, {"error": "bad_request", "detail": "revision must be an integer"})
    return rev


# ---------------------------------------------------------------- GET routes
def r_health(h, q):
    return {"ok": True, "service": "cc-engine", "version": VERSION, "api": API_MAJOR, "api_version": API_VERSION,
            "revision": h.app.store.revision()}


def r_board(h, q):
    return with_loop_fields(h.app.store, h._project(q), build_board(h.app.store, h._project(q)))


LOOP_FIELDS = ("kind", "crosses")


def with_loop_fields(store, project_id, board):
    """W1/W2 UI cues: append each node's ``kind`` and ``crosses`` as extra row columns.

    The board is row-encoded, so annotate the decoded dicts and write the two values back onto
    each row (padded to the full width first). Rows used to be passed to ``annotate`` as if they
    were dicts, which raised; a bare ``except`` hid it and the board never carried ``kind``."""
    from . import node_kinds
    if not board.get("nodes") or not isinstance(board["nodes"][0], list):
        return board
    fields = list(board["fields"])
    extra = {n["id"]: n for n in node_kinds.annotate(store, project_id, decode_nodes(board))}
    rows = []
    for row in board["nodes"]:
        row = list(row) + [None] * (len(fields) - len(row))
        n = extra.get(row[0]) or {}
        kind = n.get("kind")
        row += [kind if kind and kind != "worker" else None, n.get("crosses") or None]  # missing kind = worker (keeps rows small)
        while row and row[-1] is None:
            row.pop()
        rows.append(row)
    return dict(board, fields=fields + list(LOOP_FIELDS), nodes=rows)


def r_node(h, q):
    pid = h._project(q)
    nid = q.get("id")
    node = h.app.store.get_node(pid, nid) if nid else None
    if node is None:
        raise HttpError(404, {"error": "not_found", "detail": f"no node {nid}"})
    node["display_status"] = graph.readiness(h.app.store.list_nodes(pid))[nid]
    node["checks"] = h.app.store.check_counts(pid).get(nid)
    node["claim"] = h.app.store.node_claim(pid, nid)
    out = {"node": node, "events": h.app.store.node_events(pid, nid, 10), "revision": h.app.store.revision()}
    try:  # I2: the clean history the inspector shows
        from . import history
        out["history"] = history.node_history(h.app.store, pid, nid)
    except Exception:
        pass
    return out


def r_projects(h, q):
    store = h.app.store
    rows = store.list_projects(include_archived=q.get("all") == "1")
    return {"projects": [dict(p, **info) for p, info in zip(rows, versions.family_infos(store, rows))]}


def r_history(h, q):
    if not (q.get("family") or q.get("project")):
        raise HttpError(400, {"error": "bad_request", "detail": "give family= or project="})
    try:
        return versions.history(h.app.store, family=q.get("family"), project=q.get("project"))
    except KeyError:
        raise HttpError(404, {"error": "not_found"})


EVENTS_LIMIT = 500


def r_events(h, q):
    """``since`` (default 0); ``latest=N`` returns the newest N (ascending); ``auto=1`` = latest 500."""
    try:
        since = int(q.get("since") or 0)
        latest = int(q.get("latest") or 0)
    except ValueError:
        raise HttpError(400, {"error": "bad_request", "detail": "since and latest must be integers"})
    if q.get("auto") == "1" and not latest:
        latest = EVENTS_LIMIT
    latest = max(0, min(latest, 5000))
    return {"events": h.app.store.events(project_id=q.get("project"), since=since, limit=EVENTS_LIMIT,
                                         latest=latest or None)}


def r_usage(h, q):
    rows = h.app.usage_reader()
    from . import usage
    return {"pools": rows, "clock": usage.reset_clock(rows)}


def r_runs(h, q):
    return {"runs": h.app.store.list_runs()}


def r_questions(h, q):
    from . import needs
    return {"questions": needs.list_questions(h.app.store, open_only=q.get("open") == "1",
                                              project_id=q.get("project"))}


def r_brain(h, q):
    mod = h.app.optional("brain_index")
    if hasattr(mod, "get_graph"):
        return _call(mod.get_graph, refresh=q.get("refresh") == "1")
    return mod.build_graph()


def r_learn(h, q):
    mod = h.app.optional("learn")
    out = {}
    for name in ("concept_graph", "next_step", "stories"):
        fn = getattr(mod, name, None)
        out[name] = _call(fn, store=h.app.store) if callable(fn) else None
    return out


def r_stream(h, q):
    app = h.app
    stream_project = q.get("project") or None
    qu = app.add_queue()
    try:
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream; charset=utf-8")
        h.send_header("Cache-Control", "no-cache")
        h.send_header("Connection", "close")
        h.send_header("X-Accel-Buffering", "no")
        h._cors()
        h.end_headers()

        def emit(kind, data, eid=None):
            msg = f"event: {kind}\n"
            if eid is not None:
                msg += f"id: {eid}\n"
            msg += "data: " + json.dumps(data, separators=(",", ":"), ensure_ascii=False) + "\n\n"
            h.wfile.write(msg.encode("utf-8"))
            h.wfile.flush()

        def state():
            pid = stream_project or app.store.default_project_id()
            board = build_board(app.store, pid)
            emit("state", board, board["revision"])

        state()
        while not app.closing.is_set():
            try:
                item = qu.get(timeout=app.heartbeat)
            except queue.Empty:
                h.wfile.write(b": heartbeat\n\n")
                h.wfile.flush()
                continue
            if item is None:
                break
            kind, payload = item
            if kind == "state":
                p = (payload or {}).get("project")
                if p is None or stream_project is None or p == stream_project:
                    state()
            elif isinstance(kind, str) and kind.replace("_", "").replace("-", "").isalnum():
                emit(kind, payload)  # run, pool, question and any extension's notify(kind, ...)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
        pass
    finally:
        app.drop_queue(qu)
    return None


GET_ROUTES = {
    "/health": r_health, "/api/health": r_health, "/api/board": r_board, "/api/node": r_node,
    "/api/projects": r_projects, "/api/history": r_history, "/api/events": r_events, "/api/usage": r_usage, "/api/runs": r_runs,
    "/api/questions": r_questions, "/api/brain": r_brain, "/api/learn": r_learn, "/api/stream": r_stream,
}


# ---------------------------------------------------------------- POST routes
def p_node_status(h, q):
    b = h._json_body()
    _need(b, "project", "id", "status")
    rev = _revision(b)
    if b["status"] not in STATUSES:
        raise HttpError(400, {"error": "bad_request", "detail": f"status must be one of {list(STATUSES)}"})
    if b["status"] in VERSION_ONLY_STATUSES:  # a version roll-over closes tasks; nobody posts these
        raise HttpError(400, {"error": "bad_request", "detail": f"status {b['status']!r} is set only by a "
                                                                  "version roll-over (use waypoint version new/decide)"})
    if b["status"] == "done":
        _owner_only(h, "mark a task done")
    store = h.app.store
    if store.get_node(b["project"], b["id"]) is None:
        raise HttpError(404, {"error": "not_found", "detail": f"no node {b['id']}"})
    label = b["status"].replace("_", " ")
    new = store.set_node_status(b["project"], b["id"], b["status"], h.actor, b.get("evidence"),
                                f"{_who(h.actor)} set task {b['id']} to {label}.", expected_revision=rev)
    return {"ok": True, "revision": new}


def p_plan_import(h, q):
    b = h._json_body()
    if isinstance(b.get("plan"), dict):  # inline plan dict (MCP cc_draft_plan); validated by import_plan
        source = b["plan"]
        rev = _revision(b)
    else:
        _need(b, "path")
        rev = _revision(b)
        source = Path(b["path"])
        if source.suffix.lower() != ".json" or not source.is_file():
            raise HttpError(400, {"error": "bad_request", "detail": "path must be an existing .json plan file"})
    pid = plan.import_plan(h.app.store, source, actor=h.actor, expected_revision=rev)
    store = h.app.store
    return {"ok": True, "project": pid, "revision": store.revision(),
            "status": store.project_status(pid), "runnable": store.project_runnable(pid)}


def p_plan_accept(h, q):
    """{project, revision?}: Owner accepts a drafted plan so its tasks may run."""
    _owner_only(h, "accept a plan")
    b = h._json_body()
    _need(b, "project")
    rev = _revision(b) if "revision" in b else None
    store = h.app.store
    if store.get_project(b["project"]) is None:
        raise HttpError(404, {"error": "not_found", "detail": "no such project"})
    bad = store.verify_integrity(b["project"])
    if bad:
        raise HttpError(409, {"error": "integrity", "detail": "locked text no longer matches its hash",
                              "mismatches": bad})
    new = plan.accept_plan(store, b["project"], OWNER, expected_revision=rev)
    return {"ok": True, "project": b["project"], "status": store.project_status(b["project"]), "revision": new}


def p_run(h, q):
    # Runs start only from Meridian (Owner, 4 Nov 2026): the Router token may not start one.
    _owner_only(h, "start a run")
    b = h._json_body()
    _need(b, "project")
    store = h.app.store
    if store.get_project(b["project"]) is None:
        raise HttpError(404, {"error": "not_found", "detail": "no such project"})
    raw = b.get("settings") or {}
    if not isinstance(raw, dict):
        raise HttpError(400, {"error": "bad_request", "detail": "settings must be an object"})
    from . import runsheet
    expanded = runsheet.expand(raw)  # ValueError -> 400
    # The actor comes from the credential; any ``requested_by`` in the body is ignored.
    actor = h.actor
    if actor != OWNER and (raw.get("power_off") or expanded.get("power_off")):
        raise HttpError(403, {"error": "owner_only", "detail": f"only {config.OWNER_NAME} may turn on power-off"})
    run_id = "run-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(2)
    run = store.create_run(run_id, b["project"], raw, status="queued")
    pools = ", ".join(expanded["model_ladder"]) or "no pools"
    store.event(b["project"], None, "run", actor,
                f"{_who(actor)} queued a {raw.get('preset', 'balanced')} run using {pools}.",
                {"run": run_id, "requested_by": actor})
    store.notify("run", {"id": run_id, "project": b["project"], "status": run["status"]})
    return {"ok": True, "id": run_id, "status": run["status"], "settings": {"raw": raw, "expanded": expanded},
            "requested_by": actor, "runnable": store.project_runnable(b["project"])}


def p_run_cancel(h, q):
    b = h._json_body()
    _need(b, "id")
    store = h.app.store
    run = store.get_run(b["id"])
    if run is None:
        raise HttpError(404, {"error": "not_found", "detail": "no such run"})
    from . import scheduler
    stopped = scheduler.cancel_run(store, b["id"], actor=h.actor)  # also kills the run's live jobs
    store.event(run["project_id"], None, "run", h.actor, f"{_who(h.actor)} cancelled the run.",
                {"run": b["id"], "stopped": list(stopped or [])})
    store.notify("run", {"id": b["id"], "project": run["project_id"], "status": "cancelled"})
    return {"ok": True, "id": b["id"], "status": "cancelled"}


def p_sprint(h, q):
    _owner_only(h, "plan a sprint")
    b = h._json_body()
    _need(b, "run_id", "pool", "start_at")
    store = h.app.store
    run = store.get_run(b["run_id"])
    if run is None:
        raise HttpError(404, {"error": "not_found", "detail": "no such run"})
    start = _iso_or_400(b["start_at"], "start_at")
    end = _iso_or_400(b["end_at"], "end_at") if b.get("end_at") else None
    sid = "s-" + secrets.token_hex(5)
    store.create_sprint(sid, b["run_id"], b["pool"], start, end)
    store.event(run["project_id"], None, "sprint", h.actor,
                f"{_who(h.actor)} set the {b['pool']} sprint to start at {start}.", {"sprint": sid})
    store.notify("run", {"id": b["run_id"], "sprint": sid, "start_at": start})
    return {"ok": True, "id": sid, "start_at": start}


def p_sprint_update(h, q):
    _owner_only(h, "move a sprint")
    b = h._json_body()
    _need(b, "id", "start_at")
    store = h.app.store
    sprint = store.get_sprint(b["id"])
    if sprint is None:
        raise HttpError(404, {"error": "not_found", "detail": "no such sprint"})
    start = _iso_or_400(b["start_at"], "start_at")
    store.update_sprint(b["id"], start_at=start)
    run = store.get_run(sprint["run_id"]) or {}
    store.event(run.get("project_id"), None, "sprint", h.actor,
                f"{_who(h.actor)} moved the {sprint['pool']} sprint to {start}.", {"sprint": b["id"]})
    store.notify("run", {"id": sprint["run_id"], "sprint": b["id"], "start_at": start})
    return {"ok": True, "id": b["id"], "start_at": start}


def p_question(h, q):
    """Queue a question: {project, node, kind, prompt, options, scale, allowed, proposal}."""
    b = h._json_body()
    _need(b, "prompt")
    from . import needs
    qid = needs.ask(h.app.store, b.get("project"), b.get("node"), b.get("kind", "text"), b["prompt"],
                    options=b.get("options"), scale=b.get("scale"), allowed=b.get("allowed"),
                    proposal=b.get("proposal"), purpose=b.get("purpose"))
    return {"ok": True, "question": needs.get_question(h.app.store, qid)}


def p_question_answer(h, q):
    """{id, answer} with answer {type, value} or a bare value, or {id, type, value}."""
    _owner_only(h, "answer a question")
    b = h._json_body()
    _need(b, "id")
    if "type" in b:
        ans = {"type": b["type"], "value": b.get("value")}
    elif "answer" in b:
        ans = b["answer"]
    else:
        raise HttpError(400, {"error": "bad_request", "detail": "missing answer"})
    from . import needs
    out = _call(needs.answer, store=h.app.store, question_id=b["id"], answer=ans, capture=True,
                runner=h.app.capture_runner, capture_dir=h.app.capture_dir, actor=OWNER)
    return {"ok": True, "question": out}


def p_learn_record(h, q):
    b = h._json_body()
    _need(b, "concept", "outcome")
    mod = h.app.optional("learn")
    result = _call(mod.record, concept_id=b["concept"], outcome=b["outcome"], store=h.app.store)
    return {"ok": True, "result": result}


def p_power_cancel(h, q):
    """Cancel a planned shutdown (``shutdown /a``). Owner only."""
    _owner_only(h, "cancel a planned shutdown")
    from . import power
    ok = bool(power.cancel(runner=h.app.power_runner))
    b = h._json_body()
    summary = (f"{config.OWNER_NAME} cancelled the planned shutdown." if ok
               else f"{config.OWNER_NAME} tried to cancel a shutdown, but none was pending or the command failed.")
    h.app.store.event(b.get("project"), None, "power", OWNER, summary, {"ok": ok})
    return {"ok": ok}


def p_prompt(h, q):
    b = h._json_body()
    _need(b, "project", "id")
    return {"prompt": build_prompt_packet(h.app.store, b["project"], b["id"])}


def p_version_new(h, q):
    """{family?|project?, title?, id?, decisions?, revision}: open the next version (owner or router)."""
    b = h._json_body()
    rev = _revision(b)
    return versions.new_version(h.app.store, family=b.get("family"), project=b.get("project"),
                                title=b.get("title"), id=b.get("id"), decisions=b.get("decisions"),
                                actor=h.actor, expected_revision=rev)


def p_version_family(h, q):
    """{family, title, members, revision}: group existing projects as versions (owner only)."""
    _owner_only(h, "group projects into a family")
    b = h._json_body()
    _need(b, "family", "title", "members")
    rev = _revision(b)
    return versions.set_family(h.app.store, b["family"], b["title"], b["members"], h.actor, rev)


def p_version_decide(h, q):
    """{project, decisions, revision}: carry or skip unfinished tasks of an earlier version."""
    b = h._json_body()
    _need(b, "project", "decisions")
    rev = _revision(b)
    return versions.decide(h.app.store, b["project"], b["decisions"], h.actor, rev)


POST_ROUTES = {
    "/api/version/new": p_version_new, "/api/version/family": p_version_family,
    "/api/version/decide": p_version_decide,
    "/api/node/status": p_node_status, "/api/plan/import": p_plan_import, "/api/plan/accept": p_plan_accept,
    "/api/power/cancel": p_power_cancel, "/api/run": p_run,
    "/api/run/cancel": p_run_cancel, "/api/sprint": p_sprint, "/api/sprint/update": p_sprint_update,
    "/api/question": p_question, "/api/question/answer": p_question_answer, "/api/learn/record": p_learn_record, "/api/prompt": p_prompt,
}


WARM_UP_DELAY = 10.0


def _warm_up(srv, background, start_refresher, delay=WARM_UP_DELAY):
    """Start-up work that must never delay binding the port or answering /health.

    First the owner token that ``start()`` wrote is made user-only (``icacls``). The heavy steps wait
    until the first answer has gone out (or ``delay`` seconds): importing ~20 extension modules holds the GIL for seconds on a busy machine and
    would otherwise make the first /health wait behind it."""
    app = srv.app
    try:
        if app.owner_token() and app.owner_token_path.exists():  # start() wrote it; make sure it is user-only
            _restrict_to_user(app.owner_token_path)
    except Exception as exc:
        print(f"engine warm-up: owner token failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    answered = getattr(srv, "answered", None)
    if answered is not None:
        answered.wait(delay)
    steps = [app.attach_snapshots]
    if start_refresher:  # slow usage sources refresh in the background only
        def refresher():
            from . import usage
            srv.refresher = usage.start_refresher()
        steps.append(refresher)
    steps += list(background or ())
    steps.append(app.extension_routes)  # import the extension modules now, not on the first request
    steps.append(lambda: start_background(app))
    for step in steps:
        if app.closing.is_set():
            return
        try:
            step()
        except Exception as exc:  # one failed step never stops the rest or the engine
            print(f"engine warm-up: {getattr(step, '__name__', 'step')} failed: {type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)


def start_background(app, names=None):
    """Call ``start(app)`` on each ``BACKGROUND`` extension once; keep the handles for ``app.close``."""
    handles = app.__dict__.setdefault("background_handles", [])
    for name in (names if names is not None else BACKGROUND):
        if app.closing.is_set():
            return handles
        try:
            mod = app.import_module(f"cc_engine.{name}")
            fn = getattr(mod, "start", None)
            if callable(fn):
                handle = fn(app)
                if handle is not None:
                    handles.append(handle)
        except Exception as exc:  # one failed background task never stops the engine
            print(f"engine warm-up: {name}.start failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    return handles


def start(store, host="127.0.0.1", port=DEFAULT_PORT, background=None, start_refresher=True,
          warm_up_delay=WARM_UP_DELAY, **app_kwargs):
    """Bind the port and return the server, ready for ``serve_forever``. A missing owner token is written
    first (cheap; the warm-up thread runs its ``icacls``), so the CLI and the mod find it as soon as
    /health answers. Everything slow (snapshots, usage refresher, ``background`` callables such as the
    CLI bridge, extension imports) runs in a daemon warm-up thread."""
    if "token_path" not in app_kwargs:
        app_kwargs["token_path"] = default_token_path()
        app_kwargs.setdefault("owner_token_path", default_owner_token_path())
    owner_path = (Path(app_kwargs["owner_token_path"]) if app_kwargs.get("owner_token_path")
                  else Path(app_kwargs["token_path"]).with_name("owner.token"))
    app_kwargs["owner_token_path"] = owner_path
    if not app_kwargs.get("owner_token"):
        app_kwargs["owner_token"] = prepare_owner_token(owner_path)
    srv = make_server(store, host, port, **app_kwargs)
    srv.refresher = None
    srv.warm_up = threading.Thread(target=_warm_up, args=(srv, background, start_refresher, warm_up_delay),
                                   daemon=True, name="engine-warm-up")
    srv.warm_up.start()
    return srv


def serve(store, host="127.0.0.1", port=DEFAULT_PORT, background=None, **app_kwargs):
    srv = start(store, host, port, background=background, **app_kwargs)
    print(f"cc-engine listening on http://{host}:{srv.server_address[1]}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.app.close()
        if srv.refresher is not None:
            srv.refresher.stop()
        srv.server_close()
