"""Action handlers for projects: archive, versions, families and board snapshots.

Exposes ``register(handlers)``; run-actions discovers this module by its ``actions_`` name. Each handler is
``handler(action, ctx) -> dict`` and returns ``{"status": "done"|"failed"|"refused", "result": {...}}`` or, for
failed and refused, ``{"status": ..., "error": "<reason>"}``. ``ctx.store`` is a ``waypoint_data.Store``. The caller
(run-actions) owns the action file's own status and processes each action once.

Every write goes through the data client: creates use ``expected_rev=0`` (exclusive), changes use the rev just read
(compare-and-swap). Rules are ported from engine/cc_engine/versions.py and state_snapshot.py (folder layout instead
of SQLite). Version records are append-only: ``version_new`` only creates files, and ``version_decide`` appends
decision entries; an existing entry is never rewritten.

Folder layout used (see core/waypoint-data/schema): projects/<p>/project.json, projects/<p>/nodes/<n>.json,
versions/<p>/<n>.json, families/<f>.json, snapshots/<p>/<ts>.json.
"""
import datetime as _dt
import json
import os
import re
import sys
from pathlib import Path

_DATA_PY = Path(__file__).resolve().parents[2] / "core" / "waypoint-data" / "py"
if str(_DATA_PY) not in sys.path:  # the data client lives in the repo, not in site-packages
    sys.path.insert(0, str(_DATA_PY))

from waypoint_data import io as wio  # noqa: E402
from waypoint_data import store as wstore  # noqa: E402
from waypoint_data.store import RevConflict, serialize  # noqa: E402

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
CLOSED = ("done", "skipped", "carried")
IN_PROGRESS = ("running", "submitted")
STARTABLE = ("not_started", "ready")
SNAPSHOT_SCHEMA = "waypoint.state.v1"
EVIDENCE_MAX = 400
_VOLATILE = ("rev", "updated_at", "updated_by")
_CARRIED_RE = re.compile(r"^carried to ([^/\s]+)/(\S+)")
# A product's own version name (V10, 1.2, Core 1.2). ``version`` stays the family ordinal (order only).
LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9 ._-]{0,30}[A-Za-z0-9])?$")


def version_label(value):
    """The cleaned label, or None for no label. Raises Fail for a label that is not a short version name."""
    if value is None:
        return None
    text = " ".join(str(value).split()) if isinstance(value, str) else None
    if not text or not LABEL_RE.match(text):
        raise Fail("label must be a short version name such as V10 or 1.2 (letters, digits, space . _ -; "
                   "at most 32 characters)")
    return text


def label_slug(label):
    """The id part for a label: ``V10`` -> ``v10``, ``Core 1.2`` -> ``core-1.2``."""
    return re.sub(r"[^a-z0-9._-]+", "-", label.lower()).strip("-.")


class Refuse(Exception):
    """The action may not run (missing owner marker, work already started, stale revision)."""


class Fail(Exception):
    """The action cannot run (bad input, unknown project)."""


# ------------------------------------------------------------------ helpers
def _store(ctx):
    s = getattr(ctx, "store", None)
    if s is None and isinstance(ctx, dict):
        s = ctx.get("store")
    if s is None:
        raise Fail("no data store in ctx")
    return s


def _stamp(store):
    return wstore.iso(store.now())


def _need_owner(action):
    if action.get("by") != "owner":
        raise Refuse(f"{action.get('type')} is an owner action; it needs the owner marker (by=owner), "
                     f"not by={action.get('by')!r}")


def _pid(payload, key="project"):
    v = payload.get(key)
    if not isinstance(v, str) or not ID_RE.match(v):
        raise Fail(f"{key} must be a short id (letters, digits, . _ -)")
    return v


def _project(store, pid):
    p = store.read(f"projects/{pid}/project.json")
    if p is None:
        raise Fail(f"no project {pid}")
    return p


def _nodes(store, pid):
    out = {}
    for n in store.list_names(f"projects/{pid}/nodes"):
        if n.endswith(".json"):
            e = store.read(f"projects/{pid}/nodes/{n}")
            if isinstance(e, dict):
                out[e["id"]] = e
    return out


def _subdirs(store, rel):
    """Sorted sub-folder names (one per project); the data client lists files only."""
    d = store.path(rel)
    try:
        names = os.listdir(d)
    except (FileNotFoundError, NotADirectoryError):
        return []
    return sorted(n for n in names if not wio.is_ignored_name(n) and os.path.isdir(os.path.join(d, n)))


def _body(entity):
    return {k: v for k, v in entity.items() if k not in _VOLATILE}


def _update(store, rel, entity, **changes):
    """Compare-and-swap an existing entity with the rev it was read at."""
    new = _body(entity)
    new.update(changes)
    return store.write(rel, new, expected_rev=entity["rev"])


def _create(store, rel, entity):
    return store.write(rel, entity, expected_rev=0)


def _event(store, kind, summary, action, project=None, data=None):
    ev = {"kind": kind, "summary": summary, "data": dict(data or {}, action=action.get("id"))}
    if project:
        ev["project"] = project
    store.append_event(store.writer or "runner", ev)


def _dep_id(d):
    return d.get("id") if isinstance(d, dict) else d


def board_revision(store, pid):
    """One number for a project's files: project rev plus every node rev. Any write raises it."""
    total = (store.read(f"projects/{pid}/project.json") or {}).get("rev", 0)
    for n in _nodes(store, pid).values():
        total += n.get("rev", 0)
    return total


def _family_members(store, family):
    fam = store.read(f"families/{family}.json")
    return fam, list(fam["projects"]) if fam else []


def _current_member(store, members):
    live = [m for m in members if not (store.read(f"projects/{m}/project.json") or {}).get("archived")]
    return (live or members)[-1] if members else None


def _family_claimants(store, family):
    """Every project that names ``family`` in its own file (merged-away projects excluded)."""
    out = []
    for pid in _subdirs(store, "projects"):
        p = store.read(f"projects/{pid}/project.json")
        if isinstance(p, dict) and p.get("family") == family and not p.get("merged_into"):
            out.append(pid)
    return out


def _check_family_unique(store, family, members):
    """Upkeep: one version number per project in a family, and no project claiming a family it is not listed in.
    Raises Fail naming the projects and the fix (version_merge or family_group)."""
    strays = [c for c in _family_claimants(store, family) if c not in members]
    if strays:
        raise Fail(f"{', '.join(strays)} claim family '{family}' but are not listed in it; fold each into its twin "
                   f"with version_merge or list it with family_group first")
    seen = {}
    for m in members:
        v = (store.read(f"projects/{m}/project.json") or {}).get("version")
        if v is not None:
            seen.setdefault(v, []).append(m)
    dup = {v: ms for v, ms in seen.items() if len(ms) > 1}
    if dup:
        v, ms = sorted(dup.items())[0]
        raise Fail(f"family '{family}' has two projects at version {v} ({', '.join(ms)}); merge them with "
                   f"version_merge first")


def _unique_project(store, wanted):
    cand, n = wanted, 1
    while store.exists(f"projects/{cand}/project.json"):
        n += 1
        suffix = f"-{n}"
        cand = wanted[:64 - len(suffix)] + suffix
    return cand


def _unique_node(existing, wanted):
    cand, n = wanted, 1
    while cand in existing:
        n += 1
        suffix = f"-{n}"
        cand = wanted[:64 - len(suffix)] + suffix
    return cand


def _copy_node(store, src, dest_project, new_id, keep_deps):
    node = _body(src)
    node.update({"project": dest_project, "id": new_id, "status": "not_started", "attempts": 0, "actor": None,
                 "evidence": None, "location": {"where": "unset", "changed_at": None},
                 "deps": [d for d in src.get("deps", []) if _dep_id(d) in keep_deps]})
    _create(store, f"projects/{dest_project}/nodes/{new_id}.json", node)


def _mark(store, pid, node, status, evidence, actor):
    rel = f"projects/{pid}/nodes/{node['id']}.json"
    _update(store, rel, node, status=status, evidence=evidence, actor=actor)


def _parse_decisions(decisions, allowed):
    if decisions is None:
        return {}
    if not isinstance(decisions, dict):
        raise Fail("decisions must be an object")
    out = {}
    for nid, d in decisions.items():
        if nid not in allowed:
            raise Fail(f"task {nid} is not an unfinished task of that version")
        if not isinstance(d, dict) or d.get("action") not in ("carry", "skip"):
            raise Fail(f"decision for {nid} needs an action of carry or skip")
        if d["action"] == "skip":
            reason = d.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise Fail(f"skipping {nid} needs a reason")
            out[nid] = {"action": "skip", "reason": " ".join(reason.split())}
        else:
            out[nid] = {"action": "carry", "reason": " ".join(str(d.get("reason") or "").split())}
    return out


# ------------------------------------------------------------------ archive
def _set_archived(action, ctx, value):
    _need_owner(action)
    store = _store(ctx)
    pid = _pid(action.get("payload") or {})
    p = _project(store, pid)
    if bool(p.get("archived")) == value:
        return {"status": "done", "result": {"project": pid, "archived": value, "changed": False}}
    _update(store, f"projects/{pid}/project.json", p, archived=value)
    _event(store, "project.archive" if value else "project.unarchive",
           f"Owner {'archived' if value else 'unarchived'} {pid}.", action, project=pid)
    return {"status": "done", "result": {"project": pid, "archived": value, "changed": True}}


def project_archive(action, ctx):
    return _set_archived(action, ctx, True)


def project_unarchive(action, ctx):
    return _set_archived(action, ctx, False)


# ------------------------------------------------------------------ versions and families
def _ensure_version_file(store, pid, n, family, from_project, actor_stamp, by):
    """Create versions/<pid>/<n>.json when missing. Never touches an existing file."""
    rel = f"versions/{pid}/{n}.json"
    if store.exists(rel):
        return False
    try:
        _create(store, rel, {"schema": "waypoint.version.v1", "project": pid, "n": n, "family": family,
                             "created_at": actor_stamp, "from_project": from_project, "decisions": []})
        return True
    except RevConflict:
        return False


def family_group(action, ctx):
    _need_owner(action)
    store = _store(ctx)
    payload = action.get("payload") or {}
    family = payload.get("family")
    members = payload.get("projects")
    if not isinstance(family, str) or not ID_RE.match(family):
        raise Fail("family needs a short id (letters, digits, . _ -)")
    if not isinstance(members, list) or not members or not all(isinstance(m, str) for m in members):
        raise Fail("projects must be a non-empty list of project ids")
    if len(set(members)) != len(members):
        raise Fail("projects must not repeat")
    projects = {}
    for m in members:
        if not ID_RE.match(m):
            raise Fail(f"bad project id {m}")
        projects[m] = _project(store, m)
    # A member of another family may only move if its whole family moves with it.
    others = {}
    for m, p in projects.items():
        if p.get("family") and p["family"] != family:
            others.setdefault(p["family"], []).append(m)
    for other, moving in others.items():
        _, rest = _family_members(store, other)
        left = [x for x in rest if x not in members]
        if left:
            raise Fail(f"{', '.join(moving)} already belong to family '{other}'; include all of its versions "
                       f"({', '.join(left)} missing) or leave them out")
    strays = [c for c in _family_claimants(store, family) if c not in members]
    if strays:
        raise Fail(f"{', '.join(strays)} already claim family '{family}'; include them or fold them into their twin "
                   f"with version_merge first")
    stamp = _stamp(store)
    existing = store.read(f"families/{family}.json")
    title = (existing or {}).get("title") or family
    if existing:
        _update(store, f"families/{family}.json", existing, projects=list(members))
    else:
        _create(store, f"families/{family}.json", {"schema": "waypoint.family.v1", "id": family, "title": title,
                                                   "created_at": stamp, "projects": list(members)})
    last = len(members)
    for i, m in enumerate(members, 1):
        _update(store, f"projects/{m}/project.json", store.read(f"projects/{m}/project.json"),
                family=family, version=i, archived=(i != last))
        _ensure_version_file(store, m, i, family, members[i - 2] if i > 1 else None, stamp, action["by"])
    for other in others:  # files are never deleted: a family that lost every member is left empty
        cur = store.read(f"families/{other}.json")
        if cur:
            _update(store, f"families/{other}.json", cur, projects=[])
    _event(store, "version", f"Owner grouped {', '.join(members)} as versions of '{family}'.", action,
           project=members[-1], data={"family": family, "members": list(members)})
    return {"status": "done", "result": {"family": family, "members": list(members)}}


def version_new(action, ctx):
    _need_owner(action)
    store = _store(ctx)
    payload = action.get("payload") or {}
    pid = _pid(payload)
    src = _project(store, pid)
    fam_id = src.get("family")
    fam_row = None
    if fam_id:
        fam_row, members = _family_members(store, fam_id)
        cur = _current_member(store, members) if members else pid
        if cur != pid:
            raise Fail(f"only the current version ({cur}) can roll over; {pid} is an earlier version of '{fam_id}'")
        fam_title = (fam_row or {}).get("title") or fam_id
        _check_family_unique(store, fam_id, members)
        n = max([(store.read(f"projects/{m}/project.json") or {}).get("version") or 0 for m in members] + [0]) + 1
    else:
        fam_id = re.sub(r"-v\d+$", "", pid) or pid
        if store.exists(f"families/{fam_id}.json"):
            raise Fail(f"a family '{fam_id}' already exists; add this project with family_group first")
        fam_title = src.get("title") or pid
        n = int(src["version"]) + 1 if src.get("version") else 2
        members = [pid]
        _check_family_unique(store, fam_id, members)
    nodes = _nodes(store, pid)
    open_ids = [i for i, x in nodes.items() if (x.get("status") or "not_started") not in CLOSED]
    decided = _parse_decisions(payload.get("decisions"), set(open_ids))
    plan = {i: decided.get(i, {"action": "carry", "reason": ""}) for i in open_ids}
    carried = [i for i in open_ids if plan[i]["action"] == "carry"]
    skipped = [{"id": i, "reason": plan[i]["reason"]} for i in open_ids if plan[i]["action"] == "skip"]
    label = version_label(payload.get("label"))
    title = payload.get("title")
    if title is not None and (not isinstance(title, str) or not title.strip()):
        raise Fail("title must be a non-empty string")
    name = label or f"v{n}"
    new_id = _unique_project(store, f"{fam_id}-{label_slug(label) if label else f'v{n}'}")
    new_title = " ".join(title.split()) if title else f"{fam_title} {name}"
    by = action["by"]
    stamp = _stamp(store)

    # 1. the new project and its nodes (all creates, so nothing existing changes)
    _create(store, f"projects/{new_id}/project.json", {
        "schema": "waypoint.project.v1", "id": new_id, "title": new_title, "brief": src.get("brief", ""),
        "goal_hash": src.get("goal_hash"), "cwd": src["cwd"], "created_at": stamp, "archived": False,
        "status": src.get("status") or "active", "family": fam_id, "version": n, "version_label": label})
    for nid in carried:
        _copy_node(store, nodes[nid], new_id, nid, set(carried))
    q1 = _unique_node(set(carried), "Q1")
    goal = (f"Ask Owner the discovery questions for {new_title} and record his answers before any planning "
            f"work starts (waypoint-planning).")
    _create(store, f"projects/{new_id}/nodes/{q1}.json", {
        "schema": "waypoint.node.v1", "project": new_id, "id": q1, "title": f"Discovery questions for {new_title}",
        "goal": goal, "goal_hash": None, "check_hash": None,
        "check": {"type": "human", "prompt": f"Discovery questions for {new_title} answered (waypoint-planning)"},
        "deps": [], "parent": None, "estimate": 1, "slot": None,
        "location": {"where": "unset", "changed_at": None}, "files": [], "kind": "discovery",
        "owner": "human", "lane": "discovery", "status": "not_started", "attempts": 0, "actor": None,
        "evidence": None})
    # 2. version records: create only. The earlier version gets a baseline record if it never had one.
    _ensure_version_file(store, pid, src.get("version") or 1, fam_id, None, stamp, by)
    decisions = []
    for nid in open_ids:
        d = plan[nid]
        if d["action"] == "carry":
            reason = d["reason"] or f"Carried to {new_id}/{nid}."
        else:
            reason = d["reason"]
        decisions.append({"node": nid, "decision": d["action"], "reason": reason, "decided_by": by,
                          "decided_at": stamp})
    _create(store, f"versions/{new_id}/{n}.json", {
        "schema": "waypoint.version.v1", "project": new_id, "n": n, "family": fam_id, "created_at": stamp,
        "from_project": pid, "decisions": decisions})
    # 3. the family and the earlier project
    if fam_row:
        _update(store, f"families/{fam_id}.json", fam_row, projects=list(members) + [new_id])
    else:
        _create(store, f"families/{fam_id}.json", {"schema": "waypoint.family.v1", "id": fam_id, "title": fam_title,
                                                   "created_at": stamp, "projects": [pid, new_id]})
    src_cur = store.read(f"projects/{pid}/project.json")
    _update(store, f"projects/{pid}/project.json", src_cur, archived=True, family=fam_id,
            version=src_cur.get("version") or 1)
    # 4. settle the unfinished nodes of the earlier version
    for nid in carried:
        _mark(store, pid, store.read(f"projects/{pid}/nodes/{nid}.json"), "carried",
              f"carried to {new_id}/{nid}", by)
    for s in skipped:
        _mark(store, pid, store.read(f"projects/{pid}/nodes/{s['id']}.json"), "skipped",
              f"skipped: {s['reason']}", by)
    for q in _list(store, "questions"):  # an open question follows its node, so the answer reaches the live task
        if q.get("project") == pid and q.get("node") in carried and not q.get("answered_at") and q.get("answer") is None:
            _update(store, f"questions/{q['id']}.json", q, project=new_id)
    _event(store, "version", f"Owner started version {name} of '{fam_title}' as {new_id}: {len(carried)} carried, "
           f"{len(skipped)} skipped; {pid} is archived.", action, project=new_id,
           data={"family": fam_id, "from": pid, "version": n, "version_label": label, "carried": carried,
                 "skipped": [s["id"] for s in skipped]})
    return {"status": "done", "result": {"project": new_id, "version": n, "version_label": label, "family": fam_id,
                                          "carried": carried, "skipped": [s["id"] for s in skipped]}}


def _list(store, rel):
    try:
        return [e for e in store.list_entities(rel) if isinstance(e, dict) and e.get("id")]
    except Exception:  # noqa: BLE001 - a missing folder is an empty list
        return []


def _remap_dep(d, rename):
    if isinstance(d, str):
        return rename.get(d, d)
    if isinstance(d, dict) and d.get("id") in rename:
        return dict(d, id=rename[d["id"]])
    return d


def version_merge(action, ctx):
    """Fold the tasks of ``drop`` into ``keep`` (two projects that hold one version number). Nodes keep their status
    and evidence; a clashing id gets a -2 suffix and deps follow it. To-dos and questions move with their node. The
    dropped project is kept on disk as an archived record with ``merged_into`` and no family or version."""
    _need_owner(action)
    store = _store(ctx)
    payload = action.get("payload") or {}
    keep, drop = _pid(payload, "keep"), _pid(payload, "drop")
    if keep == drop:
        raise Fail("keep and drop must be two different projects")
    _project(store, keep)
    dp = _project(store, drop)
    if dp.get("merged_into"):
        raise Fail(f"{drop} was already merged into {dp['merged_into']}")
    kept, dropped = _nodes(store, keep), _nodes(store, drop)
    taken, rename = set(kept), {}
    for nid in sorted(dropped):
        rename[nid] = _unique_node(taken, nid)
        taken.add(rename[nid])
    for nid, node in sorted(dropped.items()):
        body = _body(node)
        body.update(project=keep, id=rename[nid], merged_from=drop,
                    deps=[_remap_dep(d, rename) for d in node.get("deps") or []])
        if body.get("parent") in rename:
            body["parent"] = rename[body["parent"]]
        _create(store, f"projects/{keep}/nodes/{rename[nid]}.json", body)
    for t in _list(store, "todos"):
        if t.get("project") == drop or (isinstance(t.get("node"), dict) and t["node"].get("project") == drop):
            node = t.get("node")
            if isinstance(node, dict) and node.get("project") == drop:
                node = {"project": keep, "id": rename.get(node.get("id"), node.get("id"))}
            _update(store, f"todos/{t['id']}.json", t, project=keep if t.get("project") == drop else t.get("project"),
                    node=node)
    for q in _list(store, "questions"):
        if q.get("project") == drop:
            _update(store, f"questions/{q['id']}.json", q, project=keep, node=rename.get(q.get("node"), q.get("node")))
    fam = dp.get("family")
    if fam:
        row = store.read(f"families/{fam}.json")
        if row and drop in (row.get("projects") or []):
            _update(store, f"families/{fam}.json", row, projects=[m for m in row["projects"] if m != drop])
    _update(store, f"projects/{drop}/project.json", store.read(f"projects/{drop}/project.json"), archived=True,
            merged_into=keep, family=None, version=None)
    _event(store, "version", f"Owner merged {drop} into {keep}: {len(dropped)} task(s) moved.", action, project=keep,
           data={"keep": keep, "drop": drop, "renamed": rename})
    return {"status": "done", "result": {"keep": keep, "drop": drop, "renamed": rename}}


def version_label_set(action, ctx):
    """Set or clear a project's product version label (``payload.label``; null or "" clears it)."""
    _need_owner(action)
    store = _store(ctx)
    payload = action.get("payload") or {}
    pid = _pid(payload)
    cur = _project(store, pid)
    raw = payload.get("label")
    label = version_label(raw) if raw not in (None, "") else None
    if cur.get("version_label") != label:
        _update(store, f"projects/{pid}/project.json", cur, version_label=label)
    _event(store, "version", f"Owner set the version label of {pid} to {label or '(none)'}.", action, project=pid,
           data={"version_label": label})
    return {"status": "done", "result": {"project": pid, "version_label": label}}


def version_decide(action, ctx):
    """Change a carry or skip decision of an earlier version: keep -> carry, discard -> skip.

    ``payload.project`` and ``payload.version`` name versions/<project>/<version>.json; its decisions concern the
    unfinished nodes of ``from_project``. Optional ``node`` narrows the change to one node, optional ``reason`` is
    recorded. The new decision is appended to the version file; earlier entries are never rewritten.
    """
    _need_owner(action)
    store = _store(ctx)
    payload = action.get("payload") or {}
    pid = _pid(payload)
    ver = str(payload.get("version") or "")
    if not ver.isdigit() or int(ver) < 1:
        raise Fail("version must be a version number")
    target = {"keep": "carry", "discard": "skip"}.get(payload.get("decision"))
    if target is None:
        raise Fail("decision must be keep or discard")
    rel = f"versions/{pid}/{int(ver)}.json"
    vf = store.read(rel)
    if vf is None:
        raise Fail(f"no version {ver} of {pid}")
    src = vf.get("from_project")
    if not src:
        raise Fail(f"version {ver} of {pid} is the first version; it has no earlier version to decide")
    effective = {}
    for d in vf["decisions"]:  # later entries win
        effective[d["node"]] = d["decision"]
    wanted = payload.get("node")
    if wanted is not None:
        if wanted not in effective:
            raise Fail(f"version {ver} of {pid} made no decision about {wanted}")
        todo = [wanted] if effective[wanted] != target else []
    else:
        todo = [n for n, d in effective.items() if d != target]
    src_nodes = _nodes(store, src)
    dest_nodes = _nodes(store, pid)
    by = action["by"]
    reason = " ".join(str(payload.get("reason") or "").split()) or (
        "Kept by owner." if target == "carry" else "Discarded by owner.")
    plan = []
    for nid in todo:
        s = src_nodes.get(nid)
        if s is None:
            raise Fail(f"{src} has no task {nid}")
        if target == "carry":
            if s.get("status") != "skipped":
                raise Refuse(f"task {nid} of {src} is {s.get('status')}, not skipped; only a skipped task can be kept")
            plan.append((nid, s, None))
        else:
            m = _CARRIED_RE.match(s.get("evidence") or "")
            copy_id = m.group(2) if m and m.group(1) == pid else None
            c = dest_nodes.get(copy_id) if copy_id else None
            if c is None:
                raise Fail(f"cannot find the copy of task {nid} in {pid}")
            if (c.get("status") or "not_started") not in STARTABLE:
                raise Refuse(f"work on {nid} has started in {pid} ({c.get('status')}); it cannot be discarded")
            plan.append((nid, s, c))
    stamp = _stamp(store)
    changed = []
    new_entries = []
    for nid, s, c in plan:
        if target == "carry":
            copy_id = _unique_node(set(dest_nodes), nid)
            _copy_node(store, s, pid, copy_id, set())
            dest_nodes[copy_id] = {"id": copy_id}
            _mark(store, src, s, "carried", f"carried to {pid}/{copy_id}", by)
            entry_reason = f"{reason} Carried as {copy_id}."
        else:
            _mark(store, pid, c, "skipped", f"skipped: {reason}", by)
            _mark(store, src, s, "skipped", f"skipped: {reason}", by)
            entry_reason = reason
        new_entries.append({"node": nid, "decision": target, "reason": entry_reason, "decided_by": by,
                            "decided_at": stamp})
        changed.append(nid)
    if new_entries:
        cur = store.read(rel)
        _update(store, rel, cur, decisions=list(cur["decisions"]) + new_entries)
        _event(store, "version", f"Owner changed {len(changed)} decision(s) of version {ver} of {pid} to {target}.",
               action, project=pid, data={"changed": changed, "decision": target})
    return {"status": "done", "result": {"project": pid, "version": int(ver), "decision": target,
                                          "changed": changed}}


# ------------------------------------------------------------------ snapshots
def _last_line(text):
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    return lines[-1][:EVIDENCE_MAX] if lines else None


def _cwd_text(cwd):
    if isinstance(cwd, dict):
        return f"{cwd.get('machine', '')}:{cwd.get('rel', '')}"
    return cwd


def _cwd_obj(text):
    if isinstance(text, dict):
        return text
    machine, sep, rel = str(text or "").partition(":")
    if not sep or len(machine) < 2:
        return {"machine": "pc", "rel": ""}
    return {"machine": machine, "rel": rel}


def _snapshot_of(store, pid):
    p = _project(store, pid)
    fam = p.get("family")
    ftitle = None
    if fam:
        ftitle = (store.read(f"families/{fam}.json") or {}).get("title")
    nodes = []
    for n in _nodes(store, pid).values():
        item = {"id": n["id"], "title": n["title"], "goal": n.get("goal"), "check": n.get("check"),
                "deps": [_dep_id(d) for d in n.get("deps", [])], "owner": n.get("owner"), "lane": n.get("lane"),
                "status": n.get("status"), "attempts": n.get("attempts", 0), "actor": n.get("actor"),
                "evidence": _last_line(n.get("evidence")), "claim": None}
        nodes.append(item)
    return {"schema": SNAPSHOT_SCHEMA,
            "project": {"id": pid, "title": p["title"], "brief": p.get("brief"), "cwd": _cwd_text(p.get("cwd")),
                        "created": p.get("created_at"), "status": p.get("status") or "active", "family": fam,
                        "family_title": ftitle, "version": None if p.get("version") is None else str(p["version"]),
                        "archived": bool(p.get("archived"))},
            "revision": board_revision(store, pid), "nodes": nodes}


def _write_snapshot(store, pid):
    snap = _snapshot_of(store, pid)
    base = _dt.datetime.fromtimestamp(store.now(), _dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    sid, i = base, 1
    while True:
        try:  # snapshots are write-once: create exclusively, never overwrite
            wio.create_exclusive(store.path(f"snapshots/{pid}/{sid}.json"), serialize(snap),
                                 writer=store.writer or "runner")
            return {"project": pid, "snapshot_id": sid, "revision": snap["revision"]}
        except FileExistsError:
            i += 1
            sid = f"{base[:-1]}-{i}Z"


def state_snapshot_write(action, ctx):
    store = _store(ctx)
    payload = action.get("payload") or {}
    if payload.get("project") is not None:
        pids = [_pid(payload)]
        _project(store, pids[0])
    else:  # every live project; archived projects never start a new snapshot
        pids = []
        for name in _subdirs(store, "projects"):
            p = store.read(f"projects/{name}/project.json")
            if isinstance(p, dict) and not p.get("archived"):
                pids.append(p["id"])
    snaps = [_write_snapshot(store, pid) for pid in sorted(pids)]
    for s in snaps:
        _event(store, "snapshot", f"Board snapshot {s['snapshot_id']} written for {s['project']}.", action,
               project=s["project"], data={"snapshot_id": s["snapshot_id"], "revision": s["revision"]})
    return {"status": "done", "result": {"snapshots": snaps, "note": payload.get("note")}}


def _find_snapshot(store, snapshot_id, project):
    if project is not None:
        pids = [project]
    else:
        pids = _subdirs(store, "snapshots")
    for pid in pids:
        rel = f"snapshots/{pid}/{snapshot_id}.json"
        if store.exists(rel):
            return pid, store.read(rel)
    raise Fail(f"no snapshot {snapshot_id}")


def _validated(snap):
    if not isinstance(snap, dict) or snap.get("schema") != SNAPSHOT_SCHEMA:
        raise Fail(f"not a {SNAPSHOT_SCHEMA} snapshot")
    p = snap.get("project")
    if not isinstance(p, dict) or not ID_RE.match(str(p.get("id") or "")):
        raise Fail("snapshot needs a safe project id")
    if not isinstance(snap.get("nodes"), list) or not isinstance(snap.get("revision"), int):
        raise Fail("snapshot needs a revision and a nodes list")
    for n in snap["nodes"]:
        if not isinstance(n, dict) or not ID_RE.match(str(n.get("id") or "")):
            raise Fail("snapshot node needs a safe id")
    return p


def state_snapshot_resume(action, ctx):
    store = _store(ctx)
    payload = action.get("payload") or {}
    sid = payload.get("snapshot_id")
    if not isinstance(sid, str) or not ID_RE.match(sid):
        raise Fail("snapshot_id must be a short id")
    project = _pid(payload) if payload.get("project") is not None else None
    folder_pid, snap = _find_snapshot(store, sid, project)
    sp = _validated(snap)
    pid = sp["id"]
    existing = store.read(f"projects/{pid}/project.json")
    if existing is not None:
        current = board_revision(store, pid)
        if snap["revision"] != current and action.get("by") != "owner":
            raise Refuse(f"stale revision: the snapshot was taken at board revision {snap['revision']} but "
                         f"{pid} is at {current}; resuming needs owner confirmation (by=owner)")
    by = action.get("by") or "runner"
    created = existing is None
    by_id = {n["id"]: n for n in snap["nodes"]}
    target = {i: n.get("status") or "not_started" for i, n in by_id.items()}

    def settled(i):
        s = target.get(i)
        return "pending" if s in IN_PROGRESS else s

    def after(n):
        status = n.get("status") or "not_started"
        if status in IN_PROGRESS:
            ok = all(settled(_dep_id(d)) == "done" for d in n.get("deps") or [])
            return "ready" if ok else "not_started"
        return status

    ready, waiting, kept, missing, restored = [], [], [], [], []
    if created:
        stamp = _stamp(store)
        _create(store, f"projects/{pid}/project.json", {
            "schema": "waypoint.project.v1", "id": pid, "title": sp.get("title") or pid, "brief": sp.get("brief") or "",
            "goal_hash": None, "cwd": _cwd_obj(sp.get("cwd")), "created_at": sp.get("created") or stamp,
            "archived": False, "status": sp.get("status") if sp.get("status") in ("draft", "active") else "active",
            "family": None, "version": None})
        for n in snap["nodes"]:
            status = after(n)
            (ready if status == "ready" else waiting if n.get("status") in IN_PROGRESS else restored).append(n["id"])
            _create(store, f"projects/{pid}/nodes/{n['id']}.json", {
                "schema": "waypoint.node.v1", "project": pid, "id": n["id"], "title": n.get("title") or n["id"],
                "goal": n.get("goal") or "", "goal_hash": None, "check_hash": None,
                "check": n.get("check") or {"type": "human", "prompt": "Confirm"},
                "deps": [{"id": _dep_id(d), "crosses": "restored from snapshot"} for d in n.get("deps") or []],
                "parent": None, "estimate": 1, "slot": None, "location": {"where": "unset", "changed_at": None},
                "files": [], "kind": "worker", "owner": n.get("owner") or "worker", "lane": n.get("lane"),
                "status": status, "attempts": int(n.get("attempts") or 0), "actor": n.get("actor"),
                "evidence": n.get("evidence")})
    else:
        local = _nodes(store, pid)
        for n in snap["nodes"]:
            nid = n["id"]
            cur = local.get(nid)
            if cur is None:
                missing.append(nid)
                continue
            snap_status = n.get("status") or "not_started"
            if cur.get("status") not in STARTABLE and cur.get("status") != snap_status:
                kept.append(nid)  # local work already moved on: never undo it
                continue
            status = after(n)
            if status == "ready" and snap_status in IN_PROGRESS:
                ready.append(nid)
            elif snap_status in IN_PROGRESS:
                waiting.append(nid)
            if status == cur.get("status") and n.get("evidence") == cur.get("evidence"):
                continue
            _update(store, f"projects/{pid}/nodes/{nid}.json", cur, status=status,
                    attempts=int(n.get("attempts") or 0), actor=n.get("actor"), evidence=n.get("evidence"))
            restored.append(nid)
    _event(store, "resume", f"Resumed '{pid}' from snapshot {sid}: {len(restored)} restored.", action, project=pid,
           data={"snapshot_id": sid, "snapshot_revision": snap["revision"], "created": created})
    return {"status": "done", "result": {"project": pid, "created": created, "restored": restored,
                                          "reset_to_ready": ready, "reset_to_waiting": waiting,
                                          "kept_local": kept, "missing": missing,
                                          "snapshot_revision": snap["revision"]}}


# ------------------------------------------------------------------ registration
_HANDLERS = {"project_archive": project_archive, "project_unarchive": project_unarchive,
             "version_new": version_new, "version_decide": version_decide, "family_group": family_group,
             "version_label": version_label_set, "version_merge": version_merge,
             "state_snapshot_write": state_snapshot_write, "state_snapshot_resume": state_snapshot_resume}


def _guard(fn):
    def handler(action, ctx):
        try:
            return fn(action, ctx)
        except Refuse as e:
            return {"status": "refused", "error": str(e)}
        except Fail as e:
            return {"status": "failed", "error": str(e)}
        except RevConflict as e:
            return {"status": "failed", "error": f"stale revision, nothing more was written: {e}"}
        except (KeyError, TypeError, ValueError, OSError) as e:
            return {"status": "failed", "error": f"{type(e).__name__}: {e}"}
    handler.__name__ = fn.__name__
    return handler


def register(handlers):
    """Add each action type of this module to ``handlers`` (type -> handler(action, ctx) -> dict)."""
    for typ, fn in _HANDLERS.items():
        handlers[typ] = _guard(fn)
