"""The dsh Waypoint tab's loopback server: Waypoint Claude's tools and page for dsh, without the retired engine.

In Waypoint 1.2 the always-on engine (127.0.0.1:8751) is retired, so the dsh plugin needs its own small backend.
This server runs the Waypoint Claude MCP tools (``mcp_board.py``, ``ops_tools.py``, the preferences add-on) in-process over the 1.2 data folder, so the dsh
Waypoint tab shows the same live data and actions as Waypoint Claude, and serves ``artifacts/waypoint-claude.html``
unchanged, so dsh always has the current UI.

- ``GET /api/wpc/page``: the Waypoint Claude page (text/html).
- ``POST /api/wpc/call`` ``{"tool", "input"}`` -> ``{"payload", "isError"}``. Only the page's tools; only the dsh
  page Origin (``http://127.0.0.1:3080``) may call, and its writes are recorded as ``by="owner"``.
- ``GET /api/wpc/features``: the tools this install offers and which add-ons are on (preferences, toolbox).
- ``GET /health``.

The preferences tools are an optional add-on: they load from the folder named by WAYPOINT_PREFS_TOOLS or
``prefs_tools`` in ~/.waypoint/config.json; without it the server runs and those tools answer ``addon_off``.

Every request must name this server in ``Host`` (DNS-rebinding guard). Binds 127.0.0.1 only. Stdlib only.
Run: ``python -m waypoint_runner.wpc_http [--folder <data folder>] [--port 8762]``; a busy port means one is already up.
"""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PAGE = REPO / "artifacts" / "waypoint-claude.html"
DEFAULT_PORT = 8762
MAX_BODY = 1 << 20
ALLOWED_ORIGINS = frozenset({"http://127.0.0.1:3080", "http://localhost:3080"})
READ_TOOLS = frozenset({"board_read", "ops_read", "toolbox_read", "pref_list", "dashboard_read"})
OPTIONAL_TOOLS = frozenset({"brain_read"})  # known optional tools: answer addon_off when their module is absent
WRITE_TOOLS = frozenset({"answer", "todo_tick", "toggle_set", "floor_set", "pref_approve", "pref_reject", "pref_edit"})

PREFS_TOOLS = frozenset({"pref_list", "pref_approve", "pref_reject", "pref_edit"})
# Reads that can be slow on a large install (a preferences registry of hundreds of notes took 30 s on 9 Nov 2026).
# Their answer is kept for CACHE_S seconds; any write through this server clears the cache.
SLOW_READS = frozenset({"pref_list", "toolbox_read", "brain_read"})
CACHE_S = 60
CACHE_LONG = {"brain_read": 600}  # the knowledge graph changes slowly and took 48 s to build on 9 Nov 2026

_lock = threading.Lock()
_mods = None
_optional = {}
_prefs = {}
# Optional tool modules (Waypoint Claude v8 Brain and Learn). Their own READ_ONLY / FUNCS decide which tools are reads
# and which are writes; a module that cannot load on this install simply offers no tools.
OPTIONAL_MODULES = ("brain_tools", "learn_tools")


def board_modules():
    """``(mcp_board, ops_tools)`` from this repo: the Waypoint Claude board and ops tools (no personal paths)."""
    global _mods
    with _lock:
        if _mods is None:
            # the data client: core/waypoint-data/py in the source repo, waypoint-data/py in the Core product
            for path in (REPO / "runner", REPO / "core" / "waypoint-data" / "py", REPO / "waypoint-data" / "py"):
                if str(path) not in sys.path:
                    sys.path.insert(0, str(path))
            _mods = (importlib.import_module("waypoint_runner.mcp_board"),
                     importlib.import_module("waypoint_runner.ops_tools"))
        return _mods


def optional_module(name):
    """An optional tool module (``brain_tools``, ``learn_tools``), or None when it cannot load on this install."""
    board_modules()
    with _lock:
        if name not in _optional:
            try:
                _optional[name] = importlib.import_module("waypoint_runner." + name)
            except Exception:  # noqa: BLE001 - optional; the rest of the server runs without it
                _optional[name] = None
        return _optional[name]


def brain_module():
    return optional_module("brain_tools")


def allowed_tools():
    """``(reads, writes, owner)``: every tool a tab may call and the module that answers each optional one."""
    reads, writes, owner = set(READ_TOOLS), set(WRITE_TOOLS), {}
    for name in OPTIONAL_MODULES:
        mod = brain_module() if name == "brain_tools" else optional_module(name)
        if mod is None:
            continue
        ro = set(getattr(mod, "READ_ONLY", ()))
        for tool in getattr(mod, "FUNCS", {}):
            (reads if tool in ro else writes).add(tool)
            owner[tool] = mod
    return reads, writes, owner


def prefs_dir(env=None, home=None):
    """The preferences add-on: a folder holding ``mcp_preferences.py`` (WAYPOINT_PREFS_TOOLS, else ``prefs_tools`` in
    ~/.waypoint/config.json). None when not configured or missing: the add-on is then off and its tools are absent."""
    env = os.environ if env is None else env
    value = env.get("WAYPOINT_PREFS_TOOLS") or _settings(home).get("prefs_tools")
    if value and (Path(value) / "mcp_preferences.py").is_file():
        return str(value)
    return None


def prefs_module(folder):
    """The preferences tool module from ``folder`` (imported once per folder), or None."""
    if not folder:
        return None
    with _lock:
        if folder not in _prefs:
            if folder not in sys.path:
                sys.path.insert(0, folder)  # its own helpers (sync_preferences) import from beside it
            try:  # load by path under a per-folder name, so two folders never share one cached module
                name = "wpc_prefs_%d" % len(_prefs)
                spec = importlib.util.spec_from_file_location(name, str(Path(folder) / "mcp_preferences.py"))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                _prefs[folder] = mod
            except Exception:  # noqa: BLE001 - a broken add-on must not take the server down
                _prefs[folder] = None
        return _prefs[folder]


def features(root, prefs_folder):
    """What this install offers: the tools a tab may call and which add-ons are on."""
    prefs_on = prefs_module(prefs_folder) is not None
    reads, writes, owner = allowed_tools()
    tools = sorted((reads | writes) - (set() if prefs_on else PREFS_TOOLS) - ({"brain_read"} - set(owner)))
    return {"tools": tools, "addons": {"preferences": prefs_on, "brain": "brain_read" in owner,
                                       "learn": "learn_read" in owner,
                                       "toolbox": (Path(root) / "toolbox.json").is_file()},
            "data_dir": str(root)}


def call_tool(root, tool, args, by, prefs_folder=None):
    """Run one Waypoint Claude tool as its MCP server would; returns ``(payload, is_error)``."""
    mb, ot = board_modules()
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": tool, "arguments": args if isinstance(args, dict) else {}}}
    if tool in PREFS_TOOLS:
        mp = prefs_module(prefs_folder)
        if mp is None:
            return {"error": "addon_off", "message": "the preferences add-on is not configured"}, True
        reply = mp.handle(msg)
    elif tool in OPTIONAL_TOOLS or tool in allowed_tools()[2]:
        mod = allowed_tools()[2].get(tool)
        if mod is None:
            return {"error": "addon_off", "message": f"{tool} is not available on this install"}, True
        reply = mod.handle(mb.BoardTools(str(root), by=by), msg)
    elif tool in ot.FUNCS:
        reply = ot.handle(mb.BoardTools(str(root), by=by), msg)
    else:
        reply = mb.handle(mb.BoardTools(str(root), by=by), msg)
    result = reply["result"]
    return json.loads(result["content"][0]["text"]), bool(result.get("isError"))


def _settings(home=None):
    try:
        data = json.loads((Path(home or str(Path.home())) / ".waypoint" / "config.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def data_dir(env=None, home=None):
    """WAYPOINT_DATA_DIR, then ``data_dir`` in ~/.waypoint/config.json (the engine and launcher setting), then the
    runner default (~/.waypoint/data-dir pointer, then ~/.waypoint/data)."""
    env = os.environ if env is None else env
    if env.get("WAYPOINT_DATA_DIR"):
        return env["WAYPOINT_DATA_DIR"]
    home = home or str(Path.home())
    if _settings(home).get("data_dir"):
        return str(_settings(home)["data_dir"])
    board_modules()
    from waypoint_runner.lifecycle import default_data_dir
    return default_data_dir(env, home)


class Handler(BaseHTTPRequestHandler):
    server_version = "wpc-http"

    def log_message(self, *a):  # quiet: the launcher runs this hidden
        pass

    def _host_ok(self):
        port = self.server.server_address[1]
        return (self.headers.get("Host") or "") in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _cors(self):
        o = self.headers.get("Origin")
        if o in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", o)
            self.send_header("Vary", "Origin")

    def _send(self, status, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        if not self._host_ok() or self.headers.get("Origin") not in ALLOWED_ORIGINS:
            return self._send(403, {"error": "forbidden"})
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad_host"})
        path = self.path.split("?", 1)[0]
        if path == "/api/wpc/page":
            return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        if path == "/api/wpc/features":
            return self._send(200, features(self.server.root, self.server.prefs))
        if path in ("/health", "/api/health"):
            return self._send(200, {"ok": True, "service": "wpc-http", "data_dir": self.server.root})
        return self._send(404, {"error": "not_found"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        raw = self.rfile.read(length) if 0 < length <= MAX_BODY else b""
        if not self._host_ok():
            return self._send(403, {"error": "bad_host"})
        if self.headers.get("Origin") not in ALLOWED_ORIGINS:
            return self._send(403, {"error": "forbidden", "detail": "only the dsh page may call this server"})
        if self.path.split("?", 1)[0] != "/api/wpc/call":
            return self._send(404, {"error": "not_found"})
        if length < 0 or length > MAX_BODY:
            return self._send(413, {"error": "too_large"})
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            return self._send(400, {"error": "bad_request", "detail": "body must be JSON"})
        tool = body.get("tool") if isinstance(body, dict) else None
        if not isinstance(tool, str) or not tool:
            return self._send(400, {"error": "bad_request", "detail": "tool is required"})
        reads, writes, _owner = allowed_tools()
        if tool not in reads and tool not in writes and tool not in OPTIONAL_TOOLS:
            return self._send(400, {"error": "unknown_tool", "detail": f"{tool} is not a Waypoint Claude page tool"})
        try:
            payload, is_error = self.server.call(tool, body.get("input"), tool in writes)
        except Exception as exc:  # noqa: BLE001 - report, never kill the server thread
            return self._send(500, {"error": "internal", "detail": f"{type(exc).__name__}: {exc}"})
        return self._send(200, {"payload": payload, "isError": is_error})


class WpcServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, root, host="127.0.0.1", port=DEFAULT_PORT, prefs=None, now=None):
        self.root = str(root)
        self.prefs = prefs
        self.now = now or __import__("time").monotonic
        self._cache = {}
        self._cache_lock = threading.Lock()
        super().__init__((host, port), Handler)


def _server_call(self, tool, args, is_write):
    """Run a tool; slow reads come from a short cache, and any write clears it."""
    if is_write:
        with self._cache_lock:
            self._cache.clear()
        return call_tool(self.root, tool, args, "owner", self.prefs)
    if tool not in SLOW_READS:
        return call_tool(self.root, tool, args, "owner", self.prefs)
    key = tool + " " + json.dumps(args if isinstance(args, dict) else {}, sort_keys=True)
    with self._cache_lock:
        hit = self._cache.get(key)
    if hit and self.now() - hit[0] < CACHE_LONG.get(tool, CACHE_S):
        return hit[1]
    answer = call_tool(self.root, tool, args, "owner", self.prefs)
    if not answer[1]:
        with self._cache_lock:
            self._cache[key] = (self.now(), answer)
    return answer


WpcServer.call = _server_call


def make_server(root, host="127.0.0.1", port=DEFAULT_PORT, prefs="auto", now=None):
    """``prefs``: the preferences add-on folder, None for off, or "auto" to read the settings (``prefs_dir``)."""
    return WpcServer(root, host, port, prefs_dir() if prefs == "auto" else prefs, now)


def port_busy(port, host="127.0.0.1"):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--folder", help="data folder (default: WAYPOINT_DATA_DIR, then ~/.waypoint/config.json data_dir)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    a = ap.parse_args(argv)
    if port_busy(a.port):
        print(f"wpc-http: 127.0.0.1:{a.port} is already serving", flush=True)
        return 0
    srv = make_server(a.folder or data_dir(), port=a.port)
    print(f"wpc-http: listening on http://127.0.0.1:{a.port} (data {srv.root})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
