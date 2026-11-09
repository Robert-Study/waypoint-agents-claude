"""plan_metrics reads v2 object deps ({"id", "crosses"}) as well as plain id strings."""
from cc_engine import plan_metrics as pm


def T(nid, deps=(), files=(), **kw):
    return dict({"id": nid, "deps": list(deps), "files": list(files)}, **kw)


def O(dep_id, crosses="a handoff"):
    return {"id": dep_id, "crosses": crosses}


def test_object_deps_chain_scores_real_critical_path_and_waves():
    nodes = [
        T("A", files=["a.py"]),
        T("B", [O("A")], ["b.py"]),
        T("C", [O("B")], ["c.py"]),
    ]
    s = pm.score(nodes)
    assert s["cycle"] is None
    assert s["critical_path"] == 3
    assert s["critical_path_ids"] == ["A", "B", "C"]
    assert s["waves"] == 3
    assert s["width_by_wave"] == [1, 1, 1]
    assert s["overlaps"] == []
    assert s["ok"] is True


def test_object_deps_parallel_writers_of_one_file_still_overlap():
    nodes = [
        T("A", files=["core.py"]),
        T("B", [O("A")], ["shared.py"]),
        T("C", [O("A")], ["shared.py"]),
    ]
    s = pm.score(nodes)
    assert len(s["overlaps"]) == 1
    o = s["overlaps"][0]
    assert {o["a"], o["b"]} == {"B", "C"}
    assert o["files"] == ["shared.py"]
    assert s["needs_repair"] is True


def test_object_deps_two_parallel_nodes_with_no_shared_file_have_no_overlap():
    nodes = [
        T("A", files=["core.py"]),
        T("B", [O("A")], ["left.py"]),
        T("C", [O("A")], ["right.py"]),
    ]
    s = pm.score(nodes)
    assert s["overlaps"] == []
    assert s["peak_width"] == 2


def test_mixed_string_and_object_deps_are_both_read():
    nodes = [
        T("A", files=["a.py"]),
        T("B", ["A"], ["b.py"]),
        T("C", [O("B")], ["c.py"]),
    ]
    s = pm.score(nodes)
    assert s["critical_path"] == 3
    assert s["waves"] == 3


def test_object_dep_on_unknown_id_is_ignored():
    nodes = [T("A", files=["a.py"]), T("B", [O("NOPE")], ["b.py"])]
    s = pm.score(nodes)
    assert s["critical_path"] == 1
    assert s["waves"] == 1
    assert s["overlaps"] == []


def test_repair_serialises_object_dep_overlap():
    nodes = [
        T("A", files=["core.py"]),
        T("B", [O("A")], ["shared.py"]),
        T("C", [O("A")], ["shared.py"]),
    ]
    r = pm.repair(nodes)
    assert r["score"]["overlaps"] == []
    assert r["score"]["ok"] is True
