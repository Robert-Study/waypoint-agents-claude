import io
import json
import threading

import pytest

from cc_engine import api, mcp_server as m


class FakeHttp:
    """Fake HTTP layer: routes (method, path-without-query) to canned (status, body); records calls."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls = []

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        key = (method, path.split("?")[0])
        val = self.routes.get(key)
        if val is None:
            return 404, {"error": "not_found"}
        if callable(val):
            return val(path, body)
        if isinstance(val, list):  # sequence of responses
            return val.pop(0) if len(val) > 1 else val[0]
        return val

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


def node(nid, deps=(), check=None, **extra):
    return dict({"id": nid, "title": f"Task {nid}", "goal": f"Do {nid}.",
                 "check": check or {"type": "command", "run": "exit 0"}, "deps": list(deps)}, **extra)


def draft_args(nodes=None, **extra):
    return dict({"title": "Ship the widget", "brief": "Build and test the widget.",
                 "nodes": nodes or [node("a"), node("b", ["a"], {"type": "files_exist", "paths": ["w.txt"]}),
                                    node("c", ["a", "b"], {"type": "human", "prompt": "Owner looks"})]},
                **extra)


def ok_http(over=None):
    routes = {("GET", "/api/projects"): (200, {"projects": []}), ("GET", "/health"): (200, {"revision": 4}),
              ("POST", "/api/plan/import"): lambda p, b: (200, {"ok": True, "project": b["plan"]["id"], "revision": 5})}
    routes.update(over or {})
    return FakeHttp(routes)


def tool(http, name, args):
    resp = m.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": name, "arguments": args}}, http)
    res = resp["result"]
    return res["isError"], res["content"][0]["text"]


# ------------------------------------------------------------------ protocol
def test_exactly_four_short_tools_no_answer_and_no_run_start_tool():
    resp = m.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = [t["name"] for t in resp["result"]["tools"]]
    assert names == ["cc_draft_plan", "cc_board", "cc_needs_you", "cc_where"]
    assert not any("answer" in n for n in names)
    assert not any("run" in n for n in names)  # runs start only from the Sprints tab
    assert all(len(t["description"]) <= 120 for t in resp["result"]["tools"])
    assert len(json.dumps(resp["result"]["tools"])) < 3200  # token cost of the whole schema


def test_initialize_ping_notifications_and_unknown_method():
    init = m.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}})
    assert init["result"]["protocolVersion"] == "2025-03-26"
    assert init["result"]["serverInfo"]["name"] == "rob-control-centre"
    assert m.handle({"jsonrpc": "2.0", "id": 2, "method": "ping"})["result"] == {}
    assert m.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert m.handle({"jsonrpc": "2.0", "id": 3, "method": "nope"})["error"]["code"] == -32601
    err, text = tool(FakeHttp(), "cc_answer_needs", {"id": "q-1", "answer": 3})
    assert err and "Unknown tool" in text


def test_main_loop_reads_lines_and_writes_responses():
    lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}), "not json", "",
             json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})]
    out = io.BytesIO()
    m.main(stdin=io.BytesIO(("\n".join(lines) + "\n").encode()), stdout=out, http=FakeHttp())
    resps = [json.loads(x) for x in out.getvalue().decode().splitlines()]
    assert resps[0] == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert resps[1]["error"]["code"] == -32700 and len(resps) == 2


# ------------------------------------------------------------------ cc_draft_plan
@pytest.mark.parametrize("bad_nodes, msg", [
    ([node("a", check={"type": "vibes"})], "check of type"),
    ([dict(node("a"), check=None)], "check of type"),
    ([node("a", check={"type": "command"})], "run"),
    ([node("a", check={"type": "files_exist", "paths": []})], "paths"),
    ([node("a", check={"type": "text_present", "path": "x"})], "text"),
    ([node("a", check={"type": "human"})], "prompt"),
    ([node("a"), node("b", ["zz"])], "unknown"),
    ([node("a", ["b"]), node("b", ["a"])], "cycle"),
    ([node("a"), node("a")], "unique"),
    ([dict(node("a"), deps=None)], "deps"),
    ([dict(node("a"), goal=" ")], "goal"),
    ([node("a", pool="codex")], "pool"),
    ([], "non-empty"),
])
def test_draft_plan_rejects_bad_nodes_without_posting(bad_nodes, msg):
    http = ok_http()
    err, text = tool(http, "cc_draft_plan", draft_args(nodes=bad_nodes) if bad_nodes else dict(draft_args(), nodes=[]))
    assert err and msg in text.lower(), text
    assert http.posts() == []


def test_draft_plan_posts_inline_dict_with_current_revision():
    http = ok_http()
    err, text = tool(http, "cc_draft_plan", draft_args(nodes=None, cwd="C:/w"))
    assert not err, text
    assert text.splitlines()[0] == "project=ship-the-widget"
    assert "3 tasks, 1 ready once accepted, 3 waves" in text and "1 command, 1 files_exist, 1 human" in text
    (_, path, body), = http.posts()
    assert path == "/api/plan/import" and body["revision"] == 4 and "path" not in body
    p = body["plan"]
    assert p["id"] == "ship-the-widget" and p["brief"] and p["cwd"] == "C:/w"
    assert [n["id"] for n in p["nodes"]] == ["a", "b", "c"] and p["nodes"][2]["deps"] == ["a", "b"]


def test_draft_plan_keeps_optional_pool_and_explicit_id():
    http = ok_http()
    err, text = tool(http, "cc_draft_plan", draft_args(nodes=[node("a", pool="freebuff")], id="my-proj"))
    assert not err and "project=my-proj" in text
    assert http.posts()[0][2]["plan"]["nodes"][0]["pool"] == "freebuff"


def test_draft_plan_never_overwrites_an_existing_project():
    http = ok_http({("GET", "/api/projects"): (200, {"projects": [{"id": "ship-the-widget"}]})})
    err, text = tool(http, "cc_draft_plan", draft_args())
    assert err and "already exists" in text and http.posts() == []


def test_draft_plan_retries_once_on_stale_revision():
    revs = iter([4, 5])
    http = ok_http({("GET", "/health"): lambda p, b: (200, {"revision": next(revs)}),
                      ("POST", "/api/plan/import"): [(409, {"error": "stale", "revision": 5}),
                                                     (200, {"project": "ship-the-widget", "revision": 6})]})
    err, text = tool(http, "cc_draft_plan", draft_args())
    assert not err, text
    assert [c[2]["revision"] for c in http.posts()] == [4, 5]


def test_draft_plan_surfaces_engine_errors():
    http = ok_http({("POST", "/api/plan/import"): (400, {"error": "bad_plan", "detail": "boom"})})
    err, text = tool(http, "cc_draft_plan", draft_args())
    assert err and "400" in text and "boom" in text


# ------------------------------------------------------------------ cc_board
def board_payload(n=6):
    statuses = ["ready", "running", "error", "needs_you", "done", "not_started"]
    rows = []
    for i in range(n):
        s = statuses[i % len(statuses)]
        rows.append([f"node-{i:03d}", f"T{i}", s, s, [], 0, None, None, None, 0, None, None])
    counts = {s: sum(1 for r in rows if r[3] == s) for s in statuses}
    counts["total"] = n
    return {"project": {"id": "p1", "title": "Proj"}, "revision": 9, "fields": list(api.BOARD_FIELDS),
            "nodes": rows, "edges": [], "counts": counts}


def test_board_compact_summary():
    http = FakeHttp({("GET", "/api/board"): (200, board_payload(6))})
    err, text = tool(http, "cc_board", {"project": "p1"})
    assert not err
    assert http.calls[0][1] == "/api/board?project=p1"
    lines = text.splitlines()
    assert lines[0].startswith("Proj (p1) rev 9 | 6 tasks: ready 1, running 1, error 1, needs_you 1")
    assert "ready: node-000" in lines and "running: node-001" in lines
    assert "error: node-002" in lines and "needs_you: node-003" in lines
    assert "node-004" not in text  # done ids are not listed


def test_board_stays_under_1500_chars_at_300_nodes():
    http = FakeHttp({("GET", "/api/board"): (200, board_payload(300))})
    err, text = tool(http, "cc_board", {"project": "p1"})
    assert not err and len(text) <= 1500
    assert "more)" in text and "ready: node-000" in text and "needs_you:" in text


def test_board_unknown_project():
    http = FakeHttp({("GET", "/api/board"): (404, {"error": "not_found", "detail": "no such project"})})
    err, text = tool(http, "cc_board", {"project": "zz"})
    assert err and "404" in text and "no such project" in text


# ------------------------------------------------------------------ no run start
def test_router_cannot_start_runs():
    """Owner, 4 Nov 2026: runs start only from the Sprints tab, so the tool does not exist."""
    http = FakeHttp({("POST", "/api/run"): (200, {"ok": True, "id": "run-1"})})
    err, text = tool(http, "cc_start_run", {"project": "p1", "preset": "balanced", "pools": ["freebuff"]})
    assert err and "Unknown tool" in text and http.calls == []
    assert not hasattr(m, "start_run") and "cc_start_run" not in m.HANDLERS
    assert "/api/run" not in open(m.__file__, encoding="utf-8").read()


# ------------------------------------------------------------------ cc_needs_you
def test_needs_you_is_read_only_and_compact():
    qs = [{"id": "q-1", "project_id": "p1", "node_id": "a", "kind": "likert", "scale": 5, "prompt": "How sure?"},
          {"id": "q-2", "project_id": "p1", "node_id": None, "kind": "select", "prompt": "Pick\n one",
           "options": [{"value": 1, "label": "One"}, {"value": 2, "label": "Two"}]},
          {"id": "q-3", "project_id": None, "node_id": None, "kind": "text", "prompt": "x" * 5000}]
    http = FakeHttp({("GET", "/api/questions"): (200, {"questions": qs})})
    err, text = tool(http, "cc_needs_you", {"project": "p1"})
    assert not err and len(text) <= 1500
    assert http.calls[0][1] == "/api/questions?open=1&project=p1" and http.posts() == []
    assert "- q-1 p1/a: How sure? [1-5]" in text and "- q-2 p1: Pick one [One / Two]" in text


def test_needs_you_empty():
    http = FakeHttp({("GET", "/api/questions"): (200, {"questions": []})})
    assert tool(http, "cc_needs_you", {}) == (False, "No open questions.")


def test_unreachable_engine_is_a_tool_error():
    http = m.UrlHttp("http://127.0.0.1:9", timeout=0.5)
    err, text = tool(http, "cc_needs_you", {})
    assert err and "not reachable" in text


# ------------------------------------------------------------------ end to end against a real engine
def test_end_to_end_against_live_engine(store, tmp_path):
    srv = api.make_server(store, host="127.0.0.1", port=0, usage_reader=lambda: [], poll_interval=0)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        http = m.UrlHttp(base, token_path=str(tmp_path / "engine.token"))
        err, text = tool(http, "cc_draft_plan", draft_args(cwd=str(tmp_path)))
        assert not err, text
        assert "draft" in text.lower()
        assert store.get_project("ship-the-widget")["title"] == "Ship the widget"
        assert store.project_status("ship-the-widget") == "draft"
        assert store.project_runnable("ship-the-widget") is False
        err, text = tool(http, "cc_board", {"project": "ship-the-widget"})
        assert not err and "DRAFT" in text and "3 tasks: not_started 3" in text
        store.accept_project("ship-the-widget", "owner")
        err, text = tool(http, "cc_board", {"project": "ship-the-widget"})
        assert not err and "3 tasks: ready 1, not_started 2" in text and "ready: a" in text
        err, text = tool(http, "cc_draft_plan", draft_args())
        assert err and "already exists" in text
        err, text = tool(http, "cc_start_run", {"project": "ship-the-widget", "preset": "frugal",
                                                "pools": ["freebuff"]})
        assert err and "Unknown tool" in text
        assert not [e for e in store.events(project_id="ship-the-widget") if e["kind"] == "run"]
        err, text = tool(http, "cc_needs_you", {"project": "ship-the-widget"})
        assert (err, text) == (False, "No open questions.")
    finally:
        srv.shutdown()
        srv.server_close()


def test_router_runs_are_not_stamped_as_owner(tmp_path):
    import inspect
    from cc_engine import api
    assert 'b.get("requested_by")' not in inspect.getsource(api.p_run)  # actor comes from the credential
    assert '"requested_by"' not in inspect.getsource(m)


def _live(store):
    srv = api.make_server(store, host="127.0.0.1", port=0, usage_reader=lambda: [], poll_interval=0)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return srv


def test_security_urlhttp_sends_token_and_engine_records_router(store, tmp_path):
    srv = _live(store)
    try:
        http = m.UrlHttp(f"http://127.0.0.1:{srv.server_address[1]}", token_path=str(tmp_path / "engine.token"))
        err, text = tool(http, "cc_draft_plan", draft_args())
        assert not err, text
        imp =[e for e in store.events(project_id="ship-the-widget") if e["kind"] == "import"][-1]
        assert imp["actor"] == "router"
    finally:
        srv.shutdown()
        srv.server_close()


def test_security_urlhttp_without_token_file_is_a_tool_error(store, tmp_path):
    srv = _live(store)
    try:
        http = m.UrlHttp(f"http://127.0.0.1:{srv.server_address[1]}", token_path=str(tmp_path / "missing.token"))
        err, text = tool(http, "cc_draft_plan", draft_args())
        assert err and "token" in text
        assert store.get_project("ship-the-widget") is None
        http = m.UrlHttp(f"http://127.0.0.1:{srv.server_address[1]}", token="not-the-token")
        err, text = tool(http, "cc_draft_plan", draft_args())
        assert err and "403" in text and store.get_project("ship-the-widget") is None
    finally:
        srv.shutdown()
        srv.server_close()
