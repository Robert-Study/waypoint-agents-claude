"""Per-pool usage readings for the runner.

``readings()`` asks each usage source for observations, marks every pool FRESH, STALE or UNKNOWN and writes
``usage/<pool>.json`` ({pool, remaining_percent, resets_at, read_at, source}; the schema has no freshness
field, so the mark is returned and recomputed from read_at by every reader).

The collection code is reused from engine/cc_engine/usage.py (claude_source, codex_source, agy_source);
not its SQLite or cache. Observation rows have the engine's ``obs`` shape (observed_at); unknown rows
have no number and no time.

Unknown evidence stays unknown and is never FRESH: a pool no source answered is written with
remaining_percent null and read_at null. A failing source never raises. An older verified number is kept
(and ages into STALE on its own read_at) when a new observation has none, as the engine does.
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
for _p in (_REPO / "core" / "waypoint-data" / "py", _REPO / "engine"):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.append(str(_p))

from waypoint_data import io as wio  # noqa: E402
from waypoint_data.store import serialize  # noqa: E402

from . import policy  # noqa: E402

WRITER = "runner"
NO_SOURCE = "none: no source answered"


def write_json(path, entity):
    """Plain entity file (usage and health files carry no rev): canonical text, atomic replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wio.atomic_write(str(path), serialize(entity), writer=WRITER)


def load(root, pool):
    """The stored reading for a pool, or None."""
    return policy.stored_reading(root, pool)


def engine_sources() -> dict:
    """Default sources: the engine's local, read-only collectors. Imported lazily; each returns obs rows."""
    from cc_engine import usage as eng  # noqa: PLC0415

    return {"claude": eng.claude_source, "codex": eng.codex_source, "antigravity": eng.agy_source}


def _reading(row) -> dict:
    pct = row.get("remaining_percent")
    read_at = row.get("observed_at") or row.get("read_at")
    if pct is None or read_at is None:
        pct, read_at = None, None  # a number is kept only with its read time
    return {"pool": row["pool"], "remaining_percent": pct, "resets_at": row.get("resets_at") if pct is not None
            else None, "read_at": read_at, "source": str(row.get("source") or "unknown")}


def _known_pools(root) -> list:
    """Pools that agents or floors name; they get a reading file even when no source answered."""
    pools = [a.get("pool") for a in policy.agents(root)] + list(policy.floors(root)["pools"])
    return list(dict.fromkeys(p for p in pools if p))


def readings(root, sources=None, now=None) -> list:
    """Collect, mark and write readings. Returns [{pool, remaining_percent, resets_at, read_at, source,
    freshness}] for every pool a source answered or an agent/floor names."""
    now = now or datetime.now(timezone.utc)
    cfg = policy.settings(root)
    sources = engine_sources() if sources is None else sources

    def collect_one(collect):
        try:
            return collect() or []
        except Exception:  # a source problem never stops the others
            return []

    # Slow sources (claude ~45 s, agy ~25 s) run side by side so a refresh costs the slowest, not the sum.
    with ThreadPoolExecutor(max_workers=max(1, len(sources))) as pool_:
        futures = [pool_.submit(collect_one, c) for c in sources.values()]
        collected = [f.result() for f in futures]

    fresh = {}
    for rows in collected:
        for row in rows:
            if isinstance(row, dict) and policy.valid_id(row.get("pool")):
                fresh[row["pool"]] = _reading(row)

    pools = list(fresh)
    pools += [p for p in _known_pools(root) if p not in fresh and policy.valid_id(p)]

    out = []
    for pool in pools:
        new = fresh.get(pool)
        old = policy.stored_reading(root, pool)
        if new is None:
            new = {"pool": pool, "remaining_percent": None, "resets_at": None, "read_at": None, "source": NO_SOURCE}
        if new["remaining_percent"] is None and old and old.get("remaining_percent") is not None \
                and old.get("read_at"):
            note = f"{new['source']} (kept last verified reading)"
            new = {"pool": pool, "remaining_percent": old["remaining_percent"], "resets_at": old.get("resets_at"),
                   "read_at": old["read_at"], "source": note}
        write_json(Path(root) / "usage" / f"{pool}.json", new)
        out.append(dict(new, freshness=policy.classify_reading(new, cfg["usage_fresh_minutes"], now)))
    return out


def main(argv=None) -> int:
    """``python -m waypoint_runner.usage --folder DIR``: collect once, write usage/<pool>.json, exit.
    `waypoint usage` starts this detached when it finds no fresh reading; nothing else refreshes usage."""
    ap = argparse.ArgumentParser(prog="waypoint_runner.usage", description="Refresh usage/<pool>.json once.")
    ap.add_argument("--folder", required=True, help="Waypoint data folder")
    a = ap.parse_args(argv)
    readings(a.folder)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
