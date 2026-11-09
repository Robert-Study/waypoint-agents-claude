"""Waypoint Claude v7 ops tools: what is running, usage for pools in use, provider state, floors and sprints.

ops_read reads; floor_set and toggle_set are Owner's two settings on the page (a pool floor, a provider switch).
They work on the same data folder as mcp_board (one BoardTools for identity, Store and Claims) and hold no limit
constant: a floor is only ever the value Owner sets, and unset means 0% (policy.py).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from . import mcp_board as mb
except ImportError:  # started as a script from this folder
    import mcp_board as mb

from waypoint_data import RevConflict  # noqa: E402
from waypoint_data.store import iso  # noqa: E402

ToolError, _id = mb.ToolError, mb._id

# ---------------------------------------------------------------- v7 ops view
MAIN_POOL_PREFIX = "claude_"  # the Claude subscription Owner works in is always in use
REFRESH_MINUTES = 10  # v8: ops_read starts the usage collector when the newest reading is older than this
_refresh = {"started": 0.0}


def _ts(value):
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp()


def _group(pool):
    """Pools of one source share a prefix: antigravity_week and antigravity_claude_5h belong to antigravity."""
    return str(pool or "").split("_")[0]


def start_usage_refresh(root):
    """Start the 1.2 usage collector detached (python -m waypoint_runner.usage --folder ROOT, about 45 s)."""
    py = os.environ.get("WAYPOINT_PYTHON") or sys.executable
    flags = 0
    if os.name == "nt":
        flags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
                 | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    subprocess.Popen([py, "-m", "waypoint_runner.usage", "--folder", str(root)],
                     cwd=str(Path(__file__).resolve().parents[1]), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)


def _maybe_refresh(board, newest):
    """Start one background refresh when the newest reading is older than REFRESH_MINUTES (at most one start per
    REFRESH_MINUTES). Returns True when a refresh is running or was just started."""
    now = board.now()
    if newest is not None and now - newest <= REFRESH_MINUTES * 60:
        return False
    if now - _refresh["started"] < REFRESH_MINUTES * 60:
        return True
    _refresh["started"] = now
    try:
        start_usage_refresh(board.root)
    except OSError:
        return False
    return True


def _readings(board, sources):
    """Pool readings: the 1.2 usage/<pool>.json files first, then the older usage_cache.json for pools they lack.
    Each row: pool, source, remaining_percent, resets_at, observed_at, failed (the source did not answer)."""
    rows = {}
    for u in board.store.list_entities("usage"):
        if not isinstance(u, dict) or not u.get("pool"):
            continue
        src = str(u.get("source") or "")
        if u.get("remaining_percent") is None:  # no number: the source never answered (a failure, not a pool row)
            rows[u["pool"]] = {"pool": u["pool"], "source": _group(u["pool"]), "remaining_percent": None, "resets_at": None,
                               "observed_at": None, "failed": True, "reason": src or "no source answered"}
            continue
        rows[u["pool"]] = {"pool": u["pool"], "source": _group(u["pool"]), "remaining_percent": u["remaining_percent"],
                           "resets_at": u.get("resets_at"), "observed_at": u.get("read_at"),
                           "failed": "failed" in src, "reason": src if "failed" in src else None}
    for src, body in sorted(sources.items()):
        for r in (body or {}).get("rows") or []:
            pool = (r or {}).get("pool")
            if not pool or r.get("remaining_percent") is None or pool in rows:
                continue
            rows[pool] = {"pool": pool, "source": src, "remaining_percent": r["remaining_percent"],
                          "resets_at": r.get("resets_at"), "observed_at": (body or {}).get("refreshed_at"),
                          "failed": bool((body or {}).get("error")), "reason": (body or {}).get("error")}
    return [rows[k] for k in sorted(rows)]


def ops_read(board, _args):
    """What is running (live claims, running runs), usage for pools in use (with floor and provider), provider
    state (up, down = failed connection, disabled) and sprints (schedules, newest first). Read only, except that a
    stale usage reading starts the usage collector in the background."""
    running = []
    for c in board.claims.list_claims():
        if isinstance(c, dict) and not board.claims.is_stale(c):
            running.append({"project": c.get("project"), "node": c.get("node"),
                            "by": c.get("claimed_by") or c.get("owner"), "since": c.get("since")})
    for r in board.store.list_entities("runs"):
        if isinstance(r, dict) and r.get("status") == "running":
            running.append({"project": r.get("project"), "node": r.get("node"), "by": r.get("route") or "run",
                            "since": r.get("started_at"), "run": r.get("id")})
    running.sort(key=lambda x: str(x.get("since") or ""), reverse=True)
    agents = [a for a in board.store.list_entities("agents") if isinstance(a, dict) and a.get("id")]
    floors = ((board.store.read("settings/floors.json") or {}).get("pools") or {})
    sources = ((board.store.read("usage_cache.json") or {}).get("sources") or {})
    readings = _readings(board, sources)
    enabled_pools = {a.get("pool") for a in agents if a.get("enabled") and a.get("pool")}
    on_groups = {_group(p) for p in enabled_pools}
    by_group = {}
    for a in sorted(agents, key=lambda a: a["id"]):
        if a.get("pool"):
            by_group.setdefault(_group(a["pool"]), a["id"])
    pools = []
    for r in readings:
        pool = r["pool"]
        if r["remaining_percent"] is None:
            continue
        if not (_group(pool) in on_groups or pool.startswith(MAIN_POOL_PREFIX)):
            continue
        fl = (floors.get(pool) or {}).get("min_percent")
        pools.append({"pool": pool, "source": r["source"], "provider": by_group.get(_group(pool)),
                      "remaining_percent": r["remaining_percent"], "resets_at": r.get("resets_at"),
                      "observed_at": r.get("observed_at"), "floor": fl,
                      "below_floor": fl is not None and r["remaining_percent"] < fl})
    providers = []
    for a in sorted(agents, key=lambda a: a["id"]):
        g = _group(a.get("pool")) if a.get("pool") else None
        src_err = next((r.get("reason") or "usage source failed" for r in readings
                        if g and _group(r["pool"]) == g and r.get("failed")), None)
        state = "disabled" if not a.get("enabled") else ("down" if src_err else "up")
        providers.append({"id": a["id"], "label": a.get("label") or a["id"], "enabled": bool(a.get("enabled")),
                          "pool": a.get("pool"), "state": state, "reason": src_err, "rev": a.get("rev")})
    stamps = [t for t in (_ts(r.get("observed_at")) for r in readings) if t is not None]
    refreshing = _maybe_refresh(board, max(stamps) if stamps else None)
    now = board.now()
    sprints = []
    for sc in board.store.list_entities("schedules"):
        if not isinstance(sc, dict) or not sc.get("id"):
            continue
        start = sc.get("start")
        try:
            t = time.mktime(time.strptime(start[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone if start else None
        except (TypeError, ValueError):
            t = None
        hour = int(start[11:13]) if isinstance(start, str) and len(start) >= 13 and start[11:13].isdigit() else None
        last = sc.get("last_run") or {}
        sprints.append({"id": sc["id"], "project": sc.get("project"), "mode": sc.get("mode"), "start": start,
                        "window": sc.get("window"), "next": bool(t and t > now),
                        "overnight": hour is not None and (hour >= 20 or hour < 6),
                        "last_result": last.get("result"), "last_ended": last.get("ended_at")})
    sprints.sort(key=lambda x: str(x.get("start") or ""), reverse=True)
    return {"running": running, "pools": pools, "providers": providers, "sprints": sprints,
            "usage_refreshing": refreshing}

def floor_set(board, args):
    """Owner's usage floor for one pool: an integer percent 0-100, or null for unset (0%)."""
    pool = _id(args.get("pool"), "pool")
    v = args.get("min_percent")
    if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 100):
        raise ToolError("bad_argument", "min_percent must be a number from 0 to 100, or null")
    cur = board.store.read("settings/floors.json")
    body = {k: val for k, val in (cur or {}).items() if k not in ("rev", "updated_at", "updated_by")}
    pools = dict(body.get("pools") or {})
    pools[pool] = {"min_percent": v, "set_by": board.by, "set_at": iso(board.now())}
    body["pools"] = pools
    try:
        board.store.write("settings/floors.json", body, expected_rev=(cur or {}).get("rev") if cur else 0)  # a hand-written file has no rev yet
    except RevConflict as exc:
        raise board._conflict(exc, "floors")
    board.store.append_event(board.by, {"kind": "floor.set", "summary": f"Floor for {pool} set to "
                                      f"{'unset' if v is None else str(v) + '%'}.", "data": {"pool": pool, "min_percent": v}})
    return {"pool": pool, "min_percent": v}

def toggle_set(board, args):
    """Switch one provider (agents/<id>.json) on or off."""
    aid = _id(args.get("id"), "id")
    on = args.get("enabled")
    if not isinstance(on, bool):
        raise ToolError("bad_argument", "enabled must be true or false")
    cur = board.store.read(f"agents/{aid}.json")
    if not isinstance(cur, dict):
        raise ToolError("not_found", f"provider {aid} does not exist")
    try:
        out = board.store.write(f"agents/{aid}.json", dict(cur, enabled=on), expected_rev=cur.get("rev"))
    except RevConflict as exc:
        raise board._conflict(exc, "provider")
    board.store.append_event(board.by, {"kind": "toggle.set", "summary": f"{cur.get('label') or aid} switched "
                                      f"{'on' if on else 'off'}.", "data": {"id": aid, "enabled": on}})
    return {"id": aid, "enabled": out["enabled"]}



TOOLS = [
    ("ops_read", "Read what is running, usage for pools in use (with floors), provider state (up/down/disabled) and sprints.", {}, []),
    ("floor_set", "Owner's setting from Waypoint Claude: set one pool's usage floor (min_percent 0-100, or null = unset). Agents never call this.",
     {"pool": {"type": "string"}, "min_percent": {"type": ["number", "null"]}}, ["pool", "min_percent"]),
    ("toggle_set", "Owner's setting from Waypoint Claude: switch one provider on or off. Agents never call this.",
     {"id": {"type": "string"}, "enabled": {"type": "boolean"}}, ["id", "enabled"]),
]
READ_ONLY = {"ops_read"}
FUNCS = {"ops_read": ops_read, "floor_set": floor_set, "toggle_set": toggle_set}


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
