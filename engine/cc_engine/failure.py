"""Failure policy.

Two kinds of failure are kept apart:

- **transient** (``classify_exit``): a non-zero exit, usage-limit text, HTTP 403/429/5xx,
  a bridge timeout, a job timeout or a lost job. The attempt never completed,
  so it is not counted and never climbs the ladder: ``on_transient`` puts the node back
  to ready after a delay and holds its pool until the pool resets (or a default pause).
  After ``MAX_TRANSIENT`` transients in a row the node is reported instead. On a provider
  limit a pool/model the scheduler picked (``scheduler.release_auto_pick``) is released, so the
  node moves to the next open pool and its own ladder; the next attempt resumes from the partial
  work (``dispatch.earlier_attempt``).
- **a completed attempt whose check failed** (``on_check_failed``): climb the
  model/effort ladder from ``runsheet.expand`` (raise effort once, then step up the
  model ladder at the raised effort), skipping every rung whose model or route is switched off
  in Agents (``cc_engine.toggles``). When the ladder is exhausted: write a ``report``
  event, set the node to ``needs_you`` and ask the orchestrator/owner what to do.

The orchestrator may add fix-up nodes (``<id>-fix<n>``) or reorder dependencies. The
goal of a node is never changed by anything here. Every automatic change writes a
one-sentence summary event, and evidence records the attempt number.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import scheduler as S
from .store import GOAL_FIELDS, GoalLocked  # noqa: F401  (re-exported for callers)

EFFORTS = ("low", "medium", "high", "xhigh", "max")

REPORT_OPTIONS = [
    "Add a fix-up step before this task",
    "Try a different model",
    "Do this task manually",
    "Skip for now and continue the rest",
]

TRANSIENT_RETRY_SECONDS = 600      # the node waits this long before it is ready again
TRANSIENT_POOL_HOLD_SECONDS = 900  # pool pause when its reset time is unknown
MAX_TRANSIENT = 6                  # transients in a row before the node is reported

# Text that marks a provider limit or bridge failure (searched in stderr / wrapper error only).
LIMIT_PATTERNS = (
    (re.compile(r"usage limit|limit reached|limit will reset|out of (?:extra )?usage|quota",
                re.I), "a usage limit was reached"),
    (re.compile(r"rate.?limit|too many requests|\b429\b", re.I), "the provider rate-limited it"),
    (re.compile(r"\b403\b|forbidden", re.I), "the provider refused it (403)"),
    (re.compile(r"did not respond within|timed out|timeout", re.I), "the bridge timed out"),
)
# Reasons that mean the provider's allowance is spent: the pool waits for its reset.
LIMIT_REASONS = ("a usage limit was reached", "the provider rate-limited it",
                 "the provider refused it (403)")


def _limit_reason(text):
    for pattern, reason in LIMIT_PATTERNS:
        if pattern.search(text or ""):
            return reason
    return None


def classify_exit(poll):
    """``('transient', reason)`` or ``('completed', None)`` for an exited job's poll result.

    Transient: a non-zero exit, an HTTP status outside 2xx, usage-limit / 403 /
    bridge-timeout text in stderr or the wrapper error, or a Claude JSON result flagged
    ``is_error``. Only a completed attempt goes on to its done-check.
    """
    poll = poll or {}
    status = poll.get("http_status")
    if status is not None:
        try:
            status = int(status)
        except (TypeError, ValueError):
            status = 0
        if not 200 <= status < 300:
            if status == 403:
                return "transient", "the provider refused it (403)"
            if status == 429:
                return "transient", "the provider rate-limited it"
            return "transient", f"the provider answered HTTP {status}"
    reason = _limit_reason(" ".join(str(poll.get(k) or "") for k in ("error", "stderr_tail")))
    if poll.get("route") == "claude_code":
        try:
            payload = json.loads(poll.get("output_tail") or "")
        except ValueError:
            payload = None
        if isinstance(payload, dict) and payload.get("is_error"):
            return "transient", (_limit_reason(str(payload.get("result") or ""))
                                 or "Claude reported an error")
    code = poll.get("exit_code")
    try:
        code = int(code)
    except (TypeError, ValueError):
        code = None
    if code != 0:
        return "transient", reason or f"it exited with code {code}"
    if reason:
        return "transient", reason
    return "completed", None


def permission_denials(poll):
    """``Claude was not allowed to edit files: Write C:\\..\\result.txt`` from a Claude job's JSON
    result (its ``permission_denials``), else None. Reads the whole output file when there is one."""
    poll = poll or {}
    if poll.get("route") != "claude_code":
        return None
    text = None
    path = poll.get("output_path")
    if path:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except (OSError, TypeError, ValueError):
            text = None
    try:
        payload = json.loads(text if text is not None else (poll.get("output_tail") or ""))
    except ValueError:
        return None
    denied = payload.get("permission_denials") if isinstance(payload, dict) else None
    if not isinstance(denied, list) or not denied:
        return None
    items = []
    for d in denied:
        if not isinstance(d, dict):
            continue
        inp = d.get("tool_input") if isinstance(d.get("tool_input"), dict) else {}
        target = inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or inp.get("command")
        item = " ".join(str(x) for x in (d.get("tool_name") or "a tool", target) if x)
        if item not in items:
            items.append(item)
    return ("Claude was not allowed to edit files: " + "; ".join(items)[:400]) if items else None


def _retry_key(project_id, node_id):
    return f"retry_after:{project_id}:{node_id}"


def _streak_key(project_id, node_id):
    return f"transients:{project_id}:{node_id}"


def retry_after(store, project_id, node_id):
    """Epoch before which a node that stopped early is not started again (or None)."""
    raw = S.meta_get(store, _retry_key(project_id, node_id))
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def clear_transients(store, project_id, node_id):
    """A completed attempt ends a transient streak."""
    for key in (_streak_key(project_id, node_id), _retry_key(project_id, node_id)):
        if S.meta_get(store, key) is not None:
            S.meta_set(store, key, None)


def on_transient(store, project_id, node, settings, reason, now, pool_resets_at=None,
                 delay=TRANSIENT_RETRY_SECONDS, hold_pool=True, asker=None):
    """Put ``node`` back to ready after ``delay`` and hold its pool until it resets.

    The attempt is not counted (``attempts`` goes back down) and the ladder is untouched.
    Returns ``{action: 'retry'|'report', ...}``.
    """
    node = S.norm_node(node)
    nid, pool = node["id"], node.get("pool")
    attempt = node["attempts"]
    streak = int(S.meta_get(store, _streak_key(project_id, nid)) or 0) + 1
    S.meta_set(store, _streak_key(project_id, nid), str(streak))
    evidence = f"attempt {attempt}: not completed ({reason})"
    S.update_node_fields(store, project_id, nid, {"attempts": max(0, attempt - 1)}, "failure",
                         f"Task {nid} did not finish because {reason}, so this try is not counted.")
    if streak >= MAX_TRANSIENT:
        summary = (f"Task {nid} stopped early {streak} times in a row because {reason}, "
                   f"so it needs a decision.")
        S.set_status(store, project_id, nid, "needs_you", summary, actor="failure",
                     evidence=evidence)
        S.emit(store, project_id, nid, "report", summary, actor="failure",
               data={"reason": reason, "transients": streak, "attempt": attempt})
        qid = None
        if asker is not None:
            qid = asker(project_id=project_id, node_id=nid, kind="select", purpose="failure_choice",
                        prompt=f"Task {nid} keeps stopping before it finishes. What should happen next?",
                        options=list(REPORT_OPTIONS))
        return {"action": "report", "question_id": qid}
    until = now + delay if delay else None
    if until:
        S.meta_set(store, _retry_key(project_id, nid), str(until))
    held = None
    if hold_pool and pool:
        reset = pool_resets_at if pool_resets_at and pool_resets_at > now else None
        if reset is not None and reason in LIMIT_REASONS:
            held = reset  # a provider limit: hold the pool until it resets
        else:  # a crash or bridge timeout: a short pause (never past the reset)
            held = min(reset or float("inf"), now + TRANSIENT_POOL_HOLD_SECONDS)
        S.hold_pool(store, pool, held, reason)
    moved = {}
    if reason in LIMIT_REASONS:
        # The pool is spent, not the task: a node the plan did not pin to this pool goes to the next
        # open pool (and its model ladder) instead of waiting for the reset; the next attempt
        # continues from the partial work (dispatch.earlier_attempt).
        moved = S.release_auto_pick(store, project_id, nid)
        if moved:
            S.update_node_fields(store, project_id, nid, moved, "failure",
                                 f"Task {nid} is released from {pool} so another open pool can take it.")
    later = max(until or 0, held or 0) if not moved.get("pool") else (until or 0)
    when = f" at {S.local_hhmm(later)}" if later else ""
    where = " on another pool" if moved.get("pool") else ""
    summary = f"Task {nid} stopped early because {reason} and will try again{where}{when}."
    S.set_status(store, project_id, nid, "not_started", summary, actor="failure", evidence=evidence)
    S.emit(store, project_id, nid, "transient", summary, actor="failure",
           data={"reason": reason, "attempt": attempt, "retry_after": until, "pool": pool,
                 "pool_held_until": held, "released": sorted(moved)})
    return {"action": "retry", "retry_after": until, "pool_held_until": held, "released": sorted(moved)}


# ---------------------------------------------------------------- ladder

def _raise_effort(effort):
    if effort not in EFFORTS:
        return "high"
    i = EFFORTS.index(effort)
    return EFFORTS[min(i + 1, len(EFFORTS) - 1)]


def ladder(settings, pool, current_model=None):
    """Ordered rungs ``[(model, effort), ...]`` for ``pool``."""
    models = list((settings.get("model_ladder") or {}).get(pool) or [current_model])
    if settings.get("effort_ladder"):
        efforts = list(settings["effort_ladder"])
        rungs = [(models[0], efforts[0])] + [(models[0], e) for e in efforts[1:]] \
            + [(m, efforts[-1]) for m in models[1:]]
    else:
        base = settings.get("effort")
        base = base.get(pool) if isinstance(base, dict) else base
        base = base or "medium"
        raised = _raise_effort(base)
        rungs = [(models[0], base)] + [(m, raised) for m in models]
    out = []
    for r in rungs:
        if r not in out:
            out.append(r)
    return out


def next_rung(node, settings, toggles=None):
    """The next ``(model, effort)`` up the ladder, skipping anything switched off in Agents.

    ``toggles``: a ``toggles.Snapshot``, Store or path; None reads the live engine database.
    A pool switched off as a whole has no next rung.
    """
    from . import toggles as T
    from .dispatch import route_for_pool
    snap = T.resolve(toggles)
    route = route_for_pool(node.get("pool"))
    rungs = ladder(settings, node.get("pool"), node.get("model"))
    cur = (node.get("model"), node.get("effort"))
    if cur in rungs:
        i = rungs.index(cur)
    else:
        same_model = [k for k, r in enumerate(rungs) if r[0] == cur[0]]
        i = same_model[0] if same_model else -1
    for rung in rungs[i + 1:]:
        if snap.allows(route, rung[0]):
            return rung
    return None


def on_check_failed(store, project_id, node, settings, detail, asker=None):
    """A completed attempt failed its check: escalate, or report when the ladder is exhausted.

    Returns ``{action, ...}``. Evidence records the attempt number.
    """
    node = S.norm_node(node)
    nid = node["id"]
    clear_transients(store, project_id, nid)
    evidence = f"attempt {node['attempts']}: {detail}"[:2000]
    rung = next_rung(node, settings, toggles=store)
    if rung is not None:
        model, effort = rung
        fields = {}
        if model != node.get("model"):
            fields["model"] = model
        if effort != node.get("effort"):
            fields["effort"] = effort
        what = []
        if "model" in fields:
            what.append(f"model {model}")
        if "effort" in fields:
            what.append(f"{effort} effort")
        summary = f"Task {nid} failed its check, so it will try again with {' and '.join(what)}."
        S.update_node_fields(store, project_id, nid, fields, "failure", summary)
        S.set_status(store, project_id, nid, "not_started", summary, actor="failure",
                     evidence=evidence)
        S.emit(store, project_id, nid, "escalate", summary, actor="failure",
               data={"model": model, "effort": effort, "detail": str(detail)[:2000],
                     "attempt": node["attempts"]})
        return {"action": "escalate", "model": model, "effort": effort}

    doctor = None
    try:  # failure_doctor (worker A): one cause line and a fix-up task, when it is present
        from . import failure_doctor
        doctor = failure_doctor.on_check_failed(store, project_id, nid, detail)
    except Exception:  # absent or broken: the needs-you report below still goes out
        doctor = None
    summary = (f"Task {nid} failed its check at every model and effort level allowed, "
               f"so it needs a decision.")
    S.emit(store, project_id, nid, "report", summary, actor="failure",
           data={"detail": str(detail)[:2000], "attempts": node.get("attempts"),
                 "model": node.get("model"), "effort": node.get("effort"),
                 "doctor": doctor if isinstance(doctor, dict) else None})
    S.set_status(store, project_id, nid, "needs_you", summary, actor="failure",
                 evidence=evidence)
    qid = None
    if asker is not None:
        qid = asker(project_id=project_id, node_id=nid, kind="select", purpose="failure_choice",
                    prompt=f"Task {nid} keeps failing its check. What should happen next?",
                    options=list(REPORT_OPTIONS))
    return {"action": "report", "question_id": qid, "doctor": doctor if isinstance(doctor, dict) else None}


# ---------------------------------------------------------------- orchestrator edits

def _would_cycle(nodes_by_id, start, target):
    """True if ``target`` is reachable from ``start`` via deps (start depends on .. target)."""
    seen, stack = set(), [start]
    while stack:
        cur = stack.pop()
        if cur == target:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(nodes_by_id.get(cur, {}).get("deps", []))
    return False


def add_fixup(store, project_id, node_id, title, check, goal=None, pool=None, model=None,
              effort=None, actor="orchestrator"):
    """Insert ``<id>-fix<n>`` before ``node_id``. The original goal is untouched."""
    nodes = {n["id"]: n for n in S.list_nodes(store, project_id)}
    orig = nodes[node_id]
    n = 1
    while f"{node_id}-fix{n}" in nodes:
        n += 1
    fid = f"{node_id}-fix{n}"
    goal = goal or f"Remove what blocks task {node_id} from passing its check, within its original goal."
    row = {"id": fid, "title": title, "goal": goal, "check": check, "deps": list(orig["deps"]),
           "owner": orig.get("owner"), "lane": orig.get("lane"), "pool": pool or orig.get("pool"),
           "model": model, "effort": effort}
    summary = f"Added fix-up step {fid} ({title}) before task {node_id}."
    store.add_node(project_id, row, actor, summary)
    S.update_node_fields(store, project_id, node_id,
                         {"deps": orig["deps"] + [fid]}, actor,
                         f"Task {node_id} now waits for fix-up step {fid}.")
    if orig["status"] in ("error", "needs_you", "submitted"):
        S.set_status(store, project_id, node_id, "not_started",
                     f"Task {node_id} will run again after fix-up step {fid}.", actor=actor)
    S.emit(store, project_id, fid, "fixup", summary, actor=actor, data={"for": node_id})
    return fid


def reorder_deps(store, project_id, node_id, deps, actor="orchestrator"):
    """Replace a node's dependencies (no cycles, only existing nodes)."""
    nodes = {n["id"]: n for n in S.list_nodes(store, project_id)}
    deps = list(dict.fromkeys(deps))
    for d in deps:
        if d not in nodes or d == node_id:
            raise ValueError(f"unknown or self dependency {d!r}")
        if _would_cycle(nodes, d, node_id):
            raise ValueError(f"depending on {d} would create a cycle")
    summary = (f"Task {node_id} now waits for {', '.join(deps)}." if deps
               else f"Task {node_id} no longer waits for other tasks.")
    S.update_node_fields(store, project_id, node_id, {"deps": deps}, actor, summary)
    S.emit(store, project_id, node_id, "reorder", summary, actor=actor,
           data={"deps": deps, "was": nodes[node_id]["deps"]})
    return deps


def swap_model(store, project_id, node_id, model, effort=None, actor="orchestrator"):
    """Pin another model on a task; one switched off in Agents is refused (``ValueError``)."""
    from . import toggles as T
    from .dispatch import route_for_pool
    node = store.get_node(project_id, node_id) or {}
    off = T.refusal(route_for_pool(node.get("pool")) if node.get("pool") else None, model, db=store)
    if off:
        raise ValueError(off)
    fields = {"model": model}
    if effort:
        fields["effort"] = effort
    summary = f"Task {node_id} will now use {model}" + (f" at {effort} effort." if effort else ".")
    S.update_node_fields(store, project_id, node_id, fields, actor, summary)
    S.emit(store, project_id, node_id, "swap_model", summary, actor=actor, data=fields)
