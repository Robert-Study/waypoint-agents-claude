"""Add and edit one node in place (Python only; the CLI and the board MCP share it). Stdlib only.

The locks are the ones engine/cc_engine/plan.py writes (store.goal_hash / check_hash; a test keeps them equal):
- ``goal_hash`` = sha256 of the goal text; only the owner changes a goal (``GoalLocked``).
- ``check_hash`` = sha256 of the check as sorted, compact JSON. A non-owner who changes the done-check sends the node
  back to ``not_started`` and clears its evidence: the old pass was against a different check.
Status changes go through the status commands, never through an edit. Callers write the result with Store.write and
check the whole project for a cycle with compute_float.
"""
import hashlib
import json
import re

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
CHECK_TYPES = ("command", "files_exist", "text_present", "human")  # = engine/cc_engine/checks.py
STATUSES = ("not_started", "ready", "running", "submitted", "done", "error", "needs_you", "skipped", "carried")
OWNER_ONLY_STATUSES = ("done", "skipped", "carried")
EDITABLE = ("title", "goal", "check", "deps", "files", "estimate", "kind", "lane", "owner", "effort", "recommended",
            "parent")


class NodeError(ValueError):
    pass


class GoalLocked(NodeError):
    pass


def goal_hash(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def check_hash(check):
    return goal_hash(json.dumps(check or {}, sort_keys=True, separators=(",", ":")))


def dep_id(d):
    return d if isinstance(d, str) else (d.get("id") if isinstance(d, dict) else None)


def _check(check):
    if not isinstance(check, dict) or check.get("type") not in CHECK_TYPES:
        raise NodeError("the done-check needs a type: %s" % "/".join(CHECK_TYPES))
    need = {"command": ("run",), "files_exist": ("paths",), "text_present": ("path", "text")}.get(check["type"], ())
    missing = [k for k in need if not check.get(k)]
    if missing:
        raise NodeError("a %s check needs %s" % (check["type"], " and ".join(missing)))
    return check


def _field(key, value):
    """One editable field, checked. Returns the value to store."""
    if key == "title":
        if not isinstance(value, str) or not value.strip():
            raise NodeError("title must be non-empty text")
        return " ".join(value.split())
    if key == "goal":
        if not isinstance(value, str) or not value.strip():
            raise NodeError("the goal must be one non-empty sentence")
        return value.strip()
    if key == "check":
        return _check(value)
    if key == "deps":
        if not isinstance(value, list) or any(not isinstance(dep_id(d), str) or not ID_RE.match(dep_id(d))
                                              for d in value):
            raise NodeError("deps must be a list of node ids or {id, crosses} objects")
        return value
    if key == "files":
        if not isinstance(value, list) or any(not isinstance(f, str) or not f.strip() for f in value):
            raise NodeError("files must be a list of paths")
        return value
    if key == "estimate":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise NodeError("estimate must be a number >= 0")
        return value
    if key == "parent":
        if value is not None and (not isinstance(value, str) or not ID_RE.match(value)):
            raise NodeError("parent must be a node id or null")
        return value
    if value is not None and not isinstance(value, str):
        raise NodeError("%s must be text or null" % key)
    return value


def build_node(project, node_id, fields, is_owner=True):
    """A new node entity (no rev yet). ``fields`` needs goal and check; status defaults to not_started."""
    for v, what in ((project, "project"), (node_id, "node id")):
        if not isinstance(v, str) or not ID_RE.match(v):
            raise NodeError("%s must be letters, digits, '.', '_' or '-'" % what)
    unknown = set(fields) - set(EDITABLE) - {"status", "evidence"}
    if unknown:
        raise NodeError("unknown node fields: %s" % ", ".join(sorted(unknown)))
    if "goal" not in fields or "check" not in fields:
        raise NodeError("a new node needs a goal and a done-check")
    status = fields.get("status") or "not_started"
    if status not in STATUSES:
        raise NodeError("status must be one of %s" % ", ".join(STATUSES))
    if status in OWNER_ONLY_STATUSES and not is_owner:
        raise GoalLocked("only the owner adds a node as %s" % status)
    evidence = fields.get("evidence")
    if evidence is not None and not isinstance(evidence, str):
        raise NodeError("evidence must be text")
    n = {"schema": "waypoint.node.v1", "project": project, "id": node_id, "title": node_id, "goal": "",
         "goal_hash": None, "check_hash": None, "check": None, "deps": [], "parent": None, "estimate": 1,
         "slot": None, "location": {"where": "unset", "changed_at": None}, "files": [], "kind": "worker",
         "owner": None, "lane": None, "effort": None, "recommended": None, "status": status, "attempts": 0,
         "actor": None, "evidence": evidence}
    for k in EDITABLE:
        if k in fields:
            n[k] = _field(k, fields[k])
    n["goal_hash"], n["check_hash"] = goal_hash(n["goal"]), check_hash(n["check"])
    return n


def apply_edit(node, changes, is_owner=False):
    """The node with ``changes`` applied (a new dict). Goal: owner only. Check changed by a non-owner: reset."""
    if not isinstance(changes, dict) or not changes:
        raise NodeError("nothing to change")
    unknown = set(changes) - set(EDITABLE)
    if unknown:
        raise NodeError("these fields are not editable here: %s (status has its own commands)"
                        % ", ".join(sorted(unknown)))
    new = {k: v for k, v in node.items() if k not in ("rev", "updated_at", "updated_by")}
    for k, v in changes.items():
        new[k] = _field(k, v)
    if new.get("goal") != node.get("goal"):
        if not is_owner:
            raise GoalLocked("the goal of %s is locked; only the owner may change it" % node.get("id"))
        new["goal_hash"] = goal_hash(new["goal"])
    if check_hash(new.get("check")) != (node.get("check_hash") or check_hash(node.get("check"))):
        new["check_hash"] = check_hash(new["check"])
        if not is_owner and new.get("status") not in ("not_started",):
            new.update(status="not_started", evidence=None, attempts=0)
    return new
