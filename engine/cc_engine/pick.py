"""Pick for me (Meridian v3 F1): one model + effort for a task, a chat or a plan.

``choose(kind, rows, node=None, failures=None)`` is pure and deterministic: the same registry rows
(``models.build_registry``), node and failure counts always give the same answer, ties broken by id.

Inputs it weighs
- Elo from the registry (unknown Elo counts as ``UNKNOWN_ELO``, below every sourced number).
- Usage left on metered pools: under ``USAGE_FLOOR`` % the model is skipped; under 50 % it loses
  points. Easy tasks keep away from metered (Claude) usage entirely when a free route can do them.
- Recent failures: the node's own attempts on its current model, plus any counts passed in.
- Health: a fresh failed probe (model_health.py) skips the model; stale or unknown health does not.
- Task type from the node's lane, title and goal (easy / normal / hard) and its attempts.

Candidates
- ``chat`` and ``plan``: orchestration models in dsh's chat picker (``kind == "chat"``, role
  ``agent``, ``in_picker``); Claude and Codex never (they have no ``agent`` role).
- ``task``: anything Waypoint can dispatch automatically (a ``route``, not a Copy Prompt hand-off).
Switched-off rows (Waypoint toggles) and rows with no access are never picked.

HTTP: ``GET /api/pick?kind=task|chat|plan&project=&id=`` -> ``{model, effort, why, label, route, dsh}``.
"""
from __future__ import annotations

import re

KINDS = ("task", "chat", "plan")
EFFORTS = ("low", "medium", "high", "max")
UNKNOWN_ELO = 1200
USAGE_FLOOR = 0.0  # note 146: every pool not in DEFAULT_FLOORS has a 0% floor
# Approved default usage floors (% left) by provider (Owner, note 146: Codex 20%, everything else 0%);
# a run's own setting may override.
# Matched by pool-name prefix (claude_5h, claude_week, codex_5h, antigravity_gemini_5h ...).
DEFAULT_FLOORS = {"claude": 0.0, "codex": 20.0, "antigravity": 0.0}


def floor_for(pool, fallback):
    """Default floor for a usage pool (prefix match on DEFAULT_FLOORS); ``fallback`` if unlisted."""
    name = str(pool or "")
    for key, value in DEFAULT_FLOORS.items():
        if name == key or name.startswith(key + "_"):
            return value
    return fallback
FAILURE_PENALTY = 150
METERED_PENALTY = {"easy": 1000, "normal": 100, "hard": 0}
MANUAL_ROUTES = ("manual", "gemini_app", "chatgpt_web", "antigravity_manual")

EASY_WORDS = ("typo", "readme", "docs", "doc ", "rename", "comment", "format", "wording", "copy edit",
              "spelling", "lint")
HARD_WORDS = ("refactor", "concurren", "race", "architecture", "security", "migrat", "rewrite", "deadlock",
              "performance", "algorithm")


def task_type(node) -> str:
    if not isinstance(node, dict):
        return "normal"
    if int(node.get("attempts") or 0) >= 1:
        return "hard"
    text = " ".join(str(node.get(k) or "") for k in ("title", "goal")).lower() + " "
    if any(w in text for w in HARD_WORDS):
        return "hard"
    if str(node.get("lane") or "").lower() == "docs" or any(w in text for w in EASY_WORDS):
        return "easy"
    return "normal"


def effort_for(kind, node=None) -> str:
    if kind == "chat":
        return "medium"
    if kind == "plan":
        return "high"
    t = task_type(node)
    if t == "easy":
        return "low"
    if t == "hard":
        return "max" if int((node or {}).get("attempts") or 0) >= 2 else "high"
    return "medium"


def _failed(row) -> bool:
    h = row.get("health")
    return isinstance(h, dict) and h.get("ok") is False and not h.get("stale")


def _usage_left(row):
    u = row.get("usage")
    if isinstance(u, dict) and isinstance(u.get("remaining_percent"), (int, float)) and u.get("status") in ("FRESH", "STALE"):
        return float(u["remaining_percent"])
    return None


def _metered(row) -> bool:
    u = row.get("usage")
    return isinstance(u, dict) and bool(u.get("pool"))


def candidates(kind, rows) -> list:
    out = []
    for r in rows or []:
        if not isinstance(r, dict) or not r.get("id"):
            continue
        if r.get("enabled") is False or r.get("access") is False or _failed(r):
            continue
        if kind in ("chat", "plan"):
            if r.get("kind") != "chat" or "agent" not in (r.get("roles") or []) or r.get("in_picker") is False:
                continue
        else:
            if not r.get("route") or r.get("route") in MANUAL_ROUTES or r.get("kind") == "manual":
                continue
        left = _usage_left(r)
        if left is not None and (left <= 0 or left < floor_for(r["usage"].get("pool"), USAGE_FLOOR)):
            continue
        out.append(r)
    return out


def _norm(s):
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def failures_for(node, rows) -> dict:
    """``{row id: count}`` from the node's attempts on its current model."""
    if not isinstance(node, dict) or not node.get("model") or not int(node.get("attempts") or 0):
        return {}
    want = _norm(node["model"])
    out = {}
    for r in rows or []:
        names = {_norm(r.get("model")), _norm((r.get("dsh") or {}).get("model"))} - {""}
        if want in names:
            out[r["id"]] = int(node["attempts"])
    return out


def score(row, kind, ttype, failures) -> float:
    s = float(row["elo"]) if isinstance(row.get("elo"), (int, float)) else float(UNKNOWN_ELO)
    left = _usage_left(row)
    if left is not None and left < 50:
        s -= (50 - left) * 4
    if kind == "task" and _metered(row):
        s -= METERED_PENALTY.get(ttype, 100)
    s -= FAILURE_PENALTY * int((failures or {}).get(row["id"], 0))
    return s


def _why(row, kind, ttype, n):
    bits = []
    if isinstance(row.get("elo"), (int, float)):
        bits.append(f"~{int(row['elo'])} Elo")
    left = _usage_left(row)
    if left is not None:
        bits.append(f"{int(left)}% usage left")
    elif not _metered(row):
        bits.append("free")
    what = {"chat": "chat model", "plan": "planning model"}.get(kind, f"model for a {ttype} task")
    tail = f" ({', '.join(bits)})" if bits else ""
    return f"Strongest working {what} of {n}{tail}."[:120]


def choose(kind, rows, node=None, failures=None) -> dict:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    cands = candidates(kind, rows)
    if not cands:
        return {"model": None, "effort": None, "why": "No working model is switched on for this.",
                "label": None, "route": None, "dsh": None}
    ttype = task_type(node) if kind == "task" else kind
    fails = dict(failures_for(node, rows))
    for k, v in (failures or {}).items():
        fails[k] = fails.get(k, 0) + int(v)
    best = sorted(cands, key=lambda r: (-score(r, kind, ttype, fails), str(r["id"])))[0]
    return {"model": best["id"], "effort": effort_for(kind, node), "why": _why(best, kind, ttype, len(cands)),
            "label": best.get("label"), "route": best.get("route"), "dsh": best.get("dsh")}


# ---------------------------------------------------------------- HTTP
def _http_error(status, body):
    from .api import HttpError
    return HttpError(status, body)


def g_pick(h, q):
    from . import models
    kind = (q.get("kind") or "task").strip().lower()
    if kind not in KINDS:
        raise _http_error(400, {"error": "bad_request", "detail": "kind must be task, chat or plan"})
    node = None
    store = getattr(getattr(h, "app", None), "store", None)
    if q.get("project") and q.get("id") and store is not None:
        try:
            node = store.get_node(q["project"], q["id"])
        except Exception:
            node = None
    rows = models.build_registry(models._sources(h), models._usage_rows(h))
    return choose(kind, rows, node=dict(node) if node else None)


ROUTES = {"GET /api/pick": g_pick}
