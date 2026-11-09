"""F2 checks and failure doctor: from a goal, draft a machine-checkable done-check (command,
files_exist or text_present, never 'looks good'); on a failed check, write a one-line cause and
add a fix-up task within the goal. Pure functions plus an injectable model runner (fakes only)."""
import json

import pytest

from cc_engine import checks, failure_doctor as FD, plan
from cc_engine.store import goal_hash

VAGUE = ("looks good", "seems fine", "works", "verify manually")


def assert_machine(check):
    assert FD.is_machine_checkable(check), check
    assert check["type"] in ("command", "files_exist", "text_present")
    assert not any(v in json.dumps(check).lower() for v in VAGUE)


# ---------------------------------------------------------------- drafting
def test_failure_doctor_drafts_command_from_goal():
    c = FD.draft_check("Add the clean history; `python -m pytest engine/tests -k node_history -q` passes.")
    assert c == {"type": "command", "run": "python -m pytest engine/tests -k node_history -q"}
    c = FD.draft_check("Make `node plugin/tests/send_to_labels.test.mjs` pass with Run and Open labels.")
    assert c == {"type": "command", "run": "node plugin/tests/send_to_labels.test.mjs"}


def test_failure_doctor_drafts_text_present_and_files_exist():
    c = FD.draft_check("README.md mentions `Waypoint` as the app name.")
    assert c == {"type": "text_present", "path": "README.md", "text": "Waypoint"}
    c = FD.draft_check("Create engine/cc_engine/history.py and engine/tests/test_node_history.py.")
    assert c == {"type": "files_exist", "paths": ["engine/cc_engine/history.py", "engine/tests/test_node_history.py"]}


def test_failure_doctor_no_signal_still_drafts_a_runnable_check():
    c = FD.draft_check("Make the sidebar calmer for Owner.")
    assert_machine(c)
    assert c["type"] == "command" and "pytest" in c["run"] and "-k sidebar" in c["run"]
    assert_machine(FD.draft_check(""))


def test_failure_doctor_uses_a_valid_model_draft():
    seen = []

    def runner(prompt):
        seen.append(prompt)
        return 'Here you go:\n```json\n{"type": "text_present", "path": "docs/a.md", "text": "Ready"}\n```'
    c = FD.draft_check("Docs say Ready.", runner=runner)
    assert c == {"type": "text_present", "path": "docs/a.md", "text": "Ready"}
    assert "Docs say Ready." in seen[0] and "looks good" in seen[0].lower()  # the prompt forbids it


@pytest.mark.parametrize("reply", [
    '{"type": "human", "prompt": "Owner says it looks good"}',
    '{"type": "command", "run": "echo looks good"}',
    '{"type": "command", "run": "exit 0"}',
    '{"type": "command", "run": "true"}',
    '{"type": "files_exist", "paths": []}',
    '{"type": "text_present", "path": "a.md"}',
    'Looks good to me!',
    'not json at all',
])
def test_failure_doctor_rejects_vague_or_broken_model_drafts(reply):
    c = FD.draft_check("Run `python -m pytest engine/tests -k thing -q` until it passes.", runner=lambda p: reply)
    assert c == {"type": "command", "run": "python -m pytest engine/tests -k thing -q"}


def test_failure_doctor_model_errors_fall_back():
    def boom(prompt):
        raise RuntimeError("bridge down")
    assert_machine(FD.draft_check("Create plugin/src/x.js.", runner=boom))


def test_failure_doctor_is_machine_checkable():
    assert FD.is_machine_checkable({"type": "command", "run": "python -m pytest -q"})
    assert FD.is_machine_checkable({"type": "files_exist", "paths": ["a.txt"]})
    assert FD.is_machine_checkable({"type": "text_present", "path": "a.txt", "text": "ok"})
    for bad in [None, {}, {"type": "human", "prompt": "x"}, {"type": "command", "run": ""},
                {"type": "command", "run": "echo done"}, {"type": "text_present", "path": "a", "text": "looks good"},
                {"type": "command", "run": "rem looks good"}, {"type": "files_exist", "paths": [""]}]:
        assert not FD.is_machine_checkable(bad), bad


# ---------------------------------------------------------------- one-line cause
@pytest.mark.parametrize("detail,expect", [
    ("missing: left.txt, b/c.txt", "Missing files: left.txt, b/c.txt"),
    ("right.txt does not contain 'ok'", "right.txt does not contain 'ok'"),
    ("exit 1: python -m pytest engine/tests -k x -q\n....F\nE   AssertionError: assert 3 == 4\n1 failed in 0.2s",
     "`python -m pytest engine/tests -k x -q` failed: AssertionError: assert 3 == 4"),
    ("command timed out after 1800s: npm test", "The check timed out after 1800s: npm test"),
    ("", "The check failed without saying why"),
])
def test_failure_doctor_cause_heuristics(detail, expect):
    cause = FD.diagnose({"id": "B", "goal": "g", "check": {"type": "command", "run": "x"}}, detail)
    assert cause == expect
    assert "\n" not in cause and len(cause) <= FD.CAUSE_LIMIT


def test_failure_doctor_cause_from_model_is_one_line_or_falls_back():
    node = {"id": "B", "goal": "g"}
    out = FD.diagnose(node, "missing: a.txt", runner=lambda p: "The file a.txt was never written.\nMore detail here.")
    assert out == "The file a.txt was never written."
    assert FD.diagnose(node, "missing: a.txt", runner=lambda p: "Looks good") == "Missing file: a.txt"
    assert FD.diagnose(node, "missing: a.txt", runner=lambda p: "   ") == "Missing file: a.txt"
    long = FD.diagnose(node, "missing: a.txt", runner=lambda p: "x" * 500)
    assert len(long) <= FD.CAUSE_LIMIT and long.endswith("…")


# ---------------------------------------------------------------- fix-up within the goal
@pytest.fixture
def loaded(store, fixture_plan, tmp_path):
    fixture_plan["cwd"] = str(tmp_path)
    plan.import_plan(store, fixture_plan)
    return "fx"


def test_failure_doctor_adds_a_fixup_within_the_goal(store, loaded, tmp_path):
    store.set_node_status("fx", "A", "done", "owner")
    res = checks.complete_node(store, "fx", "B", cwd=str(tmp_path))  # left.txt is missing
    assert not res.ok and store.get_node("fx", "B")["status"] == "error"
    before = store.get_node("fx", "B")
    out = FD.on_check_failed(store, "fx", "B", res.detail)
    assert out["cause"] == "Missing file: left.txt"
    fid = out["fixup"]
    assert fid == "B-fix1"
    fix = store.get_node("fx", fid)
    after = store.get_node("fx", "B")
    assert after["goal"] == before["goal"] and after["goal_hash"] == goal_hash(before["goal"])  # goal untouched
    assert fid in after["deps"] and fix["deps"] == before["deps"]
    assert "Build the left side." in fix["goal"] and "Missing file: left.txt" in fix["goal"]
    assert fix["check"] == before["check"]  # the fix-up is done when the task's own check passes
    assert fix["title"].startswith("Fix: ") and len(fix["title"]) <= 80
    assert after["status"] == "not_started"
    ev = [e for e in store.node_events("fx", "B", 20) if e["kind"] == "diagnosis"]
    assert len(ev) == 1 and ev[0]["summary"] == "Task B failed: Missing file: left.txt" and ev[0]["data"]["fixup"] == fid


def test_failure_doctor_human_check_gets_a_drafted_check(store, loaded):
    out = FD.on_check_failed(store, "fx", "E", "Needs Owner: Owner has looked")
    fix = store.get_node("fx", out["fixup"])
    assert_machine(fix["check"])


def test_failure_doctor_caps_fixups(store, loaded):
    for i in range(FD.MAX_FIXUPS):
        assert FD.on_check_failed(store, "fx", "C", "right.txt does not contain 'ok'")["fixup"] == f"C-fix{i + 1}"
    out = FD.on_check_failed(store, "fx", "C", "right.txt does not contain 'ok'")
    assert out["fixup"] is None and out["cause"]
    assert store.get_node("fx", f"C-fix{FD.MAX_FIXUPS + 1}") is None


def test_failure_doctor_route_drafts_without_state_change(store):
    class H:
        actor = "router"

        def __init__(self, body):
            self.body = body

        def _json_body(self):
            return self.body

    rev = store.revision()
    out = FD.ROUTES["POST /api/check/draft"](H({"goal": "Make `npm test` pass."}), {})
    assert out == {"check": {"type": "command", "run": "npm test"}, "machine_checkable": True}
    assert store.revision() == rev
