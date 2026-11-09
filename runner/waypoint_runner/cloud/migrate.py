"""Error-triggered migration to the cloud (Waypoint 1.2, node cloud-migrate).

Two triggers move a PC node to the cloud (location ``cloud``, status ``ready``; the cloud worker only picks ready
cloud nodes):

* ``migrate_if_exhausted``: the recovery ledger says the node is ``exhausted`` (its PC retries are spent).
* ``migrate_on_shutdown``: the PC is going down; running nodes are handed over like cloud.heartbeat does, except
  nodes whose remote side effect is unsettled are held back.

Retry counting belongs to ``waypoint_runner.recovery`` (the bound is ``max_retries`` in settings/settings.json,
read there). This module never counts or retries; it only reacts to the exhausted state, so no second bound exists.

Exactly once: a marker file ``migrations/<project>__<node>__<last_job>.json`` is created exclusively before the node
moves. One exhausted episode (one last job) therefore migrates once, whatever calls arrive concurrently or later.

Unsettled remote side effect: a job whose ``remote_effect`` is ``unknown`` may have already done something remote
(a push, a message). Moving it to the cloud would repeat that. It moves only when ``settle(job) is True``;
otherwise the result is ``needs_you`` and the node stays where it is.

Claims use waypoint_data Claims (the shared protocol); a node owned by a cloud run is never touched.
"""
import os
import sys
import time

from waypoint_runner import recovery as _recovery  # puts core/waypoint-data/py on sys.path
from waypoint_runner.cloud import heartbeat as _hb

from waypoint_data import io as wio  # noqa: E402
from waypoint_data.claims import Claims  # noqa: E402
from waypoint_data.store import RevConflict, Store, iso, serialize  # noqa: E402

__all__ = ["migrate_if_exhausted", "migrate_on_shutdown"]

WRITER = "cloud-migrate"
MARKER_SCHEMA = "waypoint.cloud_migration.v1"
UNSETTLED = "unsettled_remote_effect"


def _result(status, **kw):
    return dict({"status": status}, **kw)


def _stored_job(store, job_id):
    if not job_id or job_id == "none":
        return None
    try:
        j = store.read(f"jobs/{job_id}/state.json")
    except Exception:
        return None
    return j if isinstance(j, dict) else None


def has_unsettled_effect(root, project, node):
    """Default shutdown guard: True when any job of the node has remote_effect ``unknown`` (it may have already acted
    remotely), or when the job store cannot be read (fail closed: an unreadable record is treated as unsettled)."""
    store = Store(root)
    try:
        names = sorted(n for n in os.listdir(store.path("jobs")) if not wio.is_ignored_name(n))
    except OSError:
        return False
    for name in names:
        if "." in name:
            continue
        try:
            j = store.read(f"jobs/{name}/state.json")
        except Exception:
            return True
        if (isinstance(j, dict) and j.get("project") == project and j.get("node") == node
                and j.get("remote_effect") == "unknown"):
            return True
    return False


def _marker_rel(project, node, last_job):
    safe = "".join(c if c.isalnum() or c in "._-" else "-" for c in str(last_job))
    return f"migrations/{project}__{node}__{safe}.json"


def _move(store, claims, project, nid, now):
    """Write location cloud + status ready, then release a PC claim by its token. False if the node is gone."""
    rel = f"projects/{project}/nodes/{nid}.json"
    for _ in range(3):
        cur = store.read(rel)
        if not isinstance(cur, dict):
            return False
        new = dict(cur, status="ready", location={"where": "cloud", "changed_at": iso(now())})
        try:
            store.write(rel, new, expected_rev=cur.get("rev", 0))
            break
        except RevConflict:
            continue
    else:
        raise RuntimeError("rev conflict")
    claim = claims.read(project, nid)
    if claim and claim.get("token") and not str(claim.get("owner", "")).startswith("cloud:"):
        claims.release(project, nid, claim["token"])
    return True


def migrate_if_exhausted(root, project, node, ledger, now=time.time, job=None, settle=None):
    """Move ``node`` to the cloud once, only when ``ledger`` holds the exhausted state for it.

    Returns {"status": migrated | already_migrated | not_exhausted | needs_you | cloud_owned | missing, ...}.
    ``job`` (the last ended job) lets the unsettled-effect rule apply; ``settle(job)`` must return True to prove
    the remote effect settled.
    """
    if ledger.state(project, node) != "exhausted":
        return _result("not_exhausted", node=node)
    rec = ledger._read(project, node)
    last_job = rec.get("last_job") or "none"
    store = Store(root, writer=WRITER, now=now)
    claims = Claims(root, now=now, writer=WRITER)
    marker = os.path.join(os.path.abspath(root), *_marker_rel(project, node, last_job).split("/"))
    if os.path.exists(marker):
        return _result("already_migrated", node=node)
    if job is None:
        job = _stored_job(store, last_job)
    if rec.get("reason") == UNSETTLED or (job is not None and job.get("remote_effect") == "unknown"):
        # never trust a ledger flag alone: only an explicit settle(job) is True proves the effect settled
        if not (settle is not None and job is not None and settle(job) is True):
            return _result("needs_you", node=node, reason=UNSETTLED)
    claim = claims.read(project, node)
    if claim and str(claim.get("owner", "")).startswith("cloud:"):
        return _result("cloud_owned", node=node)
    os.makedirs(os.path.dirname(marker), exist_ok=True)
    body = {"schema": MARKER_SCHEMA, "project": project, "node": node, "last_job": last_job,
            "reason": rec.get("reason"), "at": iso(now())}
    try:
        wio.create_exclusive(marker, serialize(body), writer=WRITER)
    except FileExistsError:
        return _result("already_migrated", node=node)
    try:
        moved = _move(store, claims, project, node, now)
    except Exception:
        try:
            os.remove(marker)  # nothing moved: let a later call try again
        except OSError:
            pass
        raise
    if not moved:
        try:
            os.remove(marker)
        except OSError:
            pass
        return _result("missing", node=node)
    try:
        store.append_event(WRITER, {"kind": "cloud.migrated", "project": project, "node": node,
                                    "summary": "retries exhausted; moved to cloud",
                                    "data": {"last_job": last_job, "reason": rec.get("reason")}})
    except OSError:
        pass
    return _result("migrated", node=node)


def migrate_on_shutdown(root, now=time.time, host=None, budget_s=_hb.DEFAULT_BUDGET_S, unsettled=None,
                        monotonic=time.monotonic):
    """Shutdown path: hand every running PC node to the cloud, except those ``unsettled(project, node)`` flags
    (default: any job of the node with remote_effect unknown).

    Held nodes stay running with their PC claim (they are never repeated remotely); nodes past the time budget are
    ``incomplete`` and fall back to the cloud worker's staleness reclaim, as in cloud.heartbeat.
    """
    host = host or _hb.default_host()
    if unsettled is None:
        def unsettled(project, nid):
            return has_unsettled_effect(root, project, nid)
    store = Store(root, writer=_hb.WRITER, now=now)
    claims = Claims(root, now=now, writer=_hb.WRITER)
    start = monotonic()
    handed, held, incomplete = [], [], []
    todo = _hb.running_nodes(root)
    for i, (project, nd) in enumerate(todo):
        key = f"{project}/{nd['id']}"
        if monotonic() - start > budget_s:
            incomplete.extend(f"{p}/{n['id']}" for p, n in todo[i:])
            break
        if unsettled(project, nd["id"]):
            held.append(key)
            continue
        try:
            if _hb._hand_off_one(store, claims, project, nd, now):
                handed.append(key)
        except Exception:
            incomplete.append(key)
    out = {"handed_off": handed, "held": held, "incomplete": incomplete}
    if incomplete:
        out["fallback"] = "staleness"
    try:
        store.append_event(WRITER, {"kind": "pc.handoff", "summary": f"{len(handed)} node(s) handed to cloud",
                                    "data": {"host": host, "handed_off": handed, "held": held,
                                             "incomplete": incomplete}})
        _hb.write_heartbeat(root, host, now=now, state="handed_off")
    except OSError:
        pass
    return out
