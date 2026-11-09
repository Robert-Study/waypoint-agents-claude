"""Clean-environment install test for Waypoint Core (Windows only).

The product tree (exported from the source repo, or this checkout when it is the product) is committed to a fresh
git repo and cloned. install.ps1 runs from the clone with a temp home, config file, LOCALAPPDATA and Path store, a
fake `claude` first on PATH and a free port. It must add the clone's bin folder to the Path store once
(install runs twice), write python and data_dir to config.json, start an engine on that port that reports API 1, make
`waypoint --version` work through bin\waypoint.cmd on the Path and call claude to add the marketplace and install the mod.
uninstall.ps1 must reverse all of it and stop that engine; Stop-Engine.ps1 must never stop another engine, and an
engine already answering on 8751 must still answer at the end."""
import json
import os
import shutil
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SOURCE = (REPO / "scripts" / "export_standalone.py").is_file()
LIVE_PORT = 8751

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the installer is Windows PowerShell")


def _health(port, timeout=3.0):
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert port != LIVE_PORT
    return port


def _git(args, cwd):
    out = subprocess.run(["git", "-c", "user.name=Waypoint Test", "-c", "user.email=test@example.invalid",
                          "-c", "core.autocrlf=false", *args], cwd=str(cwd), capture_output=True, text=True,
                         timeout=300)
    assert out.returncode == 0, (args, out.stdout[-2000:], out.stderr[-2000:])
    return out


def _product_tree(dest: Path):
    if SOURCE:
        sys.path.insert(0, str(REPO / "scripts"))
        try:
            import export_core
        finally:
            sys.path.pop(0)
        export_core.export(REPO, dest)
    else:  # this checkout is the product: copy it without git state or local data
        shutil.copytree(REPO, dest, ignore=shutil.ignore_patterns(
            ".git", "data", "__pycache__", ".pytest_cache", "node_modules", ".venv", "*.pyc"))


class Box:
    pass


@pytest.fixture(scope="module")
def box(tmp_path_factory):
    b = Box()
    t = tmp_path_factory.mktemp("core-install")
    b.live_before = _health(LIVE_PORT) is not None
    src = t / "product"
    _product_tree(src)
    _git(["init", "-q"], src)
    _git(["add", "-A"], src)
    _git(["commit", "-q", "-m", "product"], src)
    b.clone = t / "clone"
    _git(["clone", "-q", str(src), str(b.clone)], t)

    b.home = t / "home"
    b.local = t / "localappdata"
    b.roaming = t / "appdata"
    b.fakebin = t / "fakebin"
    for d in (b.home, b.local, b.roaming, b.fakebin):
        d.mkdir()
    b.config = b.home / ".waypoint" / "config.json"
    b.path_store = t / "user_path.txt"
    b.path_store.write_text("C:\\Existing\\Tools", encoding="utf-8")
    b.claude_log = t / "claude_calls.txt"
    (b.fakebin / "claude.cmd").write_text(f'@echo off\r\necho %*>>"{b.claude_log}"\r\nexit /b 0\r\n', encoding="ascii")
    b.port = _free_port()
    b.data = b.clone / "data"
    b.shim = b.clone / "bin" / "waypoint.cmd"  # shipped in the clone; install only puts bin\ on the Path
    b.bin = b.shim.parent

    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("WAYPOINT_", "CC_", "AI_", "CODEX", "GEMINI", "GOOGLE", "GROQ", "OPENAI",
                                        "ANTHROPIC", "CLAUDE"))}
    git_dir = os.path.dirname(shutil.which("git") or "")
    env["PATH"] = os.pathsep.join(p for p in (str(b.fakebin), sysroot + r"\System32", sysroot,
                                              sysroot + r"\System32\WindowsPowerShell\v1.0", git_dir) if p)
    env.update({"USERPROFILE": str(b.home), "HOME": str(b.home), "LOCALAPPDATA": str(b.local),
                "APPDATA": str(b.roaming), "WAYPOINT_CONFIG": str(b.config),
                "WAYPOINT_TEST_PATH_STORE": str(b.path_store), "WAYPOINT_PYTHON": sys.executable,
                "CC_CLI_BRIDGE": "0", "PYTHONDONTWRITEBYTECODE": "1"})
    b.env = env
    b.ps = os.path.join(sysroot, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")

    def ps(script, *args, env_extra=None, timeout=300):
        e = dict(b.env, **(env_extra or {}))
        return subprocess.run([b.ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *args],
                              cwd=str(b.clone), env=e, capture_output=True, text=True, timeout=timeout)
    b.run = ps
    b.installs = [ps(b.clone / "install.ps1", "-Port", str(b.port)) for _ in range(2)]
    try:
        yield b
    finally:  # never leave the test engine running, whatever failed
        if _health(b.port) is not None:
            ps(b.clone / "scripts" / "Stop-Engine.ps1", "-Port", str(b.port))


def _ok(r):
    assert r.returncode == 0, (r.stdout[-3000:], r.stderr[-3000:])
    return r.stdout


def test_install_runs_clean_twice(box):
    first, second = (_ok(r) for r in box.installs)
    assert "install: done" in first and "install: done" in second
    assert "already has" in second  # the Path step saw its own entry


def test_command_is_the_shipped_file_and_nothing_is_generated(box):
    text = box.shim.read_bytes().decode("ascii")  # no paths inside: the same in every code page
    assert '"%~dp0..\\scripts\\waypoint.cmd" %*' in text
    assert "call " not in text.lower()  # call would mangle % and ^ in arguments
    assert not (box.local / "Waypoint").exists()


def test_path_store_has_bin_once(box):
    entries = [e for e in box.path_store.read_text(encoding="utf-8").strip().split(";") if e]
    assert entries.count(str(box.bin)) == 1, entries
    assert "C:\\Existing\\Tools" in entries


def test_config_has_python_and_data_dir(box):
    raw = box.config.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    cfg = json.loads(raw.decode("utf-8"))  # no BOM, valid JSON
    assert Path(cfg["python"]).is_file()
    assert Path(cfg["data_dir"]) == box.data  # the mod runs from the plugin cache and needs this


def test_engine_answers_api_1_on_its_port(box):
    h = _health(box.port)
    assert h is not None and h.get("ok") is True
    assert h.get("api") == 1, h
    pid = json.loads((box.data / "engine.pid").read_text(encoding="utf-8"))
    assert pid["port"] == box.port


def test_waypoint_version_through_the_path(box):
    env = dict(box.env, WAYPOINT_PORT=str(box.port))
    env["PATH"] = str(box.bin) + os.pathsep + env["PATH"]  # what the user Path change gives a new terminal
    env.pop("WAYPOINT_PYTHON")  # the command must find python through config.json
    out = subprocess.run('cmd /d /s /c "waypoint --version"', env=env, capture_output=True, text=True,
                         timeout=120)
    assert out.returncode == 0, (out.stdout, out.stderr)
    assert out.stdout.strip().startswith("waypoint "), out.stdout


def test_fake_claude_got_marketplace_add_and_install(box):
    if not (box.clone / "claude_mod").is_dir():  # the public engine tree has no mod: install must skip that step
        calls = box.claude_log.read_text(encoding="mbcs").splitlines() if box.claude_log.exists() else []
        assert not [c for c in calls if c.startswith("plugin ")], calls
        return
    calls = box.claude_log.read_text(encoding="mbcs").splitlines()
    assert any(c.startswith("plugin marketplace add") and "claude_mod" in c for c in calls), calls
    assert any(c.startswith("plugin install waypoint@waypoint-local --scope user") for c in calls), calls


def test_stop_engine_leaves_other_engines_alone(box):
    other = box.clone.parent / "other-data"
    r = box.run(box.clone / "scripts" / "Stop-Engine.ps1", "-Port", str(box.port), env_extra={"CC_DATA_DIR": str(other)})
    assert r.returncode == 1 and "left alone" in r.stdout, r.stdout
    assert _health(box.port) is not None
    if box.live_before:  # dry run on the live port with this clone's data dir: must refuse, never stop
        r = box.run(box.clone / "scripts" / "Stop-Engine.ps1", "-Port", str(LIVE_PORT), "-DryRun")
        assert r.returncode == 1 and "left alone" in r.stdout, r.stdout
        assert _health(LIVE_PORT) is not None


def test_uninstall_reverses_install_and_stops_its_engine(box):
    out = _ok(box.run(box.clone / "uninstall.ps1", "-Port", str(box.port)))
    assert "uninstall: done" in out
    assert box.shim.is_file()  # part of the clone; only its Path entry goes
    entries = [e for e in box.path_store.read_text(encoding="utf-8").strip().split(";") if e]
    assert str(box.bin) not in entries and "C:\\Existing\\Tools" in entries
    calls = box.claude_log.read_text(encoding="mbcs").splitlines() if box.claude_log.exists() else []
    if (box.clone / "claude_mod").is_dir():
        assert any(c.startswith("plugin uninstall waypoint@waypoint-local") for c in calls), calls
        assert any(c.startswith("plugin marketplace remove waypoint-local") for c in calls), calls
    else:
        assert not [c for c in calls if c.startswith("plugin ")], calls
    assert _health(box.port) is None
    assert not (box.data / "engine.pid").exists()
    assert box.data.is_dir() and box.config.is_file()  # data and config are kept
    again = _ok(box.run(box.clone / "uninstall.ps1", "-Port", str(box.port)))  # idempotent
    assert "uninstall: done" in again


def test_stop_engine_after_uninstall_is_a_no_op(box):
    r = box.run(box.clone / "scripts" / "Stop-Engine.ps1", "-Port", str(box.port))
    assert r.returncode == 0 and "not running" in r.stdout, r.stdout


def test_live_engine_still_answers(box):
    if not box.live_before:
        pytest.skip("no engine was answering on 8751 at the start")
    assert _health(LIVE_PORT) is not None
