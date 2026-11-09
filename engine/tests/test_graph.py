import pytest

from cc_engine import graph


def nodes(status=None):
    status = status or {}
    spec = {"A": [], "B": ["A"], "C": ["A"], "D": ["B", "C", "A"], "E": ["D"]}
    return [{"id": k, "deps": v, "status": status.get(k, "not_started")} for k, v in spec.items()]


def test_graph_status_set_is_exact():
    assert graph.STATUSES == ("not_started", "ready", "running", "submitted", "done", "error", "needs_you",
                              "skipped", "carried")


def test_graph_waves_are_longest_path_depth():
    assert graph.waves(nodes()) == {"A": 0, "B": 1, "C": 1, "D": 2, "E": 3}


def test_graph_transitive_reduction_drops_implied_edge():
    edges = graph.transitive_reduction(nodes())
    assert ("A", "D") not in edges
    assert set(edges) == {("A", "B"), ("A", "C"), ("B", "D"), ("C", "D"), ("D", "E")}


def test_graph_readiness_derives_ready():
    r = graph.readiness(nodes({"A": "done", "B": "done"}))
    assert r == {"A": "done", "B": "done", "C": "ready", "D": "not_started", "E": "not_started"}


def test_graph_roots_are_ready_and_stored_ready_with_open_deps_is_not():
    r = graph.readiness(nodes({"D": "ready"}))
    assert r["A"] == "ready"
    assert r["D"] == "not_started"


def test_graph_ready_nodes_not_strict_waves():
    ns = nodes({"A": "done", "B": "done"})
    ns.append({"id": "F", "deps": [], "status": "not_started"})
    assert [n["id"] for n in graph.ready_nodes(ns)] == ["F", "C"]


def test_graph_running_and_error_are_not_ready():
    r = graph.readiness(nodes({"A": "running"}))
    assert r["A"] == "running"
    assert graph.ready_nodes(nodes({"A": "error"})) == []


def test_graph_cycle_raises():
    with pytest.raises(ValueError):
        graph.waves([{"id": "X", "deps": ["Y"]}, {"id": "Y", "deps": ["X"]}])


def test_graph_unknown_dep_blocks_readiness():
    assert graph.readiness([{"id": "X", "deps": ["missing"], "status": "not_started"}])["X"] == "not_started"
