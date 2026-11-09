"""The ``plan_draft`` and ``brain_rebuild`` actions.

``register(handlers, ...)`` adds both handlers to a dict; the runner discovers this module by its
``actions_`` name. Each handler is ``handler(action, ctx=None) -> dict`` and returns
``{status: done|refused|failed, result?, error?, todo?}``. ``ctx`` may carry ``root``, ``now``,
``drafters`` and ``brain_builder``; they override what ``register`` was given.

plan_draft  payload {goal, project?}
    Writes ``plans/drafts/<action id>.json`` in the ``waypoint.plan_stage.v1`` draft shape for diff review.
    It reuses the engine planner as a library (prompt, reply parser, validator, plan builder); it never
    starts or needs the engine process, never imports the draft as a project and never accepts it: only
    Owner accepts a plan, through an owner-only action.
    Routing: a drafter is a callable ``drafter(prompt, cwd) -> dict`` keyed by agent id. Candidates are tried
    free pools first. A route is called only when ``policy.allowed`` says so: its toggle is on, and a metered
    route (claude headless) also needs a floor Owner has set and a fresh reading above it. An UNSET floor
    holds a metered route. This module holds no floor value. With no ``drafters`` given, the default is the
    engine's Claude Code planning job on agent ``claude_code``.

brain_rebuild  payload {scope?: all|changed}
    Rebuilds ``brain/index.json`` (``waypoint.brain_index.v1``) from the engine brain builder, without
    writing the engine's own cache. The builder reads every source whichever scope is asked, so ``changed``
    gives the same index as ``all``. It makes no model call, so it needs no route, toggle or floor. A builder
    failure leaves the previous index untouched.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import policy, usage

DEFAULT_PLAN_ROUTE = "claude_code"
SCOPES = ("all", "changed")

# engine brain provenance -> brain_index provenance
_PROVENANCE = {
    "explicit_user_statement": "owner",
    "user_reported_calibration": "owner",
    "vetted_source": "vetted",
    "agent_synthesis": "agent",
}
_NOTE_KINDS = ("mental_model", "doctrine", "learning", "source", "robert_words", "other")
# engine node kind -> brain_index kind (brain notes use their own note_kind when it is a known one)
_KIND = {"preference": "robert_words", "skill": "doctrine", "instruction": "doctrine"}


def _iso(dt) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _failed(error) -> dict:
    return {"status": "failed", "error": str(error)}


# ---------------------------------------------------------------- plan_draft
def _claude_drafter(prompt, cwd=None) -> dict:
    from cc_engine import planner  # noqa: PLC0415  (library import; starts nothing)

    return planner.run_claude(prompt, cwd, planner.DRAFT_TIMEOUT)


def default_drafters() -> dict:
    return {DEFAULT_PLAN_ROUTE: _claude_drafter}


def _choose_route(root, drafters, now):
    """(agent id, None) for the first allowed route, free pools first; else (None, refusal dict)."""
    agents = [a for a in policy.agents(root) if a["id"] in drafters]
    agents.sort(key=lambda a: 0 if policy.is_free_pool(a.get("pool")) else 1)  # stable: free first
    held, todo = [], None
    for a in agents:
        d = policy.allowed(root, a["id"], now=now)
        if d.allowed:
            return a["id"], None
        hint = " (set the floor in Waypoint)" if d.reason == "floor_unset" else ""
        held.append(f"{a['id']}: {d.reason.replace('_', ' ')}{hint}")
        todo = todo or d.todo
    if not held:
        return None, {"status": "refused", "error": "no planning route is configured: no agent is set up to draft plans"}
    out = {"status": "refused", "error": "no planning route may run now; " + "; ".join(held)}
    if todo:
        out["todo"] = todo
    return None, out


def plan_draft(action, root, drafters=None, now=None) -> dict:
    payload = action.get("payload") or {}
    goal = payload.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        return _failed("plan_draft needs payload.goal")
    from cc_engine import planner  # noqa: PLC0415

    project = payload.get("project") or planner.slug(goal)
    if not policy.valid_id(project) or not policy.valid_id(action.get("id")):
        return _failed("plan_draft needs a valid project name and action id")
    when = (now or (lambda: datetime.now(timezone.utc)))()

    drafters = default_drafters() if drafters is None else drafters
    route, refusal = _choose_route(root, drafters, when)
    if refusal:
        return refusal
    drafter = drafters[route]

    try:
        options = planner.recommend_options()
        values = {"title": project, "goal": goal.strip(), "cwd": "(not set)",
                  "answers": "(none: this is a draft for the owner to review)",
                  "max_nodes": planner.MAX_NODES, "models": planner._models_text(options),
                  "note": "", "problems": "", "constraints": ""}
        raw = drafter(planner.render_prompt("draft", values), None)
        found = planner.problems(raw)
        for _ in range(planner.REPAIR_ATTEMPTS):
            if not found:
                break
            values["problems"] = ("\nYour previous draft could not be imported. Fix every one of these:\n"
                                  + "\n".join(f"- {p}" for p in found) + "\nPrevious draft:\n"
                                  + json.dumps(raw, ensure_ascii=False)[:20000] + "\n")
            raw = drafter(planner.render_prompt("draft", values), None)
            found = planner.problems(raw)
        if found:
            return _failed("the draft was not usable: " + "; ".join(found[:5]))
        built = planner.build_plan({"title": project, "cwd": None}, raw, project, allowed_models=options)
    except Exception as exc:
        return _failed(f"{type(exc).__name__}: {exc}")

    draft = {"schema": "waypoint.plan_stage.v1", "stage": "draft", "project": project,
             "brief": built["brief"], "created_at": _iso(when), "nodes": built["nodes"]}
    rel = f"plans/drafts/{action['id']}.json"
    usage.write_json(Path(root) / rel, draft)
    return {"status": "done", "result": {"path": rel, "project": project, "route": route,
                                         "nodes": len(draft["nodes"])}}


# ---------------------------------------------------------------- brain_rebuild
def _engine_brain_builder(scope) -> dict:
    from cc_engine import brain_index  # noqa: PLC0415

    return brain_index.build_graph(write_cache=False)


def to_index(graph, when) -> dict:
    """Engine brain graph -> waypoint.brain_index.v1. Folders are tree noise, not notes."""
    nodes = [n for n in graph.get("nodes") or [] if isinstance(n, dict) and n.get("kind") != "folder" and n.get("id")]
    ids = {n["id"] for n in nodes}
    out_links: dict = {}
    for link in graph.get("links") or []:
        s, t = link.get("source"), link.get("target")
        if s in ids and t in ids and s != t:
            out_links.setdefault(s, set()).add(t)
    notes = []
    for n in nodes:
        kind = n.get("kind")
        if kind == "brain":
            kind = n.get("note_kind") if n.get("note_kind") in _NOTE_KINDS else "other"
        else:
            kind = _KIND.get(kind, "other")
        note = {"id": n["id"], "path": str(n.get("path") or n["id"]), "title": str(n.get("label") or n["id"]),
                "kind": kind, "provenance": _PROVENANCE.get(n.get("provenance"), "unknown"),
                "tags": [str(t) for t in n.get("tags") or []],
                "summary": n.get("summary") or None, "links": sorted(out_links.get(n["id"], ()))}
        if n.get("updated_at"):
            note["updated_at"] = str(n["updated_at"])
        notes.append(note)
    return {"schema": "waypoint.brain_index.v1", "generated_at": _iso(when), "notes": notes}


def brain_rebuild(action, root, builder=None, now=None) -> dict:
    scope = (action.get("payload") or {}).get("scope", "all")
    if scope not in SCOPES:
        return _failed("brain_rebuild scope must be all or changed")
    when = (now or (lambda: datetime.now(timezone.utc)))()
    try:
        index = to_index((builder or _engine_brain_builder)(scope), when)
        usage.write_json(Path(root) / "brain" / "index.json", index)
    except Exception as exc:
        return _failed(f"{type(exc).__name__}: {exc}")
    return {"status": "done", "result": {"path": "brain/index.json", "notes": len(index["notes"]), "scope": scope}}


# ---------------------------------------------------------------- registration
def register(handlers, root=None, drafters=None, brain_builder=None, now=None):
    """Add ``plan_draft`` and ``brain_rebuild``. Handlers take (action, ctx=None)."""

    def _folder(ctx):
        return ctx.get("root", root)

    def h_plan_draft(action, ctx=None):
        ctx = ctx or {}
        folder = _folder(ctx)
        if folder is None:
            return _failed("no data folder configured")
        return plan_draft(action, folder, drafters=ctx.get("drafters", drafters), now=ctx.get("now", now))

    def h_brain_rebuild(action, ctx=None):
        ctx = ctx or {}
        folder = _folder(ctx)
        if folder is None:
            return _failed("no data folder configured")
        return brain_rebuild(action, folder, builder=ctx.get("brain_builder", brain_builder), now=ctx.get("now", now))

    handlers["plan_draft"] = h_plan_draft
    handlers["brain_rebuild"] = h_brain_rebuild
    return handlers
