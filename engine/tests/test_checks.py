import sys

import pytest

from cc_engine import checks, plan
from cc_engine.store import NotAllowed

PY = '"%s"' % sys.executable


def test_checks_command_exit_code(tmp_path):
    assert checks.run_check({"type": "command", "run": "exit 0"}, tmp_path).ok
    assert not checks.run_check({"type": "command", "run": "exit 3"}, tmp_path).ok


def test_checks_command_expect_stdout(tmp_path):
    good = {"type": "command", "run": PY + " -c \"print('true')\"", "expect_stdout": "true"}
    bad = {"type": "command", "run": PY + " -c \"print('false')\"", "expect_stdout": "true"}
    assert checks.run_check(good, tmp_path).ok
    res = checks.run_check(bad, tmp_path)
    assert not res.ok and "expected" in res.detail


def test_checks_command_runner_is_injectable(tmp_path):
    calls = []

    def runner(cmd, cwd, timeout):
        calls.append((cmd, str(cwd)))
        return 0, "fine\n", ""

    assert checks.run_check({"type": "command", "run": "anything"}, tmp_path, runner=runner).ok
    assert calls == [("anything", str(tmp_path))]


def test_checks_files_exist_and_text_present(tmp_path):
    (tmp_path / "a.txt").write_text("hello ok world", encoding="utf-8")
    assert checks.run_check({"type": "files_exist", "paths": ["a.txt"]}, tmp_path).ok
    assert not checks.run_check({"type": "files_exist", "paths": ["a.txt", "b.txt"]}, tmp_path).ok
    assert not checks.run_check({"type": "files_exist", "paths": []}, tmp_path).ok
    assert checks.run_check({"type": "text_present", "path": "a.txt", "text": "ok"}, tmp_path).ok
    assert not checks.run_check({"type": "text_present", "path": "a.txt", "text": "nope"}, tmp_path).ok
    assert not checks.run_check({"type": "text_present", "path": "zz.txt", "text": "ok"}, tmp_path).ok


def test_checks_human_never_auto_passes(tmp_path):
    res = checks.run_check({"type": "human", "prompt": "Owner has looked"}, tmp_path)
    assert res.ok is False
    res = checks.run_check({"type": "human", "prompt": "x", "ok": True, "passed": True}, tmp_path)
    assert res.ok is False


def test_checks_unknown_type_fails(tmp_path):
    assert not checks.run_check({"type": "vibes"}, tmp_path).ok
    assert not checks.run_check({}, tmp_path).ok


def _imported(store, fixture_plan, tmp_path):
    fixture_plan["cwd"] = str(tmp_path)
    plan.import_plan(store, fixture_plan)
    return "fx"


def _deps_done(store, pid, node_id):
    """Owner marks every prerequisite done so the node's own check is the only variable."""
    for dep in store.get_node(pid, node_id).get("deps") or []:
        _deps_done(store, pid, dep)
        if store.get_node(pid, dep)["status"] != "done":
            store.set_node_status(pid, dep, "done", actor="owner", evidence="test setup")


def test_checks_complete_node_sets_done_with_truthful_actor(store, fixture_plan, tmp_path):
    pid = _imported(store, fixture_plan, tmp_path)
    res = checks.complete_node(store, pid, "A")
    assert res.ok
    node = store.get_node(pid, "A")
    assert node["status"] == "done"
    assert node["actor"] == "check:command"
    assert node["evidence"]


def test_checks_complete_node_failure_is_error_not_done(store, fixture_plan, tmp_path):
    pid = _imported(store, fixture_plan, tmp_path)
    _deps_done(store, pid, "B")
    res = checks.complete_node(store, pid, "B")
    assert not res.ok
    node = store.get_node(pid, "B")
    assert node["status"] == "error"
    assert node["actor"] == "check:files_exist"


def test_checks_complete_node_human_goes_to_needs_you(store, fixture_plan, tmp_path):
    pid = _imported(store, fixture_plan, tmp_path)
    _deps_done(store, pid, "E")
    res = checks.complete_node(store, pid, "E")
    assert not res.ok
    assert store.get_node(pid, "E")["status"] == "needs_you"


def test_checks_only_check_or_owner_may_set_done(store, fixture_plan, tmp_path):
    pid = _imported(store, fixture_plan, tmp_path)
    for actor in ("agent:claude", "scheduler", "check:human", "check:vibes", "Owner ", "owner:agent"):
        with pytest.raises(NotAllowed):
            store.set_node_status(pid, "C", "done", actor=actor, evidence="trust me")
    with pytest.raises(NotAllowed):
        store.update_node(pid, "C", {"status": "done"}, actor="scheduler", summary="x")
    assert store.get_node(pid, "C")["status"] == "not_started"
    store.set_node_status(pid, "C", "done", actor="owner", evidence="Owner checked it")
    node = store.get_node(pid, "C")
    assert node["status"] == "done" and node["actor"] == "owner"


def test_checks_non_done_status_changes_are_allowed(store, fixture_plan, tmp_path):
    pid = _imported(store, fixture_plan, tmp_path)
    store.set_node_status(pid, "A", "running", actor="scheduler")
    assert store.get_node(pid, "A")["status"] == "running"
    with pytest.raises(ValueError):
        store.set_node_status(pid, "A", "finished", actor="scheduler")


def test_complete_node_waits_for_dependencies_and_probe_keeps_status(tmp_path):
    from cc_engine.store import Store
    from cc_engine.plan import import_plan
    from cc_engine.checks import complete_node
    store = Store(tmp_path / "cc.sqlite")
    import_plan(store, {"id": "p", "title": "p", "cwd": str(tmp_path), "nodes": [
        {"id": "a", "title": "a", "goal": "a", "check": {"type": "human"}, "deps": []},
        {"id": "b", "title": "b", "goal": "b", "check": {"type": "files_exist", "paths": ["."]}, "deps": ["a"]},
        {"id": "c", "title": "c", "goal": "c", "check": {"type": "files_exist", "paths": ["missing"]}, "deps": []}]})
    res = complete_node(store, "p", "b")
    assert not res.ok and "waiting on a" in res.detail
    assert store.get_node("p", "b")["status"] == "not_started"
    complete_node(store, "p", "c", record_failure=False)
    assert store.get_node("p", "c")["status"] == "not_started"
    store.close()


def test_integrity_complete_node_refused_in_draft_project(store, tmp_path):
    plan.import_plan(store, {"id": "dr", "title": "Draft", "brief": "b", "cwd": str(tmp_path), "nodes": [
        {"id": "a", "goal": "g", "check": {"type": "command", "run": "exit 0"}, "deps": []}]}, actor="router")
    with pytest.raises(NotAllowed):
        checks.complete_node(store, "dr", "a")
    assert store.get_node("dr", "a")["status"] == "not_started"
    store.accept_project("dr", "owner")
    assert checks.complete_node(store, "dr", "a").ok
    assert store.get_node("dr", "a")["status"] == "done"
