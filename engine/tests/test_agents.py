"""Agents page engine: fakes only (no real CLI, bridge or model is ever called)."""
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

from cc_engine import agents
from cc_engine.api import HttpError

USAGE = [
    {"pool": "claude_5h", "label": "Claude 5 hours", "remaining_percent": 62.0, "status": "FRESH"},
    {"pool": "claude_week", "label": "Claude week", "remaining_percent": 40.0, "status": "FRESH"},
    {"pool": "codex_5h", "label": "Codex 5 hours", "remaining_percent": 80.0, "status": "FRESH"},
    {"pool": "codex_week", "label": "Codex week", "remaining_percent": None, "status": "UNKNOWN"},
    {"pool": "antigravity_week", "label": "Antigravity week", "remaining_percent": 55.0, "status": "STALE"},
]


class FakeProc:
    def __init__(self, lines, code=0, pid=4242, block=None, on_start=None, stderr=""):
        self._lines, self.code, self.pid, self.block, self.stderr = lines, code, pid, block, stderr

    @property
    def stdout(self):
        for ln in self._lines:
            yield ln + "\n"
        if self.block is not None:
            self.block.wait(5)

    def wait(self):
        return self.code

    def stderr_tail(self):
        return self.stderr


class FakePopen:
    def __init__(self, procs):
        self.procs = list(procs)
        self.calls = []

    def __call__(self, argv, cwd, env, stdin_text):
        self.calls.append({"argv": argv, "cwd": cwd, "env": env, "stdin": stdin_text})
        proc = self.procs.pop(0)
        if callable(proc):
            proc = proc(argv)
        return proc


class H:
    def __init__(self, store, body=None, actor="owner"):
        self.app = SimpleNamespace(store=store, usage_reader=lambda: USAGE)
        self.actor = actor
        self._body = body or {}

    def _json_body(self):
        return self._body


@pytest.fixture
def folders(tmp_path):
    a, b = tmp_path / "drive", tmp_path / "repo"
    a.mkdir()
    b.mkdir()
    return [{"id": "drive", "label": "Drive", "path": str(a)}, {"id": "repo", "label": "repo", "path": str(b)}]


@pytest.fixture
def events(store):
    got = []
    store.subscribe(lambda kind, p: got.append(p) if kind == "agent" else None)
    return got


def make_rt(folders, popen=None, http_stream=None, which=None, ports=(1455,), **kw):
    env = {"PATH": "x", "ANTHROPIC_API_KEY": "sk-no", "OPENAI_API_KEY": "sk-no", "CODEX_HOME": "elsewhere",
           "GEMINI_API_KEY": "no"}
    killed = []
    rt = agents.Runtime(
        which=which or (lambda name: {"claude": "C:/bin/claude.cmd", "codex": "C:/bin/codex.cmd",
                                      "agy": "C:/bin/agy.exe"}.get(name)),
        port_open=lambda port: port in ports, popen=popen or FakePopen([]),
        http_stream=http_stream or (lambda url, body, cancel: iter(())),
        killer=killed.append, usage_reader=lambda: USAGE, folders=folders, env=env,
        freebuff_models=lambda: ["deepseek/deepseek-v4-flash"], sleep=lambda s: None,
        models_reader=lambda: [], disabled_check=lambda a: False, **kw)
    rt.killed = killed
    return rt


@pytest.fixture
def rt_factory(folders):
    prev = agents.RUNTIME

    def factory(**kw):
        rt = make_rt(folders, threaded=kw.pop("threaded", False), **kw)
        agents.set_runtime(rt)
        return rt

    yield factory
    agents.set_runtime(prev)


CLAUDE_LINES = [
    json.dumps({"type": "system", "subtype": "init", "session_id": "sess-1"}),
    json.dumps({"type": "stream_event", "event": {"type": "message_start"}}),
    json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
                                                  "delta": {"type": "text_delta", "text": "Let me "}}}),
    json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
                                                  "delta": {"type": "text_delta", "text": "look."}}}),
    json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Let me look."}]}}),
    json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Read", "input": {"file_path": "engine/cc_engine/api.py"}}]}}),
    json.dumps({"type": "stream_event", "event": {"type": "message_start"}}),
    json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
                                                  "delta": {"type": "text_delta", "text": "Done."}}}),
    json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "Done.",
                "session_id": "sess-1"}),
]


def test_list_agents_availability_and_usage(store, rt_factory):
    rt_factory(which=lambda n: "C:/bin/claude.cmd" if n == "claude" else None, ports=(1455,))
    out = agents.g_agents(H(store), {})
    by = {a["id"]: a for a in out["agents"]}
    assert list(by) == ["claude_code", "codex", "freebuff", "antigravity"]
    assert by["claude_code"]["available"] and not by["codex"]["available"]
    assert "chatmode" not in by and by["freebuff"]["available"]
    assert by["claude_code"]["usage_percent"] == 40.0 and by["claude_code"]["usage_status"] == "FRESH"
    assert by["codex"]["usage_percent"] == 80.0 and by["codex"]["needs_confirm"]
    assert by["antigravity"]["usage_status"] == "STALE"
    assert by["freebuff"]["usage_percent"] is None and by["freebuff"]["usage_status"] == "UNKNOWN"
    assert [f["id"] for f in out["folders"]] == ["drive", "repo"]
    assert store.revision() == 0


def test_claude_new_chat_streams_and_records(store, rt_factory, events, folders):
    popen = FakePopen([FakeProc(CLAUDE_LINES)])
    rt_factory(popen=popen)
    res = agents.p_send(H(store, {"agent": "claude_code", "message": "Look at api.py", "cwd": "repo"}), {})
    call = popen.calls[0]
    argv = call["argv"]
    assert argv[:6] == ["C:/bin/claude.cmd", "-p", "--output-format", "stream-json", "--verbose",
                        "--include-partial-messages"]
    assert "--session-id" in argv and "--resume" not in argv
    assert call["stdin"] == "Look at api.py" and call["cwd"] == folders[1]["path"]
    assert "ANTHROPIC_API_KEY" not in call["env"] and "OPENAI_API_KEY" not in call["env"]
    deltas = "".join(e.get("delta", "") for e in events)
    assert deltas == "Let me look.\n\nDone."
    assert [e["tool"] for e in events if "tool" in e] == ["Read engine/cc_engine/api.py"]
    assert events[0]["started"] and events[-1]["done"] and not events[-1].get("error")
    assert all(e["turn_id"] == res["turn_id"] and e["session_id"] == res["session_id"] for e in events)
    msgs = agents.g_messages(H(store), {"session": res["session_id"]})
    assert [m["role"] for m in msgs["messages"]] == ["user", "assistant", "tool", "assistant"]
    assert msgs["session"]["ext_id"] == "sess-1" and msgs["session"]["title"] == "Look at api.py"
    assert msgs["active"] is None
    sessions = agents.g_sessions(H(store), {"agent": "claude_code"})["sessions"]
    assert [s["id"] for s in sessions] == [res["session_id"]]
    assert store.revision() == 0


def test_claude_resume_uses_captured_session(store, rt_factory):
    popen = FakePopen([FakeProc(CLAUDE_LINES), FakeProc(CLAUDE_LINES)])
    rt_factory(popen=popen)
    first = agents.p_send(H(store, {"agent": "claude_code", "message": "one"}), {})
    agents.p_send(H(store, {"agent": "claude_code", "message": "two", "session_id": first["session_id"]}), {})
    argv = popen.calls[1]["argv"]
    assert argv[argv.index("--resume") + 1] == "sess-1" and "--session-id" not in argv


def test_codex_needs_confirmation_then_runs_cli(store, rt_factory, events):
    lines = [json.dumps({"type": "thread.started", "thread_id": "th-9"}),
             json.dumps({"type": "item.started", "item": {"id": "i1", "type": "command_execution", "command": "ls"}}),
             json.dumps({"type": "item.completed", "item": {"id": "i1", "type": "command_execution", "command": "ls"}}),
             json.dumps({"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": "Two files."}}),
             json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5}})]
    popen = FakePopen([FakeProc(lines), FakeProc([])])
    rt_factory(popen=popen)
    with pytest.raises(HttpError) as e:
        agents.p_send(H(store, {"agent": "codex", "message": "hi"}), {})
    assert e.value.status == 409 and e.value.body["error"] == "confirm_usage"
    assert e.value.body["usage_percent"] == 80.0 and not popen.calls
    res = agents.p_send(H(store, {"agent": "codex", "message": "hi", "confirm": True}), {})
    call = popen.calls[0]
    assert call["argv"] == ["C:/bin/codex.cmd", "exec", "--json", "--skip-git-repo-check", "-"]
    assert call["stdin"] == "hi"
    assert "OPENAI_API_KEY" not in call["env"] and "CODEX_HOME" not in call["env"]
    assert [e["tool"] for e in events if "tool" in e] == ["Ran ls"]
    assert "".join(e.get("delta", "") for e in events) == "Two files."
    agents.p_send(H(store, {"agent": "codex", "message": "more", "confirm": True,
                            "session_id": res["session_id"]}), {})
    assert popen.calls[1]["argv"] == ["C:/bin/codex.cmd", "exec", "resume", "--json", "--skip-git-repo-check",
                                      "th-9", "-"]


def test_usage_floor_blocks_send(store, rt_factory):
    popen = FakePopen([])
    rt = rt_factory(popen=popen)
    rt.usage_reader = lambda: [{"pool": "codex_5h", "remaining_percent": 2.0, "status": "FRESH"}]
    with pytest.raises(HttpError) as e:
        agents.p_send(H(store, {"agent": "codex", "message": "hi", "confirm": True}), {})
    assert e.value.body["error"] == "usage_floor" and not popen.calls


def test_owner_only_and_validation(store, rt_factory):
    rt_factory(which=lambda n: None, ports=())
    with pytest.raises(HttpError) as e:
        agents.p_send(H(store, {"agent": "claude_code", "message": "x"}, actor="router"), {})
    assert e.value.status == 403
    with pytest.raises(HttpError) as e:
        agents.p_stop(H(store, {"turn_id": "x"}, actor="router"), {})
    assert e.value.status == 403
    with pytest.raises(HttpError) as e:
        agents.p_send(H(store, {"agent": "nope", "message": "x"}), {})
    assert e.value.status == 400
    with pytest.raises(HttpError) as e:
        agents.p_send(H(store, {"agent": "claude_code", "message": "x"}), {})
    assert e.value.status == 503


def test_cwd_must_be_a_listed_folder(store, rt_factory, tmp_path, folders):
    rt = rt_factory(popen=FakePopen([FakeProc(CLAUDE_LINES)]))
    with pytest.raises(HttpError) as e:
        agents.p_send(H(store, {"agent": "claude_code", "message": "x", "cwd": str(tmp_path / "other")}), {})
    assert e.value.status == 400 and e.value.body["error"] == "bad_folder"
    sub = os.path.join(folders[0]["path"], "sub")
    assert rt.resolve_cwd(sub) == str(type(tmp_path)(sub).resolve())


def test_freebuff_streams_with_history(store, rt_factory, events):
    bodies = []

    def stream(url, body, cancel):
        bodies.append((url, body))
        yield "Hel"
        yield "lo"

    rt_factory(http_stream=stream)
    res = agents.p_send(H(store, {"agent": "freebuff", "message": "first"}), {})
    agents.p_send(H(store, {"agent": "freebuff", "message": "second", "session_id": res["session_id"]}), {})
    url, body = bodies[1]
    assert url.endswith("127.0.0.1:1455/v1/chat/completions")
    assert body["model"] == "deepseek/deepseek-v4-flash"
    assert [(m["role"], m["content"]) for m in body["messages"][1:]] == [
        ("user", "first"), ("assistant", "Hello"), ("user", "second")]
    assert "".join(e.get("delta", "") for e in events if e["session_id"] == res["session_id"]) == "HelloHello"


def test_antigravity_argv_and_lenient_parse(store, rt_factory, events):
    lines = [json.dumps({"type": "init", "conversation_id": "conv-7"}),
             json.dumps({"type": "tool_call", "name": "read_file", "input": {"path": "a.txt"}}),
             json.dumps({"type": "text", "text": "Answer"}),
             "plain tail"]
    popen = FakePopen([FakeProc(lines), FakeProc([])])
    rt_factory(popen=popen)
    res = agents.p_send(H(store, {"agent": "antigravity", "message": "q"}), {})
    assert popen.calls[0]["argv"] == ["C:/bin/agy.exe", "-p", "q", "--output-format", "stream-json"]
    assert "GEMINI_API_KEY" not in popen.calls[0]["env"]
    assert [e["tool"] for e in events if "tool" in e] == ["read_file a.txt"]
    assert "".join(e.get("delta", "") for e in events) == "Answerplain tail\n"
    agents.p_send(H(store, {"agent": "antigravity", "message": "q2", "session_id": res["session_id"]}), {})
    assert popen.calls[1]["argv"][-2:] == ["--conversation", "conv-7"]


def test_error_exit_is_reported(store, rt_factory, events):
    popen = FakePopen([FakeProc([], code=2, stderr="boom: not logged in")])
    rt_factory(popen=popen)
    res = agents.p_send(H(store, {"agent": "claude_code", "message": "x"}), {})
    assert events[-1]["error"] == "boom: not logged in" and events[-1]["done"]
    roles = [m["role"] for m in agents.list_messages(store, res["session_id"])]
    assert roles == ["user", "error"]


def test_stop_kills_running_turn(store, rt_factory, events):
    gate = threading.Event()
    popen = FakePopen([FakeProc([json.dumps({"type": "system", "session_id": "s"})], block=gate, pid=99)])
    rt = rt_factory(popen=popen, threaded=True)
    rt.killer = lambda pid: (rt.killed.append(pid), gate.set())
    res = agents.p_send(H(store, {"agent": "claude_code", "message": "long job"}), {})
    for _ in range(200):
        if popen.calls and rt.turns.get(res["turn_id"]) and rt.turns[res["turn_id"]].proc:
            break
        time.sleep(0.01)
    snap = agents.g_messages(H(store), {"session": res["session_id"]})
    assert snap["active"]["turn_id"] == res["turn_id"]
    with pytest.raises(HttpError) as e:
        agents.p_send(H(store, {"agent": "claude_code", "message": "again", "session_id": res["session_id"]}), {})
    assert e.value.status == 409 and e.value.body["error"] == "busy"
    assert agents.p_stop(H(store, {"turn_id": res["turn_id"]}), {})["stopped"]
    rt.turns.get(res["turn_id"]) and rt.turns[res["turn_id"]].thread.join(5)
    for _ in range(200):
        if events and events[-1].get("done"):
            break
        time.sleep(0.01)
    assert rt.killed == [99]
    assert events[-1]["done"] and events[-1]["stopped"]
    assert agents.p_stop(H(store, {"turn_id": res["turn_id"]}), {})["stopped"] is False


def test_parsers_units():
    p = agents.ClaudeParser()
    assert p.feed(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "A"}]}})) == [("delta", "A")]
    assert p.feed(json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True, "result": ""}))[-1][0] == "error"
    c = agents.CodexParser()
    assert c.feed("not json") == []
    assert c.feed(json.dumps({"type": "turn.failed", "error": {"message": "quota"}})) == [("error", "quota")]
    assert c.feed(json.dumps({"msg": {"type": "agent_message", "message": "old"}})) == [("delta", "old")]
    assert agents.describe_tool("Bash", {"command": "ls  -la"}) == "Bash ls -la"


def test_routes_through_engine_http(store, rt_factory, tmp_path):
    import http.client
    from cc_engine import api

    rt_factory(popen=FakePopen([FakeProc(CLAUDE_LINES)]))
    srv = api.make_server(store, host="127.0.0.1", port=0, usage_reader=lambda: USAGE,
                          heartbeat=0.5, poll_interval=0.2, token_path=tmp_path / "engine.token")
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()

    def call(method, path, body=None, origin=None):
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        conn.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, json.loads(data) if data else None

    try:
        status, body = call("GET", "/api/agents")
        if status == 404:
            pytest.skip("api.py does not load extension routes yet")
        assert status == 200 and len(body["agents"]) == 4
        status, body = call("POST", "/api/agents/send", {"agent": "claude_code", "message": "hi"})
        assert status == 403
        status, body = call("POST", "/api/agents/send", {"agent": "claude_code", "message": "hi"},
                            origin="http://127.0.0.1:3080")
        assert status == 200, body
        status, msgs = call("GET", "/api/agents/messages?session=" + body["session_id"])
        assert status == 200 and msgs["messages"][0]["text"] == "hi"
    finally:
        srv.shutdown()
        srv.server_close()


def test_turn_events_carry_per_file_plus_minus_counts(store, rt_factory, events):
    lines = [
        json.dumps({"type": "system", "subtype": "init", "session_id": "sess-9"}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "a.py", "old_string": "x\ny", "new_string": "x\ny\nz"}}]}}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Write", "input": {"file_path": "b.py", "content": "1\n2\n3"}}]}}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "a.py", "old_string": "z", "new_string": "w"}}]}}),
        "not json at all",
        json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "ok", "session_id": "sess-9"}),
    ]
    rt_factory(popen=FakePopen([FakeProc(lines)]))
    agents.p_send(H(store, {"agent": "claude_code", "message": "edit", "cwd": "repo"}), {})
    tools = [e for e in events if "tool" in e]
    assert [t["files"] for t in tools] == [[{"path": "a.py", "added": 3, "removed": 2}],
                                           [{"path": "b.py", "added": 3, "removed": 0}],
                                           [{"path": "a.py", "added": 1, "removed": 1}]]
    done = events[-1]
    assert done["done"] and {f["path"]: (f["added"], f["removed"]) for f in done["files"]} == {"a.py": (4, 3), "b.py": (3, 0)}


def test_turn_events_without_edits_have_no_files_key(store, rt_factory, events):
    rt_factory(popen=FakePopen([FakeProc(CLAUDE_LINES)]))
    agents.p_send(H(store, {"agent": "claude_code", "message": "look", "cwd": "repo"}), {})
    assert not any("files" in e for e in events)
