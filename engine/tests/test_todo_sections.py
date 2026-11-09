"""Todo sections, ordering, click/archive, auto-tick and triage (waypoint-v5 E1). Fakes only."""
from datetime import datetime, timedelta, timezone

import pytest

from cc_engine import plan, todo
from cc_engine.api import HttpError
from cc_engine.store import OWNER

PROJECT = {
    "id": "fund-lab", "title": "Portfolio optimiser", "brief": "Build efficient frontier charts for the ETF portfolio.",
    "cwd": None, "nodes": [{"id": "N1", "title": "Seed", "goal": "Seed the data.", "check": {"type": "command", "run": "exit 0"},
                            "deps": []}]}


class App:
    def __init__(self, store):
        self.store = store
        self.todo_auto_triage = True


class H:
    def __init__(self, store, body=None, actor=OWNER):
        self.app = App(store)
        self.actor = actor
        self._body = body or {}

    def _json_body(self):
        return self._body


def call(store, route, body=None, actor=OWNER, q=None):
    return todo.ROUTES[route](H(store, body, actor), q or {})


def add(store, text, actor=OWNER, **kw):
    kw.setdefault("triage", False)
    return call(store, "POST /api/todos", {"text": text, **kw}, actor)["todo"]


@pytest.fixture(autouse=True)
def clean_hooks(monkeypatch):
    monkeypatch.setattr(todo, "PLAN_HOOK", lambda *a, **k: {"id": "ps-1"})
    monkeypatch.setattr(todo, "MODEL_RUNNER", None)
    monkeypatch.setattr(todo, "CHECK_HOOK", lambda goal, cwd=None: None)


def test_sections_and_ordering(store):
    c1 = add(store, "computer one")
    h1 = add(store, "human one", section="human")
    h2 = add(store, "human two", section="human")
    h0 = add(store, "human first", section="human", position=0)
    out = call(store, "GET /api/todos")
    assert [t["id"] for t in out["sections"]["human"]] == [h0["id"], h1["id"], h2["id"]]
    assert [t["id"] for t in out["sections"]["computer"]] == [c1["id"]]
    assert [t["id"] for t in out["todos"]] == [c1["id"], h0["id"], h1["id"], h2["id"]]
    assert out["counts"]["human_open"] == 3 and out["counts"]["computer_open"] == 1 and out["counts"]["archived"] == 0
    assert c1["section"] == "computer" and c1["node"] is None and c1["deliverables"] == []


def test_reorder_owner_and_router(store):
    a, b, c = (add(store, t, section="human") for t in ("a", "b", "c"))
    call(store, "POST /api/todos/reorder", {"section": "human", "ids": [c["id"], a["id"], b["id"]]}, actor="router")
    got = [t["id"] for t in call(store, "GET /api/todos")["sections"]["human"]]
    assert got == [c["id"], a["id"], b["id"]]
    assert [t["position"] for t in call(store, "GET /api/todos")["sections"]["human"]] == [1, 2, 3]
    with pytest.raises(HttpError) as e:
        call(store, "POST /api/todos/reorder", {"section": "human", "ids": [a["id"]]}, actor="stranger")
    assert e.value.status == 403


def test_click_then_archive_after_24h(store):
    t = add(store, "review the report", section="human")
    with pytest.raises(HttpError):
        call(store, "POST /api/todos/click", {"id": t["id"]}, actor="router")
    clicked = call(store, "POST /api/todos/click", {"id": t["id"]})["todo"]
    assert clicked["first_clicked_at"] and clicked["archive_due_at"]
    first = clicked["first_clicked_at"]
    assert call(store, "POST /api/todos/click", {"id": t["id"]})["todo"]["first_clicked_at"] == first
    base = datetime.fromisoformat(first)
    assert todo.housekeep(store, now=base + timedelta(hours=23)) == 0
    assert todo.housekeep(store, now=base + timedelta(hours=24)) == 1
    out = call(store, "GET /api/todos")
    assert out["todos"] == [] and out["counts"]["archived"] == 1
    arch = call(store, "GET /api/todos", q={"archived": "1"})
    assert [x["id"] for x in arch["todos"]] == [t["id"]] and arch["todos"][0]["archived_at"]


def test_auto_tick_from_done_node(store):
    plan.import_plan(store, PROJECT, actor=OWNER)
    t = add(store, "seed things", node={"project": "fund-lab", "id": "N1"})
    assert t["node"] == {"project": "fund-lab", "id": "N1"}
    assert todo.housekeep(store) == 0
    store.set_node_status("fund-lab", "N1", "done", OWNER)
    out = call(store, "GET /api/todos")["todos"][0]
    assert out["status"] == "done" and out["done_by"] == "engine"
    assert out["deliverables"] == [{"label": "Task result", "project": "fund-lab", "node": "N1"}]


def test_approval_todo_is_never_auto_ticked_by_its_node_finishing(store):
    """A "you" (human) item asks Owner to decide or approve; a worker finishing the linked task must not
    tick it, or the approval is lost. Only computer items auto-tick."""
    plan.import_plan(store, PROJECT, actor=OWNER)
    ask = add(store, "Approve the seeded data before release", section="human",
              node={"project": "fund-lab", "id": "N1"})
    job = add(store, "seed things", node={"project": "fund-lab", "id": "N1"})
    store.set_node_status("fund-lab", "N1", "done", OWNER)
    assert todo.housekeep(store) == 1
    listed = {t["id"]: t for t in call(store, "GET /api/todos")["todos"]}
    assert listed[job["id"]]["status"] == "done" and listed[job["id"]]["done_by"] == "engine"
    assert listed[ask["id"]]["status"] == "open" and listed[ask["id"]]["done_by"] is None
    assert listed[ask["id"]]["deliverables"] == []
    # moved to the human section after it was linked: still never auto-ticked
    moved = add(store, "check the charts", node={"project": "fund-lab", "id": "N1"}, section="computer")
    call(store, "POST /api/todos/update", {"id": moved["id"], "section": "human"})
    todo.housekeep(store)
    assert todo.get_todo(store, moved["id"])["status"] == "open"
    # Owner can still tick it himself
    out = call(store, "POST /api/todos/update", {"id": ask["id"], "done": True})["todo"]
    assert out["status"] == "done" and out["done_by"] == OWNER


def test_triage_to_project_and_to_run(store):
    plan.import_plan(store, PROJECT, actor=OWNER)
    t = call(store, "POST /api/todos", {"text": "Fix the efficient frontier portfolio optimiser charts"})["todo"]
    assert t["status"] == "handed_off" and t["node"]["project"] == "fund-lab"
    assert store.get_node("fund-lab", t["node"]["id"]) is not None
    r = call(store, "POST /api/todos", {"text": "Wash the car thoroughly"})["todo"]
    assert r["status"] == "handed_off" and r["node"]["project"].startswith("todo-") and r["node"]["id"] == "task"
    # human items and triage:false are left alone
    assert call(store, "POST /api/todos", {"text": "call mum", "section": "human"})["todo"]["handoffs"] == []
    assert add(store, "leave me")["status"] == "open"


def test_triage_failure_leaves_item_open(store, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("provider down")
    monkeypatch.setattr(todo, "hand_off", boom)
    t = call(store, "POST /api/todos", {"text": "anything at all"})["todo"]
    assert t["status"] == "open" and t["node"] is None
    assert "provider down" in t["handoffs"][-1]["summary"]


def test_old_rows_default_to_computer(store):
    todo.ensure_schema(store)
    with store.tx() as c:
        c.execute("UPDATE todos SET section=NULL")
    t = add(store, "legacy")
    with store.tx() as c:
        c.execute("UPDATE todos SET section=NULL, position=NULL WHERE id=?", (t["id"],))
    assert todo.get_todo(store, t["id"])["section"] == "computer"
    assert [x["id"] for x in call(store, "GET /api/todos")["sections"]["computer"]] == [t["id"]]


def test_update_accepts_new_fields(store):
    t = add(store, "move me")
    out = call(store, "POST /api/todos/update", {"id": t["id"], "section": "human", "position": 4, "link": "http://x",
                                                  "node": {"project": "p", "id": "n"},
                                                  "deliverables": [{"label": "Doc", "url": "http://d"}]})["todo"]
    assert (out["section"], out["position"], out["link"]) == ("human", 4, "http://x")
    assert out["node"] == {"project": "p", "id": "n"} and out["deliverables"][0]["label"] == "Doc"
    with pytest.raises(HttpError):
        call(store, "POST /api/todos/update", {"id": t["id"], "section": "bogus"})


def test_notify_called(store):
    seen = []
    store.subscribe(lambda kind, payload: seen.append((kind, payload)))
    t = add(store, "tell everyone", section="human")
    call(store, "POST /api/todos/click", {"id": t["id"]})
    call(store, "POST /api/todos/reorder", {"section": "human", "ids": [t["id"]]})
    kinds = [k for k, _ in seen]
    assert kinds.count("todo") >= 3
    assert seen[0][1] == {"id": t["id"], "section": "human", "status": "open"}
