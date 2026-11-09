"""Versions: one product is one project family; each version is a project row (spec: docs/v5/versions_contract.md).

- ``history(store, family=|project=)`` -> versions (oldest first) and the skipped-ideas list.
- ``set_family(store, family, title, members, actor, expected_revision)`` collates existing projects.
- ``new_version(store, family=|project=, title, id, decisions, actor, expected_revision)`` rolls the
  family over: unfinished tasks are carried (default) or skipped with a reason, the old version is archived.
- ``decide(store, project, decisions, actor, expected_revision)`` settles unfinished tasks of an earlier
  version into the family's current one.

Every write is one ``store.mutate`` with the caller's expected revision, so a stale caller gets
``StaleRevision`` (HTTP 409) and nothing changes. ``ValueError`` -> 400, ``KeyError`` -> 404.
Routes live in api.py (``/api/history``, ``/api/version/*``).
"""
from __future__ import annotations

import json
import re

from . import config, node_kinds
from .store import CLOSED_STATUSES, now_iso
from .store import files_json as store_files_json

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ACTIONS = ("carry", "skip")
COPY_COLUMNS = ("title", "goal", "goal_hash", "check_json", "check_hash", "owner", "lane", "pool", "model",
                "effort", "recommended")


def _who(actor):
    return config.OWNER_NAME if actor == "owner" else "The Router"


def _unfinished(node):
    return (node.get("status") or "not_started") not in CLOSED_STATUSES


def _members(store, family):
    return [dict(r) for r in store._q("SELECT * FROM projects WHERE family=? ORDER BY version, rowid", (family,))]


def _family_row(store, family):
    r = store._q("SELECT id, title, created_at FROM families WHERE id=?", (family,))
    return dict(r[0]) if r else None


def _current(members):
    live = [m for m in members if not int(m.get("archived") or 0)]
    return (live or members)[-1] if members else None


def family_info(store, project) -> dict:
    """``{family, family_title, version, versions}`` for a project row (nulls / 0 without a family)."""
    fam = project.get("family")
    if not fam:
        return {"family": None, "family_title": None, "version": None, "versions": 0}
    row = _family_row(store, fam)
    n = store._q("SELECT COUNT(*) FROM projects WHERE family=?", (fam,))[0][0]
    return {"family": fam, "family_title": row["title"] if row else fam,
            "version": project.get("version"), "versions": int(n)}


def family_infos(store, projects) -> list:
    """``family_info`` for many project rows with one grouped query (titles and member counts)."""
    groups = {r["family"]: (r["title"], int(r["n"])) for r in store._q(
        "SELECT p.family AS family, f.title AS title, COUNT(*) AS n FROM projects p"
        " LEFT JOIN families f ON f.id=p.family WHERE p.family IS NOT NULL AND p.family<>''"
        " GROUP BY p.family")}
    out = []
    for p in projects:
        fam = p.get("family")
        if not fam:
            out.append({"family": None, "family_title": None, "version": None, "versions": 0})
            continue
        title, n = groups.get(fam, (None, 0))
        out.append({"family": fam, "family_title": title if title is not None else fam,
                    "version": p.get("version"), "versions": n})
    return out


def counts(nodes) -> dict:
    out = {"done": 0, "open": 0, "skipped": 0, "carried": 0}
    for n in nodes:
        s = n.get("status") or "not_started"
        out[s if s in out and s != "open" else "open"] += 1
    return out


def _skip_reason(evidence):
    text = str(evidence or "")
    return text[len("skipped:"):].strip() if text.startswith("skipped:") else text


# ---------------------------------------------------------------- history
def history(store, family=None, project=None) -> dict:
    if family:
        row = _family_row(store, family)
        members = _members(store, family)
        if row is None and not members:
            raise KeyError(f"no family {family}")
        fam = {"id": family, "title": row["title"] if row else family}
    elif project:
        p = store.get_project(project)
        if p is None:
            raise KeyError(f"no project {project}")
        if p.get("family"):
            return history(store, family=p["family"])
        members = [p]
        fam = {"id": None, "title": p.get("title") or p["id"]}
    else:
        raise ValueError("give a family or a project")
    cur = _current(members)
    vers, skipped = [], []
    for i, m in enumerate(members, 1):
        nodes = store.list_nodes(m["id"])
        num = m.get("version") or i
        vers.append({"project": m["id"], "title": m.get("title"), "version": num,
                     "archived": bool(int(m.get("archived") or 0)), "status": m.get("status") or "active",
                     "created_at": m.get("created_at"), "counts": counts(nodes)})
        for n in nodes:
            if n["status"] == "skipped":
                skipped.append({"project": m["id"], "version": num, "id": n["id"], "title": n.get("title"),
                                "goal": n.get("goal"), "reason": _skip_reason(n.get("evidence")),
                                "at": n.get("updated_at")})
    skipped.sort(key=lambda s: (-(s["version"] or 0), s["at"] or "", s["id"]))
    return {"family": fam, "current": cur["id"] if cur else None, "versions": vers, "skipped": skipped}


# ---------------------------------------------------------------- set_family
def set_family(store, family, title, members, actor, expected_revision=None) -> dict:
    if not isinstance(family, str) or not ID_RE.match(family):
        raise ValueError("family needs a short id (letters, digits, . _ -)")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("family needs a title")
    if not isinstance(members, list) or not members or not all(isinstance(m, str) for m in members):
        raise ValueError("members must be a non-empty list of project ids")
    if len(set(members)) != len(members):
        raise ValueError("members must not repeat")
    for m in members:
        if store.get_project(m) is None:
            raise KeyError(f"no project {m}")
    stamp = now_iso()

    def fn(c):
        # A member that already belongs to another family may only move if its whole family moves with it:
        # otherwise that family would be left with a hole (or none of its versions).
        others = {}
        for m in members:
            row = c.execute("SELECT family FROM projects WHERE id=?", (m,)).fetchone()
            if row and row[0] and row[0] != family:
                others.setdefault(row[0], []).append(m)
        for other, moving in others.items():
            left = [r[0] for r in c.execute("SELECT id FROM projects WHERE family=? ORDER BY version, rowid",
                                            (other,)) if r[0] not in members]
            if left:
                raise ValueError(f"{', '.join(moving)} already belong{'s' if len(moving) == 1 else ''} to family "
                                 f"'{other}'; include all of its versions ({', '.join(left)} missing) or leave "
                                 f"{'it' if len(moving) == 1 else 'them'} out")
        c.execute("INSERT INTO families(id, title, created_at) VALUES(?,?,?)"
                  " ON CONFLICT(id) DO UPDATE SET title=excluded.title", (family, title.strip(), stamp))
        last = len(members)
        for i, m in enumerate(members, 1):
            c.execute("UPDATE projects SET family=?, version=?, archived=? WHERE id=?",
                      (family, i, 0 if i == last else 1, m))
        for other in others:  # a family that lost all its members goes
            c.execute("DELETE FROM families WHERE id=? AND NOT EXISTS"
                      " (SELECT 1 FROM projects WHERE family=families.id)", (other,))

    names = ", ".join(members)
    rev = store.mutate(expected_revision, fn, actor,
                       f"{_who(actor)} grouped {names} as versions 1 to {len(members)} of '{title.strip()}'; "
                       f"{members[-1]} is the current version.",
                       project_id=members[-1], kind="version",
                       data={"family": family, "members": list(members)})
    return {"ok": True, "family": family, "members": list(members), "revision": rev}


# ---------------------------------------------------------------- shared copy helpers
def _parse_decisions(decisions, allowed_ids):
    if decisions is None:
        decisions = {}
    if not isinstance(decisions, dict):
        raise ValueError("decisions must be an object")
    out = {}
    for nid, d in decisions.items():
        if nid not in allowed_ids:
            raise ValueError(f"task {nid} is not an unfinished task of that version")
        if not isinstance(d, dict) or d.get("action") not in ACTIONS:
            raise ValueError(f"decision for {nid} needs an action of carry or skip")
        if d["action"] == "skip":
            reason = d.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError(f"skipping {nid} needs a reason")
            out[nid] = {"action": "skip", "reason": " ".join(reason.split())}
        else:
            out[nid] = {"action": "carry"}
    return out


def _loops(store, pid):
    return node_kinds.rows(store, pid)


def _copy_node(c, src, dest_project, new_id, deps, loop, stamp):
    """Insert a not_started copy of ``src`` (a store node dict) into ``dest_project``."""
    c.execute("INSERT INTO nodes(project_id, id, title, goal, goal_hash, check_json, check_hash, deps_json,"
              " owner, lane, pool, model, effort, status, attempts, updated_at, recommended, files_json)"
              " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'not_started',0,?,?,?)",
              (dest_project, new_id, src.get("title"), src.get("goal"), src.get("goal_hash"),
               json.dumps(src.get("check") or {}), src.get("check_hash"), json.dumps(list(deps)),
               src.get("owner"), src.get("lane"), src.get("pool"), src.get("model"), src.get("effort"), stamp,
               src.get("recommended"), store_files_json(src.get("files"))))
    loop = loop or {}
    crosses = {d: t for d, t in (loop.get("crosses") or {}).items() if d in deps}
    item = {"id": new_id, "kind": loop.get("kind"), "script": loop.get("script"), "risk": loop.get("risk"),
            "scope": loop.get("scope") or [],
            "deps": [{"id": d, "crosses": crosses[d]} if crosses.get(d) else d for d in deps]}
    node_kinds.write_rows(c, dest_project, [item])


def _mark(c, project, node_id, status, evidence, actor, stamp):
    c.execute("UPDATE nodes SET status=?, evidence=?, actor=?, updated_at=? WHERE project_id=? AND id=?",
              (status, evidence, actor, stamp, project, node_id))


def _unique(store, wanted, taken=()):
    cand, n = wanted, 1
    while cand in taken or store.get_project(cand) is not None:
        n += 1
        cand = f"{wanted}-{n}"
    return cand


# ---------------------------------------------------------------- new_version
def new_version(store, family=None, project=None, title=None, id=None, decisions=None,
                actor="owner", expected_revision=None) -> dict:
    if project:
        src = store.get_project(project)
        if src is None:
            raise KeyError(f"no project {project}")
        if src.get("family"):  # the source is always the family's current version
            cur = _current(_members(store, src["family"]))
            if cur is not None and cur["id"] != src["id"]:
                raise ValueError(f"only the current version ({cur['id']}) can roll over; {src['id']} is an "
                                 f"earlier version of '{src['family']}'")
    elif family:
        members = _members(store, family)
        if not members:
            raise KeyError(f"no family {family}")
        src = _current(members)
    else:
        raise ValueError("give a family or a project")
    create_family = None
    if src.get("family"):
        fam = src["family"]
        row = _family_row(store, fam)
        fam_title = row["title"] if row else fam
        n = max([m.get("version") or 0 for m in _members(store, fam)] + [0]) + 1
    else:
        fam = re.sub(r"-v\d+$", "", src["id"]) or src["id"]
        fam_title = src.get("title") or src["id"]
        if _members(store, fam) or _family_row(store, fam):
            raise ValueError(f"a family '{fam}' already exists; add this project with the family call first")
        create_family = (fam, fam_title)
        n = 2
        if (src.get("version") or 1) != 1:
            n = int(src["version"]) + 1
    nodes = store.list_nodes(src["id"])
    open_ids = [x["id"] for x in nodes if _unfinished(x)]
    decided = _parse_decisions(decisions, set(open_ids))
    plan_for = {nid: decided.get(nid, {"action": "carry"}) for nid in open_ids}
    carried = [nid for nid in open_ids if plan_for[nid]["action"] == "carry"]
    skipped = [{"id": nid, "reason": plan_for[nid]["reason"]} for nid in open_ids
               if plan_for[nid]["action"] == "skip"]
    new_id = id if id else f"{fam}-v{n}"
    if id is not None and not ID_RE.match(str(id)):
        raise ValueError("id must be a short project id (letters, digits, . _ -)")
    new_id = _unique(store, new_id)
    new_title = (title or "").strip() or f"{fam_title} v{n}"
    q1 = "Q1"
    k = 1
    while q1 in carried:
        k += 1
        q1 = f"Q1-{k}"
    loops = _loops(store, src["id"])
    by_id = {x["id"]: x for x in nodes}
    stamp = now_iso()
    q1_check = {"type": "human", "prompt": f"Discovery questions for {new_title} answered (waypoint-planning)"}
    q1_goal = (f"Ask Owner the discovery questions for {new_title} and record his answers before any "
               f"planning work starts (waypoint-planning).")
    from .store import check_hash, goal_hash

    def fn(c):
        if create_family:
            c.execute("INSERT INTO families(id, title, created_at) VALUES(?,?,?)", (fam, fam_title, stamp))
            c.execute("UPDATE projects SET family=?, version=1 WHERE id=?", (fam, src["id"]))
        c.execute("UPDATE projects SET archived=1 WHERE id=?", (src["id"],))
        c.execute("INSERT INTO projects(id, title, brief, goal_hash, cwd, created_at, archived, status, family,"
                  " version) VALUES(?,?,?,?,?,?,0,?,?,?)",
                  (new_id, new_title, src.get("brief"), src.get("goal_hash"), src.get("cwd"), stamp,
                   src.get("status") or "active", fam, n))
        for nid in carried:
            node = by_id[nid]
            deps = [d for d in node["deps"] if d in carried]
            _copy_node(c, node, new_id, nid, deps, loops.get(nid), stamp)
            _mark(c, src["id"], nid, "carried", f"carried to {new_id}/{nid}", actor, stamp)
        for s in skipped:
            _mark(c, src["id"], s["id"], "skipped", f"skipped: {s['reason']}", actor, stamp)
        c.execute("INSERT INTO nodes(project_id, id, title, goal, goal_hash, check_json, check_hash, deps_json,"
                  " lane, status, attempts, updated_at) VALUES(?,?,?,?,?,?,?,'[]','discovery','not_started',0,?)",
                  (new_id, q1, f"Discovery questions for {new_title}", q1_goal, goal_hash(q1_goal),
                   json.dumps(q1_check), check_hash(q1_check), stamp))

    who = _who(actor)
    rev = store.mutate(expected_revision, fn, actor,
                       f"{who} started version {n} of '{fam_title}' as {new_id}: {len(carried)} unfinished "
                       f"task{'s' if len(carried) != 1 else ''} carried, {len(skipped)} skipped; "
                       f"{src['id']} is now archived.",
                       project_id=new_id, kind="version",
                       data={"family": fam, "from": src["id"], "version": n, "carried": carried,
                             "skipped": [s["id"] for s in skipped]})
    for nid in carried:
        store.event(src["id"], nid, "version", actor, f"{who} carried task {nid} to {new_id}.",
                    {"to": new_id, "action": "carry", "revision": rev})
    for s in skipped:
        store.event(src["id"], s["id"], "version", actor,
                    f"{who} skipped task {s['id']} because {s['reason']}.",
                    {"action": "skip", "reason": s["reason"], "revision": rev})
    return {"ok": True, "project": new_id, "version": n, "carried": carried, "skipped": skipped, "revision": rev}


# ---------------------------------------------------------------- decide
def decide(store, project, decisions, actor="owner", expected_revision=None) -> dict:
    src = store.get_project(project)
    if src is None:
        raise KeyError(f"no project {project}")
    if not src.get("family"):
        raise ValueError(f"{project} is not part of a family")
    members = _members(store, src["family"])
    cur = _current(members)
    if cur["id"] == project:
        raise ValueError(f"{project} is the current version; decide only applies to earlier versions")
    nodes = store.list_nodes(project)
    by_id = {x["id"]: x for x in nodes}
    open_ids = [x["id"] for x in nodes if _unfinished(x)]
    decided = _parse_decisions(decisions, set(open_ids))
    if not decided:
        raise ValueError("no decisions given")
    carry = [nid for nid in decided if decided[nid]["action"] == "carry"]
    skip = [{"id": nid, "reason": decided[nid]["reason"]} for nid in decided if decided[nid]["action"] == "skip"]
    existing = {x["id"] for x in store.list_nodes(cur["id"])}
    vnum = src.get("version") or 1
    mapping = {}
    for nid in carry:
        cand, k = nid, 1
        if cand in existing or cand in mapping.values():
            cand = f"v{vnum}-{nid}"
            while cand in existing or cand in mapping.values():
                k += 1
                cand = f"v{vnum}-{nid}-{k}"
        mapping[nid] = cand
    loops = _loops(store, project)
    stamp = now_iso()

    def fn(c):
        for nid in carry:
            node = by_id[nid]
            deps = [mapping[d] for d in node["deps"] if d in mapping]
            loop = dict(loops.get(nid) or {})
            loop["crosses"] = {mapping[d]: t for d, t in (loop.get("crosses") or {}).items() if d in mapping}
            _copy_node(c, node, cur["id"], mapping[nid], deps, loop, stamp)
            _mark(c, project, nid, "carried", f"carried to {cur['id']}/{mapping[nid]}", actor, stamp)
        for s in skip:
            _mark(c, project, s["id"], "skipped", f"skipped: {s['reason']}", actor, stamp)

    who = _who(actor)
    rev = store.mutate(expected_revision, fn, actor,
                       f"{who} settled {len(decided)} unfinished task{'s' if len(decided) != 1 else ''} of "
                       f"{project}: {len(carry)} carried to {cur['id']}, {len(skip)} skipped.",
                       project_id=project, kind="version",
                       data={"current": cur["id"], "carried": mapping, "skipped": [s["id"] for s in skip]})
    for nid in carry:
        store.event(project, nid, "version", actor, f"{who} carried task {nid} to {cur['id']} as {mapping[nid]}.",
                    {"to": cur["id"], "action": "carry", "revision": rev})
    for s in skip:
        store.event(project, s["id"], "version", actor, f"{who} skipped task {s['id']} because {s['reason']}.",
                    {"action": "skip", "reason": s["reason"], "revision": rev})
    return {"ok": True, "carried": [{"from": k, "to": v} for k, v in mapping.items()], "skipped": skip,
            "revision": rev}
