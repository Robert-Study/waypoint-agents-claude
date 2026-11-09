"""Answered questions go back to the work: warm sessions get the answer, cold ones resume from Waypoint.

A question Owner answers in Waypoint carries the id of the session that asked (``session``, from the receipt or
question file) when it is known. Once per answered question the runner decides how the answer reaches the work and
writes that decision into the question file itself (``resume``), so the answer and the blocked work are readable
from the data folder alone: ``questions/<id>.json`` holds the prompt, the answer, ``project``/``node`` and
``resume``; ``projects/<project>/nodes/<node>.json`` holds the blocked node.

Warm or cold
- The asking session's last activity is the newest of ``sessions/<id>.json`` (``updated_at``, ``started_at``, the
  last event) and the desktop app's own session file (``lastActivityAt``). It is warm when it is not ended and was
  active within ``settings.resume_cold_after_minutes`` (default 60: the prompt cache lifetime).
- Unknown session, no activity found, or an ended session (done, error, archived) counts as cold: a cold chat is
  never woken, because re-reading a long cold context costs more than starting fresh.

What happens
- warm  -> ``mode: direct``, ``action: deliver``. The mod in that session appends the answer to it and sets
  ``delivered_at`` (hooks/answers.ts). The node is left as it is.
- cold, with a node -> ``mode: fresh``, ``action: restart_node``. A ``needs_you`` node goes back to ``ready`` with
  the evidence "answered in questions/<id>.json; resume <project> from Waypoint"; the runner then starts a fresh
  worker on that node whose brief opens with that line and the answers (``resume_header``).
- cold, no node -> ``mode: fresh``, ``action: clear_session``. Nothing to restart, so the lead clears that session
  and gives it the one line ``resume <project> from Waypoint``; it sets ``delivered_at`` when done.

A node with several open questions is routed only when the last of them is answered, all in one decision.
Writes are revision-checked; a question or node changed underneath is left for the next tick. No model is called.
"""
from __future__ import annotations

import glob
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_PY = Path(__file__).resolve().parents[2] / "core" / "waypoint-data" / "py"
if _PY.is_dir() and str(_PY) not in sys.path:
    sys.path.insert(0, str(_PY))

from waypoint_data.store import RevConflict, iso, parse_iso  # noqa: E402

__all__ = ["SETTING", "DEFAULT_COLD_AFTER_MINUTES", "cold_after_minutes", "resume_line", "session_activity",
           "decide", "process", "resume_header", "desktop_activity"]

SETTING = "resume_cold_after_minutes"
DEFAULT_COLD_AFTER_MINUTES = 60  # the prompt cache lifetime; a setting, not a constant callers rely on
WRITER = "runner"
ENDED = ("done", "error", "archived")
_VOLATILE = ("rev", "updated_at", "updated_by")
DESKTOP_FOLDER = "claude-code-sessions"
DESKTOP_MAX_FILE = 2 * 1024 * 1024


def cold_after_minutes(store) -> float:
    """``settings.resume_cold_after_minutes`` when it is a positive number, else 60."""
    try:
        s = store.read("settings/settings.json")
    except Exception:  # noqa: BLE001 - an unreadable settings file falls back to the default
        s = None
    v = s.get(SETTING) if isinstance(s, dict) else None
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
        return float(v)
    return float(DEFAULT_COLD_AFTER_MINUTES)


def resume_line(project) -> str:
    return f"resume {project} from Waypoint" if project else "resume from Waypoint"


def _ts(value):
    """Epoch seconds for an ISO string, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        t = parse_iso(value)  # epoch seconds
        return float(t) if t is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


# ---------------------------------------------------------------- the desktop app's session files (read only)
def _desktop_roots(env=None):
    env = os.environ if env is None else env
    found = []
    local = env.get("LOCALAPPDATA")
    if local:
        found += sorted(glob.glob(os.path.join(glob.escape(local), "Claude-Data", "*", DESKTOP_FOLDER)))
    roaming = env.get("APPDATA")
    if roaming:
        found.append(os.path.join(roaming, "Claude", DESKTOP_FOLDER))
    return [p for p in dict.fromkeys(found) if os.path.isdir(p)]


def desktop_activity(session_id, roots=None):
    """``{last_activity_at, archived}`` for a desktop session (matched by ``cliSessionId`` or ``sessionId``), or None.

    Reads the desktop app's own ``local_<uuid>.json`` files; never writes them."""
    if not session_id:
        return None
    for root in (_desktop_roots() if roots is None else roots):
        for dirpath, dirs, files in os.walk(root):
            if dirpath[len(str(root)):].count(os.sep) >= 3:
                dirs[:] = []
            for name in files:
                if not (name.startswith("local_") and name.endswith(".json")):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    if os.path.getsize(path) > DESKTOP_MAX_FILE:
                        continue
                    d = json.loads(Path(path).read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(d, dict) or session_id not in (d.get("cliSessionId"), d.get("sessionId")):
                    continue
                ms = d.get("lastActivityAt")
                at = None
                if isinstance(ms, (int, float)) and not isinstance(ms, bool) and ms > 0:
                    at = datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds")
                return {"last_activity_at": at, "archived": bool(d.get("isArchived"))}
    return None


# ---------------------------------------------------------------- warm or cold
def session_activity(store, session_id, desktop=desktop_activity) -> dict:
    """``{last_at: epoch|None, ended: bool, source: str|None}`` for the asking session."""
    out = {"last_at": None, "ended": False, "source": None}
    if not session_id:
        return out
    stamps = []
    try:
        sess = store.read(f"sessions/{session_id}.json")
    except Exception:  # noqa: BLE001 - a bad id or unreadable file is "unknown"
        sess = None
    if isinstance(sess, dict):
        if sess.get("status") in ENDED:
            out["ended"] = True
        events = sess.get("events") if isinstance(sess.get("events"), list) else []
        for v in (sess.get("updated_at"), sess.get("started_at"), *(e.get("at") for e in events[-1:]
                                                                      if isinstance(e, dict))):
            t = _ts(v)
            if t is not None:
                stamps.append((t, "sessions"))
    d = None
    if desktop is not None:
        try:
            d = desktop(session_id)
        except Exception:  # noqa: BLE001 - the desktop files are a hint, never a reason to fail
            d = None
    if isinstance(d, dict):
        if d.get("archived"):
            out["ended"] = True
        t = _ts(d.get("last_activity_at"))
        if t is not None:
            stamps.append((t, "desktop"))
    if stamps:
        out["last_at"], out["source"] = max(stamps)
    return out


def decide(activity, now_s, cold_after_min):
    """``(mode, idle_minutes, reason)``: ``direct`` for a warm session, ``fresh`` otherwise."""
    last = activity.get("last_at")
    idle = None if last is None else round(max(0.0, now_s - last) / 60.0, 1)
    if activity.get("ended"):
        return "fresh", idle, "the asking session has ended"
    if last is None:
        return "fresh", None, "the asking session is unknown or has no recorded activity"
    if idle > cold_after_min:
        return "fresh", idle, f"the asking session was idle {idle:g} min, over {cold_after_min:g} (cache cold)"
    return "direct", idle, f"the asking session was idle {idle:g} min, within {cold_after_min:g} (cache warm)"


# ---------------------------------------------------------------- routing
def _answered(q) -> bool:
    return q.get("answered_at") not in (None, "") or q.get("answer") is not None


def _groups(questions):
    """Answered, unrouted questions grouped by (project, node); a group waits while any sibling is still open."""
    by = {}
    for q in questions:
        if not isinstance(q, dict) or not q.get("id"):
            continue
        key = (q.get("project"), q.get("node")) if q.get("node") else ("", "q:" + q["id"])
        by.setdefault(key, []).append(q)
    out = []
    for key, qs in sorted(by.items(), key=lambda kv: kv[0]):
        todo = [q for q in qs if _answered(q) and not q.get("resume")]
        if todo and all(_answered(q) for q in qs if not q.get("resume")):
            out.append((key, todo))
    return out


def pending(questions) -> list:
    """Answered questions that ``process`` would route now (empty when none: the runner may go idle)."""
    return [q for _, qs in _groups(questions or []) for q in qs]


def _restart_node(store, project, node_id, qids, line, now_s):
    """needs_you -> ready. Returns the status the node had, or None when it is unknown."""
    rel = f"projects/{project}/nodes/{node_id}.json"
    node = store.read(rel)
    if not isinstance(node, dict):
        return None
    before = node.get("status")
    if before == "needs_you":
        body = {k: v for k, v in node.items() if k not in _VOLATILE}
        refs = ", ".join(f"questions/{q}.json" for q in qids)
        body.update(status="ready", evidence=f"answered in {refs}; {line}"[:500])
        store.write(rel, body, expected_rev=node.get("rev"))  # RevConflict: the caller retries next tick
    return before


def process(store, now=time.time, desktop=desktop_activity) -> list:
    """Route every newly answered question once. Returns ``[{question, mode, action, session}]``."""
    now_s = now()
    limit = cold_after_minutes(store)
    try:
        questions = store.list_entities("questions")
    except Exception:  # noqa: BLE001 - no questions folder yet
        return []
    done = []
    for (project, node_id), qs in _groups(questions):
        project = project or qs[0].get("project")
        session = next((q.get("session") for q in qs if q.get("session")), None)
        mode, idle, reason = decide(session_activity(store, session, desktop), now_s, limit)
        line = resume_line(project)
        before = None
        delivered = None
        if mode == "direct":
            action = "deliver"
        elif project and qs[0].get("node"):
            action = "restart_node"
            try:
                before = _restart_node(store, project, qs[0]["node"], [q["id"] for q in qs], line, now_s)
            except RevConflict:
                continue
            delivered = iso(now_s)
        else:
            action = "clear_session"
        block = {"mode": mode, "session": session, "line": line, "idle_minutes": idle,
                 "cold_after_minutes": limit, "reason": reason, "action": action,
                 "node_status_before": before, "decided_at": iso(now_s), "delivered_at": delivered}
        for q in qs:
            body = {k: v for k, v in q.items() if k not in _VOLATILE}
            body["resume"] = block
            try:
                store.write(f"questions/{q['id']}.json", body, expected_rev=q.get("rev"))
            except RevConflict:
                continue
            done.append({"question": q["id"], "mode": mode, "action": action, "session": session})
        where = f"{project}/{qs[0]['node']}" if qs[0].get("node") else (project or "no project")
        text = {"deliver": f"Answer for {where} goes straight to the warm session {session}.",
                "restart_node": f"Answer for {where}: the session is cold, so a fresh worker resumes it ({line}).",
                "clear_session": f"Answer for {where}: the session is cold; clear it and send '{line}'."}[action]
        try:
            ev = {"kind": "question.resume", "summary": text, "data": {"questions": [q["id"] for q in qs], **block}}
            if qs[0].get("node") and project:
                ev.update(project=project, node=qs[0]["node"])
            store.append_event(WRITER, ev)
        except Exception:  # noqa: BLE001 - an event is a trace, never a reason to lose the routing
            pass
    return done


def resume_header(store, project, node_id) -> str:
    """Opening lines for a fresh worker on a node whose answers were routed ``fresh``; empty when there are none."""
    try:
        questions = store.list_entities("questions")
    except Exception:  # noqa: BLE001
        return ""
    mine = [q for q in questions if isinstance(q, dict) and q.get("project") == project and q.get("node") == node_id
            and isinstance(q.get("resume"), dict) and q["resume"].get("action") == "restart_node"]
    if not mine:
        return ""
    mine.sort(key=lambda q: str(q.get("answered_at") or ""))
    lines = [resume_line(project), "", "## Answers from Owner (Waypoint)"]
    for q in mine:
        ans = q.get("answer")
        ans = ans if isinstance(ans, str) else json.dumps(ans, ensure_ascii=False)
        lines.append(f"- {q.get('prompt')} -> {ans} (questions/{q['id']}.json)")
    return "\n".join(lines) + "\n\n"
