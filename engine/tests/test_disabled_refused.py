"""G3 done-check: a switched-off agent, route or model is never called, whatever the UI sends.

Run: ``python -m pytest engine/tests -k disabled_refused -q``. Fakes and tmp_path databases only;
the Super-Agent ticket server is loaded from its file and never launches anything.
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from cc_engine import failure, mcp_server, runsheet
from cc_engine import scheduler as S
from cc_engine import toggles as T

from test_scheduler import OK_USAGE, T0, add_run, make_env, statuses

SERVER_PATH = Path(os.environ.get(
    "DSH_SUBAGENT_SERVER",
    r"~\workspace\waypoint-workspace\tools\dsh_subagent_mcp_server.py"))


def snap(**off):
    return T.Snapshot({k: {"enabled": v, "updated_at": None, "updated_by": "owner"} for k, v in off.items()})


# ---------------------------------------------------------------- dispatch
def test_disabled_refused_by_dispatch_route_and_model(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started")])
    d = env.dispatcher
    d.toggles_db = env.store
    node = S.get_node(env.store, "p1", "A")
    assert d.refusal(node) is None
    T.set_enabled("subagent:claude/sonnet-5.5", False, actor="owner", db=env.store)
    assert "switched off" in d.refusal(dict(node, model="sonnet-5.5"))
    T.set_enabled("claude_code", False, actor="owner", db=env.store)
    out = d.dispatch(node, {}, {"id": "p1"})
    assert out["status"] == "refused" and "Claude Code" in out["reason"] and env.launched == []


def test_disabled_refused_manual_routes_too(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started", {"pool": "gemini_app"})])
    env.dispatcher.toggles_db = env.store
    T.set_enabled("manual:gemini_app", False, actor="owner", db=env.store)
    out = env.dispatcher.dispatch(S.get_node(env.store, "p1", "A"), {}, {"id": "p1"})
    assert out["status"] == "refused" and env.asked == []


# ---------------------------------------------------------------- scheduler
def test_disabled_refused_by_scheduler_tick(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started", {"pool": "claude_code"})])
    T.set_enabled("claude_code", False, actor="owner", db=env.store)
    add_run(env.store, "p1", {"preset": "balanced", "pools": ["claude_code"]})  # the UI still ticks it
    res = S.tick(env.store, T0, env.dispatcher, usage_reader=OK_USAGE)
    assert res["started"] == [] and env.launched == []
    assert statuses(env.store)["A"] == "not_started"


def test_disabled_refused_unpinned_node_moves_to_an_allowed_pool(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started", {
        "pool": None, "model": None, "check": {"type": "text_present", "path": "a.md", "text": "x"}})])
    T.set_enabled("claude_code", False, actor="owner", db=env.store)
    add_run(env.store, "p1", {"preset": "balanced", "pools": ["claude_code", "freebuff"]})
    res = S.tick(env.store, T0, env.dispatcher, usage_reader=OK_USAGE)
    assert res["started"] == ["A"] and env.launched[0]["route"] == "freebuff"


def test_disabled_refused_scheduler_skips_a_disabled_ladder_model(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "cc_engine.models", None)
    env = make_env(tmp_path, [("A", [], "not_started", {"pool": None, "model": None})])
    T.set_enabled("subagent:claude/sonnet", False, actor="owner", db=env.store)
    add_run(env.store, "p1", {"preset": "balanced", "pools": ["claude_code"]})
    res = S.tick(env.store, T0, env.dispatcher, usage_reader=OK_USAGE)
    assert res["started"] == ["A"]
    assert env.store.get_node("p1", "A")["model"] == "opus"
    assert "sonnet" not in json.dumps(env.launched[0]["argv"])


# ---------------------------------------------------------------- failure ladder
SETTINGS = {"model_ladder": {"claude_code": ["haiku", "sonnet", "opus"]}, "effort": "medium"}


def test_disabled_refused_by_failure_next_rung():
    node = {"id": "A", "pool": "claude_code", "model": "haiku", "effort": "high"}
    assert failure.next_rung(node, SETTINGS, toggles=snap()) == ("sonnet", "high")
    off = T.Snapshot({"subagent:claude/sonnet": {"enabled": False, "updated_at": None, "updated_by": "o"}})
    assert failure.next_rung(node, SETTINGS, toggles=off) == ("opus", "high")
    assert failure.next_rung(node, SETTINGS, toggles=snap(claude_code=False)) is None


def test_disabled_refused_check_failure_reports_when_only_disabled_rungs_left(tmp_path):
    env = make_env(tmp_path, [("A", [], "submitted", {"pool": "claude_code", "model": "haiku",
                                                      "effort": "high"})])
    for m in ("sonnet", "opus"):
        T.set_enabled(f"subagent:claude/{m}", False, actor="owner", db=env.store)
    node = env.store.get_node("p1", "A")
    out = failure.on_check_failed(env.store, "p1", node, SETTINGS, "check failed")
    assert out["action"] == "report"


# ---------------------------------------------------------------- run sheet
def test_disabled_refused_by_runsheet_expand():
    off = snap(freebuff=False)
    out = runsheet.expand({"preset": "balanced", "pools": ["claude_code", "freebuff"]}, toggles=off)
    assert "freebuff" not in out["model_ladder"] and "claude_code" in out["model_ladder"]
    model_off = T.Snapshot({"subagent:claude/sonnet": {"enabled": False, "updated_at": None, "updated_by": "o"}})
    out = runsheet.expand({"preset": "balanced", "pools": ["claude_code"]}, toggles=model_off)
    assert out["model_ladder"]["claude_code"] == ["opus"]
    out = runsheet.expand({"preset": "balanced", "pools": ["codex"]}, toggles=snap())  # Codex off by default
    assert out["model_ladder"] == {}
    out = runsheet.expand({"preset": "balanced", "pools": ["freebuff"],
                           "sprints": [{"pool": "freebuff", "start_at": "2026-11-04T12:00:00Z"}]}, toggles=off)
    assert out["sprints"] == []
    assert S.expand_settings({"pools": ["claude_code", "freebuff"]}, toggles=off)["pools"] == ["claude_code"]


# ---------------------------------------------------------------- Autonomous writes
def test_disabled_refused_by_autonomous_routes(tmp_path, monkeypatch):
    from test_autonomous import App, call, http_status
    monkeypatch.setitem(sys.modules, "cc_engine.models", None)
    env = make_env(tmp_path, [("A", [], "not_started")])
    app = App(env.store)
    status, body = http_status(lambda: call(app, "POST /api/autonomous/sprint", {"kind": "codex", "confirm": True}))
    assert status == 403 and body["error"] == "disabled"
    T.set_enabled("claude_code", False, actor="owner", db=env.store)
    ids = [m["id"] for m in call(app, "GET /api/autonomous")["models"]]
    assert "claude-opus-5.5" not in ids and "agent:codex" not in ids
    status, body = http_status(lambda: call(app, "POST /api/autonomous/settings",
                                            {"settings": {"models": ["claude-opus-5.5"]}}))
    assert status == 400 and "off in Agents" in body["detail"]
    status, _ = http_status(lambda: call(app, "POST /api/autonomous/sprint", {"kind": "claude"}))
    assert status == 403
    status, _ = http_status(lambda: call(app, "POST /api/sprints/plan", {"project": "p1", "pool": "claude_code"}))
    assert status == 403


# ---------------------------------------------------------------- Router MCP tools
class FakeHttp:
    def __init__(self, toggles):
        self.toggles = toggles
        self.calls = []

    def request(self, method, path, body=None):
        self.calls.append((method, path))
        p = path.split("?")[0]
        if p == "/api/toggles":
            return (200, self.toggles) if self.toggles is not None else (404, {"error": "not_found"})
        if p == "/api/projects":
            return 200, {"projects": []}
        if p == "/health":
            return 200, {"revision": 1}
        if p == "/api/plan/import":
            return 200, {"ok": True, "project": body["plan"]["id"], "revision": 2, "status": "draft"}
        return 404, {}


def plan_args(pool):
    return {"title": "T", "brief": "B", "nodes": [{"id": "a", "title": "A", "goal": "G.", "deps": [],
                                                   "check": {"type": "command", "run": "exit 0"}, "pool": pool}]}


def test_disabled_refused_by_mcp_draft_plan():
    http = FakeHttp({"enabled": {"freebuff": False, "claude_code": True}, "aliases": {}})
    with pytest.raises(ValueError, match="switched off"):
        mcp_server.call_tool("cc_draft_plan", plan_args("freebuff"), http)
    assert not any(c[0] == "POST" for c in http.calls)
    out = mcp_server.call_tool("cc_draft_plan", plan_args("claude_code"), http)
    assert "Drafted" in out
    # The engine without /api/toggles: drafting still works (dispatch refuses later anyway).
    assert "Drafted" in mcp_server.call_tool("cc_draft_plan", plan_args("freebuff"), FakeHttp(None))


def test_disabled_refused_mcp_tool_schema_hides_disabled_pools():
    http = FakeHttp({"enabled": {"freebuff": False}, "aliases": {}})
    resp = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, http)
    tool = [t for t in resp["result"]["tools"] if t["name"] == "cc_draft_plan"][0]
    enum = tool["inputSchema"]["properties"]["nodes"]["items"]["properties"]["pool"]["enum"]
    assert "freebuff" not in enum and "claude_code" in enum


# ---------------------------------------------------------------- Super-Agent ticket server
@pytest.fixture
def server(monkeypatch, tmp_path):
    if not SERVER_PATH.is_file():
        pytest.skip(f"ticket server not found at {SERVER_PATH}")
    spec = importlib.util.spec_from_file_location("dsh_subagent_mcp_server_g3", SERVER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "DENY", set())
    monkeypatch.setattr(mod, "MODELS_DISABLED_FILE", str(tmp_path / "no_models_disabled.json"))  # never the live file
    monkeypatch.setattr(mod, "TICKETS_DIR", tmp_path / "tickets")
    launched = []

    class Boom:
        def __init__(self, *a, **k):
            launched.append(a)
            raise RuntimeError("never launch in tests")

    monkeypatch.setattr(mod.subprocess, "Popen", Boom)
    monkeypatch.setattr(mod, "_run_cmd", lambda *a, **k: launched.append(a) or "ran")
    mod.launched = launched
    return mod


def test_disabled_refused_by_ticket_server(server, monkeypatch):
    monkeypatch.setattr(server, "_fetch_toggles", lambda: {"enabled": {"groq": False}, "aliases": {}})
    with pytest.raises(ValueError, match="switched off"):
        server.dispatch_ticket({"runner": "groq", "prompt": "x"})
    with pytest.raises(ValueError, match="switched off"):
        server.call_tool("delegate_groq_subagent", {"prompt": "x"})
    assert server.launched == []
    assert server.call_tool("delegate_freebuff_subagent", {"prompt": "x"}) == "ran"


def test_disabled_refused_ticket_server_codex_off_until_enabled(server, monkeypatch):
    monkeypatch.setattr(server, "_fetch_toggles", lambda: None)  # engine down, db unreadable
    with pytest.raises(ValueError, match="Codex"):
        server.call_tool("delegate_codex_metered_subagent", {"prompt": "x"})
    monkeypatch.setattr(server, "_fetch_toggles", lambda: {"enabled": {"codex": False}, "aliases": {}})
    with pytest.raises(ValueError, match="Codex"):
        server.dispatch_ticket({"runner": "codex", "prompt": "x"})
    monkeypatch.setattr(server, "_fetch_toggles", lambda: {"enabled": {"codex": True}, "aliases": {}})
    assert server.call_tool("delegate_codex_metered_subagent", {"prompt": "x"}) == "ran"
    names = [t["name"] for t in server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]]
    assert "delegate_codex_metered_subagent" in names
    monkeypatch.setattr(server, "_fetch_toggles", lambda: {"enabled": {"codex": False}, "aliases": {}})
    names = [t["name"] for t in server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]]
    assert "delegate_codex_metered_subagent" not in names


def test_disabled_refused_ticket_server_stale_models_file_is_a_mirror_once_migrated(server, monkeypatch, tmp_path):
    mirror = tmp_path / "models_disabled.json"
    mirror.write_text(json.dumps({"migrated_to_toggles": True, "runners": {"codex": {"all": True}}}), encoding="utf-8")
    monkeypatch.setattr(server, "MODELS_DISABLED_FILE", str(mirror))
    # toggles answer and say Codex is on: the lagging mirror must not refuse it
    monkeypatch.setattr(server, "_fetch_toggles", lambda: {"enabled": {"codex": True}, "aliases": {}})
    assert server.call_tool("delegate_codex_metered_subagent", {"prompt": "x"}) == "ran"
    # an unmigrated file is still the authority for what it says, and nothing weakens when toggles are unreadable
    mirror.write_text(json.dumps({"runners": {"codex": {"all": True}}}), encoding="utf-8")
    with pytest.raises(ValueError, match="Waypoint Models tab"):
        server.call_tool("delegate_codex_metered_subagent", {"prompt": "x"})
    monkeypatch.setattr(server, "_fetch_toggles", lambda: None)
    mirror.write_text(json.dumps({"migrated_to_toggles": True, "runners": {"groq": {"all": True}}}), encoding="utf-8")
    with pytest.raises(ValueError, match="Waypoint Models tab"):
        server.call_tool("delegate_groq_subagent", {"prompt": "x"})


def test_disabled_refused_ticket_server_reads_db_when_engine_down(server, monkeypatch, tmp_path):
    db = tmp_path / "cc.sqlite"
    T.set_enabled("groq", False, actor="owner", db=db)
    monkeypatch.setattr(server, "TOGGLES_URL", "http://127.0.0.1:9/api/toggles")  # nothing listens
    monkeypatch.setattr(server, "TOGGLES_DB", str(db))
    with pytest.raises(ValueError, match="switched off"):
        server.call_tool("delegate_groq_subagent", {"prompt": "x"})
