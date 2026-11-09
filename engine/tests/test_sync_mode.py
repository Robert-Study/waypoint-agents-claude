"""S2: Cloud/Local sync engine (``cc_engine.sync``).

Every repo here is a real git repo made in ``tmp_path``: a bare ``remote`` (stands in for GitHub),
the ``local`` clone the engine looks after, and a ``cloud`` clone that plays the cloud session
pushing work. No network, no global git config (``GIT_CONFIG_GLOBAL`` points at an empty file).
"""
import http.client
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from cc_engine import api, needs, sync

FORBIDDEN = ("--force", "-f", "--hard", "clean", "checkout", "restore", "stash", "push", "rebase")


IDENT = (("GIT_AUTHOR_NAME", "Test"), ("GIT_AUTHOR_EMAIL", "t@example.invalid"),
         ("GIT_COMMITTER_NAME", "Test"), ("GIT_COMMITTER_EMAIL", "t@example.invalid"))


def _env(cfg):
    env = dict(os.environ, GIT_CONFIG_GLOBAL=str(cfg), GIT_CONFIG_NOSYSTEM="1")
    env.update(IDENT)
    return env


@pytest.fixture(scope="session")
def git_cfg(tmp_path_factory):
    cfg = tmp_path_factory.mktemp("gitcfg") / "gitconfig"
    cfg.write_text("", encoding="utf-8")
    return cfg


@pytest.fixture(autouse=True)
def git_env(git_cfg, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(git_cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for k, v in IDENT:
        monkeypatch.setenv(k, v)


def git(cwd, *args, env=None):
    out = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=60,
                         env=env)
    assert out.returncode == 0, (args, out.stdout, out.stderr)
    return out.stdout.strip()


def commit(repo, name, text, msg=None, env=None):
    (Path(repo) / name).write_text(text, encoding="utf-8")
    git(repo, "add", name, env=env)
    git(repo, "commit", "-q", "-m", msg or f"edit {name}", env=env)


@pytest.fixture(scope="session")
def template(tmp_path_factory, git_cfg):
    """Built once (git is slow to start on Windows); each test gets a copy."""
    env = _env(git_cfg)
    base = tmp_path_factory.mktemp("template")
    remote = base / "remote.git"
    git(base, "init", "-q", "--bare", "-b", "main", str(remote), env=env)
    seed = base / "seed"
    git(base, "init", "-q", "-b", "main", str(seed), env=env)
    commit(seed, "a.txt", "one\n", "seed", env=env)
    git(seed, "remote", "add", "origin", str(remote), env=env)
    git(seed, "push", "-q", "origin", "main", env=env)
    (base / "disk").mkdir()
    git(base, "clone", "-q", str(remote), str(base / "disk" / "proj"), env=env)
    git(base, "clone", "-q", str(remote), str(base / "cloud"), env=env)
    return base


@pytest.fixture
def trio(tmp_path, template):
    """root/<local> tracking a bare remote, plus a separate cloud clone that pushes."""
    for name in ("remote.git", "disk", "cloud"):
        shutil.copytree(template / name, tmp_path / name)
    remote, root = tmp_path / "remote.git", tmp_path / "disk"
    local, cloud = root / "proj", tmp_path / "cloud"
    for repo in (local, cloud):
        git(repo, "remote", "set-url", "origin", str(remote))
    return {"remote": remote, "root": root, "local": local, "cloud": cloud}


class Recorder:
    """Wraps the real git runner and keeps every argument list it was given."""

    def __init__(self):
        self.calls = []

    def __call__(self, args, cwd, timeout=None):
        self.calls.append(list(args))
        return sync.default_git(args, cwd, timeout=timeout)

    def assert_safe(self):
        for args in self.calls:
            for bad in FORBIDDEN:
                assert bad not in args, f"sync ran a forbidden git command: {args}"


def head(repo):
    return git(repo, "rev-parse", "HEAD")


# ---------------------------------------------------------------- mode + repo list
def test_sync_mode_defaults_to_local_and_switches(store):
    assert sync.get_mode(store) == "Local"
    assert sync.set_mode(store, "Cloud") == "Cloud"
    assert sync.get_mode(store) == "Cloud"
    assert sync.set_mode(store, "local") == "Local"  # case-insensitive
    with pytest.raises(ValueError):
        sync.set_mode(store, "Hybrid")
    assert store.revision() == 0  # telemetry-like settings never bump the plan revision


def test_sync_mode_repos_are_git_repos_directly_under_the_root(tmp_path, store):
    root = tmp_path / "disk"
    for name in ("a", "b", "nested/c"):
        (root / name).mkdir(parents=True)
    (root / "a" / ".git").mkdir()
    (root / "nested" / "c" / ".git").mkdir()
    (root / "wt").mkdir()
    (root / "wt" / ".git").write_text("gitdir: elsewhere", encoding="utf-8")  # a worktree checkout counts
    (root / "file.txt").write_text("x", encoding="utf-8")
    found = [Path(p).name for p in sync.discover(root)]
    assert found == ["a", "wt"]
    assert [Path(p).name for p in sync.repo_paths(store, root=root)] == ["a", "wt"]
    sync.set_repos(store, [str(root / "b")])  # configurable list overrides discovery
    assert sync.repo_paths(store, root=root) == [str(root / "b")]
    sync.set_repos(store, None)
    assert [Path(p).name for p in sync.repo_paths(store, root=root)] == ["a", "wt"]


# ---------------------------------------------------------------- on open
def test_sync_mode_open_fast_forwards_a_clean_repo_that_is_behind(trio, store):
    commit(trio["cloud"], "b.txt", "cloud\n")
    git(trio["cloud"], "push", "-q", "origin", "main")
    rec = Recorder()
    out = sync.on_open(store, git=rec, repos=[str(trio["local"])], force=True)
    assert head(trio["local"]) == head(trio["cloud"])
    assert (trio["local"] / "b.txt").read_text() == "cloud\n"
    assert [u["name"] for u in out["last"]["updated"]] == ["proj"]
    assert out["last"]["updated"][0]["commits"] == 1
    repo = out["repos"][0]
    assert repo["state"] == "up_to_date" and repo["behind"] == 0
    assert out["needs_merge"] is False
    assert sync.status(store, git=rec, repos=[str(trio["local"])])["last"]["updated"][0]["name"] == "proj"
    rec.assert_safe()


def test_sync_mode_open_is_throttled_unless_forced(trio, store):
    rec = Recorder()
    sync.on_open(store, git=rec, repos=[str(trio["local"])], force=True)
    n = len(rec.calls)
    sync.on_open(store, git=rec, repos=[str(trio["local"])])
    assert len(rec.calls) <= n + 12  # a status read, but no second fetch round
    assert not any(c[:1] == ["fetch"] for c in rec.calls[n:])


def test_sync_mode_open_reports_and_skips_a_dirty_repo(trio, store):
    commit(trio["cloud"], "a.txt", "cloud edit\n")
    git(trio["cloud"], "push", "-q", "origin", "main")
    (trio["local"] / "a.txt").write_text("my unsaved edit\n", encoding="utf-8")
    before = head(trio["local"])
    rec = Recorder()
    out = sync.on_open(store, git=rec, repos=[str(trio["local"])], force=True)
    assert head(trio["local"]) == before
    assert (trio["local"] / "a.txt").read_text() == "my unsaved edit\n"  # never discards local changes
    repo = out["repos"][0]
    assert repo["state"] == "dirty" and repo["dirty"] is True and repo["behind"] == 1
    assert out["last"]["updated"] == []
    assert [s["name"] for s in out["last"]["skipped"]] == ["proj"]
    rec.assert_safe()


def test_sync_mode_open_leaves_diverged_history_for_the_update_button(trio, store):
    commit(trio["cloud"], "b.txt", "cloud\n")
    git(trio["cloud"], "push", "-q", "origin", "main")
    commit(trio["local"], "c.txt", "local\n")
    before = head(trio["local"])
    out = sync.on_open(store, git=Recorder(), repos=[str(trio["local"])], force=True)
    assert head(trio["local"]) == before  # open never merges
    repo = out["repos"][0]
    assert repo["state"] == "diverged" and repo["ahead"] == 1 and repo["behind"] == 1
    assert repo["cloud_ahead"] is True
    assert out["needs_merge"] is True
    assert out["last"]["needs_merge"] == ["proj"]


# ---------------------------------------------------------------- pull
def test_sync_mode_pull_merges_diverged_history_that_merges_cleanly(trio, store):
    commit(trio["cloud"], "b.txt", "cloud\n")
    git(trio["cloud"], "push", "-q", "origin", "main")
    commit(trio["local"], "c.txt", "local\n")
    rec = Recorder()
    out = sync.pull(store, git=rec, repos=[str(trio["local"])])
    assert (trio["local"] / "b.txt").read_text() == "cloud\n"
    assert (trio["local"] / "c.txt").read_text() == "local\n"
    res = out["results"][0]
    assert res["name"] == "proj" and res["result"] == "merged"
    assert out["needs_merge"] is False
    assert out["repos"][0]["behind"] == 0
    assert needs.list_questions(store, open_only=True) == []
    rec.assert_safe()


def test_sync_mode_pull_conflict_aborts_the_merge_and_asks_robert_once(trio, store):
    commit(trio["cloud"], "a.txt", "cloud says this\n")
    git(trio["cloud"], "push", "-q", "origin", "main")
    commit(trio["local"], "a.txt", "local says that\n")
    before = head(trio["local"])
    rec = Recorder()
    out = sync.pull(store, git=rec, repos=[str(trio["local"])])
    res = out["results"][0]
    assert res["result"] == "conflict" and res["files"] == ["a.txt"]
    assert head(trio["local"]) == before
    assert (trio["local"] / "a.txt").read_text() == "local says that\n"
    assert not (trio["local"] / ".git" / "MERGE_HEAD").exists()  # merge aborted
    assert git(trio["local"], "status", "--porcelain") == ""
    qs = needs.list_questions(store, open_only=True)
    assert len(qs) == 1 and "proj" in qs[0]["prompt"] and "a.txt" in qs[0]["prompt"]
    assert res["question"] == qs[0]["id"]
    repo = out["repos"][0]
    assert repo["state"] == "diverged" and repo["conflict"]["files"] == ["a.txt"]
    sync.pull(store, git=rec, repos=[str(trio["local"])])
    assert len(needs.list_questions(store, open_only=True)) == 1  # not asked twice
    rec.assert_safe()


def test_sync_mode_pull_fast_forwards_and_skips_dirty(trio, store):
    commit(trio["cloud"], "b.txt", "cloud\n")
    git(trio["cloud"], "push", "-q", "origin", "main")
    out = sync.pull(store, git=Recorder(), repos=[str(trio["local"])])
    assert out["results"][0]["result"] == "fast_forwarded"
    commit(trio["cloud"], "b.txt", "cloud 2\n")
    git(trio["cloud"], "push", "-q", "origin", "main")
    (trio["local"] / "b.txt").write_text("mine\n", encoding="utf-8")
    out = sync.pull(store, git=Recorder(), repos=[str(trio["local"])])
    assert out["results"][0]["result"] == "skipped" and "edit" in out["results"][0]["reason"]
    assert (trio["local"] / "b.txt").read_text() == "mine\n"


# ---------------------------------------------------------------- injected runner
def test_sync_mode_injected_runner_failures_never_raise(store, tmp_path):
    def broken(args, cwd, timeout=None):
        return 128, "", "fatal: not a git repository"
    out = sync.status(store, git=broken, repos=[str(tmp_path)])
    assert out["repos"][0]["state"] == "error"
    assert out["needs_merge"] is False

    def no_upstream(args, cwd, timeout=None):
        if args[:1] == ["status"]:
            return 0, "# branch.oid 0123abcd\n# branch.head main", ""
        return 0, "", ""
    out = sync.on_open(store, git=no_upstream, repos=[str(tmp_path)], force=True)
    assert out["repos"][0]["state"] == "no_upstream"
    assert out["last"]["updated"] == []


# ---------------------------------------------------------------- HTTP routes
OWNER_ORIGIN = "http://127.0.0.1:3080"


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
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=60)
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
    return resp, (json.loads(payload) if payload else None)


def test_sync_mode_routes_status_mode_open_and_owner_only_pull(server, store, trio):
    sync.set_repos(store, [str(trio["local"])])
    commit(trio["cloud"], "b.txt", "cloud\n")
    git(trio["cloud"], "push", "-q", "origin", "main")
    resp, body = call(server, "GET", "/api/sync/status")
    assert resp.status == 200 and body["mode"] == "Local"
    assert body["repos"][0]["name"] == "proj"

    tok = server.app.token
    resp, _ = call(server, "POST", "/api/sync/mode", {"mode": "Cloud"})
    assert resp.status == 403  # no credential
    resp, _ = call(server, "POST", "/api/sync/mode", {"mode": "Cloud"}, token=tok)
    assert resp.status == 403  # the router may not switch Owner's mode
    resp, body = call(server, "POST", "/api/sync/mode", {"mode": "Cloud"}, origin=OWNER_ORIGIN)
    assert resp.status == 200 and body["mode"] == "Cloud"
    resp, body = call(server, "POST", "/api/sync/mode", {"mode": "Nope"}, origin=OWNER_ORIGIN)
    assert resp.status == 400

    resp, body = call(server, "POST", "/api/sync/open", {}, origin=OWNER_ORIGIN)
    assert resp.status == 200
    assert [u["name"] for u in body["last"]["updated"]] == ["proj"]
    assert head(trio["local"]) == head(trio["cloud"])

    resp, _ = call(server, "POST", "/api/sync/pull", {}, token=tok)
    assert resp.status == 403
    resp, body = call(server, "POST", "/api/sync/pull", {}, origin=OWNER_ORIGIN)
    assert resp.status == 200 and body["mode"] == "Cloud"
    resp, body = call(server, "GET", "/api/sync/status?fetch=1")
    assert body["mode"] == "Cloud" and body["repos"][0]["state"] == "up_to_date"
