"""scripts/waypoint_config.ps1: env var, then the settings file, then the original install's value."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HELPER = REPO / "scripts" / "waypoint_config.ps1"
PS = shutil.which("powershell") or shutil.which("pwsh")
pytestmark = pytest.mark.skipif(PS is None, reason="PowerShell not available")
B = chr(92)


def run(expr, tmp_path, env_extra=None, cfg=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("WAYPOINT_", "AI_", "CC_"))}
    env["USERPROFILE"] = str(tmp_path)
    env["WAYPOINT_CONFIG"] = str(tmp_path / "no.json")
    if cfg is not None:
        f = tmp_path / "config.json"
        f.write_text(json.dumps(cfg), encoding="utf-8")
        env["WAYPOINT_CONFIG"] = str(f)
    env.update(env_extra or {})
    out = subprocess.run([PS, "-NoProfile", "-Command", f". '{HELPER}'; {expr}"], env=env, capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_launcher_default_follows_the_user_home(tmp_path):
    # Paths default under USERPROFILE (like Path.home()); on the original machine that is the old value.
    assert run("Get-WaypointLauncher", tmp_path) == (str(tmp_path) + B + "workspace" + B
                                                    + r"waypoint-workspace\tools\start_dsh_app.py")


def test_data_dir_helper(tmp_path):
    assert run("Get-WaypointDataDir 'R:\\repo'", tmp_path) == "R:" + B + "repo" + B + "data"
    assert run("Get-WaypointDataDir 'R:\\repo'", tmp_path, {"WAYPOINT_DATA_DIR": "W:/d"}) == "W:/d"
    assert run("Get-WaypointDataDir 'R:\\repo'", tmp_path, {"WAYPOINT_DATA_DIR": "W:/d", "CC_DATA_DIR": "C:/d"}) == "C:/d"
    assert run("Get-WaypointDataDir 'R:\\repo'", tmp_path, cfg={"data_dir": "F:/d"}) == "F:/d"


CRLF = chr(13) + chr(10)


def _fake(tmp_path, folder, name, body):
    """A <name>.cmd in its own folder; ``body`` is a path to print (like print(sys.executable)) or a cmd line."""
    d = tmp_path / folder
    d.mkdir(exist_ok=True)
    line = body if body.startswith("exit") else "echo " + body
    (d / (name + ".cmd")).write_text("@echo off" + CRLF + line + CRLF, encoding="ascii")
    return str(d)


def _file(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="ascii")
    return str(path)


def test_python_uses_the_original_venv_only_when_it_exists(tmp_path):
    ws = tmp_path / "ws"
    venv_py = ws / "second-repo" / ".venv" / "Scripts" / "python.exe"
    _file(venv_py)
    path = _fake(tmp_path, "bin", "py", _file(tmp_path / "from-py.exe"))
    cfg = {"workspace_root": str(ws)}
    assert run("Get-WaypointPython", tmp_path, {"PATH": path}, cfg=cfg) == str(venv_py)
    venv_py.unlink()  # gone: fall through to py -3
    assert run("Get-WaypointPython", tmp_path, {"PATH": path}, cfg=cfg) == str(tmp_path / "from-py.exe")


def test_python_order_after_the_settings_file_is_py_then_python(tmp_path):
    cfg = {"workspace_root": str(tmp_path / "nothing-here")}
    a, b = _file(tmp_path / "a.exe"), _file(tmp_path / "b.exe")
    both = _fake(tmp_path, "both", "py", a) + ";" + _fake(tmp_path, "both", "python", b)
    assert run("Get-WaypointPython", tmp_path, {"PATH": both}, cfg=cfg) == a
    assert run("Get-WaypointPython", tmp_path, {"PATH": _fake(tmp_path, "onlypython", "python", b)}, cfg=cfg) == b
    # a python that prints no path (like the Microsoft Store alias) is skipped; nothing found gives ''
    assert run("Get-WaypointPython", tmp_path, {"PATH": _fake(tmp_path, "broken", "python", "exit /b 9009")},
               cfg=cfg) == ""
    assert run("Get-WaypointPython", tmp_path, {"PATH": str(tmp_path / "empty")}, cfg=cfg) == ""
    # the settings file still wins over everything on PATH
    assert run("Get-WaypointPython", tmp_path, {"PATH": both}, cfg=dict(cfg, python="X:/py.exe")) == "X:/py.exe"


def test_settings_file_beats_default_and_env_beats_file(tmp_path):
    assert run("Get-WaypointPython", tmp_path, cfg={"python": "X:/py.exe"}) == "X:/py.exe"
    assert run("Get-WaypointPython", tmp_path, {"WAYPOINT_PYTHON": "E:/py.exe"}, cfg={"python": "X:/py.exe"}) == "E:/py.exe"
    assert run("Get-WaypointLauncher", tmp_path, cfg={"launcher": "X:/l.py"}) == "X:/l.py"
    assert run("Get-WaypointSetting 'data_dir' @('CC_DATA_DIR') 'dflt'", tmp_path, cfg={"data_dir": "D:/d"}) == "D:/d"


def test_derived_launcher_follows_tools_dir(tmp_path):
    assert run("Get-WaypointLauncher", tmp_path, cfg={"tools_dir": "T:/tools"}) == "T:/tools" + B + "start_dsh_app.py"


def test_bad_settings_file_falls_back(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text("{nope", encoding="utf-8")
    assert run("Get-WaypointSetting 'python' @() 'dflt'", tmp_path, {"WAYPOINT_CONFIG": str(f)}) == "dflt"


def test_scripts_dry_run_use_settings_file(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("WAYPOINT_", "AI_"))}
    f = tmp_path / "config.json"
    f.write_text(json.dumps({"python": "Z:/my/python.exe"}), encoding="utf-8")
    env["WAYPOINT_CONFIG"] = str(f)
    out = subprocess.run([PS, "-NoProfile", "-File", str(REPO / "scripts" / "Register-DbBackup.ps1"), "-DryRun"],
                         env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "Z:/my/python.exe" in out.stdout
