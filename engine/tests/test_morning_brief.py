"""Morning brief (GET /api/brief) and todo triage (F4). Fake clock, fake usage; no provider calls."""
from datetime import datetime, timedelta, timezone

import pytest

from cc_engine import brief, needs, plan, todo
from cc_engine.api import HttpError
from cc_engine.store import OWNER

NOW = datetime(2026, 11, 4, 8, 0, 0, tzinfo=timezone.utc)

PLAN = {
    "id": "fund-lab", "title": "Portfolio optimiser", "brief": "Build efficient frontier charts for the ETF portfolio.",
    "cwd": None, "nodes": [
        {"id": "A", "title": "Seed data", "goal": "Seed.", "check": {"type": "command", "run": "exit 0"}, "deps": []},
        {"id": "B", "title": "Frontier chart", "goal": "Chart.", "check": {"type": "command", "run": "exit 0"}, "deps": []},
        {"id": "C", "title": "Report", "goal": "Report.", "check": {"type": "command", "run": "exit 0"}, "deps": []},
        {"id": "D", "title": "Review", "goal": "Look.", "check": {"type": "human", "prompt": "ok"}, "deps": []}]}


class App:
    def __init__(self, store, usage=None):
        self.store = store
        self.usage_reader = (lambda: usage) if usage is not None else None


class H:
    def __init__(self, store, body=None, actor=OWNER, usage=None):
        self.app = App(store, usage)
        self.actor = actor
        self._body = body or {}

    def _json_body(self):
        return self._body


def stamp(store, seq_summary_at):
    """Move every event with the given summary to a chosen time (the store stamps real time)."""
    summary, at = seq_summary_at
    with store.tx() as c:
        c.execute("UPDATE events SET at=? WHERE summary LIKE ?", (at.isoformat(timespec="seconds"), summary))


def mark(store, node, status, hours_ago, actor="check:command"):
    store.set_node_status("fund-lab", node, status, actor if status != "error" else "check:command",
                          summary=f"mark {node} {status}")
    stamp(store, (f"mark {node} {status}", NOW - timedelta(hours=hours_ago)))


@pytest.fixture
def lab(store):
    plan.import_plan(store, PLAN, actor=OWNER)
    return store


def usage_rows(**over):
    rows = [{"pool": "claude_5h", "label": "Claude 5 hours", "remaining_percent": 62, "status": "FRESH"},
            {"pool": "claude_week", "label": "Claude week (all models)", "remaining_percent": 80.4, "status": "STALE"},
            {"pool": "codex_5h", "label": "Codex 5 hours", "remaining_percent": None, "status": "UNKNOWN"},
            {"pool": "gemini_app", "label": "Gemini app", "remaining_percent": None, "status": "UNKNOWN"}]
    return rows if not over else [dict(r, **over.get(r["pool"], {})) for r in rows]


def test_morning_brief_counts_done_failed_needs_you_and_usage(lab):
    mark(lab, "A", "done", 3)
    mark(lab, "B", "error", 2)
    mark(lab, "C", "done", 30)  # outside the 12 h window
    lab.set_node_status("fund-lab", "D", "needs_you", "engine", summary="D needs Owner")
    needs.ask(lab, None, None, "text", "Which folder should the report use?")
    out = brief.build_brief(lab, now=NOW, usage_rows=usage_rows())
    assert (out["done"], out["failed"], out["needs_you"]) == (1, 1, 2)
    assert out["hours"] == 12 and out["done_items"][0]["node"] == "A"
    assert out["failed_items"][0]["title"] == "Frontier chart"
    p = out["paragraph"]
    assert "\n" not in p and "1 task finished" in p and "1 failed" in p and "2 things need you" in p
    assert "Frontier chart" in p
    assert "Claude 5 hours 62%" in p and "Claude week (all models) 80%" in p and "(stale)" not in p
    assert "Codex" not in p and "Gemini" not in p  # unknown usage is never invented
    assert [u["pool"] for u in out["usage"]] == ["claude_5h", "claude_week"]


def test_morning_brief_since_and_default_window(lab):
    mark(lab, "A", "done", 3)
    mark(lab, "C", "done", 30)
    day = brief.build_brief(lab, since=(NOW - timedelta(hours=48)).isoformat(), now=NOW, usage_rows=[])
    assert day["done"] == 2 and day["hours"] == 48
    short = brief.build_brief(lab, hours=1, now=NOW, usage_rows=[])
    assert short["done"] == 0 and "Nothing finished" in short["paragraph"]
    assert brief.build_brief(lab, now=NOW, usage_rows=[])["hours"] == 12


def test_morning_brief_latest_state_in_window_wins(lab):
    mark(lab, "A", "error", 5)
    mark(lab, "A", "done", 4)   # retried and passed
    mark(lab, "B", "done", 6)
    mark(lab, "B", "error", 1)  # passed once, broke later
    out = brief.build_brief(lab, now=NOW, usage_rows=[])
    assert (out["done"], out["failed"]) == (1, 1)
    assert out["done_items"][0]["node"] == "A" and out["failed_items"][0]["node"] == "B"


def test_morning_brief_quiet_night(store):
    out = brief.build_brief(store, now=NOW, usage_rows=[])
    assert out["paragraph"].startswith("Nothing finished or failed in the last 12 hours")
    assert "nothing needs you" in out["paragraph"].lower()
    assert "usage" not in out["paragraph"].lower()  # no readings, so no usage sentence


def test_morning_brief_route_reads_app_usage_and_since(lab):
    mark(lab, "A", "done", 3)
    h = H(lab, usage=usage_rows())
    out = brief.ROUTES["GET /api/brief"](h, {})
    assert out["hours"] == 12 and "Claude 5 hours 62%" in out["paragraph"]
    out = brief.ROUTES["GET /api/brief"](h, {"hours": "2"})
    assert out["hours"] == 2
    with pytest.raises(HttpError) as e:
        brief.ROUTES["GET /api/brief"](h, {"since": "not a date"})
    assert e.value.status == 400


def test_morning_brief_usage_failure_does_not_break_it(lab):
    class Boom(H):
        def __init__(self, store):
            super().__init__(store)
            self.app.usage_reader = lambda: (_ for _ in ()).throw(RuntimeError("no cache"))

    out = brief.ROUTES["GET /api/brief"](Boom(lab), {})
    assert out["usage"] == [] and isinstance(out["paragraph"], str)


def test_morning_brief_route_registered():
    assert "GET /api/brief" in brief.ROUTES


# ---------------------------------------------------------------- triage
def add(store, text):
    return todo.add_todo(store, text)


def test_morning_brief_triage_merges_duplicates_keeping_the_oldest(lab):
    a = add(lab, "Book the dentist")
    b = add(lab, "book dentist")
    c = add(lab, "Fix the login bug")
    d = add(lab, "Fix login bug in checkout")
    e = add(lab, "Call mum")
    f = add(lab, "Call dad")
    out = brief.triage(lab)
    groups = {g["keep"]: sorted(g["merge"]) for g in out["merges"]}
    assert groups == {a["id"]: [b["id"]], c["id"]: [d["id"]]}
    assert all(g["similarity"] >= 0.7 for g in out["merges"])
    assert e["id"] not in groups and f["id"] not in groups


def test_morning_brief_triage_ignores_closed_and_handed_off(lab):
    a = add(lab, "Book the dentist")
    b = add(lab, "Book the dentist")
    todo.update_todo(lab, b["id"], done=True)
    assert brief.triage(lab)["merges"] == []
    c = add(lab, "Book the dentist")
    assert [g["merge"] for g in brief.triage(lab)["merges"]] == [[c["id"]]]
    todo._save(lab, a["id"], status="handed_off")
    assert brief.triage(lab)["merges"] == []


def test_morning_brief_triage_proposes_a_project_for_each_kept_todo(lab):
    plan.import_plan(lab, {"id": "cv", "title": "LaTeX report", "brief": "One page curriculum vitae in LaTeX.", "cwd": None,
                           "nodes": [{"id": "C1", "goal": "g", "check": {"type": "command", "run": "exit 0"}, "deps": []}]},
                     actor=OWNER)
    a = add(lab, "Add a frontier chart for the ETF portfolio")
    add(lab, "add frontier chart for ETF portfolio")
    c = add(lab, "Shorten the LaTeX curriculum vitae to one page")
    z = add(lab, "Zebra crossing paint")
    out = brief.triage(lab)
    by = {s["todo"]: s for s in out["suggestions"]}
    assert by[a["id"]]["project"] == "fund-lab" and by[a["id"]]["title"] == "Portfolio optimiser"
    assert by[c["id"]]["project"] == "cv"
    assert z["id"] not in by  # no clear match, no suggestion
    assert len(out["suggestions"]) == 2  # the merged duplicate gets none of its own


def test_morning_brief_triage_empty_without_todos(store):
    assert brief.triage(store) == {"merges": [], "suggestions": [], "open": 0}


def test_morning_brief_includes_triage(lab):
    add(lab, "Book the dentist")
    add(lab, "book dentist")
    out = brief.build_brief(lab, now=NOW, usage_rows=[])
    assert len(out["triage"]["merges"]) == 1
