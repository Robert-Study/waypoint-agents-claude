"""Waypoint Claude inside dsh: the engine bridges the Waypoint Claude tools, so dsh shows the same live data.

The dsh pages used to read the 1.1 SQLite board (``cc.sqlite``), which stopped at waypoint-v6. Waypoint Claude reads
the 1.2 file data folder through its MCP server (``runner/waypoint_runner/mcp_claude.py``). This module calls that
same server code in-process, against the engine's data folder (``config.data_dir()``), so both surfaces show one truth
and every rule (revisions, claims, owner-only states, preference review) stays in one place.

``POST /api/wpc/call`` ``{"tool": name, "input": {...}}`` -> ``{"payload": ..., "isError": bool}``. Only the tools
the Waypoint Claude page uses are reachable. Reads accept any engine credential; writes need the owner (the dsh page
Origin or the owner token), never the router token. Writes are recorded as ``by="owner"``.

``GET /api/wpc/page`` returns ``artifacts/waypoint-claude.html`` unchanged, so the dsh Waypoint page always shows the
current Waypoint Claude UI with no copy to keep in step.
"""
from __future__ import annotations

import sys
from pathlib import Path

from . import config
from .api import HttpError
from .store import OWNER

# One copy of the rules: the runner's wpc_http (the 1.2 backend of the dsh Waypoint tab) owns the tool lists,
# the page path and the tool call; this legacy-engine route only adds the engine's credential rule.
_RUNNER = Path(__file__).resolve().parents[2] / "runner"
if str(_RUNNER) not in sys.path:
    sys.path.insert(0, str(_RUNNER))
from waypoint_runner import wpc_http  # noqa: E402

PAGE = wpc_http.PAGE
READ_TOOLS = wpc_http.READ_TOOLS
WRITE_TOOLS = wpc_http.WRITE_TOOLS


def call_tool(tool, args, by):
    """Run one Waypoint Claude tool over the engine's data folder; returns ``(payload, is_error)``."""
    return wpc_http.call_tool(config.data_dir(), tool, args, by, wpc_http.prefs_dir())


def p_call(h, q):
    b = h._json_body()
    tool = b.get("tool")
    args = b.get("input") if isinstance(b.get("input"), dict) else {}
    if not isinstance(tool, str) or not tool:
        raise HttpError(400, {"error": "bad_request", "detail": "tool is required"})
    if tool not in READ_TOOLS and tool not in WRITE_TOOLS:
        raise HttpError(400, {"error": "unknown_tool", "detail": f"{tool} is not a Waypoint Claude page tool"})
    if tool in WRITE_TOOLS and getattr(h, "actor", None) != OWNER:
        raise HttpError(403, {"error": "owner_only", "detail": f"only {config.OWNER_NAME} may use {tool}"})
    payload, is_error = call_tool(tool, args, "owner" if h.actor == OWNER else "agent")
    return {"payload": payload, "isError": is_error}


def g_page(h, q):
    data = PAGE.read_bytes()
    h.send_response(200)
    h.send_header("Content-Type", "text/html; charset=utf-8")
    h.send_header("Content-Length", str(len(data)))
    h.send_header("Cache-Control", "no-store")
    h._cors()
    h.end_headers()
    h.wfile.write(data)


ROUTES = {"POST /api/wpc/call": p_call, "GET /api/wpc/page": g_page}
