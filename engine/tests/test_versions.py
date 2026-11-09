"""Versions (waypoint-v5 V1): one product, many versions. Spec: docs/v5/versions_contract.md."""
import http.client
import json
import sqlite3
import threading

import pytest

from cc_engine import api, graph, plan, state_snapshot, versions
from cc_engine.store import CLOSED_STATUSES, STATUSES, StaleRevision, Store

OLD_SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE projects(
  id TEXT PRIMARY KEY, title TEXT, brief TEXT, goal_hash TEXT, cwd TEXT,
  created_at TEXT, archived INT DEFAULT 0, status TEXT DEFAULT 'active');
CREATE TABLE nodes(
  project_id TEXT NOT NULL, id TEXT NOT NULL, title TEXT, goal TEXT, goal_hash TEXT,
  check_json TEXT, check_hash TEXT, deps_json TEXT, owner TEXT, lane TEXT, pool TEXT, model TEXT, effort TEXT,
  status TEXT DEFAULT 'not_started', attempts INT DEFAULT 0, actor TEXT, evidence TEXT,
  updated_at TEXT, recommended TEXT, PRIMARY KEY(project_id, id));
CREATE TABLE events(
  seq INTEGER PRIMARY KEY, at TEXT, project_id TEXT, node_id TEXT, kind TEXT, actor TEXT,
  summary TEXT, data_json TEXT);
"""


def _node(i, deps=(), **extra):
    n = {"id": i, "title": f"Task {i}", "goal": f"Goal of {i}.", "check": {"type": "command", "run": "exit 0"},
         "deps": list(deps), "owner": "w", "lane": "core"}
    n.update(extra)
    return n


def make(store, pid="alpha-v1", nodes=None, title="Alpha v1"):
    nodes = nodes or [_node("A"), _node("B", ["A"], model="m1", effort="high", pool="p", recommended="m1"),
                      _node("C", ["B"]), _node("D", ["A"]), _node("E")]
    plan.import_plan(store, {"id": pid, "title": title, "brief": "The brief.", "cwd": None, "nodes": nodes},
                     actor="owner")
    return pid


def finish(store, pid, nid, status="done"):
    store.set_node_status(pid, nid, status, "owner", "evidence", expected_revision=None)


def force_status(store, pid, nid, status):
    """Put a node in ``skipped``/``carried`` the way only the versions module may: raw SQL inside a mutate."""
    store.mutate(None, lambda c: c.execute("UPDATE nodes SET status=? WHERE project_id=? AND id=?",
                                           (status, pid, nid)), "owner", f"test forces {nid} to {status}")


def status_of(store, pid):
    return {n["id"]: n["status"] for n in store.list_nodes(pid)}


# ------------------------------------------------------------------ migration / statuses
def test_statuses_and_closed_set():
    assert "skipped" in STATUSES and "carried" in STATUSES
    assert set(CLOSED_STATUSES) == {"done", "skipped", "carried"}


def test_migration_of_old_database(tmp_path):
    path = tmp_path / "old.sqlite"
    con = sqlite3.connect(path)
    con.executescript(OLD_SCHEMA)
    con.execute("INSERT INTO meta VALUES('revision','5')")
    con.execute("INSERT INTO projects(id,title,brief,goal_hash,cwd,created_at,archived,status)"
                " VALUES('old','Old','b','h',NULL,'2026-01-01T00:00:00+00:00',0,'active')")
    con.execute("INSERT INTO nodes(project_id,id,title,goal,goal_hash,check_json,check_hash,deps_json,status)"
                " VALUES('old','A','t','g','gh','{}','ch','[]','done')")
    con.commit()
    con.close()
    s = Store(path)
    try:
        cols = {r[1] for r in s._q("PRAGMA table_info(projects)")}
        assert {"family", "version"} <= cols
        assert s._q("SELECT name FROM sqlite_master WHERE name='families'")
        p = s.get_project("old")
        assert p["title"] == "Old" and p["family"] is None and p["version"] is None
        assert s.get_node("old", "A")["status"] == "done" and s.revision() == 5
    finally:
        s.close()
    Store(path).close()  # idempotent: a second open changes nothing and does not fail


# ------------------------------------------------------------------ set_family
def test_set_family_archives_all_but_last_and_is_idempotent(store):
    make(store, "p1", title="One")
    make(store, "p2", title="Two")
    make(store, "p3", title="Three")
    rev = versions.set_family(store, "prod", "Product", ["p1", "p2", "p3"], "owner", store.revision())["revision"]
    rows = {p["id"]: p for p in store.list_projects(include_archived=True)}
    assert [(rows[k]["family"], rows[k]["version"], rows[k]["archived"]) for k in ("p1", "p2", "p3")] == \
        [("prod", 1, 1), ("prod", 2, 1), ("prod", 3, 0)]
    assert status_of(store, "p1")["A"] == "not_started"  # statuses untouched
    again = versions.set_family(store, "prod", "Product", ["p1", "p2", "p3"], "owner", rev)
    assert again["revision"] == rev + 1
    assert store.get_project("p3")["archived"] == 0
    # reorder: the last member becomes current, the earlier current is archived
    versions.set_family(store, "prod", "Product 2", ["p3", "p1", "p2"], "owner", None)
    assert store.get_project("p2")["archived"] == 0 and store.get_project("p3")["archived"] == 1
    assert store.get_project("p3")["version"] == 1
    assert store._q("SELECT title FROM families WHERE id='prod'")[0][0] == "Product 2"
    ev = [e for e in store.events("p2") if e["kind"] == "version"]
    assert ev and "Product 2" in ev[-1]["summary"]


def test_set_family_rejects_unknown_member_and_stale(store):
    make(store, "p1")
    with pytest.raises(KeyError):
        versions.set_family(store, "f", "F", ["p1", "nope"], "owner", None)
    with pytest.raises(ValueError):
        versions.set_family(store, "f", "F", [], "owner", None)
    with pytest.raises(StaleRevision):
        versions.set_family(store, "f", "F", ["p1"], "owner", store.revision() - 1)


# ------------------------------------------------------------------ new_version
def test_new_version_carries_unfinished_by_default(store):
    pid = make(store)
    finish(store, pid, "A")
    out = versions.new_version(store, project=pid, actor="owner", expected_revision=store.revision())
    assert out["ok"] and out["project"] == "alpha-v2" and out["version"] == 2
    assert sorted(out["carried"]) == ["B", "C", "D", "E"] and out["skipped"] == []
    assert out["revision"] == store.revision()
    # old project: family created from the id minus -v1, archived, nodes marked
    old, new = store.get_project(pid), store.get_project("alpha-v2")
    assert (old["family"], old["version"], old["archived"]) == ("alpha", 1, 1)
    assert (new["family"], new["version"], new["archived"]) == ("alpha", 2, 0)
    assert new["title"] == "Alpha v1 v2" or new["title"].endswith("v2")
    assert new["brief"] == old["brief"] and new["goal_hash"] == old["goal_hash"]
    assert store._q("SELECT title FROM families WHERE id='alpha'")[0][0] == "Alpha v1"
    st = status_of(store, pid)
    assert st["A"] == "done" and all(st[k] == "carried" for k in "BCDE")
    assert store.get_node(pid, "B")["evidence"] == "carried to alpha-v2/B"
    # new project: copies, status not_started, attempts 0, deps only where also carried
    b = store.get_node("alpha-v2", "B")
    assert b["status"] == "not_started" and b["attempts"] == 0
    assert (b["title"], b["goal"], b["model"], b["effort"], b["pool"], b["recommended"], b["lane"], b["owner"]) == \
        ("Task B", "Goal of B.", "m1", "high", "p", "m1", "core", "w")
    assert b["check"] == {"type": "command", "run": "exit 0"} and b["goal_hash"] == store.get_node(pid, "B")["goal_hash"]
    assert b["deps"] == []  # A was done, not carried
    assert store.get_node("alpha-v2", "C")["deps"] == ["B"]
    assert store.verify_integrity("alpha-v2") == []
    q1 = store.get_node("alpha-v2", "Q1")
    assert q1["lane"] == "discovery" and q1["title"].startswith("Discovery questions for")
    assert q1["check"]["type"] == "human" and "waypoint-planning" in q1["check"]["prompt"]
    # one event per decision plus a summary
    evs = [e for e in store.events() if e["kind"] == "version"]
    assert len(evs) >= 5 and any("Alpha" in e["summary"] for e in evs)


def test_new_version_skip_with_reason_and_dep_filtering(store):
    pid = make(store)
    out = versions.new_version(store, project=pid, title="Alpha reboot", id="alpha-next",
                               decisions={"B": {"action": "skip", "reason": "superseded by the mod"},
                                          "A": {"action": "carry"}},
                               actor="router", expected_revision=None)
    assert out["project"] == "alpha-next" and out["skipped"] == [{"id": "B", "reason": "superseded by the mod"}]
    assert "B" not in out["carried"] and "C" in out["carried"]
    assert store.get_project("alpha-next")["title"] == "Alpha reboot"
    assert store.get_node(pid, "B")["status"] == "skipped"
    assert store.get_node(pid, "B")["evidence"] == "skipped: superseded by the mod"
    assert store.get_node("alpha-next", "C")["deps"] == []  # its only dep was skipped
    assert store.get_node("alpha-next", "D")["deps"] == ["A"]
    assert store.get_node("alpha-next", "B") is None


def test_new_version_validation(store):
    pid = make(store)
    rev = store.revision()
    for bad in ({"B": {"action": "skip"}}, {"B": {"action": "skip", "reason": "  "}}, {"B": {"action": "bin"}},
                {"ZZ": {"action": "carry"}}):
        with pytest.raises(ValueError):
            versions.new_version(store, project=pid, decisions=bad, actor="owner", expected_revision=rev)
    with pytest.raises(ValueError):
        versions.new_version(store, actor="owner", expected_revision=rev)
    with pytest.raises(KeyError):
        versions.new_version(store, project="nope", actor="owner", expected_revision=rev)
    assert store.revision() == rev and store.get_project(pid)["archived"] == 0  # nothing happened


def test_new_version_stale_revision(store):
    pid = make(store)
    with pytest.raises(StaleRevision):
        versions.new_version(store, project=pid, actor="owner", expected_revision=store.revision() - 1)
    assert store.get_project("alpha-v2") is None


def test_new_version_second_roll_over_uses_family_current(store):
    pid = make(store)
    versions.new_version(store, project=pid, actor="owner", expected_revision=None)
    out = versions.new_version(store, family="alpha", actor="owner", expected_revision=None)
    assert out["project"] == "alpha-v3" and out["version"] == 3
    assert store.get_project("alpha-v2")["archived"] == 1 and store.get_project("alpha-v3")["archived"] == 0
    assert store.get_node("alpha-v2", "B")["status"] == "carried"
    assert store.get_node("alpha-v3", "B")["evidence"] is None
    assert store.get_node("alpha-v3", "Q1") is not None
    # the discovery node of v2 (not done) rolls over too, but the fresh one does not collide
    assert store.get_node("alpha-v3", "Q1")["title"].startswith("Discovery questions for")


def test_new_version_inherits_project_status_and_made_unique(store):
    pid = make(store)
    store.mutate(None, lambda c: c.execute("UPDATE projects SET status='draft' WHERE id=?", (pid,)), "owner", "x")
    make(store, "alpha-v2", title="squatter")
    out = versions.new_version(store, project=pid, actor="owner", expected_revision=None)
    assert out["project"] not in (pid, "alpha-v2")
    assert store.get_project(out["project"])["status"] == "draft"


def test_new_version_keeps_loop_fields(store):
    nodes = [_node("A", kind="code", script="scripts/x.py"), _node("B", [{"id": "A", "crosses": "the table"}])]
    pid = make(store, nodes=nodes)
    versions.new_version(store, project=pid, actor="owner", expected_revision=None)
    exported = {n["id"]: n for n in plan.export_plan(store, "alpha-v2")["nodes"]}
    assert exported["A"]["kind"] == "code" and exported["A"]["script"] == "scripts/x.py"
    assert exported["B"]["deps"] == [{"id": "A", "crosses": "the table"}]


# ------------------------------------------------------------------ dependencies
def test_carried_and_skipped_never_satisfy_a_dependency(store):
    pid = make(store, nodes=[_node("A"), _node("B", ["A"]), _node("C"), _node("D", ["C"])])
    force_status(store, pid, "A", "skipped")
    force_status(store, pid, "C", "carried")
    ready = graph.readiness(store.list_nodes(pid))
    assert ready["B"] == "not_started" and ready["D"] == "not_started"
    assert ready["A"] == "skipped" and ready["C"] == "carried"
    board = api.build_board(store, pid)
    assert board["counts"]["skipped"] == 1 and board["counts"]["carried"] == 1 and board["counts"]["ready"] == 0


# ------------------------------------------------------------------ decide
def collated(store):
    make(store, "old-1", nodes=[_node("A"), _node("B", ["A"]), _node("C")], title="Old")
    make(store, "old-2", nodes=[_node("A"), _node("X")], title="Newer")
    versions.set_family(store, "prod", "Product", ["old-1", "old-2"], "owner", None)
    return "old-1", "old-2"


def test_decide_carries_into_current_with_id_collision(store):
    old, cur = collated(store)
    finish(store, old, "C")
    out = versions.decide(store, old, {"A": {"action": "carry"}, "B": {"action": "carry"}}, "router", store.revision())
    assert out["ok"] and out["skipped"] == []
    assert {"from": "A", "to": "v1-A"} in out["carried"] and {"from": "B", "to": "B"} in out["carried"]
    assert out["revision"] == store.revision()
    assert status_of(store, old) == {"A": "carried", "B": "carried", "C": "done"}
    assert store.get_node(old, "A")["evidence"] == "carried to old-2/v1-A"
    new_b = store.get_node(cur, "B")
    assert new_b["status"] == "not_started" and new_b["deps"] == ["v1-A"]  # dep followed its carried target
    assert store.verify_integrity(cur) == []


def test_decide_skip_and_errors(store):
    old, cur = collated(store)
    out = versions.decide(store, old, {"A": {"action": "skip", "reason": "no longer wanted"}}, "owner", None)
    assert out["skipped"] == [{"id": "A", "reason": "no longer wanted"}] and out["carried"] == []
    assert store.get_node(old, "A")["evidence"] == "skipped: no longer wanted"
    with pytest.raises(ValueError):  # the current version cannot be decided
        versions.decide(store, cur, {"X": {"action": "carry"}}, "owner", None)
    with pytest.raises(ValueError):  # already skipped: not unfinished
        versions.decide(store, old, {"A": {"action": "carry"}}, "owner", None)
    with pytest.raises(ValueError):
        versions.decide(store, old, {"B": {"action": "skip"}}, "owner", None)
    with pytest.raises(StaleRevision):
        versions.decide(store, old, {"B": {"action": "carry"}}, "owner", store.revision() - 1)


# ------------------------------------------------------------------ history
def test_history_payload_shape(store):
    pid = make(store)
    finish(store, pid, "A")
    versions.new_version(store, project=pid, decisions={"B": {"action": "skip", "reason": "dropped"}},
                         actor="owner", expected_revision=None)
    h = versions.history(store, family="alpha")
    assert set(h) == {"family", "current", "versions", "skipped"}
    assert h["family"] == {"id": "alpha", "title": "Alpha v1"} and h["current"] == "alpha-v2"
    v1, v2 = h["versions"]
    assert [v1["project"], v1["version"], v1["archived"]] == [pid, 1, True]
    assert set(v1) == {"project", "title", "version", "archived", "status", "created_at", "counts"}
    assert v1["counts"] == {"done": 1, "open": 0, "skipped": 1, "carried": 3}
    assert v2["archived"] is False and v2["counts"]["open"] == 4 and v2["counts"]["skipped"] == 0
    (sk,) = h["skipped"]
    assert (sk["project"], sk["version"], sk["id"], sk["title"], sk["goal"], sk["reason"]) == \
        (pid, 1, "B", "Task B", "Goal of B.", "dropped")
    assert sk["at"]
    assert versions.history(store, project="alpha-v2") == h  # by any member project
    with pytest.raises(KeyError):
        versions.history(store, family="nope")
    with pytest.raises(KeyError):
        versions.history(store, project="nope")


def test_history_of_project_without_family(store):
    pid = make(store)
    h = versions.history(store, project=pid)
    assert h["current"] == pid and len(h["versions"]) == 1 and h["versions"][0]["version"] == 1
    assert h["versions"][0]["counts"]["open"] == 5 and h["skipped"] == []


def test_skipped_listing_newest_version_first(store):
    pid = make(store, nodes=[_node("A"), _node("B")])
    versions.new_version(store, project=pid, decisions={"A": {"action": "skip", "reason": "r1"}},
                         actor="owner", expected_revision=None)
    versions.new_version(store, family="alpha", decisions={"B": {"action": "skip", "reason": "r2"}},
                         actor="owner", expected_revision=None)
    assert [(s["version"], s["id"]) for s in versions.history(store, family="alpha")["skipped"]] == [(2, "B"), (1, "A")]


# ------------------------------------------------------------------ snapshot
def test_snapshot_keeps_and_restores_family_version_archived(store, tmp_path):
    pid = make(store)
    finish(store, pid, "A")
    versions.new_version(store, project=pid, decisions={"B": {"action": "skip", "reason": "gone"}},
                         actor="owner", expected_revision=None)
    snap = state_snapshot.snapshot_project(store, pid)
    assert snap["project"]["family"] == "alpha" and snap["project"]["version"] == 1
    assert snap["project"]["archived"] is True and snap["project"]["family_title"] == "Alpha v1"
    assert {n["id"]: n["status"] for n in snap["nodes"]}["B"] == "skipped"
    fresh = Store(tmp_path / "fresh.sqlite")
    try:
        state_snapshot.resume(fresh, json.loads(json.dumps(snap)))
        p = fresh.get_project(pid)
        assert (p["family"], p["version"], p["archived"]) == ("alpha", 1, 1)
        assert fresh._q("SELECT title FROM families WHERE id='alpha'")[0][0] == "Alpha v1"
        assert status_of(fresh, pid)["B"] == "skipped" and status_of(fresh, pid)["C"] == "carried"
        assert fresh.get_node(pid, "B")["evidence"] == "skipped: gone"
    finally:
        fresh.close()


def test_snapshot_of_plain_project_has_null_family(store):
    pid = make(store)
    p = state_snapshot.snapshot_project(store, pid)["project"]
    assert p["family"] is None and p["version"] is None and p["archived"] is False


# ------------------------------------------------------------------ HTTP
@pytest.fixture
def server(store, tmp_path):
    srv = api.make_server(store, host="127.0.0.1", port=0, usage_reader=lambda: [], heartbeat=0.5,
                          poll_interval=0)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


OWNER = {"Origin": "http://127.0.0.1:3080"}


def call(srv, method, path, body=None, owner=True):
    headers = dict(OWNER) if owner else {"X-CC-Token": srv.app.token}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    conn.request(method, path, body=data, headers=headers)
    r = conn.getresponse()
    raw = r.read()
    conn.close()
    return r.status, (json.loads(raw) if raw else None)


def test_http_new_version_history_and_listing(server, store):
    pid = make(store)
    rev = store.revision()
    st, body = call(server, "POST", "/api/version/new", {"project": pid, "revision": rev - 1})
    assert st == 409 and body == {"error": "stale", "revision": rev}
    st, body = call(server, "POST", "/api/version/new",
                    {"project": pid, "decisions": {"B": {"action": "skip"}}, "revision": rev})
    assert st == 400
    st, body = call(server, "POST", "/api/version/new",
                    {"project": pid, "decisions": {"B": {"action": "skip", "reason": "nope"}}, "revision": rev},
                    owner=False)  # the router may open a version
    assert st == 200 and body["ok"] and body["project"] == "alpha-v2" and body["skipped"] == [{"id": "B", "reason": "nope"}]
    ev = [e for e in store.events() if e["kind"] == "version"]
    assert any(e["actor"] == "router" for e in ev)

    st, h = call(server, "GET", "/api/history?family=alpha")
    assert st == 200 and h["current"] == "alpha-v2" and len(h["versions"]) == 2
    assert call(server, "GET", "/api/history?project=alpha-v1")[1] == h
    assert call(server, "GET", "/api/history?family=zzz")[0] == 404
    assert call(server, "GET", "/api/history")[0] == 400

    st, listing = call(server, "GET", "/api/projects")
    rows = {p["id"]: p for p in listing["projects"]}
    assert "alpha-v1" not in rows  # archived stay hidden by default
    cur = rows["alpha-v2"]
    assert (cur["family"], cur["family_title"], cur["version"], cur["versions"]) == ("alpha", "Alpha v1", 2, 2)
    st, listing = call(server, "GET", "/api/projects?all=1")
    rows = {p["id"]: p for p in listing["projects"]}
    assert rows["alpha-v1"]["version"] == 1 and rows["alpha-v1"]["versions"] == 2
    make(store, "solo")
    solo = {p["id"]: p for p in call(server, "GET", "/api/projects")[1]["projects"]}["solo"]
    assert (solo["family"], solo["family_title"], solo["version"], solo["versions"]) == (None, None, None, 0)

    board = call(server, "GET", "/api/board?project=alpha-v1")[1]  # archived boards stay readable
    assert board["project"]["family"] == "alpha" and board["project"]["family_title"] == "Alpha v1"
    assert board["project"]["version"] == 1 and board["project"]["archived"] is True
    assert board["counts"]["carried"] == 4 and board["counts"]["skipped"] == 1
    plain = call(server, "GET", "/api/board?project=solo")[1]["project"]
    assert plain["family"] is None and plain["version"] is None


def test_http_family_is_owner_only_and_decide(server, store):
    make(store, "p1", nodes=[_node("A"), _node("B")])
    make(store, "p2", nodes=[_node("A")])
    body = {"family": "prod", "title": "Product", "members": ["p1", "p2"], "revision": store.revision()}
    st, out = call(server, "POST", "/api/version/family", body, owner=False)
    assert st == 403 and out["error"] == "owner_only"
    st, out = call(server, "POST", "/api/version/family", body)
    assert st == 200 and out["ok"] and out["revision"] == store.revision()
    assert store.get_project("p1")["archived"] == 1

    st, out = call(server, "POST", "/api/version/decide",
                   {"project": "p2", "decisions": {"A": {"action": "carry"}}, "revision": store.revision()})
    assert st == 400  # p2 is the current version
    st, out = call(server, "POST", "/api/version/decide",
                   {"project": "p1", "decisions": {"B": {"action": "carry"}, "A": {"action": "skip", "reason": "why"}},
                    "revision": store.revision()}, owner=False)
    assert st == 200 and out["carried"] == [{"from": "B", "to": "B"}] and out["skipped"] == [{"id": "A", "reason": "why"}]
    assert call(server, "POST", "/api/version/decide",
                {"project": "p1", "decisions": {}, "revision": 0})[0] in (400, 409)
    assert call(server, "POST", "/api/version/decide", {"project": "ghost", "decisions": {"A": {"action": "carry"}},
                                                         "revision": store.revision()})[0] == 404
