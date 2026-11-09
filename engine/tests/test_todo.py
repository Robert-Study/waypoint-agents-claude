"""Todo list and one-click hand-off to Plan / a project / a run (T1). Fakes only; no provider calls."""
import pytest

from cc_engine import checks, plan, todo
from cc_engine.api import HttpError
from cc_engine.store import OWNER, StaleRevision


@pytest.fixture(autouse=True)
def _manual_handoff_only(monkeypatch):
    """These tests hand to-dos off by hand; auto-triage has its own tests."""
    monkeypatch.setattr(todo, "AUTO_TRIAGE", False)

PROJECT = {
    "id": "fund-lab", "title": "Portfolio optimiser", "brief": "Build efficient frontier charts for the ETF portfolio.",
    "cwd": None, "nodes": [{"id": "N1", "title": "Seed", "goal": "Seed the data.", "check": {"type": "command", "run": "exit 0"},
                            "deps": []}]}
OTHER = {
    "id": "cv-builder", "title": "LaTeX report", "brief": "One page A4 curriculum vitae in LaTeX.",
    "cwd": None, "nodes": [{"id": "C1", "title": "Layout", "goal": "Lay out the report.", "check": {"type": "command", "run": "exit 0"},
                            "deps": []}]}


class App:
    def __init__(self, store):
        self.store = store


class H:
    def __init__(self, store, body=None, actor=OWNER):
        self.app = App(store)
        self.actor = actor
        self._body = body or {}

    def _json_body(self):
        return self._body


def call(store, route, body=None, actor=OWNER, q=None):
    return todo.ROUTES[route](H(store, body, actor), q or {})


def add(store, text, actor=OWNER):
    return call(store, "POST /api/todos", {"text": text}, actor)["todo"]


@pytest.fixture(autouse=True)
def clean_hooks(monkeypatch):
    monkeypatch.setattr(todo, "PLAN_HOOK", None)
    monkeypatch.setattr(todo, "MODEL_RUNNER", None)
    monkeypatch.setattr(todo, "CHECK_HOOK", lambda goal, cwd=None: None)  # no failure_doctor: fallback check


@pytest.fixture
def two_projects(store):
    plan.import_plan(store, PROJECT, actor=OWNER)
    plan.import_plan(store, OTHER, actor=OWNER)
    return store


def test_todo_routes_follow_contract():
    assert {"GET /api/todos", "POST /api/todos", "POST /api/todos/update", "POST /api/todos/handoff"} <= set(todo.ROUTES)


def test_todo_add_list_update(store):
    t = add(store, "  Chase the invoice  ")
    assert t["text"] == "Chase the invoice" and t["status"] == "open" and t["done"] is False and t["handoffs"] == []
    assert call(store, "GET /api/todos")["todos"][0]["id"] == t["id"]
    out = call(store, "POST /api/todos/update", {"id": t["id"], "text": "Chase the invoice today"})["todo"]
    assert out["text"] == "Chase the invoice today"
    out = call(store, "POST /api/todos/update", {"id": t["id"], "done": True})["todo"]
    assert out["done"] is True and out["status"] == "done"
    out = call(store, "POST /api/todos/update", {"id": t["id"], "done": False})["todo"]
    assert out["done"] is False and out["status"] == "open"
    out = call(store, "POST /api/todos/update", {"id": t["id"], "status": "dropped"})["todo"]
    assert out["status"] == "dropped"


def test_todo_rejects_bad_input(store):
    for body in ({}, {"text": "   "}, {"text": "x" * 2001}):
        with pytest.raises(HttpError) as e:
            call(store, "POST /api/todos", body)
        assert e.value.status == 400
    t = add(store, "real")
    with pytest.raises(HttpError) as e:
        call(store, "POST /api/todos/update", {"id": t["id"], "status": "bogus"})
    assert e.value.status == 400
    with pytest.raises(HttpError) as e:
        call(store, "POST /api/todos/update", {"id": "td-missing", "done": True})
    assert e.value.status == 404


def test_todo_survives_a_new_connection(tmp_path):
    from cc_engine.store import Store
    s = Store(tmp_path / "cc.sqlite")
    add(s, "persist me")
    s.close()
    s2 = Store(tmp_path / "cc.sqlite")
    try:
        assert [t["text"] for t in call(s2, "GET /api/todos")["todos"]] == ["persist me"]
    finally:
        s2.close()


def test_todo_update_and_handoff_are_owner_only(store, two_projects):
    t = add(store, "Fix the frontier chart", actor="router")  # adding is open to either credential
    for route, body in (("POST /api/todos/update", {"id": t["id"], "done": True}),
                        ("POST /api/todos/handoff", {"id": t["id"], "to": "run"})):
        with pytest.raises(HttpError) as e:
            call(store, route, body, actor="router")
        assert e.value.status == 403
    assert call(store, "GET /api/todos")["todos"][0]["done"] is False


# ---------------------------------------------------------------- hand-off: plan
def test_todo_handoff_plan_calls_hook_and_records_where_it_went(store):
    seen = {}

    def hook(st, title, goal, cwd=None, actor=None, app=None):
        seen.update(title=title, goal=goal, actor=actor)
        return {"id": "ps-1234", "status": "asking", "title": title}

    todo.PLAN_HOOK = hook
    t = add(store, "Work out a monthly rebalancing routine for the ETF portfolio and automate it")
    out = call(store, "POST /api/todos/handoff", {"id": t["id"], "to": "plan"})
    assert out["ok"] and out["result"]["to"] == "plan" and out["result"]["session"] == "ps-1234"
    assert seen["goal"].startswith("Work out a monthly rebalancing") and len(seen["title"]) <= 80
    assert seen["actor"] == OWNER
    rec = call(store, "GET /api/todos")["todos"][0]
    assert rec["status"] == "handed_off" and rec["handoffs"][0]["to"] == "plan"
    assert rec["handoffs"][0]["ref"]["session"] == "ps-1234" and rec["handoffs"][0]["at"]
    ev = [e for e in store.events() if e["kind"] == "todo"]
    assert ev and "Plan" in ev[-1]["summary"]


def test_todo_handoff_plan_without_planner_is_a_clear_503(store, monkeypatch):
    def boom(*a, **k):
        raise ImportError("no planner")

    todo.PLAN_HOOK = boom
    t = add(store, "plan something")
    with pytest.raises(HttpError) as e:
        call(store, "POST /api/todos/handoff", {"id": t["id"], "to": "plan"})
    assert e.value.status == 503 and e.value.body["error"] == "unavailable"
    assert call(store, "GET /api/todos")["todos"][0]["handoffs"] == []  # nothing recorded for a failure


def test_todo_default_plan_hook_uses_planner_start(store, monkeypatch):
    from cc_engine import planner
    got = {}

    def fake_start(st, title, goal, cwd=None, actor=None, app=None):
        got.update(title=title, goal=goal)
        return {"id": "ps-9", "status": "asking"}

    monkeypatch.setattr(planner, "start", fake_start)
    t = add(store, "Draft a plan for the garden")
    out = call(store, "POST /api/todos/handoff", {"id": t["id"], "to": "plan"})
    assert out["result"]["session"] == "ps-9" and got["goal"] == "Draft a plan for the garden"


# ---------------------------------------------------------------- hand-off: project
def test_todo_handoff_project_picks_best_project_by_keywords(store, two_projects):
    t = add(store, "Add efficient frontier chart for the ETF portfolio")
    rev = store.revision()
    out = call(store, "POST /api/todos/handoff", {"id": t["id"], "to": "project"})["result"]
    assert out["project"] == "fund-lab" and out["node"].startswith("todo-")
    node = store.get_node("fund-lab", out["node"])
    assert node["status"] == "not_started" and node["goal"].startswith("Add efficient frontier chart")
    assert node["check"]["type"] in ("files_exist", "text_present", "command")
    assert out["check"] == node["check"] and out["revision"] == rev + 1 == store.revision()
    assert store.verify_integrity("fund-lab") == []  # goal and check hashes match


def test_todo_handoff_project_check_comes_from_failure_doctor_when_available(store, two_projects):
    good = {"type": "command", "run": "python -m pytest -k frontier -q"}
    todo.CHECK_HOOK = lambda goal, cwd=None: good
    t = add(store, "Test the frontier chart for the ETF portfolio")
    out = call(store, "POST /api/todos/handoff", {"id": t["id"], "to": "project"})["result"]
    assert out["check"] == good and out["check_source"] == "failure_doctor"


def test_todo_handoff_project_rejects_unusable_drafted_checks(store, two_projects):
    for bad in ({"type": "human", "prompt": "looks good"}, {"type": "command"}, "nope", None, {"check": {"type": "files_exist"}}):
        todo.CHECK_HOOK = lambda goal, cwd=None, bad=bad: bad
        t = add(store, "Chart the frontier for the ETF portfolio")
        out = call(store, "POST /api/todos/handoff", {"id": t["id"], "to": "project"})["result"]
        assert out["check_source"] == "fallback" and out["check"]["type"] in ("files_exist", "text_present")


def test_todo_handoff_project_accepts_a_wrapped_check(store, two_projects):
    wrapped = {"check": {"type": "text_present", "path": "notes.md", "text": "frontier"}}
    todo.CHECK_HOOK = lambda goal, cwd=None: wrapped
    t = add(store, "Document the frontier for the ETF portfolio")
    out = call(store, "POST /api/todos/handoff", {"id": t["id"], "to": "project"})["result"]
    assert out["check"] == wrapped["check"] and out["check_source"] == "failure_doctor"


def test_todo_handoff_project_explicit_choice_wins(store, two_projects):
    t = add(store, "Add efficient frontier chart for the ETF portfolio")
    out = call(store, "POST /api/todos/handoff", {"id": t["id"], "to": "project", "project": "cv-builder"})["result"]
    assert out["project"] == "cv-builder"


def test_todo_handoff_project_ambiguous_uses_model_runner_when_given(store, two_projects):
    prompts = []

    def runner(prompt):
        prompts.append(prompt)
        return "I would pick cv-builder."

    todo.MODEL_RUNNER = runner
    t = add(store, "Tidy up the document")  # nothing clearly matches either project
    out = call(store, "POST /api/todos/handoff", {"id": t["id"], "to": "project"})["result"]
    assert out["project"] == "cv-builder" and "fund-lab" in prompts[0] and "cv-builder" in prompts[0]
    assert out["why"]


def test_todo_handoff_project_model_runner_garbage_is_ignored(store, two_projects):
    todo.MODEL_RUNNER = lambda prompt: "no idea"
    t = add(store, "Zebra crossing paint")
    with pytest.raises(HttpError) as e:
        call(store, "POST /api/todos/handoff", {"id": t["id"], "to": "project"})
    assert e.value.status == 409 and e.value.body["error"] == "no_match"
    assert {c["project"] for c in e.value.body["candidates"]} == {"fund-lab", "cv-builder"}


def test_todo_handoff_project_skips_drafts_and_archived(store):
    plan.import_plan(store, PROJECT, actor=OWNER, draft=True)
    t = add(store, "Add efficient frontier chart for the ETF portfolio")
    with pytest.raises(HttpError) as e:
        call(store, "POST /api/todos/handoff", {"id": t["id"], "to": "project"})
    assert e.value.status == 409 and e.value.body["error"] == "no_match"
    with pytest.raises(HttpError) as e:
        call(store, "POST /api/todos/handoff", {"id": t["id"], "to": "project", "project": "fund-lab"})
    assert e.value.status == 409  # a draft is not a target even when named


def test_todo_handoff_project_stale_revision_is_refused(store, two_projects):
    t = add(store, "Add efficient frontier chart for the ETF portfolio")
    with pytest.raises(StaleRevision):
        call(store, "POST /api/todos/handoff", {"id": t["id"], "to": "project", "revision": store.revision() - 1})
    assert call(store, "GET /api/todos")["todos"][0]["handoffs"] == []
    assert [n["id"] for n in store.list_nodes("fund-lab")] == ["N1"]


def test_todo_handoff_project_fallback_goal_names_the_note_file(store, two_projects):
    t = add(store, "Add efficient frontier chart for the ETF portfolio")
    out = call(store, "POST /api/todos/handoff", {"id": t["id"], "to": "project"})["result"]
    node = store.get_node("fund-lab", out["node"])
    path = (node["check"].get("paths") or [node["check"].get("path")])[0]
    assert path in node["goal"]


# ---------------------------------------------------------------- hand-off: run
def test_todo_handoff_run_makes_a_one_node_item_and_starts_nothing(store):
    t = add(store, "Rename the downloads folder structure")
    out = call(store, "POST /api/todos/handoff", {"id": t["id"], "to": "run"})["result"]
    pid = out["project"]
    assert out["to"] == "run" and store.project_runnable(pid)
    nodes = store.list_nodes(pid)
    assert len(nodes) == 1 and nodes[0]["status"] == "not_started" and nodes[0]["goal"].startswith("Rename the downloads")
    assert nodes[0]["check"]["type"] in ("files_exist", "text_present", "command")
    assert store.list_runs() == []  # a run starts only from Autonomous
    assert store.verify_integrity(pid) == []


def test_todo_handoff_run_twice_needs_again(store):
    t = add(store, "Rename the downloads folder structure")
    first = call(store, "POST /api/todos/handoff", {"id": t["id"], "to": "run"})["result"]
    with pytest.raises(HttpError) as e:
        call(store, "POST /api/todos/handoff", {"id": t["id"], "to": "run"})
    assert e.value.status == 409 and e.value.body["error"] == "already_handed_off"
    second = call(store, "POST /api/todos/handoff", {"id": t["id"], "to": "run", "again": True})["result"]
    assert second["project"] != first["project"]
    assert len(call(store, "GET /api/todos")["todos"][0]["handoffs"]) == 2


def test_todo_handoff_rejects_unknown_target_and_done_todo(store):
    t = add(store, "x thing")
    with pytest.raises(HttpError) as e:
        call(store, "POST /api/todos/handoff", {"id": t["id"], "to": "moon"})
    assert e.value.status == 400
    call(store, "POST /api/todos/update", {"id": t["id"], "done": True})
    with pytest.raises(HttpError) as e:
        call(store, "POST /api/todos/handoff", {"id": t["id"], "to": "run"})
    assert e.value.status == 409


def test_todo_merge_duplicates_marks_them_dropped(store):
    a, b, c = add(store, "Book dentist"), add(store, "book the dentist"), add(store, "Other")
    out = call(store, "POST /api/todos/merge", {"keep": a["id"], "merge": [b["id"], c["id"], a["id"]]})
    assert sorted(m["id"] for m in out["merged"]) == sorted([b["id"], c["id"]])
    by = {t["id"]: t for t in call(store, "GET /api/todos")["todos"]}
    assert by[b["id"]]["status"] == "dropped" and by[b["id"]]["merged_into"] == a["id"]
    assert by[a["id"]]["status"] == "open"


def test_todo_checks_module_still_validates_fallback(store, two_projects):
    t = add(store, "Add efficient frontier chart for the ETF portfolio")
    out = call(store, "POST /api/todos/handoff", {"id": t["id"], "to": "project"})["result"]
    plan.validate_plan({"id": "x", "nodes": [{"id": "a", "goal": "g", "check": out["check"], "deps": []}]})
    assert out["check"]["type"] in checks.CHECK_TYPES and out["check"]["type"] != "human"


# ---------------------------------------------------------------- over HTTP
def test_todo_http_roundtrip_owner_and_router(store, tmp_path):
    import http.client
    import json
    import threading

    from cc_engine import api

    srv = api.make_server(store, host="127.0.0.1", port=0, usage_reader=lambda: [], heartbeat=0.5,
                          poll_interval=0.2, token_path=tmp_path / "engine.token")
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def req(method, path, body=None, owner=True):
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        headers = {"Content-Type": "application/json"}
        headers.update({"Origin": "http://127.0.0.1:3080"} if owner else {"X-CC-Token": srv.app.token})
        conn.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"null")
        conn.close()
        return resp.status, data

    try:
        status, body = req("POST", "/api/todos", {"text": "Rename the downloads folder"}, owner=False)
        assert status == 200
        tid = body["todo"]["id"]
        assert req("POST", "/api/todos/update", {"id": tid, "done": True}, owner=False)[0] == 403
        status, body = req("GET", "/api/todos")
        assert status == 200 and body["todos"][0]["id"] == tid
        status, body = req("POST", "/api/todos/handoff", {"id": tid, "to": "run"})
        assert status == 200 and body["result"]["to"] == "run" and body["todo"]["status"] == "handed_off"
        status, body = req("GET", "/api/brief")
        assert status == 200 and isinstance(body["paragraph"], str)
        assert req("POST", "/api/todos/handoff", {"id": tid, "to": "run"})[0] == 409
    finally:
        srv.shutdown()
        srv.server_close()
