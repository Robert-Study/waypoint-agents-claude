"""The agent tree and meters (Waypoint Core 1.1). Zero tokens: no model is ever called.

Three sources feed one ``agents`` table:

- hook events posted by the mod (``POST /api/trace``): they own ``status`` and ``tool``;
- transcript meters (``transcripts.Tailer``): they own token meters, model, context and cost;
- desktop session files (``desktop_sessions``): they own the parent link and, unless a hook gave one,
  the title.

Parent order in the tree: explicit link (``POST /api/trace/link``), desktop link, hook ``parent``. The first
candidate that is in the result wins; a row with no candidate in the result is a root. Telemetry never
bumps the plan revision. Limits: 50 events per post, 24 hours of history by default, one tail pass and
one desktop pass every 15 seconds.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from . import desktop_sessions, transcripts
from .api import HttpError, _need

EVENTS = ("session.start", "session.end", "turn.complete", "tool.call", "subagent.start", "subagent.stop",
          "heartbeat")
MAX_EVENTS = 50
STALE_AFTER = timedelta(minutes=10)
HOOK_FRESH = timedelta(seconds=120)
INTERVAL = 15.0
MAX_PUSH = 200
KINDS = ("session", "subagent", "headless")
STATUSES = ("running", "idle", "waiting", "done", "error", "ended")
TERMINAL = ("done", "error", "ended")

# Public row shape (shared brief). Order matters only for readability.
VIEW_FIELDS = ("id", "kind", "parent", "session_id", "title", "project", "node", "cwd", "status", "tool",
               "tokens_in", "tokens_out", "cache_read", "context", "context_window", "cost_eq", "model",
               "last_line", "transcript", "started_at", "updated_at")
INTERNAL = ("source", "link_parent", "desk_parent", "hook_parent", "hook_title", "desk_title", "tr_title",
            "hook_at", "tr_seen", "desk_archived", "cache_creation")
COLS = VIEW_FIELDS + INTERNAL
INT_COLS = ("tokens_in", "tokens_out", "cache_read", "context", "context_window", "tr_seen", "desk_archived",
            "cache_creation")

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents(
  id TEXT PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'session', parent TEXT, session_id TEXT, title TEXT,
  project TEXT, node TEXT, cwd TEXT, status TEXT NOT NULL DEFAULT 'idle', tool TEXT,
  tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0, cache_read INTEGER DEFAULT 0,
  context INTEGER DEFAULT 0, context_window INTEGER DEFAULT 0, cost_eq REAL, model TEXT, last_line TEXT,
  transcript TEXT, started_at TEXT, updated_at TEXT, source TEXT, link_parent TEXT, desk_parent TEXT,
  hook_parent TEXT, hook_title TEXT, desk_title TEXT, tr_title TEXT, hook_at TEXT,
  tr_seen INTEGER DEFAULT 0, desk_archived INTEGER DEFAULT 0, cache_creation INTEGER DEFAULT 0)
"""
INDEX = "CREATE INDEX IF NOT EXISTS agents_updated ON agents(updated_at)"


def ensure_schema(store):
    if getattr(store, "_trace_schema_ok", False):
        return
    with store.tx() as c:
        c.execute(SCHEMA)
        c.execute(INDEX)
    store._trace_schema_ok = True


# ---------------------------------------------------------------- time
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(stamp):
    try:
        t = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _norm(stamp):
    t = _parse(stamp) if stamp else None
    return t.astimezone(timezone.utc).isoformat(timespec="seconds") if t else None


def _stamp(dt=None) -> str:
    return (dt or _now()).astimezone(timezone.utc).isoformat(timespec="seconds")


def _later(a, b):
    """The later of two normalised stamps (either may be None)."""
    return max(x for x in (a, b) if x) if (a or b) else None


# ---------------------------------------------------------------- row storage
def _get(c, rid):
    r = c.execute("SELECT * FROM agents WHERE id=?", (rid,)).fetchone()
    return {k: r[k] for k in COLS} if r else None


def _blank(rid, kind="session", session_id=None, source="hook") -> dict:
    row = {k: None for k in COLS}
    row.update(id=rid, kind=kind, session_id=session_id or rid.split("/")[0], status="idle", source=source)
    for k in INT_COLS:
        row[k] = 0
    return row


def _put(c, row):
    c.execute(f"INSERT OR REPLACE INTO agents({','.join(COLS)}) VALUES({','.join('?' * len(COLS))})",
              [row[k] for k in COLS])


def upsert(store, rid, **fields):
    """Create or update one row (used by other modules, e.g. headless dispatch). Returns the view."""
    ensure_schema(store)
    with store.tx() as c:
        row = _get(c, rid) or _blank(rid, fields.get("kind", "session"), fields.get("session_id"), "api")
        row.update({k: v for k, v in fields.items() if k in COLS})
        row["updated_at"] = _norm(fields.get("updated_at")) or row["updated_at"] or _stamp()
        _put(c, row)
    return _view(row)


def _view(row, now=None) -> dict:
    now = now or _now()
    out = {k: row.get(k) for k in VIEW_FIELDS}
    out["title"] = row.get("hook_title") or row.get("desk_title") or row.get("tr_title") or row.get("title")
    status = row.get("status") or "idle"
    if row.get("desk_archived") and status not in ("error", "ended"):
        status = "ended"
    elif status in ("running", "waiting"):
        seen = _parse(row.get("updated_at"))
        if seen is None or now - seen > STALE_AFTER:
            status = "idle"
    out["status"] = status
    if status in TERMINAL:
        out["tool"] = None
    return out


# ---------------------------------------------------------------- hook events
def _txt(v, limit=500):
    return v.strip()[:limit] if isinstance(v, str) and v.strip() else None


def _nonneg(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else None


def _row_id(ev):
    sid = _txt(ev.get("session_id"), 200)
    if not sid:
        return None, None
    aid = _txt(ev.get("agent_id"), 200)
    return (f"{sid}/{aid}" if aid else sid), sid


def _clamp(stamp, now):
    """A client's ``at``, never later than now + 60 s (a future time would pin a row as running)."""
    t = _parse(stamp) if stamp else None
    if t is None or t > now + timedelta(seconds=60):
        return _stamp(now)
    return _stamp(t)


def _apply_event(c, ev, now):
    name = ev.get("event")
    rid, sid = _row_id(ev)
    if name not in EVENTS or rid is None:
        return None
    sub = "/" in rid
    row = _get(c, rid)
    at = _clamp(ev.get("at"), now)
    if row is None:
        row = _blank(rid, "subagent" if sub else "session", sid, "hook")
        row["started_at"] = at
        if sub:
            row["hook_parent"] = sid
    for key, col in (("cwd", "cwd"), ("model", "model"), ("transcript", "transcript"),
                     ("project", "project"), ("node", "node")):
        v = _txt(ev.get(key), 1000)
        if v:
            row[col] = v
    title = _txt(ev.get("title"), 200)
    if title:
        row["hook_title"] = title
    parent = _txt(ev.get("parent"), 200)
    if parent and parent != rid and not sub:
        row["hook_parent"] = parent
    row["hook_at"], row["updated_at"] = at, _later(row["updated_at"], at)
    if name == "session.start":
        row.update(status="running", tool=None)
    elif name == "turn.complete":
        row.update(status="idle", tool=None)
        u = ev.get("usage") if isinstance(ev.get("usage"), dict) else {}
        if not row["tr_seen"]:  # transcripts are exact; hook usage only fills in until they arrive
            ctx, win, cost = _nonneg(u.get("context_tokens")), _nonneg(u.get("context_window")), \
                _nonneg(u.get("cost_usd"))
            if ctx is not None:
                row["context"] = int(ctx)
            if win:
                row["context_window"] = int(win)
            if cost is not None:
                row["cost_eq"] = float(cost)
    elif name == "tool.call":
        row.update(status="running", tool=_txt(ev.get("tool"), 120))
    elif name == "subagent.start":
        row.update(status="running", tool=None, kind="subagent")
        row["hook_parent"] = sid
    elif name == "subagent.stop":
        row.update(status="done", tool=None)
    elif name == "session.end":
        row.update(status="ended", tool=None)
    _put(c, row)
    return row


def record_events(store, events) -> dict:
    """Apply up to 50 hook events. Returns ``{"ok": True, "accepted": n}``. Unknown events are ignored."""
    ensure_schema(store)
    if not isinstance(events, list):
        raise ValueError("events must be a list")
    now, changed, accepted = _now(), {}, 0
    with store.tx() as c:
        for ev in events[:MAX_EVENTS]:
            if not isinstance(ev, dict):
                continue
            row = _apply_event(c, ev, now)
            if row:
                accepted += 1
                changed[row["id"]] = row
    if changed:
        store.notify("trace", {"agents": [_view(r, now) for r in changed.values()]})
    return {"ok": True, "accepted": accepted}


def link(store, child, parent):
    """Remember an explicit parent for ``child`` (the fallback when desktop data has no link)."""
    ensure_schema(store)
    child, parent = _txt(child, 200), _txt(parent, 200)
    if not child or not parent or child == parent:
        raise ValueError("child and parent must be two different ids")
    with store.tx() as c:
        row = _get(c, child) or _blank(child, "subagent" if "/" in child else "session", None, "link")
        row["link_parent"] = parent
        row["updated_at"] = row["updated_at"] or _stamp()
        _put(c, row)
    store.notify("trace", {"agents": [_view(row)]})
    return {"ok": True, "child": child, "parent": parent}


# ---------------------------------------------------------------- merges
def _hook_fresh(row, now) -> bool:
    t = _parse(row.get("hook_at"))
    return t is not None and now - t < HOOK_FRESH


def merge_transcripts(store, tailer, paths=None) -> list:
    """Fold transcript meters into rows. Hooks keep ``status``; transcripts own the meters. Returns the ids
    that changed."""
    ensure_schema(store)
    now, changed = _now(), []
    snap = tailer.snapshot(paths)
    if not snap:
        return changed
    with store.tx() as c:
        for key, m in snap.items():
            row = _get(c, key)
            before = dict(row) if row else None
            if row is None:
                row = _blank(key, m["kind"], m["session_id"], "transcript")
                if m["kind"] == "subagent":
                    row["hook_parent"] = m["session_id"]
                row["started_at"] = _norm(m["started_at"])
            row["kind"] = m["kind"] if row["kind"] != "headless" else "headless"
            row.update(tokens_in=m["tokens_in"], tokens_out=m["tokens_out"], cache_read=m["cache_read"],
                       cache_creation=m["cache_creation"], context=m["context"],
                       context_window=m["context_window"], cost_eq=m["cost_eq"], tr_seen=1)
            for col, k in (("model", "model"), ("last_line", "last_line"), ("transcript", "transcript")):
                if m[k]:
                    row[col] = m[k]
            row["cwd"] = row["cwd"] or m["cwd"]
            row["started_at"] = row["started_at"] or _norm(m["started_at"])
            if m["kind"] == "subagent":
                row["tr_title"] = _txt(m["description"], 200) or _txt(m["agent_type"], 200) or row["tr_title"]
            elif m["title"]:
                row["tr_title"] = m["title"]
            row["updated_at"] = _later(row["updated_at"], _norm(m["updated_at"]))
            fresh = _hook_fresh(row, now)
            if not fresh:
                row["tool"] = m["tool"]
            if row["hook_at"] is None and row["status"] not in TERMINAL:
                seen = _parse(row["updated_at"])
                live = seen is not None and now - seen < HOOK_FRESH
                row["status"] = "running" if (live and m["tool"]) else "idle"
            if m["kind"] == "subagent" and m["finished"] and row["status"] not in TERMINAL:
                row.update(status="done", tool=None)
            if row != before:
                _put(c, row)
                changed.append(key)
    return changed


def merge_desktop(store, links) -> list:
    """Fold desktop parent links, titles and archived flags into rows that already exist."""
    ensure_schema(store)
    changed = []
    with store.tx() as c:
        for cli, d in (links or {}).items():
            row = _get(c, cli)
            if row is None or not isinstance(d, dict):
                continue
            before = dict(row)
            parent = d.get("parent_cli_session_id")
            row["desk_parent"] = parent if parent and parent != cli else None
            row["desk_title"] = _txt(d.get("title"), 200)
            row["desk_archived"] = 1 if d.get("archived") else 0
            row["cwd"] = row["cwd"] or _txt(d.get("cwd"), 1000)
            if row != before:
                _put(c, row)
                changed.append(cli)
    return changed


# ---------------------------------------------------------------- reads
def tree(store, since=None) -> dict:
    """``{agents, roots, at}``: rows active since ``since`` (default 24 h) plus every running row."""
    ensure_schema(store)
    now = _now()
    cutoff = _stamp(since if isinstance(since, datetime) else (_parse(since) if since else now - timedelta(hours=24)))
    rows = store._q("SELECT * FROM agents WHERE updated_at >= ? OR status = 'running' ORDER BY updated_at DESC",
                    (cutoff,))
    picked = {}
    for r in rows:
        row = {k: r[k] for k in COLS}
        v = _view(row, now)
        if (row["updated_at"] or "") >= cutoff or v["status"] == "running":
            picked[row["id"]] = (row, v)
    # A headless worker's own Claude session is the same agent: show the headless row only.
    shadow = {row["session_id"] for row, _ in picked.values() if row["kind"] == "headless" and row["session_id"]}
    picked = {rid: rv for rid, rv in picked.items() if rid not in shadow}
    agents = []
    for rid, (row, v) in picked.items():
        v["parent"] = next((p for p in (row["link_parent"], row["desk_parent"], row["hook_parent"])
                            if p and p != rid and p in picked), None)
        agents.append(v)
    by_id = {a["id"]: a for a in agents}
    for a in agents:  # break any cycle by cutting the edge that closes it (never a valid parent leading in)
        path, cur = [a["id"]], a["parent"]
        while cur:
            if cur in path:
                by_id[path[-1]]["parent"] = None
                break
            path.append(cur)
            cur = by_id[cur]["parent"]
    roots = [a["id"] for a in agents if a["parent"] is None]
    return {"agents": agents, "roots": roots, "at": _stamp(now)}


def recent_cwds(store, hours=48) -> list:
    ensure_schema(store)
    cutoff = _stamp(_now() - timedelta(hours=hours))
    seen, out = set(), []
    for r in store._q("SELECT cwd FROM agents WHERE cwd IS NOT NULL AND cwd != '' AND updated_at >= ? "
                      "ORDER BY updated_at DESC", (cutoff,)):
        if r[0] not in seen:
            seen.add(r[0])
            out.append(r[0])
    return out


# ---------------------------------------------------------------- background
def _digest(agents) -> dict:
    return {a["id"]: (a["status"], a["tool"], a["tokens_out"], a["context"], a["title"], a["parent"],
                      a["updated_at"], a["last_line"]) for a in agents}


KEEP_DAYS = 14
_PRUNED = {"at": None}


def prune(store, days=KEEP_DAYS, now=None) -> int:
    """Drop agent rows not updated for ``days`` days (the tree shows the last 24 h). Returns rows removed."""
    ensure_schema(store)
    cutoff = _stamp((now or _now()) - timedelta(days=days))
    with store.tx() as c:
        return c.execute("DELETE FROM agents WHERE updated_at < ?", (cutoff,)).rowcount


def tick(store, tailer, roots_fn, last, desktop_interval=INTERVAL) -> list:
    """One pass: tail transcripts, read desktop links, push changed rows. ``last`` is the digest dict that
    the caller keeps between passes. Returns the pushed rows. Old rows are pruned about once an hour."""
    ensure_schema(store)
    t = _now()
    if _PRUNED["at"] is None or t - _PRUNED["at"] > timedelta(hours=1):
        _PRUNED["at"] = t
        try:
            prune(store)
        except Exception:
            pass
    paths = tailer.poll()
    if paths:
        merge_transcripts(store, tailer, set(paths))
    try:
        merge_desktop(store, desktop_sessions.read_links(roots_fn(), min_interval=desktop_interval))
    except Exception:  # desktop data is a nicety; never stop the tail
        pass
    agents = tree(store)["agents"]
    now = _digest(agents)
    moved = [a for a in agents if last.get(a["id"]) != now[a["id"]]]
    last.clear()
    last.update(now)
    if moved:
        store.notify("trace", {"agents": moved[:MAX_PUSH]})
    return moved


class _Handle:
    def __init__(self):
        self.stop_event = threading.Event()
        self.thread = None

    def stop(self):
        self.stop_event.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)


def start(app, interval=INTERVAL, roots=None, desktop_env=None):
    """Daemon thread: tail transcripts and desktop links every ``interval`` seconds."""
    store = app.store
    ensure_schema(store)
    handle = _Handle()
    tailer = transcripts.Tailer(roots=roots)
    last: dict = {}

    def roots_fn():
        return desktop_sessions.find_roots(desktop_env)

    def run():
        while not (app.closing.is_set() or handle.stop_event.is_set()):
            try:
                tick(store, tailer, roots_fn, last, desktop_interval=interval)
            except Exception:  # one bad pass never kills the loop
                pass
            if handle.stop_event.wait(interval) or app.closing.is_set():
                break

    handle.thread = threading.Thread(target=run, name="waypoint-trace", daemon=True)
    handle.thread.start()
    return handle


# ---------------------------------------------------------------- routes
def p_trace(h, q):
    body = h._json_body()
    events = body.get("events")
    if not isinstance(events, list):
        raise HttpError(400, {"error": "bad_request", "detail": "events must be a list"})
    out = record_events(h.app.store, events)
    feed_usage(events, getattr(h.app, "usage_cache_path", None))
    return {"ok": True, "accepted": out["accepted"]}


def feed_usage(events, cache_path=None):
    """The newest ``usage.rate_limits`` in a post (``$.session.usage().rateLimits``) feeds the Claude usage bars.
    Never raises: a bad reading is dropped, the trace post still succeeds."""
    limits = None
    for ev in events[:MAX_EVENTS]:
        u = ev.get("usage") if isinstance(ev, dict) and isinstance(ev.get("usage"), dict) else None
        if u and isinstance(u.get("rate_limits"), list) and u["rate_limits"]:
            limits = (u["rate_limits"], ev.get("at"))
    if not limits:
        return 0
    try:
        from . import usage
        return usage.record_session_rate_limits(limits[0], at=limits[1], cache_path=cache_path)
    except Exception:
        return 0


def p_link(h, q):
    body = h._json_body()
    _need(body, "child", "parent")
    try:
        return link(h.app.store, body["child"], body["parent"])
    except ValueError as exc:
        raise HttpError(400, {"error": "bad_request", "detail": str(exc)})


def g_tree(h, q):
    since = q.get("since")
    if since and _parse(since) is None:
        raise HttpError(400, {"error": "bad_request", "detail": "since must be an ISO time"})
    return tree(h.app.store, since)


ROUTES = {
    "POST /api/trace": p_trace,
    "POST /api/trace/link": p_link,
    "GET /api/trace/tree": g_tree,
}
