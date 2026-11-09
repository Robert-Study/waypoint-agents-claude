"""Waypoint Core 1.0: API version header, Host guard (DNS rebinding) and fast start-up. Fakes only;
every engine here runs on an ephemeral port with a temp database, never the live engine on 8751."""
import http.client
import json
import socket
import sqlite3
import threading
import time

import pytest

import cc_engine
from cc_engine import api
from cc_engine.store import Store

ALLOWED = "http://127.0.0.1:3080"


@pytest.fixture
def server(store, tmp_path):
    srv = api.make_server(store, host="127.0.0.1", port=0, usage_reader=lambda: [], heartbeat=0.3,
                          poll_interval=0, token_path=tmp_path / "engine.token")
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def call(srv, method, path, body=None, headers=None, host=None):
    port = srv.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if host is not None:
        conn.putrequest(method, path, skip_host=True)
        if host != "":
            conn.putheader("Host", host)
        for k, v in headers.items():
            conn.putheader(k, v)
        if data is not None:
            conn.putheader("Content-Length", str(len(data)))
        conn.endheaders(data)
    else:
        conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    try:
        payload = json.loads(raw) if raw else None
    except ValueError:
        payload = raw
    return resp, payload


# ------------------------------------------------------------------ versions
def test_versions_and_name():
    assert cc_engine.__version__.startswith("1.2.")
    assert "Waypoint engine" in (cc_engine.__doc__ or "")
    assert api.API_VERSION == "1.1"


def test_health_reports_api_version_and_keeps_old_fields(server):
    resp, body = call(server, "GET", "/health")
    assert resp.status == 200
    assert body["api"] == 1 and body["api_version"] == "1.1"
    assert body["ok"] is True and body["service"] == "cc-engine" and body["version"] == cc_engine.__version__
    assert isinstance(body["revision"], int)
    assert resp.getheader("X-Waypoint-API") == "1.1"


def test_every_json_answer_carries_the_api_header(server):
    for method, path, body in (("GET", "/api/projects", None), ("GET", "/api/nope", None),
                               ("POST", "/api/node/status", {"project": "x"}),   # 403, no credential
                               ("GET", "/api/history", None)):                   # 400
        resp, _ = call(server, method, path, body)
        assert resp.status in (200, 400, 403, 404), (path, resp.status)
        assert resp.getheader("X-Waypoint-API") == "1.1", (method, path, resp.status)
    resp, _ = call(server, "GET", "/health", host="evil.example:%d" % server.server_address[1])
    assert resp.status == 403 and resp.getheader("X-Waypoint-API") == "1.1"
    resp, _ = call(server, "OPTIONS", "/api/board", headers={"Origin": ALLOWED})
    assert resp.status == 204 and resp.getheader("X-Waypoint-API") == "1.1"


def test_sse_stream_carries_the_api_header(server):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    conn.request("GET", "/api/stream", headers={"Accept": "text/event-stream"})
    resp = conn.getresponse()
    try:
        assert resp.status == 200 and resp.getheader("X-Waypoint-API") == "1.1"
        assert resp.getheader("Content-Type").startswith("text/event-stream")
    finally:
        conn.close()


# ------------------------------------------------------------------ Host guard
@pytest.mark.parametrize("name", ["127.0.0.1", "localhost", "LOCALHOST", "[::1]"])
def test_loopback_host_with_our_port_is_served(server, name):
    resp, body = call(server, "GET", "/health", host=f"{name}:{server.server_address[1]}")
    assert resp.status == 200 and body["ok"] is True


@pytest.mark.parametrize("host", ["evil.example:{port}", "attacker.test", "127.0.0.1", "localhost",
                                  "127.0.0.1:{other}", "localhost:{other}", "127.0.0.1.evil.example:{port}",
                                  "[::1]", "0.0.0.0:{port}", ""])
def test_any_other_host_is_403_bad_host(server, host):
    port = server.server_address[1]
    value = host.format(port=port, other=port + 1)
    for method, path, body in (("GET", "/health", None), ("GET", "/api/board", None),
                               ("POST", "/api/node/status", {"project": "x", "id": "A", "status": "running",
                                                             "revision": 0})):
        resp, payload = call(server, method, path, body, headers={"Origin": ALLOWED}, host=value)
        assert resp.status == 403, (value, method, path)
        assert payload == {"error": "bad_host"}, (value, payload)
    resp, payload = call(server, "OPTIONS", "/api/board", headers={"Origin": ALLOWED}, host=value)
    assert resp.status == 403 and payload == {"error": "bad_host"}


def test_cors_rules_unchanged_with_a_good_host(server):
    resp, _ = call(server, "GET", "/health", headers={"Origin": ALLOWED})
    assert resp.getheader("Access-Control-Allow-Origin") == ALLOWED
    resp, _ = call(server, "GET", "/health", headers={"Origin": "http://evil.example"})
    assert resp.status == 200 and resp.getheader("Access-Control-Allow-Origin") is None
    resp, body = call(server, "POST", "/api/node/status", {"project": "x", "id": "A", "status": "running",
                                                          "revision": 0}, headers={"Origin": "http://evil.example"})
    assert resp.status == 403 and body["error"] == "origin_not_allowed"


# ------------------------------------------------------------------ start-up
def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_health_answers_before_slow_start_up_work_finishes(store, tmp_path):
    """Binding and /health come first; slow work (usage refresher, bridges, extension imports)
    runs in the background so a slow machine never makes Start-Engine.ps1 give up."""
    gate = threading.Event()
    started = threading.Event()

    def slow():
        started.set()
        gate.wait(20)

    srv = api.start(store, "127.0.0.1", 0, background=[slow], start_refresher=False, usage_reader=lambda: [],
                    poll_interval=0, token_path=tmp_path / "engine.token")
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    try:
        t0 = time.monotonic()
        resp, body = call(srv, "GET", "/health")
        assert resp.status == 200 and body["ok"] is True
        assert time.monotonic() - t0 < 3
        assert started.wait(5) and not gate.is_set()  # the slow step was still running
        # the owner token was written before the bind, and a request gets that same token
        tok = srv.app.owner_token()
        assert tok and (tmp_path / "owner.token").read_text(encoding="utf-8").strip() == tok
    finally:
        gate.set()
        srv.shutdown()
        srv.server_close()


def test_heavy_warm_up_waits_for_the_first_answer(store, tmp_path):
    """Extension imports hold the GIL for seconds; they start only after the first request is answered
    (or after ``warm_up_delay``), so they never compete with the first /health."""
    ran = threading.Event()
    srv = api.start(store, "127.0.0.1", 0, background=[ran.set], start_refresher=False, warm_up_delay=30,
                    usage_reader=lambda: [], poll_interval=0, token_path=tmp_path / "engine.token")
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    try:
        assert not ran.wait(0.7)
        assert call(srv, "GET", "/health")[0].status == 200
        assert ran.wait(5)
    finally:
        srv.shutdown()
        srv.server_close()
    srv2 = api.start(store, "127.0.0.1", 0, background=[(done := threading.Event()).set], start_refresher=False,
                     warm_up_delay=0.2, usage_reader=lambda: [], poll_interval=0, token_path=tmp_path / "engine.token")
    try:
        assert done.wait(5)  # nobody asked: the warm-up still runs after the delay
    finally:
        srv2.app.close()
        srv2.server_close()


def test_owner_token_is_made_once_under_a_race(store, tmp_path):
    app = api.App(store, usage_reader=lambda: [], poll_interval=0, token_path=tmp_path / "engine.token")
    got, threads = [], []
    for _ in range(8):
        th = threading.Thread(target=lambda: got.append(app.owner_token()))
        threads.append(th)
        th.start()
    for th in threads:
        th.join(20)
    assert len(set(got)) == 1 and (tmp_path / "owner.token").read_text(encoding="utf-8").strip() == got[0]


def test_app_does_not_wait_on_icacls_for_an_existing_token(store, tmp_path, monkeypatch):
    path = tmp_path / "engine.token"
    token = api.ensure_token(path)
    calls = []

    def slow_restrict(p):
        calls.append(str(p))
        time.sleep(3)

    monkeypatch.setattr(api, "_restrict_to_user", slow_restrict)
    t0 = time.monotonic()
    app = api.App(store, usage_reader=lambda: [], poll_interval=0, token_path=path)
    assert time.monotonic() - t0 < 1.5
    assert app.token == token


def test_opening_a_current_database_takes_no_write_lock(tmp_path):
    path = tmp_path / "cc.sqlite"
    Store(path).close()  # create and migrate once
    holder = sqlite3.connect(str(path), timeout=1, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")  # another process (the tick, a CLI) holds the write lock
    done, errors = threading.Event(), []

    def open_it():
        try:
            s = Store(path)
            s.revision()
            s.close()
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)
        done.set()

    th = threading.Thread(target=open_it, daemon=True)
    th.start()
    try:
        assert done.wait(3), "Store() waited for another connection's write lock"
        assert not errors
    finally:
        holder.execute("ROLLBACK")
        holder.close()
        th.join(35)
