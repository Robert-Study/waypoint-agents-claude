"""The ``probe`` action: check one route's health and write health/<route>.json {route, ok, reason, checked_at}.

Payload: {"route": "<agent id or route name>"} (``target`` is accepted as an alias).

Gate, before anything is called:
* The agent's toggle must be on (a disabled route is never called).
* A metered route (claude headless, Codex) also needs a floor decision that allows it: floor set, fresh
  reading above it (policy.allowed). A refused probe is recorded as ok=false with a "refused:" reason and
  the action result is "refused"; the pinger is not called.
* A free or local route (Groq, Gemini, Ollama, no pool) may be pinged once its toggle is on.

The pinger is injectable: ``pinger(route_id, agent) -> (ok, reason)``. The default reuses
engine/cc_engine/model_health.py as a library (its cached provider checks) and never makes a model call:
Ollama gets a local tags request, a metered CLI route only gets an "is the program installed" check.
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import policy, usage

REASON_MAX = 200
OLLAMA_URL = "http://127.0.0.1:11434/api/tags"

# agent pool or id -> model_health provider name
_PROVIDERS = {"groq": "groq", "gemini": "google", "freebuff": "freebuff", "github_models": "github-copilot"}
_CLI = {"claude_cli": "claude", "claude_code": "claude", "codex_cli": "codex"}


def _iso(dt) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean(text) -> str:
    return " ".join(str(text or "").split())[:REASON_MAX] or "no reason given"


def _provider_of(agent):
    key = str(agent.get("pool") or agent.get("id") or "")
    for prefix, prov in _PROVIDERS.items():
        if key == prefix or key.startswith(prefix + "_"):
            return prov
    return None


def default_pinger(route_id, agent):
    """(ok, reason) without any model call. Only reached for a route the gate has allowed."""
    pool = agent.get("pool")
    if not policy.is_free_pool(pool):  # metered: confirm the program exists, never run a prompt
        exe = _CLI.get(str(agent.get("route") or "")) or _CLI.get(agent["id"])
        if exe and shutil.which(exe):
            return True, f"{exe} is installed (no model call made)"
        return False, "program not found" if exe else "no free check exists for this metered route"
    if route_id == "ollama" or agent.get("id") == "ollama":
        import urllib.request  # noqa: PLC0415

        with urllib.request.urlopen(OLLAMA_URL, timeout=5) as resp:  # local only
            json.load(resp)
        return True, "ollama answered on localhost"
    prov = _provider_of(agent)
    if prov:
        from cc_engine import model_health as mh  # noqa: PLC0415

        cache = mh.load()
        entries = [v for k, v in (cache.get("routes") or {}).items() if k.startswith(prov + "/")]
        if not entries:
            return False, "no model check result yet; the engine model check has not run"
        good = [e for e in entries if e.get("ok") is True]
        if good:
            return True, f"{len(good)} of {len(entries)} models answered (checked {good[0].get('checked_at')})"
        return False, _clean(entries[0].get("reason") or "all models failed")
    return False, "no health check is defined for this route"


def handle(action, root, pinger=None, now=None) -> dict:
    """Run one probe action. Returns {status: done|refused|failed, result?, error?}."""
    payload = action.get("payload") or {}
    target = payload.get("route") or payload.get("target")
    if not policy.valid_id(target):
        return {"status": "failed", "error": "probe needs payload.route (an agent id or route name)"}
    agent = policy.find_route(root, target)
    if agent is None:
        return {"status": "failed", "error": f"unknown route: {target}"}

    when = (now or (lambda: datetime.now(timezone.utc)))()
    pinger = pinger or default_pinger
    path = Path(root) / "health" / f"{target}.json"

    def record(ok, reason):
        entry = {"route": target, "ok": ok, "reason": _clean(reason), "checked_at": _iso(when)}
        usage.write_json(path, entry)
        return entry

    # Toggle always; floor decision for metered routes only. A free route draws no metered usage.
    metered = not policy.is_free_pool(agent.get("pool"))
    if not agent.get("enabled"):
        why = "disabled"
    elif metered:
        d = policy.allowed(root, agent["id"], now=when)
        why = None if d.allowed else d.reason
    else:
        why = None
    if why is not None:
        entry = record(False, f"refused: {why.replace('_', ' ')}" + (" (set the floor in Waypoint)" if why == "floor_unset" else ""))
        return {"status": "refused", "result": entry}

    try:
        ok, reason = pinger(target, agent)
    except Exception as exc:
        ok, reason = False, f"{type(exc).__name__}: {exc}"
    reason = re.sub(r"(?i)(bearer\s+)\S+", r"\1***", str(reason or ("ok" if ok else "")))
    return {"status": "done", "result": record(bool(ok), reason)}


def register(handlers, root=None, pinger=None, now=None):
    """Add the ``probe`` handler. The handler takes (action, ctx=None); ctx may carry root, pinger, now."""

    def probe(action, ctx=None):
        ctx = ctx or {}
        folder = ctx.get("root", root)
        if folder is None:
            return {"status": "failed", "error": "no data folder configured"}
        return handle(action, folder, pinger=ctx.get("pinger", pinger), now=ctx.get("now", now))

    handlers["probe"] = probe
    return handlers
