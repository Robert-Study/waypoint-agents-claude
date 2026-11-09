"""REV-engine (Waypoint v3 wave 4): regressions for engine bugs found in the v3 correctness review."""
from cc_engine import checks, dispatch, loops, needs
from cc_engine import scheduler as S

from test_scheduler import OK_USAGE, T0, add_run, make_env, sheet, statuses

SETTINGS = {"model_ladder": {"claude_code": ["sonnet", "opus"]}, "effort": "high"}
X = {"pool": "claude_code", "model": "sonnet", "effort": "high"}
OK_RUN = lambda cmd, cwd, timeout: (0, "fine", "")


def _kinds(env, nid):
    return [e["kind"] for e in env.store.events("p1") if e["node_id"] == nid]


# ---------------------------------------------------------------- W5 gate held is not a failure
def test_closed_risk_pass_held_for_owner_is_not_escalated_by_the_tick(tmp_path):
    env = make_env(tmp_path, [("A", [], "submitted", dict(X, risk="closed"))])
    add_run(env.store, "p1", sheet())
    checker = lambda store, pid, node, project=None: (
        lambda r: (bool(r.ok), r.detail))(checks.complete_node(store, pid, node["id"], runner=OK_RUN,
                                                              cwd=str(tmp_path)))
    S.tick(env.store, T0, env.dispatcher, usage_reader=OK_USAGE, asker=env.asker, checker=checker)
    node = S.get_node(env.store, "p1", "A")
    assert node["status"] == "needs_you"
    assert node["model"] == "sonnet"  # not climbed to opus as if it had failed
    kinds = _kinds(env, "A")
    assert "escalate" not in kinds and "returned" not in kinds and "replan" not in kinds
    assert any(q["purpose"] == "confirm" and q["node_id"] == "A" for q in env.asked)
    assert env.launched == []  # never re-dispatched


def test_closed_risk_manual_result_held_for_owner_is_not_escalated(tmp_path):
    env = make_env(tmp_path, [("A", [], "needs_you", dict(X, risk="closed"))])
    add_run(env.store, "p1", sheet())
    qid = needs.ask(env.store, "p1", "A", "text", "Paste the result", purpose="manual_result")
    needs.answer(env.store, qid, {"type": "reply", "value": "all done"}, check_runner=OK_RUN)
    node = S.get_node(env.store, "p1", "A")
    assert node["status"] == "needs_you" and node["model"] == "sonnet"
    assert "escalate" not in _kinds(env, "A")
    assert any(q["purpose"] == "confirm" and q["node_id"] == "A"
               for q in needs.list_questions(env.store, open_only=True, project_id="p1"))


# ---------------------------------------------------------------- W4 re-plan question is a valid question
def test_replan_question_is_accepted_by_the_live_asker(tmp_path):
    env = make_env(tmp_path, [("A", [], "error", dict(X))])
    asker = S.make_asker(env.store)
    node = S.get_node(env.store, "p1", "A")
    loops.on_failed_check(env.store, "p1", node, SETTINGS, reason="same", evidence="e", asker=asker)
    out = loops.on_failed_check(env.store, "p1", S.get_node(env.store, "p1", "A"), SETTINGS,
                                reason="same", evidence="e", asker=asker)
    assert out["action"] == "replan" and out["question_id"]
    q = needs.get_question(env.store, out["question_id"])
    assert q["node_id"] == "A" and q["kind"] == "select"


# ---------------------------------------------------------------- unavailable route
def _down(d, route, line="GitHub Copilot CLI not installed"):
    d.unavailable = lambda r: line if r == route else None  # what Dispatcher.unavailable says
    return d


def test_unavailable_route_neither_counts_attempts_nor_spams(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started", dict(X))])
    add_run(env.store, "p1", sheet())
    d = _down(env.new_dispatcher(asker=env.asker), "claude_code")
    S.tick(env.store, T0, d, usage_reader=OK_USAGE)
    before = len(env.store.events("p1"))
    for i in range(1, 4):
        res = S.tick(env.store, T0 + 60 * i, d, usage_reader=OK_USAGE)
    assert any("not installed" in str(b.get("reason")) for b in res["blocked"])
    assert S.get_node(env.store, "p1", "A")["attempts"] == 0
    assert statuses(env.store)["A"] == "not_started" and env.launched == []
    assert [e for e in env.store.events("p1")[before:] if e["node_id"] == "A"] == []


def test_unpinned_node_skips_an_unavailable_pool(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started", {"pool": None, "model": None})])
    add_run(env.store, "p1", sheet(["claude_code", "gemini_app"], default_pool="claude_code"))
    d = _down(env.new_dispatcher(asker=env.asker), "claude_code")
    res = S.tick(env.store, T0, d, usage_reader=OK_USAGE)
    assert res["needs_you"] == ["A"] and env.launched == []
    assert S.get_node(env.store, "p1", "A")["pool"] == "gemini_app"


# ---------------------------------------------------------------- usage: a stale 0% after its reset
def test_stale_zero_after_reset_passed_is_not_out_of_credits():
    past = {"pool": "antigravity_week", "remaining_percent": 0, "status": "STALE",
            "resets_at": "2020-01-01T00:00:00+00:00"}
    future = dict(past, resets_at="2099-01-01T00:00:00+00:00")
    unknown_reset = dict(past, resets_at=None)
    st = lambda rows: dispatch.route_state("agy_cli", which=lambda e: "agy", usage_rows=rows,
                                           signed_in=lambda: True)["state"]
    assert st([past]) == "ready"
    assert st([future]) == "out_of_credits"
    assert st([unknown_reset]) == "out_of_credits"  # unknown stays conservative
    assert st([dict(future, status="FRESH")]) == "out_of_credits"
