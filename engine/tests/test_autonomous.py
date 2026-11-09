"""S2: the Autonomous tab is the one place runs start (cc_engine.sprints autonomous routes).

Covers saved run controls, priority rows honoured by the scheduler, a model per project, the
Codex / Claude / Dual / Auto Dual sprint adders (Codex only with a confirm, never dispatched),
Copy Prompt, disabled models refused by dispatch, and the Codex + Claude usage strip.
Fakes only: no provider calls, tmp_path databases.
"""
import sys
import types

import pytest

from cc_engine import dispatch as D
from cc_engine import runsheet
from cc_engine import scheduler as S
from cc_engine import sprints as SP

from test_scheduler import T0, add_run, make_env, seed, statuses, usage

FIVE_H = 5 * 3600


def rows(now=T0, claude_reset=3600, codex_reset=7200, claude=80, codex=60):
    return [
        {"pool": "claude_5h", "label": "Claude 5 hours", "remaining_percent": claude, "status": "FRESH",
         "resets_at": S.iso(now + claude_reset)},
        {"pool": "claude_week", "label": "Claude week", "remaining_percent": 90, "status": "FRESH",
         "resets_at": S.iso(now + 5 * 86400)},
        {"pool": "codex_5h", "label": "Codex 5 hours", "remaining_percent": codex, "status": "FRESH",
         "resets_at": S.iso(now + codex_reset)},
        {"pool": "codex_week", "label": "Codex week", "remaining_percent": 70, "status": "FRESH",
         "resets_at": S.iso(now + 6 * 86400)},
        {"pool": "antigravity_week", "label": "Antigravity week", "remaining_percent": 0, "status": "FRESH",
         "resets_at": S.iso(now + 4 * 86400)},
    ]


class App:
    def __init__(self, store, now=T0, usage_rows=None):
        self.store = store
        self.t = now
        self.rows = usage_rows if usage_rows is not None else rows(now)
        self.kicks = []

    def clock(self):
        return self.t

    def usage_reader(self):
        return [dict(r) for r in self.rows]

    def tick_kicker(self, store):
        self.kicks.append(store.path)
        return True


class H:
    def __init__(self, app, body=None, actor="owner"):
        self.app, self.body, self.actor = app, body or {}, actor

    def _json_body(self):
        return dict(self.body)


def call(app, route, body=None, q=None, actor="owner"):
    return SP.ROUTES[route](H(app, body, actor), q or {})


def http_status(fn):
    from cc_engine.api import HttpError
    with pytest.raises(HttpError) as err:
        fn()
    return err.value.status, err.value.body


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "cc_engine.models", None)  # registry absent unless a test adds it
    e = make_env(tmp_path, [("A", [], "not_started"), ("B", ["A"], "not_started")], project_id="p1")
    seed(e.store, [("A", [], "not_started")], "p2", cwd=str(tmp_path))
    seed(e.store, [("A", [], "not_started")], "p3", cwd=str(tmp_path))
    e.app = App(e.store)
    return e


def settings(**kw):
    base = {"preset": "balanced", "models": ["claude-sonnet-5.5", "claude-opus-5.5"],
            "efforts": {"claude": "high", "codex": "medium"}, "punch_clock": True,
            "max_use": {"claude_code": True}, "auto_wake": True, "power_off": False, "merge_resets": True,
            "sequence": [[{"project": "p1", "model": None}, {"project": "p2", "model": "claude-opus-5.5"}],
                         [{"project": "p3", "model": None}]]}
    base.update(kw)
    return base


def fake_registry(monkeypatch, disabled=()):
    mod = types.ModuleType("cc_engine.models")
    models = [
        {"id": "claude-opus-5.5", "label": "Claude Opus 5.5", "provider": "anthropic", "kind": "subagent",
         "route": "claude_code", "enabled": True},
        {"id": "claude-sonnet-5.5", "label": "Claude Sonnet 5.5", "provider": "anthropic", "kind": "subagent",
         "route": "claude_code", "enabled": "claude-sonnet-5.5" not in disabled},
        {"id": "gpt-6-sol", "label": "GPT 6 Sol (ChatGPT Web)", "provider": "openai", "kind": "manual",
         "route": "chatgpt_web", "enabled": True},
        {"id": "deepseek-chat", "label": "DeepSeek chat", "provider": "deepseek", "kind": "chat",
         "route": None, "enabled": True},
        {"id": "groq-llama", "label": "Groq LPU", "provider": "groq", "kind": "chat", "route": None,
         "enabled": False},
    ]
    mod.ROUTES = {"GET /api/models": lambda h, q: {"models": [dict(m) for m in models]}}
    mod.is_disabled = lambda route, model: model in disabled
    monkeypatch.setitem(sys.modules, "cc_engine.models", mod)
    return mod


# ---------------------------------------------------------------- routes and settings

def test_autonomous_routes_are_owner_only(env):
    for key in ("GET /api/autonomous", "POST /api/autonomous/settings", "POST /api/autonomous/run",
                "POST /api/autonomous/cancel", "POST /api/autonomous/sprint", "POST /api/autonomous/prompt"):
        assert key in SP.ROUTES
    for key in ("POST /api/autonomous/settings", "POST /api/autonomous/run", "POST /api/autonomous/sprint",
                "POST /api/autonomous/cancel"):
        status, body = http_status(lambda: call(env.app, key, {"settings": settings(), "kind": "claude"},
                                                actor="router"))
        assert status == 403 and body["error"] == "owner_only"
    assert env.store.list_runs() == []


def test_autonomous_settings_persist_in_the_engine(env):
    out = call(env.app, "POST /api/autonomous/settings", {"settings": settings()})
    assert out["ok"] and out["settings"]["sequence"][1] == [{"project": "p3", "model": None}]
    again = call(App(env.store), "GET /api/autonomous")  # a fresh app reads the same database
    s = again["settings"]
    assert s["models"] == ["claude-sonnet-5.5", "claude-opus-5.5"]
    assert s["efforts"] == {"claude": "high", "codex": "medium"}
    assert s["max_use"] == {"claude_code": True} and s["auto_wake"] is True and s["punch_clock"] is True
    assert [[i["project"] for i in row] for row in s["sequence"]] == [["p1", "p2"], ["p3"]]
    assert s["sequence"][0][1]["model"] == "claude-opus-5.5"


def test_autonomous_defaults_when_nothing_saved(env):
    s = call(env.app, "GET /api/autonomous")["settings"]
    assert s["sequence"] == [] and s["models"] == [] and s["power_off"] is False


@pytest.mark.parametrize("bad", [
    {"sequence": [[{"project": "nope"}]]},
    {"sequence": [[{"project": "p1"}], [{"project": "p1"}]]},
    {"efforts": {"claude": "ultra"}},
    {"models": ["gpt-6-codex"]},          # Codex is never a run model
    {"models": ["deepseek-chat"]},        # no run route
    {"preset": "yolo"},
    {"sequence": [[{"project": "p1", "model": "gpt-6-codex"}]]},
    {"max_use": {"codex": True}},
])
def test_autonomous_settings_reject_bad_values(env, bad):
    status, _ = http_status(lambda: call(env.app, "POST /api/autonomous/settings", {"settings": settings(**bad)}))
    assert status == 400


def test_autonomous_models_fall_back_to_current_pools_when_registry_absent(env):
    out = call(env.app, "GET /api/autonomous")
    assert out["models_source"] == "fallback"
    ids = {m["id"]: m for m in out["models"]}
    assert ids["claude-opus-5.5"]["route"] == "claude_code"
    assert any(m["route"] == "chatgpt_web" and m["kind"] == "manual" for m in out["models"])
    assert not any(m["route"] == "chatmode" for m in out["models"])
    assert any(m["route"] == "freebuff" for m in out["models"])
    assert any(m["route"] == "gemini_app" and m["kind"] == "manual" for m in out["models"])
    agy = [m for m in out["models"] if m["route"] == "antigravity_manual"]
    assert agy and all(m["kind"] == "manual" for m in agy)
    assert not any("codex" in str(m["route"] or "") for m in out["models"])
    assert all(m["enabled"] for m in out["models"])


def test_autonomous_models_use_the_registry_and_hide_disabled(env, monkeypatch):
    fake_registry(monkeypatch, disabled=("claude-sonnet-5.5",))
    out = call(env.app, "GET /api/autonomous")
    assert out["models_source"] == "registry"
    ids = [m["id"] for m in out["models"]]
    assert "claude-sonnet-5.5" not in ids and "groq-llama" not in ids and "claude-opus-5.5" in ids
    status, body = http_status(lambda: call(env.app, "POST /api/autonomous/settings",
                                            {"settings": settings(models=["claude-sonnet-5.5"])}))
    assert status == 400 and "turned off" in body["detail"]


def test_autonomous_usage_strip_shows_codex_and_claude_only(env):
    strip = call(env.app, "GET /api/autonomous")["usage"]
    assert [r["pool"] for r in strip] == ["claude_5h", "claude_week", "codex_5h", "codex_week"]
    assert strip[0]["remaining_percent"] == 80


# ---------------------------------------------------------------- Run

def test_autonomous_run_creates_one_run_per_project_in_priority_rows(env):
    out = call(env.app, "POST /api/autonomous/run", {"settings": settings(power_off=True)})
    assert out["ok"] and [r["project"] for r in out["runs"]] == ["p1", "p2", "p3"]
    runs = {r["project_id"]: r for r in env.store.list_runs(limit=100) if r["project_id"] in ("p1", "p2", "p3")}
    s1, s2, s3 = (runs[p]["settings"] for p in ("p1", "p2", "p3"))
    assert s1["planned_by"] == "autonomous" and s1["after_runs"] == [] and s2["after_runs"] == []
    assert sorted(s3["after_runs"]) == sorted([runs["p1"]["id"], runs["p2"]["id"]])
    assert s1["pools"] == ["claude_code"]
    assert s1["advanced"]["model_ladder"]["claude_code"] == ["sonnet", "opus"]
    assert s2["advanced"]["model_ladder"]["claude_code"][0] == "opus"  # the project's own model first
    assert s2["default_pool"] == "claude_code"
    assert s1["use_pool_fully"] == {"claude_code": True}
    assert s1["auto_resume"] is True and s1["power_off"] is True and s1["merge_resets"] is True
    assert s1["sprint_run"] == SP.ANCHOR_RUN
    exp = S.expand_settings(s1)
    assert exp["effort"] == {"claude_code": "high"}
    assert all(r["status"] == "queued" for r in runs.values())
    assert any("Owner started Autonomous" in e["summary"] for e in env.store.events(limit=1000))


def test_autonomous_run_refuses_a_second_start_and_projects_already_running(env):
    call(env.app, "POST /api/autonomous/run", {"settings": settings()})
    status, body = http_status(lambda: call(env.app, "POST /api/autonomous/run", {"settings": settings()}))
    assert status == 409


def test_autonomous_run_refuses_a_project_with_another_active_run(env):
    add_run(env.store, "p2", {"preset": "balanced", "pools": ["claude_code"]})
    status, body = http_status(lambda: call(env.app, "POST /api/autonomous/run", {"settings": settings()}))
    assert status == 409 and "p2" in body["detail"]


def test_autonomous_run_needs_a_sequence_and_a_model(env):
    status, _ = http_status(lambda: call(env.app, "POST /api/autonomous/run", {"settings": settings(sequence=[])}))
    assert status == 400
    status, _ = http_status(lambda: call(env.app, "POST /api/autonomous/run",
                                         {"settings": settings(models=[], sequence=[[{"project": "p1"}]])}))
    assert status == 400


def test_autonomous_cancel_stops_every_autonomous_run(env):
    call(env.app, "POST /api/autonomous/run", {"settings": settings()})
    out = call(env.app, "POST /api/autonomous/cancel", {})
    assert out["ok"] and len(out["cancelled"]) == 3
    assert all(r["status"] == "cancelled" for r in env.store.list_runs(limit=100) if r["project_id"] in ("p1", "p2", "p3"))


def test_autonomous_scheduler_row_waits_for_every_project_above(env):
    call(env.app, "POST /api/autonomous/run", {"settings": settings(punch_clock=False)})
    ok = usage()
    res = S.tick(env.store, T0, env.dispatcher, usage_reader=ok)
    assert sorted(res["started"]) == ["A", "A"]  # p1/A and p2/A together; p3 waits
    assert statuses(env.store, "p3") == {"A": "not_started"}
    assert any(b.get("reason") == "waiting for the projects above it" for b in res["blocked"])
    by_project = {r["project_id"]: r for r in env.store.list_runs(limit=100)}
    assert by_project["p3"]["status"] == "queued"
    # p2 finishes; p1 still has B to do, so p3 keeps waiting.
    env.store.set_node_status("p2", "A", "done", "owner")
    S.tick(env.store, T0 + 60, env.dispatcher, usage_reader=ok)
    assert statuses(env.store, "p3") == {"A": "not_started"}
    for nid in ("A", "B"):
        env.store.set_node_status("p1", nid, "done", "owner")
    res = S.tick(env.store, T0 + 120, env.dispatcher, usage_reader=ok)  # closes p1's run, then p3 starts
    assert statuses(env.store, "p3") == {"A": "running"}
    assert res["started"] == ["A"]
    waits = [e for e in env.store.events(limit=1000) if "waits for the projects above it" in (e["summary"] or "")]
    assert len(waits) == 1  # one sentence, not one per tick


def test_autonomous_project_model_is_the_first_rung(env):
    env.store.update_node("p2", "A", {"model": None, "effort": None}, "owner", "unpin")
    call(env.app, "POST /api/autonomous/run", {"settings": settings(punch_clock=False)})
    S.tick(env.store, T0, env.dispatcher, usage_reader=usage())
    p2a = env.store.get_node("p2", "A")
    assert p2a["model"] == "opus" and p2a["effort"] == "high"  # project model, Claude effort slider
    launched = {sp["project"]: sp for sp in env.launched}
    assert launched["p2"]["argv"][launched["p2"]["argv"].index("--model") + 1] == "opus"


# ---------------------------------------------------------------- sprint adders

def test_autonomous_claude_sprint_takes_the_next_free_slot(env):
    a = call(env.app, "POST /api/autonomous/sprint", {"kind": "claude"})["sprints"]
    b = call(env.app, "POST /api/autonomous/sprint", {"kind": "claude"})["sprints"]
    c = call(env.app, "POST /api/autonomous/sprint", {"kind": "claude"})["sprints"]
    assert [s["pool"] for s in a + b + c] == ["claude_code"] * 3
    assert S.to_epoch(a[0]["start_at"]) == T0                       # slot 0: now, until the reset
    assert S.to_epoch(a[0]["end_at"]) == T0 + 3600
    assert S.to_epoch(b[0]["start_at"]) == T0 + 3600 + 60           # slot 1: reset + 1 min
    assert S.to_epoch(c[0]["start_at"]) == T0 + 3600 + FIVE_H + 60  # slot 2: the reset after
    assert all(s["run_id"] == SP.ANCHOR_RUN for s in env.store.list_sprints(SP.ANCHOR_RUN))
    assert env.store.get_run(SP.ANCHOR_RUN)["status"] == "manual"   # never a scheduler run
    assert S.active_runs(env.store) == []


def enable_codex(store):
    from cc_engine import toggles
    toggles.set_enabled("codex", True, actor="owner", db=store)


def test_autonomous_codex_sprint_needs_a_confirm(env):
    status, body = http_status(lambda: call(env.app, "POST /api/autonomous/sprint", {"kind": "codex", "confirm": True}))
    assert status == 403 and body["error"] == "disabled"  # Codex is off until Owner switches it on
    enable_codex(env.store)
    status, body = http_status(lambda: call(env.app, "POST /api/autonomous/sprint", {"kind": "codex"}))
    assert status == 409 and body["error"] == "confirm_codex"
    assert [r["pool"] for r in body["usage"]] == ["codex_5h", "codex_week"]
    for kind in ("dual", "auto_dual"):
        status, _ = http_status(lambda: call(env.app, "POST /api/autonomous/sprint", {"kind": kind}))
        assert status == 409
    assert env.store.list_sprints() == []
    out = call(env.app, "POST /api/autonomous/sprint", {"kind": "codex", "confirm": True})
    assert [s["pool"] for s in out["sprints"]] == ["codex"]
    assert any("Owner confirmed a Codex sprint" in e["summary"] for e in env.store.events(limit=1000))


def test_autonomous_dual_and_auto_dual(env):
    enable_codex(env.store)
    dual = call(env.app, "POST /api/autonomous/sprint", {"kind": "dual", "confirm": True})["sprints"]
    assert sorted(s["pool"] for s in dual) == ["claude_code", "codex"]
    assert len({s["start_at"] for s in dual}) == 1  # both start together
    auto = call(env.app, "POST /api/autonomous/sprint", {"kind": "auto_dual", "confirm": True})["sprints"]
    assert sorted(s["pool"] for s in auto) == ["claude_code", "codex"]
    first, second = sorted(auto, key=lambda s: s["start_at"])
    assert second["handoff_after"] == first["id"] and first["handoff_after"] is None
    assert S.to_epoch(first["start_at"]) == T0 + 3600 + 60   # Claude's next reset comes first
    assert S.to_epoch(second["start_at"]) == T0 + 7200 + 60


def test_autonomous_codex_sprint_runs_only_ticked_codex_work(env):
    # Codex dispatch itself is covered by test_codex_route.py; an unticked Codex never runs.
    enable_codex(env.store)
    call(env.app, "POST /api/autonomous/sprint", {"kind": "codex", "confirm": True})
    call(env.app, "POST /api/autonomous/run", {"settings": settings()})
    S.tick(env.store, T0, env.dispatcher, usage_reader=usage())
    assert env.launched and all(sp["route"] == "claude_code" for sp in env.launched)
    tl = call(env.app, "GET /api/sprints/timeline", q={"hours": "24"})
    codex = [s for s in tl["sprints"] if s["pool"] == "codex"]
    assert codex and codex[0]["state"] == "due"
    assert all("codex" not in str(sp["argv"]).lower() for sp in env.launched)


def test_autonomous_anchor_sprint_gates_the_claude_pool(env):
    call(env.app, "POST /api/autonomous/sprint", {"kind": "claude"})                  # now
    later = call(env.app, "POST /api/autonomous/sprint", {"kind": "claude"})["sprints"][0]  # after reset
    call(env.app, "POST /api/autonomous/run", {"settings": settings()})
    res = S.tick(env.store, T0, env.dispatcher, usage_reader=usage())
    assert sorted(res["started"]) == ["A", "A"]  # the current sprint is open although a later one waits
    first = [s for s in env.store.list_sprints(SP.ANCHOR_RUN) if s["id"] != later["id"]][0]
    assert first["status"] == "active"
    tl = call(env.app, "GET /api/sprints/timeline", q={"hours": "24"})
    shown = {s["id"]: s for s in tl["sprints"]}
    assert shown[later["id"]]["editable"] and shown[later["id"]]["state"] == "planned"
    assert shown[first["id"]]["state"] == "active"


def test_autonomous_used_up_pool_resumes_once_on_the_shared_clock(env):
    call(env.app, "POST /api/autonomous/sprint", {"kind": "claude"})
    call(env.app, "POST /api/autonomous/run", {"settings": settings()})
    low = usage(remaining=0, resets=T0 + 600)
    for t in (T0, T0 + 10, T0 + 20):
        S.tick(env.store, t, env.dispatcher, usage_reader=low)
    resumes = [s for s in env.store.list_sprints(SP.ANCHOR_RUN) if S.to_epoch(s["start_at"]) == T0 + 660]
    assert len(resumes) == 1


def test_autonomous_sprint_drag_moves_an_anchor_sprint(env):
    sid = call(env.app, "POST /api/autonomous/sprint", {"kind": "claude"})["sprints"][0]["id"]
    out = call(env.app, "POST /api/sprints/update", {"id": sid, "start_at": S.iso(T0 + 1800)})
    assert out["start_at"] == S.iso(T0 + 1800)
    assert S.to_epoch(env.store.get_sprint(sid)["end_at"]) == T0 + 1800 + 3600  # slot 0 ran until the reset


# ---------------------------------------------------------------- Copy Prompt and dispatch guard

def test_autonomous_copy_prompt(env):
    call(env.app, "POST /api/autonomous/sprint", {"kind": "claude"})
    out = call(env.app, "POST /api/autonomous/prompt", {"settings": settings()})
    text = out["prompt"]
    assert "Row 1" in text and "Row 2" in text and "Proj" in text
    assert "claude-opus-5.5" in text and "high" in text
    assert "Codex" in text and "never" in text.lower()
    assert env.store.list_runs() == [] or all(r["id"] == SP.ANCHOR_RUN for r in env.store.list_runs())


def test_autonomous_dispatch_refuses_disabled_models(env, monkeypatch):
    node = S.get_node(env.store, "p1", "A")
    assert env.dispatcher.refusal(node) is None  # no registry: nothing is disabled
    fake_registry(monkeypatch, disabled=("sonnet",))
    assert "turned off" in env.dispatcher.refusal(node)
    out = env.dispatcher.dispatch(node, {}, {"id": "p1"})
    assert out["status"] == "refused" and env.launched == []


def test_autonomous_scheduler_skips_a_disabled_model_for_the_next_rung(env, monkeypatch):
    fake_registry(monkeypatch, disabled=("sonnet",))
    env.store.update_node("p1", "A", {"model": None}, "owner", "unpin")
    add_run(env.store, "p1", {"preset": "balanced", "pools": ["claude_code"]})
    res = S.tick(env.store, T0, env.dispatcher, usage_reader=usage())
    assert res["started"] == ["A"]
    assert env.store.get_node("p1", "A")["model"] == "opus"
    fake_registry(monkeypatch, disabled=("sonnet", "opus"))
    env.store.update_node("p2", "A", {"model": None}, "owner", "unpin")
    add_run(env.store, "p2", {"preset": "balanced", "pools": ["claude_code"]})
    res = S.tick(env.store, T0 + 1, env.dispatcher, usage_reader=usage())
    assert statuses(env.store, "p2")["A"] == "not_started"
    assert any("turned off" in (b.get("reason") or "") for b in res["blocked"])


def test_autonomous_runsheet_accepts_per_pool_efforts():
    out = runsheet.expand({"preset": "balanced", "pools": ["claude_code", "freebuff"],
                           "efforts": {"claude_code": "max"}})
    assert out["effort"] == {"claude_code": "max", "freebuff": "medium"}
    with pytest.raises(ValueError):
        runsheet.expand({"preset": "balanced", "pools": ["claude_code"], "efforts": {"claude_code": "ultra"}})
    with pytest.raises(ValueError):
        runsheet.expand({"preset": "balanced", "pools": ["claude_code"], "efforts": {"groq": "high"}})
    # Without efforts the effort stays one word, as before.
    assert runsheet.expand({"preset": "balanced", "pools": ["claude_code"]})["effort"] == "medium"


def test_autonomous_reads_live_registry_ids(env, monkeypatch):
    """Registry ids as worker A's GET /api/models serves them (4 Nov): kind:provider/model."""
    mod = types.ModuleType("cc_engine.models")
    live = [
        {"id": "subagent:claude/opus-5.5", "label": "Claude Opus 5.5", "provider": "anthropic", "kind": "subagent",
         "route": "claude_code", "enabled": True},
        {"id": "agent:claude_code", "label": "Claude Code", "provider": "anthropic", "kind": "agent",
         "route": "claude_code", "enabled": True},
        {"id": "manual:chatgpt_web", "label": "ChatGPT Web", "provider": "openai",
         "kind": "manual", "route": "chatgpt_web", "enabled": True},
        {"id": "chat:freebuff/deepseek/deepseek-v4-flash", "label": "DeepSeek V4 Flash", "provider": "freebuff",
         "kind": "chat", "route": "freebuff", "enabled": True},
        {"id": "subagent:antigravity/gemini-3.8-flash-high", "label": "AG Flash 3.8", "provider": "antigravity",
         "kind": "subagent", "route": "antigravity", "enabled": True},
        {"id": "chat:openai-codex/gpt-6.1-sol", "label": "GPT 6.1 Sol (Codex OAuth)", "provider": "openai-codex",
         "kind": "chat", "route": None, "enabled": True},
        {"id": "chat:github-copilot/gpt-5.3-codex", "label": "GPT-5.3 Codex (GitHub Student)", "provider": "github-copilot",
         "kind": "chat", "route": None, "enabled": True},
    ]
    mod.ROUTES = {"GET /api/models": lambda h, q: {"models": [dict(m) for m in live]}}
    mod.is_disabled = lambda route, model: False
    monkeypatch.setitem(sys.modules, "cc_engine.models", mod)
    out = call(env.app, "GET /api/autonomous")
    by = {m["id"]: m for m in out["models"]}
    agy = by["subagent:antigravity/gemini-3.8-flash-high"]
    assert agy["route"] == "antigravity_manual" and agy["kind"] == "manual" and agy["runnable"]
    assert by["manual:chatgpt_web"]["runnable"] and by["manual:chatgpt_web"]["note"] == "manual: Copy Prompt"
    assert by["chat:openai-codex/gpt-6.1-sol"]["note"].startswith("Codex")
    assert by["chat:github-copilot/gpt-5.3-codex"]["note"] == "chat only"
    cfg = settings(models=["subagent:claude/opus-5.5", "agent:claude_code",
                           "chat:freebuff/deepseek/deepseek-v4-flash", "subagent:antigravity/gemini-3.8-flash-high"],
                   sequence=[[{"project": "p1", "model": "subagent:antigravity/gemini-3.8-flash-high"}]])
    call(env.app, "POST /api/autonomous/run", {"settings": cfg})
    run = [r for r in env.store.list_runs(limit=100) if r["project_id"] == "p1"][0]["settings"]
    lad = run["advanced"]["model_ladder"]
    assert run["pools"] == ["claude_code", "freebuff", "antigravity_manual"]
    assert lad["claude_code"] == ["opus"]
    assert lad["freebuff"] == ["deepseek/deepseek-v4-flash"]
    assert lad["antigravity_manual"] == ["gemini-3.8-flash-high"]
    assert run["default_pool"] == "antigravity_manual"
