"""Models registry (M1): fixture configs only, tmp_path state, no real provider calls."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cc_engine import models
from cc_engine.api import HttpError

CORDIS = """\
# fixture patch
- id: agent-default-model
  config:
    provider: freebuff
    model: deepseek/deepseek-v4-flash

- id: llm-pi-ai
  config:
    providers:
      freebuff:
        displayName: 1. Freebuff ($0 Unmetered Pool)
        apiKeyEnv: FREEBUFF_API_KEY
        baseURL: http://127.0.0.1:1455/v1
        models:
          - id: deepseek/deepseek-v4-flash
            name: DeepSeek V4.1 Flash ($0 Default)
            contextWindow: 131072
            maxTokens: 32768
          - id: openai/gpt-6-luna
            name: GPT-6 Luna ($0 Freebuff)
            contextWindow: 131072
      github-copilot:
        displayName: 3. GitHub Copilot Student ($0 Included)
        models:
          - id: kimi-k3
            name: Kimi K3 (1M Context - GitHub Student)
          - id: gone-model
            name: Not on the plan any more
      google:
        displayName: 4. Google AI Studio Pool ($0 API Keys)
        apiKeyEnv: GEMINI_API_KEY
        models:
          - id: gemini-3.8-flash
            name: Gemini 3.8 Flash (AI Studio $0)
          - id: gemini-3.6-flash
            name: Gemini 3.6 Flash (AI Studio $0)
      groq:
        displayName: 5. Groq LPU ($0 Sub-Second)
        apiKeyEnv: GROQ_API_KEY
        models:
          - id: llama-3.3-70b-versatile
            name: Llama 3.3 70B (Groq LPU $0)
      openai-codex:
        displayName: 6. OpenAI Codex OAuth (Metered ChatGPT Plus)
        models:
          - id: gpt-6.1-sol
            name: GPT-6.1 Sol (Codex OAuth - Metered)
      anthropic:
        displayName: 7. Claude Code Pro OAuth (Metered Pro)
        models:
          - id: claude-opus-5-5
            name: Claude Opus 5.5 (Claude Pro OAuth - Metered)

- insert:
    - id: mcp-rob-subagents
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: rob_subagents
        env:
          DSH_SUBAGENT_DENY: delegate_codex_metered_subagent,runner:codex
    - id: preset-x
      config:
        plugins:
          - id: persona
            config:
              prefix: >-
                You are a fixture persona. models: 'not-a-model-id'
                second line
"""

CREDENTIALS = """\
version: 1
refs:
  FREEBUFF_API_KEY: freebuff-local
  GEMINI_API_KEY: SECRET-VALUE-AAA
records:
  llm-pi-ai/openai-codex:
    kind: grant
  llm-pi-ai/anthropic:
    kind: grant
  llm-pi-ai/github-copilot:
    kind: grant
    payload:
      type: oauth
      availableModelIds:
      - kimi-k3
      - gpt-6-luna
"""

MCP = '''\
TOOLS = [
    {
        "name": "dispatch_subagent_ticket",
        "inputSchema": {"properties": {"runner": {}, "model": {}}},
    },
    {
        "name": "delegate_claude_code_subagent",
        "inputSchema": {"properties": {
            "model": {"type": "string", "description": "Model name: 'opus-5.5' (default), 'sonnet-5.5', or 'haiku-5.5'."},
        }},
    },
    {
        "name": "delegate_codex_metered_subagent",
        "inputSchema": {"properties": {"prompt": {}}},
    },
    {
        "name": "delegate_groq_subagent",
        "inputSchema": {"properties": {"model": {"description": "Groq model (e.g. 'openai/gpt-oss-120b')."}}},
    },
    {
        "name": "delegate_gemini_studio_subagent",
        "inputSchema": {"properties": {"prompt": {}}},
    },
    {
        "name": "delegate_freebuff_subagent",
        "inputSchema": {"properties": {"model": {"description": "Model ID (default: 'deepseek/deepseek-v4-flash')."}}},
    },
    {
        "name": "delegate_antigravity_subagent",
        "description": (
            "Supports models: 'gemini-3.8-flash-high', 'gemini-4-argon-high', 'claude-opus-5-5-high'."
        ),
        "inputSchema": {"properties": {"model": {"description": "Antigravity model ID (default: 'gemini-3.8-flash-high')."}}},
    },
]

def call_tool(name, args):
    runner_map = {
        "delegate_claude_code_subagent": ("claude", 600),
        "delegate_codex_metered_subagent": ("codex", 600),
        "delegate_groq_subagent": ("groq", 120),
        "delegate_gemini_studio_subagent": ("gemini", 300),
        "delegate_freebuff_subagent": ("freebuff", 180),
        "delegate_antigravity_subagent": ("antigravity", 600),
    }
'''

USAGE = [
    {"pool": "claude_5h", "label": "Claude 5 hours", "remaining_percent": 62.0, "status": "FRESH",
     "resets_at": "2026-11-04T18:00:00+00:00"},
    {"pool": "claude_week", "label": "Claude week", "remaining_percent": 40.0, "status": "FRESH",
     "resets_at": "2026-11-09T09:00:00+00:00"},
    {"pool": "codex_5h", "label": "Codex 5 hours", "remaining_percent": None, "status": "UNKNOWN", "resets_at": None},
    {"pool": "antigravity_argon_week", "label": "Argon week", "remaining_percent": 55.0, "status": "STALE",
     "resets_at": None},
    {"pool": "gemini_app", "label": "Gemini app", "remaining_percent": None, "status": "UNKNOWN", "resets_at": None},
]


@pytest.fixture
def src(tmp_path):
    (tmp_path / "cordis.patch.yml").write_text(CORDIS, encoding="utf-8")
    (tmp_path / "credentials.yaml").write_text(CREDENTIALS, encoding="utf-8")
    (tmp_path / "mcp.py").write_text(MCP, encoding="utf-8")
    (tmp_path / ".env").write_text("GEMINI_API_KEY_A=SECRET-VALUE-AAA\nGEMINI_API_KEY_B='SECRET-VALUE-BBB'\n",
                                   encoding="utf-8")
    return models.Sources(cordis=tmp_path / "cordis.patch.yml", credentials=tmp_path / "credentials.yaml",
                          mcp_server=tmp_path / "mcp.py", state=tmp_path / "data" / "models_disabled.json",
                          env_file=tmp_path / ".env", environ={}, registry_reader=lambda name: None)


def rows_by_id(src, usage=USAGE):
    return {r["id"]: r for r in models.build_registry(src, usage_rows=usage)}


class H:
    def __init__(self, src, body=None, actor="owner", store=None):
        self.app = SimpleNamespace(models_sources=src, usage_reader=lambda: USAGE, store=store)
        self.actor = actor
        self._body = body or {}

    def _json_body(self):
        return self._body


# ---------------------------------------------------------------- parser
def test_models_yaml_subset_parses_cordis_shapes():
    tree = models.parse_yaml(CORDIS)
    assert isinstance(tree, list)
    prov = models.cordis_providers(tree)
    assert list(prov) == ["freebuff", "github-copilot", "google", "groq", "openai-codex", "anthropic"]
    assert prov["freebuff"]["models"][0] == {"id": "deepseek/deepseek-v4-flash", "name": "DeepSeek V4.1 Flash ($0 Default)",
                                            "contextWindow": 131072, "maxTokens": 32768}
    assert models.cordis_default(tree) == {"provider": "freebuff", "model": "deepseek/deepseek-v4-flash"}
    assert models.cordis_deny(tree) == {"delegate_codex_metered_subagent", "runner:codex"}


def test_models_missing_files_never_raise(tmp_path):
    src = models.Sources(cordis=tmp_path / "no.yml", credentials=tmp_path / "no.yaml", mcp_server=tmp_path / "no.py",
                         state=tmp_path / "s.json", env_file=tmp_path / "no.env", environ={},
                         registry_reader=lambda n: None)
    rows = models.build_registry(src, usage_rows=[])
    ids = {r["id"] for r in rows}
    # Meridian's own routes and the manual hand-offs are always there.
    assert {"agent:claude_code", "agent:freebuff", "agent:antigravity", "agent:codex",
            "manual:gemini_app", "manual:chatgpt_web", "manual:manual"} <= ids
    assert not [r for r in rows if r["kind"] == "chat"]


# ---------------------------------------------------------------- registry contents
def test_models_registry_lists_everything_dsh_is_configured_with(src):
    rows = rows_by_id(src)
    for rid in ("chat:freebuff/deepseek/deepseek-v4-flash", "chat:freebuff/openai/gpt-6-luna",
                "chat:github-copilot/kimi-k3",
                "chat:google/gemini-3.8-flash", "chat:groq/llama-3.3-70b-versatile",
                "chat:openai-codex/gpt-6.1-sol", "chat:anthropic/claude-opus-5-5",
                "subagent:claude/opus-5.5", "subagent:claude/sonnet-5.5", "subagent:claude/haiku-5.5",
                "subagent:antigravity/gemini-4-argon-high", "subagent:antigravity/gemini-3.8-flash-high",
                "agent:claude_code", "agent:freebuff", "agent:antigravity", "agent:codex",
                "manual:gemini_app", "manual:chatgpt_web", "manual:manual"):
        assert rid in rows, rid
    keys = {"id", "label", "provider", "kind", "route", "enabled", "elo", "context_window", "usage", "source",
            "roles", "speed", "run_natively", "run_natively_reason", "elo_by_effort", "strongest"}
    for r in rows.values():
        assert keys <= set(r), r["id"]
        assert r["kind"] in ("chat", "subagent", "agent", "manual")
        assert r["enabled"] is True
        assert isinstance(r["source"], str) and r["source"]


def test_models_codex_is_only_robert_own_agent(src):
    rows = rows_by_id(src)
    codex = rows["agent:codex"]
    assert codex["route"] is None and codex["runner"] is None and codex["roles"] == []
    assert codex["run_natively"] is True
    # No Codex subagent row, even though the server source lists a codex runner.
    assert not [r for r in rows.values() if r.get("runner") == "codex" or r["id"].startswith("subagent:codex")]
    # Codex OAuth chat models are in neither dsh picker.
    assert rows["chat:openai-codex/gpt-6.1-sol"]["roles"] == []
    assert all(r["route"] not in ("codex", "codex_cli") for r in rows.values())


def test_models_claude_only_as_subagent(src):
    rows = rows_by_id(src)
    assert rows["chat:anthropic/claude-opus-5-5"]["roles"] == []
    assert rows["chat:anthropic/claude-opus-5-5"]["run_natively"] is True
    assert rows["chat:anthropic/claude-opus-5-5"]["run_natively_reason"]
    sub = rows["subagent:claude/opus-5.5"]
    assert sub["roles"] == ["subagent"] and sub["route"] == "claude_code" and sub["runner"] == "claude"
    assert not [r for r in rows.values() if "agent" in r["roles"] and "claude" in r["id"]]


def test_models_roles_follow_runners(src):
    rows = rows_by_id(src)
    assert rows["chat:freebuff/openai/gpt-6-luna"]["roles"] == ["agent", "subagent"]
    assert rows["chat:freebuff/openai/gpt-6-luna"]["runner"] == "freebuff"
    assert rows["chat:freebuff/openai/gpt-6-luna"]["route"] == "freebuff"
    assert rows["chat:github-copilot/kimi-k3"]["roles"] == ["agent"]  # no subagent runner for Copilot
    # The Gemini runner takes no model argument: only its default model is a subagent.
    assert rows["chat:google/gemini-3.8-flash"]["roles"] == ["agent", "subagent"]
    assert rows["chat:google/gemini-3.6-flash"]["roles"] == ["agent"]
    assert rows["chat:groq/llama-3.3-70b-versatile"]["roles"] == ["agent", "subagent"]


def test_models_access_from_dsh_credentials(src):
    rows = rows_by_id(src)
    assert rows["chat:github-copilot/kimi-k3"]["access"] is True
    assert rows["chat:github-copilot/gone-model"]["access"] is False
    assert rows["chat:groq/llama-3.3-70b-versatile"]["access"] is False  # GROQ_API_KEY ref missing
    assert rows["chat:google/gemini-3.8-flash"]["access"] is True
    assert rows["chat:anthropic/claude-opus-5-5"]["access"] is True


def test_models_facts_are_sourced_and_never_invented(src):
    rows = rows_by_id(src)
    argon = rows["subagent:antigravity/gemini-4-argon-high"]
    # Sourced from the public Arena text board (re-read 4 Nov 2026), never estimated.
    assert argon["elo"] == 1520 and "arena.ai/leaderboard/text" in argon["sources"]["elo"]
    assert argon["elo_by_effort"]["high"].lstrip("~") == "1520" and argon["elo_by_effort"]["low"] == "n/a"
    assert argon["effort"] == "high"
    assert argon["context_window"] == 1048576 and argon["sources"]["context_window"]
    # Provider config wins for context windows.
    fb = rows["chat:freebuff/deepseek/deepseek-v4-flash"]
    assert fb["context_window"] == 131072 and "cordis" in fb["sources"]["context_window"]
    assert fb["speed"] == "227 tok/s" and fb["sources"]["speed"]
    # Unknown stays null / n/a: these are not on the board.
    unknown = [r for r in rows.values() if r["elo"] is None]
    assert unknown, "some fixture models have no sourced Elo"
    for r in unknown:
        assert set(r["elo_by_effort"].values()) <= {"n/a"}, r["id"]
    for r in rows.values():
        if r["elo"] is not None:
            assert r["sources"].get("elo"), r["id"]
        if r["context_window"] is not None:
            assert r["sources"].get("context_window"), r["id"]
        if r["speed"] is not None:
            assert r["sources"].get("speed"), r["id"]


def test_models_strongest_per_pool_rule(src):
    rows = rows_by_id(src)
    # Antigravity: highest known Elo wins.
    agy = [r for r in rows.values() if r.get("pool") == "runner:antigravity"]
    assert [r["id"] for r in agy if r["strongest"]] == ["subagent:antigravity/gemini-4-argon-high"]
    assert "Elo" in rows["subagent:antigravity/gemini-4-argon-high"]["strongest_rule"]
    # Freebuff: no Elo known -> dsh's configured default model.
    fb = [r for r in rows.values() if r.get("pool") == "provider:freebuff"]
    assert [r["id"] for r in fb if r["strongest"]] == ["chat:freebuff/deepseek/deepseek-v4-flash"]
    # A model without access is never the pool's pick.
    cop = [r for r in rows.values() if r.get("pool") == "provider:github-copilot"]
    assert [r["id"] for r in cop if r["strongest"]] == ["chat:github-copilot/kimi-k3"]
    assert "agent:chatmode" not in rows and "manual:chatgpt_web" in rows


def test_models_strongest_rule_survives_lineup_change(src, tmp_path):
    text = CORDIS.replace("          - id: openai/gpt-6-luna\n",
                          "          - id: claude-opus-5-5\n            name: Opus in Freebuff\n"
                          "          - id: openai/gpt-6-luna\n")
    Path(src.cordis).write_text(text, encoding="utf-8")
    rows = rows_by_id(src)
    fb = [r for r in rows.values() if r.get("pool") == "provider:freebuff"]
    assert [r["id"] for r in fb if r["strongest"]] == ["chat:freebuff/claude-opus-5-5"]


def test_models_run_natively_markers(src):
    rows = rows_by_id(src)
    assert rows["subagent:antigravity/gemini-4-argon-high"]["run_natively"] is True
    assert rows["subagent:antigravity/gemini-3.8-flash-high"]["run_natively"] is False
    # Claude through the Claude Code CLI runner already is the native route.
    assert rows["subagent:claude/opus-5.5"]["run_natively"] is False
    assert rows["chat:freebuff/openai/gpt-6-luna"]["run_natively"] is False
    assert rows["chat:freebuff/openai/gpt-6-luna"]["run_natively_reason"] is None
    assert "limited" in (rows["chat:github-copilot/kimi-k3"]["note"] or "")


def test_models_usage_from_cache(src):
    rows = rows_by_id(src)
    u = rows["agent:claude_code"]["usage"]
    assert u["remaining_percent"] == 40.0 and u["status"] == "FRESH" and u["resets_at"].startswith("2026-11-09")
    assert rows["subagent:antigravity/gemini-4-argon-high"]["usage"]["remaining_percent"] == 55.0
    assert rows["agent:codex"]["usage"]["status"] == "UNKNOWN"
    assert rows["agent:codex"]["usage"]["remaining_percent"] is None
    assert rows["chat:freebuff/openai/gpt-6-luna"]["usage"] is None  # no usage source: blank
    assert rows["manual:gemini_app"]["usage"]["status"] == "UNKNOWN"


def test_models_lookup_for_picker_badges(src):
    row = models.lookup("freebuff", "deepseek/deepseek-v4-flash", sources=src, usage_rows=[])
    assert row["id"] == "chat:freebuff/deepseek/deepseek-v4-flash"
    assert "128K" in row["badge"] and "227 tok/s" in row["badge"]
    assert models.lookup("nope", "x", sources=src, usage_rows=[]) is None


# ---------------------------------------------------------------- enable / disable
def test_models_toggle_persists_and_dispatch_refuses(src):
    assert models.is_disabled(route="freebuff", model="openai/gpt-6-luna", path=src.state) is False
    models.set_enabled(["chat:freebuff/openai/gpt-6-luna"], False, sources=src)
    assert json.loads(Path(src.state).read_text(encoding="utf-8"))["disabled"] == ["chat:freebuff/openai/gpt-6-luna"]
    assert models.is_disabled(route="freebuff", model="openai/gpt-6-luna", path=src.state) is True
    assert models.is_disabled(model="openai/gpt-6-luna", path=src.state) is True
    assert models.is_disabled(route="freebuff", model="deepseek/deepseek-v4-flash", path=src.state) is False
    assert models.is_disabled(route="freebuff", path=src.state) is False
    rows = rows_by_id(src)
    assert rows["chat:freebuff/openai/gpt-6-luna"]["enabled"] is False
    models.set_enabled(["chat:freebuff/openai/gpt-6-luna"], True, sources=src)
    assert models.is_disabled(route="freebuff", model="openai/gpt-6-luna", path=src.state) is False


def test_models_route_disable_and_aliases(src):
    models.set_enabled(["agent:antigravity"], False, sources=src)
    assert models.is_disabled(route="antigravity", path=src.state) is True
    assert models.is_disabled(route="antigravity", model="anything", path=src.state) is True
    models.set_enabled(["agent:antigravity"], True, sources=src)
    models.set_enabled(["subagent:claude/opus-5.5"], False, sources=src)
    # Meridian's claude_code ladder says "opus"; the MCP tool says "opus-5.5": both refused.
    assert models.is_disabled(route="claude_code", model="opus", path=src.state) is True
    assert models.is_disabled(route="claude_code", model="claude-opus-5-5", path=src.state) is True
    assert models.is_disabled(route="claude_code", model="sonnet", path=src.state) is False
    state = json.loads(Path(src.state).read_text(encoding="utf-8"))
    assert "opus-5.5" in state["runners"]["claude"]["models"]


def test_models_runner_default_flag_for_mcp(src):
    models.set_enabled(["chat:freebuff/deepseek/deepseek-v4-flash"], False, sources=src)
    state = json.loads(Path(src.state).read_text(encoding="utf-8"))
    assert state["runners"]["freebuff"]["default"] is True
    assert state["routes"]["freebuff"]["default"] is True


def test_models_is_disabled_tolerates_bad_state(tmp_path):
    bad = tmp_path / "models_disabled.json"
    assert models.is_disabled(route="freebuff", path=bad) is False
    bad.write_text("{not json", encoding="utf-8")
    assert models.is_disabled(route="freebuff", model="x", path=bad) is False
    bad.write_text(json.dumps({"routes": ["wrong"]}), encoding="utf-8")
    assert models.is_disabled(route="freebuff", model="x", path=bad) is False


def test_models_unknown_id_rejected(src):
    with pytest.raises(ValueError):
        models.set_enabled(["chat:nope/none"], False, sources=src)


# ---------------------------------------------------------------- HTTP handlers
def test_models_routes_registered():
    assert {"GET /api/models", "POST /api/models/toggle", "GET /api/models/lookup",
            "GET /api/models/gemini_keys", "POST /api/models/gemini_keys/switch"} <= set(models.ROUTES)


def test_models_get_route_matches_contract(src):
    out = models.ROUTES["GET /api/models"](H(src), {})
    assert isinstance(out["models"], list) and out["models"]
    row = next(r for r in out["models"] if r["id"] == "agent:claude_code")
    assert set(row["usage"]) >= {"remaining_percent", "resets_at", "status"}
    assert out["kinds"] == ["chat", "subagent", "agent", "manual"]


def test_models_toggle_route_owner_only(src):
    with pytest.raises(HttpError) as e:
        models.ROUTES["POST /api/models/toggle"](H(src, {"id": "agent:freebuff", "enabled": False}, actor="router"), {})
    assert e.value.status == 403
    assert models.is_disabled(route="freebuff", path=src.state) is False
    out = models.ROUTES["POST /api/models/toggle"](H(src, {"id": "agent:freebuff", "enabled": False}), {})
    assert out["ok"] is True and out["model"]["id"] == "agent:freebuff" and out["model"]["enabled"] is False
    assert models.is_disabled(route="freebuff", path=src.state) is True


def test_models_toggle_route_bulk_and_validation(src):
    ids = ["chat:groq/llama-3.3-70b-versatile", "subagent:claude/haiku-5.5"]
    out = models.ROUTES["POST /api/models/toggle"](H(src, {"ids": ids, "enabled": False}), {})
    assert out["ok"] is True and sorted(m["id"] for m in out["models"]) == sorted(ids)
    for body in ({"id": "agent:freebuff"}, {"id": "agent:freebuff", "enabled": "no"}, {"enabled": True},
                 {"id": "chat:missing/x", "enabled": False}):
        with pytest.raises(HttpError) as e:
            models.ROUTES["POST /api/models/toggle"](H(src, body), {})
        assert e.value.status in (400, 404)


def test_models_api_serves_extension(tmp_path, monkeypatch, src):
    """The real api.App picks the module up through EXTENSIONS."""
    from cc_engine import api
    from cc_engine.store import Store

    store = Store(tmp_path / "cc.sqlite")
    try:
        app = api.App(store, usage_reader=lambda: USAGE, token="t", token_path=tmp_path / "engine.token")
        app.models_sources = src
        assert "GET /api/models" in app.extension_routes()
        assert "POST /api/models/toggle" in app.extension_routes()
    finally:
        store.close()


# ---------------------------------------------------------------- dsh MCP server guard
MCP_SERVER = Path(r"~\workspace\waypoint-workspace\tools\dsh_subagent_mcp_server.py")


@pytest.mark.skipif(not MCP_SERVER.is_file(), reason="dsh MCP server not installed")
def test_models_dsh_mcp_server_refuses_disabled(src, monkeypatch):
    spec = importlib.util.spec_from_file_location("dsh_mcp_under_test", MCP_SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "MODELS_DISABLED_FILE", str(src.state))
    monkeypatch.setattr(mod, "_fetch_toggles", lambda: {"enabled": {}, "aliases": {}})  # never the live engine
    # Missing file: nothing refused.
    assert mod._models_refusal("freebuff", "openai/gpt-6-luna") is None
    models.set_enabled(["chat:freebuff/openai/gpt-6-luna", "agent:antigravity"], False, sources=src)
    assert "turned off" in mod._models_refusal("freebuff", "openai/gpt-6-luna")
    assert mod._models_refusal("freebuff", "deepseek/deepseek-v4-flash") is None
    assert "turned off" in mod._models_refusal("antigravity", None)
    launched = []
    monkeypatch.setattr(mod, "_run_cmd", lambda *a, **k: launched.append(a) or "ran")
    monkeypatch.setattr(mod, "_build_runner_command", lambda runner, args: (["x"], None, None))
    with pytest.raises(ValueError, match="turned off"):
        mod.call_tool("delegate_freebuff_subagent", {"prompt": "hi", "model": "openai/gpt-6-luna"})
    with pytest.raises(ValueError, match="turned off"):
        mod.dispatch_ticket({"runner": "antigravity", "prompt": "hi"})
    assert launched == []
    assert mod.call_tool("delegate_freebuff_subagent", {"prompt": "hi"}) == "ran"
    # A corrupt file never breaks the server.
    Path(src.state).write_text("{bad", encoding="utf-8")
    assert mod._models_refusal("antigravity", None) is None


# ---------------------------------------------------------------- v3: full lineup, toggles
SETUP_SRC = '''\
from typing import Any
_FREEBUFF_MODEL = {"contextWindow": 131072, "maxTokens": 32768}
PROVIDER_CATALOG: list[tuple[str, dict[str, Any]]] = [
    ("freebuff", {
        "displayName": "1. Freebuff",
        "apiKeyEnv": "FREEBUFF_API_KEY",
        "models": [
            {"id": "deepseek/deepseek-v4-flash", "name": "DeepSeek V4.1 Flash", **_FREEBUFF_MODEL},
            {"id": "openai/gpt-6-luna", "name": "GPT-6 Luna", **_FREEBUFF_MODEL},
            {"id": "mimo/mimo-v2.6-pro", "name": "MiMo 2.6 Pro", **_FREEBUFF_MODEL},
        ],
    }),
    ("groq", {"displayName": "5. Groq", "apiKeyEnv": "GROQ_API_KEY",
              "models": [{"id": "llama-3.3-70b-versatile", "name": "Llama 3.3 70B"},
                         {"id": "qwen/qwen3.8-27b", "name": "Qwen 3.8 27B"}]}),
]
def not_parsed():
    PROVIDER_CATALOG = []
'''


def test_models_full_lineup_reads_the_setup_script_source(tmp_path):
    script = tmp_path / "setup.py"
    script.write_text(SETUP_SRC, encoding="utf-8")
    lineup = models.setup_lineup(script)
    assert list(lineup) == ["freebuff", "groq"]
    assert [m["id"] for m in lineup["freebuff"]["models"]][-1] == "mimo/mimo-v2.6-pro"
    assert lineup["freebuff"]["models"][2]["contextWindow"] == 131072
    assert models.setup_lineup(tmp_path / "missing.py") == {}
    (tmp_path / "bad.py").write_text("PROVIDER_CATALOG = [", encoding="utf-8")
    assert models.setup_lineup(tmp_path / "bad.py") == {}


def test_models_tab_shows_full_lineup_and_marks_picker_rows(src, tmp_path):
    script = tmp_path / "setup.py"
    script.write_text(SETUP_SRC, encoding="utf-8")
    src.lineup = script
    rows = rows_by_id(src)
    # trimmed patch rows and lineup-only rows are all on the Models tab
    for rid in ("chat:freebuff/deepseek/deepseek-v4-flash", "chat:freebuff/mimo/mimo-v2.6-pro",
                "chat:groq/qwen/qwen3.8-27b", "chat:github-copilot/kimi-k3", "chat:anthropic/claude-opus-5-5"):
        assert rid in rows, rid
    assert rows["chat:freebuff/deepseek/deepseek-v4-flash"]["in_picker"] is True
    assert rows["chat:groq/llama-3.3-70b-versatile"]["in_picker"] is True
    assert rows["chat:freebuff/mimo/mimo-v2.6-pro"]["in_picker"] is False
    assert rows["chat:groq/qwen/qwen3.8-27b"]["in_picker"] is False
    assert "PROVIDER_CATALOG" in rows["chat:freebuff/mimo/mimo-v2.6-pro"]["source"]
    # the patch's own name/context wins for a model both list
    assert rows["chat:freebuff/deepseek/deepseek-v4-flash"]["label"] == "DeepSeek V4.1 Flash ($0 Default)"
    assert all(r["in_picker"] is False for r in rows.values() if r["kind"] != "chat")


def test_models_real_setup_script_lineup_is_the_full_list():
    if not models.SETUP_SCRIPT.is_file():
        pytest.skip("setup script not present")
    lineup = models.setup_lineup(models.SETUP_SCRIPT)
    assert {"freebuff", "github-copilot", "google", "groq", "openai-codex", "anthropic"} <= set(lineup)
    assert sum(len(c.get("models") or []) for c in lineup.values()) >= 15


class FakeToggles:
    def __init__(self, off=()):
        self.off = set(off)
        self.calls = []

    def is_enabled(self, rid):
        return rid not in self.off

    def set_enabled(self, rid, on):
        self.calls.append((rid, on))
        (self.off.discard if on else self.off.add)(rid)


def test_models_enabled_flag_goes_through_toggles(src):
    tg = FakeToggles(off={"chat:google/gemini-3.6-flash"})
    src.toggles = tg
    rows = rows_by_id(src)
    assert rows["chat:google/gemini-3.6-flash"]["enabled"] is False
    assert rows["chat:google/gemini-3.8-flash"]["enabled"] is True
    models.set_enabled(["chat:freebuff/openai/gpt-6-luna"], False, sources=src)
    assert ("chat:freebuff/openai/gpt-6-luna", False) in tg.calls
    assert rows_by_id(src)["chat:freebuff/openai/gpt-6-luna"]["enabled"] is False
    # the local file still carries the refusal lists dispatch reads
    assert models.is_disabled(route="freebuff", model="openai/gpt-6-luna", path=src.state) is True


def test_models_local_disabled_ids_migrate_into_toggles_once(src):
    models.set_enabled(["chat:freebuff/openai/gpt-6-luna"], False, sources=src)  # no toggles yet
    tg = FakeToggles()
    src.toggles = tg
    assert rows_by_id(src)["chat:freebuff/openai/gpt-6-luna"]["enabled"] is False
    assert tg.calls == [("chat:freebuff/openai/gpt-6-luna", False)]
    tg.set_enabled("chat:freebuff/openai/gpt-6-luna", True)  # Owner turns it back on elsewhere
    assert rows_by_id(src)["chat:freebuff/openai/gpt-6-luna"]["enabled"] is True  # not re-migrated
    assert len(tg.calls) == 2


def test_models_strongest_skips_switched_off_models(src):
    src.toggles = FakeToggles(off={"chat:google/gemini-3.8-flash"})
    rows = rows_by_id(src)
    assert rows["chat:google/gemini-3.6-flash"]["strongest"] is True
    assert rows["chat:google/gemini-3.8-flash"]["strongest"] is False


def test_models_broken_toggles_fall_back_to_local_file(src):
    class Broken:
        def is_enabled(self, rid):
            raise RuntimeError("toggles down")

        def set_enabled(self, rid, on):
            raise RuntimeError("toggles down")
    models.set_enabled(["chat:freebuff/openai/gpt-6-luna"], False, sources=src)
    src.toggles = Broken()
    assert rows_by_id(src)["chat:freebuff/openai/gpt-6-luna"]["enabled"] is False
