"""G1 done-check: ``cc_engine.toggles`` is the one switch per agent, route and model.

Run: ``python -m pytest engine/tests -k toggles_source -q``. tmp_path databases only.
"""
import json
import sqlite3
import sys

import pytest

from cc_engine import toggles as T
from cc_engine.store import Store


class App:
    def __init__(self, store):
        self.store = store
        self.notes = []
        store.subscribe(lambda kind, payload: self.notes.append((kind, payload)))


class H:
    def __init__(self, app, body=None, actor="owner"):
        self.app, self.body, self.actor = app, body or {}, actor

    def _json_body(self):
        return dict(self.body)


@pytest.fixture
def db(tmp_path):
    s = Store(tmp_path / "cc.sqlite")
    yield s
    s.close()


def test_toggles_source_defaults_codex_off_everything_else_on(db):
    for rid in ("claude_code", "chatgpt_web", "freebuff", "gemini_app", "gemini_studio", "groq",
                "antigravity_manual", "manual", "subagent:claude/opus-5.5", "manual:chatgpt_web"):
        assert T.is_enabled(rid, db=db) is True, rid
    assert T.is_enabled("codex", db=db) is False
    assert T.is_enabled("agent:codex", db=db) is False  # one switch, several names


def test_toggles_source_set_persists_in_its_own_table(db, tmp_path):
    row = T.set_enabled("freebuff", False, actor="owner", db=db)
    assert row["id"] == "freebuff" and row["enabled"] is False and row["updated_by"] == "owner"
    assert T.is_enabled("freebuff", db=db) is False
    assert T.is_enabled("freebuff", db=tmp_path / "cc.sqlite") is False  # a path works as well as a Store
    con = sqlite3.connect(str(tmp_path / "cc.sqlite"))
    rows = con.execute("SELECT id, enabled FROM toggles").fetchall()
    con.close()
    assert ("freebuff", 0) in rows
    T.set_enabled("codex", True, actor="owner", db=db)
    assert T.is_enabled("agent:codex", db=db) is True
    T.set_enabled("agent:codex", False, actor="owner", db=db)  # an alias writes the same switch
    assert T.is_enabled("codex", db=db) is False


def test_toggles_source_alias_forms_share_one_switch(db):
    T.set_enabled("groq", False, actor="owner", db=db)
    T.set_enabled("manual:gemini_app", False, actor="owner", db=db)
    for rid in ("groq", "route:groq", "runner:groq", "agent:groq"):
        assert T.is_enabled(rid, db=db) is False, rid
    for rid in ("gemini_app", "route:gemini_app", "manual:gemini_app"):
        assert T.is_enabled(rid, db=db) is False, rid
    for rid in ("gemini_studio", "route:gemini", "runner:gemini", "route:gemini_studio"):
        assert T.canonical(rid) == "gemini_studio", rid
    assert T.canonical("runner:antigravity") == "antigravity_manual"
    assert T.canonical("agent:antigravity") == "antigravity_manual"
    rows = T.ROUTES["GET /api/toggles"](H(App(db)), {})["toggles"]
    assert all({"id", "kind", "label", "enabled"} <= set(r) for r in rows)


def test_toggles_source_route_off_greys_its_models(db):
    T.set_enabled("claude_code", False, actor="owner", db=db)
    assert T.is_enabled("subagent:claude/opus-5.5", db=db) is False
    assert T.is_enabled("agent:claude_code", db=db) is False
    assert T.is_enabled("subagent:antigravity/gemini-3.8-flash-high", db=db) is True
    assert T.refusal("claude_code", "opus", db=db)
    assert T.refusal("claude", None, db=db)  # pool names resolve to their route


def test_toggles_source_model_off_matches_aliases(db, monkeypatch):
    monkeypatch.setitem(sys.modules, "cc_engine.models", None)  # works without the registry
    T.set_enabled("subagent:claude/sonnet-5.5", False, actor="owner", db=db)
    assert T.refusal("claude_code", "sonnet-5.5", db=db)
    assert T.refusal("claude_code", "subagent:claude/sonnet-5.5", db=db)
    assert T.refusal("claude_code", "opus-5.5", db=db) is None
    assert T.refusal("freebuff", "sonnet-5.5", db=db) is None  # another route's model


def test_toggles_source_missing_db_reads_defaults_without_creating_it(tmp_path):
    path = tmp_path / "nowhere" / "cc.sqlite"
    assert T.is_enabled("claude_code", db=path) is True
    assert T.is_enabled("codex", db=path) is False
    assert not path.exists()


def test_toggles_source_migrates_models_state_once(tmp_path):
    (tmp_path / "models_disabled.json").write_text(json.dumps(
        {"schema": "models_disabled.v1", "disabled": ["agent:freebuff", "subagent:claude/haiku-5.5"]}),
        encoding="utf-8")
    s = Store(tmp_path / "cc.sqlite")
    try:
        assert T.is_enabled("freebuff", db=s) is False
        assert T.is_enabled("subagent:claude/haiku-5.5", db=s) is False
        T.set_enabled("freebuff", True, actor="owner", db=s)
        (tmp_path / "models_disabled.json").write_text(json.dumps({"disabled": ["agent:groq"]}),
                                                       encoding="utf-8")
        assert T.is_enabled("freebuff", db=s) is True     # migration ran once; the table wins
        assert T.is_enabled("groq", db=s) is True
    finally:
        s.close()


def test_toggles_source_list_has_routes_agents_and_registry_rows(db):
    registry = [{"id": "subagent:claude/opus-5.5", "label": "Claude Opus 5.5", "kind": "subagent",
                 "route": "claude_code", "runner": "claude"},
                {"id": "chat:groq/llama", "label": "Llama", "kind": "chat", "route": None}]
    T.set_enabled("subagent:claude/opus-5.5", False, actor="owner", db=db)
    rows = {r["id"]: r for r in T.list(db=db, registry=registry)}
    for rid in T.ROUTE_IDS:
        assert rid in rows and rows[rid]["kind"] in ("route", "agent")
    assert rows["codex"]["enabled"] is False and rows["codex"]["default"] is False
    assert rows["subagent:claude/opus-5.5"]["enabled"] is False
    assert rows["chat:groq/llama"]["enabled"] is True
    assert "agent:codex" in rows["codex"]["aliases"]


def test_toggles_source_validates_ids(db):
    for bad in ("", None, "x" * 300, "bad id with spaces"):
        with pytest.raises(ValueError):
            T.set_enabled(bad, False, actor="owner", db=db)
    with pytest.raises(ValueError):
        T.set_enabled("freebuff", "no", actor="owner", db=db)


def test_toggles_source_http_routes(db):
    from cc_engine.api import HttpError
    app = App(db)
    out = T.ROUTES["GET /api/toggles"](H(app), {})
    assert out["enabled"]["codex"] is False and out["enabled"]["claude_code"] is True
    assert out["aliases"]["agent:codex"] == "codex"
    with pytest.raises(HttpError) as err:
        T.ROUTES["POST /api/toggles/set"](H(app, {"id": "freebuff", "enabled": False}, actor="router"), {})
    assert err.value.status == 403
    assert T.is_enabled("freebuff", db=db) is True
    with pytest.raises(HttpError) as err:
        T.ROUTES["POST /api/toggles/set"](H(app, {"id": "freebuff", "enabled": "off"}), {})
    assert err.value.status == 400
    out = T.ROUTES["POST /api/toggles/set"](H(app, {"id": "freebuff", "enabled": False}), {})
    assert out["ok"] and out["toggle"]["enabled"] is False
    assert T.is_enabled("freebuff", db=db) is False
    assert any(kind == "toggles" for kind, _ in app.notes)  # SSE 'toggles' for the plugin
    assert any("Freebuff" in e["summary"] for e in db.events(limit=50))


def test_toggles_source_api_lists_the_module():
    from cc_engine import api
    assert "toggles" in api.EXTENSIONS
