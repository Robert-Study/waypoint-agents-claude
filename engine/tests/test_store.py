import sqlite3

import pytest

from cc_engine.store import Store, StaleRevision, NotAllowed


def test_store_starts_at_revision_zero_and_mutate_bumps(store):
    assert store.revision() == 0
    new = store.mutate(0, lambda conn: None, actor="owner", summary="Owner made a test change.")
    assert new == 1
    assert store.revision() == 1


def test_store_stale_revision_raises(store):
    store.mutate(0, lambda conn: None, actor="owner", summary="First change.")
    with pytest.raises(StaleRevision) as info:
        store.mutate(0, lambda conn: None, actor="owner", summary="Late change.")
    assert info.value.revision == 1
    assert store.revision() == 1


def test_store_mutation_rolls_back_on_error(store):
    def boom(conn):
        conn.execute("INSERT INTO projects(id, title, brief, goal_hash, cwd, created_at, archived) VALUES ('p','t','b','h',NULL,'now',0)")
        raise RuntimeError("fail")

    with pytest.raises(RuntimeError):
        store.mutate(0, boom, actor="owner", summary="Broken change.")
    assert store.revision() == 0
    assert store.get_project("p") is None


def test_store_events_and_telemetry_never_bump_revision(store):
    store.event(None, None, "telemetry", "usage", "Usage was read.", {"pool": "codex_5h"})
    store.set_meta("usage_cache", "{}")
    store.notify("pool", {"pool": "codex_5h"})
    assert store.revision() == 0
    assert store.events()[-1]["kind"] == "telemetry"


def test_store_meta_revision_is_protected(store):
    with pytest.raises(NotAllowed):
        store.set_meta("revision", "99")


def test_store_mutation_logs_one_sentence_event(store):
    store.mutate(0, lambda conn: None, actor="owner", summary="Owner renamed nothing.", project_id="p1")
    ev = store.events(project_id="p1")
    assert ev[-1]["summary"] == "Owner renamed nothing."
    assert ev[-1]["actor"] == "owner"


def test_store_uses_wal_and_contract_tables(store, tmp_path):
    conn = sqlite3.connect(tmp_path / "cc.sqlite")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert {"meta", "projects", "nodes", "events", "runs", "sprints", "questions"} <= names


def test_store_subscribe_receives_state_after_mutation(store):
    seen = []
    unsub = store.subscribe(lambda kind, payload: seen.append((kind, payload)))
    store.mutate(0, lambda conn: None, actor="owner", summary="Change.", project_id="p1")
    unsub()
    store.mutate(1, lambda conn: None, actor="owner", summary="Change two.", project_id="p1")
    states = [p for k, p in seen if k == "state"]
    assert states == [{"project": "p1", "revision": 1}]


def test_store_persists_across_reopen(tmp_path):
    s = Store(tmp_path / "x.sqlite")
    s.mutate(0, lambda conn: None, actor="owner", summary="Change.")
    s.close()
    s2 = Store(tmp_path / "x.sqlite")
    assert s2.revision() == 1
    s2.close()


def test_integrity_migrates_old_database_and_locks_existing_checks(tmp_path):
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO meta VALUES('revision','3');
        CREATE TABLE projects(id TEXT PRIMARY KEY, title TEXT, brief TEXT, goal_hash TEXT, cwd TEXT,
          created_at TEXT, archived INT DEFAULT 0);
        CREATE TABLE nodes(project_id TEXT NOT NULL, id TEXT NOT NULL, title TEXT, goal TEXT, goal_hash TEXT,
          check_json TEXT, deps_json TEXT, owner TEXT, lane TEXT, pool TEXT, model TEXT, effort TEXT,
          status TEXT DEFAULT 'not_started', attempts INT DEFAULT 0, actor TEXT, evidence TEXT,
          updated_at TEXT, PRIMARY KEY(project_id, id));
    """)
    from cc_engine.store import goal_hash
    conn.execute("INSERT INTO projects VALUES('p','P','Brief',?,NULL,'now',0)", (goal_hash("Brief"),))
    conn.execute("INSERT INTO nodes(project_id,id,goal,goal_hash,check_json,deps_json) VALUES('p','a','G',?,?,'[]')",
                 (goal_hash("G"), '{"type":"command","run":"exit 0"}'))
    conn.commit()
    conn.close()
    s = Store(path)
    try:
        assert s.revision() == 3
        assert s.project_runnable("p") is True and s.project_status("p") == "active"
        assert s.verify_integrity("p") == []
    finally:
        s.close()


def test_integrity_owner_check_update_moves_hash(store):
    from cc_engine import plan
    plan.import_plan(store, {"id": "p", "title": "P", "brief": "b", "nodes": [
        {"id": "a", "goal": "g", "check": {"type": "command", "run": "exit 0"}, "deps": []}]})
    store.update_node("p", "a", {"check": {"type": "command", "run": "exit 1"}}, actor="owner", summary="Owner.")
    assert store.verify_integrity("p") == []
    with pytest.raises(Exception):
        store.update_node("p", "a", {"check_hash": "x"}, actor="owner", summary="no")
