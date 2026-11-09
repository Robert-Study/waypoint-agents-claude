"""Ready-node detection for the Waypoint runner.

Pure functions over node dicts (the caller loads them through data-pyclient). A node is ready when
every dependency is ``done`` (only ``done`` satisfies a dependency), it is not already claimed, it is
not parked (``needs_you`` or in the ``parked`` set) and its status is ``not_started`` or ``ready``.
``evidence`` and alternatives are never prerequisites. Order is ``slot`` then ``id``; a null slot
is earliest. Board columns and screen position never create order.
"""
from __future__ import annotations

__all__ = ["dep_ids", "is_ready", "ready_nodes"]

STARTABLE = ("not_started", "ready")


def dep_ids(node) -> list:
    """Dependency ids, accepting plain ids and ``{"id", "crosses"}`` objects; duplicates removed in order."""
    seen, out = set(), []
    for d in node.get("deps") or []:
        did = d.get("id") if isinstance(d, dict) else d
        if did not in seen:
            seen.add(did)
            out.append(did)
    return out


def _slot_key(node):
    slot = node.get("slot")
    return slot if isinstance(slot, int) and not isinstance(slot, bool) else 0


def is_ready(node, by_id, claimed=(), parked=()) -> bool:
    nid = node.get("id")
    if node.get("status", "not_started") not in STARTABLE:
        return False
    if node.get("claim") or nid in claimed:
        return False
    if nid in parked:
        return False
    return all(by_id.get(d, {}).get("status") == "done" for d in dep_ids(node))


def ready_nodes(nodes, claimed=(), parked=()) -> list:
    """Node dicts that may start now, ordered by ``(slot, id)``. ``claimed`` and ``parked`` are id sets."""
    nodes = list(nodes or [])
    by_id = {n["id"]: n for n in nodes}
    claimed, parked = set(claimed or ()), set(parked or ())
    ready = [n for n in nodes if is_ready(n, by_id, claimed, parked)]
    return sorted(ready, key=lambda n: (_slot_key(n), str(n.get("id"))))
