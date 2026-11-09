"""Lead notice: every Waypoint Claude page write is journalled once, so the lead session can be told.

Owner's page actions (answer, todo_tick, snooze, floor_set, toggle_set, pref_approve, pref_reject, pref_edit) all
reach the tools through ``mcp_claude.handle`` (the ``waypoint_claude`` connector). After a call that succeeded, one line
goes to ``events/page/<day>.ndjson`` (kind ``page.<tool>``, ``data.tool``, ``data.key``, a plain summary). The mod in the
lead session (claude_mod/waypoint/hooks/lead_notice.ts) batches the lines after its cursor into one prompt.

The lead session is named in ``settings/settings.json`` ``lead_session`` = ``{id, cli_session_id, set_at}``. The lead
sets it at start: ``python -m waypoint_runner.lead_notice set <its desktop or CLI session id>``. A desktop id
(``local_<uuid>``) is resolved to the CLI id the mod sees (``$.session.id()``, the transcript name) through the desktop
app's own session file. ``set`` also starts the cursor (``settings/lead_notice.json``) at the journal's end.

The dsh page (wpc_http.py) calls the tool modules directly; it must call ``record`` the same way to be covered.
No model is called. ``record`` never raises into a tool reply.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import time
from pathlib import Path

_PY = Path(__file__).resolve().parents[2] / "core" / "waypoint-data" / "py"
if _PY.is_dir() and str(_PY) not in sys.path:
    sys.path.insert(0, str(_PY))

from waypoint_data.store import Store, iso  # noqa: E402

PAGE_TOOLS = ("answer", "todo_tick", "snooze", "floor_set", "toggle_set", "pref_approve", "pref_reject", "pref_edit")
WRITER = "page"
SETTINGS = "settings/settings.json"
CURSOR = "settings/lead_notice.json"
_CUT = 200


def _cut(s, n=_CUT):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def key_of(tool, args):
    """What the action was on: the question (or project/node), the pool, else the id."""
    args = args if isinstance(args, dict) else {}
    if tool == "answer":
        q = args.get("question")
        return str(q) if q else f"{args.get('project')}/{args.get('node')}"
    if tool == "floor_set":
        return str(args.get("pool"))
    return str(args.get("id"))


def summarize(tool, args, payload):
    """One plain clause: what Owner did."""
    args = args if isinstance(args, dict) else {}
    p = payload if isinstance(payload, dict) else {}
    k = key_of(tool, args)
    if tool == "answer":
        return _cut(f"answered question {p.get('question') or k}: {p.get('answer', '')}")
    if tool == "todo_tick":
        return _cut(f"ticked to-do {k}" + (f" ({p['text']})" if p.get("text") else ""))
    if tool == "snooze":
        until = args.get("until")
        what = "question" if args.get("kind") == "question" else "to-do"
        return _cut((f"snoozed {what} {k} until {until}" if until else f"brought back {what} {k}")
                    + (f" ({p['text']})" if p.get("text") else ""))
    if tool == "floor_set":
        v = args.get("min_percent")
        return f"set the {k} floor to {'unset' if v is None else str(v) + '%'}"
    if tool == "toggle_set":
        return f"switched {k} {'on' if args.get('enabled') else 'off'}"
    verb = {"pref_approve": "approved", "pref_reject": "rejected", "pref_edit": "edited"}.get(tool, tool)
    return f"{verb} preference {k}"


def record(store, tool, args, payload):
    """Append one journal line for a page write that succeeded."""
    return store.append_event(WRITER, {"kind": "page." + tool, "summary": summarize(tool, args, payload),
                                       "data": {"tool": tool, "key": key_of(tool, args)}})


def after_call(board, message, response):
    """mcp_claude.handle's hook: journal a successful page-type tools/call. Never raises."""
    try:
        params = message.get("params") or {}
        tool = str(params.get("name", ""))
        if message.get("method") != "tools/call" or tool not in PAGE_TOOLS:
            return
        res = (response or {}).get("result") or {}
        if res.get("isError") or "error" in (response or {}):
            return
        try:
            payload = json.loads(res["content"][0]["text"])
        except Exception:  # noqa: BLE001 - a summary without the reply is still worth a line
            payload = {}
        record(board.store, tool, params.get("arguments") or {}, payload)
    except Exception as exc:  # noqa: BLE001 - the journal never breaks a tool reply
        sys.stderr.write(f"lead_notice: not journalled: {exc!r}\n")


# ---------------------------------------------------------------- naming the lead session
def cli_session_id(session_id, roots=None):
    """The CLI session id of a desktop session ``local_<uuid>`` (its ``cliSessionId``), or None."""
    from . import resume
    if not session_id:
        return None
    for root in (resume._desktop_roots() if roots is None else roots):
        for dirpath, dirs, files in os.walk(root):
            if dirpath[len(str(root)):].count(os.sep) >= 3:
                dirs[:] = []
            if f"{session_id}.json" in files:
                try:
                    d = json.loads(Path(dirpath, f"{session_id}.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                c = d.get("cliSessionId") if isinstance(d, dict) else None
                if isinstance(c, str) and c:
                    return c
    return None


def journal_end(root):
    """``{day, line}``: the newest journal day and its line count (``line`` 0 with no journal)."""
    folder = Path(root) / "events" / WRITER
    days = sorted(p.stem for p in folder.glob("*.ndjson")) if folder.is_dir() else []
    if not days:
        return {"day": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d"), "line": 0}
    text = (folder / f"{days[-1]}.ndjson").read_text("utf-8", errors="replace")
    return {"day": days[-1], "line": text.count("\n")}


def _settings_write(store, change):
    cur = store.read(SETTINGS) or {}
    body = {k: v for k, v in cur.items() if k not in ("rev", "updated_at", "updated_by")}
    change(body)
    return store.write(SETTINGS, body, expected_rev=cur.get("rev") if cur else 0)


def set_lead(root, session_id, now=time.time, roots=None):
    """Name the lead session and start its notice cursor at the journal's end."""
    sid = str(session_id or "").strip()
    if not sid:
        raise ValueError("a session id is needed")
    store = Store(str(root), writer="cli", now=now)
    cli = cli_session_id(sid, roots) if sid.startswith("local_") else None
    lead = {"id": sid, "cli_session_id": cli, "set_at": iso(now())}
    _settings_write(store, lambda b: b.__setitem__("lead_session", lead))
    cur = store.read(CURSOR)
    end = journal_end(root)
    body = {"schema": "waypoint.lead_notice.v1", **end, "session": sid, "delivered_at": None, "count": 0}
    store.write(CURSOR, body, expected_rev=cur.get("rev") if cur else 0)
    return {"lead_session": lead, "cursor": end}


def clear_lead(root, now=time.time):
    store = Store(str(root), writer="cli", now=now)
    _settings_write(store, lambda b: b.pop("lead_session", None))
    return {"lead_session": None}


def main(argv=None):
    p = argparse.ArgumentParser(prog="waypoint_runner.lead_notice",
                                description="Name the lead session that gets Waypoint Claude page notices.")
    p.add_argument("--folder", help="data folder (default: WAYPOINT_DATA_DIR, then ~/.waypoint/data-dir pointer)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set", help="name this session the lead (desktop local_<uuid> or CLI id)")
    s.add_argument("session")
    sub.add_parser("clear", help="no lead session")
    sub.add_parser("show", help="print the lead setting and the cursor")
    a = p.parse_args(argv)
    if a.folder:
        folder = a.folder
    else:
        from .lifecycle import default_data_dir
        folder = default_data_dir()
    if a.cmd == "set":
        out = set_lead(folder, a.session)
    elif a.cmd == "clear":
        out = clear_lead(folder)
    else:
        st = Store(str(folder))
        out = {"lead_session": (st.read(SETTINGS) or {}).get("lead_session"), "cursor": st.read(CURSOR)}
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
