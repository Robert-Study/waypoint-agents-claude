"""Scheduler and punch clock.

``tick(store, now, dispatcher)`` is safe to call as often as you like (Task
Scheduler every 10 min, at logon, on resume from sleep, or by hand):

1. It takes a lease row in ``meta`` (15-minute expiry). A second tick while the
   lease is live does nothing; a lease left by a crashed tick simply expires.
2. For every active run it reconciles running jobs: finished -> classified
   (``failure.classify_exit``). A transient stop (non-zero exit, usage limit, 403,
   bridge timeout, job timeout, lost job) puts the node back to ready after a delay,
   holds the pool until it resets and is never counted as an attempt; a completed
   attempt goes to ``submitted`` and its done-check. A job is lost only when its
   recorded pid is not alive. Then it starts every ready node whose pool is ticked
   and open, up to ``max_concurrency``. Waves are display order only. Draft projects
   (``store.project_runnable``) are never dispatched.
3. Usage gate. Subscription pools (``claude_code`` = Claude Max, ``codex`` = Owner's
   Codex plan) start work only on a FRESH reading at or above the reserve floor
   (``reserve_percent``, default per pool: Codex 20, everything else 0; note 146). STALE/UNKNOWN holds the pool (one summary
   sentence per hold). Claude extra usage is never counted. Free pools
   (Freebuff, Gemini App) are not gated. Codex is gated on ``codex_5h``/``codex_week``.
4. Sprints (punch clock): ``settings.sprints`` ``[{pool, start_at}]`` become sprint
   rows on the first tick; a pool does not start before its sprint's ``start_at``,
   and edited times win. A pool below its floor with a known reset gets a resume
   sprint at ``resets_at + 60 s`` (auto-resume / merged resets), as does a run with
   ``align_to_reset``.
   Autonomous runs share one punch clock: ``settings.sprint_run`` names the Autonomous anchor
   run whose sprint rows gate every run of the sequence. A pool is open while one of its
   sprints is current, even if a later one is planned.
5. Priority rows (Autonomous): ``settings.after_runs`` lists the runs of the projects in the
   rows above; the run stays queued until none of them is active (one summary sentence).
6. Agents switches (``cc_engine.toggles``, read from this store every tick): a switched-off
   pool is never ticked or dispatched, whatever the run settings say; a switched-off model is
   skipped for the next model on the pool's ladder (``Dispatcher.refusal`` too); with none
   left the task waits and says why.
7. When every node is done the run is closed and ``power.power_off_if_done`` runs;
   it only powers off when no run in any project is active and no job is alive,
   and is retried on later ticks until then. A cancelled run's live jobs are killed.
8. Unattended runs (``cc_engine.overnight``; td-fde6a7): a job that stopped early is marked in its
   folder (``dispatch.mark_stopped``) and the next attempt continues from its partial work; a
   provider limit releases a pool/model the scheduler picked (not one the plan pinned), so the task
   moves to the next open pool instead of waiting for the reset; each ``needs_you`` task becomes
   a human to-do; a run whose open tasks all wait on Owner is parked and no longer holds back
   the Autonomous rows below it; a finished or parked run writes ``receipts/<run id>.md``.

All state lives in SQLite, so a restart simply resumes on the next tick.
"""
from __future__ import annotations

import argparse
import os
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

LEASE_KEY = "tick_lease"
LEASE_SECONDS = 15 * 60
RESET_GRACE_SECONDS = 60
ACTIVE_RUN_STATUSES = ("queued", "scheduled", "running")
ACTOR = "scheduler"

DEFAULT_RESERVE_PERCENT = 0.0  # note 146: Codex 20% (pick.DEFAULT_FLOORS), everything else 0%
from .dispatch import AUTOMATED_ROUTES, mark_stopped, route_for_pool  # noqa: E402
from .pick import floor_for  # noqa: E402
from .store import CLOSED_STATUSES  # noqa: E402


# ---------------------------------------------------------------- time helpers

def to_epoch(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def iso(epoch):
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_hhmm(epoch):
    return datetime.fromtimestamp(float(epoch)).strftime("%H:%M")


def sprint_start_for_reset(resets_at):
    """Sprints start one minute after the pool resets (the 5h01m failsafe)."""
    return to_epoch(resets_at) + RESET_GRACE_SECONDS


# ---------------------------------------------------------------- store helpers
# Thin wrappers over cc_engine.store.Store so callers read as plain English.

def goal_hash(text):
    from .store import goal_hash as gh
    return gh(text)


def norm_node(n):
    n = dict(n)
    if n.get("deps") is None:
        raw = n.get("deps_json") or "[]"
        n["deps"] = json.loads(raw) if isinstance(raw, str) else list(raw)
    if n.get("check") is None:
        raw = n.get("check_json") or "{}"
        n["check"] = json.loads(raw) if isinstance(raw, str) else dict(raw)
    n["attempts"] = int(n.get("attempts") or 0)
    return n


def list_nodes(store, project_id):
    return [norm_node(n) for n in store.list_nodes(project_id)]


def get_node(store, project_id, node_id):
    n = store.get_node(project_id, node_id)
    return norm_node(n) if n else None


def emit(store, project_id, node_id, kind, summary, actor=ACTOR, data=None):
    """Write one plain-English summary event (never bumps the revision)."""
    return store.event(project_id, node_id, kind, actor, summary, data or {})


def set_status(store, project_id, node_id, status, summary, actor=ACTOR, evidence=None):
    return store.set_node_status(project_id, node_id, status, actor, evidence, summary)


def update_node_fields(store, project_id, node_id, fields, actor, summary):
    """Revision-bumping update of non-goal node fields (``deps`` as a list)."""
    from .store import GOAL_FIELDS, GoalLocked
    if GOAL_FIELDS & set(fields):
        raise GoalLocked(f"automatic changes may not touch {sorted(GOAL_FIELDS & set(fields))}")
    return store.update_node(project_id, node_id, dict(fields), actor, summary)


def meta_get(store, key):
    return store.get_meta(key)


def meta_set(store, key, value):
    store.set_meta(key, value)


def project_runnable(store, project_id):
    """``store.project_runnable`` when the store has it (draft projects are not runnable)."""
    fn = getattr(store, "project_runnable", None)
    if callable(fn):
        return bool(fn(project_id))
    project = store.get_project(project_id) or {}
    return project.get("status") != "draft"


# ---------------------------------------------------------------- settings / usage

RUNSHEET_KEYS = ("preset", "pools", "advanced", "auto_resume", "power_off", "merge_resets",
                 "use_pool_fully", "sprints", "efforts")
# Scheduler-only keys kept beside the expanded run sheet (top level or under ``advanced``).
SCHEDULER_KEYS = ("reserve_percent", "freebuff_file_work", "job_timeout_minutes")
LOCAL_KEYS = ("align_to_reset", "default_pool", "claude_extra_args", "effort_ladder",
              "sprint_run", "after_runs", "planned_by") + SCHEDULER_KEYS
# Subscription pools -> usage pools (usage.read_usage ids) that gate them. Free pools
# (freebuff, gemini_app) are not listed and are never gated.
USAGE_POOLS = {"claude_code": ("claude_5h", "claude_week"), "codex": ("codex_5h", "codex_week")}


def pool_refusal(pool, toggles=None):
    """Reason the Agents switches keep ``pool`` from running, else None."""
    from . import toggles as T
    snap = T.resolve(toggles)
    return snap.refusal(route_for_pool(pool)) or snap.refusal(pool)


def expand_settings(raw, toggles=None):
    """Run settings -> ``runsheet.expand`` output plus ``pools``, ``sprints`` and scheduler keys.

    Pools and ladder models switched off in Agents are dropped (``toggles``: Snapshot, Store,
    path, or None for the live database).
    """
    from . import toggles as T
    snap = T.resolve(toggles)
    raw = dict(raw or {})
    sheet = {k: raw[k] for k in RUNSHEET_KEYS if k in raw}
    adv =dict(sheet.get("advanced") or {}) if isinstance(sheet.get("advanced"), dict) else None
    local = {k: raw[k] for k in LOCAL_KEYS if k in raw}
    if adv is not None:
        for k in SCHEDULER_KEYS:
            if k in adv:
                local.setdefault(k, adv.pop(k))
        sheet["advanced"] = adv
    sheet.setdefault("preset", "balanced")
    try:
        from . import runsheet
    except ImportError:  # until engine-core lands runsheet; same output shape
        runsheet = None
    if runsheet is not None:
        out = dict(runsheet.expand(sheet, toggles=snap))
    else:
        a = adv or {}
        out = {"model_ladder": a.get("model_ladder", {}), "effort": a.get("effort", "medium"),
               "max_concurrency": a.get("max_concurrency", 2),
               "auto_resume": raw.get("auto_resume", True), "power_off": raw.get("power_off", False),
               "merge_resets": raw.get("merge_resets", True)}
    pools = raw.get("pools") or []
    if isinstance(pools, dict):
        pools = [k for k, v in pools.items() if v]
    out["pools"] = [p for p in pools if not pool_refusal(p, snap)
                    and (runsheet is None or p in (out.get("model_ladder") or {}))]
    if "sprints" not in out:
        out["sprints"] = list(raw.get("sprints") or [])
    out.update(local)
    ladder = dict(out.get("model_ladder") or {})
    if "freebuff" in ladder:  # use the ids the Freebuff proxy really serves
        from .dispatch import freebuff_models
        valid = freebuff_models()
        ladder["freebuff"] = [m for m in ladder["freebuff"] if m in valid] or valid
        out["model_ladder"] = ladder
    return out


def ticked_pools(settings):
    return list(settings.get("pools") or [])


def _percent(raw):
    try:
        return min(100.0, max(0.0, float(raw)))
    except (TypeError, ValueError):
        return None


def reserve_percent(settings):
    """Explicit ``reserve_percent`` setting, or None: usage_gate then uses per-pool defaults.

    A number (0-100) applies to every gated pool. A dict sets floors per pool, keyed by usage pool
    (``codex_week``) or dispatch pool (``codex``); pools it leaves out keep their default floor.
    """
    raw = settings.get("reserve_percent")
    if isinstance(raw, dict):
        out = {str(k): _percent(v) for k, v in raw.items()}
        return {k: v for k, v in out.items() if v is not None} or None
    return None if raw is None else _percent(raw)


def _row_reserve(reserve, usage_pool, pool):
    """Floor for one usage row: the run's per-pool or global setting, else the approved default."""
    if isinstance(reserve, dict):
        for key in (usage_pool, pool):
            if key in reserve:
                return reserve[key]
        reserve = None
    return reserve if reserve is not None else floor_for(usage_pool, DEFAULT_RESERVE_PERCENT)


def usage_gate(pool, usage_by_pool, reserve, now):
    """Decide whether usage lets ``pool`` start work. Returns ``(state, info)``.

    ``state``: ``open`` | ``free`` (not gated) | ``hold`` (STALE/UNKNOWN or a reading from before
    the last reset) | ``low`` (FRESH but below the reserve floor; ``info['resets_at']``).
    Only the base-plan ``remaining_percent`` counts: Claude extra usage never does.
    """
    if pool not in USAGE_POOLS:
        return "free", {}
    rows = [usage_by_pool.get(u) or {"pool": u, "status": "UNKNOWN"} for u in USAGE_POOLS[pool]]
    low, first_resets = [], None
    for r in rows:
        label = r.get("label") or r.get("pool")
        status = r.get("status") or "UNKNOWN"
        remaining = r.get("remaining_percent")
        resets = to_epoch(r.get("resets_at"))
        if first_resets is None and resets is not None and resets > now:
            first_resets = resets
        if status != "FRESH" or remaining is None:
            return "hold", {"reason": f"{label} usage is {status.lower()}"}
        if resets is not None and resets <= now:
            return "hold", {"reason": f"the {label} reading is from before its reset"}
        row_reserve = _row_reserve(reserve, r.get("pool"), pool)
        if float(remaining) < row_reserve or (float(remaining) <= 0 and row_reserve <= 0):
            low.append((r, resets, row_reserve))
    if low:
        known = [t for _, t, _ in low if t is not None]
        r = low[0][0]
        return "low", {"reason": (f"{r.get('label') or r.get('pool')} is at "
                                  f"{float(r['remaining_percent']):g}%, below the "
                                  f"{low[0][2]:g}% reserve"),
                       "resets_at": max(known) if known else None}
    return "open", {"resets_at": first_resets}


def pool_resets_at(pool, usage_by_pool, now):
    """Soonest future reset among the usage rows gating ``pool`` (or None).

    After that reset the usage gate itself keeps the pool closed if a longer window
    (e.g. the Claude week) is still below its floor.
    """
    times = []
    for u in USAGE_POOLS.get(pool, ()):
        t = to_epoch((usage_by_pool.get(u) or {}).get("resets_at"))
        if t is not None and t > now:
            times.append(t)
    return min(times) if times else None


def default_usage_reader():
    try:
        from . import usage
    except ImportError:
        return []
    return usage.read_usage()


# ---------------------------------------------------------------- pool holds

def _hold_key(pool):
    return f"pool_hold:{pool}"


def hold_pool(store, pool, until, reason):
    """Hold ``pool`` (for every run) until ``until`` after a transient failure."""
    cur = pool_hold(store, pool)
    if cur and cur.get("until", 0) >= until:
        return
    meta_set(store, _hold_key(pool), json.dumps({"until": until, "reason": reason}))


def pool_hold(store, pool):
    raw = meta_get(store, _hold_key(pool))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _note_hold(store, run, pool, reason, now):
    """One summary sentence per hold episode (meta only, never bumps the revision)."""
    key = f"gate:{run['id']}:{pool}"
    if meta_get(store, key) == reason:
        return
    meta_set(store, key, reason)
    emit(store, run["project_id"], None, "pool",
         f"The {pool} pool is paused because {reason}.",
         data={"run": run["id"], "pool": pool, "reason": reason})


def _clear_hold_note(store, run, pool):
    key = f"gate:{run['id']}:{pool}"
    if meta_get(store, key):
        meta_set(store, key, None)


def _auto_key(project_id, node_id):
    return f"auto_pick:{project_id}:{node_id}"


def note_auto_pick(store, project_id, node, pool, model):
    """Remember which of pool/model the scheduler filled in because the plan left them open."""
    picked = auto_pick(store, project_id, node["id"])
    if pool and not node.get("pool"):
        picked["pool"] = True
    if model and not node.get("model"):
        picked["model"] = True
    if picked:
        meta_set(store, _auto_key(project_id, node["id"]), json.dumps(picked))


def auto_pick(store, project_id, node_id):
    try:
        return dict(json.loads(meta_get(store, _auto_key(project_id, node_id)) or "{}"))
    except (TypeError, ValueError):
        return {}


def release_auto_pick(store, project_id, node_id):
    """Fields to clear so a stopped node can move to another open pool and its own ladder."""
    picked = auto_pick(store, project_id, node_id)
    meta_set(store, _auto_key(project_id, node_id), None)
    fields = {}
    if picked.get("pool"):
        fields["pool"] = None
    if picked.get("model"):
        fields.update(model=None, effort=None)
    return fields


# ---------------------------------------------------------------- lease

def acquire_lease(store, now, holder=None, seconds=LEASE_SECONDS):
    """Return a token if this caller now holds the lease, else None (atomic)."""
    token = holder or uuid.uuid4().hex
    with store.tx() as c:
        row = c.execute("SELECT value FROM meta WHERE key=?", (LEASE_KEY,)).fetchone()
        if row is not None and row[0]:
            cur = json.loads(row[0])
            if cur.get("expires_at", 0) > now and cur.get("token") != token:
                return None
        value = json.dumps({"token": token, "acquired_at": now, "expires_at": now + seconds})
        c.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (LEASE_KEY, value))
    return token


def release_lease(store, token):
    with store.tx() as c:
        row = c.execute("SELECT value FROM meta WHERE key=?", (LEASE_KEY,)).fetchone()
        if row is not None and row[0] and json.loads(row[0]).get("token") == token:
            c.execute("DELETE FROM meta WHERE key=?", (LEASE_KEY,))


# ---------------------------------------------------------------- runs & sprints

def active_runs(store, project_id=None):
    runs = [r for r in store.list_runs(limit=10000) if r["status"] in ACTIVE_RUN_STATUSES]
    if project_id:
        runs = [r for r in runs if r["project_id"] == project_id]
    return sorted(runs, key=lambda r: (r.get("created_at") or "", r["id"]))


def run_settings(run):
    if isinstance(run.get("settings"), dict):
        return dict(run["settings"])
    raw = run.get("settings_json") or "{}"
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


def sprints_for(store, run_id):
    return store.list_sprints(run_id)


def create_sprint(store, run_id, pool, start_at, end_at=None, actor=ACTOR, reason=None,
                  project_id=None):
    sid = f"s-{uuid.uuid4().hex[:10]}"
    start = to_epoch(start_at)
    store.create_sprint(sid, run_id, pool, iso(start), iso(to_epoch(end_at)) if end_at else None)
    if project_id is None:
        project_id = (store.get_run(run_id) or {}).get("project_id")
    emit(store, project_id, None, "sprint",
         reason or f"The {pool} sprint is set to start at {local_hhmm(start)}.", actor=actor,
         data={"sprint": sid, "run": run_id, "pool": pool, "start_at": iso(start)})
    return sid


def update_sprint(store, sprint_id, start_at, actor="owner"):
    """Edit a sprint's start time (punch clock). Later ticks honour it."""
    s = store.get_sprint(sprint_id)
    if s is None:
        raise KeyError(sprint_id)
    start = to_epoch(start_at)
    store.update_sprint(sprint_id, start_at=iso(start), status="scheduled")
    project_id = (store.get_run(s["run_id"]) or {}).get("project_id")
    emit(store, project_id, None, "sprint",
         f"The {s['pool']} sprint now starts at {local_hhmm(start)}.", actor=actor,
         data={"sprint": sprint_id, "start_at": iso(start), "was": s["start_at"]})
    return iso(start)


def _seed_sprints(store, run, settings):
    """Turn run-sheet ``sprints`` into sprint rows once; existing rows (edited times) win."""
    key = f"sprints_seeded:{run['id']}"
    if meta_get(store, key):
        return
    have = {s["pool"] for s in sprints_for(store, run["id"])}
    for row in settings.get("sprints") or []:
        if not isinstance(row, dict) or not row.get("pool") or not row.get("start_at"):
            continue
        if row["pool"] in have:
            continue
        try:
            start = to_epoch(row["start_at"])
        except ValueError:
            continue
        create_sprint(store, run["id"], row["pool"], start, row.get("end_at"),
                      project_id=run["project_id"],
                      reason=f"The {row['pool']} sprint starts at {local_hhmm(start)} as set on the run sheet.")
        have.add(row["pool"])
    meta_set(store, key, "1")


def _clock_run(settings):
    """The shared punch-clock run (Autonomous anchor) this run follows, or None."""
    rid = settings.get("sprint_run")
    return rid if isinstance(rid, str) and rid else None


def _pool_sprints(store, run, settings):
    """This run's sprint rows plus the shared punch clock's, if it follows one."""
    rows = list(sprints_for(store, run["id"]))
    clock = _clock_run(settings)
    if clock and clock != run["id"]:
        rows += list(sprints_for(store, clock))
    return rows


def _pool_open(store, run, pool, settings, usage_by_pool, now):
    """True if work may start on ``pool`` now. May create or advance sprints."""
    project_id = run["project_id"]
    sprints = _pool_sprints(store, run, settings)
    mine = [s for s in sprints if s["pool"] == pool and s["status"] in ("scheduled", "active")]
    waiting = current = False
    for s in mine:
        start = to_epoch(s["start_at"])
        if start is not None and start > now:
            waiting = True  # a sprint for this pool has not started yet (edited times win)
            if s["status"] == "active":
                store.update_sprint(s["id"], status="scheduled")
            continue
        if s["status"] == "scheduled":
            store.update_sprint(s["id"], status="active")
            s["status"] = "active"
            emit(store, project_id, None, "sprint", f"The {pool} sprint has started.",
                 data={"sprint": s["id"], "pool": pool})
        end = to_epoch(s.get("end_at"))
        if s["status"] == "active" and end is not None and end <= now:
            store.update_sprint(s["id"], status="done")
            s["status"] = "done"
            continue
        current = True  # this sprint's window is open now
    if waiting and not current:
        return False
    hold = pool_hold(store, pool)
    if hold and float(hold.get("until") or 0) > now:
        _note_hold(store, run, pool, f"{hold.get('reason') or 'it failed for a temporary reason'}"
                   f" (until {local_hhmm(hold['until'])})", now)
        return False
    state, info = usage_gate(pool, usage_by_pool, reserve_percent(settings), now)
    if state == "hold":
        _note_hold(store, run, pool, info["reason"], now)
        return False
    if state == "low":
        resets = info.get("resets_at")
        if resets is not None and (settings.get("auto_resume") or settings.get("merge_resets")):
            start = sprint_start_for_reset(resets)
            # One resume sprint per reset, even when several runs share the punch clock.
            if not any(abs((to_epoch(s["start_at"]) or 0) - start) < RESET_GRACE_SECONDS for s in mine):
                create_sprint(store, _clock_run(settings) or run["id"], pool, start, project_id=project_id,
                              reason=(f"{pool} is used up to its reserve, so its work pauses and "
                                      f"resumes at {local_hhmm(start)}, one minute after it resets."))
        else:
            _note_hold(store, run, pool, info["reason"], now)
        return False
    had_any = any(s["pool"] == pool for s in sprints)
    resets = info.get("resets_at")
    if settings.get("align_to_reset") and not had_any and resets is not None and resets > now:
        start = sprint_start_for_reset(resets)
        create_sprint(store, run["id"], pool, start, project_id=project_id,
                      reason=(f"The {pool} sprint is aligned to its reset and starts at "
                              f"{local_hhmm(start)}."))
        return False
    _clear_hold_note(store, run, pool)
    return True


def cancel_run(store, run_id, dispatcher=None, actor="owner", now=None):
    """Cancel ``run_id``: mark it cancelled, cancel its sprints and kill its live jobs.

    Running nodes of the run's project go back to not_started (only when no other run
    of that project is still active). Returns the ids of the nodes whose jobs stopped.
    """
    now = time.time() if now is None else to_epoch(now)
    run = store.get_run(run_id)
    if run is None:
        raise KeyError(run_id)
    if run["status"] not in ("cancelled", "done"):
        store.update_run(run_id, status="cancelled", ended_at=iso(now))
    for s in sprints_for(store, run_id):
        if s["status"] not in ("done", "cancelled"):
            store.update_sprint(s["id"], status="cancelled")
    return _stop_run_jobs(store, dict(run, status="cancelled"), dispatcher, now, actor)


def _stop_run_jobs(store, run, dispatcher, now, actor=ACTOR):
    key = f"jobs_stopped:{run['id']}"
    if meta_get(store, key):
        return []
    pid = run["project_id"]
    if any(r["id"] != run["id"] for r in active_runs(store, pid)):
        meta_set(store, key, "1")
        return []  # another active run of this project owns the running nodes
    if dispatcher is None:
        from .dispatch import Dispatcher
        dispatcher = Dispatcher()
    stopped = []
    for n in list_nodes(store, pid):
        if n["status"] != "running":
            continue
        dispatcher.kill_job(pid, n["id"], reason="run cancelled", now=now)
        set_status(store, pid, n["id"], "not_started",
                   f"Task {n['id']} was stopped because the run was cancelled.", actor=actor)
        stopped.append(n["id"])
    meta_set(store, key, "1")
    return stopped


# ---------------------------------------------------------------- checks

def _held_for_owner(store, project_id, node_id):
    """True when ``checks.complete_node`` passed the check but parked the node for an owner action."""
    node = store.get_node(project_id, node_id)
    return bool(node) and node.get("status") == "needs_you"


def default_checker(store, project_id, node, project=None):
    """``checks.complete_node``: sets done (actor ``check:<type>``) or error/needs_you."""
    from . import checks
    res = checks.complete_node(store, project_id, node["id"])
    return bool(res.ok), res.detail


# ---------------------------------------------------------------- tick

def _graph_order(nodes):
    from . import graph
    return graph.ready_nodes(nodes)


def _candidate_pools(node, settings, pools):
    """A pinned node uses only its pool; an unpinned one may use any ticked pool."""
    if node.get("pool"):
        return [node["pool"]]
    first = [settings["default_pool"]] if settings.get("default_pool") in pools else []
    return first + [p for p in pools if p not in first]


def _initial_model(node, pool, settings):
    ladder = (settings.get("model_ladder") or {}).get(pool) or []
    model = node.get("model") or (ladder[0] if ladder else None)
    if pool == "freebuff" and ladder and model not in ladder:
        model = ladder[0]  # an old id the proxy does not serve
    effort = node.get("effort")
    if not effort:
        e = settings.get("effort")
        effort = e.get(pool) if isinstance(e, dict) else e
    return model, effort


def _reconcile(store, run, project, settings, dispatcher, checker, asker, now, result,
               usage_by_pool):
    from . import failure
    pid = run["project_id"]
    denied = {}  # node id -> "Claude was not allowed to edit files: ..." from this tick's exit
    for n in list_nodes(store, pid):
        if n["status"] == "running":
            p = dispatcher.poll(pid, n["id"], now=now)
            state = p.get("state")
            if state == "exited":
                kind, reason = failure.classify_exit(p)
                if kind == "transient":
                    mark_stopped(p.get("job"), reason)
                    out = failure.on_transient(
                        store, pid, n, settings, reason, now,
                        pool_resets_at=pool_resets_at(n.get("pool"), usage_by_pool, now),
                        asker=asker)
                    result["transient"].append({"id": n["id"], "reason": reason,
                                                "action": out["action"]})
                    continue
                failure.clear_transients(store, pid, n["id"])
                line = failure.permission_denials(dict(p, route=p.get("route") or n.get("pool")))
                if line:
                    denied[n["id"]] = line
                set_status(store, pid, n["id"], "submitted",
                           f"Task {n['id']} finished its work and is waiting for its check.",
                           evidence=f"attempt {n['attempts']}: "
                                    f"{p.get('output_path') or p.get('job')}")
                n["status"] = "submitted"
            elif state == "timeout":
                mark_stopped(p.get("job"), p.get("reason") or "it ran too long")
                out = failure.on_transient(
                    store, pid, n, settings, p.get("reason") or "it ran too long", now,
                    pool_resets_at=pool_resets_at(n.get("pool"), usage_by_pool, now), asker=asker)
                result["transient"].append({"id": n["id"], "reason": "timeout",
                                            "action": out["action"]})
                continue
            elif state in ("lost", "cancelled"):
                mark_stopped(p.get("job"), "it was interrupted by a restart")
                if settings.get("auto_resume", True):
                    failure.on_transient(store, pid, n, settings,
                                         "it was interrupted by a restart", now,
                                         delay=0, hold_pool=False, asker=asker)
                    result["resumed"].append(n["id"])
                else:
                    set_status(store, pid, n["id"], "error",
                               f"Task {n['id']} was interrupted by a restart and auto-resume is off.")
                continue
            else:
                continue
        if n["status"] == "submitted":
            check = n.get("check") or {}
            if check.get("type") == "human":
                set_status(store, pid, n["id"], "needs_you",
                           f"Task {n['id']} is waiting for you to confirm it is done.")
                if asker is not None:
                    asker(project_id=pid, node_id=n["id"], kind="text", purpose="confirm",
                          prompt=(f"Task {n['id']} is finished. Please confirm it is done: "
                                  f"{check.get('prompt') or n.get('title') or n['id']}"),
                          options=None)
                continue
            ok, detail = checker(store, pid, n, project)
            if ok:  # checks.complete_node has set done with a truthful check actor
                result["done"].append(n["id"])
            elif _held_for_owner(store, pid, n["id"]):
                # W5: the check passed but the risk lane needs Owner; that is not a failure to retry
                result["needs_you"].append(n["id"])
                if asker is not None:
                    asker(project_id=pid, node_id=n["id"], kind="text", purpose="confirm",
                          prompt=f"Task {n['id']} passed its check and needs your approval: {detail}",
                          options=None)
            else:
                from . import loops  # W3/W4: return packet, then escalate or re-plan
                # LIVE2: a refused edit is the cause; the failed check is only its symptom
                out = loops.on_failed_check(store, pid, get_node(store, pid, n["id"]), settings,
                                            reason=denied.get(n["id"]) or detail, evidence=detail,
                                            asker=asker)
                result["failed"].append({"id": n["id"], "action": out["action"]})


def tick(store, now=None, dispatcher=None, project_id=None, usage_reader=None, checker=None,
         asker=None, power_runner=None, holder=None):
    """Advance every active run once. Idempotent; returns a summary dict."""
    now = time.time() if now is None else to_epoch(now)
    if dispatcher is None:
        from .dispatch import Dispatcher
        dispatcher = Dispatcher()
    if hasattr(dispatcher, "toggles_db"):
        dispatcher.toggles_db = store  # dispatch reads the same Agents switches as this tick
    usage_reader = usage_reader or default_usage_reader
    checker = checker or default_checker
    result = {"lease": None, "started": [], "needs_you": [], "resumed": [], "done": [],
              "failed": [], "transient": [], "finished_runs": [], "blocked": [], "stopped": [],
              "power_off": [], "parked": [], "receipts": []}
    token = acquire_lease(store, now, holder)
    if token is None:
        result["lease"] = "held"
        return result
    result["lease"] = "acquired"
    try:
        for run in store.list_runs(limit=10000):
            if run["status"] == "cancelled" and (not project_id or run["project_id"] == project_id):
                result["stopped"] += _stop_run_jobs(store, run, dispatcher, now)
        runs = active_runs(store, project_id)
        usage_rows = usage_reader() if runs else []
        usage_by_pool = {u.get("pool"): u for u in usage_rows or []}
        for run in runs:
            _tick_run(store, run, now, dispatcher, usage_by_pool, checker, asker,
                      power_runner, result)
        _retry_power_off(store, dispatcher, power_runner, result)
        try:  # needs-you tasks become human to-dos instead of blocking the night
            from . import overnight
            result["needs_you_todos"] = overnight.sync_needs_you_todos(store, active_runs(store, project_id))
        except Exception as exc:  # never let to-do bookkeeping break a tick
            result["needs_you_todos"] = f"error: {exc}"
    finally:
        release_lease(store, token)
    try:  # to-dos: auto-tick items whose task is done, archive items 24 h after first click
        from . import todo
        result["todos_changed"] = todo.housekeep(store)
    except Exception:  # never let to-do housekeeping break a tick
        pass
    return result


def _holds_back(store, run):
    """An active run holds back the Autonomous rows below it unless it is parked on Owner."""
    from . import overnight
    return run.get("status") in ACTIVE_RUN_STATUSES and not overnight.run_parked(store, run)


def _power_pending_key(run_id):
    return f"power_pending:{run_id}"


def _retry_power_off(store, dispatcher, power_runner, result):
    """A finished run whose power-off waited for other work tries again each tick."""
    from . import power
    for run in store.list_runs(limit=10000):
        if run["status"] != "done" or not meta_get(store, _power_pending_key(run["id"])):
            continue
        out = power.power_off_if_done(store, run, power_runner, dispatcher=dispatcher)
        if out or meta_get(store, power._marker(run["id"])):
            meta_set(store, _power_pending_key(run["id"]), None)
        if out:
            result["power_off"].append(run["id"])


def _tick_run(store, run, now, dispatcher, usage_by_pool, checker, asker, power_runner, result):
    pid = run["project_id"]
    project = dict(store.get_project(pid) or {"id": pid})
    project.setdefault("id", pid)
    from . import toggles as T
    snap = T.snapshot(store)  # the Agents switches, read once per run per tick
    try:
        settings = expand_settings(run_settings(run), toggles=snap)
    except ValueError as exc:
        if meta_get(store, f"bad_settings:{run['id']}") is None:
            meta_set(store, f"bad_settings:{run['id']}", str(exc))
            emit(store, pid, None, "run",
                 f"The run cannot start because its settings are invalid ({exc}).",
                 data={"run": run["id"]})
        result["blocked"].append({"run": run["id"], "reason": f"invalid settings: {exc}"})
        return
    from . import overnight
    waiting_on = [r for r in settings.get("after_runs") or []
                  if _holds_back(store, store.get_run(r) or {})]
    if waiting_on and run["status"] in ("queued", "scheduled"):
        key = f"row_wait:{run['id']}"
        if not meta_get(store, key):
            meta_set(store, key, "1")
            emit(store, pid, None, "run",
                 "This project waits for the projects above it in Autonomous to finish first.",
                 data={"run": run["id"], "after_runs": waiting_on})
        result["blocked"].append({"run": run["id"], "reason": "waiting for the projects above it"})
        return
    runnable = project_runnable(store, pid)
    if run["status"] in ("queued", "scheduled") and runnable:
        store.update_run(run["id"], status="running", started_at=iso(now))
        emit(store, pid, None, "run", "The run has started.", data={"run": run["id"]})
    _seed_sprints(store, run, settings)

    _reconcile(store, run, project, settings, dispatcher, checker, asker, now, result,
               usage_by_pool)

    nodes = list_nodes(store, pid)
    if nodes and all(n["status"] in CLOSED_STATUSES for n in nodes):
        store.update_run(run["id"], status="done", ended_at=iso(now))
        emit(store, pid, None, "run", "Every task is done, so the run is finished.",
             data={"run": run["id"]})
        result["finished_runs"].append(run["id"])
        result["receipts"].append(str(overnight.write_receipt(store, run, "finished", now)))
        from . import power
        if settings.get("power_off"):
            if power.power_off_if_done(store, dict(run, status="done"), power_runner,
                                       settings=settings, dispatcher=dispatcher):
                result["power_off"].append(run["id"])
            elif not meta_get(store, power._marker(run["id"])):
                meta_set(store, _power_pending_key(run["id"]), "1")
        return

    if not runnable:
        result["blocked"].append({"run": run["id"], "reason": "project is a draft"})
        return

    from . import failure
    from .dispatch import freebuff_eligible
    pools = ticked_pools(settings)
    running = sum(1 for n in nodes if n["status"] == "running")
    slots = max(0, int(settings.get("max_concurrency") or 1) - running)
    open_cache, down_cache = {}, {}
    for n in _graph_order(nodes):
        wait = failure.retry_after(store, pid, n["id"])
        if wait is not None and wait > now:
            result["blocked"].append({"id": n["id"], "reason": "waiting to retry"})
            continue
        candidates = _candidate_pools(n, settings, pools)
        off = [c for c in candidates if pool_refusal(c, snap)]
        if off:
            candidates = [c for c in candidates if c not in off]
            if not candidates:
                result["blocked"].append({"id": n["id"], "reason": pool_refusal(off[0], snap)})
                continue
        if not candidates or any(c not in pools for c in candidates):
            result["blocked"].append({"id": n["id"], "reason": "pool not ticked"})
            continue
        if "freebuff" in candidates and not freebuff_eligible(n, settings):
            candidates = [c for c in candidates if c != "freebuff"]
            if not candidates:
                result["blocked"].append({"id": n["id"], "reason": "freebuff takes text-only tasks"})
                continue
        pool = None
        down = None
        for c in candidates:
            if c not in down_cache:  # not installed / signed out / no credits: try the next pool
                probe = getattr(dispatcher, "unavailable", None)
                down_cache[c] = probe(route_for_pool(c)) if callable(probe) else None
            if down_cache[c]:
                down = down or down_cache[c]
                continue
            if c not in open_cache:
                open_cache[c] = _pool_open(store, run, c, settings, usage_by_pool, now)
            if open_cache[c]:
                pool = c
                break
        if pool is None:
            result["blocked"].append({"id": n["id"], "reason": down or "pool waiting for sprint or usage"})
            continue
        automated = dispatcher.route(dict(n, pool=pool), settings) in AUTOMATED_ROUTES
        if automated and slots <= 0:  # Gemini App / manual packets never use a worker slot
            result["blocked"].append({"id": n["id"], "reason": "max concurrency reached"})
            continue
        model, effort = _initial_model(n, pool, settings)
        model, refused = _allowed_model(dispatcher, n, pool, model, settings, snap)
        if refused:
            result["blocked"].append({"id": n["id"], "reason": refused})
            _note_hold(store, run, pool, refused, now)
            continue
        fields = {"attempts": n["attempts"] + 1}
        note_auto_pick(store, pid, n, pool, model)
        for k, v in (("pool", pool), ("model", model), ("effort", effort)):
            if v != n.get(k):
                fields[k] = v
        update_node_fields(store, pid, n["id"], fields, ACTOR,
                           f"Task {n['id']} is assigned to {pool}"
                           + (f" using {model}" if model else "") + ".")
        n.update(fields)
        out = dispatcher.dispatch(n, settings, project)
        if out["status"] == "refused":
            result["blocked"].append({"id": n["id"], "reason": out.get("reason") or out["summary"]})
            continue
        if out["status"] == "needs_you":
            set_status(store, pid, n["id"], "needs_you", out["summary"], evidence=out.get("packet"))
            result["needs_you"].append(n["id"])
        else:
            set_status(store, pid, n["id"], "running", out["summary"],
                       evidence=f"attempt {n['attempts']}: {out.get('job')}")
            result["started"].append(n["id"])
            slots -= 1
    if overnight.note_parked(store, run, now):
        result["parked"].append(run["id"])


def _allowed_model(dispatcher, node, pool, model, settings, toggles=None):
    """``(model, None)`` for the first model Agents/Models allows, from ``model`` up the ladder.

    ``(model, reason)`` when every candidate is switched off. A pinned model is the task's own
    choice, so only it is tried. The Agents switches (``toggles``) always apply; a dispatcher
    without ``refusal`` (an old fake) adds nothing more.
    """
    from . import toggles as T
    snap = T.resolve(toggles)
    check = getattr(dispatcher, "refusal", None)
    route = route_for_pool(pool)
    ladder = list((settings.get("model_ladder") or {}).get(pool) or [])
    tries = [model] if node.get("model") else [model] + [m for m in ladder if m != model]
    first = None
    for m in tries:
        reason = snap.refusal(route, m)
        if not reason and callable(check):
            reason = check(dict(node, pool=pool, model=m), settings)
        if not reason:
            return m, None
        first = first or reason
    if len(tries) > 1:
        return model, f"every {pool} model on this run is turned off in Models"
    return model, first


# ---------------------------------------------------------------- CLI

def make_asker(store):
    """``asker(**kw)`` for dispatch/failure, backed by ``needs.ask``; None if absent."""
    try:
        from . import needs
    except ImportError:
        return None

    def asker(project_id, node_id, kind, prompt, options=None, purpose=None, **kw):
        # purpose: manual_result (Copy Prompt packets), failure_choice (reports), confirm
        # (human done-checks). Only "preference" answers reach the preference registry.
        return needs.ask(store, project_id, node_id, kind, prompt, options=options,
                         purpose=purpose, **kw)

    return asker


def main(argv=None):
    """``python -m cc_engine tick [--db PATH] [--data-dir DIR] [--project ID]``."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "tick":
        argv = argv[1:]
    ap = argparse.ArgumentParser(prog="python -m cc_engine tick")
    ap.add_argument("--db", default=None, help="default: $CC_DB or <repo>/data/cc.sqlite")
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--project", default=None)
    args = ap.parse_args(argv)
    from . import default_db_path
    from .store import Store
    from .dispatch import Dispatcher
    db = args.db or str(default_db_path())
    store = Store(db)
    try:
        data_dir = args.data_dir or str(Path(db).resolve().parent)
        # usage.read_usage reads <data dir>/usage_cache.json: the one beside this database, not
        # beside this copy of the code (a worktree tick on the live database must see live usage).
        os.environ.setdefault("CC_DATA_DIR", data_dir)
        asker = make_asker(store)
        dispatcher = Dispatcher(data_dir=data_dir, asker=asker)
        result = tick(store, time.time(), dispatcher, project_id=args.project, asker=asker)
    finally:
        store.close()
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
