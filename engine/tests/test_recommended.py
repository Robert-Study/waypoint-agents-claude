"""C4: the planner decides a recommended model per task; the plan schema, import, store and the
board/node payloads keep it. The live database (which holds data) migrates safely."""
import json
import sqlite3

import pytest

from cc_engine import api, plan, planner
from cc_engine.store import Store

from test_planner import App, GOOD_PLAN, QUESTIONS  # noqa: F401  (fixtures/helpers reused)


def _with_recommended(p):
    p = json.loads(json.dumps(p))
    p["nodes"][0]["recommended"] = "claude-opus"
    p["nodes"][1]["recommended"] = "freebuff"
    return p


def test_recommended_survives_import_export_and_board(store, fixture_plan):
    fixture_plan["nodes"][0]["recommended"] = "claude-sonnet"
    plan.import_plan(store, fixture_plan)
    assert store.get_node("fx", "A")["recommended"] == "claude-sonnet"
    assert store.get_node("fx", "B")["recommended"] is None
    out = plan.export_plan(store, "fx")
    assert out["nodes"][0]["recommended"] == "claude-sonnet" and "recommended" not in out["nodes"][1]
    assert "recommended" in api.BOARD_FIELDS
    by = {n["id"]: n for n in api.decode_nodes(api.build_board(store, "fx"))}
    assert by["A"]["recommended"] == "claude-sonnet" and by["B"]["recommended"] is None
    # a re-import without the field keeps it; one with a new value updates it
    del fixture_plan["nodes"][0]["recommended"]
    plan.import_plan(store, fixture_plan)
    assert store.get_node("fx", "A")["recommended"] == "claude-sonnet"
    fixture_plan["nodes"][0]["recommended"] = "freebuff"
    plan.import_plan(store, fixture_plan)
    assert store.get_node("fx", "A")["recommended"] == "freebuff"


def test_recommended_must_be_a_short_string(store, fixture_plan):
    fixture_plan["nodes"][0]["recommended"] = {"model": "x"}
    with pytest.raises(plan.PlanError):
        plan.import_plan(store, fixture_plan)
    fixture_plan["nodes"][0]["recommended"] = "x" * 200
    with pytest.raises(plan.PlanError):
        plan.import_plan(store, fixture_plan)


def test_recommended_store_column_migrates_live_data(tmp_path):
    """An existing database without the column keeps its rows and revision after migration."""
    db = tmp_path / "old.sqlite"
    c = sqlite3.connect(db)
    c.executescript("""
    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
    INSERT INTO meta VALUES('revision', '41');
    CREATE TABLE projects(id TEXT PRIMARY KEY, title TEXT, brief TEXT, goal_hash TEXT, cwd TEXT,
      created_at TEXT, archived INT DEFAULT 0, status TEXT DEFAULT 'active');
    CREATE TABLE nodes(project_id TEXT NOT NULL, id TEXT NOT NULL, title TEXT, goal TEXT, goal_hash TEXT,
      check_json TEXT, check_hash TEXT, deps_json TEXT, owner TEXT, lane TEXT, pool TEXT, model TEXT, effort TEXT,
      status TEXT DEFAULT 'not_started', attempts INT DEFAULT 0, actor TEXT, evidence TEXT,
      updated_at TEXT, PRIMARY KEY(project_id, id));
    INSERT INTO projects VALUES('live', 'Live', 'brief', 'h', NULL, '2026-11-01', 0, 'active');
    INSERT INTO nodes(project_id, id, title, goal, goal_hash, check_json, check_hash, deps_json, status, attempts)
      VALUES('live', 'N1', 'Old task', 'g', 'gh', '{}', 'ch', '[]', 'done', 3);
    """)
    c.commit()
    c.close()
    s = Store(db)
    try:
        assert s.revision() == 41
        n = s.get_node("live", "N1")
        assert n["status"] == "done" and n["attempts"] == 3 and n["recommended"] is None
        s.update_node("live", "N1", {"recommended": "claude-haiku"}, "owner", "Owner set a model.")
        assert s.get_node("live", "N1")["recommended"] == "claude-haiku"
        assert s._q("SELECT name FROM sqlite_master WHERE name='claims'")  # claims table added too
    finally:
        s.close()
    Store(db).close()  # opening again is a no-op


@pytest.fixture
def sync(monkeypatch):
    from test_planner import FakeRunner
    monkeypatch.setattr(planner, "SPAWN", lambda fn: fn())

    def use(*replies, **kw):
        runner = FakeRunner(*replies, **kw)
        monkeypatch.setattr(planner, "RUNNER", runner)
        return runner
    return use


def test_recommended_planner_prompt_asks_and_draft_keeps_it(store, sync, tmp_path, monkeypatch):
    monkeypatch.setattr(planner, "_models_module", lambda: None)  # the fallback list, deterministically
    runner = sync({"questions": []}, _with_recommended(GOOD_PLAN))
    sess = planner.start(store, "Report", "A report generator", str(tmp_path))
    planner.draft(store, sess["id"])
    prompt = runner.calls[1]["prompt"]
    assert '"recommended"' in prompt
    ids = [m["id"] for m in planner.recommend_options()]
    assert ids and all(i in prompt for i in ids)
    assert not any("codex" in i.lower() for i in ids)  # Codex is never a planned route
    view = planner.session_view(store, sess["id"])
    pid = view["project"]
    assert store.get_node(pid, "A1")["recommended"] == "claude-opus"
    assert store.get_node(pid, "A2")["recommended"] == "freebuff"
    assert store.get_node(pid, "A3")["recommended"] is None
    assert view["draft"]["nodes"][0]["recommended"] == "claude-opus"


def test_recommended_planner_drops_unknown_or_codex_ids(store, sync, tmp_path, monkeypatch):
    monkeypatch.setattr(planner, "_models_module", lambda: None)
    p = _with_recommended(GOOD_PLAN)
    p["nodes"][0]["recommended"] = "codex-gpt"
    p["nodes"][1]["recommended"] = "made-up-model"
    sync({"questions": []}, p)
    sess = planner.start(store, "Report", "A report generator", str(tmp_path))
    planner.draft(store, sess["id"])
    pid = planner.session_view(store, sess["id"])["project"]
    assert store.get_node(pid, "A1")["recommended"] is None
    assert store.get_node(pid, "A2")["recommended"] is None


def test_recommended_options_follow_the_models_catalogue(monkeypatch):
    import types
    fake = types.SimpleNamespace(list_models=lambda: {"models": [
        {"id": "claude-x", "label": "Claude X", "kind": "subagent", "route": "claude_code", "enabled": True},
        {"id": "off", "label": "Off", "kind": "subagent", "route": "freebuff", "enabled": False},
        {"id": "codex-5", "label": "Codex", "kind": "agent", "route": None, "enabled": True, "provider": "openai"},
        {"id": "gemini-app", "label": "Gemini App", "kind": "manual", "route": "gemini_app", "enabled": True},
        {"id": "chat:anthropic/x", "label": "Metered chat", "kind": "chat", "route": None, "enabled": True},
        {"id": "manual:manual", "label": "Manual", "kind": "manual", "route": "manual", "enabled": True}]})
    monkeypatch.setattr(planner, "_models_module", lambda: fake)
    assert [m["id"] for m in planner.recommend_options()] == ["claude-x", "gemini-app"]


def test_recommended_options_from_the_real_catalogue_are_task_routes():
    """Whatever the live catalogue holds, offered ids are enabled task routes and never Codex."""
    opts = planner.recommend_options()
    assert opts and len({m["id"] for m in opts}) == len(opts)
    assert not any("codex" in m["id"].lower() or m["id"].startswith("chat:") for m in opts)
