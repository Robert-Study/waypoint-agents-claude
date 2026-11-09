"""The test run never sees the user's real settings file or data-folder variables (conftest.py)."""
import os

from cc_engine import config

DROPPED = ("CC_DATA_DIR", "WAYPOINT_DATA_DIR", "CC_TOKEN_FILE", "WAYPOINT_HOME_WORKSPACE", "CC_SNAPSHOT_DIR")


def test_settings_file_points_at_a_missing_temp_file():
    named = os.environ.get("WAYPOINT_CONFIG")
    assert named, "conftest must name a settings file"
    assert not os.path.exists(named)
    assert config._override_file() == config.Path(named)
    assert config._FILE == {}  # read at import, before any test ran
    assert config.home_workspace() is None


def test_data_folder_variables_are_dropped():
    for name in DROPPED:
        assert name not in os.environ, name
    # CC_DB comes back per test from the autouse fixture, always a temp path
    assert "live-cc.sqlite" in os.environ["CC_DB"]
