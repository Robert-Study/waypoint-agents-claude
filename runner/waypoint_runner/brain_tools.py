"""Waypoint Claude brain tool: brain_read returns Owner's knowledge graph (preferences, brain notes, skills,
instructions) for the page to draw. Read only: the index comes from engine/cc_engine/brain_index.py, which never
writes a source note; the only file written is the generated cache under the Waypoint data folder."""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    from . import mcp_board as mb
except ImportError:  # started as a script from this folder
    import mcp_board as mb

_REPO = Path(__file__).resolve().parents[2]
if (_REPO / "engine").is_dir() and str(_REPO / "engine") not in sys.path:
    sys.path.append(str(_REPO / "engine"))


ToolError = mb.ToolError

MAX_NODES = 1500
CACHE_MAX_AGE = 30 * 60  # seconds
DEFAULT_BRIGHTNESS = 0.4  # skills and instructions carry no provenance


def _rel(path, drive):
    try:
        return Path(path).relative_to(drive).as_posix()
    except ValueError:
        return str(path).replace("\\", "/")


def brain_read(board, args):
    """The knowledge graph: nodes (no folders unless folders=true), valid links, capped at 1500 by brightness.
    Cached for 30 minutes; rebuild=true forces a rebuild. Read only."""
    from cc_engine import brain_index  # noqa: PLC0415  (lazy: a missing engine/ must not take down the other tools)
    cache = Path(board.store.root) / "cache" / "brain_index.json"
    graph = brain_index.get_graph(refresh=bool(args.get("rebuild")), max_age_seconds=CACHE_MAX_AGE, cache_path=cache)
    drive = Path((graph.get("roots") or {}).get("drive") or brain_index.drive_root())
    folders = bool(args.get("folders"))
    nodes = []
    for n in graph.get("nodes") or []:
        if n.get("kind") == "folder" and not folders:
            continue
        b = n.get("brightness")
        nodes.append({"id": n["id"], "kind": n["kind"], "title": n.get("label") or n["id"],
                      "cluster": n.get("cluster"), "brightness": DEFAULT_BRIGHTNESS if b is None else round(b, 2),
                      "path": _rel(n.get("path") or "", drive)})
    truncated = len(nodes) > MAX_NODES
    if truncated:
        nodes = sorted(nodes, key=lambda n: -n["brightness"])[:MAX_NODES]  # stable: ties keep index order
    index = {n["id"]: i for i, n in enumerate(nodes)}
    # Compact links (the page draws ~2000): [source index, target index, kind]; ends outside the kept nodes drop.
    links = [[index[l["source"]], index[l["target"]], l["kind"]]
             for l in graph.get("links") or [] if l.get("source") in index and l.get("target") in index]
    counts = {}
    for n in nodes:
        counts[n["kind"]] = counts.get(n["kind"], 0) + 1
    return {"built_at": graph.get("built_at"), "counts": counts, "truncated": truncated, "nodes": nodes, "links": links}


TOOLS = [
    ("brain_read", "Read Owner's knowledge graph (preferences, brain notes, skills, instructions) as nodes and links. "
     "Read only. Args: rebuild (force a fresh index), folders (include folder nodes).",
     {"rebuild": {"type": "boolean"}, "folders": {"type": "boolean"}}, []),
]
READ_ONLY = {"brain_read"}
FUNCS = {"brain_read": brain_read}


def tools_list():
    return [{"name": n, "description": d, "inputSchema": {"type": "object", "properties": p, "required": r},
             "annotations": {"readOnlyHint": n in READ_ONLY}} for n, d, p, r in TOOLS]


def handle(board, message):
    params = message.get("params") or {}
    name, args = str(params.get("name", "")), params.get("arguments") or {}
    try:
        result = mb._text(FUNCS[name](board, args if isinstance(args, dict) else {}), False)
    except ToolError as exc:
        result = mb._text({"error": exc.code, "message": str(exc), **exc.data}, True)
    except Exception as exc:  # noqa: BLE001
        result = mb._text({"error": "internal", "message": str(exc)}, True)
    return {"jsonrpc": "2.0", "id": message.get("id"), "result": result}
