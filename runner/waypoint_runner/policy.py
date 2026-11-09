"""Runner policy: per-agent toggles, editable floors and the floor decision.

Python twin of core/waypoint-data/src/floors.ts. Both replay core/waypoint-data/fixtures/floors.json.

FLOOR DECISION (Owner, 7 Nov 2026, EXPLICIT_OWNER_DECISION): no floor is hard-coded or seeded. Floors are
settings Owner sets in Waypoint (settings/floors.json). An UNSET floor means 0% (use it or lose it): the route
runs and no to-do is raised. A set floor is enforced against a FRESH reading; STALE or UNKNOWN readings never
count as FRESH.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import memory

FRESH, STALE, UNKNOWN = "FRESH", "STALE", "UNKNOWN"

# Pools that may run with no floor set (free or local). Matched by name or "<name>_" prefix.
FREE_POOLS = ("groq", "gemini", "ollama", "freebuff", "github_models")

# Reading freshness window used only when settings/settings.json is missing or unreadable. This is a
# time window, not a floor; the real value is the usage_fresh_minutes setting.
FALLBACK_FRESH_MINUTES = 30

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str  # ok | disabled | memory_low | floor_unset | below_floor | stale | unknown | unknown_route
    freshness: str | None
    todo: str | None
    detail: str | None = None  # plain sentence for the refusal, when there is one


def valid_id(value) -> bool:
    return isinstance(value, str) and bool(_ID.match(value))


def is_free_pool(pool) -> bool:
    if not pool:
        return True  # draws from no pool: free or local
    return any(pool == p or pool.startswith(p + "_") for p in FREE_POOLS)


def _parse(ts):
    if not ts or not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def classify_reading(reading, fresh_minutes, now) -> str:
    """FRESH: read within fresh_minutes, not from the future, not taken before a reset that has since passed."""
    if not reading or reading.get("remaining_percent") is None:
        return UNKNOWN
    read_at = _parse(reading.get("read_at"))
    if read_at is None:
        return UNKNOWN
    if read_at > now:
        return UNKNOWN
    if (now - read_at).total_seconds() > fresh_minutes * 60:
        return STALE
    resets_at = _parse(reading.get("resets_at"))
    if resets_at is not None and resets_at <= now and read_at < resets_at:
        return STALE
    return FRESH


def decide(route, settings, reading, now) -> Decision:
    """route: {id, enabled, pool}. settings: {usage_fresh_minutes, floors: {pools: {pool: {min_percent}}}}."""
    if not route.get("enabled"):
        return Decision(False, "disabled", None, None)

    pool = route.get("pool") or None
    own = reading if (pool is not None and reading and reading.get("pool") == pool) else None
    freshness = classify_reading(own, settings.get("usage_fresh_minutes", FALLBACK_FRESH_MINUTES), now)
    entry = ((settings.get("floors") or {}).get("pools") or {}).get(pool) if pool is not None else None
    floor = entry.get("min_percent") if isinstance(entry, dict) else None

    if floor is None:
        return Decision(True, "ok", freshness, None)

    if freshness == STALE:
        return Decision(False, "stale", freshness, None)
    if freshness != FRESH or own is None or own.get("remaining_percent") is None:
        return Decision(False, "unknown", UNKNOWN, None)
    if own["remaining_percent"] < floor:
        return Decision(False, "below_floor", freshness, None)
    return Decision(True, "ok", freshness, None)


# ---------------------------------------------------------------- folder reading
def _read_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data


def agents(root) -> list[dict]:
    """agents/<id>.json entries, sorted by file name."""
    out = []
    d = Path(root) / "agents"
    if d.is_dir():
        for f in sorted(d.glob("*.json")):
            e = _read_json(f)
            if isinstance(e, dict) and e.get("id"):
                out.append(e)
    return out


def toggles(root) -> dict:
    """{agent id: enabled} from the Agents tab switches. A missing flag counts as off."""
    return {a["id"]: bool(a.get("enabled")) for a in agents(root)}


def floors(root) -> dict:
    data = _read_json(Path(root) / "settings" / "floors.json")
    return data if isinstance(data, dict) and isinstance(data.get("pools"), dict) else {"pools": {}}


def floor(root, pool):
    """The floor Owner set for a pool, or None while unset (no default exists)."""
    entry = floors(root)["pools"].get(pool)
    value = entry.get("min_percent") if isinstance(entry, dict) else None
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def settings(root) -> dict:
    """Freshness window plus floors, in the shape decide() takes."""
    data = _read_json(Path(root) / "settings" / "settings.json")
    minutes = data.get("usage_fresh_minutes") if isinstance(data, dict) else None
    if not isinstance(minutes, (int, float)) or isinstance(minutes, bool) or minutes <= 0:
        minutes = FALLBACK_FRESH_MINUTES
    return {"usage_fresh_minutes": minutes, "floors": floors(root)}


def find_route(root, route_id):
    """The agents entry whose id, else whose route name, equals route_id; None if there is none."""
    entries = agents(root)
    for a in entries:
        if a["id"] == route_id:
            return a
    for a in entries:
        if a.get("route") == route_id:
            return a
    return None


def stored_reading(root, pool):
    if not valid_id(pool):
        return None
    data = _read_json(Path(root) / "usage" / f"{pool}.json")
    return data if isinstance(data, dict) else None


def allowed(root, route_id, now=None) -> Decision:
    """May this route (agent id or route name) be called now? Reads toggles, floors and stored readings."""
    now = now or datetime.now(timezone.utc)
    agent = find_route(root, route_id)
    if agent is None:
        return Decision(False, "unknown_route", None, None)
    route = {"id": agent["id"], "enabled": bool(agent.get("enabled")), "pool": agent.get("pool")}
    # RAM floor: no NEW worker while memory is short. Running workers are never touched.
    mem = memory.check(root, now=now) if memory.gate_enabled() else None
    if mem and not mem["ok"]:
        return Decision(False, "memory_low", None, None, mem["reason"])
    return decide(route, settings(root), stored_reading(root, route["pool"]), now)
