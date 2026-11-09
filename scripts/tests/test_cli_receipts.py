import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import waypoint_cli as w  # noqa: E402

TREE = {"roots": ["s1"], "at": "x", "agents": [
    {"id": "s1", "title": "Lead", "tool": "Bash", "tokens_in": 1000, "tokens_out": 234, "context": 50,
     "context_window": 200, "status": "running", "parent": None, "last_line": "Running tests"},
    {"id": "s1/a", "title": "Worker", "tool": None, "tokens_in": 5, "tokens_out": 5, "context": 0,
     "context_window": 0, "status": "done", "parent": "s1", "last_line": ""}]}
RECEIPTS = {"receipts": [{"ingested_at": "2026-11-06T10:00:00+00:00", "status": "accepted", "claimed": "done",
                          "project": "p", "node": "A", "summary": "Did it.", "outcome": "check passed"},
                         {"ingested_at": "2026-11-06T09:00:00+00:00", "status": "invalid", "claimed": None,
                          "project": None, "node": None, "summary": None, "reason": "bad"}]}
SEEN = []


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        SEEN.append(self.path)
        p = self.path.split("?")[0]
        body = {"/api/trace/tree": TREE, "/api/receipts": RECEIPTS}.get(p)
        raw = json.dumps(body if body is not None else {}).encode()
        self.send_response(200 if body is not None else 404)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for name in ("WAYPOINT_URL", "WAYPOINT_PORT", "CC_DATA_DIR", "WAYPOINT_DATA_DIR", "CC_TOKEN_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WAYPOINT_CONFIG", str(tmp_path / "no-config.json"))
    monkeypatch.setenv("WAYPOINT_URL", "http://127.0.0.1:1")


@pytest.fixture()
def srv(monkeypatch, tmp_path):
    s = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    monkeypatch.setenv("WAYPOINT_URL", "http://127.0.0.1:%d" % s.server_port)
    monkeypatch.setenv("CC_DATA_DIR", str(tmp_path / "data"))
    SEEN.clear()
    yield s
    s.shutdown()


def run(capsys, *argv):
    code = w.main(list(argv))
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def test_receipt_write_node_file(tmp_path, capsys):
    code, o, _ = run(capsys, "receipt", "write", "--status", "done", "--project", "docs", "--node", "B3",
                     "--summary", "The guide is written.", "--evidence", "3 passed", "--evidence", "ok",
                     "--output", "core/docs/API.md", "--todo", "Read it", "--todo-section", "human",
                     "--todo-link", "core/docs/API.md", "--cwd", str(tmp_path))
    target = tmp_path / ".waypoint" / "receipts" / "docs.B3.json"
    assert code == 0 and o.strip() == str(target)
    obj = json.loads(target.read_text(encoding="utf-8"))
    assert obj["schema"] == "waypoint.receipt.v1" and obj["status"] == "done"
    assert obj["project"] == "docs" and obj["node"] == "B3" and obj["evidence"] == ["3 passed", "ok"]
    assert obj["todo"] == {"text": "Read it", "section": "human", "link": "core/docs/API.md"}
    assert not [p for p in target.parent.iterdir() if p.name.endswith(".tmp")]


def test_receipt_write_session_default_name_and_replace(tmp_path, capsys):
    for text in ("First.", "Second."):
        code, o, _ = run(capsys, "receipt", "write", "--status", "needs_you", "--summary", text,
                         "--cwd", str(tmp_path))
        assert code == 0
    files = list((tmp_path / ".waypoint" / "receipts").iterdir())
    assert [f.name for f in files] == ["session.json"]
    assert json.loads(files[0].read_text())["summary"] == "Second."
    code, o, _ = run(capsys, "receipt", "write", "--status", "done", "--summary", "x", "--name", "release-notes.json",
                     "--cwd", str(tmp_path))
    assert code == 0 and o.strip().endswith("release-notes.json")


@pytest.mark.parametrize("extra", [
    ["--summary", "x" * 301],
    ["--summary", "ok", "--project", "p"],
    ["--summary", "ok", "--evidence", "a" * 501],
    ["--summary", "ok", "--output", "p"] * 0 + ["--summary", "ok", "--todo-link", "l"],
    ["--summary", "ok", "--name", "bad name"],
    ["--summary", "ok", "--output", "a", "--output", "b"] + ["--evidence", "e"] * 21,
])
def test_receipt_write_refuses_bad_input(tmp_path, capsys, extra):
    code, _, err = run(capsys, "receipt", "write", "--status", "done", "--cwd", str(tmp_path), *extra)
    assert code == w.EXIT_USAGE and "receipt:" in err
    assert not (tmp_path / ".waypoint").exists() or not list((tmp_path / ".waypoint" / "receipts").glob("*.json"))


def test_receipt_write_needs_no_engine(tmp_path, capsys):
    code, _, _ = run(capsys, "receipt", "write", "--status", "error", "--summary", "Broke.", "--cwd", str(tmp_path))
    assert code == 0  # WAYPOINT_URL points at a dead port


def test_receipt_list(srv, capsys):
    code, o, _ = run(capsys, "receipt", "list", "--limit", "5")
    assert code == 0 and "limit=5" in SEEN[-1]
    assert "accepted" in o and "p/A" in o and "check passed" in o and "invalid" in o and "(bad)" in o
    code, o, _ = run(capsys, "receipt", "list", "--json")
    assert json.loads(o)["receipts"][0]["node"] == "A"


def test_tree_plain_text(srv, capsys):
    code, o, _ = run(capsys, "tree")
    lines = o.splitlines()
    assert code == 0 and SEEN[-1] == "/api/trace/tree"
    assert lines[0] == "* Lead  Bash  1234  25%  Running tests"
    assert lines[1] == "  + Worker  -  10  -"
    code, o, _ = run(capsys, "tree", "--json")
    assert json.loads(o)["roots"] == ["s1"]


POSTED = []


class P(H):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        POSTED.append((self.path, body))
        if self.path == "/api/claim" and body.get("by") == "rival":
            code, out = 409, {"error": "claimed", "detail": "other is working on task T3"}
        elif self.path == "/api/claim":
            code, out = 200, {"ok": True, "claim": {"by": body["by"]}, "refreshed": False}
        else:
            code, out = 200, {"ok": True, "released": True, "status": "not_started"}
        raw = json.dumps(out).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture()
def srv_post(monkeypatch, tmp_path):
    s = HTTPServer(("127.0.0.1", 0), P)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    monkeypatch.setenv("WAYPOINT_URL", "http://127.0.0.1:%d" % s.server_port)
    monkeypatch.setenv("CC_DATA_DIR", str(tmp_path / "data"))
    POSTED.clear()
    yield s
    s.shutdown()


def test_claim_and_release(srv_post, capsys):
    code, o, _ = run(capsys, "claim", "p", "T3", "--by", "w1", "--cwd", "C:/wt", "--env", "desktop")
    assert code == 0 and "claimed p/T3 for w1" in o
    assert POSTED[-1] == ("/api/claim", {"project": "p", "node": "T3", "by": "w1", "cwd": "C:/wt",
                                         "environment": "desktop"})
    code, o, _ = run(capsys, "release", "p", "T3")
    assert code == 0 and "not started" in o and POSTED[-1] == ("/api/release", {"project": "p", "node": "T3"})
    code, _, err = run(capsys, "claim", "p", "T3", "--by", "rival")
    assert code == w.EXIT_ERROR and "claim refused" in err
