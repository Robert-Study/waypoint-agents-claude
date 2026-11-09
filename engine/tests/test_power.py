from cc_engine import power
from cc_engine import scheduler as S

from test_scheduler import make_env, add_run, events


class FakeRunner:
    def __init__(self, code=0):
        self.calls = []
        self.code = code

    def __call__(self, argv):
        self.calls.append(list(argv))
        return self.code


def _run(store, run_id):
    return store.get_run(run_id)


def test_power_off_when_all_done_and_setting_on(tmp_path):
    env = make_env(tmp_path, [("A", [], "done"), ("B", ["A"], "done")])
    rid = add_run(env.store, "p1", {"pools": ["claude_code"], "power_off": True}, status="done")
    r = FakeRunner()
    assert power.power_off_if_done(env.store, _run(env.store, rid), r) is True
    assert r.calls == [["shutdown", "/s", "/t", "600", "/c", "Control Centre run finished"]]
    assert "shut down in 10 minutes" in events(env.store)[-1]["summary"]
    # once per run
    assert power.power_off_if_done(env.store, _run(env.store, rid), r) is False
    assert len(r.calls) == 1


def test_no_power_off_when_setting_off(tmp_path):
    env = make_env(tmp_path, [("A", [], "done")])
    rid = add_run(env.store, "p1", {"pools": ["claude_code"], "power_off": False}, status="done")
    r = FakeRunner()
    assert power.power_off_if_done(env.store, _run(env.store, rid), r) is False
    assert r.calls == []


def test_no_power_off_while_work_remains(tmp_path):
    env = make_env(tmp_path, [("A", [], "done"), ("B", ["A"], "needs_you")])
    rid = add_run(env.store, "p1", {"pools": ["claude_code"], "power_off": True})
    r = FakeRunner()
    assert power.power_off_if_done(env.store, _run(env.store, rid), r) is False
    assert r.calls == []


def test_failed_shutdown_command_is_reported(tmp_path):
    env = make_env(tmp_path, [("A", [], "done")])
    rid = add_run(env.store, "p1", {"pools": ["claude_code"], "power_off": True}, status="done")
    assert power.power_off_if_done(env.store, _run(env.store, rid), FakeRunner(code=5)) is False
    assert "stays on" in events(env.store)[-1]["summary"]


def test_cancel_uses_shutdown_abort(tmp_path):
    env = make_env(tmp_path, [("A", [], "done")])
    r = FakeRunner()
    assert power.cancel(r, store=env.store, project_id="p1") is True
    assert r.calls == [["shutdown", "/a"]]
    assert events(env.store)[-1]["summary"] == "The planned shutdown was cancelled."


def test_power_module_never_runs_real_shutdown_in_tests(monkeypatch):
    # The default runner is only reached when no runner is injected; guard it here.
    called = []
    monkeypatch.setattr(power, "default_runner", lambda argv: called.append(argv) or 0)
    power.cancel()
    assert called == [["shutdown", "/a"]]


# ---------------------------------------------------------------- all projects

def test_power_all_projects_other_active_run_keeps_pc_on(tmp_path):
    from test_scheduler import seed
    env = make_env(tmp_path, [("A", [], "done")])
    seed(env.store, [("X", [], "not_started")], project_id="p2")
    rid = add_run(env.store, "p1", {"pools": ["claude_code"], "power_off": True}, status="done")
    add_run(env.store, "p2", {"pools": ["claude_code"]}, status="running")
    r = FakeRunner()
    assert power.power_off_if_done(env.store, _run(env.store, rid), r) is False
    assert r.calls == []
    assert "another run is still active" in events(env.store)[-1]["summary"]


def test_power_all_projects_running_node_or_live_job_keeps_pc_on(tmp_path):
    from test_scheduler import seed
    env = make_env(tmp_path, [("A", [], "done")])
    seed(env.store, [("X", [], "running")], project_id="p2")
    rid = add_run(env.store, "p1", {"pools": ["claude_code"], "power_off": True}, status="done")
    r = FakeRunner()
    assert power.power_off_if_done(env.store, _run(env.store, rid), r) is False
    env.store.set_node_status("p2", "X", "done", "owner")

    class Live:
        def live_jobs(self):
            return [{"project": "p3", "node": "Q", "pid": 7}]

    assert power.power_off_if_done(env.store, _run(env.store, rid), r, dispatcher=Live()) is False
    assert r.calls == []
    assert power.power_off_if_done(env.store, _run(env.store, rid), r, dispatcher=env.dispatcher)
    assert r.calls == [list(power.SHUTDOWN_ARGV)]


def test_power_all_projects_pending_power_off_retried_by_tick(tmp_path):
    from test_scheduler import seed, sheet, T0, OK_USAGE
    env = make_env(tmp_path, [("A", [], "done")])
    seed(env.store, [("X", [], "not_started", {"pool": "gemini_app", "model": None})],
         project_id="p2")
    add_run(env.store, "p1", sheet(power_off=True))
    other = add_run(env.store, "p2", sheet(["gemini_app"]))
    r = FakeRunner()
    res = S.tick(env.store, T0, env.dispatcher, usage_reader=OK_USAGE, power_runner=r)
    assert res["finished_runs"] == ["r1"] and r.calls == []  # p2 is still active
    env.store.set_node_status("p2", "X", "done", "owner")
    res = S.tick(env.store, T0 + 60, env.dispatcher, usage_reader=OK_USAGE, power_runner=r)
    assert other in res["finished_runs"] and res["power_off"] == ["r1"]
    assert r.calls == [list(power.SHUTDOWN_ARGV)]
    S.tick(env.store, T0 + 120, env.dispatcher, usage_reader=OK_USAGE, power_runner=r)
    assert len(r.calls) == 1


def test_power_cancel_without_runner_runs_shutdown_abort(monkeypatch):
    called = []
    monkeypatch.setattr(power, "default_runner", lambda argv: called.append(argv) or 0)
    assert power.cancel(runner=None) is True
    assert called == [["shutdown", "/a"]]
