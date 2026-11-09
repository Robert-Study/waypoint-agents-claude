"""S3: Antigravity is a manual agentic-coder route (``antigravity_manual``), like the Gemini App.

A ready packet (prompt, cwd, checks) plus a needs-you question; never launched, never unattended.
Fakes only: no provider calls, tmp_path databases.
"""
import json
from pathlib import Path

from cc_engine import dispatch as D
from cc_engine import providers, runsheet
from cc_engine import scheduler as S

from test_scheduler import T0, OK_USAGE, add_run, make_env, sheet, statuses


def test_antigravity_manual_pool_names_route_to_the_manual_route():
    for name in ("antigravity", "antigravity_manual", "agy", "Antigravity"):
        assert D.route_for_pool(name) == "antigravity_manual"
    assert "antigravity_manual" in D.ROUTES
    assert "antigravity_manual" not in D.AUTOMATED_ROUTES
    assert "antigravity_manual" in D.MANUAL_ROUTES


def test_antigravity_manual_adapter_is_a_copy_prompt_hand_off():
    launch = providers.build("antigravity_manual", prompt="do it")
    assert launch.kind == "manual" and launch.argv == ()
    assert launch.label == "Antigravity"
    # Gemini API (AI Studio keys) and Antigravity stay different routes.
    assert providers.build("gemini_app").label == "Gemini App"


def test_antigravity_manual_dispatch_writes_packet_and_asks(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started", {"pool": "antigravity_manual",
                                                        "model": "gemini-3.8-flash-high"})])
    node = S.get_node(env.store, "p1", "A")
    out = env.dispatcher.dispatch(node, {}, {"id": "p1", "title": "Proj", "cwd": str(tmp_path)})
    assert out["route"] == "antigravity_manual" and out["status"] == "needs_you"
    assert env.launched == []  # never launched, so never unattended
    packet = json.loads((Path(out["job"]) / "packet.json").read_text(encoding="utf-8"))
    assert packet["route"] == "antigravity_manual" and packet["label"] == "Antigravity"
    assert packet["cwd"] == str(tmp_path)
    assert packet["check"] == {"type": "command", "run": "exit 0"}
    assert packet["model"] == "gemini-3.8-flash-high"
    assert Path(packet["prompt_path"]).read_text(encoding="utf-8").startswith("# Task A")
    assert "Goal A." in packet["prompt"]
    q = env.asked[0]
    assert q["purpose"] == "manual_result" and q["node_id"] == "A"
    assert "Antigravity" in q["prompt"] and str(tmp_path) in q["prompt"]
    assert out["question_id"] == "q1"


def test_antigravity_manual_in_a_run_becomes_needs_you_without_a_worker_slot(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started", {"pool": "antigravity_manual", "model": None}),
                              ("B", [], "not_started", {"pool": "antigravity_manual", "model": None})])
    add_run(env.store, "p1", sheet(pools=("antigravity_manual",), max_concurrency=1))
    res = S.tick(env.store, T0, env.dispatcher, usage_reader=OK_USAGE, asker=env.asker)
    assert sorted(res["needs_you"]) == ["A", "B"] and res["started"] == []
    assert statuses(env.store) == {"A": "needs_you", "B": "needs_you"}
    assert env.launched == []


def test_antigravity_manual_is_a_run_pool_but_not_a_create_sheet_pool():
    out = runsheet.expand({"preset": "balanced", "pools": ["antigravity_manual"]})
    assert out["model_ladder"]["antigravity_manual"]
    assert "antigravity_manual" in runsheet.RUN_POOLS
    assert "antigravity_manual" not in D.AUTOMATED_ROUTES
