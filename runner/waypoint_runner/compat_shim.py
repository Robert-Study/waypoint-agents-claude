"""Optional read-only compatibility shim for the old engine address 127.0.0.1:8751.

Serves GET /api/board, /api/projects and /api/health straight from the Waypoint data folder, shaped like
core/docs/API.md for those routes. Every write (POST, PUT, PATCH, DELETE) answers 405 and points at the runner
and the web page. Binds 127.0.0.1 only. Never started by default: nothing in 1.2 depends on it.

Lifetime: the shim runs on a daemon thread. The runner owns it and calls stop() on exit. Run it by hand with
`python -m waypoint_runner.compat_shim --root <data folder>`; Ctrl+C stops it.

Known gaps versus the engine (documented, not hidden): `checks` and `crosses` are null, `edges` are every
dependency edge (not transitively reduced), `integrity` is not verified, and `claim.stale` is null. The response
carries `shim.integrity_checked: false` so a caller can tell.
"""
import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Data access goes only through the Python data-pyclient in core/waypoint-data/py (runner rule).
_DATA_PY = Path(__file__).resolve().parents[2] / "core" / "waypoint-data" / "py"
if str(_DATA_PY) not in sys.path:
    sys.path.insert(0, str(_DATA_PY))

from waypoint_data import io as wio  # noqa: E402
from waypoint_data.store import Store  # noqa: E402

VERSION = "1.2.0"
HOST = "127.0.0.1"
DEFAULT_PORT = 8751

BOARD_FIELDS = ["id", "title", "status", "display_status", "deps", "wave", "pool", "model", "effort",
                "attempts", "actor", "lane", "checks", "recommended", "claim", "kind", "crosses"]
COUNT_KEYS = ["not_started", "ready", "running", "submitted", "done", "error", "needs_you",
              "skipped", "carried"]
WRITE_MESSAGE = ("read-only compatibility shim: writes go through the Waypoint runner or the web page, "
                 "not this address")


def _cwd_text(cwd):
    if isinstance(cwd, dict):
        return f"{cwd.get('machine')}/{cwd.get('rel')}"
    return cwd


def _project_row(store, p):
    family = p.get("family")
    family_title = None
    if family:
        fam = store.read(f"families/{family}.json")
        family_title = fam.get("title") if fam else None
    return {
        "id": p["id"], "title": p.get("title"), "brief": p.get("brief"), "goal_hash": p.get("goal_hash"),
        "cwd": _cwd_text(p.get("cwd")), "created_at": p.get("created_at"),
        "archived": 1 if p.get("archived") else 0, "status": p.get("status"),
        "family": family, "family_title": family_title, "version": p.get("version"),
    }


def _project_dirs(store):
    """Project folders are directories (projects/<id>/project.json), so list directories, not files."""
    base = store.path("projects")
    try:
        names = os.listdir(base)
    except (FileNotFoundError, NotADirectoryError):
        return []
    return sorted(n for n in names if not wio.is_ignored_name(n) and os.path.isdir(os.path.join(base, n)))


def list_projects(store, include_archived=False):
    rows = []
    for name in _project_dirs(store):
        p = store.read(f"projects/{name}/project.json")
        if p and p.get("id") and (include_archived or not p.get("archived")):
            rows.append(_project_row(store, p))
    rows.sort(key=lambda r: r["id"])
    return {"projects": rows}


def default_project_id(store):
    rows = list_projects(store)["projects"]
    return rows[0]["id"] if rows else None


def _claims_by_node(store, project_id):
    out = {}
    for name in store.list_names("claims"):
        c = store.read(f"claims/{name}")
        if not c or c.get("project") != project_id:
            continue
        # The owner token is deliberately never copied into the payload.
        out[c.get("node")] = {"by": c.get("owner"), "worktree": None, "cwd": None,
                              "environment": c.get("location"), "since": c.get("heartbeat_at"),
                              "refreshed_at": c.get("heartbeat_at"), "stale": None}
    return out


def _dep_pairs(n):
    """Dependencies as (id, crosses) pairs. The folder stores a dep as a bare id or as {"id", "crosses"}."""
    out = []
    for d in n.get("deps") or []:
        if isinstance(d, dict):
            out.append((d.get("id"), d.get("crosses")))
        else:
            out.append((d, None))
    return [(i, c) for i, c in out if i]


def _waves(by_id):
    memo, visiting = {}, set()

    def depth(nid):
        if nid in memo:
            return memo[nid]
        if nid in visiting:  # cycle guard: treat as a root for display
            return 0
        visiting.add(nid)
        deps = [d for d, _ in _dep_pairs(by_id[nid]) if d in by_id]
        memo[nid] = 0 if not deps else 1 + max(depth(d) for d in deps)
        visiting.discard(nid)
        return memo[nid]

    return {nid: depth(nid) for nid in by_id}


def board(store, project_id):
    p = store.read(f"projects/{project_id}/project.json")
    if not p or not p.get("id"):
        return None
    nodes = [n for n in store.list_entities(f"projects/{project_id}/nodes") if n.get("id")]
    by_id = {n["id"]: n for n in nodes}
    status_of = {nid: (n.get("status") or "not_started") for nid, n in by_id.items()}
    waves = _waves(by_id)
    claims = _claims_by_node(store, project_id)

    rows, counts = [], {k: 0 for k in COUNT_KEYS}
    for nid in sorted(by_id):
        n = by_id[nid]
        pairs = _dep_pairs(n)
        deps = [d for d, _ in pairs]
        status = status_of[nid]
        display = status
        if status == "not_started" and all(status_of.get(d) == "done" for d in deps):
            display = "ready"
        counts[display] = counts.get(display, 0) + 1
        kind = n.get("kind")
        row = [nid, (n.get("title") or "")[:80], status, display, deps, waves[nid], n.get("pool"),
               n.get("model"), n.get("effort"), n.get("attempts"), n.get("actor"), n.get("lane"), None,
               n.get("recommended"), claims.get(nid), None if kind in (None, "worker") else kind,
               [c for _, c in pairs] if any(c for _, c in pairs) else None]
        while row and row[-1] is None:  # trailing nulls are left off, as the engine does
            row.pop()
        rows.append(row)

    edges = [[d, nid] for nid in sorted(by_id) for d, _ in _dep_pairs(by_id[nid]) if d in by_id]
    counts["total"] = len(nodes)
    return {
        "project": {"id": p["id"], "title": p.get("title"), "cwd": _cwd_text(p.get("cwd")),
                    "family": p.get("family"), "family_title": _project_row(store, p)["family_title"],
                    "version": p.get("version"), "status": p.get("status"), "draft": False,
                    "archived": bool(p.get("archived"))},
        "revision": p.get("rev", 0),
        "fields": BOARD_FIELDS,
        "nodes": rows,
        "edges": edges,
        "counts": counts,
        "integrity_ok": True,
        "integrity": [],
        "shim": {"read_only": True, "integrity_checked": False},
    }


def health():
    return {"ok": True, "service": "waypoint-compat-shim", "version": VERSION, "api": 1,
            "api_version": "1.1", "read_only": True}


class _Handler(BaseHTTPRequestHandler):
    server_version = "WaypointCompatShim"
    sys_version = ""

    def log_message(self, format, *args):  # keep test and console output quiet
        pass

    def _send(self, status, payload, extra_headers=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        store = self.server.store
        try:
            if url.path in ("/api/health", "/health"):
                return self._send(200, health())
            if url.path == "/api/projects":
                include = query.get("all", ["0"])[0] in ("1", "true")
                return self._send(200, list_projects(store, include_archived=include))
            if url.path == "/api/board":
                pid = (query.get("project") or [None])[0] or default_project_id(store)
                payload = board(store, pid) if pid else None
                if payload is None:
                    return self._send(404, {"error": f"unknown project: {pid}"})
                return self._send(200, payload)
            return self._send(404, {"error": "unknown route; this shim serves GET /api/board, "
                                             "/api/projects and /api/health only"})
        except wio.WaypointIOError:
            return self._send(503, {"error": "folder busy; retry"}, {"Retry-After": "2"})
        except Exception:  # a bad file must not drop the connection; the message stays generic
            return self._send(500, {"error": "shim could not read the folder; see the runner log"})

    def _refuse(self):
        # Drain any request body first so the client gets the 405 instead of a reset connection.
        length = int(self.headers.get("Content-Length") or 0)
        if length > 0:
            self.rfile.read(min(length, 1 << 20))
        self.close_connection = True
        self._send(405, {"error": WRITE_MESSAGE}, {"Allow": "GET", "Connection": "close"})

    do_POST = _refuse
    do_PUT = _refuse
    do_PATCH = _refuse
    do_DELETE = _refuse


class CompatShim:
    """Read-only 8751 shim. Constructing it binds nothing; start() opens 127.0.0.1 on a daemon thread."""

    def __init__(self, root, host=HOST, port=DEFAULT_PORT):
        if host != HOST:
            raise ValueError("the compatibility shim binds 127.0.0.1 only")
        self.root = str(root)
        self.host = host
        self.port = port
        self.server = None
        self._thread = None

    @property
    def url(self):
        if self.server is None:
            return None
        return f"http://{self.host}:{self.server.server_address[1]}"

    def start(self):
        if self.server is not None:
            return self
        srv = ThreadingHTTPServer((self.host, self.port), _Handler)
        srv.daemon_threads = True
        srv.store = Store(self.root)  # no writer: this process never writes to the folder
        self.server = srv
        self._thread = threading.Thread(target=srv.serve_forever, name="compat-shim", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Read-only 8751 compatibility shim (optional).")
    ap.add_argument("--root", required=True, help="Waypoint data folder")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args(argv)
    shim = CompatShim(args.root, port=args.port).start()
    print(f"compat shim (read-only) on {shim.url}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        shim.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
