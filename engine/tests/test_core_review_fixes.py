"""Waypoint Core 1.0 review fixes: CLI-bridge legacy key off by default (5), owner token before the port
is bound (6), one owner-token path rule (7), a settings file with a BOM (8). Fakes and temp folders only;
nothing here touches the live engine on 8751."""
import importlib
import json
import threading
import time

import pytest

from cc_engine import api, cli_bridge, config

LEGACY = "waypoint-cli-local"


def _settings(tmp_path, monkeypatch, data, bom=False):
    f = tmp_path / "config.json"
    f.write_text(("\ufeff" if bom else "") + json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("WAYPOINT_CONFIG", str(f))
    return f


# ------------------------------------------------------------------ 5: legacy key
def _bridge():
    return cli_bridge.Bridge(enabled=lambda r: True, which=lambda n: None, cwd=None, token="t" * 43)


def test_legacy_key_is_off_by_default(monkeypatch):
    monkeypatch.delenv("CC_CLI_BRIDGE_LEGACY_KEY", raising=False)
    assert cli_bridge.legacy_keys_allowed() is False
    assert not _bridge().key_ok(LEGACY)
    assert _bridge().key_ok("t" * 43)


def test_legacy_key_on_from_settings_file_read_fresh(tmp_path, monkeypatch):
    monkeypatch.delenv("CC_CLI_BRIDGE_LEGACY_KEY", raising=False)
    f = _settings(tmp_path, monkeypatch, {"cli_bridge_legacy_key": True})
    b = _bridge()
    assert config.cli_bridge_legacy_key() is True and b.key_ok(LEGACY)
    f.write_text(json.dumps({"cli_bridge_legacy_key": False}), encoding="utf-8")
    assert not b.key_ok(LEGACY)  # read fresh: no restart needed
    f.write_text(json.dumps({"cli_bridge_legacy_key": "yes please"}), encoding="utf-8")
    assert not b.key_ok(LEGACY)  # only a real true turns it on


def test_legacy_key_env_wins_both_ways(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, {"cli_bridge_legacy_key": True})
    monkeypatch.setenv("CC_CLI_BRIDGE_LEGACY_KEY", "0")
    assert not _bridge().key_ok(LEGACY)
    _settings(tmp_path, monkeypatch, {})
    monkeypatch.setenv("CC_CLI_BRIDGE_LEGACY_KEY", "1")
    assert _bridge().key_ok(LEGACY)


def test_start_logs_one_warning_only_while_legacy_key_is_on(monkeypatch, capsys):
    monkeypatch.delenv("CC_CLI_BRIDGE", raising=False)
    for value, warned in (("1", True), ("0", False)):
        monkeypatch.setenv("CC_CLI_BRIDGE_LEGACY_KEY", value)
        srv = cli_bridge.start_background(port=0, enabled=lambda r: True, which=lambda n: None, cwd=None,
                                          token="t" * 43)
        try:
            err = capsys.readouterr().err
            lines = [ln for ln in err.splitlines() if "warning" in ln.lower()]
            assert len(lines) == (1 if warned else 0), err
            if warned:
                assert LEGACY in lines[0] and "cli_bridge_legacy_key" in lines[0]
        finally:
            srv.shutdown()
            srv.server_close()


def test_docstring_no_longer_claims_the_key_is_on_by_default():
    doc = cli_bridge.__doc__
    assert "while ``CC_CLI_BRIDGE_LEGACY_KEY`` is not 0" not in doc
    assert "cli_bridge_legacy_key" in doc and "off by default" in doc


# ------------------------------------------------------------------ 6: owner token before bind
def test_owner_token_exists_before_the_port_is_bound(store, tmp_path, monkeypatch):
    seen = {}
    real_make = api.make_server

    def spy(*a, **kw):
        seen["owner_at_bind"] = (tmp_path / "owner.token").exists()
        return real_make(*a, **kw)

    acl = []

    def slow_restrict(p):
        acl.append(str(p))
        time.sleep(3)

    monkeypatch.setattr(api, "make_server", spy)
    monkeypatch.setattr(api, "_restrict_to_user", slow_restrict)
    t0 = time.monotonic()
    srv = api.start(store, "127.0.0.1", 0, start_refresher=False, warm_up_delay=30, usage_reader=lambda: [],
                    poll_interval=0, token="r" * 43, token_path=tmp_path / "engine.token")
    try:
        assert time.monotonic() - t0 < 1.5  # icacls never delays the bind
        assert seen["owner_at_bind"] is True
        tok = (tmp_path / "owner.token").read_text(encoding="utf-8").strip()
        assert len(tok) >= 32 and srv.app.owner_token() == tok
        assert not (tmp_path / "owner.token.tmp").exists()
    finally:
        srv.app.close()
        srv.server_close()


def test_existing_owner_token_is_kept_at_start(store, tmp_path):
    (tmp_path / "owner.token").write_text("o" * 43, encoding="utf-8")
    srv = api.start(store, "127.0.0.1", 0, start_refresher=False, warm_up_delay=30, usage_reader=lambda: [],
                    poll_interval=0, token="r" * 43, token_path=tmp_path / "engine.token")
    try:
        assert srv.app.owner_token() == "o" * 43
    finally:
        srv.app.close()
        srv.server_close()


# ------------------------------------------------------------------ 7: one owner-token path rule
def test_owner_token_path_follows_cc_token_file_then_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("CC_TOKEN_FILE", raising=False)
    assert api.default_owner_token_path() == tmp_path / "data" / "owner.token"
    assert api.default_token_path() == tmp_path / "data" / "engine.token"  # read at call time too
    monkeypatch.setenv("CC_TOKEN_FILE", str(tmp_path / "keys" / "router.tok"))
    assert api.default_owner_token_path() == tmp_path / "keys" / "owner.token"


def test_start_writes_the_owner_token_where_the_rule_says(store, tmp_path, monkeypatch):
    monkeypatch.setenv("CC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CC_TOKEN_FILE", str(tmp_path / "keys" / "engine.token"))
    srv = api.start(store, "127.0.0.1", 0, start_refresher=False, warm_up_delay=30, usage_reader=lambda: [],
                    poll_interval=0)
    try:
        assert srv.app.owner_token_path == api.default_owner_token_path() == tmp_path / "keys" / "owner.token"
        assert (tmp_path / "keys" / "owner.token").exists()
    finally:
        srv.app.close()
        srv.server_close()


# ------------------------------------------------------------------ 8: settings file with a BOM
def test_settings_file_with_a_bom_is_honoured(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, {"home_workspace": "D:/Work", "extra_repo": "D:/extra"}, bom=True)
    monkeypatch.delenv("WAYPOINT_HOME_WORKSPACE", raising=False)
    monkeypatch.delenv("WAYPOINT_EXTRA_REPO", raising=False)
    monkeypatch.delenv("WAYPOINT_FF_REPO", raising=False)
    assert config.home_workspace() == "D:/Work"
    try:
        fresh = importlib.reload(config)
        assert fresh.EXTRA_REPO == "D:/extra"
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_every_settings_key_is_a_module_attribute():
    for key in config.KEYS:
        assert hasattr(config, key.upper()), key
