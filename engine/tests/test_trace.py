"""Agent tree: hook events, merges, tree building, tick and routes. tmp_path fixtures only."""
import http.client
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

from cc_engine import api, trace
from cc_engine.transcripts import Tailer
from test_transcripts import PRICES, append, asst, result, write

ALLOWED = "http://127.0.0.1:3080"


def ts(minutes_ago=0):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def by_id(store, **kw):
    return {a["id"]: a for a in trace.tree(store, **kw)["agents"]}


def ev(event, session="s1", **kw):
    return {"event": event, "session_id": session, **kw}


# ---------------------------------------------------------------- hook events
def test_event_lifecycle(store):
    rev = store.revision()
    seen = []
    store.subscribe(lambda kind, p: seen.append(kind))
    out = trace.record_events(store, [
        ev("session.start", cwd="C:/work/demo", title="Lead", model="claude-opus-9", at=ts(1)),
        ev("tool.call", tool="Bash"),
        ev("bogus.event"), {"event": "tool.call"}, "junk"])
    assert out == {"ok": True, "accepted": 2} and "trace" in seen
    a = by_id(store)["s1"]
    assert a["status"] == "running" and a["tool"] == "Bash" and a["title"] == "Lead" and a["kind"] == "session"
    trace.record_events(store, [ev("turn.complete", usage={"context_tokens": 5000, "context_window": 200000,
                                                           "cost_usd": 0.5})])
    a = by_id(store)["s1"]
    assert a["status"] == "idle" and a["tool"] is None and a["context"] == 5000 and a["cost_eq"] == 0.5
    trace.record_events(store, [ev("heartbeat", at=ts(0)), ev("session.end")])
    assert by_id(store)["s1"]["status"] == "ended"
    assert store.revision() == rev  # telemetry never bumps the plan revision


def test_subagent_events_and_cap(store):
    trace.record_events(store, [ev("session.start"), ev("subagent.start", agent_id="a1", title="Check"),
                                ev("tool.call", agent_id="a1", tool="Read")])
    sub = by_id(store)["s1/a1"]
    assert sub["kind"] == "subagent" and sub["parent"] == "s1" and sub["tool"] == "Read"
    trace.record_events(store, [ev("subagent.stop", agent_id="a1")])
    assert by_id(store)["s1/a1"]["status"] == "done"
    out = trace.record_events(store, [ev("heartbeat", session=f"x{i}") for i in range(80)])
    assert out["accepted"] == 50
    with pytest.raises(ValueError):
        trace.record_events(store, "no")


def test_stale_running_row_reads_as_idle_and_old_rows_drop_out(store):
    trace.record_events(store, [ev("session.start", session="stale", at=ts(30)),
                                ev("session.start", session="old", at=ts(60 * 30)),
                                ev("session.end", session="old", at=ts(60 * 30))])
    agents = by_id(store)
    assert agents["stale"]["status"] == "idle" and "old" not in agents
    assert "old" in by_id(store, since=ts(60 * 40))


# ---------------------------------------------------------------- merges
def make_transcripts(tmp_path):
    root = tmp_path / "projects"
    write(root / "slug" / "s1.jsonl", [asst("r1", 30, text="Working on it", tool=("t1", "Edit"), ts=ts(0)),
                                       {"type": "ai-title", "aiTitle": "From transcript", "timestamp": ts(0)}])
    sub = root / "slug" / "s1" / "subagents"
    write(sub / "agent-z9.jsonl", [asst("q1", 7, text="Sub result", ts=ts(0))])
    (sub / "agent-z9.meta.json").write_text(json.dumps({"agentType": "explore", "description": "Look around",
                                                        "finished": True}), encoding="utf-8")
    return Tailer(roots=[root], prices=PRICES, recent_days=None)


def test_merge_transcripts_owns_meters_but_not_hook_status(store, tmp_path):
    trace.record_events(store, [ev("session.start", title="Hook title", at=ts(0)),
                                ev("turn.complete", at=ts(0))])
    t = make_transcripts(tmp_path)
    t.poll()
    assert sorted(trace.merge_transcripts(store, t)) == ["s1", "s1/z9"]
    a = by_id(store)
    s = a["s1"]
    assert s["tokens_out"] == 30 and s["context"] == 160 and s["model"] == "claude-opus-9"
    assert s["status"] == "idle"  # hook said idle; the pending tool does not override it
    assert s["title"] == "Hook title" and s["last_line"] == "Working on it" and s["cost_eq"] is not None
    sub = a["s1/z9"]
    assert sub["status"] == "done" and sub["title"] == "Look around" and sub["parent"] == "s1"
    assert trace.merge_transcripts(store, t) == []  # nothing new, nothing rewritten


def test_transcript_only_rows_derive_status_and_title(store, tmp_path):
    t = make_transcripts(tmp_path)
    t.poll()
    trace.merge_transcripts(store, t)
    s = by_id(store)["s1"]
    assert s["status"] == "running" and s["tool"] == "Edit" and s["title"] == "From transcript"
    append(tmp_path / "projects" / "slug" / "s1.jsonl", [result("t1", ts=ts(0))])
    trace.merge_transcripts(store, t, set(t.poll()))
    s = by_id(store)["s1"]
    assert s["tool"] is None and s["status"] == "idle"


def test_desktop_owns_parent_and_title_unless_hook_titled(store):
    trace.record_events(store, [ev("session.start", session="lead", at=ts(0)),
                                ev("session.start", session="worker", at=ts(0), title="Hook worker"),
                                ev("session.start", session="other", at=ts(0))])
    links = {"worker": {"parent_cli_session_id": "lead", "title": "Desk worker", "archived": False, "cwd": "C:/w"},
             "other": {"parent_cli_session_id": "lead", "title": "Desk other", "archived": False, "cwd": None},
             "unknown": {"parent_cli_session_id": "lead", "title": "x"}}
    assert sorted(trace.merge_desktop(store, links)) == ["other", "worker"]
    a = by_id(store)
    assert a["worker"]["parent"] == "lead" and a["worker"]["title"] == "Hook worker"
    assert a["other"]["title"] == "Desk other" and a["worker"]["cwd"] == "C:/w"
    assert trace.merge_desktop(store, links) == []
    links["other"]["archived"] = True
    trace.merge_desktop(store, links)
    assert by_id(store)["other"]["status"] == "ended"


def test_parent_order_and_missing_parent_becomes_root(store):
    trace.record_events(store, [ev("session.start", session=s, at=ts(0)) for s in ("a", "b", "c", "d")])
    trace.record_events(store, [ev("session.start", session="d", parent="a", at=ts(0))])  # hook parent
    assert by_id(store)["d"]["parent"] == "a"
    trace.merge_desktop(store, {"d": {"parent_cli_session_id": "b"}})
    assert by_id(store)["d"]["parent"] == "b"  # desktop beats hook
    trace.link(store, "d", "c")
    assert by_id(store)["d"]["parent"] == "c"  # explicit beats desktop
    trace.link(store, "d", "gone")
    assert by_id(store)["d"]["parent"] == "b"  # a missing parent is skipped, the next source answers
    trace.record_events(store, [ev("session.start", session="e", parent="not-there", at=ts(0))])
    t = trace.tree(store)
    assert "e" in t["roots"] and {a["id"]: a["parent"] for a in t["agents"]}["e"] is None


def test_link_cycles_are_cut_and_bad_links_refused(store):
    trace.record_events(store, [ev("session.start", session="a", at=ts(0)), ev("session.start", session="b", at=ts(0))])
    trace.link(store, "a", "b")
    trace.link(store, "b", "a")
    t = trace.tree(store)
    assert len(t["roots"]) >= 1
    with pytest.raises(ValueError):
        trace.link(store, "a", "a")


def test_recent_cwds(store):
    trace.record_events(store, [ev("session.start", session="a", cwd="C:/one", at=ts(10)),
                                ev("session.start", session="b", cwd="C:/two", at=ts(5)),
                                ev("session.start", session="c", cwd="C:/old", at=ts(60 * 72)),
                                ev("session.start", session="d", cwd="C:/two", at=ts(1))])
    assert trace.recent_cwds(store) == ["C:/two", "C:/one"]
    assert "C:/old" in trace.recent_cwds(store, hours=100)


def test_tick_pushes_only_changes(store, tmp_path):
    t = make_transcripts(tmp_path)
    pushed = []
    store.subscribe(lambda kind, p: pushed.append(p) if kind == "trace" else None)
    last = {}
    desk = tmp_path / "desktop"
    desk.mkdir()

    def roots():
        return [desk]

    moved = trace.tick(store, t, roots, last, desktop_interval=0)
    assert {a["id"] for a in moved} == {"s1", "s1/z9"} and len(pushed) == 1
    assert trace.tick(store, t, roots, last, desktop_interval=0) == [] and len(pushed) == 1
    append(tmp_path / "projects" / "slug" / "s1.jsonl", [asst("r2", 99, ts=ts(0))])
    moved = trace.tick(store, t, roots, last, desktop_interval=0)
    assert [a["id"] for a in moved] == ["s1"] and len(pushed) == 2


def test_start_runs_and_stops(store, tmp_path):
    class App:
        closing = threading.Event()

    App.store = store
    h = trace.start(App, interval=0.05, roots=[tmp_path / "none"], desktop_env={})
    h.stop()
    assert not h.thread.is_alive()


# ---------------------------------------------------------------- routes
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
    return resp, (json.loads(payload) if payload else {})


def test_routes(server, store):
    tok = server.app.token
    assert call(server, "POST", "/api/trace", {"events": []})[0].status == 403  # no credential
    resp, body = call(server, "POST", "/api/trace", {"events": [ev("session.start", session="r1", at=ts(0)),
                                                                  ev("session.start", session="r2", at=ts(0))]},
                      token=tok)
    assert resp.status == 200 and body == {"ok": True, "accepted": 2}
    assert call(server, "POST", "/api/trace", {"events": "x"}, token=tok)[0].status == 400
    resp, body = call(server, "POST", "/api/trace/link", {"child": "r2", "parent": "r1"}, origin=ALLOWED)
    assert resp.status == 200 and body["ok"] is True
    assert call(server, "POST", "/api/trace/link", {"child": "r2"}, token=tok)[0].status == 400
    assert call(server, "POST", "/api/trace/link", {"child": "r2", "parent": "r2"}, token=tok)[0].status == 400
    resp, tree = call(server, "GET", "/api/trace/tree")
    assert resp.status == 200 and tree["roots"] == ["r1"] and {a["id"] for a in tree["agents"]} == {"r1", "r2"}
    assert {a["id"]: a["parent"] for a in tree["agents"]}["r2"] == "r1" and tree["at"]
    assert set(trace.VIEW_FIELDS) <= set(tree["agents"][0])
    assert call(server, "GET", "/api/trace/tree?since=" + ts(0).replace("+", "%2B"))[1]["agents"]
    assert call(server, "GET", "/api/trace/tree?since=garbage")[0].status == 400


def test_future_at_is_clamped(store):
    trace.record_events(store, [ev("session.start", at="2099-01-01T00:00:00+00:00")])
    row = by_id(store)["s1"]
    assert row["updated_at"] < "2099" and row["started_at"] < "2099"


def test_cycle_cut_keeps_the_valid_parent_leading_in(store):
    trace.record_events(store, [ev("session.start", s) for s in ("a", "b", "c")])
    trace.link(store, "a", "b")
    trace.link(store, "b", "c")
    trace.link(store, "c", "b")
    got = {x["id"]: x["parent"] for x in trace.tree(store)["agents"]}
    assert got["a"] == "b"
    assert (got["b"] is None) != (got["c"] is None)  # exactly one edge of the b<->c loop is cut


def test_prune_drops_old_rows_only(store):
    trace.record_events(store, [ev("session.start", "old"), ev("session.start", "new")])
    with store.tx() as c:
        c.execute("UPDATE agents SET updated_at = ? WHERE id = 'old'", ("2020-01-01T00:00:00+00:00",))
    assert trace.prune(store) == 1
    assert [r[0] for r in store._q("SELECT id FROM agents")] == ["new"]
