"""Autonomous tab routes (the page id stays ``cc-sprints``): the one place runs start, plus a
48-hour punch clock over the existing runs/sprints tables.

Extension module (CONTRACT.md "Extension routes"): api.py loads ``ROUTES`` lazily.

    GET  /api/sprints/timeline?hours=48&project=   pools + reset markers + sprint blocks + runs
    POST /api/sprints/plan       {project, pool, start_at?, duration_min?, preset?}   (owner)
    POST /api/sprints/update     {id, start_at, duration_min?}                        (owner)
    POST /api/sprints/cancel     {id}                                                 (owner)
    POST /api/sprints/start-now  {id}                                                 (owner)

    GET  /api/autonomous            saved run controls, enabled models, Codex + Claude usage, live runs
    POST /api/autonomous/settings   {settings}                       save the run controls (owner)
    POST /api/autonomous/run        {settings?}                      start the project sequence (owner)
    POST /api/autonomous/cancel     {}                               stop every Autonomous run (owner)
    POST /api/autonomous/sprint     {kind: claude|codex|dual|auto_dual, confirm?}          (owner)
    POST /api/autonomous/prompt     {settings?}  -> {prompt}         Copy Prompt (orchestrator brief)

Autonomous (Owner, 4 Nov 2026: "the router shouldn't be able to start runs automatically"):

* Run controls are saved in ``meta`` (``autonomous:settings``): ticked models (from
  ``GET /api/models`` when ``cc_engine.models`` exists, else the current pools), Claude and
  Codex effort, punch clock, max use per pool, auto-wake on reset, merge resets, power off when
  done, and the project sequence: priority rows, a model per project. Owner only; the Router
  token gets 403 on every Autonomous write.
* Run creates one run per project. Projects in a row start together; each run lists the runs
  of every row above in ``after_runs`` and the scheduler holds it until they finish.
* The punch clock is one shared anchor run (``ANCHOR_RUN``, status ``manual``, never ticked)
  whose sprint rows every Autonomous run follows (``settings.sprint_run``). Sprint adders take
  the next free slot per provider: now until the reset, then reset + 1 min for each 5-hour
  window. Dual starts Claude and Codex together; Auto Dual runs them one after the other.
* A Codex sprint needs ``confirm: true`` (it draws on Owner's Codex plan) and Codex switched on
  in Agents (``cc_engine.toggles``; off by default). Its window gates the ``codex`` pool exactly
  as a Claude sprint gates ``claude_code``: ticked Codex work dispatches through ``codex exec``.
* Anything switched off in Agents is hidden from the ticks and refused by every write here.

Design (no scheduler.py changes):

* Planning a sprint creates one dedicated run (``settings.planned_by = "sprints"``, status
  ``scheduled``) ticking only that pool, with one sprint row. The scheduler already holds a pool
  until its sprint's ``start_at`` (edited times win) and starts the run's ready nodes after it.
* The default start is the pool's next known reset + 60 s (``scheduler.sprint_start_for_reset``),
  otherwise now. ``end_at`` is the planned window (default per pool); the scheduler marks the
  sprint done once it passes. It never stops work already running.
* Actual execution time comes from events: the scheduler's "sprint has started" event, then the
  first and last status events of that pool's tasks inside the sprint window.
* Cancel on a sprint-owned run cancels the run (the next tick stops its jobs, as for
  ``/api/run/cancel``). On another run's sprint it cancels only the sprint.
* Start now moves the start to now and kicks one lease-protected ``python -m cc_engine tick``
  (live database only; ``app.tick_kicker`` overrides it, tests inject a fake).

Hooks written here because scheduler.py has none (note for its owner): ``next_reset`` (soonest
reset per pool; ``scheduler.pool_resets_at`` returns the latest), ``kick_tick``, and the event
reading for actual start/end. ``next_wake_at`` in the timeline is the soonest planned start, for a
Task Scheduler one-shot wake trigger if Register-Tick ever adds one.
"""
from __future__ import annotations

import json
import secrets
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import scheduler as S

ANCHOR_RUN = "autonomous-clock"

OWNER = "owner"
ACTOR = "owner"

# Rows on the timeline. ``usage`` names the read_usage pools whose resets mark the row.
POOL_ROWS = (
    {"id": "claude_code", "label": "Claude Code", "kind": "dispatch", "usage": ("claude_5h", "claude_week")},
    {"id": "freebuff", "label": "Freebuff", "kind": "dispatch", "usage": ()},
    {"id": "codex", "label": "Codex", "kind": "manual", "usage": ("codex_5h", "codex_week")},
)
PLANNABLE = tuple(r["id"] for r in POOL_ROWS if r["kind"] == "dispatch")
LABEL = {r["id"]: r["label"] for r in POOL_ROWS}
# Planned window when no duration is given: Claude's 5 h window.
DEFAULT_MINUTES = {"claude_code": 300, "freebuff": 240}
MAX_MINUTES = 24 * 60
MAX_AHEAD_SECONDS = 7 * 86400
PAST_SLACK_SECONDS = 300
DEFAULT_HOURS = 48
LOOKBACK_SECONDS = 24 * 3600
LIVE_SPRINT = ("scheduled", "active")
ENDED_RUN = ("done", "cancelled")
EVENT_SCAN = 5000


# ---------------------------------------------------------------- small helpers

def _http(status, body):
    from .api import HttpError  # lazy: api imports this module
    return HttpError(status, body)


def _owner_only(h, what):
    if getattr(h, "actor", None) != OWNER:
        raise _http(403, {"error": "owner_only", "detail": f"only Owner may {what}"})


def _now(h):
    clock = getattr(h.app, "clock", None)
    return float(clock()) if callable(clock) else time.time()


def _usage(h):
    reader = getattr(h.app, "usage_reader", None)
    try:
        rows = reader() if callable(reader) else []
    except Exception:
        rows = []
    return [r for r in rows or [] if isinstance(r, dict)]


def _epoch_or_400(value, field):
    if isinstance(value, bool):
        raise _http(400, {"error": "bad_request", "detail": f"{field} must be an ISO time or epoch seconds"})
    try:
        t = S.to_epoch(value)
    except (TypeError, ValueError):
        t = None
    if t is None:
        raise _http(400, {"error": "bad_request", "detail": f"{field} must be an ISO time or epoch seconds"})
    return t


def _minutes_or_400(value, pool):
    if value is None:
        return DEFAULT_MINUTES.get(pool, 240)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= value <= MAX_MINUTES:
        raise _http(400, {"error": "bad_request", "detail": f"duration_min must be 1-{MAX_MINUTES}"})
    return int(value)


def _check_start(start, now):
    if start < now - PAST_SLACK_SECONDS:
        raise _http(400, {"error": "bad_request", "detail": "start_at is in the past"})
    if start > now + MAX_AHEAD_SECONDS:
        raise _http(400, {"error": "bad_request", "detail": "start_at is more than 7 days ahead"})


def _hm(epoch):
    return S.local_hhmm(epoch)


def _span(seconds):
    m = max(0, int(round(seconds / 60.0)))
    return f"{m // 60} h {m % 60:02d} m" if m >= 60 else f"{m} m"


def _row(pool):
    return next((r for r in POOL_ROWS if r["id"] == pool), None)


def _usage_rows_for(pool, rows):
    names = (_row(pool) or {}).get("usage") or ()
    return [r for r in rows if r.get("pool") in names]


def next_reset(pool, rows, now):
    """Soonest future reset among the usage rows behind ``pool``: ``(epoch, usage_row)`` or None."""
    best = None
    for r in _usage_rows_for(pool, rows):
        try:
            t = S.to_epoch(r.get("resets_at"))
        except ValueError:
            t = None
        if t is not None and t > now and (best is None or t < best[0]):
            best = (t, r)
    return best


def _settings(run):
    try:
        return S.run_settings(run)
    except ValueError:
        return {}


def _owned(run):
    return bool(run) and _settings(run).get("planned_by") == "sprints"


def _project_title(store, pid, cache):
    if not pid:
        return "Autonomous"
    if pid not in cache:
        cache[pid] = (store.get_project(pid) or {}).get("title") or pid
    return cache[pid]


# ---------------------------------------------------------------- actual execution

def _events_by_project(store, pids):
    out = {}
    for pid in pids:
        out[pid] = store.events(project_id=pid, latest=EVENT_SCAN) if pid else []
    return out


def _actual(sprint, run, events, nodes_pool, now, next_start):
    """Actual start/end and task counts for one sprint, from its project's events.

    ``nodes_pool`` maps node id (or ``(project, node)`` for the shared clock) to its pool."""
    sid = sprint["id"]
    started_ev = next((e for e in events if e.get("kind") == "sprint"
                       and (e.get("data") or {}).get("sprint") == sid
                       and "has started" in (e.get("summary") or "")), None)
    planned = S.to_epoch(sprint.get("start_at"))
    win_lo = S.to_epoch(started_ev["at"]) if started_ev else planned
    run_end = S.to_epoch(run.get("ended_at")) if run and run.get("ended_at") else None
    caps = [t for t in (run_end, next_start, now) if t is not None]
    win_hi = min(caps) if caps else now
    started, finished, first, last = set(), set(), None, None
    for e in events:
        if e.get("kind") != "status" or not e.get("node_id"):
            continue
        pool = nodes_pool.get((e.get("project_id"), e["node_id"]), nodes_pool.get(e["node_id"]))
        if pool != sprint["pool"]:
            continue
        t = S.to_epoch(e.get("at"))
        if t is None or t < win_lo - 1 or t > win_hi + 1:
            continue
        st = (e.get("data") or {}).get("status")
        if st == "running":
            started.add(e["node_id"])
            first = t if first is None else min(first, t)
        if st in ("submitted", "done", "error", "needs_you", "running"):
            last = t if last is None else max(last, t)
        if st == "done":
            finished.add(e["node_id"])
    actual_start = S.to_epoch(started_ev["at"]) if started_ev else first
    return {"actual_start": actual_start, "first_task_at": first, "last_activity_at": last,
            "tasks_started": len(started), "tasks_done": len(finished & started) if started else len(finished)}


def _anchor_state(sprint, now):
    status = sprint.get("status")
    if status == "cancelled":
        return "cancelled"
    end = S.to_epoch(sprint.get("end_at")) if sprint.get("end_at") else None
    if status == "done" or (end is not None and end <= now):
        return "finished"
    if status == "active":
        return "active"
    start = S.to_epoch(sprint.get("start_at"))
    return "due" if start is not None and start <= now else "planned"


def _state(sprint, run, now):
    if (run or {}).get("id") == ANCHOR_RUN:
        return _anchor_state(sprint, now)
    status = sprint.get("status")
    run_status = (run or {}).get("status")
    if status == "cancelled" or (run_status == "cancelled" and status in LIVE_SPRINT):
        return "cancelled"
    if status == "done" or run_status == "done":
        return "finished"
    end = S.to_epoch(sprint.get("end_at")) if sprint.get("end_at") else None
    if end is not None and end <= now:
        return "finished"  # the window has passed; the scheduler flips the row on its next look
    start = S.to_epoch(sprint.get("start_at"))
    if status == "active" or (start is not None and start <= now and status == "scheduled"
                              and run_status in S.ACTIVE_RUN_STATUSES):
        # A scheduled sprint whose time has come starts on the next tick; show it as due.
        return "active" if status == "active" else "due"
    return "planned"


def _anchor_summary(s):
    label = LABEL.get(s["pool"], s["pool"])
    codex = s["pool"] == "codex"
    end = S.to_epoch(s["end_at"]) if s.get("end_at") else None
    until = f" until {_hm(end)}" if end else ""
    if s["state"] == "planned" and codex:
        return (f"The Codex sprint starts at {_hm(s['start_epoch'])}; Codex runs ticked work then "
                "while it is switched on in Agents.")
    if s["state"] in ("due", "active") and codex and not s.get("live_runs"):
        return f"The Codex sprint is open{until}; press Run to use it."
    if s["state"] == "due" and not s.get("live_runs"):
        return f"The {label} sprint is open{until}; press Run to use it."
    return None


def _summary(s):
    if s.get("anchor"):
        text = _anchor_summary(s)
        if text:
            return text
        who = f"{LABEL.get(s['pool'], s['pool'])} sprint"
    else:
        who = f"{LABEL.get(s['pool'], s['pool'])} sprint for {s['project_title']}"
    if s["state"] == "planned":
        tail = ", one minute after the reset" if s.get("aligned") else ""
        return f"The {who} starts at {_hm(s['start_epoch'])}{tail}."
    if s["state"] == "due":
        return f"The {who} is due now and starts on the next scheduler tick."
    if s["state"] == "active":
        since = s.get("actual_start_epoch") or s["start_epoch"]
        n = s["tasks_started"]
        return (f"The {who} has been running since {_hm(since)}; "
                f"{n} task{'s' if n != 1 else ''} started so far.")
    if s["state"] == "cancelled":
        return f"The {who} planned for {_hm(s['start_epoch'])} was cancelled."
    a, b = s.get("actual_start_epoch"), s.get("actual_end_epoch")
    if a is None or s["tasks_started"] == 0:
        return f"The {who} closed at {_hm(b or s['start_epoch'])} without starting any task."
    return (f"The {who} ran {_hm(a)}–{_hm(b)} ({_span(b - a)}) and finished "
            f"{s['tasks_done']} of the {s['tasks_started']} task{'s' if s['tasks_started'] != 1 else ''} it started.")


def describe_sprints(store, now, project_id=None, window=(None, None)):
    """Every sprint (newest runs first) with derived state, actual times and one summary sentence."""
    lo, hi = window
    runs = {r["id"]: r for r in store.list_runs(limit=10000)}
    rows = []
    for run in runs.values():
        if project_id and run["project_id"] != project_id:
            continue
        for sp in run.get("sprints") or []:
            rows.append((sp, run))
    followers = [r for r in runs.values() if _settings(r).get("sprint_run") == ANCHOR_RUN]
    live_runs = any(r["status"] in S.ACTIVE_RUN_STATUSES for r in followers)
    clock_pids = sorted({r["project_id"] for r in followers if r.get("project_id")})
    pids = sorted({run["project_id"] for _, run in rows if run.get("project_id")} | set(clock_pids))
    events = _events_by_project(store, pids)
    node_pools = {pid: {n["id"]: n.get("pool") for n in store.list_nodes(pid)} for pid in pids}
    clock_events = sorted((e for pid in clock_pids for e in events.get(pid, [])), key=lambda e: e.get("seq") or 0)
    clock_pools = {(pid, nid): pool for pid in clock_pids for nid, pool in node_pools.get(pid, {}).items()}
    titles = {}
    out = []
    for sp, run in rows:
        start = S.to_epoch(sp.get("start_at"))
        if start is None:
            continue
        end = S.to_epoch(sp.get("end_at")) if sp.get("end_at") else None
        later = [S.to_epoch(o["start_at"]) for o, r in rows
                 if r["id"] == run["id"] and o["pool"] == sp["pool"] and o["id"] != sp["id"]
                 and o.get("status") != "cancelled" and S.to_epoch(o["start_at"]) > start]
        next_start = min(later) if later else None
        pid = run["project_id"]
        anchor = run["id"] == ANCHOR_RUN
        state = _state(sp, run, now)
        if anchor:
            act = _actual(sp, run, clock_events, clock_pools, now, next_start)
        else:
            act = _actual(sp, run, events.get(pid, []), node_pools.get(pid, {}), now, next_start)
        actual_end = None
        if state == "finished":
            # Last task activity inside the window; with none, when the run or the window closed.
            run_end = S.to_epoch(run["ended_at"]) if run.get("ended_at") else None
            actual_end = act["last_activity_at"] or run_end or end or start
        settings = _settings(run)
        aligned = any(isinstance(x, dict) and x.get("pool") == sp["pool"] and x.get("aligned")
                      for x in settings.get("sprints") or [])
        extra = _sprint_meta(store, sp["id"]) if anchor else {}
        aligned = bool(extra.get("aligned")) if anchor else aligned
        item = {
            "id": sp["id"], "run_id": run["id"], "project_id": pid,
            "project_title": _project_title(store, pid, titles), "pool": sp["pool"],
            "pool_label": LABEL.get(sp["pool"], sp["pool"]),
            "start_at": S.iso(start), "end_at": S.iso(end) if end else None,
            "status": sp.get("status"), "state": state, "run_status": run.get("status"),
            "owned": _owned(run), "aligned": aligned,
            "editable": state in ("planned", "due", "active"),
            "actual_start": S.iso(act["actual_start"]) if act["actual_start"] else None,
            "actual_end": S.iso(actual_end) if actual_end else None,
            "tasks_started": act["tasks_started"], "tasks_done": act["tasks_done"],
            "start_epoch": start, "actual_start_epoch": act["actual_start"], "actual_end_epoch": actual_end,
            "anchor": anchor, "kind": extra.get("kind"), "handoff_after": extra.get("handoff_after"),
            "live_runs": live_runs if anchor else None,
        }
        item["summary"] = _summary(item)
        last_seen = max(t for t in (start, end, actual_end, act["last_activity_at"]) if t is not None)
        if state in ("planned", "due", "active") or ((lo is None or last_seen >= lo) and (hi is None or start <= hi)):
            out.append(item)
    out.sort(key=lambda s: (s["start_epoch"], s["id"]))
    for s in out:
        for k in ("start_epoch", "actual_start_epoch", "actual_end_epoch", "live_runs"):
            s.pop(k, None)
    return out


# ---------------------------------------------------------------- timeline

def timeline(store, rows, now, hours=DEFAULT_HOURS, project_id=None):
    lo, hi = now - LOOKBACK_SECONDS, now + hours * 3600
    pools = []
    for spec in POOL_ROWS:
        usage_rows = _usage_rows_for(spec["id"], rows)
        resets = []
        for r in usage_rows:
            try:
                t = S.to_epoch(r.get("resets_at"))
            except ValueError:
                t = None
            if t is not None and now < t <= hi:
                resets.append({"at": S.iso(t), "usage_pool": r.get("pool"),
                               "label": r.get("label") or r.get("pool"), "status": r.get("status") or "UNKNOWN"})
        resets.sort(key=lambda x: x["at"])
        nxt = next_reset(spec["id"], rows, now)
        pools.append({
            "id": spec["id"], "label": spec["label"], "kind": spec["kind"],
            "gate": "free" if not spec["usage"] else "usage",
            "usage": [{"pool": r.get("pool"), "label": r.get("label") or r.get("pool"),
                       "remaining_percent": r.get("remaining_percent"), "status": r.get("status") or "UNKNOWN",
                       "resets_at": r.get("resets_at"), "observed_at": r.get("observed_at")} for r in usage_rows],
            "resets": resets,
            "default_start": (S.iso(S.sprint_start_for_reset(nxt[0])) if nxt else S.iso(now))
            if spec["kind"] == "dispatch" else None,
            "default_aligned": bool(nxt) and spec["kind"] == "dispatch",
        })
    sprints = describe_sprints(store, now, project_id, (lo, hi))
    runs = []
    for r in store.list_runs(limit=200):
        if project_id and r["project_id"] != project_id:
            continue
        st = S.to_epoch(r.get("started_at")) if r.get("started_at") else None
        en = S.to_epoch(r.get("ended_at")) if r.get("ended_at") else None
        if r["status"] in S.ACTIVE_RUN_STATUSES or (en or st or 0) >= lo:
            pools_ticked = (_settings(r).get("pools") or [])
            if isinstance(pools_ticked, dict):
                pools_ticked = [k for k, v in pools_ticked.items() if v]
            runs.append({"id": r["id"], "project_id": r["project_id"], "status": r["status"],
                         "created_at": r.get("created_at"), "started_at": r.get("started_at"),
                         "ended_at": r.get("ended_at"), "pools": list(pools_ticked), "owned": _owned(r)})
    upcoming = [s for s in sprints if s["state"] in ("planned", "due")]
    wake = min((S.to_epoch(s["start_at"]) for s in upcoming), default=None)
    next_up = sorted([s for s in sprints if s["state"] in ("active", "due", "planned")],
                     key=lambda s: ({"active": 0, "due": 1, "planned": 2}[s["state"]], s["start_at"]))
    finished = sorted([s for s in sprints if s["state"] in ("finished", "cancelled")],
                      key=lambda s: s.get("actual_end") or s.get("end_at") or s["start_at"], reverse=True)
    return {
        "now": S.iso(now), "from": S.iso(now), "to": S.iso(hi), "lookback_from": S.iso(lo), "hours": hours,
        "pools": pools, "sprints": sprints, "runs": runs,
        "next_up": [s["id"] for s in next_up][:8], "recently_finished": [s["id"] for s in finished][:8],
        "next_wake_at": S.iso(wake) if wake is not None else None,
    }


def g_timeline(h, q):
    try:
        hours = int(q.get("hours") or DEFAULT_HOURS)
    except ValueError:
        raise _http(400, {"error": "bad_request", "detail": "hours must be an integer"})
    hours = max(6, min(hours, 7 * 24))
    return timeline(h.app.store, _usage(h), _now(h), hours, q.get("project") or None)


# ---------------------------------------------------------------- mutations

def plan_sprint(store, project_id, pool, now, rows, start_at=None, duration_min=None, preset="balanced",
                actor=ACTOR):
    """Create one dedicated sprint run; returns ``{run_id, sprint_id, start_at, end_at, aligned}``."""
    if store.get_project(project_id) is None:
        raise _http(404, {"error": "not_found", "detail": "no such project"})
    if pool not in PLANNABLE:
        raise _http(400, {"error": "bad_request", "detail": f"pool must be one of {list(PLANNABLE)}"})
    off = S.pool_refusal(pool, store)
    if off:
        raise _http(403, {"error": "disabled", "detail": off})
    from . import runsheet
    if preset not in runsheet.PRESETS:
        raise _http(400, {"error": "bad_request", "detail": f"preset must be one of {sorted(runsheet.PRESETS)}"})
    aligned = False
    if start_at in (None, "", "after_reset"):
        nxt = next_reset(pool, rows, now)
        start = S.sprint_start_for_reset(nxt[0]) if nxt else now
        aligned = bool(nxt)
    else:
        start = _epoch_or_400(start_at, "start_at")
    _check_start(start, now)
    minutes = _minutes_or_400(duration_min, pool)
    end = start + minutes * 60
    settings = {"preset": preset, "pools": [pool], "auto_resume": True, "merge_resets": True,
                "power_off": False, "planned_by": "sprints",
                "sprints": [{"pool": pool, "start_at": S.iso(start), "end_at": S.iso(end), "aligned": aligned}]}
    runsheet.expand(settings)  # same validation as /api/run (ValueError -> 400)
    run_id = "run-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(2)
    store.create_run(run_id, project_id, settings, status="scheduled")
    sid = S.create_sprint(store, run_id, pool, start, end, actor=actor, project_id=project_id,
                          reason=(f"Owner planned a {LABEL[pool]} sprint from {_hm(start)} to {_hm(end)}"
                                  + (", one minute after the reset." if aligned else ".")))
    store.set_meta(f"sprints_seeded:{run_id}", "1")  # the row exists; never re-seed from settings
    store.notify("run", {"id": run_id, "project": project_id, "status": "scheduled", "sprint": sid})
    return {"run_id": run_id, "sprint_id": sid, "start_at": S.iso(start), "end_at": S.iso(end),
            "aligned": aligned, "runnable": store.project_runnable(project_id)}


def _sprint_and_run(store, sid):
    sp = store.get_sprint(sid)
    if sp is None:
        raise _http(404, {"error": "not_found", "detail": "no such sprint"})
    return sp, store.get_run(sp["run_id"]) or {}


def _require_live(sp, run):
    if sp.get("status") not in LIVE_SPRINT or run.get("status") in ENDED_RUN:
        raise _http(409, {"error": "sprint_closed", "detail": "this sprint has already finished or been cancelled"})


def move_sprint(store, sid, start, now, duration_min=None, actor=ACTOR):
    sp, run = _sprint_and_run(store, sid)
    _require_live(sp, run)
    old_start = S.to_epoch(sp["start_at"])
    old_end = S.to_epoch(sp["end_at"]) if sp.get("end_at") else None
    if duration_min is not None:
        length = _minutes_or_400(duration_min, sp["pool"]) * 60
    else:
        length = (old_end - old_start) if old_end is not None else DEFAULT_MINUTES.get(sp["pool"], 240) * 60
    new = S.update_sprint(store, sid, start, actor=actor)  # status -> scheduled, one summary event
    store.update_sprint(sid, end_at=S.iso(start + length))
    store.notify("run", {"id": sp["run_id"], "sprint": sid, "start_at": new})
    return {"id": sid, "start_at": new, "end_at": S.iso(start + length)}


def p_plan(h, q):
    _owner_only(h, "plan a sprint")
    b = h._json_body()
    for k in ("project", "pool"):
        if not b.get(k):
            raise _http(400, {"error": "bad_request", "detail": f"missing {k}"})
    out = plan_sprint(h.app.store, b["project"], b["pool"], _now(h), _usage(h), b.get("start_at"),
                      b.get("duration_min"), b.get("preset") or "balanced")
    return dict(out, ok=True)


def p_update(h, q):
    _owner_only(h, "move a sprint")
    b = h._json_body()
    if not b.get("id") or b.get("start_at") in (None, ""):
        raise _http(400, {"error": "bad_request", "detail": "missing id or start_at"})
    now = _now(h)
    start = _epoch_or_400(b["start_at"], "start_at")
    _check_start(start, now)
    return dict(move_sprint(h.app.store, b["id"], start, now, b.get("duration_min")), ok=True)


def p_cancel(h, q):
    _owner_only(h, "cancel a sprint")
    b = h._json_body()
    if not b.get("id"):
        raise _http(400, {"error": "bad_request", "detail": "missing id"})
    store = h.app.store
    sp, run = _sprint_and_run(store, b["id"])
    _require_live(sp, run)
    label = LABEL.get(sp["pool"], sp["pool"])
    pid = run.get("project_id")
    if run.get("id") == ANCHOR_RUN:
        store.update_sprint(sp["id"], status="cancelled")
        store.event(None, None, "sprint", ACTOR, f"Owner removed the {label} sprint from the punch clock.",
                    {"sprint": sp["id"], "run": ANCHOR_RUN})
        store.notify("run", {"id": ANCHOR_RUN, "sprint": sp["id"], "status": "cancelled"})
        return {"ok": True, "id": sp["id"], "run_cancelled": False}
    if _owned(run):
        stamp = S.iso(_now(h))
        store.update_run(run["id"], status="cancelled", ended_at=stamp)
        for o in store.list_sprints(run["id"]):
            if o["status"] not in ("done", "cancelled"):
                store.update_sprint(o["id"], status="cancelled")
        store.event(pid, None, "sprint", ACTOR,
                    f"Owner cancelled the {label} sprint, so its run stops on the next tick.",
                    {"sprint": sp["id"], "run": run["id"]})
        store.notify("run", {"id": run["id"], "project": pid, "status": "cancelled"})
        return {"ok": True, "id": sp["id"], "run_cancelled": True}
    store.update_sprint(sp["id"], status="cancelled")
    store.event(pid, None, "sprint", ACTOR,
                f"Owner cancelled the {label} sprint; that pool now follows its run's usual rules.",
                {"sprint": sp["id"], "run": run.get("id")})
    store.notify("run", {"id": run.get("id"), "sprint": sp["id"], "status": "cancelled"})
    return {"ok": True, "id": sp["id"], "run_cancelled": False}


def kick_tick(store):
    """Start one detached ``python -m cc_engine tick`` for the live database (lease-protected).

    Returns True when a tick was launched. Never runs for a database other than the live one.
    """
    from . import default_db_path
    try:
        if Path(store.path).resolve() != Path(default_db_path()).resolve():
            return False
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen([sys.executable, "-m", "cc_engine", "tick", "--db", str(store.path)],
                         cwd=str(Path(__file__).resolve().parents[1]), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
        return True
    except Exception:
        return False


def p_start_now(h, q):
    _owner_only(h, "start a sprint")
    b = h._json_body()
    if not b.get("id"):
        raise _http(400, {"error": "bad_request", "detail": "missing id"})
    store = h.app.store
    now = _now(h)
    out = move_sprint(store, b["id"], now, now)
    kicker = getattr(h.app, "tick_kicker", None)
    kicked = bool(kicker(store) if callable(kicker) else kick_tick(store))
    return dict(out, ok=True, tick_started=kicked)


# ---------------------------------------------------------------- Autonomous

AUTONOMOUS_KEY = "autonomous:settings"
SPRINT_META_PREFIX = "autonomous:sprint:"
SLOT_SECONDS = 5 * 3600          # Claude and Codex both reset in 5-hour windows
MIN_NOW_SLOT_SECONDS = 300       # a "now" slot shorter than this is skipped
SLOT_COUNT = 12
STRIP_POOLS = ("claude_5h", "claude_week", "codex_5h", "codex_week")
CODEX_USAGE = ("codex_5h", "codex_week")
CLOCK_POOLS = {"claude_code": "claude_5h", "codex": "codex_5h"}
SPRINT_KINDS = ("claude", "codex", "dual", "auto_dual")
KIND_POOLS = {"claude": ("claude_code",), "codex": ("codex",), "dual": ("claude_code", "codex"),
              "auto_dual": ("claude_code", "codex")}
ROUTE_LABEL = {"claude_code": "Claude Code", "freebuff": "Freebuff",
               "gemini_app": "Gemini App", "chatgpt_web": "ChatGPT Web", "antigravity_manual": "Antigravity", "codex": "Codex"}

# The current pools, used when the model registry (cc_engine.models, GET /api/models) is absent.
FALLBACK_MODELS = (
    {"id": "claude-opus-5.5", "label": "Claude Opus 5.5", "provider": "anthropic", "kind": "subagent",
     "route": "claude_code", "enabled": True, "source": "fallback"},
    {"id": "claude-sonnet-5.5", "label": "Claude Sonnet 5.5", "provider": "anthropic", "kind": "subagent",
     "route": "claude_code", "enabled": True, "source": "fallback"},
    {"id": "deepseek/deepseek-v4-flash", "label": "DeepSeek V4 Flash (Freebuff)", "provider": "freebuff",
     "kind": "subagent", "route": "freebuff", "enabled": True, "source": "fallback"},
    {"id": "gemini-4-argon", "label": "Gemini 4 Argon (Gemini App)", "provider": "google", "kind": "manual",
     "route": "gemini_app", "enabled": True, "source": "fallback"},
    {"id": "gpt-6-sol", "label": "GPT 6 Sol (ChatGPT Web)", "provider": "openai", "kind": "manual",
     "route": "chatgpt_web", "enabled": True, "source": "fallback"},
    {"id": "gemini-3.8-flash-high", "label": "AG Flash 3.8 (Antigravity)", "provider": "antigravity",
     "kind": "manual", "route": "antigravity_manual", "enabled": True, "source": "fallback"},
    {"id": "ag-pro-3.1", "label": "AG Pro 3.1 (Antigravity)", "provider": "antigravity",
     "kind": "manual", "route": "antigravity_manual", "enabled": True, "source": "fallback"},
    {"id": "agent:codex", "label": "Codex", "provider": "openai-codex", "kind": "agent",
     "route": "codex", "enabled": True, "source": "fallback"},
)


def _rs():
    from . import runsheet
    return runsheet


def _run_routes():
    return tuple(_rs().RUN_POOLS)


# Registry routes that Autonomous offers under another name: Antigravity is a manual agentic
# coder here (Owner, 4 Nov 2026), so its registry route ``antigravity`` becomes ``antigravity_manual``.
ROUTE_ALIASES = {"antigravity": "antigravity_manual"}
CODEX_PROVIDERS = ("openai-codex", "codex")


def _normalise(m):
    row = dict(m)
    route = row.get("route")
    if route in ROUTE_ALIASES:
        row["registry_route"] = route
        row["route"] = ROUTE_ALIASES[route]
        row["kind"] = "manual"
    elif not route and row.get("kind") == "agent" and _is_codex(row):
        row["route"] = "codex"  # the registry's Codex agent row is the Codex run route
    return row


def registry(h):
    """``(models, source)``: the model registry when ``cc_engine.models`` exists, else the pools."""
    import importlib
    try:
        mod = importlib.import_module("cc_engine.models")
    except ImportError:
        mod = None
    fn = (getattr(mod, "ROUTES", None) or {}).get("GET /api/models") if mod is not None else None
    if callable(fn):
        try:
            out = fn(h, {})
        except Exception:
            out = None
        models = out.get("models") if isinstance(out, dict) else None
        if isinstance(models, list):
            return [_normalise(m) for m in models if isinstance(m, dict) and m.get("id")], "registry"
    return [dict(m) for m in FALLBACK_MODELS], "fallback"


def _disabled(m, toggles=None):
    """Switched off in Agents (``toggles``: Store/path/Snapshot) or in the registry row itself."""
    from . import toggles as T
    from .dispatch import is_disabled
    snap = T.resolve(toggles)
    route = m.get("registry_route") or m.get("route")
    if m.get("enabled") is False or not snap.is_enabled(m.get("id")):
        return True
    if route and snap.refusal(m.get("route") or route):
        return True
    return is_disabled(route, m.get("id"), toggles=snap)


def _is_codex(m):
    return ("codex" in str(m.get("route") or "").lower() or str(m.get("provider") or "").lower() in CODEX_PROVIDERS
            or str(m.get("id") or "").lower() in ("agent:codex", "codex"))


def _runnable(m):
    return str(m.get("route") or "") in _run_routes()


def enabled_models(models, toggles=None):
    """Enabled models only, each marked ``runnable`` (a run may use it) with a short ``note``."""
    from . import toggles as T
    snap = T.resolve(toggles)
    out = []
    for m in models:
        if _disabled(m, snap):
            continue
        row = dict(m)
        row["runnable"] = _runnable(m)
        route = row.get("route")
        if row["runnable"]:
            row["note"] = "manual: Copy Prompt" if route in ("gemini_app", "chatgpt_web", "antigravity_manual") else ROUTE_LABEL.get(route, route)
        elif _is_codex(row):
            row["note"] = "Codex: tick the Codex agent"
        else:
            row["note"] = "chat only"
        out.append(row)
    return out


def cli_model(route, m):
    """The model name a route's adapter takes, from a registry row; None means the preset ladder.

    Registry ids look like ``subagent:claude/opus-5.5`` or ``chat:freebuff/deepseek/deepseek-v4-flash``:
    the ``kind:`` prefix and a leading provider segment are dropped. An ``agent`` row (a whole
    route, e.g. ``agent:claude_code``) names no model.
    """
    if m.get("kind") == "agent" and not m.get("model"):
        return None
    mid = str(m.get("model") or m.get("id") or "")
    name = mid.split(":", 1)[-1] if ":" in mid else mid
    parts = name.split("/")
    lead = {str(m.get("provider") or "").lower(), str(m.get("registry_route") or route or "").lower(),
            "freebuff", "antigravity", "claude", "google", "manual"}
    if len(parts) > 1 and parts[0].lower() in lead:
        name = "/".join(parts[1:])
    if name in (route, m.get("registry_route")):
        return None
    low = name.lower()
    if route == "claude_code":
        for alias in ("opus", "sonnet", "haiku"):
            if alias in low:
                return alias
        return name
    return name


def default_settings():
    return {"preset": "balanced", "models": [], "efforts": {"claude": "medium", "codex": "medium"},
            "punch_clock": True, "max_use": {}, "auto_wake": True, "power_off": False,
            "merge_resets": True, "sequence": []}


def _bad(detail):
    return _http(400, {"error": "bad_request", "detail": detail})


def _flag(raw, key, default):
    v = raw.get(key, default)
    if not isinstance(v, bool):
        raise _bad(f"{key} must be true or false")
    return v


def normalize_settings(raw, store, models):
    """Validated Autonomous settings (raises a 400 HttpError naming the first problem)."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise _bad("settings must be an object")
    rs = _rs()
    base = default_settings()
    out = {}
    out["preset"] = raw.get("preset", base["preset"])
    if out["preset"] not in rs.PRESETS:
        raise _bad(f"preset must be one of {sorted(rs.PRESETS)}")
    known = {m["id"]: m for m in models}
    usable = {m["id"]: m for m in enabled_models(models, store)}

    def model_ok(mid, where):
        if not isinstance(mid, str) or not mid:
            raise _bad(f"{where} must be a model id")
        if mid not in known:
            raise _bad(f"unknown model {mid!r} in {where}")
        if mid not in usable:
            raise _bad(f"{known[mid].get('label') or mid} is turned off in Agents")
        if not usable[mid]["runnable"]:
            raise _bad(f"{usable[mid].get('label') or mid} is not a run model ({usable[mid]['note']})")
        return mid

    ticks = raw.get("models", base["models"])
    if not isinstance(ticks, list):
        raise _bad("models must be a list of model ids")
    out["models"] = []
    for mid in ticks:
        mid = model_ok(mid, "models")
        if mid not in out["models"]:
            out["models"].append(mid)
    efforts = raw.get("efforts", base["efforts"])
    if not isinstance(efforts, dict):
        raise _bad("efforts must be {claude, codex}")
    out["efforts"] = {}
    for key in ("claude", "codex"):
        value = efforts.get(key, base["efforts"][key])
        if value not in rs.EFFORTS:
            raise _bad(f"{key} effort must be one of {list(rs.EFFORTS)}")
        out["efforts"][key] = value
    for key in ("punch_clock", "auto_wake", "power_off", "merge_resets"):
        out[key] = _flag(raw, key, base[key])
    max_use = raw.get("max_use", base["max_use"])
    if not isinstance(max_use, dict):
        raise _bad("max_use must be a {pool: true|false} map")
    out["max_use"] = {}
    for pool, on in max_use.items():
        if pool not in _run_routes():
            raise _bad(f"max use is only for run pools, not {pool!r}")
        if on and S.pool_refusal(pool, store):
            raise _bad(f"{S.pool_refusal(pool, store)}, so it has no max use")
        if not isinstance(on, bool):
            raise _bad("max_use values must be true or false")
        if on:
            out["max_use"][pool] = True
    seq = raw.get("sequence", base["sequence"])
    if not isinstance(seq, list):
        raise _bad("sequence must be a list of rows")
    seen, rows = set(), []
    for i, row in enumerate(seq):
        if not isinstance(row, list):
            raise _bad(f"sequence row {i + 1} must be a list of projects")
        items = []
        for item in row:
            if isinstance(item, str):
                item = {"project": item}
            if not isinstance(item, dict) or not isinstance(item.get("project"), str):
                raise _bad(f"sequence row {i + 1} has an item without a project")
            pid = item["project"]
            if store.get_project(pid) is None:
                raise _bad(f"no such project {pid!r}")
            if pid in seen:
                raise _bad(f"project {pid!r} appears twice in the sequence")
            seen.add(pid)
            model = item.get("model")
            items.append({"project": pid, "model": model_ok(model, f"the model for {pid}") if model else None})
        if items:
            rows.append(items)
    out["sequence"] = rows
    return out


def load_settings(store):
    raw = store.get_meta(AUTONOMOUS_KEY)
    if not raw:
        return default_settings()
    try:
        data = json.loads(raw)
    except ValueError:
        return default_settings()
    out = default_settings()
    if isinstance(data, dict):
        out.update(data)
    return out


def save_settings(store, settings):
    store.set_meta(AUTONOMOUS_KEY, json.dumps(settings, sort_keys=True))


def usage_strip(rows):
    """Codex and Claude rows only (Owner: "you only really need to show codex and claude")."""
    by = {r.get("pool"): r for r in rows}
    out = []
    for pool in STRIP_POOLS:
        r = by.get(pool)
        if r is None:
            out.append({"pool": pool, "label": pool, "remaining_percent": None, "resets_at": None, "status": "UNKNOWN"})
            continue
        out.append({"pool": pool, "label": r.get("label") or pool, "remaining_percent": r.get("remaining_percent"),
                    "resets_at": r.get("resets_at"), "status": r.get("status") or "UNKNOWN",
                    "observed_at": r.get("observed_at")})
    return out


def autonomous_runs(store, active_only=True):
    out = []
    for r in store.list_runs(limit=10000):
        if _settings(r).get("planned_by") != "autonomous":
            continue
        if active_only and r["status"] not in S.ACTIVE_RUN_STATUSES:
            continue
        out.append(r)
    return out


def g_autonomous(h, q):
    store = h.app.store
    models, source = registry(h)
    usage_rows = _usage(h)
    titles = {}
    runs = []
    for r in sorted(autonomous_runs(store), key=lambda r: (_settings(r).get("autonomous_row", 0), r["id"])):
        st = _settings(r)
        waiting = [x for x in st.get("after_runs") or []
                   if (store.get_run(x) or {}).get("status") in S.ACTIVE_RUN_STATUSES]
        runs.append({"id": r["id"], "project_id": r["project_id"],
                     "project_title": _project_title(store, r["project_id"], titles),
                     "status": r["status"], "row": st.get("autonomous_row", 0), "waiting": bool(waiting)})
    return {"settings": load_settings(store), "models": enabled_models(models, store), "models_source": source,
            "usage": usage_strip(usage_rows), "runs": runs, "running": bool(runs),
            "kinds": list(SPRINT_KINDS)}


def p_auto_settings(h, q):
    _owner_only(h, "change the Autonomous run controls")
    b = h._json_body()
    store = h.app.store
    models, _ = registry(h)
    settings = normalize_settings(b.get("settings"), store, models)
    save_settings(store, settings)
    store.notify("run", {"autonomous": "settings"})
    return {"ok": True, "settings": settings}


def _ordered(names, preset_ladder):
    rank = {m: i for i, m in enumerate(preset_ladder)}
    seen = []
    for m in names:
        if m not in seen:
            seen.append(m)
    return sorted(seen, key=lambda m: (rank.get(m, len(rank)), seen.index(m)))


def run_settings_for(cfg, item, models_by_id, row, after_runs):
    """The run settings for one project of the sequence (validated by ``runsheet.expand``)."""
    rs = _rs()
    ticked = [models_by_id[i] for i in cfg["models"] if i in models_by_id]
    own = models_by_id.get(item.get("model")) if item.get("model") else None
    pools = []
    for m in ticked + ([own] if own else []):
        if m["route"] not in pools:
            pools.append(m["route"])
    pools = [p for p in rs.RUN_POOLS if p in pools]
    ladder = {}
    for pool in pools:
        preset = rs.LADDERS[cfg["preset"]].get(pool, [])
        names = _ordered([n for n in (cli_model(pool, m) for m in ticked if m["route"] == pool) if n], preset)
        first = cli_model(pool, own) if own and own["route"] == pool else None
        if first:
            names = [first] + [n for n in names if n != first]
        ladder[pool] = names or list(preset)
    settings = {
        "preset": cfg["preset"], "pools": pools, "advanced": {"model_ladder": ladder},
        "use_pool_fully": {p: bool(cfg["max_use"].get(p)) for p in pools},
        "auto_resume": cfg["auto_wake"], "power_off": cfg["power_off"], "merge_resets": cfg["merge_resets"],
        "planned_by": "autonomous", "autonomous_row": row, "after_runs": list(after_runs),
        "codex_effort": cfg["efforts"]["codex"],
    }
    efforts = {}
    if "claude_code" in pools:
        efforts["claude_code"] = cfg["efforts"]["claude"]
    if "codex" in pools:
        efforts["codex"] = cfg["efforts"]["codex"]
    if efforts:
        settings["efforts"] = efforts
    if cfg["punch_clock"]:
        settings["sprint_run"] = ANCHOR_RUN
    if own:
        settings["default_pool"] = own["route"]
        settings["project_model"] = own["id"]
    try:
        rs.expand(settings)
    except ValueError as exc:
        raise _bad(str(exc))
    return settings


def _settings_from_body(h, b):
    store = h.app.store
    models, _ = registry(h)
    if b.get("settings") is not None:
        cfg = normalize_settings(b.get("settings"), store, models)
    else:
        cfg = normalize_settings(load_settings(store), store, models)
    return cfg, models


def p_auto_run(h, q):
    _owner_only(h, "start an Autonomous run")
    b = h._json_body()
    store = h.app.store
    cfg, models = _settings_from_body(h, b)
    if not cfg["sequence"]:
        raise _bad("add at least one project to the sequence")
    for row in cfg["sequence"]:
        for item in row:
            if not cfg["models"] and not item.get("model"):
                raise _bad(f"tick a model, or give {item['project']} its own model")
    if autonomous_runs(store):
        raise _http(409, {"error": "already_running", "detail": "Autonomous is already running; stop it first"})
    busy = sorted({r["project_id"] for r in S.active_runs(store)}
                  & {i["project"] for row in cfg["sequence"] for i in row})
    if busy:
        raise _http(409, {"error": "project_busy",
                          "detail": "these projects already have an active run: " + ", ".join(busy)})
    save_settings(store, cfg)
    models_by_id = {m["id"]: m for m in enabled_models(models, store) if m["runnable"]}
    # Validate every project's settings before creating any run.
    prepared = [(r_i, item, run_settings_for(cfg, item, models_by_id, r_i, []))
                for r_i, row in enumerate(cfg["sequence"]) for item in row]
    titles = {}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    token = secrets.token_hex(2)
    # Ids sort in sequence order, so the scheduler ticks the rows above first.
    ids = [f"run-{stamp}-a{k:02d}{token}" for k in range(len(prepared))]
    created = []
    for k, (r_i, item, st) in enumerate(prepared):
        st["after_runs"] = [ids[j] for j, (rj, _, _) in enumerate(prepared) if rj < r_i]
        store.create_run(ids[k], item["project"], st, status="queued")
        title = _project_title(store, item["project"], titles)
        uses = ", ".join(ROUTE_LABEL.get(p, p) for p in st["pools"]) or "no pools"
        wait = " after the rows above finish" if st["after_runs"] else ""
        store.event(item["project"], None, "run", OWNER,
                    f"Owner started Autonomous: {title} is in row {r_i + 1} and uses {uses}{wait}.",
                    {"run": ids[k], "row": r_i, "after_runs": st["after_runs"], "requested_by": OWNER})
        store.notify("run", {"id": ids[k], "project": item["project"], "status": "queued"})
        created.append({"id": ids[k], "project": item["project"], "row": r_i, "after_runs": st["after_runs"]})
    kicker = getattr(h.app, "tick_kicker", None)
    kicked = bool(kicker(store) if callable(kicker) else kick_tick(store))
    return {"ok": True, "runs": created, "tick_started": kicked, "settings": cfg}


def p_auto_cancel(h, q):
    _owner_only(h, "stop Autonomous")
    store = h.app.store
    cancelled = []
    for r in autonomous_runs(store):
        S.cancel_run(store, r["id"], actor=OWNER, now=_now(h))
        cancelled.append(r["id"])
        store.notify("run", {"id": r["id"], "project": r["project_id"], "status": "cancelled"})
    if cancelled:
        store.event(None, None, "run", OWNER,
                    f"Owner stopped Autonomous, so its {len(cancelled)} run{'s' if len(cancelled) != 1 else ''} "
                    "stop on the next tick.", {"runs": cancelled})
    return {"ok": True, "cancelled": cancelled}


# ---- punch clock slots

def _sprint_meta(store, sid):
    raw = store.get_meta(SPRINT_META_PREFIX + sid)
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def ensure_anchor(store):
    if store.get_run(ANCHOR_RUN) is None:
        store.create_run(ANCHOR_RUN, None, {"planned_by": "autonomous-clock"}, status="manual")
    return ANCHOR_RUN


def _reset_for(pool, rows, now):
    best = None
    for r in rows:
        if r.get("pool") != CLOCK_POOLS[pool]:
            continue
        try:
            t = S.to_epoch(r.get("resets_at"))
        except ValueError:
            t = None
        if t is not None and t > now and (best is None or t < best):
            best = t
    return best


def slots(pool, rows, now, count=SLOT_COUNT):
    """``[(start, end, aligned)]``: now until the reset, then reset + 1 min for each 5-hour window."""
    reset = _reset_for(pool, rows, now)
    out = []
    if reset is None:
        return [(now + k * SLOT_SECONDS, now + (k + 1) * SLOT_SECONDS, False) for k in range(count)]
    if reset - now >= MIN_NOW_SLOT_SECONDS:
        out.append((now, reset, False))
    for k in range(count):
        b = reset + k * SLOT_SECONDS
        out.append((S.sprint_start_for_reset(b), b + SLOT_SECONDS, True))
    return out


def _live(store, pool, now):
    out = []
    for sp in store.list_sprints(ANCHOR_RUN) if store.get_run(ANCHOR_RUN) else []:
        if sp["pool"] != pool or sp["status"] not in LIVE_SPRINT:
            continue
        a = S.to_epoch(sp["start_at"])
        b = S.to_epoch(sp["end_at"]) if sp.get("end_at") else a + SLOT_SECONDS
        if b > now:
            out.append((a, b))
    return out


def _free(live, start, end):
    return not any(a < end and start < b for a, b in live)


def first_free(store, pool, rows, now):
    live = _live(store, pool, now)
    for start, end, aligned in slots(pool, rows, now):
        if _free(live, start, end):
            return start, end, aligned
    raise _http(409, {"error": "clock_full", "detail": f"no free {ROUTE_LABEL[pool]} slot in the next few days"})


def dual_slot(store, rows, now):
    """The first time both Claude and Codex have a free slot window: ``{pool: (start, end, aligned)}``."""
    windows = {p: slots(p, rows, now) for p in ("claude_code", "codex")}
    live = {p: _live(store, p, now) for p in windows}
    for t in sorted({w[0] for ws in windows.values() for w in ws}):
        pick = {}
        for p, ws in windows.items():
            w = next(((a, b, al) for a, b, al in ws if a <= t < b), None)
            if w is None or not _free(live[p], t, w[1]):
                break
            pick[p] = (t, w[1], w[2] and t == w[0])
        if len(pick) == 2:
            return pick
    raise _http(409, {"error": "clock_full", "detail": "no time when both Claude and Codex are free"})


def _add_clock_sprint(store, pool, start, end, aligned, kind, group=None, handoff_after=None):
    label = ROUTE_LABEL[pool]
    tail = ", one minute after the reset" if aligned else ""
    if pool == "codex":
        reason = (f"Owner confirmed a Codex sprint from {_hm(start)} to {_hm(end)}{tail}; "
                  "ticked Codex work runs in this window.")
    else:
        reason = f"Owner added a {label} sprint from {_hm(start)} to {_hm(end)}{tail}."
    sid = S.create_sprint(store, ANCHOR_RUN, pool, start, end, actor=OWNER, project_id=None, reason=reason)
    meta = {"kind": kind, "aligned": bool(aligned), "group": group, "handoff_after": handoff_after}
    store.set_meta(SPRINT_META_PREFIX + sid, json.dumps(meta))
    return {"id": sid, "pool": pool, "start_at": S.iso(start), "end_at": S.iso(end), "aligned": bool(aligned),
            "kind": kind, "handoff_after": handoff_after}


def add_sprints(store, kind, rows, now):
    ensure_anchor(store)
    if kind == "claude":
        return [_add_clock_sprint(store, "claude_code", *first_free(store, "claude_code", rows, now), kind)]
    if kind == "codex":
        return [_add_clock_sprint(store, "codex", *first_free(store, "codex", rows, now), kind)]
    if kind == "dual":
        pick = dual_slot(store, rows, now)
        group = "g-" + secrets.token_hex(4)
        return [_add_clock_sprint(store, p, *pick[p], kind, group=group) for p in ("claude_code", "codex")]
    # auto_dual: each provider at its own next slot, the later one picking up the earlier one's work
    group = "g-" + secrets.token_hex(4)
    picks = sorted(((p,) + first_free(store, p, rows, now) for p in ("claude_code", "codex")),
                   key=lambda x: (x[1], x[0] != "claude_code"))
    first = _add_clock_sprint(store, picks[0][0], *picks[0][1:], kind, group=group)
    second = _add_clock_sprint(store, picks[1][0], *picks[1][1:], kind, group=group, handoff_after=first["id"])
    return [first, second]


def p_auto_sprint(h, q):
    _owner_only(h, "add a sprint")
    b = h._json_body()
    kind = b.get("kind")
    if kind not in SPRINT_KINDS:
        raise _bad(f"kind must be one of {list(SPRINT_KINDS)}")
    rows = _usage(h)
    store = h.app.store
    for pool in KIND_POOLS[kind]:  # a switched-off agent gets no sprint, whatever the page sent
        off = S.pool_refusal(pool, store)
        if off:
            raise _http(403, {"error": "disabled", "detail": off})
    if "codex" in KIND_POOLS[kind] and b.get("confirm") is not True:
        raise _http(409, {"error": "confirm_codex", "usage": [r for r in usage_strip(rows) if r["pool"] in CODEX_USAGE],
                          "detail": ("A Codex sprint draws on your Codex plan; "
                                     "confirm after checking its usage.")})
    out = add_sprints(store, kind, rows, _now(h))
    store.notify("run", {"id": ANCHOR_RUN, "sprints": [s["id"] for s in out]})
    return {"ok": True, "sprints": out}


# ---- Copy Prompt

def build_autonomous_prompt(store, cfg, models, rows, now):
    titles = {}
    by_id = {m["id"]: m for m in models}

    def name(mid):
        m = by_id.get(mid) or {}
        return f"{m.get('label') or mid} ({mid}, {ROUTE_LABEL.get(m.get('route'), m.get('route') or 'no route')})"

    lines = ["# Meridian Autonomous run", "",
             f"Prepared {S.iso(now)} from the Autonomous tab. Engine: http://127.0.0.1:8751 "
             "(read /api/board?project=<id>, /api/autonomous and /api/sprints/timeline).", "",
             "## Project sequence",
             "Projects in one row start together; a row waits for every project above it."]
    if not cfg["sequence"]:
        lines.append("- (no projects yet)")
    for i, row in enumerate(cfg["sequence"]):
        parts = []
        for item in row:
            title = _project_title(store, item["project"], titles)
            model = name(item["model"]) if item.get("model") else "the ticked models"
            parts.append(f"{title} [{item['project']}] using {model}")
        lines.append(f"- Row {i + 1}: " + "; ".join(parts))
    lines += ["", "## Models ticked"]
    lines += [f"- {name(mid)}" for mid in cfg["models"]] or ["- (none; each project names its own)"]
    fully = [ROUTE_LABEL.get(p, p) for p, on in cfg["max_use"].items() if on]
    lines += ["", "## Effort and timing",
              f"- Claude effort: {cfg['efforts']['claude']}. Codex effort: {cfg['efforts']['codex']}. "
              f"Spend preset: {cfg['preset']}.",
              f"- Punch clock: {'on' if cfg['punch_clock'] else 'off'}. Use fully: {', '.join(fully) or 'none'}. "
              f"Auto-wake on reset: {'on' if cfg['auto_wake'] else 'off'}. "
              f"Merge resets: {'on' if cfg['merge_resets'] else 'off'}. "
              f"Power off when done: {'on (10 minutes to cancel)' if cfg['power_off'] else 'off'}.",
              "", "## Punch clock"]
    clock = [s for s in describe_sprints(store, now) if s.get("anchor") and s["state"] in ("planned", "due", "active")]
    for s in clock:
        tail = " (reset + 1 min)" if s.get("aligned") else ""
        who = "Owner's own Codex window" if s["pool"] == "codex" else s["state"]
        lines.append(f"- {s['pool_label']} {_hm(S.to_epoch(s['start_at']))}-{_hm(S.to_epoch(s['end_at']))}{tail}: {who}")
    if not clock:
        lines.append("- (no sprints; pools follow their usage)")
    lines += ["", "## Usage now (Codex and Claude)"]
    for r in usage_strip(rows):
        pct = "unknown" if r["remaining_percent"] is None else f"{r['remaining_percent']:g}% left"
        resets = f", resets {_hm(S.to_epoch(r['resets_at']))}" if r.get("resets_at") else ""
        lines.append(f"- {r['label']}: {pct}{resets} ({str(r['status']).lower()})")
    lines += ["", "## Rules",
              "- Work only on the tasks in these projects' plans; every goal stays exactly as written.",
              "- Call only agents, routes and models switched on in Agents (GET /api/toggles); a switched-off one is never a subagent.",
              "- Gemini App and Antigravity are manual: prepare a Copy Prompt packet and ask Owner; never run them unattended.",
              "- No paid API routes. Respect usage floors; when a pool is used up, wait for its reset plus one minute.",
              "- Finish each task with a short plain-English summary and the done-check result."]
    return "\n".join(lines) + "\n"


def p_auto_prompt(h, q):
    b = h._json_body()
    cfg, models = _settings_from_body(h, b)
    return {"prompt": build_autonomous_prompt(h.app.store, cfg, enabled_models(models, h.app.store), _usage(h), _now(h))}


ROUTES = {
    "GET /api/sprints/timeline": g_timeline,
    "POST /api/sprints/plan": p_plan,
    "POST /api/sprints/update": p_update,
    "POST /api/sprints/cancel": p_cancel,
    "POST /api/sprints/start-now": p_start_now,
    "GET /api/autonomous": g_autonomous,
    "POST /api/autonomous/settings": p_auto_settings,
    "POST /api/autonomous/run": p_auto_run,
    "POST /api/autonomous/cancel": p_auto_cancel,
    "POST /api/autonomous/sprint": p_auto_sprint,
    "POST /api/autonomous/prompt": p_auto_prompt,
}
