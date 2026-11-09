"""I4 Codex Run is Owner's own click: POST /api/node/run_owner with route codex starts
``codex exec`` for that task only when the browser owner sends it with confirm=true; the router
token, the scheduler and any agent are refused. Fakes only: codex is never really called."""
import http.client
import json
import threading
from pathlib import Path

import pytest

from cc_engine import api, history, owner_runs, plan


ALLOWED = "http://127.0.0.1:3080"
CODEX = r"C:\fake\codex.cmd"


@pytest.fixture
def loaded(store, fixture_plan, tmp_path):
    fixture_plan["cwd"] = str(tmp_path)
    plan.import_plan(store, fixture_plan)
    return "fx"


class FakeRunner:
    def __init__(self, exit_code=None):
        self.calls = []
        self.exit_code = exit_code

    def __call__(self, argv, cwd, stdin_text, out_dir, env, on_exit):
        self.calls.append({"argv": list(argv), "cwd": cwd, "stdin": stdin_text, "out_dir": out_dir, "env": env})
        if self.exit_code is not None:
            on_exit(self.exit_code)
        return {"pid": 4242}


def passing_check(cmd, cwd, timeout):
    return 0, "fine", ""


def failing_check(cmd, cwd, timeout):
    return 1, "", "nope"


def start(store, actor="owner", confirm=True, route="codex", node="A", runner=None, **kw):
    kw.setdefault("codex_exe", CODEX)
    kw.setdefault("usage_rows", [])
    kw.setdefault("disabled", lambda: False)
    return owner_runs.start_owner_run(store, "fx", node, route, actor=actor, confirm=confirm,
                                      runner=runner or FakeRunner(), **kw)


# ---------------------------------------------------------------- module level
def test_codex_owner_run_starts_codex_exec_for_robert(store, loaded, tmp_path):
    runner = FakeRunner()
    out = start(store, runner=runner)
    assert out["ok"] is True and out["route"] == "codex" and out["status"] == "running"
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["argv"][0] == CODEX and call["argv"][1] == "exec"
    assert "--skip-git-repo-check" in call["argv"] and call["argv"][-1] == "-"
    assert call["argv"][call["argv"].index("-C") + 1] == str(tmp_path)
    assert "Lay the root." in call["stdin"]  # the task's own prompt packet
    assert Path(call["out_dir"]).parent.parent == Path(store.path).parent
    for k in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_HOME"):
        assert k not in call["env"]  # Owner's Codex plan only, never a metered key
    node = store.get_node("fx", "A")
    assert node["status"] == "running" and node["actor"] == "owner"
    rows = history.node_history(store, "fx", "A")
    assert [r["kind"] for r in rows] == ["started"] and rows[0]["by"] == "Owner"


@pytest.mark.parametrize("actor", ["router", "scheduler", "failure", "planner", "worker-a", "check:command", None])
def test_codex_owner_run_refuses_everyone_but_robert(store, loaded, actor):
    runner = FakeRunner()
    with pytest.raises(owner_runs.OwnerRunRefused) as e:
        start(store, actor=actor, runner=runner)
    assert e.value.status == 403
    assert runner.calls == [] and store.get_node("fx", "A")["status"] == "not_started"


@pytest.mark.parametrize("confirm", [None, False, "true", 1])
def test_codex_owner_run_needs_confirm_true(store, loaded, confirm):
    runner = FakeRunner()
    with pytest.raises(owner_runs.OwnerRunRefused) as e:
        start(store, confirm=confirm, runner=runner)
    assert e.value.status == 409 and e.value.body["error"] == "confirm"
    assert runner.calls == []


def test_codex_owner_run_only_codex_route(store, loaded):
    for route in ("claude_code", "gemini_app", "freebuff", "", None):
        with pytest.raises(owner_runs.OwnerRunRefused) as e:
            start(store, route=route)
        assert e.value.status == 400


def test_codex_owner_run_respects_toggle_usage_and_state(store, loaded):
    with pytest.raises(owner_runs.OwnerRunRefused) as e:
        start(store, disabled=lambda: True)
    assert e.value.status == 403 and e.value.body["error"] == "disabled"
    low = [{"pool": "codex_5h", "remaining_percent": 2, "status": "FRESH"}]
    with pytest.raises(owner_runs.OwnerRunRefused) as e:
        start(store, usage_rows=low)
    assert e.value.status == 409 and e.value.body["error"] == "usage_floor"
    with pytest.raises(owner_runs.OwnerRunRefused) as e:
        start(store, codex_exe="")  # not found on PATH
    assert e.value.status == 503
    with pytest.raises(owner_runs.OwnerRunRefused) as e:
        start(store, node="NOPE")
    assert e.value.status == 404
    start(store)
    with pytest.raises(owner_runs.OwnerRunRefused) as e:  # already running
        start(store)
    assert e.value.status == 409


def test_codex_owner_run_disabled_switch_uses_toggles(monkeypatch, store, loaded):
    import sys
    import types
    fake = types.ModuleType("cc_engine.toggles")
    state = {"codex": False}
    fake.is_enabled = lambda i: state.get(i, True)
    monkeypatch.setitem(sys.modules, "cc_engine.toggles", fake)
    assert owner_runs.default_disabled() is True
    runner = FakeRunner()
    with pytest.raises(owner_runs.OwnerRunRefused) as e:
        start(store, disabled=owner_runs.default_disabled, runner=runner)
    assert e.value.status == 403 and e.value.body["error"] == "disabled" and runner.calls == []
    state["codex"] = True
    assert owner_runs.default_disabled() is False
    fake.is_enabled = lambda i: 1 / 0  # a broken switch never lets Codex run on a guess
    assert owner_runs.default_disabled() is True


def test_codex_owner_run_disabled_switch_falls_back_to_models(monkeypatch):
    import sys
    from cc_engine import models
    monkeypatch.setitem(sys.modules, "cc_engine.toggles", None)  # toggles.py absent
    monkeypatch.setattr(models, "is_disabled", lambda **kw: False)
    monkeypatch.setattr(models, "load_state", lambda path=None: {"disabled": ["agent:codex"]})
    assert owner_runs.default_disabled() is True
    monkeypatch.setattr(models, "load_state", lambda path=None: {"disabled": ["agent:claude"]})
    assert owner_runs.default_disabled() is False
    monkeypatch.setattr(models, "is_disabled", lambda **kw: kw.get("route") == "codex")
    assert owner_runs.default_disabled() is True


def test_codex_owner_run_exit_runs_the_done_check(store, loaded):
    start(store, runner=FakeRunner(exit_code=0), check_runner=passing_check)
    assert store.get_node("fx", "A")["status"] == "done"
    assert store.get_node("fx", "A")["actor"] == "check:command"


def test_codex_owner_run_failed_exit_or_check_marks_error(store, loaded):
    start(store, runner=FakeRunner(exit_code=0), check_runner=failing_check)
    assert store.get_node("fx", "A")["status"] == "error"
    store.set_node_status("fx", "A", "not_started", "owner")
    calls = []
    start(store, runner=FakeRunner(exit_code=3), check_runner=lambda *a: calls.append(a) or (0, "", ""))
    assert store.get_node("fx", "A")["status"] == "error" and calls == []
    kinds = [r["kind"] for r in history.node_history(store, "fx", "A")]
    assert kinds == ["started", "failed", "started", "failed"]


def test_codex_owner_run_is_not_wired_into_automatic_paths():
    # Owner's click path is never reached by the scheduler, failure handler, sprints or MCP.
    root = Path(owner_runs.__file__).parent
    for name in ("scheduler.py", "dispatch.py", "failure.py", "sprints.py", "mcp_server.py"):
        text = (root / name).read_text(encoding="utf-8")
        assert "owner_runs" not in text and "run_owner" not in text, name


# ---------------------------------------------------------------- HTTP route
@pytest.fixture
def server(store, tmp_path):
    srv = api.make_server(store, host="127.0.0.1", port=0, usage_reader=lambda: [], heartbeat=0.5,
                          poll_interval=0.2, token_path=tmp_path / "engine.token")
    srv.app.owner_runner = FakeRunner()
    srv.app.codex_exe = CODEX
    srv.app.owner_disabled = lambda: False
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def call(srv, body, origin=None, token=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    headers = {"Content-Type": "application/json"}
    if origin:
        headers["Origin"] = origin
    if token:
        headers["X-CC-Token"] = token
    conn.request("POST", "/api/node/run_owner", body=json.dumps(body).encode(), headers=headers)
    resp = conn.getresponse()
    payload = resp.read()
    conn.close()
    return resp.status, (json.loads(payload) if payload else None)


def test_codex_owner_run_route(server, store, loaded):
    assert "POST /api/node/run_owner" in owner_runs.ROUTES
    body = {"project": "fx", "id": "A", "route": "codex", "confirm": True}
    tok = server.app.token
    assert call(server, body)[0] == 403  # no credential
    assert call(server, body, token=tok)[0] == 403  # the router token
    assert call(server, body, origin=ALLOWED, token=tok)[0] == 403  # a router posing as the page
    assert call(server, body, origin="http://evil.test")[0] == 403
    status, out = call(server, dict(body, confirm=False), origin=ALLOWED)
    assert status == 409 and out["error"] == "confirm"
    assert server.app.owner_runner.calls == []
    status, out = call(server, dict(body, route="claude_code"), origin=ALLOWED)
    assert status == 400
    status, out = call(server, body, origin=ALLOWED)
    assert status == 200 and out["ok"] is True and out["run_id"]
    assert len(server.app.owner_runner.calls) == 1
    assert store.get_node("fx", "A")["status"] == "running"
