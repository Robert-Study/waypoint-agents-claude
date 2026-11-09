"""W9: correction returns and replans show in a task's history as one quiet line each."""
import pytest

from cc_engine import history, plan


@pytest.fixture
def loaded(store, fixture_plan, tmp_path):
    fixture_plan["cwd"] = str(tmp_path)
    plan.import_plan(store, fixture_plan)
    return "fx"


def test_returned_event_is_one_history_line(store, loaded):
    store.set_node_status("fx", "B", "running", "scheduler", summary="Task B started.")
    store.event("fx", "B", "returned", "check", "Returned: test_auth_redirect failed, scope handlers/auth.py",
                {"unit": "B", "verdict": "fail", "reason": "test_auth_redirect failed", "evidence": "exit 1",
                 "scope": ["handlers/auth.py"]})
    rows = history.node_history(store, "fx", "B")
    ret = [r for r in rows if r["kind"] == "returned"]
    assert len(ret) == 1
    assert ret[0]["text"] == "Returned: test_auth_redirect failed, scope handlers/auth.py"
    assert "\n" not in ret[0]["text"] and ret[0]["at"]


def test_replan_event_is_a_replanned_line(store, loaded):
    store.event("fx", "B", "replan", "planner", "Replanned: split B into B1 and B2", {"unit": "B"})
    rows = history.node_history(store, "fx", "B")
    assert [r["kind"] for r in rows] == ["replanned"]
    assert rows[0]["text"].startswith("Replanned")


def test_replan_found_by_data_unit_and_other_nodes_excluded(store, loaded):
    store.event("fx", None, "replan", "planner", "Replanned the rest", {"unit": "B"})
    store.event("fx", "C", "replan", "planner", "Replanned C", {"unit": "C"})
    assert [r["kind"] for r in history.node_history(store, "fx", "B")] == ["replanned"]
    assert len(history.node_history(store, "fx", "C")) == 1


def test_kinds_include_new_ones():
    assert "returned" in history.KINDS and "replanned" in history.KINDS
