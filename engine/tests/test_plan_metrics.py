"""plan_metrics: score a draft task graph and repair overlapping parallel writers. Pure, no store."""
import pytest

from cc_engine import plan_metrics as pm


def T(nid, deps=(), files=(), **kw):
    return dict({"id": nid, "deps": list(deps), "files": list(files)}, **kw)


DIAMOND = [T("A", files=["core.py"]), T("B", ["A"], ["left.py"]), T("C", ["A"], ["right.py"]),
           T("D", ["B", "C"], ["join.py"])]


def test_plan_metrics_scores_a_diamond():
    s = pm.score(DIAMOND)
    assert s["ok"] is True and s["cycle"] is None
    assert s["tasks"] == 4
    assert s["critical_path"] == 3 and s["critical_path_ids"][0] == "A" and s["critical_path_ids"][-1] == "D"
    assert s["waves"] == 3
    assert s["width_by_wave"] == [1, 2, 1]
    assert s["peak_width"] == 2
    assert s["overlaps"] == []
    assert s["problems"] == []


def test_plan_metrics_cost_weights_the_critical_path():
    nodes = [T("A", cost=1), T("B", ["A"], cost=5), T("C", ["A"], cost=1), T("D", ["B", "C"], cost=1)]
    s = pm.score(nodes)
    assert s["critical_path"] == 7 and s["critical_path_ids"] == ["A", "B", "D"]
    assert s["waves"] == 3


def test_plan_metrics_finds_overlaps_only_between_parallel_tasks():
    nodes = [T("A", files=["engine/x.py"]), T("B", ["A"], ["engine/x.py"]),        # serial: fine
             T("C", files=["Engine\\X.py", "c.py"]), T("D", files=["docs/"]),       # C parallel with A and B
             T("E", files=["docs/guide.md"])]                                      # dir prefix overlap with D
    s = pm.score(nodes)
    pairs = {(o["a"], o["b"]): o["files"] for o in s["overlaps"]}
    assert set(pairs) == {("A", "C"), ("B", "C"), ("D", "E")}
    assert pairs[("A", "C")] == ["engine/x.py"]
    assert pairs[("D", "E")] == ["docs/guide.md"]
    assert s["ok"] is False and s["needs_repair"] is True
    assert any("C" in p and "engine/x.py" in p for p in s["problems"])


def test_plan_metrics_glob_overlap():
    s = pm.score([T("A", files=["plugin/src/*.js"]), T("B", files=["plugin/src/page_plan.js"])])
    assert [(o["a"], o["b"]) for o in s["overlaps"]] == [("A", "B")]


def test_plan_metrics_reports_undeclared_files():
    s = pm.score([T("A", files=["a.py"]), {"id": "B", "deps": []}])
    assert s["undeclared"] == ["B"]
    assert s["ok"] is True  # a warning, not a failure


def test_plan_metrics_rejects_cycles():
    nodes = [T("A", ["C"]), T("B", ["A"]), T("C", ["B"])]
    s = pm.score(nodes)
    assert s["ok"] is False and s["cycle"] and set(s["cycle"]) == {"A", "B", "C"}
    assert s["critical_path"] is None and s["waves"] is None
    assert any("cycle" in p for p in s["problems"])
    with pytest.raises(pm.CycleError):
        pm.repair(nodes)


def test_plan_metrics_find_cycle_none_and_self_loop():
    assert pm.find_cycle(DIAMOND) is None
    assert pm.find_cycle([T("A", ["A"])]) == ["A", "A"]


def test_plan_metrics_repair_serialises_when_it_costs_no_time():
    # B and C both write shared.py; C is short so it can wait for B without stretching the path.
    nodes = [T("A", files=["a.py"]), T("B", ["A"], ["shared.py"]), T("C", [], ["shared.py"]),
             T("D", ["B"], ["d.py"])]
    before = pm.score(nodes)
    out = pm.repair(nodes)
    assert out["score"]["overlaps"] == [] and out["score"]["ok"] is True
    assert out["score"]["critical_path"] == before["critical_path"]
    by = {n["id"]: n for n in out["nodes"]}
    assert "B" in by["C"]["deps"] or "C" in by["B"]["deps"]
    assert out["changes"] and all(isinstance(c, str) and c.endswith(".") for c in out["changes"])
    assert nodes[2]["deps"] == []  # input untouched


def test_plan_metrics_repair_reassigns_when_serialising_would_cost_time():
    # Two long parallel chains share one config file; serialising would lengthen the path.
    nodes = [T("A1", files=["a1.py", "config.json"]), T("A2", ["A1"], ["a2.py"]),
             T("B1", files=["b1.py", "b2.py", "config.json"]), T("B2", ["B1"], ["b3.py"])]
    before = pm.score(nodes)
    out = pm.repair(nodes)
    s = out["score"]
    assert s["overlaps"] == [] and s["critical_path"] == before["critical_path"]
    by = {n["id"]: n for n in out["nodes"]}
    kept = [i for i in ("A1", "B1") if "config.json" in by[i]["files"]]
    gave = [i for i in ("A1", "B1") if "config.json" not in by[i]["files"]]
    assert len(kept) == 1 and len(gave) == 1
    assert by[gave[0]]["handoff"] == [{"file": "config.json", "to": kept[0]}]
    assert any("config.json" in c for c in out["changes"])


def test_plan_metrics_repair_strategy_serialise_only():
    nodes = [T("A1", files=["a1.py", "config.json"]), T("A2", ["A1"], ["a2.py"]),
             T("B1", files=["b1.py", "config.json"]), T("B2", ["B1"], ["b3.py"])]
    out = pm.repair(nodes, strategy="serialise")
    assert out["score"]["overlaps"] == []
    assert not any(n.get("handoff") for n in out["nodes"])
    assert out["score"]["critical_path"] == 3


def test_plan_metrics_repair_single_file_tasks_are_serialised():
    nodes = [T("A", files=["x.py"]), T("B", files=["x.py"])]
    out = pm.repair(nodes)
    by = {n["id"]: n for n in out["nodes"]}
    assert by["B"]["deps"] == ["A"] and out["score"]["overlaps"] == []


def test_plan_metrics_rank_prefers_no_overlap_then_short_then_wide():
    clean_long = pm.score([T("A", files=["a"]), T("B", ["A"], ["b"]), T("C", ["B"], ["c"])])
    clean_wide = pm.score([T("A", files=["a"]), T("B", files=["b"]), T("C", files=["c"])])
    dirty_wide = pm.score([T("A", files=["a"]), T("B", files=["a"]), T("C", files=["c"])])
    cyc = pm.score([T("A", ["B"]), T("B", ["A"])])
    ranked = sorted([cyc, dirty_wide, clean_long, clean_wide], key=pm.rank_key)
    assert ranked == [clean_wide, clean_long, dirty_wide, cyc]
    assert pm.best([dirty_wide, clean_long]) is clean_long


def test_plan_metrics_line_is_one_short_sentence():
    line = pm.line(pm.score(DIAMOND))
    assert "critical path 3" in line and "width 2" in line and "\n" not in line
    assert "overlap" in pm.line(pm.score([T("A", files=["a"]), T("B", files=["a"])]))


def test_plan_metrics_ignores_unknown_deps_and_bad_files():
    s = pm.score([T("A", ["ghost"], files=["a.py", None, 3, ""])])
    assert s["ok"] is True and s["critical_path"] == 1
