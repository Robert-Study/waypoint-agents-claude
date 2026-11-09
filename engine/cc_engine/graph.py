"""Pure graph model: readiness, waves (longest-path depth) and transitive reduction.

Nodes are dicts with ``id``, ``deps`` and optional ``status``. A dep is an id or an
``{"id", "crosses"}`` object (``crosses`` names the output the dependant reads).
Edges run dependency -> dependant, i.e. ``(src=dep, dst=node)``.
"""
from __future__ import annotations

from .store import STATUSES

__all__ = ["STATUSES", "readiness", "waves", "transitive_reduction", "ready_nodes", "missing_crosses"]


def _deps(node):
    seen, out = set(), []
    for d in node.get("deps") or []:
        d = d.get("id") if isinstance(d, dict) else d
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def readiness(nodes) -> dict:
    """``{id: display_status}``; ``ready`` = not started (or stored ready) with every dep done."""
    status = {n["id"]: n.get("status") or "not_started" for n in nodes}
    out = {}
    for n in nodes:
        s = status[n["id"]]
        if s in ("not_started", "ready"):
            s = "ready" if all(status.get(d) == "done" for d in _deps(n)) else "not_started"
        out[n["id"]] = s
    return out


def waves(nodes) -> dict:
    """Longest-path depth from roots (0-based). Unknown deps are ignored. Cycles raise ValueError."""
    by_id = {n["id"]: n for n in nodes}
    depth: dict = {}
    visiting: set = set()

    def visit(nid):
        if nid in depth:
            return depth[nid]
        if nid in visiting:
            raise ValueError(f"dependency cycle through {nid!r}")
        visiting.add(nid)
        ds = [d for d in _deps(by_id[nid]) if d in by_id]
        depth[nid] = 1 + max(visit(d) for d in ds) if ds else 0
        visiting.discard(nid)
        return depth[nid]

    for n in nodes:
        visit(n["id"])
    return {n["id"]: depth[n["id"]] for n in nodes}


def transitive_reduction(nodes) -> list:
    """Display edges ``[(src, dst)]`` with edges implied by longer paths removed."""
    waves(nodes)  # validates acyclicity
    by_id = {n["id"]: n for n in nodes}
    anc: dict = {}

    def ancestors(nid):
        if nid not in anc:
            acc = set()
            for d in _deps(by_id[nid]):
                if d in by_id:
                    acc.add(d)
                    acc |= ancestors(d)
            anc[nid] = acc
        return anc[nid]

    edges = []
    for n in nodes:
        ds = [d for d in _deps(n) if d in by_id]
        for u in ds:
            if not any(u in ancestors(w) for w in ds if w != u):
                edges.append((u, n["id"]))
    return edges


def ready_nodes(nodes) -> list:
    """Node dicts that may start now (not strict waves), ordered by wave then id."""
    r = readiness(nodes)
    w = waves(nodes)
    return sorted((n for n in nodes if r[n["id"]] == "ready"), key=lambda n: (w[n["id"]], n["id"]))


def missing_crosses(nodes) -> list:
    """``[(src, dst)]`` edges whose dependant does not name what crosses (removable waits).
    A node may carry ``crosses`` as ``{dep: text}`` (the board shape) or inside object deps."""
    out = []
    for n in nodes:
        named = dict(n.get("crosses") or {}) if isinstance(n.get("crosses"), dict) else {}
        for d in n.get("deps") or []:
            if isinstance(d, dict) and str(d.get("crosses") or "").strip():
                named[d.get("id")] = d["crosses"]
        out += [(d, n["id"]) for d in _deps(n) if not str(named.get(d) or "").strip()]
    return out
