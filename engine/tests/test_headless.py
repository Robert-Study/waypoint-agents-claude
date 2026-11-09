"""Headless workers and the worker context. No real claude or codex ever runs: a fake process prints canned lines."""
import json
import threading

import pytest

from cc_engine import claims, headless, plan
from cc_engine import toggles as T

from e2e_helpers import Engine, node, plan_dict

CLAUDE_LINES = [
    {"type": "system", "subtype": "init", "session_id": "sess-1", "model": "model-x"},
    {"type": "assistant", "message": {"id": "m1", "model": "model-x", "content": [{"type": "tool_use", "name": "Edit"}],
                                      "usage": {"input_tokens": 10, "output_tokens": 5,
                                                "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20}}},
    {"type": "assistant", "message": {"id": "m1", "model": "model-x", "content": [{"type": "text", "text": "Writing the file.\nMore."}],
                                      "usage": {"input_tokens": 10, "output_tokens": 5,
                                                "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20}}},
    {"type": "assistant", "message": {"id": "m2", "content": [{"type": "text", "text": "All done here."}],
                                      "usage": {"input_tokens": 12, "output_tokens": 7,
                                                "cache_read_input_tokens": 130, "cache_creation_input_tokens": 0}}},
    {"type": "result", "result": "Finished.", "total_cost_usd": 0.5},
]


class FakeProc:
    def __init__(self, lines, code=0):
        self.stdout = iter([json.dumps(x) + "\n" if isinstance(x, dict) else x + "\n" for x in lines])
        self.code = code

    def wait(self):
        return self.code


class FakeRunner:
    def __init__(self, lines=CLAUDE_LINES, code=0, fail=None):
        self.lines, self.code, self.fail, self.calls = lines, code, fail, []

    def __call__(self, argv, cwd, stdin_text, env):
        self.calls.append({"argv": argv, "cwd": cwd, "stdin": stdin_text, "env": env})
        if self.fail:
            raise OSError(self.fail)
        return FakeProc(self.lines, self.code)


@pytest.fixture
def eng(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    e = Engine(tmp_path / "engine")
    e.work = work
    p = plan_dict("hp", "Headless plan", [node("A", files=["src/**"], model="model-x"), node("B", deps=["A"])])
    p["cwd"] = str(work)
    status, d = e.post("/api/plan/import", {"plan": p, "revision": e.revision()}, "owner")
    assert status == 200, d
    yield e
    e.close()


def wait_thread(agent_id):
    for t in threading.enumerate():
        if t.name == f"waypoint-{agent_id}":
            t.join(10)
            assert not t.is_alive()


def agent(eng, agent_id):
    rows = eng.store._q("SELECT * FROM agents WHERE id=?", (agent_id,))
    return dict(rows[0]) if rows else None


# ---------------------------------------------------------------- context route
def test_context_by_session_and_by_cwd_and_none(eng, tmp_path):
    claims.claim(eng.store, "hp", "A", "session-abc", cwd=str(eng.work))
    status, d = eng.get("/api/worker/context?session=session-abc")
    assert status == 200 and d["claimed_by"] == "session-abc"
    assert d["node"] == {"project": "hp", "id": "A", "title": "Task A", "goal": "Goal of A.",
                         "check": {"type": "command", "run": "exit 0"}, "files": ["src/**"]}
    assert d["receipt_path"].replace("\\", "/") == str(eng.work).replace("\\", "/") + "/.waypoint/receipts/hp.A.json"
    # by cwd only (a different session id)
    status, d = eng.get("/api/worker/context?session=other&cwd=" + str(eng.work).replace("\\", "/").upper().replace("/", "\\"))
    assert d["node"]["id"] == "A" and d["claimed_by"] == "session-abc"
    assert d["receipt_path"].startswith(str(eng.work).replace("/", "\\").upper()) or d["receipt_path"].lower().startswith(str(eng.work).lower())
    # none
    status, d = eng.get("/api/worker/context?session=nobody&cwd=" + str(tmp_path / "elsewhere"))
    assert status == 200 and d == {"node": None, "receipt_path": None, "claimed_by": None}


def test_context_ignores_released_claims(eng):
    claims.claim(eng.store, "hp", "A", "session-abc", cwd=str(eng.work))
    claims.release(eng.store, "hp", "A", by="session-abc")
    assert eng.get("/api/worker/context?session=session-abc")[1]["node"] is None


# ---------------------------------------------------------------- dispatch
def test_dispatch_runs_fake_claude_and_feeds_agent_row(eng):
    runner = FakeRunner()
    eng.server.app.headless_runner = runner
    status, d = eng.post("/api/dispatch/headless", {"project": "hp", "node": "A", "engine": "claude", "parent": "sess-parent"}, "owner")
    assert status == 200 and d["ok"] and d["id"].startswith("headless-"), d
    wait_thread(d["id"])
    call = runner.calls[0]
    assert call["argv"][1:6] == ["-p", "--output-format", "stream-json", "--verbose", "--model"] and "model-x" in call["argv"]
    assert call["cwd"] == str(eng.work) and "Goal of A." in call["stdin"]
    assert "When you finish, write your receipt to " in call["stdin"] and "hp.A.json" in call["stdin"]
    assert "waypoint.receipt.v1" in call["stdin"]
    row = agent(eng, d["id"])
    assert row["kind"] == "headless" and row["status"] == "done" and row["parent"] == "sess-parent"
    assert row["title"] == "hp/A" and row["project"] == "hp" and row["node"] == "A"
    assert (row["tokens_in"], row["tokens_out"], row["cache_read"]) == (10 + 20 + 12, 12, 230)  # m1 counted once
    assert row["context"] == 142 and row["last_line"] == "Finished." and row["cost_eq"] == 0.5
    assert row["session_id"] == "sess-1" and row["transcript"].endswith("sess-1.jsonl")
    # No receipt: the claim (taken as headless-<id>) is released so the task is free again.
    assert eng.store.claim_row("hp", "A") is None and eng.store.get_node("hp", "A")["status"] == "not_started"
    ev = [e for e in eng.store.events("hp") if e["kind"] == "headless"]
    assert ev and "without writing a receipt" in ev[-1]["summary"] and "released" in ev[-1]["summary"]


def test_receipt_written_means_no_note(eng):
    def runner(argv, cwd, stdin, env):
        path = headless.receipt_path(cwd, "hp", "A")
        import os
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").write("{}")
        return FakeProc(CLAUDE_LINES)
    eng.server.app.headless_runner = runner
    status, d = eng.post("/api/dispatch/headless", {"project": "hp", "node": "A", "engine": "claude"}, "owner")
    wait_thread(d["id"])
    assert agent(eng, d["id"])["status"] == "done"
    assert not [e for e in eng.store.events("hp") if e["kind"] == "headless"]


def test_nonzero_exit_marks_error(eng):
    eng.server.app.headless_runner = FakeRunner(code=2)
    status, d = eng.post("/api/dispatch/headless", {"project": "hp", "node": "A", "engine": "claude"}, "owner")
    wait_thread(d["id"])
    assert agent(eng, d["id"])["status"] == "error"


def test_codex_stream_and_argv(eng):
    T.set_enabled("codex", True, db=eng.store)
    lines = [{"type": "item.started", "item": {"type": "command_execution"}},
             {"type": "item.completed", "item": {"type": "agent_message", "text": "Codex says hi."}},
             {"type": "turn.completed", "usage": {"input_tokens": 50, "output_tokens": 9, "cached_input_tokens": 30}}]
    runner = FakeRunner(lines)
    eng.server.app.headless_runner = runner
    eng.server.app.usage_reader = lambda: [{"pool": p, "label": p, "remaining_percent": 90, "status": "FRESH",
                                            "resets_at": None} for p in ("codex_5h", "codex_week")]
    status, d = eng.post("/api/dispatch/headless", {"project": "hp", "node": "A", "engine": "codex"}, "owner")
    assert status == 200, d
    wait_thread(d["id"])
    assert runner.calls[0]["argv"][1:3] == ["exec", "--json"] and runner.calls[0]["argv"][-1] == "-"
    row = agent(eng, d["id"])
    assert (row["tokens_in"], row["tokens_out"], row["cache_read"]) == (50, 9, 30) and row["last_line"] == "Codex says hi."


def test_dispatch_refused_when_switched_off(eng):
    runner = FakeRunner()
    eng.server.app.headless_runner = runner
    T.set_enabled("claude_code", False, db=eng.store)
    status, d = eng.post("/api/dispatch/headless", {"project": "hp", "node": "A", "engine": "claude"}, "owner")
    assert status == 409 and d["error"] == "refused" and d["detail"] and not runner.calls
    assert eng.store.get_node("hp", "A")["status"] == "not_started"  # nothing was claimed
    # Codex is off by default
    status, d = eng.post("/api/dispatch/headless", {"project": "hp", "node": "A", "engine": "codex"}, "owner")
    assert status == 409 and not runner.calls


def test_dispatch_refused_below_floor(eng):
    T.set_enabled("codex", True, db=eng.store)
    rows = [{"pool": p, "label": p, "remaining_percent": 10, "status": "FRESH", "resets_at": None}
            for p in ("codex_5h", "codex_week")]
    eng.server.app.usage_reader = lambda: rows
    runner = FakeRunner()
    eng.server.app.headless_runner = runner
    status, d = eng.post("/api/dispatch/headless", {"project": "hp", "node": "A", "engine": "codex"}, "owner")
    assert status == 409 and "low" in d["detail"] and "20%" in d["detail"] and not runner.calls
    for r in rows:
        r["remaining_percent"] = 60
    status, d = eng.post("/api/dispatch/headless", {"project": "hp", "node": "A", "engine": "codex"}, "owner")
    assert status == 200, d
    wait_thread(d["id"])


def test_owner_only_and_bad_requests(eng):
    eng.server.app.headless_runner = FakeRunner()
    body = {"project": "hp", "node": "A", "engine": "claude"}
    status, d = eng.post("/api/dispatch/headless", body, "router")
    assert status == 403 and d["error"] == "owner_only"
    assert eng.post("/api/dispatch/headless", body, "none")[0] == 403
    assert eng.post("/api/dispatch/headless", dict(body, engine="gemini"), "owner")[0] == 400
    assert eng.post("/api/dispatch/headless", dict(body, node="Z"), "owner")[0] == 404
    assert eng.store.get_node("hp", "A")["status"] == "not_started"


def test_second_dispatch_on_a_claimed_node_conflicts(eng):
    eng.server.app.headless_runner = FakeRunner()
    body = {"project": "hp", "node": "A", "engine": "claude"}
    from cc_engine import claims
    claims.claim(eng.store, "hp", "A", by="headless-live")  # a worker still running holds the node
    assert eng.post("/api/dispatch/headless", body, "owner")[0] == 409


def test_start_failure_releases_claim(eng):
    eng.server.app.headless_runner = FakeRunner(fail="no such program")
    status, d = eng.post("/api/dispatch/headless", {"project": "hp", "node": "A", "engine": "claude"}, "owner")
    assert status == 409 and "could not start" in d["detail"]
    assert eng.store.get_node("hp", "A")["status"] == "not_started"
    assert eng.store.claim_row("hp", "A") is None


def test_stream_state_tracks_tool_and_ignores_noise():
    st = headless.StreamState("claude")
    assert st.feed("not json") is False
    assert st.feed(json.dumps(CLAUDE_LINES[1])) is True and st.tool == "Edit"
    assert st.tokens() == (30, 5, 100)


# ---------------------------------------------------------------- review fixes (M1, M4, M7, M9, M10)
import os
import time


class BlockProc:
    """A worker that hangs until it is killed."""
    def __init__(self):
        self.dead = threading.Event()
        self.killed = 0
        self.stdout = self._lines()

    def _lines(self):
        self.dead.wait(10)
        return
        yield

    def wait(self):
        self.dead.wait(10)
        return 1

    def kill(self):
        self.killed += 1
        self.dead.set()


def _join(th):
    th.join(10)
    assert not th.is_alive()


def test_m1_unknown_or_stale_usage_refuses_codex_but_not_claude(eng):
    T.set_enabled("codex", True, db=eng.store)
    runner = FakeRunner()
    eng.server.app.headless_runner = runner
    eng.server.app.usage_reader = lambda: []
    status, d = eng.post("/api/dispatch/headless", {"project": "hp", "node": "A", "engine": "codex"}, "owner")
    assert status == 409 and "usage" in d["detail"] and not runner.calls
    stale = [{"pool": p, "label": p, "remaining_percent": 90, "status": "STALE", "resets_at": None}
             for p in ("codex_5h", "codex_week")]
    eng.server.app.usage_reader = lambda: stale
    assert eng.post("/api/dispatch/headless", {"project": "hp", "node": "A", "engine": "codex"}, "owner")[0] == 409
    assert eng.store.claim_row("hp", "A") is None
    # Claude has floor 0: an unknown reading may run.
    status, d = eng.post("/api/dispatch/headless", {"project": "hp", "node": "A", "engine": "claude"}, "owner")
    assert status == 200, d
    wait_thread(d["id"])


def test_m4_old_receipt_does_not_keep_the_claim(eng):
    old = headless.receipt_path(eng.work, "hp", "A")
    os.makedirs(os.path.dirname(old), exist_ok=True)
    open(old, "w").write("{}")
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    eng.server.app.headless_runner = FakeRunner(code=1)
    status, d = eng.post("/api/dispatch/headless", {"project": "hp", "node": "A", "engine": "claude"}, "owner")
    assert status == 200, d
    wait_thread(d["id"])
    assert eng.store.claim_row("hp", "A") is None
    assert [e for e in eng.store.events("hp") if e["kind"] == "headless"]


def test_m7_env_and_context_scoping(eng):
    runner = FakeRunner()
    eng.server.app.headless_runner = runner
    status, d = eng.post("/api/dispatch/headless", {"project": "hp", "node": "A", "engine": "claude"}, "owner")
    wait_thread(d["id"])
    env = runner.calls[0]["env"]
    assert env["WAYPOINT_NODE"] == "hp/A" and env["WAYPOINT_CLAIM_BY"] == d["id"]
    # a headless claim at the project folder is not attached to other sessions by cwd
    claims.claim(eng.store, "hp", "A", "headless-xyz", cwd=str(eng.work))
    assert eng.get("/api/worker/context?session=me&cwd=" + str(eng.work))[1]["node"] is None
    # but the worker itself finds it with node + by
    got = eng.get("/api/worker/context?session=s&node=hp/A&by=headless-xyz")[1]
    assert got["node"]["id"] == "A" and got["claimed_by"] == "headless-xyz"
    assert eng.get("/api/worker/context?session=s&node=hp/A&by=someone-else")[1]["node"] is None
    assert eng.get("/api/worker/context?node=hp/A&session=headless-xyz")[1]["node"]["id"] == "A"
    assert eng.get("/api/worker/context?node=hp/Z&by=headless-xyz")[1]["node"] is None


def test_m7_cwd_match_needs_exactly_one_claim(eng):
    claims.claim(eng.store, "hp", "A", "s1", cwd=str(eng.work))
    one = eng.get("/api/worker/context?session=me&cwd=" + str(eng.work))[1]
    assert one["node"]["id"] == "A"
    claims.claim(eng.store, "hp", "B", "s2", cwd=str(eng.work))
    two = eng.get("/api/worker/context?session=me&cwd=" + str(eng.work))[1]
    assert two["node"] is None and two["ambiguous"] is True


def test_m9_model_name_is_validated(eng):
    eng.store.update_node("hp", "A", {"model": "sonnet&calc"}, "owner", "bad model")
    runner = FakeRunner()
    eng.server.app.headless_runner = runner
    status, d = eng.post("/api/dispatch/headless", {"project": "hp", "node": "A", "engine": "claude"}, "owner")
    assert status == 409 and d["detail"] == "model name not allowed" and not runner.calls
    assert eng.store.claim_row("hp", "A") is None
    eng.store.update_node("hp", "A", {"model": "claude-sonnet-4.5[1m]"}, "owner", "ok model")
    status, d = eng.post("/api/dispatch/headless", {"project": "hp", "node": "A", "engine": "claude"}, "owner")
    assert status == 200, d
    wait_thread(d["id"])


def test_m9_cmd_shim_refuses_shell_characters(eng, monkeypatch):
    monkeypatch.setattr(headless, "IS_WINDOWS", True)
    assert headless.cmd_shim_refusal(["C:/x/claude.cmd", "-p", "--model", "sonnet"]) is None
    assert headless.cmd_shim_refusal(["C:/x/claude.exe", "-C", "a&b"]) is None
    for bad in ("a&b", "a|b", "a<b", "a>b", "a^b", "a%b", "a!b", 'a"b'):
        assert headless.cmd_shim_refusal(["C:/x/codex.cmd", "-C", bad])
    T.set_enabled("codex", True, db=eng.store)
    rows = [{"pool": p, "label": p, "remaining_percent": 90, "status": "FRESH", "resets_at": None}
            for p in ("codex_5h", "codex_week")]
    odd = eng.work.parent / "w&calc"
    odd.mkdir()
    p2 = plan_dict("hq", "Odd folder", [node("A")])
    p2["cwd"] = str(odd)
    assert eng.post("/api/plan/import", {"plan": p2, "revision": eng.revision()}, "owner")[0] == 200
    runner = FakeRunner()
    with pytest.raises(Exception) as e:
        headless.launch(eng.store, "hq", "A", "codex", runner=runner, usage_reader=lambda: rows,
                        codex_exe="C:/x/codex.cmd")
    assert not runner.calls and eng.store.claim_row("hq", "A") is None


def test_m10_timeout_kills_and_marks_error(eng):
    proc = BlockProc()
    out = headless.launch(eng.store, "hp", "A", "claude", runner=lambda *a: proc, usage_reader=lambda: [],
                          timeout=0.2, killer=lambda p: p.kill())
    _join(out["thread"])
    row = agent(eng, out["id"])
    assert proc.killed == 1 and row["status"] == "error" and row["last_line"] == "Stopped after the time limit"
    assert eng.store.claim_row("hp", "A") is None
    assert out["id"] not in headless.RUNNING


def test_m10_stop_kills_every_running_worker(eng):
    proc = BlockProc()
    out = headless.launch(eng.store, "hp", "A", "claude", runner=lambda *a: proc, usage_reader=lambda: [],
                          timeout=60, killer=lambda p: p.kill())
    assert out["id"] in headless.RUNNING
    handle = headless.start(eng.server.app)
    handle.stop()
    _join(out["thread"])
    assert proc.killed == 1 and not headless.RUNNING
    assert agent(eng, out["id"])["status"] == "error"
