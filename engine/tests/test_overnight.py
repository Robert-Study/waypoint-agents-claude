"""Unattended (overnight) runs: resume from partial work, across the 5-hour reset, per-pool floors,
retired-pool safety, receipts and needs-you tasks as human to-dos (td-fde6a7)."""
import json
from pathlib import Path

from cc_engine import dispatch as D
from cc_engine import overnight, retired_ids
from cc_engine import scheduler as S
from cc_engine import todo

from test_scheduler import (NO_USAGE, OK_USAGE, T0, _finish, add_run, events, make_env, seed, sheet,
                            statuses, usage)

LIMIT = "Claude AI usage limit reached|1800000000"


def _prompt(env, node_id):
    return (env.dispatcher.latest_job("p1", node_id) / "prompt.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------- resume from partial work

def test_earlier_attempt_section_only_for_attempts_that_stopped_early(tmp_path):
    job = tmp_path / "1"
    job.mkdir()
    (job / "out.txt").write_text("edited a.py; next: b.py", encoding="utf-8")
    text = D.earlier_attempt(job)
    assert "Continue from an earlier attempt" in text and "cut off" in text
    assert "edited a.py; next: b.py" in text and str(job) in text
    (job / "exit.json").write_text(json.dumps({"exit_code": 1}), encoding="utf-8")
    assert "exited with code 1" in D.earlier_attempt(job)
    (job / "exit.json").write_text(json.dumps({"exit_code": 0}), encoding="utf-8")
    assert D.earlier_attempt(job) == ""  # a finished attempt travels as the W7 return packet
    assert D.earlier_attempt(None) == "" and D.earlier_attempt(tmp_path / "none") == ""


def test_restart_resumes_from_the_partial_work(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started")])
    add_run(env.store, "p1", sheet(auto_resume=True))
    S.tick(env.store, T0, env.dispatcher, usage_reader=OK_USAGE)
    first = env.dispatcher.latest_job("p1", "A")
    (first / "out.txt").write_text("step 1 of 3 done", encoding="utf-8")
    env.alive.clear()  # the machine restarted mid-job: no exit file
    res = S.tick(env.store, T0 + 120, env.dispatcher, usage_reader=OK_USAGE)
    assert res["resumed"] == ["A"] and res["started"] == ["A"]
    prompt = _prompt(env, "A")
    assert "Continue from an earlier attempt" in prompt and "step 1 of 3 done" in prompt


# ---------------------------------------------------------------- across the 5-hour reset

def test_limit_moves_unpinned_task_to_another_open_pool_with_its_partial_work(tmp_path):
    text_check = {"type": "text_present", "path": "a", "text": "b"}  # Freebuff takes text-only work
    env = make_env(tmp_path, [("A", [], "not_started", {"pool": None, "model": None, "effort": None,
                                                        "check": text_check})])
    add_run(env.store, "p1", sheet(["claude_code", "freebuff"], default_pool="claude_code"))
    u = usage(resets=T0 + 4 * 3600)
    assert S.tick(env.store, T0, env.dispatcher, usage_reader=u)["started"] == ["A"]
    assert env.store.get_node("p1", "A")["pool"] == "claude_code"
    _finish(env, "A", err_text=LIMIT)
    res = S.tick(env.store, T0 + 30, env.dispatcher, usage_reader=u)
    assert res["transient"][0]["reason"] == "a usage limit was reached"
    n = env.store.get_node("p1", "A")
    assert n["pool"] is None and n["model"] is None  # released, not parked until the reset
    assert S.pool_hold(env.store, "claude_code")["until"] == T0 + 4 * 3600
    later = T0 + 30 + 601
    assert S.tick(env.store, later, env.dispatcher, usage_reader=u)["started"] == ["A"]
    n = env.store.get_node("p1", "A")
    assert n["pool"] == "freebuff" and n["model"]  # the freebuff ladder's model, not Claude's
    assert "Continue from an earlier attempt" in _prompt(env, "A")


def test_pinned_task_waits_for_the_reset_then_continues(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started"), ("B", [], "not_started")])
    add_run(env.store, "p1", sheet(["claude_code", "freebuff"], max_concurrency=1))
    u = usage(resets=T0 + 4 * 3600)
    S.tick(env.store, T0, env.dispatcher, usage_reader=u)
    _finish(env, "A", err_text=LIMIT)
    S.tick(env.store, T0 + 30, env.dispatcher, usage_reader=u)
    assert env.store.get_node("p1", "A")["pool"] == "claude_code"  # the plan pinned it
    assert S.tick(env.store, T0 + 3 * 3600, env.dispatcher, usage_reader=u)["started"] == []
    fresh = usage(remaining=100, resets=T0 + 9 * 3600)
    started = S.tick(env.store, T0 + 4 * 3600 + 61, env.dispatcher, usage_reader=fresh)["started"]
    assert started == ["A"] and "Continue from an earlier attempt" in _prompt(env, "A")


# ---------------------------------------------------------------- floors per pool (note 146)

def _row(pool, left):
    return {"pool": pool, "remaining_percent": left, "status": "FRESH", "resets_at": None}


def test_floors_default_codex_20_everything_else_0():
    claude = {"claude_5h": _row("claude_5h", 1), "claude_week": _row("claude_week", 1)}
    assert S.usage_gate("claude_code", claude, None, 1.0)[0] == "open"
    assert S.usage_gate("claude_code", {"claude_5h": _row("claude_5h", 0), "claude_week": _row("claude_week", 5)},
                        None, 1.0)[0] == "low"
    assert S.usage_gate("freebuff", {}, None, 1.0)[0] == "free"
    codex = {"codex_5h": _row("codex_5h", 19), "codex_week": _row("codex_week", 80)}
    assert S.usage_gate("codex", codex, None, 1.0)[0] == "low"


def test_floors_can_be_set_per_pool():
    codex = {"codex_5h": _row("codex_5h", 10), "codex_week": _row("codex_week", 40)}
    assert S.usage_gate("codex", codex, S.reserve_percent({"reserve_percent": {"codex": 5}}), 1.0)[0] == "open"
    week = S.reserve_percent({"reserve_percent": {"codex_week": 50, "codex_5h": 5}})
    assert S.usage_gate("codex", codex, week, 1.0)[0] == "low"
    claude = {"claude_5h": _row("claude_5h", 1), "claude_week": _row("claude_week", 1)}
    only_codex = S.reserve_percent({"reserve_percent": {"codex": 30}})
    assert S.usage_gate("claude_code", claude, only_codex, 1.0)[0] == "open"  # Claude keeps 0%


# ---------------------------------------------------------------- retired pool (removed 5 Nov 2026)

def test_an_old_run_still_ticking_the_retired_pool_runs_on_the_others(tmp_path):
    old = retired_ids.RETIRED_TOGGLE_ID
    env = make_env(tmp_path, [("A", [], "not_started", {"pool": None, "model": None}),
                              ("B", [], "not_started", {"pool": old, "model": None})])
    add_run(env.store, "p1", sheet(["claude_code", old], max_concurrency=4, default_pool=old))
    res = S.tick(env.store, T0, env.dispatcher, usage_reader=OK_USAGE)
    assert not res.get("error")
    assert env.store.get_node("p1", "B")["pool"] != old or "B" not in res["started"]


# ---------------------------------------------------------------- needs-you -> human to-dos

def test_needs_you_becomes_a_human_todo_and_the_rest_carries_on(tmp_path):
    env = make_env(tmp_path, [("A", [], "needs_you"), ("B", [], "not_started")])
    add_run(env.store, "p1", sheet())
    res = S.tick(env.store, T0, env.dispatcher, usage_reader=OK_USAGE)
    assert res["started"] == ["B"] and res["needs_you_todos"] == 1
    mine = [t for t in todo.list_todos(env.store) if t["created_by"] == overnight.NEEDS_YOU_ACTOR]
    assert len(mine) == 1 and mine[0]["section"] == "human" and mine[0]["status"] == "open"
    assert mine[0]["node"] == {"project": "p1", "id": "A"} and "Waypoint needs you" in mine[0]["text"]
    S.tick(env.store, T0 + 60, env.dispatcher, usage_reader=OK_USAGE)
    assert len([t for t in todo.list_todos(env.store) if t["created_by"] == overnight.NEEDS_YOU_ACTOR]) == 1
    env.store.set_node_status("p1", "A", "not_started", "owner")  # Owner answered: it retries
    S.tick(env.store, T0 + 120, env.dispatcher, usage_reader=OK_USAGE)
    assert todo.get_todo(env.store, mine[0]["id"])["status"] == "done"


def test_parked_run_writes_a_receipt_and_lets_the_next_row_start(tmp_path):
    env = make_env(tmp_path, [("A", [], "needs_you"), ("B", ["A"], "not_started")])
    seed(env.store, [("X", [], "not_started")], "p2", cwd=str(tmp_path))
    r1 = add_run(env.store, "p1", sheet())
    add_run(env.store, "p2", {**sheet(), "after_runs": [r1]})
    res = S.tick(env.store, T0, env.dispatcher, usage_reader=OK_USAGE)
    assert res["parked"] == [r1] and "X" in res["started"]
    assert statuses(env.store, "p2")["X"] == "running"
    assert env.store.get_run(r1)["status"] == "running"  # still active: the answer resumes it
    receipt = overnight.receipts_dir(env.store) / f"{r1}.md"
    text = receipt.read_text(encoding="utf-8")
    assert "parked: waiting on Owner" in text and "Waiting for Owner" in text
    S.tick(env.store, T0 + 60, env.dispatcher, usage_reader=OK_USAGE)
    assert sum(e["kind"] == "receipt" for e in events(env.store)) == 1  # once per parked episode


def test_finished_run_writes_a_receipt(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started")])
    rid = add_run(env.store, "p1", sheet())
    S.tick(env.store, T0, env.dispatcher, usage_reader=OK_USAGE)
    _finish(env, "A")
    res = S.tick(env.store, T0 + 30, env.dispatcher, usage_reader=OK_USAGE)
    assert res["finished_runs"] == [rid] and len(res["receipts"]) == 1
    text = Path(res["receipts"][0]).read_text(encoding="utf-8")
    assert f"Run: {rid} (finished)" in text and "- A: Task A (done, claude_code / sonnet, 1 attempt)" in text
    assert any(e["kind"] == "receipt" and e["data"]["path"] == res["receipts"][0] for e in events(env.store))


def test_no_active_run_means_a_tick_dispatches_nothing(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started")])
    res = S.tick(env.store, T0, env.dispatcher, usage_reader=NO_USAGE)
    assert res["started"] == [] and env.launched == []


# ---------------------------------------------------------------- usage probe never hangs the refresher

def test_usage_probe_timeout_kills_the_grandchild_and_returns(tmp_path):
    """claude.cmd -> claude.exe: a timeout must not leave run() waiting on the grandchild's pipe."""
    import subprocess
    import sys
    import time

    import pytest

    from cc_engine import usage
    child = tmp_path / "child.py"
    child.write_text("import time\nprint('x', flush=True)\ntime.sleep(60)\n", encoding="utf-8")
    parent = tmp_path / "parent.py"  # stands in for claude.cmd: starts the real worker and waits
    parent.write_text(f"import subprocess,sys\nsubprocess.call([sys.executable, r'{child}'])\n",
                      encoding="utf-8")
    t = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        usage._run_tree([sys.executable, str(parent)], timeout=2)
    assert time.monotonic() - t < 20


def test_tick_cli_reads_usage_beside_its_database(tmp_path, monkeypatch):
    import os

    from cc_engine import usage
    monkeypatch.delenv("CC_DATA_DIR", raising=False)
    data = tmp_path / "live"
    data.mkdir()
    db = data / "cc.sqlite"
    S.main(["tick", "--db", str(db)])
    assert os.environ["CC_DATA_DIR"] == str(data.resolve())
    assert usage.default_cache_path() == data.resolve() / "usage_cache.json"
