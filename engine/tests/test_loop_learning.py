"""W6 loop_learning: an accepted node yields a one-line constraint ACCEPTED / DERIVED / LANDS IN,
stored per project in its own table; later briefs (same and other projects) include relevant ones."""
import json
import re
import sqlite3

import pytest

from cc_engine import constraints, deep_plan, plan, planner
from cc_engine.api import HttpError
from cc_engine.store import OWNER

ROWS = {"id": "lr", "title": "Rows", "brief": "Parse CSV rows for the report generator.", "edges_v2": True, "nodes": [
    {"id": "A1", "title": "Row parser", "goal": "The CSV row parser turns exports into rows.json with typed columns.",
     "check": {"type": "command", "run": "python -m pytest engine/tests/test_rows.py -q"}, "deps": []},
    {"id": "A2", "title": "Report", "goal": "The report renders a table from rows.json.",
     "check": {"type": "files_exist", "paths": ["report/out.html"]},
     "deps": [{"id": "A1", "crosses": "rows.json"}]},
    {"id": "A3", "title": "Logo", "goal": "Owner approves the new logo colours.",
     "check": {"type": "human", "prompt": "Logo looks right"}, "deps": []}]}


def accept(store, pid, nid, evidence="exit 0: pytest\n12 passed in 0.4s"):
    store.set_node_status(pid, nid, "done", OWNER, evidence)


@pytest.fixture
def rows(store):
    plan.import_plan(store, ROWS)
    return store


def test_loop_learning_derive_is_one_deterministic_line(rows):
    accept(rows, "lr", "A1")
    node = rows.get_node("lr", "A1")
    a = constraints.derive(node, "lr")
    assert a == constraints.derive(node, "lr")
    assert re.match(r"^ACCEPTED: .+ / DERIVED: .+ / LANDS IN: .+$", a["line"]) and "\n" not in a["line"]
    assert "A1" in a["accepted"] and "row parser" in a["accepted"].lower()
    assert "python -m pytest engine/tests/test_rows.py -q" in a["derived"] and "12 passed" in a["derived"]
    assert "engine/tests/test_rows.py" in a["lands_in"]
    human = constraints.derive(dict(rows.get_node("lr", "A3"), status="done"), "lr")
    assert "Logo looks right" in human["derived"]


def test_loop_learning_records_accepted_nodes_once_in_own_table(rows):
    assert constraints.sync(rows, "lr") == []  # nothing accepted yet
    accept(rows, "lr", "A1")
    made = constraints.sync(rows, "lr")
    assert [c["node"] for c in made] == ["A1"]
    assert constraints.sync(rows, "lr") == []  # idempotent
    with sqlite3.connect(str(rows.path)) as c:
        got = c.execute("SELECT project_id, node_id, line FROM constraints").fetchall()
    assert len(got) == 1 and got[0][0] == "lr" and got[0][2].startswith("ACCEPTED:")
    listed = constraints.list_constraints(rows, "lr")
    assert listed[0]["node"] == "A1" and listed[0]["line"] == got[0][2]


def test_loop_learning_record_accepted_refuses_unfinished(rows):
    with pytest.raises(ValueError):
        constraints.record_accepted(rows, "lr", "A2")
    accept(rows, "lr", "A2", evidence="present: report/out.html")
    c = constraints.record_accepted(rows, "lr", "A2")
    assert "report/out.html" in c["lands_in"]


def test_loop_learning_optional_model_runner(rows):
    accept(rows, "lr", "A1")
    seen = []

    def runner(node, draft):
        seen.append(draft["derived"])
        return "keep rows.json columns typed; the report depends on it"

    c = constraints.record_accepted(rows, "lr", "A1", runner=runner)
    assert seen and c["derived"].startswith("keep rows.json columns typed") and c["source"] == "model"
    rows2 = constraints.derive(rows.get_node("lr", "A1"), "lr", runner=lambda n, d: 1 / 0)
    assert rows2["source"] == "template"  # a failing model never blocks the learning edge


def test_loop_learning_route(rows):
    accept(rows, "lr", "A1")

    class App:
        store = rows

    class H:
        app = App()

    out = constraints.ROUTES["GET /api/constraints"](H(), {"project": "lr"})
    assert out["project"] == "lr" and out["constraints"][0]["node"] == "A1"
    assert out["constraints"][0]["line"].startswith("ACCEPTED:")
    with pytest.raises(HttpError):
        constraints.ROUTES["GET /api/constraints"](H(), {"project": "nope"})
    assert constraints.ROUTES["GET /api/constraints"](H(), {})["constraints"]


def test_loop_learning_relevant_by_keyword_overlap(rows):
    accept(rows, "lr", "A1")
    accept(rows, "lr", "A3", evidence="Owner confirmed")
    hits = constraints.relevant(rows, "Extend the CSV row parser so rows.json gains a date column")
    assert [c["node"] for c in hits][:1] == ["A1"]
    assert all(c["node"] != "A3" for c in hits)
    assert constraints.relevant(rows, "Translate the French help page") == []


def test_loop_learning_later_node_brief_includes_upstream_constraint(rows):
    rows.set_meta(deep_plan.BRIEFS_PREFIX + "lr", json.dumps({
        "A2": {"text": "Task A2: Report\nGoal: The report renders a table from rows.json.", "files": ["report/x.py"]}}))
    before = deep_plan.task_brief(rows, "lr", "A2")
    assert "ACCEPTED" not in before["text"]
    accept(rows, "lr", "A1")
    after = deep_plan.task_brief(rows, "lr", "A2")
    assert "Rules learned" in after["text"] and "ACCEPTED: A1" in after["text"]
    assert after["rules"] and after["rules"][0].startswith("ACCEPTED: A1")


# ---------------------------------------------------------------- planner prompt fixtures
STAGE = re.compile(r"^Stage: (\w+)", re.M)


def test_loop_learning_deep_plan_prompt_contains_earlier_constraint(rows, monkeypatch):
    accept(rows, "lr", "A1")
    calls = []
    graph = {"title": "Rows v2", "brief": "Add a date column to the CSV row parser.", "nodes": [
        {"id": "B1", "title": "Date column", "goal": "The CSV row parser writes a typed date column to rows.json.",
         "check": {"type": "command", "run": "python -m pytest -q"}, "deps": [], "files": ["rows.py"]},
        {"id": "B2", "title": "Docs", "goal": "The docs page lists the new column.",
         "check": {"type": "files_exist", "paths": ["docs/rows.md"]}, "deps": [{"id": "B1", "crosses": "rows.json"}],
         "files": ["docs/rows.md"]}]}
    replies = {"brief": {"brief": "Add a date column to the CSV row parser and rows.json.", "topics": [],
                         "unknowns": []},
               "research": {"findings": ["rows.py exists"], "sources": [], "unknowns": []},
               "framework": {"decomposition": "one stream", "streams": [], "contracts": []},
               "graph": graph, "prompts": {"tasks": []},
               "critique": {"verdict": "ready", "summary": "ok", "issues": []}}

    def runner(argv, cwd, stdin_text, timeout):
        stage = STAGE.search(stdin_text).group(1)
        calls.append((stage, stdin_text))
        return 0, json.dumps({"result": json.dumps(replies[stage])}), ""

    monkeypatch.setattr(deep_plan, "SPAWN", lambda fn: fn())
    monkeypatch.setattr(deep_plan, "PICK", lambda app: None)
    monkeypatch.setattr(deep_plan, "DISABLED", lambda o: False)
    monkeypatch.setattr(deep_plan, "RUNNER", runner)
    monkeypatch.setattr(planner, "recommend_options", lambda: [dict(m) for m in planner.FALLBACK_MODELS])
    view = deep_plan.start(rows, title="Rows v2", goal="Add a date column to the CSV row parser output rows.json.")
    assert view["status"] == "ready", view
    prompt = dict(calls)["graph"]
    assert "ACCEPTED: A1" in prompt and "Rules learned" in prompt
    b1 = deep_plan.task_brief(rows, view["project"], "B1")
    assert "ACCEPTED: A1" in b1["text"]


def test_loop_learning_planner_draft_prompt_contains_constraint(rows, monkeypatch):
    accept(rows, "lr", "A1")
    prompts = []
    good = {"brief": "b", "nodes": [{"id": "B1", "title": "Date", "goal": "The CSV row parser writes dates to rows.json.",
                                     "check": {"type": "command", "run": "exit 0"}, "deps": []}]}
    replies = [{"questions": []}, good]

    def runner(argv, cwd, stdin_text, timeout):
        prompts.append(stdin_text)
        return 0, json.dumps({"result": json.dumps(replies.pop(0))}), ""

    monkeypatch.setattr(planner, "SPAWN", lambda fn: fn())
    monkeypatch.setattr(planner, "RUNNER", runner)
    monkeypatch.setattr(planner, "recommend_options", lambda: [dict(m) for m in planner.FALLBACK_MODELS])
    view = planner.start(rows, "Rows v2", "Add a date column to the CSV row parser output rows.json")
    planner.draft(rows, view["id"])
    assert "ACCEPTED: A1" in prompts[1]


def test_loop_learning_prompts_have_constraints_slot():
    for name in ("planner_draft.md", "deep_graph.md", "deep_prompts.md"):
        assert "{{constraints}}" in (planner.PROMPT_DIR / name).read_text(encoding="utf-8"), name
