import ast
import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import waypoint_cli_engine as w  # noqa: E402  (the 1.1 engine CLI; waypoint_cli.py is the 1.2 folder CLI)

LOG = []
STATE = {"board_rev": 7, "stale": False, "forbid": False, "api": None,
         "health": {"ok": True, "service": "cc-engine", "version": "1.0.0", "revision": 7, "api": 1, "api_version": "1.0"}}
BOARD = {"project": {"id": "p"}, "revision": 7, "fields": ["id", "title", "status", "display_status"],
         "nodes": [["A", "first", "done", "done"], ["B", "second", "not_started", "ready"]]}


HISTORY = {"family": {"id": "fam", "title": "Family"}, "current": "p",
           "versions": [{"project": "old", "title": "Old", "version": 1, "archived": True, "status": "active",
                         "created_at": "x", "counts": {"done": 1, "open": 0, "skipped": 1, "carried": 2}},
                        {"project": "p", "title": "Proj", "version": 2, "archived": False, "status": "active",
                         "created_at": "y", "counts": {"done": 1, "open": 1, "skipped": 0, "carried": 0}}],
           "skipped": [{"project": "old", "version": 1, "id": "U7", "title": "Idea", "goal": "g",
                        "reason": "superseded", "at": "z"}]}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        if STATE["api"]:
            self.send_header("X-Waypoint-API", STATE["api"])
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        LOG.append(("GET", self.path, None, {k.lower(): v for k, v in self.headers.items()}))
        p = self.path.split("?")[0]
        if p in ("/health", "/api/health"):
            return self._send(200, STATE["health"])
        if p == "/api/board":
            return self._send(200, dict(BOARD, revision=STATE["board_rev"]))
        if p == "/api/projects":
            return self._send(200, {"projects": [{"id": "p", "title": "Proj"}]})
        if p == "/api/node":
            return self._send(200, {"node": {"id": "B", "status": "not_started", "title": "t", "goal": "g"},
                                    "events": [{"kind": "x"}], "revision": 7})
        if p == "/api/usage":
            return self._send(200, {"pools": [{"pool": "a", "remaining_percent": 75.0, "status": "FRESH"},
                                              {"pool": "b", "remaining_percent": None, "status": "UNKNOWN"}]})
        if p == "/api/todos":
            return self._send(200, {"todos": [{"id": "td1", "section": "human", "text": "h", "done": False},
                                              {"id": "td2", "section": "computer", "text": "c", "done": False}]})
        if p == "/api/history":
            qs = dict(x.split("=", 1) for x in self.path.split("?", 1)[1].split("&"))
            if qs.get("family") == "fam" or qs.get("project") in ("p", "old"):
                return self._send(200, HISTORY)
            return self._send(404, {"error": "not_found"})
        self._send(404, {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        LOG.append(("POST", self.path, body, {k.lower(): v for k, v in self.headers.items()}))
        if STATE["forbid"]:
            return self._send(403, {"error": "forbidden"})
        if self.path.startswith("/api/version/") and STATE["stale"]:
            return self._send(409, {"error": "stale", "revision": 99})
        if self.path == "/api/version/new":
            return self._send(200, {"ok": True, "project": "fam-v2", "version": 2,
                                    "carried": ["B"], "skipped": [], "revision": 8})
        if self.path == "/api/version/decide":
            return self._send(200, {"ok": True, "carried": [{"from": "B", "to": "B"}], "skipped": [], "revision": 8})
        if self.path == "/api/node/status" and STATE["stale"]:
            return self._send(409, {"error": "stale", "revision": 99})
        if self.path == "/api/run":
            return self._send(200, {"run": {"id": "run-1"}})
        if self.path == "/api/todos":
            return self._send(200, {"todo": {"id": "td9"}})
        self._send(200, {"ok": True})


HEALTH = dict(STATE["health"])


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    """No test reads the real settings file, data folder, Claude config or engine."""
    for name in ("WAYPOINT_URL", "WAYPOINT_PORT", "CC_DATA_DIR", "WAYPOINT_DATA_DIR", "CC_TOKEN_FILE",
                 "WAYPOINT_PYTHON"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WAYPOINT_CONFIG", str(tmp_path / "no-config.json"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "no-claude"))
    monkeypatch.setenv("WAYPOINT_URL", "http://127.0.0.1:1")  # nothing listens; srv overrides it


@pytest.fixture()
def srv(monkeypatch, tmp_path):
    s = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    data = tmp_path / "data"
    data.mkdir()
    (data / "owner.token").write_text("OWNER-SECRET\n")
    tok = data / "engine.token"  # the engine puts owner.token beside CC_TOKEN_FILE
    tok.write_text("SECRET-TOKEN")
    monkeypatch.setenv("WAYPOINT_URL", "http://127.0.0.1:%d" % s.server_port)
    monkeypatch.setenv("CC_TOKEN_FILE", str(tok))
    monkeypatch.setenv("CC_DATA_DIR", str(data))
    LOG.clear()
    STATE.update(board_rev=7, stale=False, forbid=False, api=None, health=dict(HEALTH))
    yield s
    s.shutdown()


def run(capsys, *argv):
    code = w.main(list(argv))
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def test_board_list_and_project(srv, capsys):
    code, o, _ = run(capsys, "board")
    assert code == 0 and "p  Proj" in o
    code, o, _ = run(capsys, "board", "p")
    assert code == 0 and "2 nodes, 1 open" in o and "B [" in o and "A [" not in o
    code, o, _ = run(capsys, "board", "p", "--json")
    assert json.loads(o)["nodes_unpacked"][1]["id"] == "B"


def test_node(srv, capsys):
    code, o, _ = run(capsys, "node", "p", "B")
    assert code == 0 and "goal: g" in o and "event" in o
    assert "id=B" in LOG[0][1] and "project=p" in LOG[0][1]


def test_done_sends_revision_and_token(srv, capsys):
    code, o, _ = run(capsys, "done", "p", "B", "--evidence", "ok")
    assert code == 0
    method, path, body, hdr = LOG[-1]
    assert (method, path) == ("POST", "/api/node/status")
    assert body == {"project": "p", "id": "B", "status": "done", "evidence": "ok", "revision": 7}
    assert hdr.get("x-cc-token") == "SECRET-TOKEN" and "origin" not in hdr
    assert "SECRET-TOKEN" not in o


def test_owner_sends_the_owner_token_never_an_origin(srv, capsys):
    code, o, e = run(capsys, "status", "p", "B", "blocked", "--evidence", "e", "--owner")
    assert code == 0
    hdr = LOG[-1][3]
    assert hdr.get("x-cc-owner-token") == "OWNER-SECRET" and "x-cc-token" not in hdr and "origin" not in hdr
    assert LOG[-1][2]["status"] == "blocked"
    assert "OWNER-SECRET" not in o + e


def test_owner_without_token_file_is_exit_4_and_sends_nothing(srv, capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("CC_DATA_DIR", str(tmp_path / "empty"))
    monkeypatch.delenv("CC_TOKEN_FILE")  # else owner.token is looked for beside it
    code, o, err = run(capsys, "done", "p", "B", "--evidence", "x", "--owner")
    assert code == 4 and "owner.token" in err and "waypoint engine start" in err
    assert len(err.strip().splitlines()) == 1
    assert not [l for l in LOG if l[0] == "POST"]


def test_owner_token_from_settings_file_data_dir(srv, capsys, tmp_path, monkeypatch):
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "owner.token").write_text("FROM-CONFIG")
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"data_dir": str(other)}))
    monkeypatch.delenv("CC_DATA_DIR")
    monkeypatch.delenv("CC_TOKEN_FILE")
    monkeypatch.setenv("WAYPOINT_CONFIG", str(cfg))
    assert run(capsys, "done", "p", "B", "--evidence", "x", "--owner")[0] == 0
    assert LOG[-1][3].get("x-cc-owner-token") == "FROM-CONFIG"


def test_owner_token_sits_beside_cc_token_file_like_the_engine(srv, capsys, tmp_path, monkeypatch):
    # engine/cc_engine/api.py: owner_token_path = token_path.with_name("owner.token"), and token_path is
    # CC_TOKEN_FILE or <data dir>/engine.token. The CLI must look in the same place.
    tokens = tmp_path / "tokens"
    tokens.mkdir()
    (tokens / "engine.token").write_text("SECRET-TOKEN")
    (tokens / "owner.token").write_text("BESIDE-TOKEN-FILE")  # <data dir>/owner.token still says OWNER-SECRET
    monkeypatch.setenv("CC_TOKEN_FILE", str(tokens / "engine.token"))
    assert w.owner_token_path() == tokens / "owner.token"
    assert run(capsys, "done", "p", "B", "--evidence", "x", "--owner")[0] == 0
    assert LOG[-1][3].get("x-cc-owner-token") == "BESIDE-TOKEN-FILE"
    monkeypatch.delenv("CC_TOKEN_FILE")
    assert w.owner_token_path() == tmp_path / "data" / "owner.token"


def test_stale_exit_3_no_retry(srv, capsys):
    STATE["stale"] = True
    code, _, err = run(capsys, "done", "p", "B", "--evidence", "x")
    assert code == 3 and "99" in err
    assert sum(1 for l in LOG if l[0] == "POST") == 1


def test_retry_refetches_once(srv, capsys):
    STATE["stale"] = True
    code, _, _ = run(capsys, "done", "p", "B", "--evidence", "x", "--retry")
    assert code == 3
    assert sum(1 for l in LOG if l[0] == "POST") == 2
    assert sum(1 for l in LOG if l[1].startswith("/api/board")) == 2


def test_forbidden_exit_4(srv, capsys):
    STATE["forbid"] = True
    code, _, err = run(capsys, "done", "p", "B", "--evidence", "x")
    assert code == 4 and "forbidden" in err and "SECRET-TOKEN" not in err


def test_unreachable_exit_5(monkeypatch, capsys):
    monkeypatch.setenv("WAYPOINT_URL", "http://127.0.0.1:1")
    code, _, err = run(capsys, "usage")
    assert code == 5 and "unreachable" in err


def test_usage_error_exit_2(srv, capsys):
    assert run(capsys, "done", "p", "B")[0] == 2
    assert run(capsys, "bogus")[0] == 2


def test_todos(srv, capsys):
    code, o, _ = run(capsys, "todo", "list", "--section", "human")
    assert "td1" in o and "td2" not in o and "For you (1)" in o
    run(capsys, "todo", "add", "do it", "--human", "--position", "0", "--link", "http://x", "--node", "p/B")
    assert LOG[-1][2] == {"text": "do it", "section": "human", "position": 0, "link": "http://x",
                          "node": {"project": "p", "id": "B"}}
    run(capsys, "todo", "tick", "td1")
    assert LOG[-1][1] == "/api/todos/update" and LOG[-1][2] == {"id": "td1", "done": True}
    run(capsys, "todo", "reorder", "--section", "human", "a", "b")
    assert LOG[-1][2] == {"section": "human", "ids": ["a", "b"]}


def test_run_and_usage(srv, capsys):
    code, o, _ = run(capsys, "run", "p", "--settings", '{"a":1}')
    assert "run-1" in o and LOG[-1][2] == {"project": "p", "settings": {"a": 1}}
    code, o, _ = run(capsys, "usage")
    assert "a: 75% left FRESH" in o and "b: unknown UNKNOWN" in o


# ---- sync write ----
import subprocess  # noqa: E402

ENGINE = Path(__file__).resolve().parents[2] / "engine"


def _git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True, check=True)


@pytest.fixture
def syncrepo(tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    repo = tmp_path / "work"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "README").write_text("x")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "init")
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-u", "origin", "main")
    return repo, bare


def _sync(capsys, repo, *args):
    return run(capsys, "sync", *args, "--repo", str(repo), "--session", "cloud1")


def test_sync_write_and_importer_parses(syncrepo, capsys):
    repo, _ = syncrepo
    assert _sync(capsys, repo, "status", "p", "N1", "submitted", "--evidence", "tests pass", "--base-revision", "42")[0] == 0
    assert _sync(capsys, repo, "todo", "Review the draft", "--human", "--node", "p/N1")[0] == 0
    assert _sync(capsys, repo, "receipt", "p", "N1", "--summary", "done it", "--link", "http://a", "http://b")[0] == 0
    files = list((repo / "sync" / "inbox").glob("*.json"))
    assert len(files) == 1 and files[0].name.endswith("-cloud1-1.json")
    doc = json.loads(files[0].read_text())
    assert doc["schema"] == "waypoint.sync.v1" and doc["session"] == "cloud1"
    assert [i["kind"] for i in doc["items"]] == ["node_status", "todo_add", "receipt"]
    assert doc["items"][1] == {"kind": "todo_add", "text": "Review the draft", "section": "human",
                               "node": {"project": "p", "id": "N1"}}
    assert doc["items"][2]["links"] == ["http://a", "http://b"]
    # the importer accepts every item (unknown task -> conflict is a data matter, not a format one)
    import ast
    tree = ast.parse((ENGINE / "cc_engine" / "cloud_sync.py").read_text(encoding="utf-8"))  # no engine import
    consts = {t.targets[0].id: ast.literal_eval(t.value) for t in tree.body
              if isinstance(t, ast.Assign) and isinstance(t.targets[0], ast.Name)
              and t.targets[0].id in ("SCHEMA", "RANK")}

    class cs:
        SCHEMA, RANK = consts["SCHEMA"], consts["RANK"]
    assert doc["schema"] == cs.SCHEMA
    assert all(i["status"] in cs.RANK for i in doc["items"] if i["kind"] == "node_status")
    assert isinstance(doc["items"][0]["base_revision"], int)


def test_sync_rejects_bad_input(syncrepo, capsys):
    repo, _ = syncrepo
    assert _sync(capsys, repo, "status", "p", "N1", "bogus", "--evidence", "e", "--base-revision", "1")[0] == 2
    assert _sync(capsys, repo, "todo", "x", "--node", "noslash")[0] == 2
    assert _sync(capsys, repo, "todo", "   ")[0] == 2
    assert not (repo / "sync" / "inbox").exists() or not list((repo / "sync" / "inbox").glob("*.json"))


def test_sync_push_refuses_without_backup_github(syncrepo, capsys):
    repo, bare = syncrepo
    _sync(capsys, repo, "todo", "t")
    code, _, err = _sync(capsys, repo, "push")
    assert code == 4 and "backup.github" in err
    assert "sync" not in _git(bare, "ls-tree", "-r", "--name-only", "main").stdout


def test_sync_push_commits_and_pushes(syncrepo, capsys):
    repo, bare = syncrepo
    _git(repo, "config", "backup.github", "private/repo")
    _sync(capsys, repo, "todo", "t")
    code, o, _ = _sync(capsys, repo, "push")
    assert code == 0 and "pushed 1" in o
    assert "sync/inbox/" in _git(bare, "ls-tree", "-r", "--name-only", "main").stdout
    assert "Waypoint cloud sync" in _git(bare, "log", "-1", "--format=%s", "main").stdout
    _sync(capsys, repo, "todo", "t2")  # committed file is not reused: a new -2 file
    names = sorted(p.name for p in (repo / "sync" / "inbox").glob("*.json"))
    assert len(names) == 2 and names[1].endswith("-cloud1-2.json")


# ---- global flags combine with subcommand flags ----
@pytest.mark.parametrize("argv", [["--owner", "status", "p", "B", "done", "--evidence", "e"],
                                  ["status", "--owner", "p", "B", "done", "--evidence", "e"],
                                  ["status", "p", "B", "done", "--evidence", "e", "--owner"]])
def test_owner_flag_works_in_every_position(srv, capsys, argv):
    assert run(capsys, *argv)[0] == 0
    hdr = LOG[-1][3]
    assert hdr.get("x-cc-owner-token") == "OWNER-SECRET" and "x-cc-token" not in hdr and "origin" not in hdr


def test_json_flag_works_before_and_after(srv, capsys):
    for argv in (["--json", "board", "p"], ["board", "p", "--json"]):
        assert json.loads(run(capsys, *argv)[1])["nodes_unpacked"]
    assert "nodes_unpacked" not in run(capsys, "board", "p")[1]


# ---- version ----
def test_version_history(srv, capsys):
    code, o, _ = run(capsys, "version", "history", "fam")
    assert code == 0 and "v2 p *current*" in o and "v1 old" in o and "[archived]" in o
    assert "U7" in o and "superseded" in o
    code, o, _ = run(capsys, "version", "history", "p")  # a project id falls back to project=
    assert code == 0 and "*current*" in o
    assert any(l[1].startswith("/api/history?project=p") for l in LOG)
    assert run(capsys, "version", "history", "nope")[0] == 1


def test_version_new_sends_decisions_and_revision(srv, capsys):
    code, o, _ = run(capsys, "version", "new", "fam", "--skip", "B=not needed now", "--title", "Next", "--id", "fam-v2")
    assert code == 0 and "fam-v2" in o
    method, path, body, hdr = LOG[-1]
    assert (method, path) == ("POST", "/api/version/new")
    assert body == {"family": "fam", "title": "Next", "id": "fam-v2",
                    "decisions": {"B": {"action": "skip", "reason": "not needed now"}}, "revision": 7}
    assert hdr.get("x-cc-token") == "SECRET-TOKEN"
    run(capsys, "version", "new", "p", "--carry", "B", "--owner")
    assert LOG[-1][2] == {"project": "p", "decisions": {"B": {"action": "carry"}}, "revision": 7}
    assert LOG[-1][3].get("x-cc-owner-token") == "OWNER-SECRET"


def test_version_new_dry_run_writes_nothing(srv, capsys):
    code, o, _ = run(capsys, "version", "new", "fam", "--skip", "B=later", "--dry-run")
    assert code == 0 and "skip B" in o and "later" in o and "nothing written" in o
    assert not [l for l in LOG if l[0] == "POST"]
    code, o, _ = run(capsys, "version", "new", "fam", "--dry-run")
    assert "carry B" in o and "A " not in o.replace("dry run", "")


def test_version_usage_errors(srv, capsys):
    assert run(capsys, "version", "new", "fam", "--skip", "B")[0] == 2          # no reason
    assert run(capsys, "version", "new", "fam", "--skip", "B=   ")[0] == 2
    assert run(capsys, "version", "new", "fam", "--skip", "B=x", "--carry", "B")[0] == 2
    assert run(capsys, "version", "new", "fam", "--carry", "A")[0] == 2         # A is done, not unfinished
    assert run(capsys, "version", "decide", "p")[0] == 2                        # nothing to decide
    assert not [l for l in LOG if l[0] == "POST"]


def test_version_stale_exit_3(srv, capsys):
    STATE["stale"] = True
    code, _, err = run(capsys, "version", "new", "fam")
    assert code == 3 and "99" in err
    code, _, err = run(capsys, "version", "decide", "p", "--carry", "B")
    assert code == 3 and "99" in err


def test_version_decide_and_family(srv, capsys):
    code, o, _ = run(capsys, "version", "decide", "p", "--carry", "B")
    assert code == 0 and LOG[-1][1] == "/api/version/decide"
    assert LOG[-1][2] == {"project": "p", "decisions": {"B": {"action": "carry"}}, "revision": 7}
    code, o, _ = run(capsys, "version", "family", "fam", "--title", "Family", "old", "p")
    assert code == 0
    method, path, body, hdr = LOG[-1]
    assert path == "/api/version/family" and hdr.get("x-cc-owner-token") == "OWNER-SECRET" and "origin" not in hdr
    assert body == {"family": "fam", "title": "Family", "members": ["old", "p"], "revision": 7}
    STATE["forbid"] = True
    assert run(capsys, "version", "decide", "p", "--carry", "B")[0] == 4


def test_version_new_of_an_earlier_version_gives_the_clear_error_and_writes_nothing(srv, capsys):
    code, o, err = run(capsys, "version", "new", "old", "--carry", "B")
    assert code == 2 and "only the current version (p) can roll over" in err
    assert any(l[1].startswith("/api/history?project=old") for l in LOG)  # resolved through its family
    assert not [l for l in LOG if l[0] == "POST"]


def test_version_new_of_a_family_member_that_is_current_works_and_uses_the_family_current(srv, capsys):
    code, o, _ = run(capsys, "version", "new", "p", "--dry-run")
    assert code == 0 and "dry run for p" in o and "carry B" in o
    assert any(l[1].startswith("/api/history?project=p") for l in LOG)
    assert any(l[1] == "/api/board?project=p" for l in LOG)
    code, o, _ = run(capsys, "version", "new", "p", "--carry", "B")
    assert code == 0 and LOG[-1][1] == "/api/version/new" and LOG[-1][2]["project"] == "p"


# ---- base URL, data folder ----
def test_base_url_order(monkeypatch):
    monkeypatch.delenv("WAYPOINT_URL")
    assert w.base_url() == "http://127.0.0.1:8751"
    monkeypatch.setenv("WAYPOINT_PORT", "8760")
    assert w.base_url() == "http://127.0.0.1:8760"
    monkeypatch.setenv("WAYPOINT_URL", "http://127.0.0.1:9999/")
    assert w.base_url() == "http://127.0.0.1:9999"


def test_bad_port_is_a_usage_error(monkeypatch, capsys):
    monkeypatch.delenv("WAYPOINT_URL")
    monkeypatch.setenv("WAYPOINT_PORT", "eighty")
    code, _, err = run(capsys, "board")
    assert code == 2 and "WAYPOINT_PORT" in err


def test_data_dir_order(monkeypatch, tmp_path):
    assert w.data_dir() == w.REPO / "data"
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"data_dir": "D:/cfg"}))
    monkeypatch.setenv("WAYPOINT_CONFIG", str(cfg))
    assert w.data_dir() == Path("D:/cfg")
    monkeypatch.setenv("WAYPOINT_DATA_DIR", "D:/wp")
    assert w.data_dir() == Path("D:/wp")
    monkeypatch.setenv("CC_DATA_DIR", "D:/cc")
    assert w.data_dir() == Path("D:/cc")
    assert w.router_token_path() == Path("D:/cc") / "engine.token"
    assert w.owner_token_path() == Path("D:/cc") / "owner.token"


# ---- API version ----
@pytest.mark.parametrize("api", [None, "1.0", "1.7"])
def test_same_api_major_is_fine(srv, capsys, api):
    STATE["api"] = api
    assert run(capsys, "board", "p")[0] == 0


def test_other_api_major_is_exit_6(srv, capsys):
    STATE["api"] = "2.0"
    code, o, err = run(capsys, "board", "p")
    assert code == 6 and "API 2.0" in err and len(err.strip().splitlines()) == 1 and not o
    code, _, _ = run(capsys, "done", "p", "B", "--evidence", "x")
    assert code == 6 and not [l for l in LOG if l[0] == "POST"]  # caught on the read, before the write


# ---- --version ----
def test_version_flag_with_engine(srv, capsys):
    code, o, _ = run(capsys, "--version")
    assert code == 0 and o.strip() == "waypoint %s (engine 1.0.0, API 1.0)" % w.__version__
    STATE["health"] = {"ok": True, "version": "0.1.0", "revision": 3}  # an older engine: no api field = 1
    assert run(capsys, "--version")[1].strip() == "waypoint %s (engine 0.1.0, API 1.0)" % w.__version__
    assert json.loads(run(capsys, "--json", "--version")[1]) == {"cli": w.__version__, "engine": "0.1.0", "api": "1.0"}


@pytest.mark.parametrize("argv", [["board", "--version"], ["--version", "board"], ["node", "--version"],
                                  ["todo", "add", "--version"], ["--owner", "done", "--version"]])
def test_version_flag_before_or_after_the_command(srv, capsys, argv):
    code, o, e = run(capsys, *argv)
    assert code == 0 and o.strip() == "waypoint %s (engine 1.0.0, API 1.0)" % w.__version__, (o, e)
    assert not [l for l in LOG if l[1].startswith("/api/")]  # the command itself does not run
    assert json.loads(run(capsys, *(argv + ["--json"]))[1])["cli"] == w.__version__


def test_version_flag_without_engine(capsys):
    code, o, _ = run(capsys, "--version")
    assert code == 0 and o.strip() == "waypoint %s (engine not running)" % w.__version__
    assert w.__version__ == "1.1.0"


# ---- doctor ----
def _plugins(tmp_path, monkeypatch, names):
    root = tmp_path / "claude"
    (root / "plugins").mkdir(parents=True)
    (root / "plugins" / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {n: [] for n in names}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))


def test_doctor_all_ok(srv, capsys, tmp_path, monkeypatch):
    _plugins(tmp_path, monkeypatch, ["other@x", "waypoint@waypoint-local"])
    monkeypatch.setenv("CC_TOKEN_FILE", str(tmp_path / "data" / "engine.token"))
    code, o, _ = run(capsys, "doctor")
    lines = o.strip().splitlines()
    assert code == 0, o
    assert [l.split()[1] for l in lines[:-1]] == ["python", "engine", "api", "tokens", "mod", "mod-token"]
    assert all(l.startswith("ok") for l in lines[:-1]) and lines[-1] == "all checks passed"
    assert str(tmp_path / "data" / "owner.token") in lines[5]
    assert "SECRET" not in o
    d = json.loads(run(capsys, "doctor", "--json")[1])
    assert d["ok"] is True and len(d["checks"]) == 6


def test_doctor_fails_when_the_mod_cannot_find_the_data_folder(srv, capsys, tmp_path, monkeypatch):
    # The mod runs from Claude Code's plugin cache: without CC_DATA_DIR or data_dir in config.json it cannot
    # find owner.token, even though the CLI (which falls back to <repo>/data) may.
    _plugins(tmp_path, monkeypatch, ["waypoint@waypoint-local"])
    monkeypatch.delenv("CC_DATA_DIR")
    monkeypatch.delenv("CC_TOKEN_FILE")
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"python": sys.executable}), encoding="utf-8")
    monkeypatch.setenv("WAYPOINT_CONFIG", str(cfg))
    code, o, _ = run(capsys, "doctor")
    rows = {l.split()[1]: l for l in o.strip().splitlines()[:-1]}
    assert code == 1 and rows["mod-token"].startswith("FAIL")
    assert "data_dir" in rows["mod-token"] and "install.ps1" in rows["mod-token"]
    cfg.write_text(json.dumps({"data_dir": str(tmp_path / "nowhere")}), encoding="utf-8")  # set, but no token there
    rows = {l.split()[1]: l for l in run(capsys, "doctor")[1].strip().splitlines()[:-1]}
    assert rows["mod-token"].startswith("FAIL") and str(tmp_path / "nowhere" / "owner.token") in rows["mod-token"]
    cfg.write_text(json.dumps({"data_dir": str(tmp_path / "data")}), encoding="utf-8")
    rows = {l.split()[1]: l for l in run(capsys, "doctor")[1].strip().splitlines()[:-1]}
    assert rows["mod-token"].startswith("ok")


def test_doctor_reports_failures(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("CC_DATA_DIR", str(tmp_path / "nodata"))
    _plugins(tmp_path, monkeypatch, ["other@x"])
    code, o, _ = run(capsys, "doctor")
    rows = {l.split()[1]: l for l in o.strip().splitlines()[:-1]}
    assert code == 1
    assert rows["python"].startswith("ok")
    assert rows["engine"].startswith("FAIL") and "waypoint engine start" in rows["engine"]
    assert rows["api"].startswith("skip")
    assert rows["tokens"].startswith("FAIL") and "owner.token" in rows["tokens"]
    assert rows["mod"].startswith("FAIL") and "claude plugin install" in rows["mod"]
    assert rows["mod-token"].startswith("skip")  # nothing to check while the mod is not installed


def test_doctor_api_mismatch_and_no_claude(srv, capsys, tmp_path, monkeypatch):
    STATE["health"] = dict(HEALTH, api=2, api_version="2.0")
    monkeypatch.setattr(w.shutil, "which", lambda name: None)
    code, o, _ = run(capsys, "doctor")
    rows = {l.split()[1]: l for l in o.strip().splitlines()[:-1]}
    assert code == 1 and rows["api"].startswith("FAIL") and "2.0" in rows["api"]
    assert rows["mod"].startswith("skip")


# ---- engine start / stop / status ----
@pytest.fixture
def ps_calls(monkeypatch):
    calls = []

    class R:
        returncode = 0

    monkeypatch.setattr(w.shutil, "which", lambda name: "C:/ps/" + name + ".exe" if name == "powershell" else None)
    monkeypatch.setattr(w.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or R())
    return calls


def test_engine_start_and_stop_pass_the_port(ps_calls, capsys, monkeypatch):
    monkeypatch.delenv("WAYPOINT_URL")
    assert run(capsys, "engine", "start")[0] == 0
    cmd = ps_calls[-1]
    assert cmd[0] == "C:/ps/powershell.exe" and "-File" in cmd
    assert Path(cmd[cmd.index("-File") + 1]) == w.SCRIPTS / "Start-Engine.ps1"
    assert cmd[cmd.index("-Port") + 1] == "8751" and cmd[cmd.index("-Python") + 1] == sys.executable
    monkeypatch.setenv("WAYPOINT_PORT", "8762")
    run(capsys, "engine", "stop")
    cmd = ps_calls[-1]
    assert Path(cmd[cmd.index("-File") + 1]) == w.SCRIPTS / "Stop-Engine.ps1" and cmd[cmd.index("-Port") + 1] == "8762"
    run(capsys, "engine", "stop", "--port", "8770")
    assert ps_calls[-1][ps_calls[-1].index("-Port") + 1] == "8770"


def test_engine_script_failure_code_passes_through(monkeypatch, capsys):
    class R:
        returncode = 1
    monkeypatch.setattr(w.shutil, "which", lambda name: "ps.exe")
    monkeypatch.setattr(w.subprocess, "run", lambda cmd, **kw: R())
    assert run(capsys, "engine", "start", "--port", "8799")[0] == 1


def test_engine_without_powershell(monkeypatch, capsys):
    monkeypatch.setattr(w.shutil, "which", lambda name: None)
    code, _, err = run(capsys, "engine", "start")
    assert code == 1 and "PowerShell" in err and "cc_engine serve" in err


def test_engine_status(srv, capsys):
    code, o, _ = run(capsys, "engine", "status")
    assert code == 0 and "engine: running at http://127.0.0.1:%d" % srv.server_port in o and "API 1.0" in o
    code, o, _ = run(capsys, "engine", "status", "--port", str(srv.server_port))
    assert code == 0 and "running" in o
    assert any(l[1] == "/health" for l in LOG)


def test_engine_status_down(capsys):
    code, o, _ = run(capsys, "engine", "status", "--port", "1")
    assert code == 5 and "not running at http://127.0.0.1:1" in o and "waypoint engine start" in o


# ---- help and errors ----
def _walk(parser, path=()):
    subs = getattr(parser, "_wp_sub", None)
    if subs is None:
        return
    helps = {a.dest: a.help for a in subs._choices_actions}
    for name, child in subs.choices.items():
        yield path + (name,), helps.get(name), child
        yield from _walk(child, path + (name,))


def test_every_command_has_a_one_line_description_and_examples():
    found = list(_walk(w.build_parser()))
    assert len(found) > 25
    for path, line, child in found:
        assert line and "\n" not in line and len(line) < 100, path
        assert "Examples:\n  waypoint" in (child.epilog or ""), path


def test_no_args_and_help_print_short_usage(capsys):
    code, o, _ = run(capsys)
    assert code == 0 and "Usage: waypoint <command>" in o and "Examples:" in o
    for name in ("board", "todo", "version", "sync", "engine", "doctor", "help"):
        assert "\n  %s " % name in o
    assert run(capsys, "help")[1] == o
    assert run(capsys, "--help")[1] == o


def test_help_for_a_command(capsys):
    code, o, _ = run(capsys, "help", "engine", "start")
    assert code == 0 and "--port" in o and "waypoint engine start" in o and "Examples:" in o
    code, o, _ = run(capsys, "help", "todo")
    assert code == 0 and "list" in o and "reorder" in o
    code, _, err = run(capsys, "help", "todo", "nope")
    assert code == 2 and "no command 'todo nope'" in err and "waypoint help todo" in err


@pytest.mark.parametrize("argv,needle", [(["bogus"], "unknown command 'bogus'"),
                                         (["done", "p"], "required"),
                                         (["todo"], "choose one of list, add, tick, reorder"),
                                         (["engine", "start", "--port", "x"], "invalid int")])
def test_usage_errors_are_one_line_with_a_next_step(capsys, argv, needle):
    code, o, err = run(capsys, *argv)
    assert code == 2 and not o
    assert len(err.strip().splitlines()) == 1 and needle in err and "waypoint help" in err


def test_cli_source_parses_as_python_3_10():
    src = (w.SCRIPTS / "waypoint_cli.py").read_text(encoding="utf-8")
    ast.parse(src, feature_version=(3, 10))


# ---- the cmd shim ----
SHIM = w.SCRIPTS / "waypoint.cmd"
windows = pytest.mark.skipif(os.name != "nt", reason="cmd shim is Windows only")


def _shim_env(tmp_path, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("WAYPOINT_", "CC_"))}
    env["WAYPOINT_URL"] = "http://127.0.0.1:1"
    env["WAYPOINT_CONFIG"] = str(tmp_path / "no-config.json")
    env.update(extra)
    return env


def _cmd(target, args, tmp_path, env, timeout=60):
    return subprocess.run(["cmd", "/d", "/c", str(target)] + list(args), cwd=str(tmp_path), env=env,
                          capture_output=True, text=True, timeout=timeout)


@windows
def test_cmd_shim_from_another_folder_and_through_a_forwarder(tmp_path):
    env = _shim_env(tmp_path, WAYPOINT_PYTHON=sys.executable)
    r = _cmd(SHIM, ["--version"], tmp_path, env)
    assert r.returncode == 0 and r.stdout.strip() == "waypoint 1.1.0 (engine not running)", r.stderr
    bin_dir = tmp_path / "Waypoint bin"
    bin_dir.mkdir()
    fwd = bin_dir / "waypoint.cmd"
    fwd.write_text('@echo off\r\n"%s" %%*\r\n' % SHIM, encoding="ascii")
    r = _cmd(fwd, ["--version"], tmp_path, env)
    assert r.returncode == 0 and "waypoint 1.1.0" in r.stdout, r.stderr
    r = _cmd(fwd, ["bogus"], tmp_path, env)
    assert r.returncode == 2 and "unknown command" in r.stderr  # the exit code survives both shims
    assert _cmd(fwd, ["board"], tmp_path, env).returncode == 5


@windows
@pytest.mark.skipif(shutil.which("powershell") is None, reason="PowerShell not available")
def test_cmd_shim_uses_the_python_key_in_the_settings_file(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"python": sys.executable}), encoding="utf-8")
    r = _cmd(SHIM, ["--version"], tmp_path, _shim_env(tmp_path, WAYPOINT_CONFIG=str(cfg)), timeout=120)
    assert r.returncode == 0 and "waypoint 1.1.0" in r.stdout, r.stderr


@windows
@pytest.mark.skipif(shutil.which("powershell") is None, reason="PowerShell not available")
@pytest.mark.parametrize("bom", [False, True])
def test_cmd_shim_reads_a_non_ascii_python_path_from_the_settings_file(tmp_path, bom):
    venv = tmp_path / "José venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True, timeout=180)
    exe = venv / "Scripts" / "python.exe"
    cfg = tmp_path / "config.json"
    raw = json.dumps({"python": str(exe)}, ensure_ascii=False).encode("utf-8")
    cfg.write_bytes((b"\xef\xbb\xbf" if bom else b"") + raw)
    r = _cmd(SHIM, ["--version"], tmp_path, _shim_env(tmp_path, WAYPOINT_CONFIG=str(cfg)), timeout=120)
    assert r.returncode == 0 and "waypoint 1.1.0" in r.stdout, (r.stdout, r.stderr)


# ---- todo list: open only by default, grouped like the app and the mod; --all tags every status ----
MIXED_TODOS = [
    {"id": "c2", "section": "computer", "text": "queued second", "status": "open", "position": 2},
    {"id": "h3", "section": "human", "text": "dropped one", "status": "dropped", "position": 0},
    {"id": "h2", "section": "human", "text": "second\nfor you", "status": "open", "position": 2},
    {"id": "c1", "section": "computer", "text": "handed off", "status": "handed_off", "position": 1},
    {"id": "h1", "section": "human", "text": "first for you", "status": "open", "position": 1},
    {"id": "h4", "section": "human", "text": "done one", "status": "done", "done": True, "position": 3},
    {"id": "old", "section": "human", "text": "old engine, no status", "done": True},
]


def test_todo_list_default_shows_open_and_handed_off_grouped_in_position_order():
    rows = w.todo_rows(MIXED_TODOS)
    assert [t["id"] for t in rows] == ["h1", "h2", "c1", "c2"]
    assert w.todo_text(rows).splitlines() == [
        "For you (2)", "  1. h1  first for you", "  2. h2  second for you", "",
        "Queued for agents (2)", "  1. c1  [handed off] handed off", "  2. c2  queued second"]


def test_todo_list_all_tags_every_status_and_keeps_the_grouping():
    rows = w.todo_rows(MIXED_TODOS, show_all=True)
    assert [t["id"] for t in rows] == ["h3", "h1", "h2", "h4", "old", "c1", "c2"]
    text = w.todo_text(rows, show_all=True)
    assert "  1. h3  [dropped] dropped one" in text and "  4. h4  [done] done one" in text
    assert "  5. old  [done] old engine, no status" in text and "  2. c2  [open] queued second" in text
    assert w.todo_rows(MIXED_TODOS, section="computer") == [MIXED_TODOS[3], MIXED_TODOS[0]]


def test_todo_list_empty_says_how_to_see_the_rest():
    assert "--all" in w.todo_text(w.todo_rows([MIXED_TODOS[1]]))


def test_todo_list_cli_flags(srv, capsys):
    code, o, _ = run(capsys, "todo", "list")
    assert code == 0 and o.splitlines()[0] == "For you (1)" and "Queued for agents (1)" in o
    code, o, _ = run(capsys, "todo", "list", "--all")
    assert code == 0 and "  1. td1  [open] h" in o
    code, o, _ = run(capsys, "todo", "list", "--json")
    assert [t["id"] for t in json.loads(o)["todos"]] == ["td1", "td2"]
