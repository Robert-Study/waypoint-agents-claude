"""Agents tab (A1): per-agent model and effort selectors reach the CLI. Fakes only."""
import json
from types import SimpleNamespace

import pytest

from cc_engine import agents
from cc_engine.api import HttpError

ROWS = [
    {"id": "subagent:claude/opus-5.5", "label": "Claude Opus 5.5", "kind": "subagent", "route": "claude_code",
     "model": "opus-5.5", "provider": "anthropic", "enabled": True, "elo": 1507, "access": None},
    {"id": "subagent:claude/sonnet-5.5", "label": "Claude Sonnet 5.5", "kind": "subagent", "route": "claude_code",
     "model": "sonnet-5.5", "provider": "anthropic", "enabled": True, "elo": 1400, "access": None},
    {"id": "subagent:claude/haiku-5.5", "label": "Claude Haiku 5.5", "kind": "subagent", "route": "claude_code",
     "model": "haiku-5.5", "provider": "anthropic", "enabled": False, "elo": None, "access": None},
    {"id": "chat:freebuff/deepseek", "label": "DeepSeek V4.1 Flash", "kind": "chat", "route": "freebuff",
     "model": "deepseek/deepseek-v4-flash", "provider": "freebuff", "enabled": True, "access": True},
    {"id": "chat:freebuff/gone", "label": "Gone", "kind": "chat", "route": "freebuff", "model": "x/gone",
     "provider": "freebuff", "enabled": True, "access": False},
    {"id": "subagent:antigravity/flash", "label": "Gemini Flash", "kind": "subagent", "route": "antigravity",
     "model": "gemini-3.8-flash-high", "provider": "antigravity", "enabled": True},
    {"id": "chat:openai-codex/gpt-6-codex", "label": "GPT-6 Codex", "kind": "chat", "route": None,
     "model": "gpt-6-codex", "provider": "openai-codex", "enabled": True},
    {"id": "agent:claude_code", "label": "Claude Code", "kind": "agent", "route": "claude_code", "model": None,
     "provider": "anthropic", "enabled": True},
]
USAGE = [{"pool": "claude_5h", "label": "Claude 5h", "remaining_percent": 62.0, "status": "FRESH"},
         {"pool": "codex_5h", "label": "Codex 5h", "remaining_percent": 80.0, "status": "FRESH"}]


class FakeProc:
    def __init__(self, lines=(), code=0, pid=7):
        self._lines, self.code, self.pid = list(lines), code, pid

    @property
    def stdout(self):
        for ln in self._lines:
            yield ln + "\n"

    def wait(self):
        return self.code

    def stderr_tail(self):
        return ""


class FakePopen:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, cwd, env, stdin_text):
        self.calls.append({"argv": argv, "cwd": cwd, "env": env, "stdin": stdin_text})
        return FakeProc()


class H:
    def __init__(self, store, body=None, actor="owner"):
        self.app = SimpleNamespace(store=store, usage_reader=lambda: USAGE)
        self.actor = actor
        self._body = body or {}

    def _json_body(self):
        return self._body


@pytest.fixture
def folders(tmp_path):
    a = tmp_path / "drive"
    a.mkdir()
    return [{"id": "drive", "label": "Drive", "path": str(a)}]


@pytest.fixture
def rt(folders):
    popen = FakePopen()
    seen = []

    def stream(url, body, cancel):
        seen.append(body)
        return iter(["ok"])

    r = agents.Runtime(
        which=lambda n: {"claude": "C:/bin/claude.cmd", "codex": "C:/bin/codex.cmd", "agy": "C:/bin/agy.exe"}.get(n),
        port_open=lambda p: True, popen=popen, http_stream=stream, killer=lambda pid: None,
        usage_reader=lambda: USAGE, folders=folders, env={"PATH": "x"}, models_reader=lambda: ROWS,
        disabled_check=lambda a: a == "freebuff-off",
        freebuff_models=lambda: ["deepseek/deepseek-v4-flash"], sleep=lambda s: None,
        threaded=False)
    r.popen_calls, r.http_bodies = popen.calls, seen
    prev = agents.set_runtime(r)
    yield r
    agents.set_runtime(prev)


def test_agents_model_effort_lists_only_enabled_models_per_agent(store, rt):
    out = agents.g_agents(H(store), {})
    by = {a["id"]: a for a in out["agents"]}
    assert [m["model"] for m in by["claude_code"]["models"]] == ["opus-5.5", "sonnet-5.5"]
    assert by["claude_code"]["models"][0]["label"] == "Claude Opus 5.5"
    assert by["claude_code"]["efforts"] == ["low", "medium", "high", "xhigh", "max"]
    assert [m["model"] for m in by["freebuff"]["models"]] == ["deepseek/deepseek-v4-flash"]  # no access -> hidden
    assert "chatmode" not in by
    assert [m["model"] for m in by["codex"]["models"]] == ["gpt-6-codex"]
    assert by["codex"]["efforts"] == ["low", "medium", "high", "xhigh"]
    assert by["freebuff"]["efforts"] == [] and by["antigravity"]["efforts"] == []
    assert store.revision() == 0


def test_agents_model_effort_registry_down_gives_empty_lists(store, folders):
    def boom():
        raise OSError("no registry")

    r = agents.Runtime(which=lambda n: None, port_open=lambda p: False, usage_reader=lambda: [], folders=folders,
                       models_reader=boom)
    prev = agents.set_runtime(r)
    try:
        by = {a["id"]: a for a in agents.g_agents(H(store), {})["agents"]}
    finally:
        agents.set_runtime(prev)
    assert by["claude_code"]["models"] == [] and by["claude_code"]["efforts"]


def test_agents_model_effort_claude_flags(store, rt):
    agents.p_send(H(store, {"agent": "claude_code", "message": "hi", "model": "opus-5.5", "effort": "max"}), {})
    argv = rt.popen_calls[0]["argv"]
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5"  # the CLI's own spelling
    assert argv[argv.index("--effort") + 1] == "max"
    assert argv[:2] == ["C:/bin/claude.cmd", "-p"]


def test_agents_model_effort_claude_default_has_no_flags(store, rt):
    agents.p_send(H(store, {"agent": "claude_code", "message": "hi"}), {})
    argv = rt.popen_calls[0]["argv"]
    assert "--model" not in argv and "--effort" not in argv


def test_agents_model_effort_codex_flags_and_confirm_kept(store, rt):
    with pytest.raises(HttpError) as e:
        agents.p_send(H(store, {"agent": "codex", "message": "hi", "model": "gpt-6-codex", "effort": "high"}), {})
    assert e.value.body["error"] == "confirm_usage" and not rt.popen_calls
    res = agents.p_send(H(store, {"agent": "codex", "message": "hi", "model": "gpt-6-codex", "effort": "high",
                                  "confirm": True}), {})
    argv = rt.popen_calls[0]["argv"]
    assert argv == ["C:/bin/codex.cmd", "exec", "--json", "--skip-git-repo-check", "-m", "gpt-6-codex",
                    "-c", "model_reasoning_effort=high", "-"]
    # resumed chats carry the flags before the session id
    agents.add_messages(store, res["session_id"], "x", [], ext_id="th-1")
    agents.p_send(H(store, {"agent": "codex", "message": "more", "session_id": res["session_id"],
                            "model": "gpt-6-codex", "effort": "low", "confirm": True}), {})
    argv = rt.popen_calls[1]["argv"]
    assert argv == ["C:/bin/codex.cmd", "exec", "resume", "--json", "--skip-git-repo-check", "-m", "gpt-6-codex",
                    "-c", "model_reasoning_effort=low", "th-1", "-"]


def test_agents_model_effort_other_agents_get_the_model(store, rt):
    agents.p_send(H(store, {"agent": "freebuff", "message": "hi", "model": "deepseek/deepseek-v4-flash"}), {})
    assert rt.http_bodies[0]["model"] == "deepseek/deepseek-v4-flash"
    agents.p_send(H(store, {"agent": "antigravity", "message": "hi", "model": "gemini-3.8-flash-high"}), {})
    argv = rt.popen_calls[0]["argv"]
    assert argv[argv.index("--model") + 1] == "gemini-3.8-flash-high"


def test_agents_model_effort_refuses_models_the_registry_does_not_offer(store, rt):
    for bad in ("haiku-5.5", "not-a-model", "gpt-6-codex"):  # disabled, unknown, another agent's
        with pytest.raises(HttpError) as e:
            agents.p_send(H(store, {"agent": "claude_code", "message": "hi", "model": bad}), {})
        assert e.value.status == 400 and e.value.body["error"] == "model_not_offered"
    with pytest.raises(HttpError) as e:
        agents.p_send(H(store, {"agent": "claude_code", "message": "hi", "effort": "ludicrous"}), {})
    assert e.value.status == 400 and e.value.body["error"] == "bad_effort"
    with pytest.raises(HttpError) as e:
        agents.p_send(H(store, {"agent": "freebuff", "message": "hi", "effort": "high"}), {})
    assert e.value.body["error"] == "bad_effort"
    assert not rt.popen_calls and not rt.http_bodies


def test_agents_model_effort_codex_refusal_precedes_confirm(store, rt):
    with pytest.raises(HttpError) as e:
        agents.p_send(H(store, {"agent": "codex", "message": "hi", "model": "bogus"}), {})
    assert e.value.body["error"] == "model_not_offered"


def test_agents_model_effort_disabled_agent_is_refused(store, rt):
    rt.disabled_check = lambda a: a == "codex"
    with pytest.raises(HttpError) as e:
        agents.p_send(H(store, {"agent": "codex", "message": "hi", "confirm": True}), {})
    assert e.value.status == 409 and e.value.body["error"] == "agent_disabled"
    agents.p_send(H(store, {"agent": "claude_code", "message": "hi"}), {})
    assert len(rt.popen_calls) == 1
