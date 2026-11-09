"""GET /api/toggles carries ``home_workspace`` from the settings file or env; None when unset."""
import json

from cc_engine import config, toggles


def test_home_workspace_is_none_when_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("WAYPOINT_HOME_WORKSPACE", raising=False)
    monkeypatch.setenv("WAYPOINT_CONFIG", str(tmp_path / "missing.json"))
    assert config.home_workspace() is None
    assert toggles.payload(db=str(tmp_path / "t.sqlite"))["home_workspace"] is None


def test_home_workspace_reads_settings_file_then_env(tmp_path, monkeypatch):
    f = tmp_path / "config.json"
    f.write_text(json.dumps({"home_workspace": "D:/Work"}), encoding="utf-8")
    monkeypatch.setenv("WAYPOINT_CONFIG", str(f))
    monkeypatch.delenv("WAYPOINT_HOME_WORKSPACE", raising=False)
    assert toggles.payload(db=str(tmp_path / "t.sqlite"))["home_workspace"] == "D:/Work"
    monkeypatch.setenv("WAYPOINT_HOME_WORKSPACE", "E:/Other")
    assert config.home_workspace() == "E:/Other"
    assert "home_workspace" in config.KEYS
