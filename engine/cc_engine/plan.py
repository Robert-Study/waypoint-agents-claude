"""Plan import/export with hash-locked goals, done-checks and brief.

A plan file: ``{id, title, brief, cwd, created, nodes: [{id, title, goal, check, deps, owner,
lane, pool?, model?, effort?, recommended?, kind?, script?}]}``. Every node must carry a done-check
(written first).
``recommended`` is the planner's first-choice model id for the task (a short string or null);
sprints may still route elsewhere because they follow live usage.

Integrity rules (actor ``owner`` is Owner; everyone else is automatic):
- goal text and the project brief change only by the owner; otherwise ``GoalLocked``.
- a re-import that changes a node's done-check by a non-owner resets that node to
  ``not_started`` (its old pass no longer counts) and the new check becomes the lock.
- a plan imported by the ``router`` lands as a ``draft`` project; nothing runs until Owner
  accepts it (``accept_plan``). A router re-import that adds tasks, changes a check or moves the
  working directory sends an accepted project back to draft.

Loop fields (``node_kinds``): ``kind`` is splitter / worker / code / gate; a ``code`` task runs its
``script`` (or command check), never a model. A plan with ``edges_v2: true``, or any dependency
written as ``{"id", "crosses"}``, must name on every edge what crosses it; older plans with plain
string deps still import. ``nodes.deps_json`` keeps plain ids; kind, script and crosses live in the
``node_loops`` table and come back in ``export_plan``.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import graph, node_kinds
from .checks import CHECK_TYPES
from .store import OWNER, GoalLocked, NotAllowed, brief_hash, check_hash, clean_files, files_json, goal_hash, now_iso

__all__ = ["GoalLocked", "NotAllowed", "PlanError", "load_plan", "validate_plan", "import_plan",
           "export_plan", "change_goal", "change_check", "change_brief", "accept_plan", "verify_goals",
           "verify_integrity"]

ROUTER = "router"

OPTIONAL_FIELDS = ("pool", "model", "effort", "recommended")
RECOMMENDED_MAX = 64


class PlanError(ValueError):
    pass


def load_plan(path_or_dict) -> dict:
    if isinstance(path_or_dict, dict):
        return path_or_dict
    try:
        return json.loads(Path(path_or_dict).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanError(f"cannot read plan {path_or_dict}: {exc}") from exc


def validate_plan(plan: dict):
    if not isinstance(plan, dict) or not plan.get("id") or not isinstance(plan.get("nodes"), list):
        raise PlanError("plan needs an 'id' and a 'nodes' list")
    ids = [n.get("id") for n in plan["nodes"]]
    if any(not i for i in ids):
        raise PlanError("every node needs an id")
    if len(set(ids)) != len(ids):
        raise PlanError("node ids must be unique")
    known = set(ids)
    v2 = node_kinds.is_v2(plan)
    for n in plan["nodes"]:
        if not isinstance(n.get("goal"), str) or not n["goal"].strip():
            raise PlanError(f"node {n['id']} needs goal text")
        check = n.get("check")
        if not isinstance(check, dict) or check.get("type") not in CHECK_TYPES:
            raise PlanError(f"node {n['id']} needs a done-check of type {'/'.join(CHECK_TYPES)}")
        if check["type"] == "command" and not check.get("run"):
            raise PlanError(f"node {n['id']} command check needs 'run'")
        if check["type"] == "files_exist" and not check.get("paths"):
            raise PlanError(f"node {n['id']} files_exist check needs 'paths'")
        if check["type"] == "text_present" and not (check.get("path") and check.get("text")):
            raise PlanError(f"node {n['id']} text_present check needs 'path' and 'text'")
        rec = n.get("recommended")
        if rec is not None and (not isinstance(rec, str) or not rec.strip() or len(rec) > RECOMMENDED_MAX):
            raise PlanError(f"node {n['id']} recommended must be a model id of 1-{RECOMMENDED_MAX} characters")
        try:
            clean_files(n.get("files"))
        except ValueError as exc:
            raise PlanError(f"node {n['id']}: {exc}") from exc
        deps = n.get("deps") or []
        if not isinstance(deps, list):
            raise PlanError(f"node {n['id']} deps must be a list")
        if any(not isinstance(node_kinds.dep_id(d), str) for d in deps):
            raise PlanError(f"node {n['id']} deps must be node ids or {{\"id\", \"crosses\"}} objects")
        unknown = [node_kinds.dep_id(d) for d in deps if node_kinds.dep_id(d) not in known]
        if unknown:
            raise PlanError(f"node {n['id']} depends on unknown {unknown}")
        if v2:
            bad = node_kinds.edge_problems(n)
            if bad:
                raise PlanError(bad[0])
        bad = node_kinds.kind_problems(n)
        if bad:
            raise PlanError(bad[0])
    try:
        graph.waves([{"id": n["id"], "deps": node_kinds.split_deps(n.get("deps"))[0]} for n in plan["nodes"]])
    except ValueError as exc:
        raise PlanError(str(exc)) from exc


def import_plan(store, path_or_dict, actor="import", expected_revision=None, draft=None) -> str:
    """Create or refresh a project from a plan. Existing status/attempts/evidence are kept.

    Changing an existing node's goal or the project brief needs ``actor='owner'``; otherwise
    ``GoalLocked``. A changed done-check by a non-owner resets that node to ``not_started``.
    ``draft`` defaults to ``actor == 'router'`` (see the module docstring).
    """
    plan = load_plan(path_or_dict)
    validate_plan(plan)
    pid = plan["id"]
    nodes = plan["nodes"]
    draft = (actor == ROUTER) if draft is None else bool(draft)
    counts = {"new": 0, "reset": [], "draft": False}
    brief = plan.get("brief") or ""

    def fn(c):
        row = c.execute("SELECT created_at, goal_hash, cwd, status FROM projects WHERE id=?", (pid,)).fetchone()
        if row is not None and actor != OWNER and (row["goal_hash"] or "") != brief_hash(brief):
            raise GoalLocked(f"the brief of {pid} is locked; only the owner may change it")
        created = row["created_at"] if row else (plan.get("created") or now_iso())
        executable_change = row is None or (row["cwd"] or None) != (plan.get("cwd") or None)
        c.execute("INSERT INTO projects(id, title, brief, goal_hash, cwd, created_at, archived, status)"
                  " VALUES(?,?,?,?,?,?,0,'active') ON CONFLICT(id) DO UPDATE SET title=excluded.title,"
                  " brief=excluded.brief, goal_hash=excluded.goal_hash, cwd=excluded.cwd",
                  (pid, plan.get("title") or pid, brief, brief_hash(brief), plan.get("cwd"), created))
        stamp = now_iso()
        node_kinds.ensure_schema(c)
        for n in nodes:
            old_loop = node_kinds.read_row(c, pid, n["id"]) or {}
            loop_changed = ((old_loop.get("kind") or None) != (n.get("kind") or None)
                            or (old_loop.get("script") or None) != (n.get("script") or None))
            old = c.execute("SELECT goal_hash, check_hash, pool, model, effort, recommended, files_json FROM nodes"
                            " WHERE project_id=? AND id=?", (pid, n["id"])).fetchone()
            h = goal_hash(n["goal"])
            check, deps = json.dumps(n["check"]), json.dumps(node_kinds.split_deps(n.get("deps"))[0])
            ch = check_hash(n["check"])
            if old is None:
                counts["new"] += 1
                executable_change = True
                c.execute("INSERT INTO nodes(project_id, id, title, goal, goal_hash, check_json, check_hash,"
                          " deps_json, owner, lane, pool, model, effort, status, attempts, updated_at, recommended,"
                          " files_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'not_started',0,?,?,?)",
                          (pid, n["id"], n.get("title") or n["id"], n["goal"], h, check, ch, deps,
                           n.get("owner"), n.get("lane"), n.get("pool"), n.get("model"), n.get("effort"), stamp,
                           n.get("recommended"), files_json(n.get("files"))))
                continue
            if old["goal_hash"] != h and actor != OWNER:
                raise GoalLocked(f"goal of {n['id']} is locked; only the owner may change it")
            keep = {k: (n.get(k) if n.get(k) is not None else old[k]) for k in OPTIONAL_FIELDS}
            c.execute("UPDATE nodes SET title=?, goal=?, goal_hash=?, check_json=?, check_hash=?, deps_json=?,"
                      " owner=?, lane=?, pool=?, model=?, effort=?, recommended=?, files_json=?, updated_at=?"
                      " WHERE project_id=? AND id=?",
                      (n.get("title") or n["id"], n["goal"], h, check, ch, deps, n.get("owner"), n.get("lane"),
                       keep["pool"], keep["model"], keep["effort"], keep["recommended"],
                       files_json(n["files"]) if "files" in n else old["files_json"], stamp, pid, n["id"]))
            if old["check_hash"] != ch or loop_changed:
                executable_change = True
                if actor != OWNER:  # the old pass was against a different check: it no longer counts
                    counts["reset"].append(n["id"])
                    c.execute("UPDATE nodes SET status='not_started', attempts=0, evidence=NULL, actor=?"
                              " WHERE project_id=? AND id=?", (actor, pid, n["id"]))
        node_kinds.write_rows(c, pid, nodes)
        if draft and executable_change:
            counts["draft"] = True
            c.execute("UPDATE projects SET status='draft' WHERE id=?", (pid,))

    title = plan.get("title") or pid
    summary = f"Imported the plan '{title}' with {len(nodes)} tasks."
    if draft:
        summary = f"The Router drafted the plan '{title}' with {len(nodes)} tasks; it waits for Owner to accept it."
    store.mutate(expected_revision, fn, actor, summary, project_id=pid, kind="import",
                 data={"nodes": len(nodes), "reset": counts["reset"], "draft": counts["draft"]})
    for nid in counts["reset"]:
        store.event(pid, nid, "integrity", actor,
                    f"Task {nid} got a different done-check, so it starts again.", {"node": nid})
    return pid


def export_plan(store, project_id) -> dict:
    project = store.get_project(project_id)
    if project is None:
        raise KeyError(f"no project {project_id}")
    out_nodes = []
    stored = store.list_nodes(project_id)
    loops = node_kinds.rows(store, project_id)
    v2 = any((loops.get(n["id"]) or {}).get("crosses") for n in stored)
    for n in stored:
        loop = loops.get(n["id"]) or {}
        deps = node_kinds.join_deps(n["deps"], loop.get("crosses")) if v2 else n["deps"]
        item = {"id": n["id"], "title": n["title"], "goal": n["goal"], "check": n["check"],
                "deps": deps, "owner": n["owner"], "lane": n["lane"]}
        for k in OPTIONAL_FIELDS:
            if n.get(k) is not None:
                item[k] = n[k]
        if n.get("files"):
            item["files"] = list(n["files"])
        for k in ("kind", "script"):  # exactly as imported, so a round trip changes nothing
            if loop.get(k):
                item[k] = loop[k]
        if loop.get("risk"):
            item["risk"] = loop["risk"]
        if loop.get("scope"):
            item["scope"] = list(loop["scope"])
        out_nodes.append(item)
    out = {"id": project["id"], "title": project["title"], "brief": project["brief"],
           "cwd": project["cwd"], "created": project["created_at"], "nodes": out_nodes}
    if v2:
        out["edges_v2"] = True
    return out


def change_goal(store, project_id, node_id, new_goal, actor, expected_revision=None) -> int:
    """Only the owner may change goal text; anyone else gets ``GoalLocked``."""
    if actor != "owner":
        raise GoalLocked(f"goal of {node_id} is locked; {actor!r} is not the owner")
    if not isinstance(new_goal, str) or not new_goal.strip():
        raise PlanError("goal text must be non-empty")
    if store.get_node(project_id, node_id) is None:
        raise KeyError(f"no node {project_id}/{node_id}")

    def fn(c):
        c.execute("UPDATE nodes SET goal=?, goal_hash=?, updated_at=? WHERE project_id=? AND id=?",
                  (new_goal, goal_hash(new_goal), now_iso(), project_id, node_id))

    return store.mutate(expected_revision, fn, "owner", f"Owner changed the goal of task {node_id}.",
                        project_id=project_id, node_id=node_id, kind="goal")


def change_check(store, project_id, node_id, new_check, actor, expected_revision=None) -> int:
    """Only the owner may change a done-check; anyone else gets ``GoalLocked``."""
    if actor != OWNER:
        raise GoalLocked(f"the done-check of {node_id} is locked; {actor!r} is not the owner")
    validate_plan({"id": project_id, "nodes": [{"id": node_id, "goal": "x", "check": new_check, "deps": []}]})
    return store.update_node(project_id, node_id, {"check": new_check}, OWNER,
                             f"Owner changed the done-check of task {node_id}.",
                             expected_revision=expected_revision)


def change_brief(store, project_id, new_brief, actor, expected_revision=None) -> int:
    """Only the owner may change the project brief; anyone else gets ``GoalLocked``."""
    if actor != OWNER:
        raise GoalLocked(f"the brief of {project_id} is locked; {actor!r} is not the owner")
    if store.get_project(project_id) is None:
        raise KeyError(f"no project {project_id}")
    text = new_brief or ""

    def fn(c):
        c.execute("UPDATE projects SET brief=?, goal_hash=? WHERE id=?", (text, brief_hash(text), project_id))

    return store.mutate(expected_revision, fn, OWNER, "Owner changed the project brief.",
                        project_id=project_id, kind="brief")


def accept_plan(store, project_id, actor, expected_revision=None) -> int:
    """Owner accepts a drafted plan; its tasks become runnable. Owner only (``NotAllowed``)."""
    return store.accept_project(project_id, actor, expected_revision=expected_revision)


def verify_integrity(store, project_id) -> list:
    """Every locked goal/check/brief whose text no longer matches its hash (empty when intact)."""
    return store.verify_integrity(project_id)


def verify_goals(store, project_id) -> list:
    """Ids of nodes whose stored goal text no longer matches its lock hash."""
    return [n["id"] for n in store.list_nodes(project_id) if n["goal_hash"] != goal_hash(n["goal"])]
