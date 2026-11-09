"""One place for machine-specific paths.

Resolution order for every key: environment variable, then the optional JSON override file
(``~/.waypoint/config.json``, or the file named by ``WAYPOINT_CONFIG``), then the built-in default.
Defaults are the original install's values, so that install behaves exactly as before; every
other install sets the environment variables or the JSON file instead of editing code.

JSON keys are the lower-case names in ``KEYS`` (for example ``{"drive_root": "D:/Drive"}``).
Derived paths follow their base unless set themselves. The file is read as UTF-8 with or without a BOM
(Windows PowerShell 5.1 writes one), the same way the ``waypoint`` CLI and the Claude mod read it.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_DEFAULT_HOME = str(Path.home())


def _join(base: str, *parts: str) -> str:
    sep = "\\" if "\\" in base else "/"
    return sep.join([base.rstrip("\\/"), *parts])


def _override_file() -> Path:
    named = os.environ.get("WAYPOINT_CONFIG")
    return Path(named) if named else Path.home() / ".waypoint" / "config.json"


def _load_json() -> dict:
    try:
        data = json.loads(_override_file().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


_FILE = _load_json()


def _get(key: str, envs: tuple, default: str) -> str:
    for name in envs:
        value = os.environ.get(name)
        if value:
            return value
    value = _FILE.get(key)
    return str(value) if value else default


USER_HOME = _get("user_home", ("WAYPOINT_USER_HOME",), _DEFAULT_HOME)
EXCHANGE_DIR = _get("exchange_dir", ("WAYPOINT_EXCHANGE_DIR",), "notes")  # notes hub folder name under DRIVE_ROOT
DRIVE_ROOT = _get("drive_root", ("AI_DRIVE_ROOT",), _join(USER_HOME, "Notes"))
WORKSPACE_ROOT = _get("workspace_root", ("AI_WORKSPACE_ROOT", "AI_DISK_ROOT"), _join(USER_HOME, "workspace"))
REPO_ROOT = str(Path(__file__).resolve().parents[2])
PYTHON = _get("python", ("WAYPOINT_PYTHON",),
              sys.executable)


def _extra_repo() -> str:
    """The optional second repo offered as an agent folder (key ``extra_repo``, env ``WAYPOINT_EXTRA_REPO``).

    The old names (key ``fama_french_repo``, env ``WAYPOINT_FF_REPO``) are still read as a silent fallback,
    so an install that set them is unchanged. Order: new env, old env, new key, old key, default."""
    for name in ("WAYPOINT_EXTRA_REPO", "WAYPOINT_FF_REPO"):
        if os.environ.get(name):
            return os.environ[name]
    for key in ("extra_repo", "fama_french_repo"):
        if _FILE.get(key):
            return str(_FILE[key])
    return _join(WORKSPACE_ROOT, "second-repo")


EXTRA_REPO = _extra_repo()
TOOLS_DIR = _get("tools_dir", ("WAYPOINT_TOOLS_DIR",), _join(WORKSPACE_ROOT, "waypoint-workspace", "tools"))
WORKSPACE_DIR = _get("workspace_dir", ("WAYPOINT_WORKSPACE_DIR",), _join(WORKSPACE_ROOT, "waypoint-workspace"))
WORKTREES_ROOT = _get("worktrees_root", ("WAYPOINT_WORKTREES_ROOT",), _join(WORKSPACE_ROOT, "worktrees"))
ENV_FILE = _get("env_file", ("WAYPOINT_ENV_FILE",), _join(WORKSPACE_ROOT, ".env"))
INSTRUCTIONS = _get("instructions", ("WAYPOINT_INSTRUCTIONS",), _join(DRIVE_ROOT, EXCHANGE_DIR, "AGENTS.md"))
DSH_DIR = _get("dsh_dir", ("WAYPOINT_DSH_DIR",), _join(USER_HOME, ".dsh"))
AGY_EXE = _get("agy_exe", ("WAYPOINT_AGY_EXE",), _join(USER_HOME, "AppData", "Local", "agy", "bin", "agy.exe"))
NPM_ROOT = _get("npm_root", ("WAYPOINT_NPM_ROOT",), _join(USER_HOME, "AppData", "Roaming", "npm"))
CLAUDE_MEMORY_INDEX = _get("claude_memory_index", ("WAYPOINT_CLAUDE_MEMORY_INDEX",),
                           _join(USER_HOME, ".claude", "memory", "MEMORY.md"))

DATA_DIR = _get("data_dir", ("CC_DATA_DIR", "WAYPOINT_DATA_DIR"), str(Path(__file__).resolve().parents[2] / "data"))


def data_dir() -> Path:
    """The data folder, read at call time: ``CC_DATA_DIR`` (tests set it late), else ``DATA_DIR``
    (``WAYPOINT_DATA_DIR`` or the settings file's ``data_dir`` at import, else ``<repo>/data``)."""
    env = os.environ.get("CC_DATA_DIR")
    return Path(env) if env else Path(DATA_DIR)

LAUNCHER = _get("launcher", ("WAYPOINT_LAUNCHER",), _join(TOOLS_DIR, "start_dsh_app.py"))

LEARN_SEED = _get("learn_seed", ("WAYPOINT_LEARN_SEED",), str(Path(__file__).with_name("learn_seed.json")))
OWNER_NAME = _get("owner_name", ("WAYPOINT_OWNER_NAME",), "Owner")

# Add-on ids that standalone/addons.json knows; only these pass through from the settings file.
ADDON_IDS = ("gemini_key_rotator", "free_runners", "claude_codex_pages", "brain_learn",
             "windows_shortcut_autostart", "db_backup", "preferences_digest")


def home_workspace():
    """Folder where new chats should start (settings key ``home_workspace``, env ``WAYPOINT_HOME_WORKSPACE``).

    Read fresh each call; ``None`` when unset, so clients leave new chats where their host puts them."""
    value = os.environ.get("WAYPOINT_HOME_WORKSPACE") or _load_json().get("home_workspace")
    return str(value) if value else None


HOME_WORKSPACE = home_workspace() or ""  # the value at import; call home_workspace() for the live one


def cli_bridge_legacy_key() -> bool:
    """True only when the settings file says ``"cli_bridge_legacy_key": true`` (read fresh each call).

    It lets the CLI bridge accept its fixed transition key (see ``cli_bridge``); off unless set."""
    return _load_json().get("cli_bridge_legacy_key") is True


def addon_settings() -> dict:
    """``{addon_id: bool}`` from the ``addons`` object of the settings file (read fresh each call).

    Only known add-on ids and real booleans pass through; an add-on is ON unless this says False."""
    raw = _load_json().get("addons")
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if k in ADDON_IDS and isinstance(v, bool)}


KEYS = ("user_home", "drive_root", "workspace_root", "python", "extra_repo", "tools_dir",
        "workspace_dir", "worktrees_root", "env_file", "instructions", "dsh_dir", "agy_exe", "npm_root",
        "claude_memory_index", "learn_seed", "owner_name", "data_dir", "launcher", "exchange_dir",
        "home_workspace")
