import json

import pytest

from cc_engine import failure
from cc_engine import scheduler as S

from test_scheduler import make_env, statuses, events

SETTINGS = {"model_ladder": {"claude_code": ["sonnet", "opus"]}, "effort": "high"}


def goal_state(store, pid="p1"):
    return {n["id"]: (n["goal"], n["goal_hash"]) for n in S.list_nodes(store, pid)}


def test_ladder_raises_effort_then_climbs_models():
    assert failure.ladder(SETTINGS, "claude_code") == [
        ("sonnet", "high"), ("sonnet", "xhigh"), ("opus", "xhigh")]
    assert failure.ladder({"model_ladder": {"x": ["m"]}, "effort": "max"}, "x") == [("m", "max")]
    # no ladder for the pool: escalate effort on the current model, never to "no model"
    assert failure.ladder({"model_ladder": {}, "effort": "high"}, "x", "sonnet") == [
        ("sonnet", "high"), ("sonnet", "xhigh")]


def test_next_rung():
    n = {"pool": "claude_code", "model": "sonnet", "effort": "high"}
    assert failure.next_rung(n, SETTINGS) == ("sonnet", "xhigh")
    n.update(model="opus", effort="xhigh")
    assert failure.next_rung(n, SETTINGS) is None
    n.update(model="haiku", effort="low")  # not on the ladder: start at the bottom
    assert failure.next_rung(n, SETTINGS) == ("sonnet", "high")


def test_escalates_then_reports_and_goal_never_changes(tmp_path):
    env = make_env(tmp_path, [("A", [], "error", {"pool": "claude_code", "model": "sonnet",
                                                   "effort": "high"})])
    before = goal_state(env.store)
    rev0 = env.store.revision()

    out = failure.on_check_failed(env.store, "p1", S.get_node(env.store, "p1", "A"), SETTINGS,
                                  "tests failed", asker=env.asker)
    assert out == {"action": "escalate", "model": "sonnet", "effort": "xhigh"}
    n = S.get_node(env.store, "p1", "A")
    assert n["status"] == "not_started" and n["effort"] == "xhigh"
    assert env.store.revision() > rev0

    failure.on_check_failed(env.store, "p1", S.get_node(env.store, "p1", "A"), SETTINGS, "x")
    assert S.get_node(env.store, "p1", "A")["model"] == "opus"

    out = failure.on_check_failed(env.store, "p1", S.get_node(env.store, "p1", "A"), SETTINGS,
                                  "still failing", asker=env.asker)
    assert out["action"] == "report" and out["question_id"] == "q1"
    assert statuses(env.store)["A"] == "needs_you"
    q = env.asked[0]
    assert q["kind"] == "select" and q["node_id"] == "A"
    assert all(isinstance(o, str) for o in q["options"])  # plain options, nothing preselected
    kinds = [e["kind"] for e in events(env.store)]
    assert kinds.count("escalate") == 2 and kinds.count("report") == 1
    after = goal_state(env.store)
    assert {k: after[k] for k in before} == before  # no goal changed
    # failure_doctor (when present) may add one fix-up task; nothing else is added.
    assert all(k in before or k.startswith("A-fix") for k in after)


def test_every_auto_change_writes_one_sentence_summary(tmp_path):
    env = make_env(tmp_path, [("A", [], "error", {"pool": "claude_code"})])
    failure.on_check_failed(env.store, "p1", S.get_node(env.store, "p1", "A"), SETTINGS, "x")
    failure.add_fixup(env.store, "p1", "A", "Install the missing fixture",
                      {"type": "files_exist", "paths": ["f.txt"]})
    auto = [e for e in events(env.store) if e["kind"] in ("escalate", "fixup", "report", "reorder")]
    assert auto
    for e in auto:
        s = e["summary"]
        assert s and s.endswith(".") and s.count(". ") == 0 and "\n" not in s


def test_fixup_node_inserted_before_original(tmp_path):
    env = make_env(tmp_path, [("A", [], "done"), ("B", ["A"], "needs_you")])
    before = goal_state(env.store)
    fid = failure.add_fixup(env.store, "p1", "B", "Repair config", {"type": "command", "run": "exit 0"})
    assert fid == "B-fix1"
    assert failure.add_fixup(env.store, "p1", "B", "Second repair",
                             {"type": "command", "run": "exit 0"}) == "B-fix2"
    fix = S.get_node(env.store, "p1", "B-fix1")
    b = S.get_node(env.store, "p1", "B")
    assert fix["deps"] == ["A"] and fix["status"] == "not_started"
    assert b["deps"] == ["A", "B-fix1", "B-fix2"] and b["status"] == "not_started"
    after = goal_state(env.store)
    assert after["A"] == before["A"] and after["B"] == before["B"]  # original goals unchanged
    assert fix["goal_hash"] == S.goal_hash(fix["goal"])


def test_reorder_deps_rejects_cycles_and_unknown(tmp_path):
    env = make_env(tmp_path, [("A", [], "done"), ("B", ["A"], "not_started"),
                              ("C", ["B"], "not_started")])
    with pytest.raises(ValueError):
        failure.reorder_deps(env.store, "p1", "A", ["C"])
    with pytest.raises(ValueError):
        failure.reorder_deps(env.store, "p1", "B", ["nope"])
    before = goal_state(env.store)
    failure.reorder_deps(env.store, "p1", "C", ["A"])
    assert S.get_node(env.store, "p1", "C")["deps"] == ["A"]
    assert goal_state(env.store) == before
    assert any(e["kind"] == "reorder" and "now waits for A" in e["summary"] for e in events(env.store))


def test_failure_policy_cannot_change_goal(tmp_path):
    env = make_env(tmp_path, [("A", [], "error")])
    with pytest.raises(failure.GoalLocked):
        S.update_node_fields(env.store, "p1", "A", {"goal": "Something easier."}, "failure", "x.")
    with pytest.raises(failure.GoalLocked):
        S.update_node_fields(env.store, "p1", "A", {"goal_hash": "zzz"}, "failure", "x.")
    assert S.get_node(env.store, "p1", "A")["goal"] == "Goal A."


def test_swap_model_logs_summary(tmp_path):
    env = make_env(tmp_path, [("A", [], "error")])
    failure.swap_model(env.store, "p1", "A", "opus", "max")
    n = S.get_node(env.store, "p1", "A")
    assert (n["model"], n["effort"]) == ("opus", "max")
    assert events(env.store)[-1]["summary"] == "Task A will now use opus at max effort."


# ---------------------------------------------------------------- transient classification

def test_transient_classify_exit_cases():
    ok = {"exit_code": 0, "route": "claude_code", "output_tail": '{"result": "done"}'}
    assert failure.classify_exit(ok) == ("completed", None)
    assert failure.classify_exit({"exit_code": 3})[0] == "transient"
    assert failure.classify_exit({"exit_code": 1, "http_status": 403}) == (
        "transient", "the provider refused it (403)")
    assert failure.classify_exit({"exit_code": 0, "stderr_tail": "Claude usage limit reached"}) == (
        "transient", "a usage limit was reached")
    assert failure.classify_exit({"exit_code": 1, "stderr_tail":
                                  "ChatGPT browser DOM observation did not respond within 5000ms"}) == (
        "transient", "the bridge timed out")
    err = {"exit_code": 0, "route": "claude_code",
           "output_tail": json.dumps({"is_error": True, "result": "rate limit"})}
    assert failure.classify_exit(err) == ("transient", "the provider rate-limited it")
    # Ordinary output that merely mentions limits is a completed attempt.
    assert failure.classify_exit({"exit_code": 0, "output_tail": "fixed the usage limit gate"}) == (
        "completed", None)


def test_transient_does_not_count_attempt_or_touch_ladder(tmp_path):
    env = make_env(tmp_path, [("A", [], "running", {"pool": "claude_code", "model": "sonnet",
                                                     "effort": "high"})])
    env.store.update_node("p1", "A", {"attempts": 2}, "test", "x.")
    out = failure.on_transient(env.store, "p1", S.get_node(env.store, "p1", "A"), SETTINGS,
                               "the provider refused it (403)", 1000.0, pool_resets_at=5000.0)
    assert out == {"action": "retry", "retry_after": 1000.0 + failure.TRANSIENT_RETRY_SECONDS,
                   "pool_held_until": 5000.0, "released": []}
    n = S.get_node(env.store, "p1", "A")
    assert (n["status"], n["attempts"], n["model"], n["effort"]) == ("not_started", 1, "sonnet", "high")
    assert n["evidence"] == "attempt 2: not completed (the provider refused it (403))"
    assert failure.retry_after(env.store, "p1", "A") == 1000.0 + failure.TRANSIENT_RETRY_SECONDS
    assert S.pool_hold(env.store, "claude_code")["until"] == 5000.0
    assert not any(e["kind"] == "escalate" for e in events(env.store))


def test_transient_crash_pauses_pool_briefly_not_until_reset(tmp_path):
    env = make_env(tmp_path, [("A", [], "running", {"pool": "claude_code"})])
    failure.on_transient(env.store, "p1", S.get_node(env.store, "p1", "A"), SETTINGS,
                         "it exited with code 1", 1000.0, pool_resets_at=99999.0)
    assert S.pool_hold(env.store, "claude_code")["until"] == 1000.0 + failure.TRANSIENT_POOL_HOLD_SECONDS


def test_check_failure_evidence_records_attempt(tmp_path):
    env = make_env(tmp_path, [("A", [], "error", {"pool": "claude_code", "model": "sonnet",
                                                   "effort": "high"})])
    env.store.update_node("p1", "A", {"attempts": 3}, "test", "x.")
    failure.on_check_failed(env.store, "p1", S.get_node(env.store, "p1", "A"), SETTINGS, "boom")
    assert S.get_node(env.store, "p1", "A")["evidence"] == "attempt 3: boom"
