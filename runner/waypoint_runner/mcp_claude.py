"""Waypoint Claude: the one local MCP connector for the Waypoint Claude product (pref 218).

Serves the board tools of mcp_board, the v7 ops tools of ops_tools (ops_read, floor_set, toggle_set), brain_read of brain_tools, the learning tools of learn_tools (learn_*) (board_read, toolbox_read, dashboard_read, to-dos, nodes, claims) and the
optional preference review tools of an external mcp_preferences.py, loaded only when found (pref_list, pref_approve, pref_reject,
pref_edit) under one server name, so a page or agent needs one connector. Identity, data folder and all rules stay in
those two modules; this file only routes calls.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from waypoint_runner import mcp_board as mb  # noqa: E402
    from waypoint_runner import ops_tools as ot  # noqa: E402
    from waypoint_runner import brain_tools as bt  # noqa: E402
    from waypoint_runner import learn_tools as lt  # noqa: E402
    from waypoint_runner import lead_notice as ln  # noqa: E402
except ImportError:  # started as a script from this folder
    import mcp_board as mb  # noqa: E402
    import ops_tools as ot  # noqa: E402
    import brain_tools as bt  # noqa: E402
    import learn_tools as lt  # noqa: E402
    import lead_notice as ln  # noqa: E402



def _prefs_folder():
    """The preferences add-on folder (holds ``mcp_preferences.py``): WAYPOINT_PREFS_TOOLS, then ``prefs_tools`` in
    ~/.waypoint/config.json, then the owner's install default. None when no such folder has the module."""
    value = os.environ.get("WAYPOINT_PREFS_TOOLS")
    if not value:
        try:
            cfg = json.loads((Path.home() / ".waypoint" / "config.json").read_text(encoding="utf-8"))
            value = cfg.get("prefs_tools") if isinstance(cfg, dict) else None
        except (OSError, ValueError):
            value = None
    value = value or globals().get("_PREFS_DEFAULT")
    return str(value) if value and (Path(value) / "mcp_preferences.py").is_file() else None


def _load_prefs():
    """The optional preferences module, or None: without it the pref_* tools are not listed and not callable."""
    folder = _prefs_folder()
    if not folder:
        return None
    sys.path.insert(0, folder)
    try:
        import mcp_preferences  # noqa: E402
        return mcp_preferences
    except Exception:  # noqa: BLE001 - a broken add-on must not stop the board tools
        return None


mp = _load_prefs()


def tools_list():
    return mb.tools_list() + ot.tools_list() + bt.tools_list() + lt.tools_list() + (mp.tools_list() if mp else [])


def handle(board, message):
    response = _route(board, message)
    ln.after_call(board, message, response)  # a page write is journalled once for the lead (lead_notice.py)
    return response


def _route(board, message):
    method, msg_id = message.get("method"), message.get("id")
    params = message.get("params") or {}
    if msg_id is None:
        return None
    if method == "initialize":
        r = mb.handle(board, message)
        r["result"]["serverInfo"] = {"name": "waypoint-claude", "version": "0.1.0"}
        return r
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tools_list()}}
    if method == "tools/call" and str(params.get("name", "")) in ot.FUNCS:
        return ot.handle(board, message)
    if method == "tools/call" and str(params.get("name", "")) in bt.FUNCS:
        return bt.handle(board, message)
    if method == "tools/call" and str(params.get("name", "")) in lt.FUNCS:
        return lt.handle(board, message)
    if method == "tools/call" and str(params.get("name", "")) in (mp.FUNCS if mp else ()):
        return mp.handle(message)
    return mb.handle(board, message)


def main(data_root=None, by=None, stdin=None, stdout=None):
    root = data_root or os.environ.get("WAYPOINT_DATA_DIR")
    if not root:
        sys.stderr.write("WAYPOINT_DATA_DIR is not set\n")
        return 2
    board = mb.BoardTools(root, by=by or os.environ.get("WAYPOINT_MCP_BY") or "agent")
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    for raw in iter(stdin.readline, b""):
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            response = handle(board, message) if isinstance(message, dict) else {
                "jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
        if response is not None:
            stdout.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
            stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
