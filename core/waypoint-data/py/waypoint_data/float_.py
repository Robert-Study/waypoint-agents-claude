"""Float and critical path (Python twin of src/float.ts, replayed from fixtures/float.json). Stdlib only.

Edges are finish-to-start; a node with children (`parent`) also waits for all of them. Done/skipped/carried nodes are
zero remaining work (early=late=float=0, not critical) and never constrain anything. Deps on missing nodes are ignored.
A bad estimate (not finite, negative, not a number) counts as 1; 0 is allowed. A cycle raises ValueError("cycle: ...").
"""
import math

FINISHED = frozenset(("done", "skipped", "carried"))
EPS = 1e-9


def _duration(n):
    if n.get("status") in FINISHED:
        return 0
    e = n.get("estimate")
    if isinstance(e, (int, float)) and not isinstance(e, bool) and math.isfinite(e) and e >= 0:
        return e
    return 1


def _tidy(x):
    return int(x) if isinstance(x, float) and x == int(x) else x


def compute_float(nodes):
    ids = {n["id"] for n in nodes}
    preds = {n["id"]: {} for n in nodes}
    succs = {n["id"]: {} for n in nodes}

    def edge(frm, to):
        if frm in ids and to in ids:
            preds[to][frm] = True
            succs[frm][to] = True

    for n in nodes:
        for d in n.get("deps") or []:
            edge(d if isinstance(d, str) else d["id"], n["id"])
        if n.get("parent"):
            edge(n["id"], n["parent"])  # child finishes before parent

    indeg = {n["id"]: len(preds[n["id"]]) for n in nodes}
    order = []
    queue = [n["id"] for n in nodes if indeg[n["id"]] == 0]
    while queue:
        i = queue.pop(0)
        order.append(i)
        for s in succs[i]:
            indeg[s] -= 1
            if indeg[s] == 0:
                queue.append(s)
    if len(order) != len(nodes):
        seen = set(order)
        raise ValueError("cycle: " + " -> ".join(n["id"] for n in nodes if n["id"] not in seen))

    by_id = {n["id"]: n for n in nodes}

    def done(i):
        return by_id[i].get("status") in FINISHED

    dur = {n["id"]: _duration(n) for n in nodes}
    early, finish, end = {}, {}, 0
    for i in order:
        es = 0
        if not done(i):
            for p in preds[i]:
                es = max(es, finish[p])
        early[i] = es
        finish[i] = es + dur[i]
        end = max(end, finish[i])

    late_start = {}
    for i in reversed(order):
        if done(i):
            late_start[i] = 0
            continue
        lf = end
        for s in succs[i]:
            if not done(s):
                lf = min(lf, late_start[s])
        late_start[i] = lf - dur[i]

    out = []
    for n in nodes:
        i = n["id"]
        e, l = early[i], late_start[i]
        f = 0 if done(i) else max(0, l - e)
        clean = 0 if abs(f) < EPS else f
        out.append({"id": i, "early": _tidy(e), "late": _tidy(e if clean == 0 else l), "float": _tidy(clean),
                    "critical": (not done(i)) and clean == 0})
    return out
