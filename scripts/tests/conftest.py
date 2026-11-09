"""Test isolation for scripts/tests: runs before any test module imports cc_engine or the CLI.

The user's real ~/.waypoint/config.json and Waypoint environment variables must never reach a test. WAYPOINT_CONFIG
points at a file that does not exist, and the data-folder, token and engine-address variables are removed. A test
that needs one sets it itself (monkeypatch.setenv, or the env it passes to a subprocess)."""
import atexit
import os
import shutil
import tempfile
from pathlib import Path

ISOLATED_VARS = ("CC_DATA_DIR", "WAYPOINT_DATA_DIR", "CC_TOKEN_FILE", "WAYPOINT_URL", "WAYPOINT_PORT")

_NO_CONFIG_DIR = Path(tempfile.mkdtemp(prefix="waypoint-tests-"))
NO_CONFIG = _NO_CONFIG_DIR / "no-config.json"  # never created
atexit.register(shutil.rmtree, str(_NO_CONFIG_DIR), True)

os.environ["WAYPOINT_CONFIG"] = str(NO_CONFIG)
for _name in ISOLATED_VARS:
    os.environ.pop(_name, None)
