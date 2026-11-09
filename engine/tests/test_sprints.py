"""Sprints page routes (cc_engine.sprints): fakes only, no provider calls, tmp_path databases."""
import time

import pytest

from cc_engine import api, plan
from cc_engine import scheduler as S
from cc_engine import sprints as SP

from test_scheduler import make_env


def usage_rows(now, five=7200, week=5 * 86400, codex=3600):
    return [
        {"pool": "claude_5h", "label": "Claude 5 hours", "remaining_percent": 80, "status": "FRESH",
         "resets_at": S.iso(now + five)},
        {"pool": "claude_week", "label": "Claude week", "remaining_percent": 90, "status": "FRESH",
         "resets_at": S.iso(now + week)},
        {"pool": "codex_5h", "label": "Codex 5 hours", "remaining_percent": 50, "status": "FRESH",
         "resets_at": S.iso(now + codex)},
    ]


class App:
    def __init__(self, store, now, rows=None):
        self.store = store
        self.t = now
        self.rows = rows if rows is not None else usage_rows(now)
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


@pytest.fixture
def proj(store, fixture_plan, tmp_path):
    fixture_plan["cwd"] = str(tmp_path)
    plan.import_plan(store, fixture_plan)
    return "fx"


def test_routes_are_the_extension_contract():
    assert set(SP.ROUTES) == {"GET /api/sprints/timeline", "POST /api/sprints/plan",
                              "POST /api/sprints/update", "POST /api/sprints/cancel",
                              "POST /api/sprints/start-now",
                              # Autonomous tab (test_autonomous.py)
                              "GET /api/autonomous", "POST /api/autonomous/settings", "POST /api/autonomous/run",
                              "POST /api/autonomous/cancel", "POST /api/autonomous/sprint",
                              "POST /api/autonomous/prompt"}


def test_timeline_rows_resets_and_default_starts(store, proj):
    now = time.time()
    app = App(store, now)
    tl = call(app, "GET /api/sprints/timeline", q={"hours": "48"})
    assert [p["id"] for p in tl["pools"]] == ["claude_code", "freebuff", "codex"]
    by = {p["id"]: p for p in tl["pools"]}
    assert by["codex"]["kind"] == "manual" and by["codex"]["default_start"] is None  # Owner's own Codex windows
    assert [r["usage_pool"] for r in by["claude_code"]["resets"]] == ["claude_5h"]  # week is beyond 48 h
    # Default start is the soonest reset + 60 s; a pool with no reset starts now.
    assert by["claude_code"]["default_start"] == S.iso(now + 7200 + 60) and by["claude_code"]["default_aligned"]
    assert by["freebuff"]["default_start"] == S.iso(now) and not by["freebuff"]["default_aligned"]
    assert by["freebuff"]["gate"] == "free" and by["claude_code"]["gate"] == "usage" and "chatmode" not in by
    assert tl["sprints"] == [] and tl["next_wake_at"] is None


def test_plan_defaults_to_reset_plus_60s_and_makes_a_dedicated_run(store, proj):
    now = time.time()
    app = App(store, now)
    out = call(app, "POST /api/sprints/plan", {"project": proj, "pool": "claude_code"})
    assert out["ok"] and out["aligned"] and out["start_at"] == S.iso(now + 7260)
    assert out["end_at"] == S.iso(now + 7260 + 300 * 60)
    run = store.get_run(out["run_id"])
    assert run["status"] == "scheduled" and run["settings"]["planned_by"] == "sprints"
    assert run["settings"]["pools"] == ["claude_code"]
    sp = store.get_sprint(out["sprint_id"])
    assert sp["status"] == "scheduled" and sp["start_at"] == out["start_at"]
    tl = call(app, "GET /api/sprints/timeline")
    [s] = tl["sprints"]
    assert s["state"] == "planned" and s["owned"] and s["editable"]
    assert "one minute after the reset" in s["summary"] and s["project_title"] == "Fixture project"
    assert tl["next_up"] == [s["id"]] and tl["next_wake_at"] == out["start_at"]
    assert any(r["id"] == out["run_id"] for r in tl["runs"])
    # The expanded run sheet is valid for the scheduler.
    assert S.expand_settings(run["settings"])["pools"] == ["claude_code"]


def test_plan_custom_start_and_duration(store, proj):
    now = time.time()
    app = App(store, now)
    out = call(app, "POST /api/sprints/plan", {"project": proj, "pool": "freebuff",
                                                "start_at": S.iso(now + 600), "duration_min": 30})
    assert out["start_at"] == S.iso(now + 600) and out["end_at"] == S.iso(now + 2400)
    assert not out["aligned"]


@pytest.mark.parametrize("body,status", [
    ({"project": "fx", "pool": "codex"}, 400),        # reference row only
    ({"project": "fx", "pool": "gemini_app"}, 400),   # manual, never a sprint
    ({"project": "nope", "pool": "freebuff"}, 404),
    ({"project": "fx", "pool": "freebuff", "start_at": "soon"}, 400),
    ({"project": "fx", "pool": "freebuff", "start_at": 1.0}, 400),       # in the past
    ({"project": "fx", "pool": "freebuff", "duration_min": 0}, 400),
    ({"project": "fx", "pool": "freebuff", "preset": "lavish"}, 400),
])
def test_plan_rejects_bad_input(store, proj, body, status):
    with pytest.raises(api.HttpError) as e:
        call(App(store, time.time()), "POST /api/sprints/plan", body)
    assert e.value.status == status


def test_mutations_are_owner_only(store, proj):
    app = App(store, time.time())
    for route in ("POST /api/sprints/plan", "POST /api/sprints/update", "POST /api/sprints/cancel",
                  "POST /api/sprints/start-now"):
        with pytest.raises(api.HttpError) as e:
            call(app, route, {"project": proj, "pool": "freebuff", "id": "x"}, actor="router")
        assert e.value.status == 403


def test_update_moves_start_and_keeps_length(store, proj):
    now = time.time()
    app = App(store, now)
    out = call(app, "POST /api/sprints/plan", {"project": proj, "pool": "freebuff", "start_at": S.iso(now + 3600)})
    moved = call(app, "POST /api/sprints/update", {"id": out["sprint_id"], "start_at": S.iso(now + 7200)})
    assert moved["start_at"] == S.iso(now + 7200) and moved["end_at"] == S.iso(now + 7200 + 240 * 60)
    sp = store.get_sprint(out["sprint_id"])
    assert sp["start_at"] == S.iso(now + 7200) and sp["status"] == "scheduled"
    assert any("now starts at" in e["summary"] for e in store.events(project_id=proj))
    with pytest.raises(api.HttpError) as e:
        call(app, "POST /api/sprints/update", {"id": "s-missing", "start_at": S.iso(now + 60)})
    assert e.value.status == 404


def test_cancel_owned_sprint_cancels_its_run_and_closes_edits(store, proj):
    now = time.time()
    app = App(store, now)
    out = call(app, "POST /api/sprints/plan", {"project": proj, "pool": "freebuff", "start_at": S.iso(now + 900)})
    res = call(app, "POST /api/sprints/cancel", {"id": out["sprint_id"]})
    assert res["run_cancelled"]
    assert store.get_run(out["run_id"])["status"] == "cancelled"
    assert store.get_sprint(out["sprint_id"])["status"] == "cancelled"
    tl = call(app, "GET /api/sprints/timeline")
    [s] = tl["sprints"]
    assert s["state"] == "cancelled" and not s["editable"] and "was cancelled" in s["summary"]
    assert tl["recently_finished"] == [s["id"]]
    with pytest.raises(api.HttpError) as e:
        call(app, "POST /api/sprints/update", {"id": out["sprint_id"], "start_at": S.iso(now + 60)})
    assert e.value.status == 409


def test_cancel_sprint_of_another_run_keeps_that_run(store, proj):
    now = time.time()
    store.create_run("r-create", proj, {"preset": "balanced", "pools": ["claude_code"]}, status="running")
    sid = S.create_sprint(store, "r-create", "claude_code", now + 3600, project_id=proj)
    res = call(App(store, now), "POST /api/sprints/cancel", {"id": sid})
    assert res["run_cancelled"] is False
    assert store.get_run("r-create")["status"] == "running"
    assert store.get_sprint(sid)["status"] == "cancelled"


def test_start_now_moves_start_and_kicks_a_tick(store, proj):
    now = time.time()
    app = App(store, now)
    out = call(app, "POST /api/sprints/plan", {"project": proj, "pool": "claude_code"})
    res = call(app, "POST /api/sprints/start-now", {"id": out["sprint_id"]})
    assert res["start_at"] == S.iso(now) and res["tick_started"] and app.kicks == [store.path]
    [s] = call(app, "GET /api/sprints/timeline")["sprints"]
    assert s["state"] == "due"


def test_kick_tick_never_runs_for_a_test_database(store):
    assert SP.kick_tick(store) is False


@pytest.fixture
def store_clock(monkeypatch):
    """The store stamps events with ``store.now_iso``; tie it to the test's own clock, so the sprint's
    event window (``_actual`` allows only 1 s of slack) never depends on how busy the machine is."""
    from datetime import datetime, timezone

    from cc_engine import store as ST
    clock = {"t": float(int(time.time()))}
    monkeypatch.setattr(ST, "now_iso",
                        lambda: datetime.fromtimestamp(clock["t"], timezone.utc).isoformat(timespec="seconds"))
    return clock


def test_sprint_holds_pool_until_start_then_reports_actual_time(tmp_path, store_clock):
    env = make_env(tmp_path, [("A", [], "not_started")])
    store = env.store
    now = store_clock["t"]
    rows = usage_rows(now)
    app = App(store, now, rows)
    out = call(app, "POST /api/sprints/plan", {"project": "p1", "pool": "claude_code",
                                                "start_at": S.iso(now + 120), "duration_min": 60})
    reader = lambda: [dict(r) for r in rows]  # noqa: E731
    early = S.tick(store, now, env.dispatcher, usage_reader=reader)
    assert early["started"] == []  # the pool waits for its sprint
    assert store.get_run(out["run_id"])["status"] == "running"
    # Owner pulls it forward; the next tick starts the sprint and its task.
    call(app, "POST /api/sprints/start-now", {"id": out["sprint_id"]})
    store_clock["t"] = app.t = now + 1
    started = S.tick(store, now + 1, env.dispatcher, usage_reader=reader)
    assert started["started"] == ["A"]
    assert store.get_sprint(out["sprint_id"])["status"] == "active"
    [s] = call(app, "GET /api/sprints/timeline")["sprints"]
    assert s["state"] == "active" and s["actual_start"] and s["tasks_started"] == 1
    assert "running since" in s["summary"]
    # The task finishes; the run closes and the block shows its actual execution time.
    store_clock["t"] = app.t = now + 2
    env.dispatcher.kill_job("p1", "A", reason="test", now=now + 2)
    store.set_node_status("p1", "A", "done", "owner", summary="Owner marked A done.")
    store_clock["t"] = app.t = now + 3
    S.tick(store, now + 3, env.dispatcher, usage_reader=reader)
    assert store.get_run(out["run_id"])["status"] == "done"
    tl = call(app, "GET /api/sprints/timeline")
    [s] = tl["sprints"]
    assert s["state"] == "finished" and s["actual_start"] and s["actual_end"]
    assert S.to_epoch(s["actual_end"]) >= S.to_epoch(s["actual_start"])
    assert s["tasks_done"] == 1 and "finished 1 of the 1 task it started" in s["summary"]
    assert tl["recently_finished"] == [s["id"]] and tl["next_up"] == []
