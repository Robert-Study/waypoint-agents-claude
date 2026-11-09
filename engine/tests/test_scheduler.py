import json
from pathlib import Path

import pytest

from cc_engine import scheduler as S
from cc_engine import dispatch as D
from cc_engine import failure
from cc_engine import plan
from cc_engine.dispatch import Dispatcher, run_job
from cc_engine.store import Store

T0 = 1_800_000_000.0  # fixed epoch for determinism

CORDIS = """\
- id: llm-pi-ai
  config:
    providers:
      freebuff:
        models:
          - id: deepseek/deepseek-v4-flash
          - id: openai/gpt-6-luna
"""


# ---------------------------------------------------------------- helpers (shared by
# test_failure.py and test_power.py). Real Store, real plan import; fakes for runners.

class FakeClock:
    def __init__(self, t=T0):
        self.t = t
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


class Env:
    pass


def sheet(pools=("claude_code",), preset="balanced", **kw):
    """Run settings in run-sheet form; scheduler/advanced keys pass through kw."""
    top = {k: kw.pop(k) for k in list(kw) if k in ("auto_resume", "power_off", "merge_resets",
                                                   "align_to_reset", "default_pool", "sprints",
                                                   "reserve_percent", "job_timeout_minutes")}
    out = {"preset": preset, "pools": list(pools), **top}
    if kw:
        out["advanced"] = kw
    return out


def make_env(tmp_path, specs, project_id="p1"):
    """specs: [(id, deps, status[, extra node fields])] -> Env with a seeded real Store."""
    env = Env()
    env.db = tmp_path / "cc.sqlite"
    env.store = Store(env.db)
    env.clock = FakeClock()
    env.launched = []
    env.alive = set()
    env.killed = []
    env.next_pid = [1000]
    cfg = tmp_path / "cordis.patch.yml"
    cfg.write_text(CORDIS, encoding="utf-8")
    env.cordis = cfg

    def launcher(spec_path):
        env.launched.append(json.loads(Path(spec_path).read_text(encoding="utf-8")))
        env.next_pid[0] += 1
        env.alive.add(env.next_pid[0])
        return {"pid": env.next_pid[0]}

    def killer(pid):
        env.killed.append(pid)
        env.alive.discard(pid)
        return 0

    env.launcher = launcher
    env.killer = killer
    env.pid_alive = lambda pid, started_at=None: pid in env.alive
    env.asked = []

    def asker(**kw):
        env.asked.append(kw)
        return f"q{len(env.asked)}"

    env.asker = asker
    env.new_dispatcher = lambda **kw: Dispatcher(
        data_dir=tmp_path / "data", launcher=launcher, clock=env.clock, sleep=env.clock.sleep,
        pid_alive=env.pid_alive, killer=killer, freebuff_config=cfg, **kw)
    env.dispatcher = env.new_dispatcher(asker=asker)
    env.reopen = lambda: Store(env.db)
    seed(env.store, specs, project_id, cwd=str(tmp_path))
    return env


def seed(store, specs, project_id="p1", cwd=None):
    nodes, statuses_ = [], {}
    for spec in specs:
        nid, deps, status = spec[:3]
        extra = dict(spec[3]) if len(spec) > 3 else {}
        node = {"id": nid, "title": f"Task {nid}", "goal": f"Goal {nid}.",
                "check": {"type": "command", "run": "exit 0"}, "deps": list(deps),
                "owner": "execution", "lane": "engine",
                "pool": "claude_code", "model": "sonnet", "effort": "high"}
        node.update(extra)
        nodes.append({k: v for k, v in node.items() if v is not None})
        statuses_[nid] = status
    plan.import_plan(store, {"id": project_id, "title": "Proj", "brief": "Brief.", "cwd": cwd,
                             "nodes": nodes})
    accept = getattr(store, "accept_project", None)
    if callable(accept) and getattr(store, "is_draft", lambda p: False)(project_id):
        accept(project_id)
    for nid, status in statuses_.items():
        if status != "not_started":
            store.set_node_status(project_id, nid, status, "owner" if status == "done" else "test")


def add_run(store, project_id, settings, status="queued", run_id=None):
    run_id = run_id or f"r{len(store.list_runs(limit=10000)) + 1}"
    store.create_run(run_id, project_id, settings, status=status)
    return run_id


def statuses(store, project_id="p1"):
    return {n["id"]: n["status"] for n in store.list_nodes(project_id)}


def events(store):
    return store.events(limit=100000)


def usage(remaining=80, resets=T0 + 86400, status="FRESH", week=90, extra=()):
    """A usage reader with Claude 5h/week rows (and any extra rows)."""
    rows = [{"pool": "claude_5h", "label": "Claude 5 hours", "remaining_percent": remaining,
             "resets_at": S.iso(resets) if resets else None, "status": status},
            {"pool": "claude_week", "label": "Claude week", "remaining_percent": week,
             "resets_at": S.iso(T0 + 7 * 86400), "status": "FRESH"}]
    rows += list(extra)
    return lambda: [dict(r) for r in rows]


NO_USAGE = lambda: []  # noqa: E731
OK_USAGE = usage()


# ---------------------------------------------------------------- scheduling

def test_flexible_waves_start_every_ready_node(tmp_path):
    env = make_env(tmp_path, [
        ("A", [], "done"), ("B", ["A"], "not_started"),          # wave 1 ready
        ("C", [], "not_started"),                                  # wave 0 ready
        ("X", ["A"], "done"), ("Y", ["X"], "done"), ("Z", ["Y"], "not_started"),  # wave 3 ready
        ("W", ["B"], "not_started"),                               # blocked by B
    ])
    add_run(env.store, "p1", sheet(max_concurrency=10))
    res = S.tick(env.store, T0, env.dispatcher, usage_reader=OK_USAGE)
    assert sorted(res["started"]) == ["B", "C", "Z"]
    st = statuses(env.store)
    assert st["W"] == "not_started" and st["B"] == "running"
    assert len(env.launched) == 3


def test_max_concurrency_and_idempotent_ticks(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started"), ("B", [], "not_started"),
                              ("C", [], "not_started")])
    add_run(env.store, "p1", sheet(max_concurrency=2))
    r1 = S.tick(env.store, T0, env.dispatcher, usage_reader=OK_USAGE)
    assert len(r1["started"]) == 2
    rev = env.store.revision()
    r2 = S.tick(env.store, T0 + 1, env.dispatcher, usage_reader=OK_USAGE)
    assert r2["started"] == [] and len(env.launched) == 2
    assert env.store.revision() == rev  # a no-op tick changes nothing in the plan


def test_lease_blocks_concurrent_tick_and_expires_after_15_min(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started")])
    add_run(env.store, "p1", sheet())
    assert S.acquire_lease(env.store, T0, holder="other") == "other"
    held = S.tick(env.store, T0 + 60, env.dispatcher, usage_reader=OK_USAGE)
    assert held["lease"] == "held" and held["started"] == []
    assert S.tick(env.store, T0 + S.LEASE_SECONDS - 1, env.dispatcher,
                  usage_reader=OK_USAGE)["lease"] == "held"
    later = S.tick(env.store, T0 + S.LEASE_SECONDS + 1, env.dispatcher, usage_reader=OK_USAGE)
    assert later["lease"] == "acquired" and later["started"] == ["A"]
    assert env.store.get_meta(S.LEASE_KEY) is None  # released


def test_lease_from_second_process_connection(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started")])
    other = env.reopen()  # another process's Store on the same file
    try:
        assert S.acquire_lease(other, T0) is not None
        assert S.acquire_lease(env.store, T0 + 1) is None
    finally:
        other.close()


def test_unticked_pool_is_not_used(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started", {"pool": "freebuff"})])
    add_run(env.store, "p1", sheet(["claude_code"]))
    res = S.tick(env.store, T0, env.dispatcher, usage_reader=OK_USAGE)
    assert res["started"] == [] and res["blocked"][0]["id"] == "A"
    assert env.launched == []


def test_assigns_pool_and_default_model_from_ladder(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started", {"pool": None, "model": None,
                                                         "effort": None})])
    add_run(env.store, "p1", sheet(effort="medium", model_ladder={"claude_code": ["sonnet", "opus"]}))
    S.tick(env.store, T0, env.dispatcher, usage_reader=OK_USAGE)
    n = env.store.get_node("p1", "A")
    assert (n["pool"], n["model"], n["effort"], n["attempts"]) == ("claude_code", "sonnet", "medium", 1)
    argv = env.launched[0]["argv"]
    # argv[0] is the resolved CLI (claude.cmd on Windows; a bare "claude" cannot be spawned there).
    assert Path(argv[0]).stem.lower() == "claude"
    assert argv[1:6] == ["-p", "--model", "sonnet", "--output-format", "json"]
    assert n["evidence"].startswith("attempt 1: ")


def test_unpinned_text_node_falls_over_to_freebuff(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started", {
        "pool": None, "model": None, "check": {"type": "text_present", "path": "a.md", "text": "x"}})])
    add_run(env.store, "p1", sheet(["claude_code", "freebuff"]))
    res = S.tick(env.store, T0, env.dispatcher, usage_reader=usage(remaining=0, resets=T0 + 600))
    assert res["started"] == ["A"]
    n = env.store.get_node("p1", "A")
    assert n["pool"] == "freebuff" and n["model"] == "deepseek/deepseek-v4-flash"
    assert env.launched[0]["body"]["model"] == "deepseek/deepseek-v4-flash"


def test_freebuff_never_gets_file_or_command_nodes(tmp_path):
    env = make_env(tmp_path, [
        ("A", [], "not_started", {"pool": "freebuff"}),                       # command check
        ("B", [], "not_started", {"pool": None, "model": None,
                                  "check": {"type": "files_exist", "paths": ["b"]}}),
    ])
    add_run(env.store, "p1", sheet(["freebuff"]))
    res = S.tick(env.store, T0, env.dispatcher, usage_reader=NO_USAGE)
    assert res["started"] == [] and env.launched == []
    assert {b["reason"] for b in res["blocked"]} == {"freebuff takes text-only tasks"}


def test_freebuff_file_work_allowed_by_advanced_flag(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started", {"pool": "freebuff", "model": "deepseek-chat"})])
    add_run(env.store, "p1", sheet(["freebuff"], freebuff_file_work=True))
    res = S.tick(env.store, T0, env.dispatcher, usage_reader=NO_USAGE)
    assert res["started"] == ["A"]
    assert env.store.get_node("p1", "A")["model"] == "deepseek/deepseek-v4-flash"


def test_freebuff_ladder_uses_cordis_ids(tmp_path, monkeypatch):
    cfg = tmp_path / "c.yml"
    cfg.write_text(CORDIS, encoding="utf-8")
    monkeypatch.setattr(D, "FREEBUFF_CONFIG", cfg)
    out = S.expand_settings(sheet(["freebuff"]))
    assert out["model_ladder"]["freebuff"] == ["deepseek/deepseek-v4-flash", "openai/gpt-6-luna"]


def test_codex_pool_waits_while_codex_is_switched_off(tmp_path):
    # Codex is a dispatch route (test_codex_route.py) but off until Owner switches it on.
    env = make_env(tmp_path, [("A", [], "not_started", {"pool": "codex"})])
    add_run(env.store, "p1", {"preset": "balanced", "pools": ["claude_code", "codex"]})
    res = S.tick(env.store, T0, env.dispatcher, usage_reader=OK_USAGE)
    assert res["started"] == [] and env.launched == []
    assert res["blocked"][0]["reason"] == "Codex is switched off in Agents"
    assert "codex" not in S.expand_settings({"pools": ["claude_code", "codex"]}, toggles=env.store)["pools"]


def test_draft_project_is_not_dispatched(tmp_path, monkeypatch):
    env = make_env(tmp_path, [("A", [], "not_started")])
    add_run(env.store, "p1", sheet())
    monkeypatch.setattr(env.store, "project_runnable", lambda pid: False, raising=False)
    res = S.tick(env.store, T0, env.dispatcher, usage_reader=OK_USAGE)
    assert res["started"] == [] and env.launched == []
    assert {"run": "r1", "reason": "project is a draft"} in res["blocked"]


# ---------------------------------------------------------------- usage gate

def test_usage_gate_stale_or_unknown_holds_claude_and_logs_once(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started")])
    add_run(env.store, "p1", sheet())
    stale = usage(status="STALE")
    for t in (T0, T0 + 1, T0 + 2):
        assert S.tick(env.store, t, env.dispatcher, usage_reader=stale)["started"] == []
    paused = [e for e in events(env.store) if e["kind"] == "pool"]
    assert len(paused) == 1 and "stale" in paused[0]["summary"]
    # No usage rows at all = UNKNOWN: still held.
    assert S.tick(env.store, T0 + 3, env.dispatcher, usage_reader=NO_USAGE)["started"] == []
    assert S.tick(env.store, T0 + 4, env.dispatcher, usage_reader=OK_USAGE)["started"] == ["A"]


def test_usage_gate_below_reserve_floor_holds(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started")])
    run_id = add_run(env.store, "p1", sheet(auto_resume=False, merge_resets=False, reserve_percent=10))
    res = S.tick(env.store, T0, env.dispatcher, usage_reader=usage(remaining=8))
    assert res["started"] == []
    assert any("below the 10% reserve" in e["summary"] for e in events(env.store))
    assert env.store.list_sprints(run_id) == []


def test_usage_gate_reserve_floor_from_settings(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started")])
    add_run(env.store, "p1", sheet(reserve_percent=5))
    assert S.tick(env.store, T0, env.dispatcher, usage_reader=usage(remaining=8))["started"] == ["A"]


def test_usage_gate_week_pool_counts_too(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started")])
    add_run(env.store, "p1", sheet(auto_resume=False, merge_resets=False, reserve_percent=10))
    assert S.tick(env.store, T0, env.dispatcher, usage_reader=usage(week=3))["started"] == []


def test_usage_gate_extra_usage_never_counts(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started")])
    add_run(env.store, "p1", sheet(auto_resume=False, merge_resets=False))
    extra = [{"pool": "claude_extra", "remaining_percent": 100, "status": "FRESH"}]
    res = S.tick(env.store, T0, env.dispatcher, usage_reader=usage(remaining=0, extra=extra))
    assert res["started"] == []


def test_usage_gate_free_pools_unaffected(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started", {
        "pool": "freebuff", "check": {"type": "text_present", "path": "a", "text": "b"}})])
    add_run(env.store, "p1", sheet(["freebuff"]))
    assert S.tick(env.store, T0, env.dispatcher, usage_reader=NO_USAGE)["started"] == ["A"]


def test_usage_gate_freebuff_not_gated_on_codex(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started", {"pool": "freebuff", "model": None,
                                                         "check": {"type": "text_present", "path": "a", "text": "b"}})])
    add_run(env.store, "p1", sheet(["freebuff"]))
    codex_out = lambda: [{"pool": "codex_5h", "remaining_percent": 0, "status": "FRESH",  # noqa: E731
                          "resets_at": S.iso(T0 + 600)}]
    assert S.tick(env.store, T0, env.dispatcher, usage_reader=codex_out)["started"] == ["A"]


# ---------------------------------------------------------------- sprints

def test_sprint_aligned_to_reset_plus_60s(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started")])
    run_id = add_run(env.store, "p1", sheet(align_to_reset=True))
    u = usage(remaining=40, resets=T0 + 3600)
    assert S.tick(env.store, T0, env.dispatcher, usage_reader=u)["started"] == []
    sp = env.store.list_sprints(run_id)
    assert len(sp) == 1 and S.to_epoch(sp[0]["start_at"]) == T0 + 3660
    assert S.tick(env.store, T0 + 3659, env.dispatcher, usage_reader=u)["started"] == []
    fresh = usage(remaining=100, resets=T0 + 3600 + 5 * 3600)
    assert S.tick(env.store, T0 + 3660, env.dispatcher, usage_reader=fresh)["started"] == ["A"]
    assert env.store.list_sprints(run_id)[0]["status"] == "active"


def test_used_up_pool_pauses_and_auto_resumes_after_reset(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started")])
    run_id = add_run(env.store, "p1", sheet(auto_resume=True))
    used = usage(remaining=0, resets=T0 + 600)
    assert S.tick(env.store, T0, env.dispatcher, usage_reader=used)["started"] == []
    assert S.tick(env.store, T0 + 10, env.dispatcher, usage_reader=used)["started"] == []
    sp = env.store.list_sprints(run_id)
    assert len(sp) == 1 and S.to_epoch(sp[0]["start_at"]) == T0 + 660
    assert any("used up" in e["summary"] for e in events(env.store))
    # After the reset the old reading predates it: held until a fresh reading arrives.
    assert S.tick(env.store, T0 + 661, env.dispatcher, usage_reader=used)["started"] == []
    fresh = usage(remaining=100, resets=T0 + 600 + 5 * 3600)
    assert S.tick(env.store, T0 + 662, env.dispatcher, usage_reader=fresh)["started"] == ["A"]


def test_editable_sprint_start(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started")])
    run_id = add_run(env.store, "p1", sheet())
    sid = S.create_sprint(env.store, run_id, "claude_code", T0 + 7200)
    assert S.tick(env.store, T0 + 100, env.dispatcher, usage_reader=OK_USAGE)["started"] == []
    S.update_sprint(env.store, sid, T0 + 200)
    assert env.store.get_sprint(sid)["start_at"] == S.iso(T0 + 200)
    assert any(e["actor"] == "owner" and "now starts at" in e["summary"] for e in events(env.store))
    assert S.tick(env.store, T0 + 201, env.dispatcher, usage_reader=OK_USAGE)["started"] == ["A"]


def test_settings_sprints_start_at_is_honoured_and_edits_win(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started"), ("B", ["A"], "not_started")])
    run_id = add_run(env.store, "p1", sheet(
        sprints=[{"pool": "claude_code", "start_at": S.iso(T0 + 3600)}]))
    assert S.tick(env.store, T0, env.dispatcher, usage_reader=OK_USAGE)["started"] == []
    sp = env.store.list_sprints(run_id)
    assert len(sp) == 1 and sp[0]["pool"] == "claude_code"
    assert S.to_epoch(sp[0]["start_at"]) == T0 + 3600
    # Owner edits it earlier (the api route updates start_at only): the edit wins.
    env.store.update_sprint(sp[0]["id"], start_at=S.iso(T0 + 600))
    assert S.tick(env.store, T0 + 599, env.dispatcher, usage_reader=OK_USAGE)["started"] == []
    assert S.tick(env.store, T0 + 600, env.dispatcher, usage_reader=OK_USAGE)["started"] == ["A"]
    assert len(env.store.list_sprints(run_id)) == 1  # never re-seeded from settings
    # Moving an active sprint into the future pauses the pool again.
    env.store.update_sprint(sp[0]["id"], start_at=S.iso(T0 + 9000))
    env.store.set_node_status("p1", "A", "done", "owner")
    assert S.tick(env.store, T0 + 700, env.dispatcher, usage_reader=OK_USAGE)["started"] == []


def test_settings_sprints_skip_pools_with_existing_rows(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started")])
    run_id = add_run(env.store, "p1", sheet(
        sprints=[{"pool": "claude_code", "start_at": S.iso(T0 + 3600)}]))
    S.create_sprint(env.store, run_id, "claude_code", T0 + 60, actor="owner")  # made by p_run
    assert S.tick(env.store, T0 + 61, env.dispatcher, usage_reader=OK_USAGE)["started"] == ["A"]
    assert len(env.store.list_sprints(run_id)) == 1


# ---------------------------------------------------------------- jobs: lost / timeout

def test_lost_job_resume_after_simulated_restart(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started")])
    add_run(env.store, "p1", sheet(auto_resume=True))
    S.tick(env.store, T0, env.dispatcher, usage_reader=OK_USAGE)
    env.store.close()
    env.alive.clear()  # "Reboot": the wrapper process is gone and wrote no exit file
    store2 = env.reopen()
    disp2 = env.new_dispatcher()
    res = S.tick(store2, T0 + 120, disp2, usage_reader=OK_USAGE)
    assert res["resumed"] == ["A"] and res["started"] == ["A"]
    n = store2.get_node("p1", "A")
    assert n["status"] == "running" and n["attempts"] == 1  # the lost try is not counted
    assert any("interrupted" in e["summary"] for e in events(store2))
    store2.close()


def test_lost_job_live_pid_after_sleep_starts_no_second_copy(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started")])
    add_run(env.store, "p1", sheet(auto_resume=True))
    S.tick(env.store, T0, env.dispatcher, usage_reader=OK_USAGE)
    # Woke after 8 h: the heartbeat is ancient but the pid is alive and active time is small.
    res = S.tick(env.store, T0 + 8 * 3600, env.dispatcher, usage_reader=usage(resets=T0 + 9 * 3600))
    assert res["resumed"] == [] and res["started"] == [] and res["transient"] == []
    assert len(env.launched) == 1 and env.killed == []
    assert statuses(env.store)["A"] == "running"


def test_lost_job_restart_with_auto_resume_off_marks_error(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started")])
    add_run(env.store, "p1", sheet(auto_resume=False))
    S.tick(env.store, T0, env.dispatcher, usage_reader=OK_USAGE)
    env.alive.clear()
    S.tick(env.store, T0 + 120, env.dispatcher, usage_reader=OK_USAGE)
    assert statuses(env.store)["A"] == "error"


def test_lost_job_timeout_kills_tree_and_is_transient(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started")])
    add_run(env.store, "p1", sheet(job_timeout_minutes=90))
    S.tick(env.store, T0, env.dispatcher, usage_reader=OK_USAGE)
    job = env.dispatcher.latest_job("p1", "A")
    (job / "heartbeat").write_text(json.dumps({"at": T0 + 5401, "active": 5401}), encoding="utf-8")
    res = S.tick(env.store, T0 + 5401, env.dispatcher, usage_reader=OK_USAGE)
    assert res["transient"] == [{"id": "A", "reason": "timeout", "action": "retry"}]
    assert env.killed == [1001]
    n = env.store.get_node("p1", "A")
    assert n["status"] == "not_started" and n["attempts"] == 0 and n["model"] == "sonnet"


# ---------------------------------------------------------------- transient exits

def _finish(env, node_id, exec_runner=lambda *a: 0, err_text=None):
    job = env.dispatcher.latest_job("p1", node_id)

    def runner(argv, cwd, stdin_text, out, err):
        if err_text:
            Path(err).write_text(err_text, encoding="utf-8")
        return exec_runner(argv, cwd, stdin_text, out, err)

    run_job(job / "spec.json", exec_runner=runner, clock=env.clock, heartbeat=False)


def test_transient_nonzero_exit_never_escalates_or_counts(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started")])
    add_run(env.store, "p1", sheet(model_ladder={"claude_code": ["sonnet", "opus"]}))
    S.tick(env.store, T0, env.dispatcher, usage_reader=OK_USAGE)
    _finish(env, "A", exec_runner=lambda *a: 1)
    res = S.tick(env.store, T0 + 30, env.dispatcher, usage_reader=OK_USAGE)
    assert res["failed"] == [] and res["transient"][0]["id"] == "A"
    n = env.store.get_node("p1", "A")
    assert (n["model"], n["effort"], n["attempts"], n["status"]) == ("sonnet", "high", 0, "not_started")
    assert n["evidence"].startswith("attempt 1: not completed")
    assert not any(e["kind"] == "escalate" for e in events(env.store))
    # The node waits out its retry delay and the pool is held.
    assert S.tick(env.store, T0 + 60, env.dispatcher, usage_reader=OK_USAGE)["started"] == []
    later = T0 + 30 + failure.TRANSIENT_POOL_HOLD_SECONDS + 1
    assert S.tick(env.store, later, env.dispatcher, usage_reader=OK_USAGE)["started"] == ["A"]
    assert env.store.get_node("p1", "A")["attempts"] == 1


def test_transient_usage_limit_text_holds_pool_until_reset(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started"), ("B", [], "not_started")])
    add_run(env.store, "p1", sheet(max_concurrency=1))
    u = usage(resets=T0 + 4 * 3600)
    S.tick(env.store, T0, env.dispatcher, usage_reader=u)
    _finish(env, "A", exec_runner=lambda *a: 0, err_text="Claude AI usage limit reached|1800000000")
    res = S.tick(env.store, T0 + 30, env.dispatcher, usage_reader=u)
    assert res["transient"][0]["reason"] == "a usage limit was reached"
    assert res["started"] == []  # B is not started either: the pool is held
    assert S.pool_hold(env.store, "claude_code")["until"] == T0 + 4 * 3600
    assert S.tick(env.store, T0 + 3 * 3600, env.dispatcher, usage_reader=u)["started"] == []
    fresh = usage(resets=T0 + 9 * 3600)
    assert len(S.tick(env.store, T0 + 4 * 3600 + 1, env.dispatcher,
                      usage_reader=fresh)["started"]) == 1


def test_transient_claude_json_error_is_not_a_completed_attempt(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started")])
    add_run(env.store, "p1", sheet())
    S.tick(env.store, T0, env.dispatcher, usage_reader=OK_USAGE)

    def out_error(argv, cwd, stdin_text, out, err):
        Path(out).write_text(json.dumps({"is_error": True, "result": "Usage limit reached"}),
                             encoding="utf-8")
        return 0

    _finish(env, "A", exec_runner=out_error)
    res = S.tick(env.store, T0 + 30, env.dispatcher, usage_reader=OK_USAGE)
    assert res["transient"][0]["reason"] == "a usage limit was reached" and res["done"] == []


def test_transient_streak_reports_after_max(tmp_path, monkeypatch):
    monkeypatch.setattr(failure, "MAX_TRANSIENT", 2)
    env = make_env(tmp_path, [("A", [], "not_started")])
    add_run(env.store, "p1", sheet())
    t = T0
    for _ in range(2):
        S.tick(env.store, t, env.dispatcher, usage_reader=OK_USAGE)
        _finish(env, "A", exec_runner=lambda *a: 2)
        S.tick(env.store, t + 30, env.dispatcher, usage_reader=OK_USAGE, asker=env.asker)
        t += 3600
    assert statuses(env.store)["A"] == "needs_you"
    assert env.asked[-1]["purpose"] == "failure_choice"


def test_finished_job_goes_through_real_check_to_done_and_run_finishes(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started")])
    run_id = add_run(env.store, "p1", sheet(power_off=True))
    S.tick(env.store, T0, env.dispatcher, usage_reader=OK_USAGE)
    _finish(env, "A")
    env.alive.clear()  # the wrapper has exited
    calls = []
    res = S.tick(env.store, T0 + 30, env.dispatcher, usage_reader=OK_USAGE,
                 power_runner=lambda argv: calls.append(argv) or 0)
    assert res["done"] == ["A"] and res["finished_runs"] == [run_id]
    n = env.store.get_node("p1", "A")
    assert n["status"] == "done" and n["actor"] == "check:command"
    assert calls == [["shutdown", "/s", "/t", "600", "/c", "Control Centre run finished"]]
    assert env.store.get_run(run_id)["status"] == "done"
    assert S.tick(env.store, T0 + 60, env.dispatcher, usage_reader=OK_USAGE,
                  power_runner=lambda argv: calls.append(argv) or 0)["finished_runs"] == []
    assert len(calls) == 1


def test_failed_check_escalates_and_retries_in_same_tick(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started", {"check": {"type": "command", "run": "exit 3"}})])
    add_run(env.store, "p1", sheet(effort="high", max_concurrency=1,
                                   model_ladder={"claude_code": ["sonnet", "opus"]}))
    S.tick(env.store, T0, env.dispatcher, usage_reader=OK_USAGE)
    _finish(env, "A")  # completed attempt (exit 0) whose check fails
    goal_before = env.store.get_node("p1", "A")["goal_hash"]
    res = S.tick(env.store, T0 + 30, env.dispatcher, usage_reader=OK_USAGE)
    assert res["failed"] == [{"id": "A", "action": "escalate"}]
    assert res["started"] == ["A"]
    n = env.store.get_node("p1", "A")
    assert (n["model"], n["effort"], n["attempts"]) == ("sonnet", "xhigh", 2)
    assert n["goal_hash"] == goal_before
    esc = [e for e in events(env.store) if e["kind"] == "escalate"][0]
    data = esc.get("data") or json.loads(esc.get("data_json") or "{}")
    assert data["attempt"] == 1


def test_human_check_waits_for_owner(tmp_path):
    env = make_env(tmp_path, [("A", [], "submitted",
                               {"check": {"type": "human", "prompt": "Owner has seen it"}})])
    add_run(env.store, "p1", sheet())
    S.tick(env.store, T0, env.dispatcher, usage_reader=OK_USAGE, asker=env.asker,
           checker=lambda *a: pytest.fail("human checks are never run automatically"))
    assert statuses(env.store)["A"] == "needs_you"
    assert env.asked[-1]["purpose"] == "confirm" and env.asked[-1]["kind"] == "text"


def test_gemini_node_becomes_needs_you_without_launch(tmp_path):
    env = make_env(tmp_path, [("G", [], "not_started", {"pool": "gemini_app", "model": None}),
                              ("A", [], "not_started")])
    add_run(env.store, "p1", sheet(["claude_code", "gemini_app"], max_concurrency=1))
    res = S.tick(env.store, T0, env.dispatcher, usage_reader=OK_USAGE)
    assert res["needs_you"] == ["G"] and res["started"] == ["A"]
    assert [s["route"] for s in env.launched] == ["claude_code"]
    assert env.asked and env.asked[0]["node_id"] == "G"


# ---------------------------------------------------------------- cancel

def test_cancelled_run_is_ignored(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started")])
    add_run(env.store, "p1", sheet(), status="cancelled")
    assert S.tick(env.store, T0, env.dispatcher, usage_reader=OK_USAGE)["started"] == []


def test_cancel_run_kills_live_jobs(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started"), ("B", [], "not_started")])
    run_id = add_run(env.store, "p1", sheet(max_concurrency=2))
    S.tick(env.store, T0, env.dispatcher, usage_reader=OK_USAGE)
    stopped = S.cancel_run(env.store, run_id, env.dispatcher, now=T0 + 10)
    assert sorted(stopped) == ["A", "B"] and sorted(env.killed) == [1001, 1002]
    assert statuses(env.store) == {"A": "not_started", "B": "not_started"}
    assert env.store.get_run(run_id)["status"] == "cancelled"


def test_tick_kills_jobs_of_a_run_cancelled_elsewhere(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started")])
    run_id = add_run(env.store, "p1", sheet())
    S.tick(env.store, T0, env.dispatcher, usage_reader=OK_USAGE)
    env.store.update_run(run_id, status="cancelled")  # what the api route does
    res = S.tick(env.store, T0 + 10, env.dispatcher, usage_reader=OK_USAGE)
    assert res["stopped"] == ["A"] and env.killed == [1001]
    assert statuses(env.store)["A"] == "not_started"
    assert S.tick(env.store, T0 + 20, env.dispatcher, usage_reader=OK_USAGE)["stopped"] == []


# ---------------------------------------------------------------- misc

def test_invalid_settings_reported_once(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started")])
    add_run(env.store, "p1", {"preset": "yolo", "pools": ["claude_code"]})
    for t in (T0, T0 + 1):
        res = S.tick(env.store, t, env.dispatcher, usage_reader=OK_USAGE)
        assert res["started"] == [] and "invalid settings" in res["blocked"][0]["reason"]
    assert sum("settings are invalid" in e["summary"] for e in events(env.store)) == 1


def test_sprint_start_helper():
    assert S.sprint_start_for_reset("2026-11-04T15:00:00Z") == S.to_epoch("2026-11-04T15:01:00Z")


def test_main_cli_runs_one_tick(tmp_path, monkeypatch, capsys):
    env = make_env(tmp_path, [("A", [], "done")])
    add_run(env.store, "p1", sheet())
    env.store.close()
    monkeypatch.setattr(D, "default_launcher", lambda p: pytest.fail("no launch expected"))
    monkeypatch.setattr(S, "default_usage_reader", NO_USAGE)
    rc = S.main(["tick", "--db", str(env.db), "--data-dir", str(tmp_path / "data")])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["lease"] == "acquired" and out["finished_runs"] == ["r1"]


def test_real_needs_asker_for_gemini_and_report(tmp_path):
    from cc_engine import needs
    env = make_env(tmp_path, [("G", [], "not_started", {"pool": "gemini_app", "model": None}),
                              ("A", [], "error", {"effort": "max"})])
    asker = S.make_asker(env.store)
    disp = env.new_dispatcher(asker=asker)
    add_run(env.store, "p1", sheet(["gemini_app"]))
    S.tick(env.store, T0, disp, usage_reader=NO_USAGE, asker=asker)
    out = failure.on_check_failed(env.store, "p1", env.store.get_node("p1", "A"),
                                  {"model_ladder": {}, "effort": "max"}, "boom", asker=asker)
    assert out["action"] == "report"
    qs = needs.list_questions(env.store, open_only=True)
    by_node = {q["node_id"]: q for q in qs}
    assert by_node["G"]["kind"] == "text" and "Gemini App" in by_node["G"]["prompt"]
    assert by_node["A"]["kind"] == "select"
    if "purpose" in by_node["G"]:
        assert by_node["G"]["purpose"] == "manual_result"
        assert by_node["A"]["purpose"] == "failure_choice"
    assert all(set(o) <= {"value", "label", "description"} for o in by_node["A"]["options"])
    assert env.launched == []


def test_python_m_cc_engine_tick_end_to_end(tmp_path):
    import os
    import subprocess
    import sys
    env = make_env(tmp_path, [("A", [], "done")])
    add_run(env.store, "p1", sheet())
    env.store.close()
    engine = Path(__file__).resolve().parents[1]
    proc = subprocess.run([sys.executable, "-m", "cc_engine", "tick"], cwd=str(engine),
                          env=dict(os.environ, CC_DB=str(env.db)), capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["finished_runs"] == ["r1"]
