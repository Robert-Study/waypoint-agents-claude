"""Parent links and titles from the Claude desktop app's own session files (Waypoint Core 1.1).

The desktop app keeps ``local_<uuid>.json`` per session under ``claude-code-sessions/<org>/<account>/``.
``spawnedFrom.sessionId`` names the parent's local id; ``cliSessionId`` is the transcript id. Read-only,
cached by file mtime, rescanned at most every ``MIN_INTERVAL`` seconds. No model is called.
"""
from __future__ import annotations

import glob
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

MIN_INTERVAL = 15.0
FOLDER = "claude-code-sessions"
MAX_FILE = 2 * 1024 * 1024

_FILES: dict = {}   # path -> (mtime, record | None)
_RESULT: dict = {}  # tuple(roots) -> (checked_at, links)


def find_roots(env=None) -> list:
    """Candidate ``claude-code-sessions`` folders that exist. ``env`` is injectable (LOCALAPPDATA, APPDATA)."""
    env = os.environ if env is None else env
    found = []
    local = env.get("LOCALAPPDATA")
    if local:
        found += sorted(glob.glob(os.path.join(glob.escape(local), "Claude-Data", "*", FOLDER)))
    roaming = env.get("APPDATA")
    if roaming:
        found.append(os.path.join(roaming, "Claude", FOLDER))
    out = []
    for p in found:
        if os.path.isdir(p) and p not in out:
            out.append(p)
    return out


def _iso_ms(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        return None
    try:
        return datetime.fromtimestamp(v / 1000, timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def _s(v):
    return v if isinstance(v, str) and v else None


def _parse(path, mtime):
    cached = _FILES.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    rec = None
    try:
        if os.path.getsize(path) <= MAX_FILE:
            d = json.loads(Path(path).read_text(encoding="utf-8"))
            if isinstance(d, dict) and _s(d.get("cliSessionId")):
                spawn = d.get("spawnedFrom") if isinstance(d.get("spawnedFrom"), dict) else {}
                post = d.get("postTurnSummary") if isinstance(d.get("postTurnSummary"), dict) else {}
                rec = {"cli": d["cliSessionId"], "local_id": _s(d.get("sessionId")),
                       "parent_local": _s(spawn.get("sessionId")), "title": _s(d.get("title")),
                       "cwd": _s(d.get("cwd")), "last_activity_at": _iso_ms(d.get("lastActivityAt")),
                       "archived": bool(d.get("isArchived")), "name": _s(spawn.get("name")) or _s(d.get("name")),
                       "status_category": _s(post.get("status_category"))}
    except (OSError, ValueError):
        rec = None
    _FILES[path] = (mtime, rec)
    return rec


def _scan(roots):
    recs = []
    for root in roots:
        for dirpath, dirs, files in os.walk(root):
            if dirpath[len(str(root)):].count(os.sep) >= 3:
                dirs[:] = []
            for name in files:
                if not (name.startswith("local_") and name.endswith(".json")):
                    continue  # deleted_* and anything else
                p = os.path.join(dirpath, name)
                try:
                    mt = os.stat(p).st_mtime
                except OSError:
                    continue
                rec = _parse(p, mt)
                if rec:
                    recs.append(rec)
    return recs


def read_links(roots, min_interval=MIN_INTERVAL, clock=time.monotonic, force=False) -> dict:
    """``{cli_session_id: {local_id, parent_cli_session_id, title, cwd, last_activity_at, archived, name,
    status_category}}``."""
    key = tuple(str(r) for r in roots)
    hit = _RESULT.get(key)
    now = clock()
    if hit and not force and now - hit[0] < min_interval:
        return hit[1]
    recs = _scan(roots)
    by_local = {r["local_id"]: r["cli"] for r in recs if r["local_id"]}
    links = {}
    for r in recs:
        links[r["cli"]] = {
            "local_id": r["local_id"], "parent_cli_session_id": by_local.get(r["parent_local"]),
            "title": r["title"], "cwd": r["cwd"], "last_activity_at": r["last_activity_at"],
            "archived": r["archived"], "name": r["name"], "status_category": r["status_category"]}
    _RESULT[key] = (now, links)
    return links
