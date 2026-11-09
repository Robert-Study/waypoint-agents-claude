"""Stale tag (waypoint-v6/auto-live-updates): a task ``running`` with no update for 30 minutes gets the tag ``stale``.

Tag only, never a status change. The tag clears on the next update: a hook or tool write after the tag (the node's
``updated_by`` is no longer this writer), a newer claim heartbeat, or a status other than running.

Runs from the runner (``__main__`` calls ``sweep`` at most once a minute) and from the Windows scheduled task
``Waypoint-StaleSweep`` (``pythonw -m waypoint_runner.stale --folder <data>``). No model call, no spend.
Data access only through core/waypoint-data/py.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

_PY = Path(__file__).resolve().parents[2] / "core" / "waypoint-data" / "py"
if _PY.is_dir() and str(_PY) not in sys.path:
    sys.path.insert(0, str(_PY))

from waypoint_data import Claims, Store  # noqa: E402
from waypoint_data.store import RevConflict, parse_iso  # noqa: E402

WRITER = "stale"
TAG = "stale"
IDLE_S = 30 * 60


def _projects(store):
    try:
        return sorted(n for n in os.listdir(store.path("projects")) if os.path.isdir(store.path(f"projects/{n}")))
    except OSError:
        return []


def _with_tags(node, tags):
    new = {k: v for k, v in node.items() if k not in ("rev", "updated_at", "updated_by", "tags")}
    if tags:
        new["tags"] = tags
    return new


def decide(node, claim, now, idle_s=IDLE_S):
    """'add', 'clear' or None for one node. ``claim`` is its claim record or None."""
    tags = node.get("tags") if isinstance(node.get("tags"), list) else []
    tagged = TAG in tags
    if node.get("status") != "running":
        return "clear" if tagged else None
    touched = parse_iso(node.get("updated_at")) or 0
    beat = parse_iso((claim or {}).get("heartbeat_at")) or 0
    if tagged:
        if node.get("updated_by") != WRITER or beat > touched:
            return "clear"  # somebody updated it after the tag
        return None
    return "add" if now - max(touched, beat) >= idle_s else None


def sweep(root, now=time.time, idle_s=IDLE_S):
    """Tag or clear every node in the data folder. Returns {"tagged": [...], "cleared": [...]} as project/node."""
    store = Store(str(root), writer=WRITER, now=now)
    claims = Claims(str(root), now=now, writer=WRITER)
    out = {"tagged": [], "cleared": []}
    t = now()
    for project in _projects(store):
        try:
            names = store.list_names(f"projects/{project}/nodes")
        except (OSError, ValueError):
            continue
        for name in names:
            if not name.endswith(".json"):
                continue
            rel = f"projects/{project}/nodes/{name}"
            try:
                node = store.read(rel)
            except Exception:  # noqa: BLE001 - a torn file waits for the next sweep
                continue
            if not isinstance(node, dict) or not node.get("id"):
                continue
            claim = None
            try:
                claim = claims.read(project, node["id"])
            except Exception:  # noqa: BLE001
                pass
            what = decide(node, claim if isinstance(claim, dict) else None, t, idle_s)
            if what is None:
                continue
            tags = [x for x in (node.get("tags") or []) if x != TAG]
            if what == "add":
                tags.append(TAG)
            try:
                store.write(rel, _with_tags(node, tags), expected_rev=node.get("rev"))
            except (RevConflict, OSError):
                continue  # it changed under us: that is an update, the next sweep sees it
            key = f"{project}/{node['id']}"
            out["tagged" if what == "add" else "cleared"].append(key)
            if what == "add":
                try:
                    store.append_event(WRITER, {"kind": "node.stale", "project": project, "node": node["id"],
                                                "summary": f"{node['id']} is running with no update for "
                                                           f"{int(idle_s // 60)} min; tagged stale.", "data": None})
                except (OSError, ValueError):
                    pass
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="waypoint_runner.stale", description="Tag running tasks idle 30 min as stale.")
    ap.add_argument("--folder", help="data folder (default: WAYPOINT_DATA_DIR)")
    ap.add_argument("--idle-min", type=float, default=IDLE_S / 60)
    a = ap.parse_args(argv)
    folder = a.folder or os.environ.get("WAYPOINT_DATA_DIR")
    if not folder:
        from .lifecycle import default_data_dir
        folder = default_data_dir()
    res = sweep(folder, idle_s=a.idle_min * 60)
    if sys.stdout is not None:
        print(f"stale: tagged {len(res['tagged'])}, cleared {len(res['cleared'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
