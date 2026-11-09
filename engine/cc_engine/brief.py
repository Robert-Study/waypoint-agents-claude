"""Morning brief and todo triage (F4).

``GET /api/brief?since=<ISO time>|hours=<n>`` (default the last 12 hours) returns one plain-English
paragraph (what finished, what failed, what needs Owner, how much usage is left) plus the counts it
was built from and the todo triage. Everything is derived from the ``events`` table, the open
questions, the node statuses and the usage cache; nothing is invented (a usage pool with no reading
is left out) and no provider is called.

A task counts once, by its latest status event inside the window: ``done`` finished, ``error``
failed. Needs-you is what is waiting right now: tasks in ``needs_you`` plus open questions, counted
once per task.

Triage (``triage(store)``): open todos that are near-duplicates (normalised wording) are grouped
under the oldest one, and each remaining open todo that clearly fits one active project gets that
project proposed (keyword scoring over titles and briefs, as in ``todo.py``). Merging is applied only
by Owner, with ``POST /api/todos/merge``.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

from . import needs, todo
from .api import HttpError

DEFAULT_HOURS = 12
MAX_HOURS = 24 * 31
FAIL_TITLES = 3
USAGE_SHOWN = 4
SIMILAR_RATIO, SIMILAR_JACCARD = 0.82, 0.7
USAGE_FIRST = ("claude_5h", "claude_week", "codex_5h", "codex_week")


# ---------------------------------------------------------------- time
def _utc(value) -> datetime:
    text = str(value).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    t = datetime.fromisoformat(text)
    return t.astimezone(timezone.utc) if t.tzinfo else t.replace(tzinfo=timezone.utc)


def window(since=None, hours=None, now=None):
    """``(start, now, hours)``; ``since`` (ISO time) wins over ``hours``; default 12 hours."""
    now = now or datetime.now(timezone.utc)
    if since not in (None, ""):
        try:
            start = _utc(since)
        except ValueError:
            raise ValueError("since must be an ISO time such as 2026-11-04T08:00:00Z")
    else:
        try:
            span = float(hours) if hours not in (None, "") else DEFAULT_HOURS
        except (TypeError, ValueError):
            raise ValueError("hours must be a number")
        if span <= 0 or span > MAX_HOURS:
            raise ValueError(f"hours must be between 0 and {MAX_HOURS}")
        start = now - timedelta(hours=span)
    if start > now:
        raise ValueError("since is in the future")
    span = (now - start).total_seconds() / 3600
    return start, now, (int(round(span)) if abs(span - round(span)) < 0.05 else round(span, 1))


# ---------------------------------------------------------------- facts
def _outcomes(store, start):
    """Latest done/error status event per task inside the window: ``{(project, node): status}``."""
    rows = store._q("SELECT project_id, node_id, data_json FROM events WHERE kind='status' AND at>=?"
                    " AND node_id IS NOT NULL ORDER BY seq", (start.isoformat(timespec="seconds"),))
    latest = {}
    for pid, nid, data_json in rows:
        try:
            status = json.loads(data_json or "{}").get("status")
        except ValueError:
            continue
        if status:
            latest[(pid, nid)] = status
    return latest


def _item(store, key):
    node = store.get_node(*key) or {}
    return {"project": key[0], "node": key[1], "title": node.get("title") or key[1]}


def _needs_you(store) -> int:
    seen = set()
    for p in store.list_projects():
        for n in store.list_nodes(p["id"]):
            if n["status"] == "needs_you":
                seen.add((p["id"], n["id"]))
    for q in needs.list_questions(store, open_only=True):
        seen.add((q.get("project_id"), q.get("node_id")) if q.get("node_id") else ("question", q["id"]))
    return len(seen)


def _usage_items(rows) -> list:
    out = []
    for r in rows or []:
        pct = r.get("remaining_percent")
        if not isinstance(pct, (int, float)) or isinstance(pct, bool) or r.get("status") not in ("FRESH", "STALE"):
            continue
        out.append({"pool": r.get("pool"), "label": r.get("label") or r.get("pool"),
                    "remaining_percent": pct, "status": r.get("status")})
    order = {p: i for i, p in enumerate(USAGE_FIRST)}
    out.sort(key=lambda u: order.get(u["pool"], len(order)))
    return out[:USAGE_SHOWN]


def _hours_word(hours):
    return "hour" if hours == 1 else f"{hours} hours"


def paragraph(hours, done, failed, failed_titles, needs_you, usage) -> str:
    span = _hours_word(hours)
    if not done and not failed:
        first = f"Nothing finished or failed in the last {span}."
    else:
        fin = f"{done} task{'' if done == 1 else 's'} finished" if done else "nothing finished"
        bad = f"{failed} failed" if failed else "nothing failed"
        if failed_titles:
            bad += " (" + ", ".join(failed_titles[:FAIL_TITLES]) + (", and more" if failed > FAIL_TITLES else "") + ")"
        first = f"In the last {span}, {fin} and {bad}."
    if needs_you:
        second = "1 thing needs you." if needs_you == 1 else f"{needs_you} things need you."
    else:
        second = "Nothing needs you."
    parts = [first, second]
    if usage:
        parts.append("Usage left: " + ", ".join(
            f"{u['label']} {int(round(u['remaining_percent']))}%"
            for u in usage) + ".")
    return " ".join(parts)


def build_brief(store, since=None, hours=None, now=None, usage_rows=None, usage_reader=None) -> dict:
    start, end, span = window(since, hours, now)
    latest = _outcomes(store, start)
    done_items = [_item(store, k) for k, s in latest.items() if s == "done"]
    failed_items = [_item(store, k) for k, s in latest.items() if s == "error"]
    waiting = _needs_you(store)
    if usage_rows is None and usage_reader is not None:
        try:
            usage_rows = usage_reader()
        except Exception:  # a missing usage cache never blocks the brief
            usage_rows = []
    usage_items = _usage_items(usage_rows)
    return {"since": start.isoformat(timespec="seconds"), "until": end.isoformat(timespec="seconds"), "hours": span,
            "paragraph": paragraph(span, len(done_items), len(failed_items), [i["title"] for i in failed_items],
                                   waiting, usage_items),
            "done": len(done_items), "failed": len(failed_items), "needs_you": waiting,
            "done_items": done_items, "failed_items": failed_items, "usage": usage_items,
            "triage": triage(store)}


# ---------------------------------------------------------------- triage
def _similarity(a, b):
    """0 when the two todos are not near-duplicates, else a score in (0, 1]."""
    ta, tb = todo.tokens(a), todo.tokens(b)
    if not ta or not tb:
        x, y = " ".join(str(a).lower().split()), " ".join(str(b).lower().split())
        return 1.0 if x == y else 0.0
    sa, sb = set(ta), set(tb)
    jaccard = len(sa & sb) / len(sa | sb)
    ratio = SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()
    near = jaccard >= SIMILAR_JACCARD or ratio >= SIMILAR_RATIO
    return round(max(jaccard, ratio), 2) if near else 0.0


def triage(store) -> dict:
    items = [t for t in todo.list_todos(store) if t["status"] == "open"]
    order = {t["id"]: i for i, t in enumerate(items)}
    parent = {t["id"]: t["id"] for t in items}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    score = {}
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            s = _similarity(a["text"], b["text"])
            if s:
                score[(a["id"], b["id"])] = s
                ra, rb = find(a["id"]), find(b["id"])
                if ra != rb:
                    parent[rb] = ra
    groups = {}
    for t in items:
        groups.setdefault(find(t["id"]), []).append(t)
    merges, keepers = [], []
    for members in groups.values():
        members.sort(key=lambda t: order[t["id"]])
        keep, rest = members[0], members[1:]
        keepers.append(keep)
        if rest:
            sims = [score.get((keep["id"], m["id"]), SIMILAR_JACCARD) for m in rest]
            merges.append({"keep": keep["id"], "merge": [m["id"] for m in rest], "text": keep["text"],
                           "similarity": min(sims), "texts": [m["text"] for m in rest]})
    merges.sort(key=lambda m: order[m["keep"]])
    suggestions = []
    for t in sorted(keepers, key=lambda t: order[t["id"]]):
        best = todo.propose_project(store, t["text"])
        if best:
            suggestions.append({"todo": t["id"], "text": t["text"], "project": best["project"],
                                "title": best["title"], "words": best["words"][:4]})
    return {"merges": merges, "suggestions": suggestions, "open": len(items)}


# ---------------------------------------------------------------- routes
def g_brief(h, q):
    try:
        return build_brief(h.app.store, since=q.get("since"), hours=q.get("hours"),
                           usage_reader=getattr(h.app, "usage_reader", None))
    except ValueError as exc:
        raise HttpError(400, {"error": "bad_request", "detail": str(exc)})


ROUTES = {"GET /api/brief": g_brief}
