"""cli_bridge: OpenAI-compatible loopback server that runs the claude / codex CLIs.

No real CLI or provider is ever called: the bridge gets an injected ``popen`` (fake child),
``which`` and route switches. Each test starts a real server on an ephemeral loopback port.
"""
from __future__ import annotations

import http.client
import json
import socket
import threading
import time

import pytest

from cc_engine import cli_bridge

TOKEN = "t" * 43  # stands in for data/engine.token
AUTH = {"Authorization": "Bearer " + TOKEN}


class FakeProc:
    def __init__(self, lines, code=0, block=None):
        self._lines = list(lines)
        self.code = code
        self.block = block  # threading.Event: stdout stalls until it is set (or the child is killed)
        self.pid = 4242
        self.killed = threading.Event()

    @property
    def stdout(self):
        for ln in self._lines:
            yield ln
        if self.block is not None:
            while not self.block.is_set() and not self.killed.is_set():
                time.sleep(0.02)

    def wait(self):
        return -9 if self.killed.is_set() else self.code

    def stderr_tail(self):
        return ""


def claude_lines(*texts):
    out = [json.dumps({"type": "system", "subtype": "init", "session_id": "s-1"}) + "\n",
           json.dumps({"type": "stream_event", "event": {"type": "message_start"}}) + "\n"]
    for t in texts:
        out.append(json.dumps({"type": "stream_event", "event": {
            "type": "content_block_delta", "delta": {"type": "text_delta", "text": t}}}) + "\n")
    out.append(json.dumps({"type": "result", "subtype": "success", "result": "".join(texts),
                           "session_id": "s-1"}) + "\n")
    return out


def codex_lines(text):
    return [json.dumps({"type": "thread.started", "thread_id": "t-1"}) + "\n",
            json.dumps({"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": text}}) + "\n",
            json.dumps({"type": "turn.completed"}) + "\n"]


class Rig:
    def __init__(self, switches=None, exes=("claude", "codex"), lines=None, code=0, block=None, timeout=30,
                 legacy=False):
        self.switches = dict({"claude_code": True, "codex": False}, **(switches or {}))
        self.calls = []
        self.kills = []
        self.lines = lines if lines is not None else claude_lines("Hel", "lo")
        self.code = code
        self.block = block
        self.procs = []
        base_env = {"PATH": "x", "ANTHROPIC_API_KEY": "sk-ant-bad", "OPENAI_API_KEY": "sk-bad",
                    "ANTHROPIC_AUTH_TOKEN": "bad", "CODEX_API_KEY": "bad", "HOME": "h"}
        self.bridge = cli_bridge.Bridge(
            enabled=lambda route: self.switches.get(route, False),
            which=lambda name: (f"C:/fake/{name}.exe" if name in exes else None),
            popen=self.popen, killer=self.kill, env=base_env, timeout=timeout, cwd=None, token=TOKEN,
            legacy_keys=legacy)
        self.server = cli_bridge.make_server(0, bridge=self.bridge)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def popen(self, argv, cwd, env, stdin_text):
        proc = FakeProc(self.lines, self.code, self.block)
        self.calls.append({"argv": argv, "env": env, "stdin": stdin_text, "cwd": cwd})
        self.procs.append(proc)
        return proc

    def kill(self, pid):
        self.kills.append(pid)
        for p in self.procs:
            p.killed.set()

    def request(self, method, path, body=None, headers=None, auth=True, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        data = json.dumps(body).encode() if body is not None else None
        hdrs = {"Content-Type": "application/json", **(AUTH if auth else {})}
        hdrs.update(headers or {})
        if host is None:
            conn.request(method, path, body=data, headers=hdrs)
        else:
            conn.putrequest(method, path, skip_host=True)
            if host:
                conn.putheader("Host", host)
            for k, v in hdrs.items():
                conn.putheader(k, v)
            conn.putheader("Content-Length", str(len(data or b"")))
            conn.endheaders(data)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        conn.close()
        return resp.status, resp.getheader("Content-Type") or "", raw

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def rig_factory():
    rigs = []

    def make(**kw):
        r = Rig(**kw)
        rigs.append(r)
        return r
    yield make
    for r in rigs:
        r.close()


def _ids(raw):
    return [m["id"] for m in json.loads(raw)["data"]]


def test_models_list_respects_switches(rig_factory):
    r = rig_factory()  # Claude on, Codex off (its default)
    status, ctype, raw = r.request("GET", "/v1/models")
    assert status == 200 and "json" in ctype
    ids = _ids(raw)
    assert ids == ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
    assert json.loads(raw)["object"] == "list"

    r.switches["codex"] = True
    ids = _ids(r.request("GET", "/v1/models")[2])
    assert set(cli_bridge.CODEX_MODEL_IDS) <= set(ids)

    r.switches["claude_code"] = False
    ids = _ids(r.request("GET", "/v1/models")[2])
    assert not any(i.startswith("claude-") for i in ids)


def test_default_switches_keep_codex_off():
    # With no injected switches the bridge asks toggles.py; with an unreadable db Codex stays off.
    b = cli_bridge.Bridge(which=lambda n: None, toggles_db="Z:/definitely/missing/cc.sqlite")
    assert b.route_enabled("codex") is False


def test_non_stream_completion(rig_factory):
    r = rig_factory()
    body = {"model": "claude-sonnet-5-5", "messages": [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello."},
        {"role": "user", "content": [{"type": "text", "text": "Say hello"}]}]}
    status, ctype, raw = r.request("POST", "/v1/chat/completions", body)
    assert status == 200, raw
    out = json.loads(raw)
    assert out["object"] == "chat.completion"
    assert out["model"] == "claude-sonnet-5-5"
    assert out["choices"][0]["message"] == {"role": "assistant", "content": "Hello"}
    assert out["choices"][0]["finish_reason"] == "stop"
    call = r.calls[0]
    argv = call["argv"]
    assert argv[0] == "C:/fake/claude.exe"
    assert argv[1:4] == ["-p", "--output-format", "stream-json"]
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5-5"
    for part in ("Be brief.", "Hi", "Hello.", "Say hello"):
        assert part in call["stdin"]


def test_stream_completion_sse(rig_factory):
    r = rig_factory(lines=claude_lines("Hel", "lo", " world"))
    body = {"model": "claude-opus-5-5", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
    status, ctype, raw = r.request("POST", "/v1/chat/completions", body)
    assert status == 200 and ctype.startswith("text/event-stream")
    data = [ln[len("data: "):] for ln in raw.splitlines() if ln.startswith("data: ")]
    assert data[-1] == "[DONE]"
    chunks = [json.loads(d) for d in data[:-1]]
    assert all(c["object"] == "chat.completion.chunk" for c in chunks)
    text = "".join((c["choices"][0].get("delta") or {}).get("content") or "" for c in chunks if c["choices"])
    assert text == "Hello world"
    assert chunks[0]["choices"][0]["delta"].get("role") == "assistant"
    finals = [c for c in chunks if c["choices"] and c["choices"][0].get("finish_reason")]
    assert finals and finals[-1]["choices"][0]["finish_reason"] == "stop"


def test_codex_route_when_switched_on(rig_factory):
    r = rig_factory(switches={"codex": True}, lines=codex_lines("from codex"))
    model = cli_bridge.CODEX_MODEL_IDS[0]
    status, _, raw = r.request("POST", "/v1/chat/completions",
                               {"model": model, "messages": [{"role": "user", "content": "hi"}],
                                "reasoning_effort": "high"})
    assert status == 200, raw
    assert json.loads(raw)["choices"][0]["message"]["content"] == "from codex"
    argv = r.calls[0]["argv"]
    assert argv[:2] == ["C:/fake/codex.exe", "exec"] and "--json" in argv
    assert argv[argv.index("-m") + 1] == model
    assert "model_reasoning_effort=high" in argv
    assert "CODEX_HOME" not in r.calls[0]["env"]


def test_disabled_route_is_clean_error(rig_factory):
    r = rig_factory()  # Codex off
    status, ctype, raw = r.request("POST", "/v1/chat/completions",
                                   {"model": cli_bridge.CODEX_MODEL_IDS[0], "stream": True,
                                    "messages": [{"role": "user", "content": "hi"}]})
    assert 400 <= status < 500 and "json" in ctype
    err = json.loads(raw)["error"]
    assert err["code"] == "route_disabled" and err["message"]
    assert r.calls == []


def test_missing_executable_is_503(rig_factory):
    r = rig_factory(exes=())
    status, _, raw = r.request("POST", "/v1/chat/completions",
                               {"model": "claude-haiku-5-5", "messages": [{"role": "user", "content": "hi"}]})
    assert status == 503
    assert json.loads(raw)["error"]["code"] == "cli_unavailable"
    assert r.calls == []


def test_unknown_model_and_bad_json(rig_factory):
    r = rig_factory()
    status, _, raw = r.request("POST", "/v1/chat/completions",
                               {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]})
    assert status == 404 and json.loads(raw)["error"]["code"] == "model_not_found"
    conn = http.client.HTTPConnection("127.0.0.1", r.port, timeout=5)
    conn.request("POST", "/v1/chat/completions", body=b"{nope", headers={"Content-Type": "application/json", **AUTH})
    resp = conn.getresponse()
    assert resp.status == 400 and "error" in json.loads(resp.read())
    conn.close()
    status, _, _ = r.request("GET", "/nope")
    assert status == 404


def test_browser_origin_refused(rig_factory):
    r = rig_factory()
    status, _, raw = r.request("POST", "/v1/chat/completions",
                               {"model": "claude-haiku-5-5", "messages": [{"role": "user", "content": "hi"}]},
                               headers={"Origin": "https://evil.example"})
    assert status == 403
    assert r.calls == []


def test_child_env_has_no_api_keys(rig_factory):
    r = rig_factory(switches={"codex": True})
    r.request("POST", "/v1/chat/completions",
              {"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "hi"}]})
    r.lines = codex_lines("ok")
    r.request("POST", "/v1/chat/completions",
              {"model": cli_bridge.CODEX_MODEL_IDS[0], "messages": [{"role": "user", "content": "hi"}]})
    assert len(r.calls) == 2
    for call in r.calls:
        env = call["env"]
        for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CODEX_API_KEY"):
            assert key not in env
        assert env.get("PATH") == "x"


def test_child_failure_reports_error(rig_factory):
    lines = [json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                         "result": "Not logged in"}) + "\n"]
    r = rig_factory(lines=lines, code=1)
    status, _, raw = r.request("POST", "/v1/chat/completions",
                               {"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "hi"}]})
    assert status == 502
    assert "Not logged in" in json.loads(raw)["error"]["message"]


def test_timeout_kills_child(rig_factory):
    block = threading.Event()
    r = rig_factory(lines=claude_lines("partial"), block=block, timeout=0.5)
    t0 = time.time()
    status, _, raw = r.request("POST", "/v1/chat/completions",
                               {"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "hi"}]})
    assert time.time() - t0 < 8
    assert status == 504 and json.loads(raw)["error"]["code"] == "timeout"
    assert r.kills == [4242]


def test_client_disconnect_kills_child(rig_factory):
    block = threading.Event()
    r = rig_factory(lines=claude_lines("first"), block=block, timeout=60)
    body = json.dumps({"model": "claude-opus-5-5", "stream": True,
                       "messages": [{"role": "user", "content": "hi"}]}).encode()
    sock = socket.create_connection(("127.0.0.1", r.port), timeout=5)
    sock.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1:" + str(r.port).encode() + b"\r\n"
                 b"Authorization: Bearer " + TOKEN.encode() + b"\r\n"
                 b"Content-Type: application/json\r\nContent-Length: " + str(len(body)).encode()
                 + b"\r\n\r\n" + body)
    got = b""
    while b"data: " not in got:  # first SSE bytes arrived: the child is running
        got += sock.recv(4096)
    assert got.startswith(b"HTTP/1.1 200")
    sock.close()
    deadline = time.time() + 8
    while not r.kills and time.time() < deadline:
        time.sleep(0.05)
    block.set()
    assert r.kills == [4242]


def test_flatten_messages():
    assert cli_bridge.flatten_messages([{"role": "user", "content": "just this"}]) == "just this"
    text = cli_bridge.flatten_messages([
        {"role": "system", "content": "S"}, {"role": "user", "content": "U1"},
        {"role": "assistant", "content": "A1"}, {"role": "tool", "content": "T1"},
        {"role": "user", "content": "U2"}])
    assert text.index("S") < text.index("U1") < text.index("A1") < text.index("T1") < text.index("U2")


def test_start_background_best_effort(monkeypatch):
    monkeypatch.setenv("CC_CLI_BRIDGE", "0")
    assert cli_bridge.start_background(port=0) is None
    monkeypatch.delenv("CC_CLI_BRIDGE")
    bridge = cli_bridge.Bridge(enabled=lambda r: r == "claude_code", which=lambda n: None, cwd=None, token=TOKEN)
    srv = cli_bridge.start_background(port=0, bridge=bridge)
    assert srv is not None
    try:
        port = srv.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/v1/models", headers={"X-CC-Token": TOKEN})
        assert len(json.loads(conn.getresponse().read())["data"]) == 3
        conn.close()
        # The port is taken: a second start logs and returns None instead of raising.
        assert cli_bridge.start_background(port=port, bridge=bridge) is None
    finally:
        srv.shutdown()
        srv.server_close()


def test_cli_arg_parsing():
    args = cli_bridge.parse_args(["--port", "8799"])
    assert args.port == 8799 and args.host == "127.0.0.1"
    assert cli_bridge.parse_args([]).port == cli_bridge.DEFAULT_PORT == 8753


# ---------------------------------------------------------------- access (Waypoint Core 1.0)
CHAT = {"model": "claude-haiku-5-5", "messages": [{"role": "user", "content": "hi"}]}


def test_chat_and_models_need_the_engine_token(rig_factory):
    r = rig_factory()
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"X-CC-Token": "wrong"},
                    {"Authorization": "Basic " + TOKEN}, {"Authorization": "Bearer waypoint-cli-local"}):
        status, _, raw = r.request("POST", "/v1/chat/completions", CHAT, headers=headers, auth=False)
        assert status == 401 and json.loads(raw)["error"]["code"] == "invalid_api_key", headers
        assert r.request("GET", "/v1/models", headers=headers, auth=False)[0] == 401
    assert r.calls == []
    assert r.request("POST", "/v1/chat/completions", CHAT, headers={"X-CC-Token": TOKEN}, auth=False)[0] == 200
    assert r.request("POST", "/v1/chat/completions", CHAT)[0] == 200  # Authorization: Bearer <token>
    assert len(r.calls) == 2
    status, _, raw = r.request("GET", "/health", auth=False)  # health stays open
    assert status == 200 and json.loads(raw)["ok"] is True


def test_any_browser_origin_is_refused_even_with_the_token(rig_factory):
    r = rig_factory()
    for origin in ("https://evil.example", "http://127.0.0.1:3080", "http://localhost:8753", "null"):
        status, _, raw = r.request("POST", "/v1/chat/completions", CHAT, headers={"Origin": origin})
        assert status == 403 and json.loads(raw)["error"]["code"] == "forbidden_origin", origin
        assert r.request("GET", "/v1/models", headers={"Origin": origin})[0] == 403
    assert r.calls == []


@pytest.mark.parametrize("host", ["evil.example:{port}", "127.0.0.1", "localhost:{other}", "rebind.test", ""])
def test_bad_host_is_refused_even_with_the_token(rig_factory, host):
    r = rig_factory()
    value = host.format(port=r.port, other=r.port + 1)
    for method, path, body in (("POST", "/v1/chat/completions", CHAT), ("GET", "/v1/models", None),
                               ("GET", "/health", None)):
        status, _, raw = r.request(method, path, body, host=value)
        assert status == 403 and json.loads(raw)["error"]["code"] == "bad_host", (value, path)
    assert r.calls == []
    for name in ("127.0.0.1", "localhost", "[::1]"):
        assert r.request("GET", "/v1/models", host=f"{name}:{r.port}")[0] == 200


def test_legacy_placeholder_key_only_while_allowed(rig_factory, monkeypatch):
    r = rig_factory(legacy=None)  # follow CC_CLI_BRIDGE_LEGACY_KEY, then the settings file
    legacy = {"Authorization": "Bearer waypoint-cli-local"}
    monkeypatch.delenv("CC_CLI_BRIDGE_LEGACY_KEY", raising=False)
    assert r.request("GET", "/v1/models", headers=legacy, auth=False)[0] == 401  # off by default
    monkeypatch.setenv("CC_CLI_BRIDGE_LEGACY_KEY", "1")
    assert r.request("GET", "/v1/models", headers=legacy, auth=False)[0] == 200  # an old dsh keeps working
    assert r.request("POST", "/v1/chat/completions", CHAT, headers={**legacy, "Origin": "null"},
                     auth=False)[0] == 403  # never from a browser
    monkeypatch.setenv("CC_CLI_BRIDGE_LEGACY_KEY", "0")
    assert r.request("GET", "/v1/models", headers=legacy, auth=False)[0] == 401
    assert r.request("GET", "/v1/models")[0] == 200


def test_default_token_is_the_engine_token_file(tmp_path, monkeypatch):
    path = tmp_path / "engine.token"
    path.write_text("k" * 43, encoding="utf-8")
    monkeypatch.setenv("CC_TOKEN_FILE", str(path))
    b = cli_bridge.Bridge(enabled=lambda r: True, which=lambda n: None, cwd=None, legacy_keys=False)
    assert b.key_ok("k" * 43) and not b.key_ok("x" * 43) and not b.key_ok("")
    path.unlink()
    assert not b.key_ok("k" * 43)  # no token file: nothing is accepted
