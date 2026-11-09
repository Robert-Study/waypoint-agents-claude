"""F1 Pick for me: one deterministic choice of model + effort from Elo, usage, failures, health, task type."""
from types import SimpleNamespace

import pytest

from cc_engine import models, pick
from cc_engine.api import HttpError


def row(rid, kind="chat", elo=None, roles=("agent",), route=None, provider="freebuff", enabled=True, access=True,
        health=None, usage=None, in_picker=True, model=None, runner=None):
    return {"id": rid, "label": rid.split("/")[-1], "kind": kind, "elo": elo, "roles": list(roles), "route": route,
            "provider": provider, "enabled": enabled, "access": access, "health": health, "usage": usage,
            "in_picker": in_picker, "model": model or rid.split("/", 1)[-1], "runner": runner,
            "dsh": {"provider": provider, "model": model or rid.split("/", 1)[-1]} if kind == "chat" else None}


FAILED = {"ok": False, "reason": "HTTP 500", "checked_at": "2026-11-04T15:00:00+00:00", "stale": False}
STALE_FAIL = dict(FAILED, stale=True)
CLAUDE_OK = {"remaining_percent": 62.0, "status": "FRESH", "pool": "claude_5h"}
CLAUDE_LOW = {"remaining_percent": 8.0, "status": "FRESH", "pool": "claude_5h"}

CHAT = [
    row("chat:freebuff/deepseek", elo=1350, route="freebuff"),
    row("chat:google/gemini-3.8-flash", elo=1420, provider="google"),
    row("chat:groq/qwen", elo=1300, provider="groq"),
    row("chat:github-copilot/kimi-k3", elo=1500, provider="github-copilot", health=FAILED),
    row("chat:groq/off", elo=1600, provider="groq", enabled=False),
    row("chat:google/noaccess", elo=1600, provider="google", access=False),
    row("chat:anthropic/claude-opus-5-5", elo=1507, provider="anthropic", roles=()),
    row("chat:openai-codex/gpt-6.1-sol", elo=1550, provider="openai-codex", roles=()),
    row("chat:freebuff/hidden", elo=1700, in_picker=False),
]
TASK = [
    row("agent:freebuff", kind="agent", elo=None, route="freebuff", roles=(), provider="freebuff"),
    row("subagent:claude/opus-5.5", kind="subagent", elo=1507, route="claude_code", roles=("subagent",),
        provider="anthropic", usage=CLAUDE_OK, model="opus-5.5", runner="claude"),
    row("chat:freebuff/deepseek", elo=1350, route="freebuff", roles=("agent", "subagent"), model="deepseek/deepseek-v4-flash"),
    row("chat:groq/gpt-oss-120b", elo=1450, route="groq", roles=("agent", "subagent"), provider="groq"),
    row("manual:manual", kind="manual", route="manual", roles=(), provider="manual"),
    row("agent:codex", kind="agent", elo=1550, route=None, roles=(), provider="openai-codex"),
]


def test_pick_for_me_chat_takes_the_strongest_working_orchestration_model():
    out = pick.choose("chat", CHAT)
    assert out["model"] == "chat:google/gemini-3.8-flash"
    assert out["effort"] in pick.EFFORTS
    assert out["why"] and "\n" not in out["why"] and len(out["why"]) <= 120
    assert out["dsh"] == {"provider": "google", "model": "gemini-3.8-flash"}


def test_pick_for_me_skips_failed_disabled_no_access_claude_codex_and_hidden():
    picked = {pick.choose("chat", [r])["model"] for r in CHAT}
    for bad in ("chat:github-copilot/kimi-k3", "chat:groq/off", "chat:google/noaccess",
                "chat:anthropic/claude-opus-5-5", "chat:openai-codex/gpt-6.1-sol", "chat:freebuff/hidden"):
        assert bad not in picked


def test_pick_for_me_stale_failure_does_not_exclude():
    r = row("chat:x/y", elo=1300, health=STALE_FAIL)
    assert pick.choose("chat", [r])["model"] == "chat:x/y"


def test_pick_for_me_is_deterministic_with_id_tiebreak():
    a = row("chat:b/m", elo=1400)
    b = row("chat:a/m", elo=1400)
    assert pick.choose("chat", [a, b]) == pick.choose("chat", [b, a])
    assert pick.choose("chat", [a, b])["model"] == "chat:a/m"


def test_pick_for_me_easy_task_protects_strong_usage():
    node = {"id": "D1", "title": "Fix typo in README", "goal": "Correct the docs wording.", "lane": "docs",
            "attempts": 0, "model": None}
    out = pick.choose("task", TASK, node=node)
    assert out["model"] != "subagent:claude/opus-5.5"
    assert out["effort"] == "low"
    assert out["route"] in ("freebuff", "groq")


def test_pick_for_me_hard_task_may_use_claude_when_usage_allows():
    node = {"id": "E1", "title": "Refactor scheduler concurrency", "goal": "Rework the engine lease and races.",
            "lane": "engine", "attempts": 1, "model": "deepseek/deepseek-v4-flash"}
    out = pick.choose("task", TASK, node=node)
    assert out["model"] == "subagent:claude/opus-5.5"
    assert out["effort"] in ("high", "max")
    low = [dict(r, usage=CLAUDE_LOW) if r["id"].startswith("subagent:claude") else r for r in TASK]
    assert pick.choose("task", low, node=node)["model"] != "subagent:claude/opus-5.5"


def test_pick_for_me_recent_failures_push_a_model_down():
    node = {"id": "N", "title": "Add a check", "goal": "Add a test.", "lane": "engine", "attempts": 0,
            "model": None}
    base = pick.choose("task", TASK, node=node)
    assert base["model"] == "chat:groq/gpt-oss-120b"
    out = pick.choose("task", TASK, node=node, failures={"chat:groq/gpt-oss-120b": 3})
    assert out["model"] != "chat:groq/gpt-oss-120b"


def test_pick_for_me_never_picks_codex_or_manual_for_tasks():
    for r in TASK:
        out = pick.choose("task", [r])
        assert out["model"] not in ("agent:codex", "manual:manual")


def test_pick_for_me_effort_by_kind():
    assert pick.choose("plan", CHAT)["effort"] == "high"
    assert pick.choose("chat", CHAT)["effort"] == "medium"


def test_pick_for_me_nothing_usable_says_so():
    out = pick.choose("chat", [row("chat:x/y", enabled=False)])
    assert out["model"] is None and out["effort"] is None and out["why"]


def test_pick_for_me_failures_from_node_attempts():
    node = {"id": "N", "attempts": 2, "model": "deepseek/deepseek-v4-flash"}
    f = pick.failures_for(node, TASK)
    assert f.get("chat:freebuff/deepseek") == 2


# ---------------------------------------------------------------- HTTP
CORDIS = """\
- id: llm-pi-ai
  config:
    providers:
      freebuff:
        apiKeyEnv: FREEBUFF_API_KEY
        models:
          - id: deepseek/deepseek-v4-flash
            name: DeepSeek V4.1 Flash
      anthropic:
        models:
          - id: claude-opus-5-5
            name: Claude Opus 5.5
"""


@pytest.fixture
def h(tmp_path):
    (tmp_path / "c.yml").write_text(CORDIS, encoding="utf-8")
    (tmp_path / "creds.yaml").write_text("version: 1\nrefs:\n  FREEBUFF_API_KEY: x\n", encoding="utf-8")
    src = models.Sources(cordis=tmp_path / "c.yml", credentials=tmp_path / "creds.yaml",
                         mcp_server=tmp_path / "none.py", state=tmp_path / "s.json", environ={},
                         registry_reader=lambda n: None)

    class Store:
        def get_node(self, project, nid):
            return {"id": nid, "title": "Write docs", "goal": "Docs.", "lane": "docs", "attempts": 0,
                    "model": None} if nid == "D1" else None

    return SimpleNamespace(app=SimpleNamespace(models_sources=src, usage_reader=lambda: [], store=Store()),
                           actor="owner")


def test_pick_for_me_route_contract(h):
    assert "GET /api/pick" in pick.ROUTES
    out = pick.ROUTES["GET /api/pick"](h, {"kind": "chat"})
    assert set(out) >= {"model", "effort", "why"}
    assert out["model"] == "chat:freebuff/deepseek/deepseek-v4-flash"
    out2 = pick.ROUTES["GET /api/pick"](h, {"kind": "task", "project": "p", "id": "D1"})
    assert out2["model"] and out2["effort"] == "low"
    with pytest.raises(HttpError) as e:
        pick.ROUTES["GET /api/pick"](h, {"kind": "bogus"})
    assert e.value.status == 400
