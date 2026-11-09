"""I2 clean task history: one plain line per moment that matters (prerequisites met, started,
interrupted, failed, completed) with time and who, oldest first; internal events left out."""
import http.client
import json
import threading

import pytest

from cc_engine import api, checks, claims, history, plan

KINDS = {"prereqs_met", "started", "interrupted", "failed", "completed"}


@pytest.fixture
def loaded(store, fixture_plan, tmp_path):
    fixture_plan["cwd"] = str(tmp_path)
    plan.import_plan(store, fixture_plan)
    return "fx"


def _pass(cmd, cwd, timeout):
    return 0, "fine", ""


def _fail(cmd, cwd, timeout):
    return 1, "", "boom"


def kinds(rows):
    return [r["kind"] for r in rows]


def test_node_history_empty_for_untouched_task(store, loaded):
    assert history.node_history(store, "fx", "B") == []
    assert history.node_history(store, "fx", "A") == []


def test_node_history_full_story_oldest_first(store, loaded, tmp_path):
    # A: claimed, then its check passes -> B's prerequisites are met
    claims.claim(store, "fx", "A", "worker-a")
    checks.complete_node(store, "fx", "A", runner=_pass)
    # B: scheduler starts it, internal noise, the check fails, restart, interrupted, then Owner marks it done
    store.set_node_status("fx", "B", "running", "scheduler", summary="Task B started on claude_code.")
    store.update_node("fx", "B", {"model": "sonnet"}, "failure", "Task B moved to a stronger model.")
    store.event("fx", "B", "swap_model", "failure", "Task B swapped model.")
    store.set_node_status("fx", "B", "submitted", "scheduler", summary="Task B finished its work.")
    checks.complete_node(store, "fx", "B", runner=_fail, cwd=str(tmp_path))
    store.set_node_status("fx", "B", "not_started", "failure", summary="Task B will try again.")
    store.set_node_status("fx", "B", "running", "scheduler", summary="Task B started again.")
    store.set_node_status("fx", "B", "not_started", "scheduler",
                          summary="Task B was stopped because the run was cancelled.")
    store.set_node_status("fx", "B", "done", "owner", summary="Owner set task B to done.")

    rows = history.node_history(store, "fx", "B")
    assert kinds(rows) == ["prereqs_met", "started", "failed", "started", "interrupted", "completed"]
    for r in rows:
        assert set(r) >= {"at", "kind", "text", "by"}
        assert r["kind"] in KINDS and r["at"]
        assert "\n" not in r["text"] and 0 < len(r["text"]) <= 140
    assert rows[0]["text"].startswith("Prerequisites done") and "A" in rows[0]["text"]
    assert rows[1]["by"] == "Scheduler"
    assert "check" in rows[2]["text"].lower()
    assert "cancelled" in rows[4]["text"]
    assert rows[-1]["by"] == "Owner"
    ats = [r["at"] for r in rows]
    assert ats == sorted(ats)  # oldest first
    # A's own history: started by the worker, completed by its check
    a = history.node_history(store, "fx", "A")
    assert kinds(a) == ["started", "completed"]
    assert a[0]["by"] == "worker-a" and a[1]["by"] == "Check"


def test_node_history_waits_for_every_prerequisite(store, loaded):
    store.set_node_status("fx", "A", "done", "owner")
    store.set_node_status("fx", "B", "done", "owner")
    assert "prereqs_met" not in kinds(history.node_history(store, "fx", "D"))  # C still open
    store.set_node_status("fx", "C", "done", "owner")
    rows = history.node_history(store, "fx", "D")
    assert kinds(rows) == ["prereqs_met"]
    assert all(d in rows[0]["text"] for d in ("A", "B", "C"))


def test_node_history_reopen_counts_as_interruption(store, loaded):
    store.set_node_status("fx", "A", "done", "owner")
    store.set_node_status("fx", "A", "not_started", "owner", summary="Owner set task A to not started.")
    assert kinds(history.node_history(store, "fx", "A")) == ["completed", "interrupted"]


def test_node_history_pure_function_without_store():
    node = {"id": "X", "deps": ["W"], "status": "running"}
    events = [
        {"seq": 1, "at": "2026-11-04T10:00:00+00:00", "node_id": "W", "kind": "status", "actor": "check:command",
         "summary": "Task W passed its command check and is done.", "data": {"status": "done"}},
        {"seq": 2, "at": "2026-11-04T10:01:00+00:00", "node_id": "X", "kind": "update", "actor": "scheduler",
         "summary": "internal", "data": {}},
        {"seq": 3, "at": "2026-11-04T10:02:00+00:00", "node_id": "X", "kind": "status", "actor": "owner",
         "summary": "Owner set task X to running.", "data": {"status": "running"}},
    ]
    rows = history.build_history(node, events, {"W": "done", "X": "running"})
    assert rows == [
        {"at": "2026-11-04T10:00:00+00:00", "kind": "prereqs_met", "text": "Prerequisites done: W", "by": "Check"},
        {"at": "2026-11-04T10:02:00+00:00", "kind": "started", "text": "Started by Owner", "by": "Owner"},
    ]


# ---------------------------------------------------------------- HTTP route
@pytest.fixture
def server(store, tmp_path):
    srv = api.make_server(store, host="127.0.0.1", port=0, usage_reader=lambda: [], heartbeat=0.5,
                          poll_interval=0.2, token_path=tmp_path / "engine.token")
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def get(srv, path):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = json.loads(resp.read() or b"null")
    conn.close()
    return resp, body


def test_node_history_route(server, store, loaded):
    assert "GET /api/node/history" in history.ROUTES
    store.set_node_status("fx", "A", "running", "scheduler")
    resp, body = get(server, "/api/node/history?project=fx&id=A")
    assert resp.status == 200
    assert kinds(body["history"]) == ["started"] and body["node"] == "A"
    resp, body = get(server, "/api/node/history?project=fx&id=NOPE")
    assert resp.status == 404
    resp, body = get(server, "/api/node/history?project=nope&id=A")
    assert resp.status == 404


def test_node_history_correction_returns_are_single_lines(store, loaded):
    store.set_node_status("fx", "A", "running", "scheduler")
    store.event("fx", "A", "loop", "checker", "Task A returned: UNIT parser\nVERDICT fail\nREASON the regex misses tabs.",
                {"kind": "returned", "unit": "parser"})
    store.event("fx", "A", "returned", "checker", "", {"reason": "Scope: only engine/x.py"})
    store.event("fx", "B", "returned", "checker", "Task B returned for its own fix.", {})
    rows = history.node_history(store, "fx", "A")
    assert [r["kind"] for r in rows] == ["started", "returned", "returned"]
    assert rows[1]["text"].startswith("Returned: UNIT parser VERDICT fail") and "\n" not in rows[1]["text"]
    assert rows[2]["text"] == "Scope: only engine/x.py" and rows[2]["by"] == "checker"


def test_node_history_failed_line_carries_the_doctor_cause(store, loaded, tmp_path):
    from cc_engine import failure_doctor
    store.set_node_status("fx", "A", "done", "owner")
    store.set_node_status("fx", "B", "running", "scheduler")
    res = checks.complete_node(store, "fx", "B", cwd=str(tmp_path))
    failure_doctor.on_check_failed(store, "fx", "B", res.detail)
    rows = history.node_history(store, "fx", "B")
    failed = [r for r in rows if r["kind"] == "failed"]
    assert len(failed) == 1 and failed[0]["text"] == "Failed: Missing file: left.txt"
    assert [r["kind"] for r in rows] == ["prereqs_met", "started", "failed"]


def test_node_history_fixup_dependency_counts_from_when_added(store, loaded, tmp_path):
    from cc_engine import failure_doctor
    store.set_node_status("fx", "A", "done", "owner")
    res = checks.complete_node(store, "fx", "B", cwd=str(tmp_path))
    out = failure_doctor.on_check_failed(store, "fx", "B", res.detail)
    store.set_node_status("fx", out["fixup"], "done", "owner")
    rows = history.node_history(store, "fx", "B")
    assert [r["kind"] for r in rows] == ["prereqs_met", "failed", "prereqs_met"]
    assert rows[-1]["text"] == "Prerequisites done: A, B-fix1"
