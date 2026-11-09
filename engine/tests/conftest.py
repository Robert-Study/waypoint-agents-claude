"""Shared fixtures for the Control Centre engine tests (stdlib + pytest only)."""
import copy
import os
import sys
import tempfile
import uuid
from pathlib import Path

# Isolation first, before anything imports cc_engine (config.py reads the settings file at import):
# the user's real ~/.waypoint/config.json and data-folder variables must never change a test.
os.environ["WAYPOINT_CONFIG"] = str(Path(tempfile.gettempdir()) / f"waypoint-tests-{uuid.uuid4().hex}"
                                    / "no-config.json")
for _name in ("CC_DATA_DIR", "WAYPOINT_DATA_DIR", "CC_TOKEN_FILE", "WAYPOINT_HOME_WORKSPACE",
              "CC_SNAPSHOT_DIR", "CC_DB"):
    os.environ.pop(_name, None)

import pytest  # noqa: E402

ENGINE = Path(__file__).resolve().parents[1]
REPO = ENGINE.parent
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

FIXTURE_PLAN = {
    "id": "fx",
    "title": "Fixture project",
    "brief": "A small fixture plan used by the engine tests.",
    "cwd": None,
    "nodes": [
        {"id": "A", "title": "Root", "goal": "Lay the root.", "check": {"type": "command", "run": "exit 0"}, "deps": [], "owner": "engine-core", "lane": "core"},
        {"id": "B", "title": "Left", "goal": "Build the left side.", "check": {"type": "files_exist", "paths": ["left.txt"]}, "deps": ["A"], "owner": "engine-core", "lane": "core"},
        {"id": "C", "title": "Right", "goal": "Build the right side.", "check": {"type": "text_present", "path": "right.txt", "text": "ok"}, "deps": ["A"], "owner": "engine-core", "lane": "core"},
        {"id": "D", "title": "Join", "goal": "Join both sides.", "check": {"type": "command", "run": "exit 0"}, "deps": ["B", "C", "A"], "owner": "engine-core", "lane": "core"},
        {"id": "E", "title": "Owner look", "goal": "Owner approves the result.", "check": {"type": "human", "prompt": "Owner has looked"}, "deps": ["D"], "owner": "lead", "lane": "ui"},
    ],
}


@pytest.fixture
def fixture_plan():
    return copy.deepcopy(FIXTURE_PLAN)


@pytest.fixture
def store(tmp_path):
    from cc_engine.store import Store

    s = Store(tmp_path / "cc.sqlite")
    yield s
    s.close()


@pytest.fixture
def repo_root():
    return REPO


@pytest.fixture(autouse=True)
def _isolated_live_db(tmp_path, monkeypatch):
    """Code paths that fall back to the live engine db read a temp one in tests, so a switch Owner
    turns off in Waypoint can never change a test's outcome (and tests never touch data/cc.sqlite)."""
    monkeypatch.setenv("CC_DB", str(tmp_path / "live-cc.sqlite"))
