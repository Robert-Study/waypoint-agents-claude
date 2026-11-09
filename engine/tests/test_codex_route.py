"""G2 done-check: Codex is a dispatch route (``codex exec --json``) when Owner switches it on.

Run: ``python -m pytest engine/tests -k codex_route -q``. Codex is never called: the launcher,
usage and processes are fakes; tmp_path databases only.
"""
import ast
import json
import sys
from pathlib import Path

import pytest

from cc_engine import dispatch as D
from cc_engine import runsheet
from cc_engine import scheduler as S
from cc_engine import toggles as T
from cc_engine.dispatch import Dispatcher

from test_scheduler import T0, add_run, make_env, statuses, usage

ENGINE = Path(__file__).resolve().parents[1] / "cc_engine"


def codex_usage(remaining=60, status="FRESH", week=70):
    return usage(extra=[
        {"pool": "codex_5h", "label": "Codex 5 hours", "remaining_percent": remaining, "status": status,
         "resets_at": S.iso(T0 + 3600)},
        {"pool": "codex_week", "label": "Codex week", "remaining_percent": week, "status": status,
         "resets_at": S.iso(T0 + 5 * 86400)}])


def node(**kw):
    d = {"id": "N1", "title": "Title", "goal": "Goal.", "check": {"type": "command", "run": "exit 0"},
         "deps": [], "pool": "codex", "model": "gpt-6.1-sol", "effort": "high", "attempts": 1}
    d.update(kw)
    return d


@pytest.fixture
def disp(tmp_path):
    launched = []

    def launcher(spec_path):
        launched.append(json.loads(Path(spec_path).read_text(encoding="utf-8")))
        return {"pid": 4242}

    d = Dispatcher(data_dir=tmp_path, launcher=launcher, clock=lambda: 1000.0, sleep=lambda s: None,
                   codex_exe="codex", pid_alive=lambda pid, started_at=None: True)
    d.launched = launched
    return d


def test_codex_route_pool_maps_to_codex(disp):
    assert D.route_for_pool("codex") == "codex"
    assert disp.route(node()) == "codex"
    assert "codex" in D.ROUTES and "codex" in D.AUTOMATED_ROUTES
    assert not hasattr(D, "NEVER_ROUTED")


def test_codex_route_command_exact(disp):
    argv = disp.codex_argv(node(), {}, cwd="C:/work")
    assert argv == ["codex", "exec", "--json", "--skip-git-repo-check", "--sandbox", "workspace-write",
                    "-C", "C:/work", "-m", "gpt-6.1-sol", "-c", "model_reasoning_effort=high", "-"]
    plain = disp.codex_argv(node(model=None, effort=None), {}, cwd=None)
    assert plain == ["codex", "exec", "--json", "--skip-git-repo-check", "--sandbox", "workspace-write", "-"]


def test_codex_route_refused_while_switched_off(disp, tmp_path):
    out = disp.dispatch(node(), {}, {"id": "p1", "cwd": "C:/work"})  # Codex is off by default
    assert out["status"] == "refused" and "Codex" in out["reason"] and disp.launched == []


def test_codex_route_job_record_like_claude(disp, tmp_path):
    T.set_enabled("codex", True, actor="owner", db=tmp_path / "cc.sqlite")
    out = disp.dispatch(node(), {}, {"id": "p1", "cwd": "C:/work"})
    assert out["status"] == "running" and out["route"] == "codex"
    spec = disp.launched[0]
    assert spec["kind"] == "exec" and spec["route"] == "codex" and spec["cwd"] == "C:/work"
    assert spec["argv"][:3] == ["codex", "exec", "--json"] and spec["argv"][-1] == "-"
    assert Path(spec["stdin_file"]).read_text(encoding="utf-8").startswith("# Task N1")
    assert "CODEX_HOME" in spec["env_drop"] and "OPENAI_API_KEY" in spec["env_drop"]
    job = Path(out["job"])
    assert (job / "launch.json").exists() and (job / "spec.json").exists()
    assert json.loads((job / "launch.json").read_text(encoding="utf-8"))["route"] == "codex"


def test_codex_route_run_job_drops_codex_home(tmp_path, monkeypatch):
    job = tmp_path / "job"
    job.mkdir()
    (job / "prompt.md").write_text("do it", encoding="utf-8")
    spec = {"kind": "exec", "route": "codex", "argv": ["codex", "exec", "-"], "cwd": None,
            "stdin_file": str(job / "prompt.md"), "env_drop": ["CODEX_HOME", "OPENAI_API_KEY"]}
    (job / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", "C:/elsewhere")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    seen = {}

    def runner(argv, cwd, stdin_text, out, err):
        import os
        seen["env"] = {k: os.environ.get(k) for k in ("CODEX_HOME", "OPENAI_API_KEY")}
        seen["stdin"] = stdin_text
        return 0

    res = D.run_job(job / "spec.json", exec_runner=runner, heartbeat=False)
    assert res["exit_code"] == 0 and seen["stdin"] == "do it"
    assert seen["env"] == {"CODEX_HOME": None, "OPENAI_API_KEY": None}


def test_codex_route_runsheet_offers_codex(tmp_path):
    assert "codex" in runsheet.RUN_POOLS
    on = T.Snapshot({"codex": {"enabled": True, "updated_at": None, "updated_by": "owner"}})
    out = runsheet.expand({"preset": "balanced", "pools": ["claude_code", "codex"],
                           "efforts": {"codex": "xhigh"}}, toggles=on)
    assert out["model_ladder"]["codex"] and out["effort"]["codex"] == "xhigh"


def test_codex_route_scheduler_dispatches_codex_when_enabled(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started", {"pool": "codex", "model": None, "effort": None})])
    T.set_enabled("codex", True, actor="owner", db=env.store)
    add_run(env.store, "p1", {"preset": "balanced", "pools": ["codex"], "efforts": {"codex": "high"}})
    res = S.tick(env.store, T0, env.dispatcher, usage_reader=codex_usage())
    assert res["started"] == ["A"]
    spec = env.launched[0]
    assert spec["route"] == "codex" and spec["argv"][1:3] == ["exec", "--json"]
    assert "model_reasoning_effort=high" in spec["argv"]
    assert spec["cwd"] == str(tmp_path)


@pytest.mark.parametrize("rows,reason", [
    (lambda: codex_usage(remaining=5), "reserve"),       # under the reserve floor
    (lambda: codex_usage(status="STALE"), "stale"),       # not fresh: held, like Claude
    (lambda: usage(), "unknown"),                         # no Codex reading at all
])
def test_codex_route_fresh_usage_floor(tmp_path, rows, reason):
    env = make_env(tmp_path, [("A", [], "not_started", {"pool": "codex"})])
    T.set_enabled("codex", True, actor="owner", db=env.store)
    add_run(env.store, "p1", {"preset": "balanced", "pools": ["codex"]})
    res = S.tick(env.store, T0, env.dispatcher, usage_reader=rows())
    assert res["started"] == [] and env.launched == []
    assert statuses(env.store)["A"] == "not_started"
    assert S.USAGE_POOLS["codex"] == ("codex_5h", "codex_week")
    notes = " ".join(e["summary"] for e in env.store.events(limit=1000)).lower()
    assert reason in notes


def test_codex_route_codex_sprint_dispatches_like_claude(tmp_path, monkeypatch):
    from cc_engine import sprints as SP
    from test_autonomous import App, call
    monkeypatch.setitem(sys.modules, "cc_engine.models", None)
    env = make_env(tmp_path, [("A", [], "not_started", {"pool": None, "model": None})])
    T.set_enabled("codex", True, actor="owner", db=env.store)
    app = App(env.store, usage_rows=[{"pool": "codex_5h", "label": "Codex 5 hours", "remaining_percent": 60,
                                      "status": "FRESH", "resets_at": S.iso(T0 + 7200)}])
    models = call(app, "GET /api/autonomous")["models"]
    codex = [m for m in models if m["route"] == "codex"]
    assert codex and codex[0]["runnable"]
    call(app, "POST /api/autonomous/sprint", {"kind": "codex", "confirm": True})
    cfg = {"preset": "balanced", "models": [codex[0]["id"]], "efforts": {"claude": "medium", "codex": "high"},
           "punch_clock": True, "max_use": {}, "auto_wake": True, "power_off": False, "merge_resets": True,
           "sequence": [[{"project": "p1", "model": None}]]}
    call(app, "POST /api/autonomous/run", {"settings": cfg})
    res = S.tick(env.store, T0, env.dispatcher, usage_reader=codex_usage())
    assert res["started"] == ["A"] and env.launched[0]["route"] == "codex"
    tl = call(app, "GET /api/sprints/timeline", q={"hours": "24"})
    sp = [s for s in tl["sprints"] if s["pool"] == "codex"]
    assert sp and "yourself" not in sp[0]["summary"]


def test_codex_route_never_rule_is_gone_from_the_engine():
    for name in ("dispatch.py", "runsheet.py", "scheduler.py", "sprints.py"):
        text = (ENGINE / name).read_text(encoding="utf-8").lower()
        assert "codex is never a dispatch route" not in text, name
        assert "codex is never a pool" not in text, name
        assert "never_routed" not in text, name
        ast.parse(text)
