"""dsh shows the same live data as Waypoint Claude: the engine bridges the Waypoint Claude tools (wpc_bridge)."""
import json
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from cc_engine import wpc_bridge
from cc_engine.api import make_server

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "core" / "waypoint-data" / "fixtures" / "sample-folder"
OWNER_ORIGIN = "http://127.0.0.1:3080"
TOKEN = "router-test-token"


class _StubPrefs:
    @staticmethod
    def handle(message):
        return {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text",
                "text": json.dumps({"pending": [], "counts": {}})}], "isError": False}}


@pytest.fixture
def base(store, tmp_path, monkeypatch):
    data = tmp_path / "data"
    shutil.copytree(FIXTURE, data)
    monkeypatch.setenv("CC_DATA_DIR", str(data))
    # Preference tools read Owner's live registry: keep the test on a stub.
    # Preference tools are an add-on read from Owner's registry: keep the test on a stub.
    monkeypatch.setattr(wpc_bridge.wpc_http, "prefs_module", lambda folder: _StubPrefs)
    srv = make_server(store, port=0, poll_interval=0, token=TOKEN)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", data
    srv.shutdown()


def call(url, body, who="owner"):
    headers = {"Content-Type": "application/json"}
    if who == "owner":
        headers["Origin"] = OWNER_ORIGIN
    elif who == "router":
        headers["X-CC-Token"] = TOKEN
    req = urllib.request.Request(url + "/api/wpc/call", data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_board_read_comes_from_the_file_data_folder(base):
    url, data = base
    status, body = call(url, {"tool": "board_read", "input": {}})
    assert status == 200 and body["isError"] is False
    ids = {p["id"] for p in body["payload"]["projects"]}
    on_disk = {p.name for p in (data / "projects").iterdir() if p.is_dir()}
    assert ids and ids <= on_disk


def test_router_may_read_but_not_write(base):
    url, _ = base
    assert call(url, {"tool": "ops_read", "input": {}}, who="router")[0] == 200
    status, body = call(url, {"tool": "todo_tick", "input": {"id": "t-x"}}, who="router")
    assert status == 403 and body["error"] == "owner_only"


def test_only_page_tools_are_reachable(base):
    url, _ = base
    status, body = call(url, {"tool": "node_status", "input": {}})
    assert status == 400 and body["error"] == "unknown_tool"
    assert call(url, {"input": {}})[0] == 400


def test_owner_write_reaches_the_tool(base):
    url, _ = base
    status, body = call(url, {"tool": "pref_list", "input": {}})
    assert status == 200 and body["payload"] == {"pending": [], "counts": {}}
    status, body = call(url, {"tool": "todo_tick", "input": {"id": "t-no-such"}})
    assert status == 200 and body["isError"] is True  # the tool ran and refused, as in Waypoint Claude


def test_page_is_the_current_waypoint_claude_html(base):
    if not (REPO / "artifacts" / "waypoint-claude.html").is_file():
        pytest.skip("this tree ships the engine without the dashboard page")
    url, _ = base
    with urllib.request.urlopen(url + "/api/wpc/page") as r:
        html = r.read()
        assert r.headers["Content-Type"].startswith("text/html")
    assert html == (REPO / "artifacts" / "waypoint-claude.html").read_bytes()


def test_no_request_without_credentials(base):
    url, _ = base
    assert call(url, {"tool": "board_read", "input": {}}, who="nobody")[0] == 403
