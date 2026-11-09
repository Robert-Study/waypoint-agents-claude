"""Serve the Waypoint web page and the folder HTTP API from the runner, on loopback only.

Implements ``core/docs/data/HTTP_FOLDER_API.md`` exactly (Host and Origin checks, session header, If-Match
compare-and-swap, 1 MiB body cap, entity folders only). The data folder is touched only through the data-pyclient
(``waypoint_data``); every write is an atomic write through ``Store``. Stdlib only.

``serve_page(port, data_dir, web_root=..., lock=...)`` starts the server on a background thread and, when a runner
lock is passed, records the bound URL with ``lock.update({"url": ...})`` and shuts itself down when the lock is
released, so the page lives exactly as long as the runner.

The session token is random per server start, lives in memory, and is handed to the page only inside its HTML
(``<meta name="waypoint-session">``). It is never written to the folder, a URL or a log. No owner token exists here.
"""
from __future__ import annotations

import hmac
import json
import mimetypes
import os
import re
import secrets
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

_DATA_PY = Path(__file__).resolve().parents[2] / "core" / "waypoint-data" / "py"
if str(_DATA_PY) not in sys.path:  # the data client lives in the repo, not in site-packages
    sys.path.insert(0, str(_DATA_PY))

from waypoint_data import io as wio  # noqa: E402
from waypoint_data.claims import Claims  # noqa: E402
from waypoint_data.store import LockBusy, RevConflict, Store, WriteLocked, acquire_lock  # noqa: E402

DEFAULT_PORT = 8760  # REVERSIBLE_AGENT_DEFAULT; undo: change settings.web_url
MAX_BODY = 1024 * 1024
LIST_MAX = 500
SESSION_HEADER = "X-Waypoint-Session"
CLAIM_TOKEN_HEADER = "X-Waypoint-Claim-Token"

# Entity folders of the data folder that the API can reach (file contract). Nothing else is reachable.
ENTITY_DIRS = frozenset({
    "actions", "agents", "brain", "briefs", "chats", "claims", "dispatch", "families", "health", "learn",
    "orchestrators", "plan_stages", "projects", "questions", "receipts", "runs", "schedules", "sessions",
    "settings", "snapshots", "todos", "usage", "versions", "events", "floors", "cloud",
})
_EXT_TYPES = {".json": "application/json", ".ndjson": "application/x-ndjson"}
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}\.ndjson$")
_NODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_STATUS = {"bad_path": 400, "bad_origin": 403, "no_session": 401, "not_found": 404, "rev_conflict": 412,
           "precondition_required": 428, "too_large": 413, "bad_schema": 422, "lock_busy": 423,
           "write_locked": 503, "claim_held": 409, "bad_host": 421, "forbidden": 403, "method_not_allowed": 405}


class ApiError(Exception):
    def __init__(self, code, message, current=None, status=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.current = current
        self.status = status or _STATUS[code]


def is_loopback_host(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1", "[::1]")


# ----------------------------------------------------------------------------- path rules

def clean_rel(raw: str, *, allow_empty=False) -> str:
    """Relative data-folder path from a URL tail. Raises ApiError bad_path or not_found."""
    rel = unquote(raw)  # decoded exactly once
    if "\x00" in rel or "\\" in rel or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        raise ApiError("bad_path", "bad path")
    parts = rel.split("/")
    if parts and parts[-1] == "" and allow_empty:
        parts = parts[:-1]
    if not parts and allow_empty:
        return ""
    if not parts or any(p in ("", ".", "..") for p in parts):
        raise ApiError("bad_path", "bad path")
    if parts[0].lower() not in ENTITY_DIRS:
        raise ApiError("not_found", "not found")
    if any(wio.is_ignored_name(p) for p in parts):
        raise ApiError("not_found", "not found")
    return "/".join([parts[0].lower()] + parts[1:])


def check_file_rel(rel: str) -> str:
    ext = os.path.splitext(rel)[1].lower()
    if ext not in _EXT_TYPES or "/" not in rel:
        raise ApiError("not_found", "not found")
    return rel


def _strong_etag(rev) -> str:
    return f'"{rev}"'


def _parse_etag(value):
    """Revision int from a header like "7" (quotes and W/ tolerated); None when absent or not a revision."""
    if value is None:
        return None
    v = value.strip()
    if v.startswith("W/"):
        v = v[2:]
    v = v.strip('"')
    return int(v) if v.isdigit() else None


# ----------------------------------------------------------------------------- the server

class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, addr, app):
        self.app = app
        self.address_family = socket.AF_INET6 if ":" in addr[0] else socket.AF_INET
        super().__init__(addr, _Handler)

    def handle_error(self, request, client_address):  # dropped or malformed connections: no traceback on stderr
        return


class PageServer:
    """A running page and API server. ``url`` is the bound loopback URL; ``token`` is the session token."""

    def __init__(self, data_dir, web_root=None, host="127.0.0.1", port=DEFAULT_PORT, token=None, lock=None,
                 now=time.time, watch_interval=0.25):
        if not is_loopback_host(host):
            raise ValueError("the page server binds to loopback only (127.0.0.1 or ::1)")
        bind = "127.0.0.1" if host == "localhost" else host.strip("[]")
        self.data_dir = os.path.abspath(data_dir)
        self.web_root = os.path.abspath(web_root) if web_root else None
        self.store = Store(self.data_dir, writer="web", now=now)
        self.claims = Claims(self.data_dir, now=now, writer="web")
        self.token = token or secrets.token_urlsafe(32)
        self.lock = lock
        self.now = now
        self._httpd = _Server((bind, port), self)
        self.host = bind
        self.port = self._httpd.server_address[1]
        label = f"[{bind}]" if ":" in bind else bind
        self.url = f"http://{label}:{self.port}"
        self.allowed_hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}"}
        self.allowed_origins = {f"http://{h}" for h in self.allowed_hosts}
        self._thread = None
        self._watch = None
        self._stopped = threading.Event()
        self._stopping = False
        self._watch_interval = watch_interval

    # -- lifecycle
    def start(self):
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.1},
                                        name="waypoint-page", daemon=True)
        self._thread.start()
        if self.lock is not None:
            self.lock.update({"url": self.url})
            self._watch = threading.Thread(target=self._watch_lock, name="waypoint-page-watch", daemon=True)
            self._watch.start()
        return self

    def _watch_lock(self):
        # The page exits with the runner: when the runner lock is released (or lost) the server stops.
        while not self._stopping and not self._stopped.wait(self._watch_interval):
            if not getattr(self.lock, "held", True):
                threading.Thread(target=self.stop, daemon=True).start()
                return

    def stop(self):
        if self._stopping:
            return
        self._stopping = True
        try:
            self._httpd.shutdown()
        except Exception:
            pass
        self._httpd.server_close()
        if self.lock is not None and getattr(self.lock, "held", False):
            try:
                self.lock.update({"url": None})
            except Exception:
                pass
        self._stopped.set()  # last: waiters see a server that is fully down

    def wait(self, timeout=None):
        return self._stopped.wait(timeout)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # -- helpers used by the handler
    def abs_path(self, rel):
        """Absolute path for a validated rel path; refuses anything that resolves outside the root (symlinks)."""
        p = self.store.path(rel)
        root = os.path.realpath(self.data_dir)
        real = os.path.realpath(p)
        if real != root and not real.startswith(root + os.sep):
            raise ApiError("bad_path", "bad path")
        return p


def serve_page(port=DEFAULT_PORT, data_dir=None, web_root=None, lock=None, host="127.0.0.1", settings=None,
               token=None, now=time.time) -> PageServer:
    """Start the page server on 127.0.0.1:<port> (0 picks a free port) and return it, already running.

    ``web_root`` falls back to ``settings["web_root"]`` then ``web/waypoint-web/dist`` in the repo. With ``lock`` (the
    runner's ``RunnerLock``) the bound URL is recorded in the lock file and the server stops when the lock is released.
    """
    if data_dir is None:
        raise ValueError("data_dir is required")
    root = web_root or (settings or {}).get("web_root") or str(Path(__file__).resolve().parents[2] / "web" / "waypoint-web" / "dist")
    return PageServer(data_dir, root, host=host, port=port, token=token, lock=lock, now=now).start()


class _Handler(BaseHTTPRequestHandler):
    server_version = "WaypointPage"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # no request logging: URLs and headers can carry state
        return

    @property
    def app(self) -> PageServer:
        return self.server.app

    # -- plumbing
    def _send(self, status, body=b"", ctype="application/json", headers=None):
        self.send_response(status)
        origin = self.headers.get("Origin")
        if origin in self.app.allowed_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if self._close:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, obj, headers=None):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), headers=headers)

    def _error(self, err: ApiError):
        self._json(err.status, {"error": err.code, "message": err.message, "current": err.current})

    def _guard(self, need_session):
        host = self.headers.get("Host", "")
        if host.lower() not in self.app.allowed_hosts:
            raise ApiError("bad_host", "host not allowed")
        origin = self.headers.get("Origin")
        if origin is not None and origin not in self.app.allowed_origins:
            raise ApiError("bad_origin", "origin not allowed")
        if need_session:
            got = self.headers.get(SESSION_HEADER) or ""
            if not hmac.compare_digest(got.encode("utf-8"), self.app.token.encode("utf-8")):
                raise ApiError("no_session", "missing or wrong session token")

    def _body(self):
        raw = self.headers.get("Content-Length")
        if raw is None or not raw.isdigit():
            raise ApiError("bad_schema", "a request body with Content-Length is required", status=411)
        n = int(raw)
        if n > MAX_BODY:
            self._close = True
            raise ApiError("too_large", "body over 1 MiB")
        return self.rfile.read(n)

    def _json_body(self):
        try:
            obj = json.loads(self._body().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError("bad_schema", "body is not valid JSON")
        if not isinstance(obj, dict):
            raise ApiError("bad_schema", "body must be a JSON object")
        return obj

    # -- dispatch
    def _handle(self):
        self._close = False
        try:
            path = urlsplit(self.path)
            p = path.path
            if self.command == "OPTIONS":
                return self._preflight()
            is_api = p.startswith(("/files/", "/list/", "/append/", "/rename", "/claims/"))
            self._guard(need_session=is_api)
            if not is_api:
                if self.command not in ("GET", "HEAD"):
                    raise ApiError("method_not_allowed", "method not allowed")
                return self._static(p)
            q = parse_qs(path.query)
            if p.startswith("/files/"):
                rel = check_file_rel(clean_rel(p[len("/files/"):]))
                return {"GET": self._get_file, "HEAD": self._get_file, "PUT": self._put_file,
                        "DELETE": self._delete_file}.get(self.command, self._no_method)(rel)
            if p.startswith("/list/") and self.command == "GET":
                return self._list(clean_rel(p[len("/list/"):], allow_empty=False), q)
            if p.startswith("/append/") and self.command == "POST":
                return self._append(p[len("/append/"):])
            if p == "/rename" and self.command == "POST":
                return self._rename()
            m = re.match(r"^/claims/([^/]+)/reclaim$", p)
            if m and self.command == "POST":
                return self._reclaim(unquote(m.group(1)))
            raise ApiError("not_found", "not found") if self.command in ("GET", "HEAD", "POST") else ApiError(
                "method_not_allowed", "method not allowed")
        except ApiError as err:
            self._error(err)
        except RevConflict as exc:
            self._error(ApiError("rev_conflict", "revision conflict", exc.current))
        except LockBusy:
            self._error(ApiError("lock_busy", "file is locked by another writer"))
        except WriteLocked:
            self._error(ApiError("write_locked", "file is locked, retry"))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:  # never a stack trace, path or environment value in the body
            self._error(ApiError("write_locked", "internal error", status=500))

    do_GET = do_HEAD = do_PUT = do_POST = do_DELETE = _handle

    def do_OPTIONS(self):
        self._close = False
        self._preflight()

    def _no_method(self, *_):
        raise ApiError("method_not_allowed", "method not allowed")

    def _preflight(self):
        origin = self.headers.get("Origin")
        if self.headers.get("Host", "").lower() not in self.app.allowed_hosts:
            return self._error(ApiError("bad_host", "host not allowed"))
        if origin not in self.app.allowed_origins:
            return self._error(ApiError("bad_origin", "origin not allowed"))
        self._send(204, b"", headers={
            "Access-Control-Allow-Methods": "GET, PUT, POST, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": f"If-Match, If-None-Match, Content-Type, {SESSION_HEADER}, {CLAIM_TOKEN_HEADER}",
            "Access-Control-Max-Age": "600"})

    # -- static page
    def _static(self, p):
        root = self.app.web_root
        if not root or not os.path.isdir(root):
            raise ApiError("not_found", "no web page is installed")
        rel = unquote(p).lstrip("/")
        if rel in ("", "app", "app/"):
            rel = "index.html"
        if rel.startswith("app/"):
            rel = rel[4:] or "index.html"
        if "\x00" in rel or "\\" in rel or any(s == ".." for s in rel.split("/")):
            raise ApiError("bad_path", "bad path")
        full = os.path.realpath(os.path.join(root, *rel.split("/")))
        base = os.path.realpath(root)
        if full != base and not full.startswith(base + os.sep):
            raise ApiError("bad_path", "bad path")
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        if not os.path.isfile(full):
            raise ApiError("not_found", "not found")
        with open(full, "rb") as fh:
            data = fh.read()
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if full.lower().endswith((".js", ".mjs")):
            ctype = "text/javascript"
        if full.lower().endswith(".html"):
            meta = f'<meta name="waypoint-session" content="{self.app.token}">'.encode("ascii")
            low = data.lower()
            i = low.find(b"<head>")
            data = data[:i + 6] + meta + data[i + 6:] if i >= 0 else meta + data
            ctype = "text/html"
        if ctype.startswith("text/") or ctype in ("application/json", "image/svg+xml"):
            ctype += "; charset=utf-8"
        self._send(200, data, ctype)

    # -- files
    def _read_entry(self, rel):
        """(bytes, content type, etag) for a file, or raise not_found."""
        p = self.app.abs_path(rel)
        if not os.path.isfile(p):
            raise ApiError("not_found", "not found")
        ext = os.path.splitext(rel)[1].lower()
        with open(p, "rb") as fh:
            data = fh.read()
        rev = None
        if ext == ".json":
            try:
                obj = json.loads(data.decode("utf-8"))
                rev = obj.get("rev") if isinstance(obj, dict) else None
            except (ValueError, UnicodeDecodeError):
                raise ApiError("write_locked", "file is being written, retry")
        if isinstance(rev, int) and not isinstance(rev, bool):
            etag = _strong_etag(rev)
        else:
            st = os.stat(p)
            etag = f'"{st.st_size}-{int(st.st_mtime * 1000)}"'
        return data, _EXT_TYPES[ext], etag

    def _get_file(self, rel):
        data, ctype, etag = self._read_entry(rel)
        inm = self.headers.get("If-None-Match")
        if inm and etag in [t.strip() for t in inm.split(",")]:
            return self._send(304, b"", ctype, {"ETag": etag})
        self._send(200, data, ctype, {"ETag": etag})

    def _put_file(self, rel):
        if rel.lower().endswith(".ndjson"):
            raise ApiError("method_not_allowed", "logs are append-only")
        if_match = self.headers.get("If-Match")
        none_match = (self.headers.get("If-None-Match") or "").strip()
        if if_match is None and none_match != "*":
            raise ApiError("precondition_required", "If-Match (or If-None-Match: *) is required")
        expected = 0 if if_match is None else _parse_etag(if_match)
        if expected is None:
            raise ApiError("precondition_required", "If-Match must be a quoted revision")
        entity = self._json_body()
        schema = entity.get("schema")
        if schema is not None and not (isinstance(schema, str) and schema.startswith("waypoint.")):
            raise ApiError("bad_schema", "schema must be a waypoint.* id")
        for k in ("rev", "updated_at", "updated_by"):  # the server owns these; client values are ignored
            entity.pop(k, None)
        self.app.abs_path(rel)
        creating = if_match is None
        stored = self.app.store.write(rel, entity, expected_rev=expected)
        self._json(201 if creating else 200, stored, {"ETag": _strong_etag(stored.get("rev", 0))})

    def _delete_file(self, rel):
        if not rel.startswith("claims/"):
            raise ApiError("method_not_allowed", "this file cannot be deleted")
        if self.headers.get("If-Match") is None:
            raise ApiError("precondition_required", "If-Match is required")
        expected = _parse_etag(self.headers.get("If-Match"))
        node = rel.split("/", 1)[1][:-len(".json")] if rel.endswith(".json") else ""
        if rel.count("/") != 1 or not _NODE.match(node):
            raise ApiError("method_not_allowed", "this file cannot be deleted")
        token = self.headers.get(CLAIM_TOKEN_HEADER) or ""
        cur = self.app.store.read(rel)
        if cur is None:
            raise ApiError("not_found", "not found")
        if cur.get("rev") != expected:
            raise ApiError("rev_conflict", "revision conflict", cur)
        if not token or cur.get("token") != token:
            raise ApiError("claim_held", "claim token does not match", status=409)
        if not self.app.claims.release(cur.get("project"), node, token):
            raise ApiError("lock_busy", "claim could not be released")
        self._json(200, {"released": True})

    def _reclaim(self, node):
        if not _NODE.match(node):
            raise ApiError("bad_path", "bad node id")
        rel = f"claims/{node}.json"
        path = self.app.abs_path(rel)
        cur = self.app.store.read(rel)
        if cur is None:
            raise ApiError("not_found", "no claim to reclaim")
        if not self.app.claims.is_stale(cur):
            raise ApiError("claim_held", "claim is live", cur, status=409)
        lock = acquire_lock(path + ".lock", owner="web", wait_s=2.0, now=self.app.now)
        try:
            again = self.app.store.read(rel)
            if again is None or again.get("token") != cur.get("token") or not self.app.claims.is_stale(again):
                raise ApiError("claim_held", "claim is live", again, status=409)
            wio.remove_if_exists(path)
        finally:
            lock.release()
        self._json(200, {"reclaimed": True})

    # -- list, append, rename
    def _list(self, rel_dir, q):
        d = self.app.abs_path(rel_dir)
        if not os.path.isdir(d):
            raise ApiError("not_found", "not found")
        try:
            limit = max(1, min(int((q.get("limit") or [LIST_MAX])[0]), LIST_MAX))
        except ValueError:
            raise ApiError("bad_path", "limit must be a number")
        after = (q.get("after") or [""])[0]
        out = []
        for name in sorted(os.listdir(d)):
            if wio.is_ignored_name(name) or name <= after and after:
                continue
            full = os.path.join(d, name)
            if os.path.isdir(full):
                if os.path.islink(full) or name.startswith("_"):
                    continue
                out.append({"name": name, "kind": "dir", "size": 0, "mtimeMs": int(os.stat(full).st_mtime * 1000), "rev": None})
            elif os.path.splitext(name)[1].lower() in _EXT_TYPES and os.path.isfile(full):
                st = os.stat(full)
                rev = None
                if name.lower().endswith(".json"):
                    try:
                        obj = wio.read_json(full, torn_wait_s=0.01, retries=1)
                        rev = obj.get("rev") if isinstance(obj, dict) else None
                    except Exception:
                        rev = None
                out.append({"name": name, "kind": "file", "size": st.st_size, "mtimeMs": int(st.st_mtime * 1000), "rev": rev})
            if len(out) >= limit:
                break
        self._json(200, out)

    def _append(self, raw):
        rel = unquote(raw)
        parts = rel.split("/")
        if "\x00" in rel or "\\" in rel or len(parts) != 3 or parts[0] != "events" or parts[1] != "web" \
                or not _DAY.match(parts[2]):
            raise ApiError("forbidden", "the page can append only to events/web/<day>.ndjson")
        event = self._json_body()
        kind = event.get("kind")
        if not isinstance(kind, str) or not kind:
            raise ApiError("bad_schema", "event needs a kind")
        os.makedirs(os.path.dirname(self.app.abs_path(rel)), exist_ok=True)
        line = self.app.store.append_event("web", event, day=parts[2][:-len(".ndjson")])
        self._json(200, line)

    def _rename(self):
        body = self._json_body()
        src = check_file_rel(clean_rel(str(body.get("from", ""))))
        dst = check_file_rel(clean_rel(str(body.get("to", ""))))
        if self.headers.get("If-Match") is None:
            raise ApiError("precondition_required", "If-Match is required")
        expected = _parse_etag(self.headers.get("If-Match"))
        if not src.lower().endswith(".json") or not dst.lower().endswith(".json") or src.split("/")[0] != dst.split("/")[0]:
            raise ApiError("bad_path", "a rename stays inside one entity folder and keeps .json")
        sp, dp = self.app.abs_path(src), self.app.abs_path(dst)
        lock = acquire_lock(sp + ".lock", owner="web", wait_s=2.0, now=self.app.now)
        try:
            cur = self.app.store.read(src)
            if cur is None:
                raise ApiError("not_found", "not found")
            if cur.get("rev") != expected:
                raise ApiError("rev_conflict", "revision conflict", cur)
            if os.path.exists(dp):
                raise ApiError("rev_conflict", "target exists", self.app.store.read(dst))
            os.makedirs(os.path.dirname(dp), exist_ok=True)
            os.rename(sp, dp)
        finally:
            lock.release()
        self._json(200, cur, {"ETag": _strong_etag(cur.get("rev", 0))})
