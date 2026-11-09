"""Score a draft task graph and repair overlapping parallel writers. Pure stdlib, no store.

A draft task is ``{"id", "deps": [...], "files": [paths it writes], "cost"?: number}``.

``score(nodes)`` measures what makes a graph good to run:
- ``critical_path``: the longest dependency chain, in tasks (or summed ``cost`` when given), and
  ``critical_path_ids`` the chain itself. Shorter finishes sooner.
- ``waves`` and ``width_by_wave``: longest-path depth from the roots (the same rule as
  ``graph.waves``); ``peak_width`` is the largest wave, the most tasks that start together.
- ``overlaps``: pairs of tasks that can run at the same time (neither depends on the other, even
  indirectly) and write the same file. Paths compare case-insensitively with ``/`` separators; a
  path ending in ``/`` covers everything below it and ``*``/``?`` globs match.
- ``cycle``: the ids of one dependency cycle, or None. A cycle makes the graph unusable.

``repair(nodes)`` removes every overlap, one pair at a time: it serialises the pair (one waits for
the other) when that costs no critical-path time, otherwise reassigns the shared file to one owner
(the other task keeps its own files and records a ``handoff`` saying who applies its change),
and serialises when neither task has other work. Cycles raise ``CycleError``.
``rank_key(score)`` orders candidates: no cycle, fewest overlaps, shortest critical path, widest.
"""
from __future__ import annotations

import copy
import fnmatch

from .node_kinds import dep_id

STRATEGIES = ("auto", "serialise", "reassign")
MAX_REPAIRS = 200


class CycleError(ValueError):
    pass


# ---------------------------------------------------------------- helpers
def _deps(node, known):
    out = []
    for d in node.get("deps") or []:
        i = dep_id(d)
        if isinstance(i, str) and i in known and i not in out:
            out.append(i)
    return out


def norm_path(p):
    if not isinstance(p, str):
        return None
    s = p.strip().replace("\\", "/").lower()
    while s.startswith("./"):
        s = s[2:]
    while "//" in s:
        s = s.replace("//", "/")
    return s or None


def files_of(node) -> list:
    out = []
    for f in node.get("files") or []:
        n = norm_path(f)
        if n and n not in out:
            out.append(n)
    return out


def _is_pattern(p):
    return p.endswith("/") or any(c in p for c in "*?[")


def _covers(pattern, path):
    if pattern.endswith("/"):
        return path.startswith(pattern) or path == pattern.rstrip("/")
    return fnmatch.fnmatchcase(path, pattern)


def shared_files(fa, fb) -> list:
    """The paths two file lists both write; for a directory or glob, the more specific path."""
    out = []
    for f in fa:
        for g in fb:
            hit = None
            if f == g:
                hit = f
            elif _is_pattern(f) and _covers(f, g):
                hit = g
            elif _is_pattern(g) and _covers(g, f):
                hit = f
            if hit and hit not in out:
                out.append(hit)
    return out


def find_cycle(nodes):
    """Ids along one dependency cycle (first id repeated at the end), or None."""
    by_id = {n["id"]: n for n in nodes}
    state, stack = {}, []

    def visit(nid):
        state[nid] = 1
        stack.append(nid)
        for d in _deps(by_id[nid], by_id):
            if state.get(d) == 1:
                return stack[stack.index(d):] + [d]
            if d not in state:
                found = visit(d)
                if found:
                    return found
        stack.pop()
        state[nid] = 2
        return None

    for n in nodes:
        if n["id"] not in state:
            found = visit(n["id"])
            if found:
                return found
    return None


def _ancestors(nodes):
    by_id = {n["id"]: n for n in nodes}
    memo = {}

    def anc(nid):
        if nid not in memo:
            out = set()
            for d in _deps(by_id[nid], by_id):
                out.add(d)
                out |= anc(d)
            memo[nid] = out
        return memo[nid]

    return {n["id"]: anc(n["id"]) for n in nodes}


def _cost(node):
    c = node.get("cost")
    return float(c) if isinstance(c, (int, float)) and not isinstance(c, bool) and c > 0 else 1.0


def _num(x):
    return int(x) if float(x).is_integer() else round(x, 2)


# ---------------------------------------------------------------- score
def score(nodes) -> dict:
    nodes = [n for n in nodes or [] if isinstance(n, dict) and isinstance(n.get("id"), str)]
    out = {"tasks": len(nodes), "critical_path": None, "critical_path_ids": [], "waves": None,
           "width_by_wave": [], "peak_width": None, "overlaps": [], "cycle": None,
           "undeclared": [n["id"] for n in nodes if not files_of(n)], "problems": [],
           "ok": False, "needs_repair": False}
    cycle = find_cycle(nodes)
    if cycle:
        out["cycle"] = cycle
        out["problems"] = ["dependency cycle: " + " → ".join(cycle)]
        return out
    by_id = {n["id"]: n for n in nodes}
    order, seen = [], set()

    def topo(nid):
        if nid in seen:
            return
        seen.add(nid)
        for d in _deps(by_id[nid], by_id):
            topo(d)
        order.append(nid)

    for n in nodes:
        topo(n["id"])
    depth, finish, via = {}, {}, {}
    for nid in order:
        ds = _deps(by_id[nid], by_id)
        depth[nid] = 1 + max(depth[d] for d in ds) if ds else 0
        prev = max(ds, key=lambda d: (finish[d], -order.index(d))) if ds else None
        via[nid] = prev
        finish[nid] = (finish[prev] if prev else 0.0) + _cost(by_id[nid])
    if nodes:
        end = max(order, key=lambda i: (finish[i], -order.index(i)))
        chain = []
        while end:
            chain.append(end)
            end = via[end]
        out["critical_path"] = _num(finish[chain[0]])
        out["critical_path_ids"] = chain[::-1]
        count = max(depth.values()) + 1
        widths = [0] * count
        for d in depth.values():
            widths[d] += 1
        out.update(waves=count, width_by_wave=widths, peak_width=max(widths))
    else:
        out.update(critical_path=0, waves=0, peak_width=0)
    anc = _ancestors(nodes)
    files = {n["id"]: files_of(n) for n in nodes}
    for i, a in enumerate(nodes):
        for b in nodes[i + 1:]:
            if a["id"] in anc[b["id"]] or b["id"] in anc[a["id"]]:
                continue
            shared = shared_files(files[a["id"]], files[b["id"]])
            if shared:
                out["overlaps"].append({"a": a["id"], "b": b["id"], "files": shared})
    for o in out["overlaps"]:
        out["problems"].append(f"tasks {o['a']} and {o['b']} can run at the same time and both write "
                               + ", ".join(o["files"]))
    out["needs_repair"] = bool(out["overlaps"])
    out["ok"] = not out["overlaps"]
    return out


def line(s) -> str:
    """One short line for Owner: critical path, width, waves, overlaps."""
    if s.get("cycle"):
        return "Dependency cycle: " + " → ".join(s["cycle"])
    n = len(s.get("overlaps") or [])
    return (f"{s['tasks']} tasks · critical path {s['critical_path']} · width {s['peak_width']} · "
            f"{s['waves']} waves · " + ("no overlaps" if not n else f"{n} overlap{'s' if n != 1 else ''}"))


def rank_key(s):
    """Lower is better: no cycle, fewest overlaps, shortest critical path, widest, fewest tasks."""
    if s.get("cycle"):
        return (1, 0, float("inf"), 0, s.get("tasks") or 0)
    return (0, len(s.get("overlaps") or []), float(s.get("critical_path") or 0), -(s.get("peak_width") or 0),
            s.get("tasks") or 0)


def best(scores):
    scores = [s for s in scores if s]
    return min(scores, key=rank_key) if scores else None


# ---------------------------------------------------------------- repair
def _with_dep(nodes, waiter, first):
    out = copy.deepcopy(nodes)
    for n in out:
        if n["id"] == waiter:
            n["deps"] = list(n.get("deps") or []) + [first]
    return out


def _reassign(nodes, keeper, giver, shared):
    out = copy.deepcopy(nodes)
    for n in out:
        if n["id"] != giver:
            continue
        n["files"] = [f for f in n.get("files") or [] if norm_path(f) not in shared
                      and not any(_is_pattern(norm_path(f) or "") and _covers(norm_path(f), s) for s in shared)]
        hand = list(n.get("handoff") or [])
        for f in shared:
            item = {"file": f, "to": keeper}
            if item not in hand:
                hand.append(item)
        n["handoff"] = hand
    return out


def _serialise_options(nodes, a, b):
    """(critical path, preference, nodes, waiter, first) for both directions, best first."""
    opts = []
    for pref, (waiter, first) in enumerate(((b, a), (a, b))):
        cand = _with_dep(nodes, waiter, first)
        s = score(cand)
        opts.append((float(s["critical_path"]), pref, cand, waiter, first))
    opts.sort(key=lambda o: (o[0], o[1]))
    return opts


def repair(nodes, strategy="auto") -> dict:
    """``{"nodes", "changes": [one sentence each], "score"}`` with every parallel overlap removed.
    The input list is never modified. Raises ``CycleError`` for a cyclic graph."""
    if strategy not in STRATEGIES:
        raise ValueError(f"strategy must be one of {', '.join(STRATEGIES)}")
    work = copy.deepcopy([n for n in nodes or [] if isinstance(n, dict) and isinstance(n.get("id"), str)])
    cycle = find_cycle(work)
    if cycle:
        raise CycleError("dependency cycle: " + " → ".join(cycle))
    changes = []
    for _ in range(MAX_REPAIRS):
        s = score(work)
        if not s["overlaps"]:
            return {"nodes": work, "changes": changes, "score": s}
        o = s["overlaps"][0]
        a, b, shared = o["a"], o["b"], o["files"]
        base = float(s["critical_path"])
        opts = _serialise_options(work, a, b)
        by = {n["id"]: n for n in work}
        own = {i: [f for f in files_of(by[i]) if f not in shared and not any(_covers(f, x) for x in shared
                                                                              if _is_pattern(f))]
               for i in (a, b)}
        can_give = [i for i in (a, b) if own[i]]
        free = opts[0][0] <= base
        if strategy == "serialise" or (strategy == "auto" and (free or not can_give)):
            _, _, work, waiter, first = opts[0]
            changes.append(f"{waiter} now waits for {first} (both write {', '.join(shared)}).")
            continue
        if not can_give:  # strategy == "reassign" but neither task has other work
            _, _, work, waiter, first = opts[0]
            changes.append(f"{waiter} now waits for {first} (both write {', '.join(shared)}).")
            continue

        def share(i):
            total = len(files_of(by[i])) or 1
            return (len(files_of(by[i])) - len(own[i])) / total

        # The task for which the shared files are the bigger part of its work keeps them.
        giver = max(can_give, key=lambda i: (-share(i), (a, b).index(i)))
        keeper = b if giver == a else a
        work = _reassign(work, keeper, giver, shared)
        changes.append(f"{', '.join(shared)} stays with {keeper}; {giver} hands its change to {keeper}.")
    raise RuntimeError("the overlaps could not be repaired")  # pragma: no cover - bounded loop guard
