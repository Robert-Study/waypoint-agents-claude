"""Subagent chats: the workers that orchestration models dispatched, so Owner can read and answer them.

Two read-only sources:

- dsh's ticket server (``dispatch_subagent_ticket``): ``<tickets_dir>/tickets.json`` maps ticket id to
  ``{runner, pid, status (working|awaiting-review|failed|stopped), created_at, cwd, branch, log_path,
  prompt_preview, receipt?, model?}``; the worker's stdout is the ticket's log. Tickets are one-shot, so
  only a ``claude`` ticket whose log holds Claude's JSON result (with a ``session_id``) can be resumed.
- Meridian dispatch jobs: ``<data>/jobs/<project>/<node>/<n>/`` with ``spec.json`` (route, argv, cwd),
  ``launch.json``, ``prompt.md``, ``out.txt``, ``exit.json`` / ``killed.json``. A ``claude_code`` job ran
  ``claude -p --output-format json``, so ``out.txt`` carries the ``session_id`` to resume.

Routes (extension module, ``api.EXTENSIONS``):

- ``GET /api/subagent_chats[?agent=claude_code|codex]`` -> ``{"chats": [{id, source, title, agent, agent_name, model, started, status
  (running|done|error|stopped), output, prompt, cwd, resumable, resume_note, agent_session}]}``, newest first.
- ``POST /api/subagent_chats/reply {id, message}`` (owner only) resumes the Claude Code session with
  ``claude -p --resume <id>`` (or a Codex thread with ``codex exec resume``, which still needs
  ``confirm: true``) through the Agents runner (``agents.p_send``). The first reply copies the
  original prompt and output into an Agents session so the whole conversation reads in one place.

Nothing here writes to the tickets folder or the job folders. Every location and the pid check are
injectable; tests use tmp_path and fakes only.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import agents, dispatch
from . import config

TICKETS_DIR = Path(config.DSH_DIR) / "subagent_tickets"
LIMIT = 60
OUTPUT_CHARS = 20000
PROMPT_CHARS = 4000
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
RUNNER_AGENT = {"claude": "claude_code", "claude_code": "claude_code", "codex": "codex"}
RESUMABLE_AGENTS = ("claude_code", "codex")
TICKET_STATUS = {"working": "running", "awaiting-review": "done", "failed": "error", "stopped": "stopped"}
JOB_ROUTES = ("claude_code", "codex", "freebuff")


def _http_error(status, body):
    from .api import HttpError  # lazy: api loads this module through EXTENSIONS
    return HttpError(status, body)


def _read_text(path, limit=OUTPUT_CHARS, tail=True):
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except (OSError, TypeError, ValueError):
        return ""
    text = ANSI_RE.sub("", text).strip()
    return (text[-limit:] if tail else text[:limit]) if len(text) > limit else text


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


def _iso(epoch):
    try:
        return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _epoch(iso):
    try:
        d = datetime.fromisoformat(str(iso))
    except ValueError:
        return 0.0
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def claude_result(text):
    """``(session_id, result_text, model)`` from ``claude -p --output-format json`` output, else Nones."""
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None, None, None
    if isinstance(obj, list):  # verbose form: a list of events
        obj = next((e for e in reversed(obj) if isinstance(e, dict) and e.get("type") == "result"), None)
    if not isinstance(obj, dict) or not (obj.get("session_id") or obj.get("result")):
        return None, None, None
    usage = obj.get("modelUsage")
    model = next(iter(usage), None) if isinstance(usage, dict) and usage else None
    result = obj.get("result")
    return obj.get("session_id") or None, result if isinstance(result, str) else None, model


def codex_result(text):
    """``(thread_id, last agent message, model)`` from ``codex exec --json`` JSONL output, else Nones."""
    thread, reply = None, None
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "thread.started" and ev.get("thread_id"):
            thread = ev["thread_id"]
        item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
        if ev.get("type") == "item.completed" and item.get("type") in ("agent_message", "assistant_message"):
            reply = item.get("text") or reply
    return thread, reply, None


def _lines(text):
    return len(str(text).splitlines()) if text else 0


def _jsonl(text):
    for line in str(text or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict):
                yield ev


def _int(v):
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def _add_file(files, path, added, removed):
    if not path or not isinstance(path, str):
        return
    f = files.setdefault(path, {"path": path, "added": 0, "removed": 0})
    f["added"] += added
    f["removed"] += removed


def stream_summary(agent, text):
    """Parse ``claude --output-format stream-json`` / ``codex exec --json`` lines (bad lines skipped) into
    ``{session, reply, steps: [{text, files?}], files: [{path, added, removed}]}``."""
    session, reply, steps, files = None, None, [], {}
    if agent == "codex":
        for ev in _jsonl(text):
            if ev.get("type") == "thread.started" and ev.get("thread_id"):
                session = ev["thread_id"]
            item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
            if ev.get("type") != "item.completed":
                continue
            kind = item.get("type")
            if kind in ("agent_message", "assistant_message"):
                reply = item.get("text") or reply
            elif kind == "command_execution":
                steps.append({"text": "Ran " + str(item.get("command") or "")[:120]})
            elif kind == "file_change":
                changes = [c for c in (item.get("changes") or []) if isinstance(c, dict)]                     if isinstance(item.get("changes"), list) else []
                paths = [c.get("path") for c in changes]
                for c in changes:  # Codex usually gives paths only; use counts when it sends them
                    _add_file(files, c.get("path"), _int(c.get("additions", c.get("added"))),
                              _int(c.get("deletions", c.get("removed"))))
                steps.append({"text": "Edited " + ", ".join(x for x in paths if x)})
            elif kind:
                steps.append({"text": str(kind)})
    else:
        for ev in _jsonl(text):
            session = ev.get("session_id") or session
            if ev.get("type") == "result" and isinstance(ev.get("result"), str):
                reply = ev["result"]
            msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
            content = msg.get("content")
            for block in (content if isinstance(content, list) else []):
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name, args = str(block.get("name") or "tool"), (block.get("input") if isinstance(block.get("input"), dict) else {})
                path = args.get("file_path") or args.get("path") or args.get("notebook_path")
                if name in ("Edit", "MultiEdit") and path:
                    edits = args.get("edits") or [args]
                    a = sum(_lines(e.get("new_string")) for e in edits if isinstance(e, dict))
                    r = sum(_lines(e.get("old_string")) for e in edits if isinstance(e, dict))
                    _add_file(files, path, a, r)
                    steps.append({"text": f"{name} {path} +{a} -{r}", "files": [{"path": path, "added": a, "removed": r}]})
                elif name == "Write" and path:
                    a = _lines(args.get("content"))
                    _add_file(files, path, a, 0)
                    steps.append({"text": f"Write {path} +{a} -0", "files": [{"path": path, "added": a, "removed": 0}]})
                else:
                    detail = path or args.get("command") or args.get("pattern") or ""
                    steps.append({"text": (name + " " + str(detail)).strip()[:140]})
    return {"session": session, "reply": reply, "steps": steps, "files": list(files.values())}


def resume_argv(agent, session_id, message):
    """The CLI argv that resumes a past session, or None for agents that cannot."""
    if agent == "claude_code":
        return ["claude", "-p", "--resume", str(session_id), "--output-format", "stream-json", "--verbose",
                "--include-partial-messages", "--", str(message)]
    if agent == "codex":
        return ["codex", "exec", "resume", str(session_id), "--json", "--", str(message)]
    return None


def session_result(agent, text):
    if agent == "claude_code":
        res = claude_result(text)
        if res[0] is None and res[1] is None:
            s = stream_summary(agent, text)
            return s["session"], s["reply"], None
        return res
    if agent == "codex":
        return codex_result(text)
    return None, None, None


def _first_line(text, limit=70):
    line = next((ln.strip() for ln in str(text or "").splitlines() if ln.strip()), "")
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _argv_value(argv, flag):
    argv = [str(a) for a in argv or []]
    return argv[argv.index(flag) + 1] if flag in argv[:-1] else None


def _note(agent, status, session_id, source):
    """``(resumable, one-line reason)``."""
    if status == "running":
        return False, "Still running; reply once it has finished."
    if agent in RESUMABLE_AGENTS:
        if session_id:
            return True, None
        name = "Claude" if agent == "claude_code" else "Codex"
        return False, f"No {name} session id was recorded for this job."
    if source == "ticket":
        return False, "This ticket worker ran once and cannot be resumed."
    return False, f"{agents.AGENTS.get(agent, {}).get('name') or agent} jobs cannot be resumed."


def _entry(**kw):
    agent = kw["agent"]
    return {"id": kw["id"], "source": kw["source"], "title": kw["title"], "agent": agent,
            "agent_name": (agents.AGENTS.get(agent) or {}).get("name") or agent, "model": kw.get("model"),
            "started": kw.get("started"), "status": kw["status"], "output": kw.get("output") or "",
            "prompt": (kw.get("prompt") or "")[:PROMPT_CHARS], "cwd": kw.get("cwd"),
            "resumable": kw["resumable"], "resume_note": kw["resume_note"], "claude_session": kw.get("session_id"),
            "agent_session": None, "raw": kw.get("raw") or ""}


def ticket_chats(tickets_dir, pid_alive):
    data = _read_json(Path(tickets_dir) / "tickets.json")
    if not isinstance(data, dict):
        return []
    out = []
    for tid, info in data.items():
        if not isinstance(info, dict):
            continue
        runner = str(info.get("runner") or "").lower()
        agent = RUNNER_AGENT.get(runner, runner or "unknown")
        status = TICKET_STATUS.get(str(info.get("status") or ""), "done")
        log = _read_text(info.get("log_path"))
        if status == "running" and not pid_alive(info.get("pid")):
            status = "done" if log else "error"
        session_id, result, model = session_result(agent, log)
        output = (info.get("receipt") or "") if info.get("receipt") else (result if result is not None else log)
        resumable, note = _note(agent, status, session_id, "ticket")
        prompt = info.get("prompt_preview") or ""
        out.append(_entry(id=f"ticket:{tid}", source="ticket", title=_first_line(prompt) or str(tid), agent=agent,
                          model=info.get("model") or model, started=info.get("created_at"), status=status,
                          output=output[-OUTPUT_CHARS:], prompt=prompt, cwd=info.get("cwd"), resumable=resumable,
                          resume_note=note, session_id=session_id, raw=log[-OUTPUT_CHARS:]))
    return out


def job_chats(data_dir, pid_alive):
    base = Path(data_dir) / "jobs"
    out = []
    if not base.is_dir():
        return out
    for spec_file in base.glob("*/*/*/spec.json"):
        job = spec_file.parent
        spec = _read_json(spec_file)
        if not isinstance(spec, dict) or spec.get("route") not in JOB_ROUTES:
            continue
        project, node = job.parent.parent.name, job.parent.name
        launch = _read_json(job / "launch.json") or {}
        exit_info = _read_json(job / "exit.json")
        killed = (job / "killed.json").exists()
        if killed:
            status = "stopped"
        elif exit_info is None:
            status = "running" if pid_alive(launch.get("pid"), launch.get("started_at")) else "error"
            if status == "error" and not launch:
                status = "running"  # launched but not recorded yet
        else:
            status = "done" if (exit_info or {}).get("exit_code") == 0 else "error"
        raw = _read_text(job / "out.txt")
        if not raw and status == "error":
            raw = _read_text(job / "err.txt", 4000)
        agent = spec["route"]
        session_id, result, model = session_result(agent, raw)
        model = model if model else (_argv_value(spec.get("argv"), "--model") or _argv_value(spec.get("argv"), "-m") or
                                     ((spec.get("body") or {}).get("model") if isinstance(spec.get("body"), dict) else None))
        resumable, note = _note(agent, status, session_id, "job")
        out.append(_entry(id=f"job:{project}/{node}/{job.name}", source="job", title=f"{node} · {project}",
                          agent=agent, model=model, started=_iso(launch.get("started_at") or spec.get("created_at")),
                          status=status, output=(result if result is not None else raw)[-OUTPUT_CHARS:],
                          prompt=_read_text(job / "prompt.md", PROMPT_CHARS, tail=False), cwd=spec.get("cwd"),
                          resumable=resumable, resume_note=note, session_id=session_id, raw=raw[-OUTPUT_CHARS:]))
    return out


def session_list(data_dir, tickets_dir=None, store=None, pid_alive=None, agent=None, limit=LIMIT):
    """Past sessions for one agent from tickets and jobs, with parsed steps/files and the resume session id."""
    out = []
    for c in list_chats(data_dir, tickets_dir, store, pid_alive, 10 ** 6):
        if agent and c["agent"] != agent:
            continue
        summ = stream_summary(c["agent"], c.get("raw") or "")
        out.append({"id": c["id"], "title": c["title"], "agent": c["agent"], "started": c["started"],
                    "status": c["status"], "cwd": c["cwd"], "resumable": c["resumable"],
                    "session": c.get("claude_session"), "steps": summ["steps"], "files": summ["files"]})
        if len(out) >= limit:
            break
    return out


def _link_sessions(chats, store):
    if store is None or not chats:
        return
    try:
        rows = agents._rows(store, "SELECT id, agent, ext_id FROM agent_sessions WHERE ext_id IS NOT NULL")
    except Exception:
        return
    by_ext = {(r["agent"], r["ext_id"]): r["id"] for r in rows}
    for c in chats:
        c["agent_session"] = by_ext.get((c["agent"], c.get("claude_session")))


def list_chats(data_dir, tickets_dir=None, store=None, pid_alive=None, limit=LIMIT):
    pid_alive = pid_alive or dispatch.pid_alive

    def alive(pid, started=None):
        try:
            return bool(pid) and bool(pid_alive(pid, started))
        except Exception:
            return False

    chats = []
    for source in (lambda: ticket_chats(tickets_dir or TICKETS_DIR, alive), lambda: job_chats(data_dir, alive)):
        try:
            chats += source()
        except Exception:
            continue  # one unreadable source never hides the other
    chats.sort(key=lambda c: (_epoch(c["started"]), c["id"]), reverse=True)
    chats = chats[:limit]
    _link_sessions(chats, store)
    return chats


# ---------------------------------------------------------------- HTTP
def _paths(h):
    app = h.app
    data = getattr(app, "subagent_data_dir", None) or Path(app.store.path).parent
    return Path(data), getattr(app, "tickets_dir", None) or TICKETS_DIR, getattr(app, "pid_alive", None)


def _chats(h, limit=LIMIT):
    data, tickets, pid_alive = _paths(h)
    return list_chats(data, tickets, h.app.store, pid_alive, limit)


def g_chats(h, q):
    chats = _chats(h)
    if q.get("agent"):
        chats = [c for c in chats if c["agent"] == q["agent"]]
    for c in chats:
        c.pop("claude_session", None)
        summ = stream_summary(c["agent"], c.pop("raw", ""))
        c["steps"], c["files"] = summ["steps"], summ["files"]
    return {"chats": chats}


def g_sessions(h, q):
    data, tickets, pid_alive = _paths(h)
    return {"sessions": session_list(data, tickets, h.app.store, pid_alive, q.get("agent"))}


class _Body:
    """The request object ``agents.p_send`` expects, carrying the Agents-shaped body."""

    def __init__(self, h, body):
        self._h, self._body = h, body

    def _json_body(self):
        return self._body

    def __getattr__(self, name):
        return getattr(self._h, name)


def p_reply(h, q):
    if getattr(h, "actor", None) != "owner":
        raise _http_error(403, {"error": "owner_only", "detail": "only Owner may reply to a subagent"})
    body = h._json_body()
    chat_id, message = body.get("id"), str(body.get("message") or "").strip()
    if not chat_id or not message:
        raise _http_error(400, {"error": "bad_request", "detail": "missing id or message"})
    chat = next((c for c in _chats(h, limit=10 ** 6) if c["id"] == chat_id), None)
    if chat is None:
        raise _http_error(404, {"error": "not_found", "detail": "no such subagent chat"})
    if not chat["resumable"]:
        raise _http_error(409, {"error": "not_resumable", "detail": chat["resume_note"]})
    store = h.app.store
    sid = chat["agent_session"]
    if not sid:
        session = agents.create_session(store, chat["agent"], chat["cwd"], chat["title"])
        sid = session["id"]
        seed = [(role, text) for role, text in (("user", chat["prompt"]), ("assistant", chat["output"])) if text]
        agents.add_messages(store, sid, "seed", seed, ext_id=chat["claude_session"])
    send = {"agent": chat["agent"], "session_id": sid, "message": message}
    for key in ("model", "effort", "confirm"):
        if body.get(key):
            send[key] = body[key]
    res = agents.p_send(_Body(h, send), q)
    return dict(res, chat=chat_id)


ROUTES = {
    "GET /api/subagent_chats": g_chats,
    "POST /api/subagent_chats/reply": p_reply,
}
