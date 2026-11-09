"""Import -> export keeps the loop fields ``risk`` and ``scope`` (and omits them when never set)."""
from cc_engine import node_kinds, plan


def _plan():
    return {"id": "rt", "title": "Round trip", "brief": "b", "nodes": [
        {"id": "A", "title": "Edit", "goal": "Change the handler.", "deps": [],
         "check": {"type": "files_exist", "paths": ["a.py"]},
         "risk": "wide", "scope": ["handlers/auth.py", "tests/"]},
        {"id": "B", "title": "Plain", "goal": "Nothing special here.", "deps": ["A"],
         "check": {"type": "files_exist", "paths": ["b.py"]}},
        {"id": "C", "title": "Real world", "goal": "Send the invoice.", "deps": ["B"],
         "check": {"type": "human", "prompt": "Sent?"}, "risk": "closed"}]}


def test_loops_part2_roundtrip_keeps_risk_and_scope(store):
    plan.import_plan(store, _plan())
    out = plan.export_plan(store, "rt")
    by = {n["id"]: n for n in out["nodes"]}
    assert by["A"]["risk"] == "wide" and by["A"]["scope"] == ["handlers/auth.py", "tests/"]
    assert by["C"]["risk"] == "closed" and "scope" not in by["C"]
    assert "risk" not in by["B"] and "scope" not in by["B"]
    plan.import_plan(store, out)  # second pass changes nothing
    again = {n["id"]: n for n in plan.export_plan(store, "rt")["nodes"]}
    assert again == by
    row = node_kinds.rows(store, "rt")["A"]
    assert row["risk"] == "wide" and row["scope"] == ["handlers/auth.py", "tests/"]
