"""Y1 cloud sync: inbox files from cloud sessions are imported revision-consistently."""
import http.client
import json
import threading

import pytest

from cc_engine import api, cloud_sync, plan, todo

ALLOWED = "http://127.0.0.1:3080"


@pytest.fixture
def loaded(store, fixture_plan):
    plan.import_plan(store, fixture_plan)
    return "fx"


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "repo" / "sync" / "inbox").mkdir(parents=True)
    return tmp_path / "repo"


def put(repo, name, items, session="cloud-1"):
    doc = {"schema": "waypoint.sync.v1", "session": session, "written_at": "2026-11-05T10:00:00Z", "items": items}
    (repo / "sync" / "inbox" / name).write_text(json.dumps(doc), encoding="utf-8")


def st(node, status, base, **kw):
    return {"kind": "node_status", "project": "fx", "id": node, "status": status, "evidence": "ran it",
            "base_revision": base, **kw}


def kinds(store, kind):
    return [e for e in store.events(limit=10000) if e["kind"] == kind]


def test_apply_ok_moves_file_and_records_actor(store, loaded, repo):
    put(repo, "20261105-a-1.json", [st("A", "running", store.revision()),
                                    {"kind": "receipt", "project": "fx", "id": "A", "summary": "built", "links": ["u"]}])
    res = cloud_sync.import_inbox(store, repo)
    assert res["files"] == 1 and res["applied"] == 2 and res["conflicts"] == 0
    node = store.get_node("fx", "A")
    assert node["status"] == "running" and node["actor"] == "cloud-sync:cloud-1" and node["evidence"] == "ran it"
    assert kinds(store, "receipt")[0]["data"]["links"] == ["u"]
    assert not list((repo / "sync" / "inbox").iterdir())
    assert (repo / "sync" / "processed" / "20261105-a-1.json").exists()


def test_cloud_done_is_only_submitted(store, loaded, repo):
    put(repo, "a.json", [st("A", "done", store.revision())])
    cloud_sync.import_inbox(store, repo)
    assert store.get_node("fx", "A")["status"] == "submitted"


def test_stale_base_revision_is_a_conflict_not_applied(store, loaded, repo):
    base = store.revision()
    store.set_node_status("fx", "A", "needs_you", "owner", summary="Owner flagged it")
    put(repo, "a.json", [st("A", "running", base)])
    res = cloud_sync.import_inbox(store, repo)
    assert res["conflicts"] == 1 and res["applied"] == 0
    assert store.get_node("fx", "A")["status"] == "needs_you"
    assert kinds(store, "sync_conflict")
    entry = json.loads((repo / "sync" / "conflicts" / "a.json").read_text())
    assert "changed locally" in entry["conflicts"][0]["reason"]
    assert any("Cloud sync conflict" in t["text"] for t in todo.list_todos(store))


def test_undo_progress_is_a_conflict(store, loaded, repo):
    store.set_node_status("fx", "A", "submitted", "worker")
    put(repo, "a.json", [st("A", "running", store.revision())])
    res = cloud_sync.import_inbox(store, repo)
    assert res["conflicts"] == 1
    assert store.get_node("fx", "A")["status"] == "submitted"
    assert "undo local progress" in kinds(store, "sync_conflict")[0]["summary"]


def test_idempotent_rerun(store, loaded, repo):
    put(repo, "a.json", [st("A", "running", store.revision()), {"kind": "todo_add", "text": "Check the PR"}])
    cloud_sync.import_inbox(store, repo)
    rev, nev = store.revision(), len(store.events(limit=10000))
    again = cloud_sync.import_inbox(store, repo)
    assert again["files"] == 0 and store.revision() == rev and len(store.events(limit=10000)) == nev


def test_todo_dedupe(store, loaded, repo):
    put(repo, "a.json", [{"kind": "todo_add", "text": "Review the draft", "section": "human"}])
    put(repo, "b.json", [{"kind": "todo_add", "text": "review  the draft", "section": "human"}])
    res = cloud_sync.import_inbox(store, repo)
    assert res["applied"] == 1 and res["skipped"] == 1
    assert len([t for t in todo.list_todos(store) if "draft" in t["text"]]) == 1


def test_malformed_file_is_a_conflict_not_a_crash(store, loaded, repo):
    (repo / "sync" / "inbox" / "bad.json").write_text("{not json", encoding="utf-8")
    put(repo, "good.json", [st("A", "running", store.revision())])
    res = cloud_sync.import_inbox(store, repo)
    assert res["files"] == 2 and res["conflicts"] == 1 and res["applied"] == 1
    assert (repo / "sync" / "conflicts" / "bad.json").exists()
    assert (repo / "sync" / "processed" / "bad.json").exists()


def test_missing_inbox_is_a_noop(store, tmp_path):
    assert cloud_sync.import_inbox(store, tmp_path / "nowhere")["files"] == 0


@pytest.fixture
def server(store, tmp_path):
    srv = api.make_server(store, host="127.0.0.1", port=0, usage_reader=lambda: [], heartbeat=0.5,
                          poll_interval=0.2, token_path=tmp_path / "engine.token")
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def post(srv, body, origin=None, token=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-CC-Token"] = token
    if origin:
        headers["Origin"] = origin
    conn.request("POST", "/api/sync/import", body=json.dumps(body).encode(), headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, json.loads(data) if data else None


def test_route_is_owner_only(store, loaded, repo, server):
    put(repo, "a.json", [st("A", "running", store.revision())])
    status, _ = post(server, {"repo": str(repo)})
    assert status == 403
    status, _ = post(server, {"repo": str(repo)}, token=server.app.token)  # router token
    assert status == 403
    assert store.get_node("fx", "A")["status"] != "running"
    status, body = post(server, {"repo": str(repo)}, origin=ALLOWED)
    assert status == 200 and body["applied"] == 1


def test_cli_subcommand(tmp_path, fixture_plan, repo, capsys):
    from cc_engine.__main__ import main
    from cc_engine.store import Store
    db = tmp_path / "cli.sqlite"
    s = Store(db)
    plan.import_plan(s, fixture_plan)
    rev = s.revision()
    s.close()
    put(repo, "a.json", [st("A", "running", rev)])
    assert main(["sync-import", "--repo", str(repo), "--db", str(db)]) == 0
    assert "1 applied" in capsys.readouterr().out
