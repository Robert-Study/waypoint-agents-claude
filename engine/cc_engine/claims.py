"""Claims: a lead session or worker says "I am working on this task" so Owner sees it.

Claiming sets the task to ``running`` with the claimer's own name as the actor (``lead-session``
or a worker name, recorded truthfully), so it pulses blue on every view live over SSE, and keeps
who is on it, the worktree, the working folder and the environment for the Inspector.

Rules
- A claim never marks a task done. Release returns a still-``running`` task to its derived status
  (stored ``not_started``; the board shows ``ready`` when its dependencies are done). When a check
  completed the task meanwhile, release only drops the claim.
- A claim is stale after ``store.CLAIM_STALE_SECONDS`` (about 2 hours) without a refresh; the
  same claimer refreshes it by claiming again. Someone else may take over only a stale claim.
- Claims are refused in a draft project, on a done task, on a task the scheduler is already
  running, and for names that would impersonate Owner or a check (``owner``, ``check:*``).
- Routes need a credential (dsh page Origin = owner, engine token = router); the router may claim
  and release but, as everywhere, never mark a task done.

CLI: ``python -m cc_engine claim --project P --node N --by "<who>" [--worktree W] [--cwd C] [--env E]``
and ``python -m cc_engine release --project P --node N [--by "<who>"]``.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timezone

from . import env_policy
from .store import CLAIM_LIVE_STATUSES, NotAllowed, claim_view

LEAD = "lead-session"
MAX_TEXT = 300
NAME_RE = re.compile(r"^[\w .:@()/+-]{1,64}$")
RESERVED = ("owner", "router")
CLAIMABLE = ("not_started", "ready", "error", "needs_you")


class ClaimConflict(Exception):
    """Someone else holds a fresh claim, or the task is already running without one."""


def _now(now=None):
    return now or datetime.now(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _name(by):
    by = " ".join(str(by or "").split())
    if not by or not NAME_RE.match(by):
        raise ValueError("'by' must name who is working (letters, digits, spaces, .:-_@()/+; up to 64 characters)")
    low = by.lower()
    if low in RESERVED or low.startswith("check:") or low.startswith("scheduler"):
        raise NotAllowed(f"{by!r} is reserved; name the session or worker that is really doing the work")
    return by


def _text(value):
    if value in (None, ""):
        return None
    text = " ".join(str(value).split())
    return text[:MAX_TEXT]


def claim(store, project_id, node_id, by, worktree=None, cwd=None, environment=None, now=None) -> dict:
    """Claim (or refresh a claim on) a task. Returns ``{"claim": view, "refreshed": bool}``."""
    by = _name(by)
    t = _now(now)
    node = store.get_node(project_id, node_id)
    if node is None:
        raise KeyError(f"no node {project_id}/{node_id}")
    if store.is_draft(project_id):
        raise NotAllowed(f"project {project_id} is a draft; Owner must accept it before tasks run")
    status = node["status"]
    if status == "done":
        raise NotAllowed(f"task {node_id} is done; Owner reopens it before anyone works on it again")
    row = store.claim_row(project_id, node_id)
    live = row is not None and status in CLAIM_LIVE_STATUSES
    fields = {"worktree": _text(worktree), "cwd": _text(cwd), "environment": _text(environment)}
    if fields["environment"] is None:  # T2: nobody said where it runs, so env_policy decides
        others = {k: v for k, v in store.claims(project_id, t).items() if k != node_id and v.get("by") != by}
        task = {"repo": fields["cwd"] or node.get("cwd"), "bounded": True}
        pick = env_policy.choose(task, {"writers": env_policy.writers_from_claims(others, task["repo"])})
        fields["environment"] = _text(pick["env"].replace("_", " "))
    if live and row["claimed_by"] == by:  # a refresh: same claimer, status untouched
        merged = {k: (v if v is not None else row.get(k)) for k, v in fields.items()}
        store.put_claim(project_id, node_id, by, since=row["since"], refreshed_at=_iso(t), **merged)
        store.notify("state", {"project": project_id, "revision": store.revision()})
        return {"claim": claim_view(store.claim_row(project_id, node_id), t), "refreshed": True}
    if live and not claim_view(row, t)["stale"]:
        raise ClaimConflict(f"{row['claimed_by']} is working on task {node_id} (claimed {row['since']})")
    if not live and status not in CLAIMABLE:
        who = node.get("actor") or "the scheduler"
        raise ClaimConflict(f"task {node_id} is already {status.replace('_', ' ')} (by {who})")
    takeover = row["claimed_by"] if live else None
    store.put_claim(project_id, node_id, by, since=_iso(t), refreshed_at=_iso(t), **fields)
    where = fields["worktree"] or fields["cwd"]
    summary = f"{by} is working on task {node_id}" + (f" in {where}" if where else "") + "."
    if takeover:
        summary = f"{by} took over task {node_id} from {takeover}, whose claim had gone stale."
    try:
        if status != "running":
            store.set_node_status(project_id, node_id, "running", by, summary=summary,
                                  data={"claim": dict(fields, by=by)})
        else:  # a stale takeover: the task stays running; record the new actor truthfully
            store.update_node(project_id, node_id, {"actor": by}, by, summary)
    except BaseException:
        store.drop_claim(project_id, node_id)
        raise
    return {"claim": claim_view(store.claim_row(project_id, node_id), t), "refreshed": False}


def release(store, project_id, node_id, by=None, actor=None) -> dict:
    """Drop a claim. A task still ``running`` under it returns to ``not_started`` (derived status);
    anything a check or Owner set meanwhile is left alone. Never sets ``done``."""
    node = store.get_node(project_id, node_id)
    if node is None:
        raise KeyError(f"no node {project_id}/{node_id}")
    row = store.claim_row(project_id, node_id)
    if row is None:
        return {"released": False, "status": node["status"]}
    claimer = row["claimed_by"]
    who = actor or (_name(by) if by else claimer)
    store.drop_claim(project_id, node_id)
    status = node["status"]
    if status == "running" and node.get("actor") == claimer:
        label = "Owner" if who == "owner" else who
        summary = (f"{label} stopped working on task {node_id}; it goes back to waiting."
                   if label == claimer else f"{label} released {claimer}'s claim on task {node_id}.")
        store.set_node_status(project_id, node_id, "not_started", who, summary=summary,
                              data={"released": claimer})
        status = "not_started"
    else:
        store.notify("state", {"project": project_id, "revision": store.revision()})
    return {"released": True, "status": status, "by": claimer}


def claims_for(store, project_id, now=None) -> dict:
    return store.claims(project_id, now)


# ---------------------------------------------------------------- routes
def _http_error(status, body):
    from .api import HttpError  # lazy: api imports extension modules
    return HttpError(status, body)


def _body(h, *keys):
    """The JSON body with ``node`` defaulting to ``id``; 400 when a required key is missing."""
    b = h._json_body()
    if not b.get("node"):
        b["node"] = b.get("id")
    missing = [k for k in keys if not str(b.get(k) or "").strip()]
    if missing:
        raise _http_error(400, {"error": "bad_request", "detail": "missing " + ", ".join(missing)})
    return b


def p_claim(h, q):
    """{project, node (or id), by, worktree?, cwd?, environment?} -> {ok, claim, refreshed}."""
    b = _body(h, "project", "node", "by")
    if h.app.store.get_project(b["project"]) is None:
        raise _http_error(404, {"error": "not_found", "detail": "no such project"})
    try:
        out = claim(h.app.store, b["project"], b["node"], b["by"], worktree=b.get("worktree"), cwd=b.get("cwd"),
                    environment=b.get("environment") or b.get("env"))
    except ClaimConflict as e:
        raise _http_error(409, {"error": "claimed", "detail": str(e)})
    return {"ok": True, **out}


def p_release(h, q):
    """{project, node (or id), by?} -> {ok, released, status}."""
    b = _body(h, "project", "node")
    actor = "owner" if getattr(h, "actor", None) == "owner" else None
    out = release(h.app.store, b["project"], b["node"], by=b.get("by"), actor=actor)
    return {"ok": True, **out}


def g_claims(h, q):
    pid = h._project(q)
    return {"project": pid, "claims": claims_for(h.app.store, pid)}


ROUTES = {
    "POST /api/claim": p_claim,
    "POST /api/release": p_release,
    "GET /api/claims": g_claims,
}


# ---------------------------------------------------------------- CLI
def _open_store(db):
    from . import default_db_path
    from .store import Store
    return Store(db or default_db_path())


def cli_claim(argv) -> int:
    ap = argparse.ArgumentParser(prog="cc_engine claim", description="Mark a task as being worked on.")
    ap.add_argument("--project", required=True)
    ap.add_argument("--node", required=True)
    ap.add_argument("--by", default=LEAD, help=f"who is working (default {LEAD})")
    ap.add_argument("--worktree", default=None)
    ap.add_argument("--cwd", default=None, help="working folder (default: the current folder)")
    ap.add_argument("--env", default=None, help="environment, e.g. 'Claude Code desktop, Opus'")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)
    store = _open_store(args.db)
    try:
        out = claim(store, args.project, args.node, args.by, worktree=args.worktree,
                    cwd=args.cwd or os.getcwd(), environment=args.env)
    except (KeyError, ValueError, NotAllowed, ClaimConflict) as e:
        print(f"claim refused: {str(e).strip(chr(39))}", file=sys.stderr)
        return 1
    finally:
        store.close()
    c = out["claim"]
    verb = "refreshed" if out["refreshed"] else "claimed"
    print(f"{verb} {args.project}/{args.node} for {c['by']}" + (f" in {c['worktree']}" if c.get("worktree") else ""))
    return 0


def cli_release(argv) -> int:
    ap = argparse.ArgumentParser(prog="cc_engine release", description="Stop working on a claimed task.")
    ap.add_argument("--project", required=True)
    ap.add_argument("--node", required=True)
    ap.add_argument("--by", default=None)
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)
    store = _open_store(args.db)
    try:
        out = release(store, args.project, args.node, by=args.by)
    except (KeyError, ValueError, NotAllowed) as e:
        print(f"release refused: {str(e).strip(chr(39))}", file=sys.stderr)
        return 1
    finally:
        store.close()
    if not out["released"]:
        print(f"{args.project}/{args.node} had no claim")
    else:
        print(f"released {args.project}/{args.node}; it is now {out['status'].replace('_', ' ')}")
    return 0
