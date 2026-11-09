"""Loops part 2: W3 return packets, W4 re-plan, W5 risk lanes, W7 isolation, W8 one human gate."""
import pytest

from cc_engine import checks, failure, loops, node_kinds, planner
from cc_engine import scheduler as S

from test_scheduler import make_env

SETTINGS = {"model_ladder": {"claude_code": ["sonnet", "opus"]}, "effort": "high"}
X = {"pool": "claude_code", "model": "sonnet", "effort": "high"}


def events_of(env, kind, nid=None):
    return [e for e in env.store.events("p1") if e["kind"] == kind and (nid is None or e["node_id"] == nid)]


# ---------------------------------------------------------------- W3
def test_w3_loop_return_return_packet_event_and_summary(tmp_path):
    env = make_env(tmp_path, [("A", [], "error", dict(X))])
    node = S.get_node(env.store, "p1", "A")
    node["scope"] = ["handlers/auth.py", "tests/"]
    out = loops.on_failed_check(env.store, "p1", node, SETTINGS, verdict="fail",
                                reason="test_auth_redirect failed",
                                evidence="handlers/auth.py:42 assert 302 == 200")
    pk = out["packet"]
    assert set(pk) == {"unit", "verdict", "reason", "evidence", "scope"}
    ev = events_of(env, "returned", "A")
    assert len(ev) == 1
    assert ev[0]["summary"] == "Returned: test_auth_redirect failed, scope handlers/auth.py, tests/"
    text = loops.packet_text(pk)
    assert "handlers/auth.py:42 assert 302 == 200" in text and "UNIT: A" in text
    assert text in loops.rework_prompt("BASE PROMPT", pk)


def test_w3_loop_return_sibling_stays_done_and_out_of_scope_diff_rejected(tmp_path):
    env = make_env(tmp_path, [("A", [], "done"), ("B", [], "error", dict(X)), ("C", [], "done")])
    node = S.get_node(env.store, "p1", "B")
    node["scope"] = ["src/b.py"]
    loops.on_failed_check(env.store, "p1", node, SETTINGS, verdict="fail", reason="r", evidence="e")
    st = {n["id"]: n["status"] for n in S.list_nodes(env.store, "p1")}
    assert st["A"] == "done" and st["C"] == "done" and st["B"] == "not_started"
    ok, outside = loops.diff_check(node, ["src/b.py", "src/other.py"])
    assert not ok and outside == ["src/other.py"]
    assert loops.diff_check(node, ["src/b.py"])[0]
    assert loops.diff_check({"scope": []}, ["anything"])[0]          # empty = unrestricted
    assert loops.diff_check({"scope": ["tests"]}, ["tests/x.py"])[0]  # directory prefix
    assert not loops.diff_check({"scope": ["tests"]}, ["tests2/x.py"])[0]


# ---------------------------------------------------------------- W4
def test_w4_loop_replan_repeated_reason_triggers_replan_not_another_attempt(tmp_path):
    env = make_env(tmp_path, [("A", [], "error", dict(X))])
    get = lambda: S.get_node(env.store, "p1", "A")
    asked = []
    asker = lambda **kw: asked.append(kw) or "q1"
    o1 = loops.on_failed_check(env.store, "p1", get(), SETTINGS, "fail", "same reason", "e1", asker=asker)
    assert o1["action"] == "escalate"
    env.store.set_node_status("p1", "A", "error", "t", None, "x")
    o2 = loops.on_failed_check(env.store, "p1", get(), SETTINGS, "fail", "same reason", "e2", asker=asker)
    assert o2["action"] == "replan"
    assert get()["status"] == "needs_you"
    assert len(events_of(env, "replan", "A")) == 1
    assert asked and asked[0]["purpose"] == "replan" and "re-plan" in asked[0]["prompt"].lower()


def test_w4_loop_replan_different_reasons_climb_then_ladder_exhaustion_replans(tmp_path):
    env = make_env(tmp_path, [("A", [], "error", {"pool": "claude_code", "model": "opus", "effort": "xhigh"})])
    o = loops.on_failed_check(env.store, "p1", S.get_node(env.store, "p1", "A"), SETTINGS,
                              "fail", "another", "e")
    assert o["action"] == "replan"
    assert events_of(env, "replan", "A") and not events_of(env, "report", "A")


# ---------------------------------------------------------------- W5
def test_w5_loop_lanes_risk_default_and_lane_untouched():
    assert loops.risk_of({"lane": "core"}) == "contained"
    assert loops.risk_of({"risk": "wide", "lane": "core"}) == "wide"
    assert loops.risk_of({"risk": "bogus"}) == "contained"
    assert loops.RISKS == ("contained", "wide", "closed")


def test_w5_loop_lanes_closed_node_cannot_complete_without_owner_action(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started", dict(X, risk="closed", scope=["a.py"]))])
    assert loops.scope_of(S.get_node(env.store, "p1", "A"), env.store) == ["a.py"]
    ok_run = lambda cmd, cwd, timeout: (0, "fine", "")
    res = checks.complete_node(env.store, "p1", "A", runner=ok_run, cwd=str(tmp_path))
    assert S.get_node(env.store, "p1", "A")["status"] == "needs_you"
    res = checks.complete_node(env.store, "p1", "A", runner=ok_run, cwd=str(tmp_path), owner_action=True)
    assert S.get_node(env.store, "p1", "A")["status"] == "done"


def test_w5_loop_lanes_wide_requires_deterministic_check():
    assert loops.can_complete({"risk": "wide"}, "command", True)[0]
    assert not loops.can_complete({"risk": "wide"}, "human", True)[0]
    assert not loops.can_complete({"risk": "wide"}, "model", True)[0]
    assert loops.can_complete({"risk": "contained"}, "human", True)[0]
    assert not loops.can_complete({"risk": "closed"}, "command", True)[0]
    assert loops.can_complete({"risk": "closed"}, "command", True, owner_action=True)[0]


# ---------------------------------------------------------------- W7
def test_w7_loop_isolation_sibling_prompts_contain_no_other_workers_output(tmp_path):
    env = make_env(tmp_path, [("A", [], "done"), ("B", [], "running", dict(X)), ("C", [], "running", dict(X))])
    secret = "OUTPUT-OF-WORKER-B-9f3"
    env.store.event("p1", "B", "note", "worker", secret, {"output": secret})
    pb = loops.worker_prompt(S.get_node(env.store, "p1", "B"), {}, others_output=[secret])
    pc = loops.worker_prompt(S.get_node(env.store, "p1", "C"), {}, others_output=[secret])
    assert secret not in pc and secret not in pb
    assert loops.leaks("hello " + secret, [secret]) == [secret]
    assert loops.leaks(pc, [secret]) == []
    # a leaking prompt is scrubbed
    assert secret not in loops.isolate("x " + secret + " y", [secret])


# ---------------------------------------------------------------- W8
def gate(i, risk=None):
    n = {"id": i, "goal": "Owner approves.", "kind": "gate", "check": {"type": "human", "prompt": "ok?"}, "deps": []}
    if risk:
        n["risk"] = risk
    return n


def test_w8_loop_one_gate_default_template_has_exactly_one_human_gate():
    t = planner.default_template("T", "brief")
    humans = [n for n in t["nodes"] if n["check"]["type"] == "human"]
    assert len(humans) == 1 and humans[0]["id"] == t["nodes"][-1]["id"]
    assert planner.human_gate_problems(t["nodes"]) == []


def test_w8_loop_one_gate_extra_human_gates_flagged_unless_closed():
    assert planner.human_gate_problems([gate("a"), gate("b")])
    assert planner.human_gate_problems([gate("a"), gate("b", "closed")]) == []
    assert planner.human_gate_problems([gate("a", "closed"), gate("b", "closed")]) == []
    assert "human" in planner.human_gate_problems([gate("a"), gate("b")])[0]
