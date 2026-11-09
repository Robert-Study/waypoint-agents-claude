"""Subagent chats (A2): dsh tickets and Claude Code dispatch jobs, listed and resumed. Fakes and tmp_path only."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from cc_engine import agents, subagent_chats
from cc_engine.api import HttpError

USAGE = []


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if isinstance(text, str) else json.dumps(text), encoding="utf-8")


def make_job(data, project, node, n, *, route="claude_code", argv=None, out=None, exit_code=0, cwd="C:/work",
             started=1_700_000_000.0, prompt="Do the thing\nmore", exited=True, killed=False):
    job = data / "jobs" / project / node / str(n)
    argv = argv if argv is not None else ["claude", "-p", "--model", "sonnet", "--output-format", "json"]
    write(job / "spec.json", {"kind": "exec", "route": route, "argv": argv, "cwd": cwd, "node": node,
                              "project": project, "attempt": n})
    write(job / "launch.json", {"pid": 11, "started_at": started, "route": route})
    write(job / "prompt.md", prompt)
    if out is not None:
        write(job / "out.txt", out)
    if exited:
        write(job / "exit.json", {"exit_code": exit_code, "started_at": started, "ended_at": started + 60})
    if killed:
        write(job / "killed.json", {"state": "cancelled"})
    return job


CLAUDE_OUT = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "Built the widget.",
                         "session_id": "sess-abc", "modelUsage": {"claude-sonnet-5-5": {"inputTokens": 5}}})


@pytest.fixture
def dirs(tmp_path):
    data, tickets = tmp_path / "data", tmp_path / "tickets"
    data.mkdir()
    tickets.mkdir()
    return data, tickets


def list_(dirs, **kw):
    data, tickets = dirs
    return subagent_chats.list_chats(data, tickets, pid_alive=kw.pop("pid_alive", lambda pid, started=None: False), **kw)


def by_id(chats):
    return {c["id"]: c for c in chats}


def test_subagent_chats_reads_claude_jobs(dirs):
    data, _ = dirs
    make_job(data, "meridian-v3", "A1", 1, out=CLAUDE_OUT, cwd="C:/repo")
    chats = list_(dirs)
    assert len(chats) == 1
    c = chats[0]
    assert c["id"] == "job:meridian-v3/A1/1" and c["source"] == "job"
    assert c["agent"] == "claude_code" and c["model"] == "claude-sonnet-5-5"
    assert c["status"] == "done" and c["output"] == "Built the widget."
    assert c["started"] == iso(1_700_000_000.0)
    assert c["title"].startswith("A1") and "meridian-v3" in c["title"]
    assert c["prompt"].startswith("Do the thing")
    assert c["resumable"] is True and c["resume_note"] is None and c["cwd"] == "C:/repo"
    assert c["agent_session"] is None


def test_subagent_chats_job_states_and_non_resumable_reasons(dirs):
    data, _ = dirs
    make_job(data, "p", "run", 1, out=None, exited=False)                                   # still running
    make_job(data, "p", "bad", 1, out="Traceback boom", exit_code=1)                         # failed, no session id
    make_job(data, "p", "stop", 1, out="", exited=False, killed=True)                        # cancelled
    make_job(data, "p", "cm", 1, route="freebuff", argv=["x"], out="Chat reply")
    make_job(data, "p", "nosid", 1, out="plain text answer")                                 # claude, not json
    packet = data / "jobs" / "p" / "manual" / "1"
    write(packet / "packet.json", {"route": "gemini_app"})                                   # Copy Prompt packet: skipped
    got = by_id(list_(dirs, pid_alive=lambda pid, started=None: True))  # only "run" has no exit file
    assert got["job:p/run/1"]["status"] == "running" and not got["job:p/run/1"]["resumable"]
    assert got["job:p/run/1"]["resume_note"] == "Still running; reply once it has finished."
    assert got["job:p/bad/1"]["status"] == "error"
    assert got["job:p/bad/1"]["resume_note"] == "No Claude session id was recorded for this job."
    assert got["job:p/stop/1"]["status"] == "stopped"
    assert got["job:p/cm/1"]["agent"] == "freebuff" and not got["job:p/cm/1"]["resumable"]
    assert got["job:p/cm/1"]["resume_note"] == "Freebuff jobs cannot be resumed."
    assert got["job:p/nosid/1"]["output"] == "plain text answer"
    assert all("\n" not in c["resume_note"] for c in got.values() if c["resume_note"])
    assert not any(k.startswith("job:p/manual") for k in got)


def test_subagent_chats_reads_dsh_tickets(dirs):
    _, tickets = dirs
    log = tickets / "tkt_1.log"
    write(log, "\x1b[32mhello\x1b[0m\nworker output")
    claude_log = tickets / "tkt_2.log"
    write(claude_log, CLAUDE_OUT)
    write(tickets / "tickets.json", {
        "tkt_1": {"ticket_id": "tkt_1", "runner": "freebuff", "pid": 5, "status": "awaiting-review",
                  "created_at": "2026-11-04T09:00:00+00:00", "cwd": "C:/x", "log_path": str(log),
                  "prompt_preview": "Summarise the repo\nplease", "receipt": "Receipt text"},
        "tkt_2": {"ticket_id": "tkt_2", "runner": "claude", "pid": 6, "status": "awaiting-review",
                  "created_at": "2026-11-04T10:00:00+00:00", "cwd": "C:/y", "log_path": str(claude_log),
                  "prompt_preview": "Fix the bug"},
        "tkt_3": {"ticket_id": "tkt_3", "runner": "antigravity", "pid": 7, "status": "failed",
                  "created_at": "2026-11-04T08:00:00+00:00", "log_path": str(tickets / "gone.log"),
                  "prompt_preview": ""},
        "tkt_4": {"ticket_id": "tkt_4", "runner": "groq", "pid": 8, "status": "working",
                  "created_at": "2026-11-04T11:00:00+00:00", "log_path": str(tickets / "none.log"),
                  "prompt_preview": "Still going"},
    })
    chats = list_(dirs, pid_alive=lambda pid, started=None: True)
    assert [c["id"] for c in chats] == ["ticket:tkt_4", "ticket:tkt_2", "ticket:tkt_1", "ticket:tkt_3"]  # newest first
    got = by_id(chats)
    t1 = got["ticket:tkt_1"]
    assert t1["agent"] == "freebuff" and t1["status"] == "done" and t1["title"] == "Summarise the repo"
    assert t1["output"] == "Receipt text" and "\x1b" not in t1["output"]
    assert t1["resumable"] is False and t1["resume_note"] == "This ticket worker ran once and cannot be resumed."
    t2 = got["ticket:tkt_2"]
    assert t2["agent"] == "claude_code" and t2["output"] == "Built the widget." and t2["resumable"] is True
    assert got["ticket:tkt_3"]["status"] == "error" and got["ticket:tkt_3"]["output"] == ""
    assert got["ticket:tkt_4"]["status"] == "running"


def test_subagent_chats_working_ticket_whose_process_died_is_done(dirs):
    _, tickets = dirs
    write(tickets / "t.log", "finished text")
    write(tickets / "tickets.json", {"t": {"ticket_id": "t", "runner": "freebuff", "pid": 9, "status": "working",
                                           "created_at": "2026-11-04T09:00:00+00:00", "log_path": str(tickets / "t.log"),
                                           "prompt_preview": "x"}})
    assert list_(dirs)[0]["status"] == "done"
    assert list_(dirs, pid_alive=lambda pid, started=None: True)[0]["status"] == "running"
    assert list_(dirs)[0]["output"] == "finished text"


def test_subagent_chats_tolerates_missing_and_broken_sources(dirs, tmp_path):
    data, tickets = dirs
    assert subagent_chats.list_chats(tmp_path / "nowhere", tmp_path / "nothing") == []
    write(tickets / "tickets.json", "{not json")
    write(data / "jobs" / "p" / "n" / "1" / "spec.json", "{broken")
    assert list_(dirs) == []


def test_subagent_chats_limit_keeps_newest(dirs):
    data, _ = dirs
    for i in range(5):
        make_job(data, "p", f"n{i}", 1, out=CLAUDE_OUT, started=1_700_000_000.0 + i)
    chats = list_(dirs, limit=3)
    assert [c["id"] for c in chats] == ["job:p/n4/1", "job:p/n3/1", "job:p/n2/1"]


# ---------------------------------------------------------------- HTTP handlers
class FakeProc:
    def __init__(self, lines=()):
        self._lines = list(lines)
        self.pid = 3

    @property
    def stdout(self):
        for ln in self._lines:
            yield ln + "\n"

    def wait(self):
        return 0

    def stderr_tail(self):
        return ""


class H:
    def __init__(self, store, dirs, body=None, actor="owner"):
        data, tickets = dirs
        self.app = SimpleNamespace(store=store, usage_reader=lambda: USAGE, tickets_dir=tickets,
                                   subagent_data_dir=data, pid_alive=lambda pid, started=None: False)
        self.actor = actor
        self._body = body or {}

    def _json_body(self):
        return self._body


@pytest.fixture
def rt(tmp_path):
    folder = tmp_path / "repo"
    folder.mkdir()
    calls = []
    lines = [json.dumps({"type": "system", "session_id": "sess-abc"}),
             json.dumps({"type": "result", "subtype": "success", "result": "Reply text", "session_id": "sess-abc"})]

    def popen(argv, cwd, env, stdin_text):
        calls.append({"argv": argv, "cwd": cwd, "stdin": stdin_text})
        return FakeProc(lines)

    r = agents.Runtime(which=lambda n: "C:/bin/claude.cmd" if n == "claude" else None, port_open=lambda p: True,
                       popen=popen, killer=lambda pid: None, usage_reader=lambda: USAGE,
                       folders=[{"id": "repo", "label": "repo", "path": str(folder)}], env={"PATH": "x"},
                       models_reader=lambda: [], disabled_check=lambda a: False, threaded=False)
    r.calls = calls
    prev = agents.set_runtime(r)
    yield r
    agents.set_runtime(prev)


def test_subagent_chats_get_route(store, dirs):
    data, _ = dirs
    make_job(data, "p", "A1", 1, out=CLAUDE_OUT)
    out = subagent_chats.g_chats(H(store, dirs), {})
    assert out["chats"][0]["id"] == "job:p/A1/1"
    for key in ("title", "agent", "model", "started", "status", "output"):
        assert key in out["chats"][0]
    assert set(subagent_chats.ROUTES) == {"GET /api/subagent_chats", "POST /api/subagent_chats/reply"}
    assert store.revision() == 0


def test_subagent_chats_reply_resumes_the_claude_session(store, dirs, rt, tmp_path):
    data, _ = dirs
    make_job(data, "p", "A1", 1, out=CLAUDE_OUT, cwd=str(tmp_path / "repo"))
    res = subagent_chats.p_reply(H(store, dirs, {"id": "job:p/A1/1", "message": "Now add tests"}), {})
    call = rt.calls[0]
    argv = call["argv"]
    assert argv[:2] == ["C:/bin/claude.cmd", "-p"] and argv[argv.index("--resume") + 1] == "sess-abc"
    assert "--session-id" not in argv
    assert call["stdin"] == "Now add tests" and call["cwd"] == str(tmp_path / "repo")
    assert res["agent"] == "claude_code" and res["session_id"] and res["chat"] == "job:p/A1/1"
    msgs = agents.list_messages(store, res["session_id"])
    assert [m["role"] for m in msgs][:3] == ["user", "assistant", "user"]   # original prompt, original output, reply
    assert msgs[1]["text"] == "Built the widget." and msgs[2]["text"] == "Now add tests"
    assert msgs[-1]["role"] == "assistant" and msgs[-1]["text"] == "Reply text"
    # the list now links the chat to its agent session; a second reply reuses it
    chat = subagent_chats.g_chats(H(store, dirs), {})["chats"][0]
    assert chat["agent_session"] == res["session_id"]
    again = subagent_chats.p_reply(H(store, dirs, {"id": "job:p/A1/1", "message": "And docs"}), {})
    assert again["session_id"] == res["session_id"]
    assert [m["role"] for m in agents.list_messages(store, res["session_id"])].count("user") == 3
    assert store.revision() == 0


def test_subagent_chats_reply_rules(store, dirs, rt):
    data, _ = dirs
    make_job(data, "p", "A1", 1, out=CLAUDE_OUT)
    make_job(data, "p", "cm", 1, route="freebuff", argv=["x"], out="x")
    with pytest.raises(HttpError) as e:
        subagent_chats.p_reply(H(store, dirs, {"id": "job:p/A1/1", "message": "hi"}, actor="router"), {})
    assert e.value.status == 403
    with pytest.raises(HttpError) as e:
        subagent_chats.p_reply(H(store, dirs, {"id": "job:p/cm/1", "message": "hi"}), {})
    assert e.value.status == 409 and e.value.body["error"] == "not_resumable"
    assert e.value.body["detail"] == "Freebuff jobs cannot be resumed."
    with pytest.raises(HttpError) as e:
        subagent_chats.p_reply(H(store, dirs, {"id": "job:p/nope/1", "message": "hi"}), {})
    assert e.value.status == 404
    with pytest.raises(HttpError) as e:
        subagent_chats.p_reply(H(store, dirs, {"id": "job:p/A1/1", "message": "  "}), {})
    assert e.value.status == 400
    assert not rt.calls


# ---------------------------------------------------------------- Codex sessions
CODEX_LINES = "\n".join([
    json.dumps({"type": "thread.started", "thread_id": "th-77"}),
    json.dumps({"type": "item.completed", "item": {"id": "i1", "type": "command_execution", "command": "ls"}}),
    json.dumps({"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": "Codex did it."}}),
    json.dumps({"type": "turn.completed", "usage": {}}),
])


def test_subagent_chats_codex_jobs_and_tickets_are_resumable(dirs):
    data, tickets = dirs
    make_job(data, "p", "cx", 1, route="codex", argv=["codex", "exec", "--json", "-m", "gpt-6-codex", "-"],
             out=CODEX_LINES)
    write(tickets / "c.log", CODEX_LINES)
    write(tickets / "tickets.json", {"c": {"ticket_id": "c", "runner": "codex", "pid": 1, "status": "awaiting-review",
                                           "created_at": "2026-11-04T09:00:00+00:00", "log_path": str(tickets / "c.log"),
                                           "prompt_preview": "Review it"}})
    got = by_id(list_(dirs))
    j = got["job:p/cx/1"]
    assert j["agent"] == "codex" and j["agent_name"] == "Codex" and j["output"] == "Codex did it."
    assert j["resumable"] is True and j["model"] == "gpt-6-codex"
    t = got["ticket:c"]
    assert t["agent"] == "codex" and t["resumable"] is True and t["output"] == "Codex did it."


def test_subagent_chats_agent_filter_and_codex_reply_needs_confirm(store, dirs, tmp_path):
    data, _ = dirs
    make_job(data, "p", "cl", 1, out=CLAUDE_OUT)
    make_job(data, "p", "cx", 1, route="codex", argv=["codex"], out=CODEX_LINES, cwd=str(tmp_path))
    out = subagent_chats.g_chats(H(store, dirs), {"agent": "codex"})
    assert [c["id"] for c in out["chats"]] == ["job:p/cx/1"]
    calls = []

    class P(FakeProc):
        pass

    def popen(argv, cwd, env, stdin_text):
        calls.append(argv)
        return P([json.dumps({"type": "thread.started", "thread_id": "th-77"}),
                  json.dumps({"type": "item.completed", "item": {"id": "z", "type": "agent_message", "text": "ok"}})])

    rt = agents.Runtime(which=lambda n: "C:/bin/codex.cmd" if n == "codex" else None, port_open=lambda p: True,
                        popen=popen, killer=lambda pid: None, usage_reader=lambda: USAGE,
                        folders=[{"id": "d", "label": "d", "path": str(tmp_path)}], env={}, models_reader=lambda: [],
                        threaded=False)
    prev = agents.set_runtime(rt)
    try:
        with pytest.raises(HttpError) as e:
            subagent_chats.p_reply(H(store, dirs, {"id": "job:p/cx/1", "message": "more"}), {})
        assert e.value.body["error"] == "confirm_usage" and not calls
        res = subagent_chats.p_reply(H(store, dirs, {"id": "job:p/cx/1", "message": "more", "confirm": True}), {})
    finally:
        agents.set_runtime(prev)
    assert calls[0][:5] == ["C:/bin/codex.cmd", "exec", "resume", "--json", "--skip-git-repo-check"]
    assert "th-77" in calls[0] and res["agent"] == "codex"
