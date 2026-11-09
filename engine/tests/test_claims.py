"""C2 claims: a lead session or worker claims a task so it pulses blue, recording who works on it,
the worktree, cwd and environment. Claims never mark a task done."""
import http.client
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

from cc_engine import api, checks, claims, plan
from cc_engine.__main__ import main as cli_main
from cc_engine.store import NotAllowed, Store

ALLOWED = "http://127.0.0.1:3080"
T0 = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def loaded(store, fixture_plan, tmp_path):
    fixture_plan["cwd"] = str(tmp_path)
    plan.import_plan(store, fixture_plan)
    return "fx"


def board_nodes(store, pid="fx"):
    return {n["id"]: n for n in api.decode_nodes(api.build_board(store, pid))}


def _pass(cmd, cwd, timeout):
    return 0, "fine", ""


# ---------------------------------------------------------------- store-level behaviour
def test_claims_claim_sets_running_records_who_and_where(store, loaded):
    seen = []
    store.subscribe(lambda kind, payload: seen.append(kind))
    rev = store.revision()
    out = claims.claim(store, "fx", "A", "lead-session", worktree="wt-meridian", cwd="C:/repo",
                       environment="Claude Code desktop", now=T0)
    node = store.get_node("fx", "A")
    assert node["status"] == "running" and node["actor"] == "lead-session"
    assert store.revision() == rev + 1 and "state" in seen  # SSE state goes out live
    assert out["claim"]["by"] == "lead-session" and out["claim"]["stale"] is False
    b = board_nodes(store)
    assert "claim" in api.BOARD_FIELDS
    c = b["A"]["claim"]
    assert {k: c[k] for k in ("by", "worktree", "cwd", "environment")} == {
        "by": "lead-session", "worktree": "wt-meridian", "cwd": "C:/repo", "environment": "Claude Code desktop"}
    assert c["since"] == T0.isoformat(timespec="seconds")
    assert b["A"]["display_status"] == "running" and b["B"]["claim"] is None
    ev = store.node_events("fx", "A")[-1]
    assert ev["actor"] == "lead-session" and "lead-session" in ev["summary"]


def test_claims_release_returns_to_derived_status(store, loaded):
    claims.claim(store, "fx", "A", "worker-c", now=T0)
    out = claims.release(store, "fx", "A", by="worker-c")
    assert out["released"] is True
    node = store.get_node("fx", "A")
    assert node["status"] == "not_started" and node["actor"] == "worker-c"
    b = board_nodes(store)
    assert b["A"]["display_status"] == "ready" and b["A"]["claim"] is None


def test_claims_release_keeps_a_check_completion(store, loaded):
    claims.claim(store, "fx", "A", "worker-c", now=T0)
    checks.complete_node(store, "fx", "A", runner=_pass)
    assert store.get_node("fx", "A")["status"] == "done"
    assert board_nodes(store)["A"]["claim"] is None  # a finished task shows no live claim
    claims.release(store, "fx", "A", by="worker-c")
    assert store.get_node("fx", "A")["status"] == "done"


def test_claims_never_mark_done(store, loaded):
    store.set_node_status("fx", "A", "done", "owner")
    with pytest.raises(NotAllowed):
        claims.claim(store, "fx", "A", "lead-session")
    claims.claim(store, "fx", "B", "lead-session", now=T0)
    claims.release(store, "fx", "B", by="lead-session")
    statuses = [e["data"].get("status") for e in store.node_events("fx", "B") if e["kind"] == "status"]
    assert "done" not in statuses
    for bad in ("owner", "check:command", ""):
        with pytest.raises((ValueError, NotAllowed)):
            claims.claim(store, "fx", "C", bad)


def test_claims_stale_after_two_hours_and_refresh(store, loaded):
    claims.claim(store, "fx", "A", "lead-session", now=T0)
    assert claims.claims_for(store, "fx", now=T0 + timedelta(minutes=110))["A"]["stale"] is False
    assert claims.claims_for(store, "fx", now=T0 + timedelta(hours=2, minutes=5))["A"]["stale"] is True
    # someone else cannot take a fresh claim
    with pytest.raises(claims.ClaimConflict):
        claims.claim(store, "fx", "A", "worker-b", now=T0 + timedelta(minutes=30))
    # the same claimer refreshes it: fresh again, since kept, status untouched
    rev = store.revision()
    claims.claim(store, "fx", "A", "lead-session", environment="new env", now=T0 + timedelta(hours=3))
    c = claims.claims_for(store, "fx", now=T0 + timedelta(hours=3, minutes=1))["A"]
    assert c["stale"] is False and c["since"] == T0.isoformat(timespec="seconds") and c["environment"] == "new env"
    assert store.revision() == rev
    # a stale claim may be taken over, truthfully recorded
    claims.claim(store, "fx", "A", "worker-b", now=T0 + timedelta(hours=6))
    assert store.get_node("fx", "A")["actor"] == "worker-b"
    assert claims.claims_for(store, "fx", now=T0 + timedelta(hours=6))["A"]["by"] == "worker-b"
    assert store.get_node("fx", "A")["status"] == "running"


def test_claims_refused_in_a_draft_and_for_a_task_already_running(store, fixture_plan):
    plan.import_plan(store, fixture_plan, actor="router")  # router import lands as a draft
    with pytest.raises(NotAllowed):
        claims.claim(store, "fx", "A", "lead-session")
    plan.accept_plan(store, "fx", "owner")
    store.set_node_status("fx", "A", "running", "scheduler:test")
    with pytest.raises(claims.ClaimConflict):
        claims.claim(store, "fx", "A", "lead-session")
    with pytest.raises(KeyError):
        claims.claim(store, "fx", "nope", "lead-session")


# ---------------------------------------------------------------- HTTP routes
@pytest.fixture
def server(store, tmp_path):
    srv = api.make_server(store, host="127.0.0.1", port=0, usage_reader=lambda: [], heartbeat=0.5,
                          poll_interval=0.2, token_path=tmp_path / "engine.token")
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def call(srv, method, path, body=None, origin=None, token=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    headers = {}
    if token:
        headers["X-CC-Token"] = token
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if origin:
        headers["Origin"] = origin
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    payload = resp.read()
    conn.close()
    return resp, (json.loads(payload) if payload else None)


def test_claims_routes_router_token_and_owner(server, store, loaded):
    tok = server.app.token
    resp, _ = call(server, "POST", "/api/claim", {"project": "fx", "node": "A", "by": "worker-c"})
    assert resp.status == 403  # no credential
    resp, body = call(server, "POST", "/api/claim", {"project": "fx", "node": "A", "by": "worker-c",
                                                     "worktree": "wt-c", "cwd": "C:/wt-c", "environment": "codex"},
                      token=tok)
    assert resp.status == 200 and body["claim"]["by"] == "worker-c"
    assert store.get_node("fx", "A")["actor"] == "worker-c"
    resp, body = call(server, "GET", "/api/node?project=fx&id=A")
    assert body["node"]["claim"]["worktree"] == "wt-c" and body["node"]["claim"]["environment"] == "codex"
    resp, body = call(server, "GET", "/api/claims?project=fx")
    assert resp.status == 200 and list(body["claims"]) == ["A"]
    # the router may claim but never mark done
    rev = store.revision()
    resp, _ = call(server, "POST", "/api/node/status", {"project": "fx", "id": "A", "status": "done", "revision": rev},
                   token=tok)
    assert resp.status == 403
    resp, body = call(server, "POST", "/api/release", {"project": "fx", "node": "A"}, token=tok)
    assert resp.status == 200 and body["released"] is True
    assert store.get_node("fx", "A")["status"] == "not_started"
    # Owner (dsh page) may claim and release too
    resp, body = call(server, "POST", "/api/claim", {"project": "fx", "node": "B", "by": "lead-session"}, origin=ALLOWED)
    assert resp.status == 200
    resp, body = call(server, "POST", "/api/claim", {"project": "fx", "node": "B", "by": "worker-b"}, token=tok)
    assert resp.status == 409
    resp, body = call(server, "POST", "/api/claim", {"project": "fx", "node": "C", "by": "owner"}, token=tok)
    assert resp.status in (400, 403)
    resp, body = call(server, "POST", "/api/release", {"project": "fx", "node": "B"}, origin=ALLOWED)
    assert resp.status == 200 and store.get_node("fx", "B")["status"] == "not_started"


# ---------------------------------------------------------------- CLI
def test_claims_cli_claim_and_release(tmp_path, fixture_plan, capsys):
    db = tmp_path / "cli.sqlite"
    s = Store(db)
    plan.import_plan(s, fixture_plan)
    s.close()
    code = cli_main(["claim", "--project", "fx", "--node", "A", "--by", "lead-session", "--worktree", "wt-x",
                     "--cwd", "C:/wt-x", "--env", "Claude Code desktop", "--db", str(db)])
    assert code == 0
    assert "lead-session" in capsys.readouterr().out
    s = Store(db)
    try:
        node = s.get_node("fx", "A")
        assert node["status"] == "running" and node["actor"] == "lead-session"
        assert claims.claims_for(s, "fx")["A"]["worktree"] == "wt-x"
    finally:
        s.close()
    assert cli_main(["release", "--project", "fx", "--node", "A", "--db", str(db)]) == 0
    assert cli_main(["claim", "--project", "fx", "--node", "missing", "--by", "x", "--db", str(db)]) != 0
    s = Store(db)
    try:
        assert s.get_node("fx", "A")["status"] == "not_started"
        assert claims.claims_for(s, "fx") == {}
    finally:
        s.close()
