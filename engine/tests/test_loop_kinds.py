"""W1 loop_kinds: node kinds splitter|worker|code|gate; code nodes run a script, never a model;
the planner validator rejects a model node that needs no judgement and checks a pure transform."""
import json
import sys

import pytest

from cc_engine import deep_plan, node_kinds, plan, planner
from cc_engine.plan import PlanError

PY = f'"{sys.executable}"'


def cmd(run):
    return {"type": "command", "run": run}


def kinds_plan(**over):
    p = {"id": "lk", "title": "Kinds", "brief": "b", "nodes": [
        {"id": "S1", "title": "Split", "goal": "Decide how the work divides.", "kind": "splitter",
         "check": {"type": "files_exist", "paths": ["plan.json"]}, "deps": []},
        {"id": "W1", "title": "Build", "goal": "Implement the parser with tests.", "kind": "worker",
         "check": cmd("python -m pytest -q"), "deps": ["S1"]},
        {"id": "C1", "title": "Merge", "goal": "Merge the three CSV exports into one sorted file.", "kind": "code",
         "script": "python merge.py", "check": {"type": "files_exist", "paths": ["out.csv"]}, "deps": ["W1"]},
        {"id": "G1", "title": "Ship", "goal": "Owner approves the release.", "kind": "gate",
         "check": {"type": "human", "prompt": "Ship it?"}, "deps": ["C1"]}]}
    p.update(over)
    return p


def test_loop_kinds_set():
    assert node_kinds.KINDS == ("splitter", "worker", "code", "gate")
    assert "code" not in node_kinds.MODEL_KINDS


def test_loop_kinds_import_stores_and_exports_kind(store):
    plan.import_plan(store, kinds_plan())
    nodes = node_kinds.annotate(store, "lk", store.list_nodes("lk"))
    assert {n["id"]: n["kind"] for n in nodes} == {"S1": "splitter", "W1": "worker", "C1": "code", "G1": "gate"}
    assert store.get_node("lk", "W1")["deps"] == ["S1"]  # deps_json keeps plain ids
    out = plan.export_plan(store, "lk")
    by = {n["id"]: n for n in out["nodes"]}
    assert by["C1"]["kind"] == "code" and by["C1"]["script"] == "python merge.py"
    plan.import_plan(store, out)  # round trip imports again
    assert node_kinds.node_kind(store.get_node("lk", "C1"), store) == "code"


def test_loop_kinds_defaults_for_old_nodes(store, fixture_plan):
    plan.import_plan(store, fixture_plan)
    kinds = {n["id"]: n["kind"] for n in node_kinds.annotate(store, "fx", store.list_nodes("fx"))}
    assert kinds == {"A": "worker", "B": "worker", "C": "worker", "D": "worker", "E": "gate"}
    assert "kind" not in plan.export_plan(store, "fx")["nodes"][0]


def test_loop_kinds_import_rejects_bad_kinds(store):
    bad = kinds_plan()
    bad["nodes"][0]["kind"] = "oracle"
    with pytest.raises(PlanError, match="kind"):
        plan.import_plan(store, bad)
    no_script = kinds_plan()
    no_script["nodes"][2].pop("script")
    with pytest.raises(PlanError, match="script"):
        plan.import_plan(store, no_script)
    ok = kinds_plan()
    ok["nodes"][2].pop("script")
    ok["nodes"][2]["check"] = cmd("python merge.py --verify")  # the command check is the script
    plan.import_plan(store, ok)


def test_loop_kinds_code_runner_runs_script_never_a_model(tmp_path):
    calls = []

    def runner(c, cwd, timeout):
        calls.append(c)
        return 0, "merged 3\n", ""

    n = {"id": "C1", "kind": "code", "script": "python merge.py", "goal": "Merge.", "check": cmd("exit 0")}
    res = node_kinds.code_runner(n, tmp_path, runner=runner)
    assert calls == ["python merge.py"] and res["ok"] and res["route"] == "code"
    assert "no model" in res["summary"]
    real = node_kinds.code_runner({"id": "C2", "kind": "code", "check": cmd(f"{PY} -c \"print(41 + 1)\"")}, tmp_path)
    assert real["ok"] and real["stdout"].strip() == "42"
    with pytest.raises(node_kinds.KindError):
        node_kinds.code_runner({"id": "W", "kind": "worker", "check": cmd("exit 0")}, tmp_path, runner=runner)


def test_loop_kinds_code_runner_reads_stored_kind(store, tmp_path):
    plan.import_plan(store, kinds_plan())
    seen = []
    res = node_kinds.code_runner(store.get_node("lk", "C1"), tmp_path, db=store,
                                 runner=lambda c, cwd, t: (seen.append(c) or (0, "", "")))
    assert res["ok"] and seen == ["python merge.py"]


def test_loop_kinds_dispatch_code_never_routes_to_a_model(store, tmp_path):
    from cc_engine.dispatch import Dispatcher

    plan.import_plan(store, kinds_plan())
    launched = []
    d = Dispatcher(data_dir=tmp_path / "data", launcher=lambda p: launched.append(json.loads(open(p).read()))
                   or {"pid": None}, toggles_db=store)
    node = store.get_node("lk", "C1")
    assert node_kinds.is_code(node, d.toggles_db)
    out = node_kinds.dispatch_code(d, node, {"id": "lk", "cwd": str(tmp_path)})
    assert out["route"] == "code" and out["status"] == "running"
    spec = launched[0]
    assert spec["route"] == "code" and spec["argv"][-1] == "python merge.py"
    text = " ".join(spec["argv"]).lower()
    assert "claude" not in text and "codex" not in text and "--model" not in text
    assert not node_kinds.is_code(store.get_node("lk", "W1"), store)


# ---------------------------------------------------------------- planner validator
def graph_with(n):
    return {"brief": "b", "nodes": [dict({"id": "A1", "title": "T"}, **n)]}


def test_loop_kinds_validator_rejects_model_node_doing_a_transform():
    merge = {"goal": "Merge the three CSV exports into one sorted file.", "check": cmd("python check_out.py")}
    found = planner.problems(graph_with(merge))
    assert any("code" in p and "A1" in p for p in found)
    found = planner.problems(graph_with(dict(merge, kind="worker")))
    assert any("kind \"code\"" in p for p in found)
    assert planner.problems(graph_with(dict(merge, kind="code", script="python merge.py"))) == []


def test_loop_kinds_validator_allows_judgement_and_authoring():
    for goal in ("Assess the merged candidates and rank them by fit.",
                 "Summarise the compared reports for Owner.",
                 "Decide which duplicates to drop after the dedupe.",
                 "Implement the merge step in merge.py with tests."):
        assert planner.problems(graph_with({"goal": goal, "check": cmd("python -m pytest -q")})) == [], goal
    human = {"goal": "Merge the lists.", "check": {"type": "human", "prompt": "Looks right"}}
    assert planner.problems(graph_with(human)) == []  # not a deterministic check: not a pure transform


def test_loop_kinds_validator_shape_rules():
    assert any("kind must be" in p for p in planner.problems(graph_with(
        {"goal": "Do it.", "kind": "oracle", "check": cmd("exit 0")})))
    assert any("script" in p for p in planner.problems(graph_with(
        {"goal": "Merge.", "kind": "code", "check": {"type": "files_exist", "paths": ["x"]}})))


def test_loop_kinds_deep_plan_rejects_transform_worker():
    g = {"brief": "b", "nodes": [{"id": "A1", "title": "Dedupe", "goal": "Dedupe the rows into rows.json.",
                                  "check": cmd("python verify.py"), "files": ["rows.json"], "deps": []}]}
    ev = deep_plan.evaluate(g)
    assert not ev["valid"] and any("code" in p for p in ev["problems"])
    g["nodes"][0].update(kind="code", script="python dedupe.py")
    assert deep_plan.evaluate(g)["valid"]


def test_loop_kinds_build_plan_keeps_kind_and_script():
    raw = graph_with({"goal": "Merge.", "kind": "code", "script": "python merge.py", "check": cmd("exit 0")})
    built = planner.build_plan({"title": "T"}, raw, "p1", allowed_models=[])
    assert built["nodes"][0]["kind"] == "code" and built["nodes"][0]["script"] == "python merge.py"
    assert planner.summarise(built)["nodes"][0]["kind"] == "code"


def test_loop_kinds_prompts_teach_the_rule():
    for name in ("planner_draft.md", "deep_graph.md", "deep_revise.md"):
        text = (planner.PROMPT_DIR / name).read_text(encoding="utf-8")
        assert '"kind"' in text and "code" in text and "judge" in text, name
