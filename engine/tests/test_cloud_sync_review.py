"""Cloud sync review fixes: closed/archived tasks are never reopened; the change check is one indexed query."""
import json

import pytest

from cc_engine import cloud_sync, todo, versions

from test_cloud_sync import kinds, loaded, put, repo, st  # noqa: F401  (shared helpers and fixtures)


def _force(store, status, node="A", project="fx"):
    store.mutate(None, lambda c: c.execute("UPDATE nodes SET status=? WHERE project_id=? AND id=?",
                                           (status, project, node)), "owner", f"test sets {node} {status}")


def _no_conflict_todo(store):
    return not any("Cloud sync conflict" in t["text"] for t in todo.list_todos(store))


# ------------------------------------------------------------------ finding 1
@pytest.mark.parametrize("closed", ["skipped", "carried"])
@pytest.mark.parametrize("target", ["running", "needs_you", "error", "submitted", "done", "not_started"])
def test_cloud_status_never_reopens_a_skipped_or_carried_task(store, loaded, repo, closed, target):
    _force(store, closed)
    put(repo, "a.json", [st("A", target, store.revision())])
    res = cloud_sync.import_inbox(store, repo)
    assert res["applied"] == 0 and res["conflicts"] == 1
    assert store.get_node("fx", "A")["status"] == closed
    why = kinds(store, "sync_conflict")[0]["data"]["reason"]
    assert closed in why and "fx/A" in why
    assert _no_conflict_todo(store)  # nothing for Owner to decide: the task is closed on purpose


def test_cloud_status_does_not_reopen_tasks_closed_by_a_real_rollover(store, loaded, repo):
    versions.new_version(store, project="fx", decisions={"B": {"action": "skip", "reason": "not wanted"}},
                         actor="owner", expected_revision=None)
    put(repo, "a.json", [st("A", "running", store.revision()), st("B", "running", store.revision())])
    res = cloud_sync.import_inbox(store, repo)
    assert res["applied"] == 0 and res["conflicts"] == 2
    assert store.get_node("fx", "A")["status"] == "carried" and store.get_node("fx", "B")["status"] == "skipped"


def test_cloud_status_never_changes_a_task_of_an_archived_project(store, loaded, repo):
    store.mutate(None, lambda c: c.execute("UPDATE projects SET archived=1 WHERE id='fx'"), "owner", "archive fx")
    put(repo, "a.json", [st("A", "running", store.revision())])
    res = cloud_sync.import_inbox(store, repo)
    assert res["applied"] == 0 and res["conflicts"] == 1
    assert store.get_node("fx", "A")["status"] == "not_started"
    assert "archived" in kinds(store, "sync_conflict")[0]["data"]["reason"]
    assert _no_conflict_todo(store)


def test_cloud_status_still_moves_open_tasks_of_a_live_project(store, loaded, repo):
    put(repo, "a.json", [st("A", "running", store.revision())])
    assert cloud_sync.import_inbox(store, repo)["applied"] == 1


# ------------------------------------------------------------------ finding 7 (and 5): the change check
def test_changed_since_does_not_load_the_event_log(store, loaded, monkeypatch):
    base = store.revision()
    store.set_node_status("fx", "A", "needs_you", "owner", summary="flagged")

    def boom(*a, **k):
        raise AssertionError("_changed_since must not read events in bulk")

    monkeypatch.setattr(store, "events", boom)
    assert cloud_sync._changed_since(store, "fx", "A", base) is True
    assert cloud_sync._changed_since(store, "fx", "A", store.revision()) is False
    assert cloud_sync._changed_since(store, "fx", "B", base) is False


def test_changed_since_keeps_revision_semantics(store, loaded):
    base = store.revision()
    assert cloud_sync._changed_since(store, "fx", "A", base) is False
    store.event("fx", "A", "note", "x", "a plain event with no revision")  # never counts
    assert cloud_sync._changed_since(store, "fx", "A", base) is False
    store.set_node_status("fx", "B", "needs_you", "owner")  # a different node
    assert cloud_sync._changed_since(store, "fx", "A", base) is False
    store.set_node_status("fx", "A", "needs_you", "owner")
    assert cloud_sync._changed_since(store, "fx", "A", base) is True
    assert cloud_sync._changed_since(store, "fx", "A", store.revision()) is False


def test_changed_since_counts_a_version_event_even_without_a_revision(store, loaded):
    base = store.revision()
    store.event("fx", "A", "version", "owner", "Owner carried task A.", {"to": "fx-v2", "action": "carry"})
    assert cloud_sync._changed_since(store, "fx", "A", base) is True
    assert cloud_sync._changed_since(store, "fx", "B", base) is False


def test_changed_since_sees_a_real_carry(store, loaded):
    base = store.revision()
    versions.new_version(store, project="fx", actor="owner", expected_revision=None)
    assert cloud_sync._changed_since(store, "fx", "A", base) is True


def test_changed_since_is_a_single_indexed_query(store, loaded):
    plan = store._q("EXPLAIN QUERY PLAN SELECT 1 FROM events WHERE project_id=? AND node_id=?", ("fx", "A"))
    assert any("USING" in str(tuple(r)) and "INDEX" in str(tuple(r)).upper() for r in plan)
    names = {r[0] for r in store._q("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "events_node" in names
