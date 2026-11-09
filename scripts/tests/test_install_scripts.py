"""Fast checks of install.ps1, uninstall.ps1 and waypoint_config.ps1 (Windows only; no engine, no mod).

Each test builds a small product tree (install.ps1, uninstall.ps1, the shipped bin/waypoint.cmd,
scripts/waypoint_config.ps1 and a stand-in scripts/waypoint.cmd that records its arguments) in a folder whose name is
not ASCII, then runs install with -NoStart and -NoMod against a temp config file, LOCALAPPDATA and Path store.
test_core_install.py covers the full install."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SOURCE = (REPO / "core" / "install.ps1").is_file()  # the source repo keeps the installer in core/
INSTALL = REPO / ("core" if SOURCE else "") / "install.ps1"
UNINSTALL = REPO / ("core" if SOURCE else "") / "uninstall.ps1"
BIN_CMD = REPO / ("core" if SOURCE else "") / "bin" / "waypoint.cmd"  # the shipped command: bin/ on the user Path
CONFIG_PS1 = REPO / "scripts" / "waypoint_config.ps1"
SYSROOT = os.environ.get("SystemRoot", r"C:\Windows")
PS = os.path.join(SYSROOT, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
BOM = b"\xef\xbb\xbf"

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the installer is Windows PowerShell")

FAKE_TARGET = (
    "@echo off\r\n"
    "rem stand-in for scripts\\waypoint.cmd: records its arguments as JSON, then exits 7\r\n"
    "\"%WAYPOINT_PYTHON%\" -c \"import sys, json; open(sys.argv[1], 'w', encoding='utf-8')"
    ".write(json.dumps(sys.argv[2:]))\" \"%ARGS_OUT%\" %*\r\n"
    "exit /b 7\r\n")
FAKE_REGISTER = "Write-Output 'autostart: no task for this port'\r\nexit 0\r\n"


def test_conftest_isolates_the_environment():
    assert not Path(os.environ["WAYPOINT_CONFIG"]).exists()
    for name in ("CC_DATA_DIR", "WAYPOINT_DATA_DIR", "CC_TOKEN_FILE", "WAYPOINT_URL", "WAYPOINT_PORT"):
        assert name not in os.environ, name


class Box:
    pass


@pytest.fixture()
def box(tmp_path):
    b = Box()
    base = tmp_path / "José"
    b.clone = base / "clone"
    (b.clone / "scripts").mkdir(parents=True)
    shutil.copy2(INSTALL, b.clone / "install.ps1")
    shutil.copy2(UNINSTALL, b.clone / "uninstall.ps1")
    shutil.copy2(CONFIG_PS1, b.clone / "scripts" / "waypoint_config.ps1")
    (b.clone / "bin").mkdir()
    shutil.copy2(BIN_CMD, b.clone / "bin" / "waypoint.cmd")
    (b.clone / "scripts" / "waypoint.cmd").write_bytes(FAKE_TARGET.encode("ascii"))
    (b.clone / "scripts" / "Register-Engine.ps1").write_bytes(FAKE_REGISTER.encode("ascii"))
    b.home = base / "home"
    b.local = base / "Local Données"
    b.home.mkdir()
    b.local.mkdir()
    b.config = b.home / ".waypoint" / "config.json"
    b.path_store = base / "user_path.txt"
    b.path_store.write_bytes("C:\\Existing\\Tools;C:\\Café\\Tools".encode("utf-8"))
    b.args_out = base / "args.json"
    b.bin = b.clone / "bin"
    b.shim = b.bin / "waypoint.cmd"
    b.old_shim = b.local / "Waypoint" / "bin" / "waypoint.cmd"  # what installs before bin/ wrote
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("WAYPOINT_", "CC_"))}
    env.update({"USERPROFILE": str(b.home), "LOCALAPPDATA": str(b.local), "WAYPOINT_CONFIG": str(b.config),
                "WAYPOINT_TEST_PATH_STORE": str(b.path_store), "WAYPOINT_PYTHON": sys.executable,
                "ARGS_OUT": str(b.args_out)})
    b.env = env

    def ps(script, *args, env_extra=None):
        e = dict(b.env, **(env_extra or {}))
        return subprocess.run([PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *args],
                              cwd=str(b.clone), env=e, capture_output=True, timeout=180)
    b.ps = ps

    def install(*extra, env_extra=None):
        r = ps(b.clone / "install.ps1", "-NoStart", "-NoMod", *extra, env_extra=env_extra)
        out = r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")
        assert r.returncode == 0, out
        return out
    b.install = install
    return b


def _cfg(path):
    raw = path.read_bytes()
    assert not raw.startswith(BOM), raw[:10]  # UTF-8 without a BOM
    return json.loads(raw.decode("utf-8"))


# ---------------------------------------------------------------- finding 1: data_dir
def test_install_writes_python_and_data_dir_when_missing(box):
    box.install()
    cfg = _cfg(box.config)
    assert Path(cfg["python"]).is_file()
    assert cfg["data_dir"] == str(box.clone / "data")


def test_install_adds_only_the_missing_key_and_never_overwrites(box):
    box.config.parent.mkdir(parents=True)
    box.config.write_text(json.dumps({"python": "C:\\Py\\python.exe", "owner_name": "Sam"}), encoding="utf-8")
    box.install()
    cfg = _cfg(box.config)
    assert cfg == {"python": "C:\\Py\\python.exe", "owner_name": "Sam", "data_dir": str(box.clone / "data")}
    box.config.write_text(json.dumps({"data_dir": "D:\\elsewhere"}), encoding="utf-8")
    box.install()
    cfg = _cfg(box.config)
    assert cfg["data_dir"] == "D:\\elsewhere" and Path(cfg["python"]).is_file()
    before = box.config.read_bytes()
    out = box.install()
    assert box.config.read_bytes() == before and "kept" in out  # both keys set: the file is not rewritten


def test_install_dry_run_writes_no_config(box):
    out = box.install("-DryRun")
    assert not box.config.exists() and "data_dir" in out


# ---------------------------------------------------------------- finding 2: UTF-8 everywhere
@pytest.mark.parametrize("bom", [False, True])
def test_install_keeps_non_ascii_config_values_and_path_entries(box, bom):
    box.config.parent.mkdir(parents=True)
    text = json.dumps({"python": "C:\\Users\\José\\py\\python.exe", "home_workspace": "C:\\Users\\José\\ws"},
                      ensure_ascii=False)
    box.config.write_bytes((BOM if bom else b"") + text.encode("utf-8"))
    out = box.install()
    cfg = _cfg(box.config)
    assert cfg["python"] == "C:\\Users\\José\\py\\python.exe", out
    assert cfg["home_workspace"] == "C:\\Users\\José\\ws"
    assert cfg["data_dir"] == str(box.clone / "data")
    entries = box.path_store.read_bytes().decode("utf-8").split(";")
    assert entries == ["C:\\Existing\\Tools", "C:\\Café\\Tools", str(box.bin)]
    again = box.install()
    assert "already has" in again, again
    assert box.path_store.read_bytes().decode("utf-8").split(";") == entries


@pytest.fixture(scope="session")
def nonascii_python(tmp_path_factory):
    """A venv whose python.exe sits under a folder named José (no pip, made in a few seconds)."""
    venv = tmp_path_factory.mktemp("venv") / "José venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True, timeout=180)
    exe = venv / "Scripts" / "python.exe"
    assert exe.is_file()
    return exe


def test_install_probe_reads_a_non_ascii_python_path(box, nonascii_python):
    out = box.install(env_extra={"WAYPOINT_PYTHON": str(nonascii_python)})
    assert _cfg(box.config)["python"] == str(nonascii_python), out


def _setting(env, key):
    """Get-WaypointSetting's answer for key, passed back through a UTF-8 file (console output is not UTF-8)."""
    out = Path(env["WAYPOINT_CONFIG"]).with_name("answer.txt")
    script = ("$ErrorActionPreference = 'Stop'; . '%s'; [System.IO.File]::WriteAllText('%s', "
              "(Get-WaypointSetting '%s' @() 'none'), (New-Object System.Text.UTF8Encoding($false)))"
              % (CONFIG_PS1, out, key))
    r = subprocess.run([PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script], env=env,
                       capture_output=True, timeout=120)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    return out.read_bytes().decode("utf-8")


@pytest.mark.parametrize("bom", [False, True])
def test_waypoint_config_ps1_reads_utf8_with_or_without_bom(tmp_path, bom):
    folder = tmp_path / "José"
    folder.mkdir()
    cfg = folder / "config.json"
    cfg.write_bytes((BOM if bom else b"") + json.dumps({"data_dir": "C:\\Users\\José\\wp"},
                                                       ensure_ascii=False).encode("utf-8"))
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("WAYPOINT_", "CC_"))}
    env["WAYPOINT_CONFIG"] = str(cfg)
    assert _setting(env, "data_dir") == "C:\\Users\\José\\wp"


def _run_line(line, env):
    """Run one command line the way a terminal does (cmd /s with the whole line in one pair of quotes)."""
    return subprocess.run('cmd /d /s /c "%s"' % line, env=env, capture_output=True, timeout=60)


def test_shipped_command_is_ascii_and_forwards_without_call():
    raw = BIN_CMD.read_bytes()
    raw.decode("ascii")  # no paths and no other non-ASCII text: cmd reads it the same in every code page
    text = raw.decode("ascii")
    assert '"%~dp0..\\scripts\\waypoint.cmd" %*' in text
    assert "call " not in text.lower()


def test_install_writes_no_command_file_and_puts_the_clone_bin_on_the_path(box):
    out = box.install()
    assert not (box.local / "Waypoint").exists(), out  # nothing generated: the shipped bin/waypoint.cmd is used
    assert box.shim.read_bytes() == BIN_CMD.read_bytes()
    entries = box.path_store.read_bytes().decode("utf-8").split(";")
    assert entries.count(str(box.bin)) == 1


def test_command_reaches_a_non_ascii_clone_through_the_path(box):
    box.install()
    env = dict(box.env, PATH=str(box.bin) + os.pathsep + box.env.get("PATH", ""))
    r = _run_line("waypoint plain", env)
    assert r.returncode == 7, (r.stdout, r.stderr)
    assert json.loads(box.args_out.read_text(encoding="utf-8")) == ["plain"]


# ---------------------------------------------------------------- finding 3: the command keeps % and ^
def test_command_passes_percent_and_caret_unchanged_and_keeps_the_exit_code(box):
    args = ["done", "demo", "A1", "--evidence", "100% tests pass, 5^2 ok"]
    r = _run_line('"%s" %s' % (box.shim, subprocess.list2cmdline(args)), box.env)
    assert r.returncode == 7, (r.stdout, r.stderr)  # the target's exit code comes back through bin/waypoint.cmd
    assert json.loads(box.args_out.read_text(encoding="utf-8")) == args
    box.install()
    env = dict(box.env, PATH=str(box.bin) + os.pathsep + box.env.get("PATH", ""))
    box.args_out.unlink()
    r = _run_line("waypoint " + subprocess.list2cmdline(args), env)
    assert r.returncode == 7 and json.loads(box.args_out.read_text(encoding="utf-8")) == args, (r.stdout, r.stderr)


def test_real_scripts_command_runs_the_cli_from_a_non_ascii_clone(tmp_path):
    clone = tmp_path / "Jos\u00e9 clone"
    (clone / "scripts").mkdir(parents=True)
    (clone / "bin").mkdir()
    shutil.copy2(BIN_CMD, clone / "bin" / "waypoint.cmd")
    for name in ("waypoint.cmd", "waypoint_cli.py", "waypoint_config.ps1"):
        shutil.copy2(REPO / "scripts" / name, clone / "scripts" / name)
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("WAYPOINT_", "CC_"))}
    env.update({"WAYPOINT_PYTHON": sys.executable, "WAYPOINT_URL": "http://127.0.0.1:1",
                "WAYPOINT_CONFIG": str(tmp_path / "no-config.json"),
                "PATH": str(clone / "bin") + os.pathsep + os.environ.get("PATH", "")})
    r = _run_line("waypoint --version", env)
    out = r.stdout.decode("utf-8", "replace")
    assert r.returncode == 0 and out.strip() == "waypoint 1.1.0 (engine not running)", (out, r.stderr)
    r = _run_line("waypoint bogus", env)
    assert r.returncode == 2  # the CLI's exit code comes back through both files


# ---------------------------------------------------------------- finding 4: Windows PowerShell, not $PSHOME
@pytest.mark.parametrize("script", [INSTALL, UNINSTALL])
def test_child_scripts_run_in_system32_windows_powershell(script):
    text = script.read_text(encoding="utf-8")
    assert "$PSHOME" not in text
    assert 'Join-Path $env:SystemRoot "System32\\WindowsPowerShell\\v1.0\\powershell.exe"' in text


def test_uninstall_keeps_non_ascii_path_entries_and_the_shipped_command(box):
    box.install()
    r = box.ps(box.clone / "uninstall.ps1", "-KeepEngine", "-NoMod")
    out = r.stdout.decode("utf-8", "replace")
    assert r.returncode == 0, out + r.stderr.decode("utf-8", "replace")
    assert box.shim.is_file()  # part of the clone, never deleted
    assert box.path_store.read_bytes().decode("utf-8").split(";") == ["C:\\Existing\\Tools", "C:\\Café\\Tools"]
    assert box.config.is_file()


MARKER = "rem Waypoint Core command (made by install.ps1; uninstall.ps1 removes it).\r\n"


@pytest.mark.parametrize("script", ["install.ps1", "uninstall.ps1"])
def test_an_old_localappdata_shim_and_its_path_entry_are_removed(box, script):
    box.old_shim.parent.mkdir(parents=True)
    box.old_shim.write_bytes(("@echo off\r\n" + MARKER + 'call "C:\\old\\scripts\\waypoint.cmd" %*\r\n').encode("ascii"))
    box.path_store.write_bytes(("C:\\Existing\\Tools;" + str(box.old_shim.parent)).encode("utf-8"))
    args = ("-NoStart", "-NoMod") if script == "install.ps1" else ("-KeepEngine", "-NoMod")
    r = box.ps(box.clone / script, *args)
    out = r.stdout.decode("utf-8", "replace")
    assert r.returncode == 0, out + r.stderr.decode("utf-8", "replace")
    assert not box.old_shim.exists() and not (box.local / "Waypoint").exists(), out
    entries = box.path_store.read_bytes().decode("utf-8").split(";")
    assert str(box.old_shim.parent) not in entries and "C:\\Existing\\Tools" in entries
    assert (str(box.bin) in entries) == (script == "install.ps1")


def test_a_foreign_file_in_the_old_place_is_left_alone(box):
    box.old_shim.parent.mkdir(parents=True)
    box.old_shim.write_bytes(b"@echo off\r\necho someone else's command\r\n")
    box.install()
    r = box.ps(box.clone / "uninstall.ps1", "-KeepEngine", "-NoMod")
    assert r.returncode == 0 and box.old_shim.is_file(), r.stdout.decode("utf-8", "replace")
