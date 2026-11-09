"""Owner-scoped Claude mod token (waypoint-v5 M1): X-CC-Owner-Token -> actor owner, credential claude-mod."""
import http.client
import json
import threading

import pytest

from cc_engine import api

OWNER_HEADER = "X-CC-Owner-Token"


@pytest.fixture
def server(store, tmp_path):
    unknown = [{"pool": "codex_5h", "remaining_percent": None, "observed_at": None, "resets_at": None,
                "status": "UNKNOWN", "source": "unreachable: test"}]
    srv = api.make_server(store, host="127.0.0.1", port=0, usage_reader=lambda: unknown,
                          capture_runner=lambda argv: (0, "", ""), capture_dir=tmp_path,
                          heartbeat=0.5, poll_interval=0)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def call(srv, path, body, headers):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    h = dict(headers)
    h["Content-Type"] = "application/json"
    conn.request("POST", path, body=json.dumps(body).encode(), headers=h)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, raw.decode("utf-8")


def human_todo(srv, text="Read the M1 receipt"):
    status, raw = call(srv, "/api/todos", {"text": text, "section": "human"}, {"X-CC-Token": srv.app.token})
    assert status == 200, raw
    return json.loads(raw)["todo"]["id"]


def test_owner_token_file_made_on_first_use_beside_engine_token(server):
    path = server.app.owner_token_path
    assert path.parent == server.app.token_path.parent and path.name == "owner.token"
    assert not path.exists()  # lazy: tests never touch data/, and nothing asked yet
    tok = server.app.owner_token()
    assert path.exists() and path.read_text(encoding="utf-8").strip() == tok
    assert len(tok) >= 32 and tok != server.app.token
    assert server.app.owner_token() == tok  # stable across calls


def test_owner_token_ticks_and_clicks_a_todo_as_owner(server):
    tid = human_todo(server)
    mod = {OWNER_HEADER: server.app.owner_token()}
    status, raw = call(server, "/api/todos/update", {"id": tid, "done": True}, mod)
    assert status == 200, raw
    assert json.loads(raw)["todo"]["done"] is True
    status, raw = call(server, "/api/todos/click", {"id": tid}, mod)
    assert status == 200 and json.loads(raw)["todo"]["first_clicked_at"]


def test_router_token_still_cannot_tick(server):
    tid = human_todo(server)
    status, raw = call(server, "/api/todos/update", {"id": tid, "done": True}, {"X-CC-Token": server.app.token})
    assert status == 403 and "owner_only" in raw


def test_wrong_owner_token_is_forbidden_and_never_echoed(server):
    tid = human_todo(server)
    real = server.app.owner_token()
    status, raw = call(server, "/api/todos/update", {"id": tid, "done": True}, {OWNER_HEADER: "x" * 43})
    assert status == 403 and "forbidden" in raw
    assert real not in raw and "x" * 43 not in raw


def test_foreign_origin_with_owner_token_is_refused(server):
    status, raw = call(server, "/api/todos", {"text": "t", "section": "human"},
                       {OWNER_HEADER: server.app.owner_token(), "Origin": "app://claude-desktop"})
    assert status == 403 and "origin_not_allowed" in raw


def test_owner_token_answers_a_needs_you_question(server):
    status, raw = call(server, "/api/question", {"prompt": "Ship it?", "kind": "text"},
                       {"X-CC-Token": server.app.token})
    assert status == 200, raw
    qid = json.loads(raw)["question"]["id"]
    status, raw = call(server, "/api/question/answer", {"id": qid, "answer": "yes"},
                       {OWNER_HEADER: server.app.owner_token()})
    assert status == 200, raw


def test_injected_owner_token_is_used(store, tmp_path):
    app = api.App(store, token="r" * 40, owner_token="o" * 40, owner_token_path=tmp_path / "x.token")
    assert app.owner_token() == "o" * 40
    assert not (tmp_path / "x.token").exists()
