"""usage.py native sources (T1): Codex from its own local state, Antigravity from `agy --print /usage`.

Fixtures only: no network, no real agy, no real ~/.codex. Unknown stays unknown.
"""
import json
import os
import socket
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cc_engine import usage

NOW = datetime(2026, 11, 4, 16, 0, 0, tzinfo=timezone.utc)
USAGE_SRC = Path(usage.__file__).read_text(encoding="utf-8")

AGY_TSV = ("Gemini Models\tWeekly Limit Remaining\t0%\t2026-11-08T19:21:34Z\n"
           "Gemini Models\tFive Hour Limit Remaining\tdisabled\t\n"
           "Claude and GPT models\tWeekly Limit Remaining\t3%\t2026-11-08T19:19:33Z\n"
           "Claude and GPT models\tFive Hour Limit Remaining\tdisabled\t\n"
           "Gemini 4 Argon\tGemini 4 Argon Weekly Limit\t1%\t2026-11-08T19:21:22Z\n")


@pytest.fixture
def exe(tmp_path):
    f = tmp_path / "agy.exe"
    f.write_text("", encoding="utf-8")
    return str(f)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("native usage sources must not touch the network")
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def by_pool(rows):
    return {r["pool"]: r for r in rows}


def rate_event(at, used5=30.0, used_week=12.0, primary_reset=None, secondary_reset=None, **over):
    primary_reset = primary_reset or int((at + timedelta(hours=1)).timestamp())
    secondary_reset = secondary_reset or int((at + timedelta(days=3)).timestamp())
    rl = {"limit_id": "codex",
          "primary": {"used_percent": used5, "window_minutes": 300, "resets_at": primary_reset},
          "secondary": {"used_percent": used_week, "window_minutes": 10080, "resets_at": secondary_reset}}
    rl.update(over)
    return {"timestamp": at.isoformat().replace("+00:00", "Z"), "type": "event_msg",
            "payload": {"type": "token_count", "info": None, "rate_limits": rl}}


def write_rollout(home, name, events, mtime=None, day="2026/11/04"):
    d = Path(home) / "sessions" / day
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(f, (mtime, mtime))
    return f


# ---------------------------------------------------------------- module no longer depends on RobWorkflow
def test_usage_native_module_has_no_robworkflow_source():
    assert "8744" not in USAGE_SRC
    assert "RobWorkflow" not in USAGE_SRC
    assert "telemetry" not in USAGE_SRC.lower()
    for gone in ("TELEMETRY_URL", "ANTIGRAVITY_RECEIPT", "codex_from_telemetry", "_http_get"):
        assert not hasattr(usage, gone), gone


# ---------------------------------------------------------------- Codex
def test_usage_native_codex_reads_newest_rollout(tmp_path):
    old = write_rollout(tmp_path, "rollout-old.jsonl", [rate_event(NOW - timedelta(hours=9), used5=99.0, used_week=99.0)],
                        mtime=(NOW - timedelta(hours=9)).timestamp())
    write_rollout(tmp_path, "rollout-new.jsonl",
                  [{"type": "other"}, rate_event(NOW - timedelta(minutes=30), used5=30.0, used_week=12.0)],
                  mtime=NOW.timestamp())
    assert old.exists()
    rows = by_pool(usage.codex_source(codex_home=tmp_path))
    assert set(rows) == set(usage.CODEX_POOLS)
    assert rows["codex_5h"]["remaining_percent"] == 70.0
    assert rows["codex_week"]["remaining_percent"] == 88.0
    assert rows["codex_5h"]["observed_at"] == (NOW - timedelta(minutes=30)).isoformat(timespec="seconds")
    assert rows["codex_5h"]["resets_at"] == (NOW + timedelta(minutes=30)).isoformat(timespec="seconds")
    assert "rollout" in rows["codex_5h"]["source"]


def test_usage_native_codex_uses_last_rate_limits_event_in_file(tmp_path):
    write_rollout(tmp_path, "rollout-a.jsonl", [rate_event(NOW - timedelta(hours=2), used5=90.0),
                                                 rate_event(NOW - timedelta(minutes=5), used5=10.0),
                                                 {"type": "event_msg", "payload": {"type": "agent_message"}}])
    assert by_pool(usage.codex_source(codex_home=tmp_path))["codex_5h"]["remaining_percent"] == 90.0


def test_usage_native_codex_skips_newest_file_without_limits(tmp_path):
    write_rollout(tmp_path, "rollout-real.jsonl", [rate_event(NOW - timedelta(hours=1), used5=40.0)],
                  mtime=(NOW - timedelta(hours=1)).timestamp())
    write_rollout(tmp_path, "rollout-empty.jsonl", [{"type": "session_meta"}], mtime=NOW.timestamp())
    assert by_pool(usage.codex_source(codex_home=tmp_path))["codex_5h"]["remaining_percent"] == 60.0


def test_usage_native_codex_no_rollouts_or_no_limits_is_unknown(tmp_path):
    for setup in (lambda: None, lambda: write_rollout(tmp_path, "r.jsonl", [{"type": "x"}])):
        setup()
        rows = usage.codex_source(codex_home=tmp_path)
        assert {r["pool"] for r in rows} == set(usage.CODEX_POOLS)
        assert all(r["remaining_percent"] is None and r["observed_at"] is None for r in rows)
    assert all(r["remaining_percent"] is None for r in usage.codex_source(codex_home=tmp_path / "missing"))


def test_usage_native_codex_wrong_window_or_bad_number_is_unknown(tmp_path):
    ev = rate_event(NOW)
    ev["payload"]["rate_limits"]["primary"]["window_minutes"] = 60
    ev["payload"]["rate_limits"]["secondary"]["used_percent"] = 140
    write_rollout(tmp_path, "r.jsonl", [ev])
    rows = by_pool(usage.codex_source(codex_home=tmp_path))
    assert rows["codex_5h"]["remaining_percent"] is None
    assert rows["codex_week"]["remaining_percent"] is None


def test_usage_native_codex_aging_through_read_usage(tmp_path):
    write_rollout(tmp_path, "r.jsonl", [rate_event(NOW)])
    cache = tmp_path / "usage_cache.json"
    ref = usage.UsageRefresher(cache, fetchers={"codex": lambda: usage.codex_source(codex_home=tmp_path)},
                               clock=lambda: NOW)
    ref.refresh("codex")
    r = by_pool(usage.read_usage(cache, now=NOW + timedelta(minutes=3)))["codex_5h"]
    assert r["status"] == "FRESH" and r["remaining_percent"] == 70.0
    r = by_pool(usage.read_usage(cache, now=NOW + timedelta(minutes=5)))["codex_5h"]
    assert r["status"] == "STALE" and r["remaining_percent"] == 70.0  # older than 2 x 2 min
    r = by_pool(usage.read_usage(cache, now=NOW + timedelta(hours=2)))["codex_5h"]
    assert r["status"] == "STALE"  # the 5 h window has reset since


# ---------------------------------------------------------------- Antigravity: sign-in detection
def write_token(path, **token):
    path.write_text(json.dumps({"token": token, "auth_method": "consumer", "id_token_email": "x@example.com"}),
                    encoding="utf-8")
    return path


def runner_returning(out, code=0, calls=None):
    def run(cmd, timeout):
        if calls is not None:
            calls.append((list(cmd), timeout))
        return code, out
    return run


def test_usage_native_agy_signed_in_detection(tmp_path):
    ok = write_token(tmp_path / "ok", access_token="a", refresh_token="r")
    assert usage.agy_signed_in(ok) is True
    assert usage.agy_signed_in(write_token(tmp_path / "norefresh", access_token="a")) is False
    assert usage.agy_signed_in(write_token(tmp_path / "empty", access_token="a", refresh_token="")) is False
    assert usage.agy_signed_in(tmp_path / "missing") is False
    bad = tmp_path / "bad"
    bad.write_text("{nope", encoding="utf-8")
    assert usage.agy_signed_in(bad) is False
    notdict = tmp_path / "list"
    notdict.write_text("[]", encoding="utf-8")
    assert usage.agy_signed_in(notdict) is False


def test_usage_native_agy_signed_out_never_runs_the_cli(tmp_path, exe):
    calls = []
    rows = usage.agy_source(runner=runner_returning(AGY_TSV, calls=calls), token_path=tmp_path / "none",
                            exe=exe, now=NOW)
    assert calls == []
    assert [r["pool"] for r in rows] == list(usage.AGY_POOLS)
    assert all(r["remaining_percent"] is None and "signed out" in r["source"] for r in rows)


def test_usage_native_agy_missing_exe_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(usage.shutil, "which", lambda name: None)
    tok = write_token(tmp_path / "t", refresh_token="r")
    calls = []
    rows = usage.agy_source(runner=runner_returning(AGY_TSV, calls=calls), token_path=tok,
                            exe=str(tmp_path / "no-agy.exe"), now=NOW)
    assert calls == [] and all(r["remaining_percent"] is None and "not installed" in r["source"] for r in rows)


# ---------------------------------------------------------------- Antigravity: running the CLI
def test_usage_native_agy_parses_report_and_uses_safe_command(tmp_path, exe):
    tok = write_token(tmp_path / "t", refresh_token="r")
    calls = []
    rows = by_pool(usage.agy_source(runner=runner_returning(AGY_TSV, calls=calls), token_path=tok,
                                    exe=exe, now=NOW))
    (cmd, timeout), = calls
    assert cmd[0] == exe and cmd[1:3] == ["--print", "/usage"]
    assert "--print-timeout" in cmd and "25s" in cmd and timeout <= 35
    assert rows["antigravity_week"]["remaining_percent"] == 0.0
    assert rows["antigravity_week"]["resets_at"] == "2026-11-08T19:21:34+00:00"
    assert rows["antigravity_week"]["observed_at"] == NOW.isoformat(timespec="seconds")
    assert rows["antigravity_claude_week"]["remaining_percent"] == 3.0
    assert rows["antigravity_argon_week"]["remaining_percent"] == 1.0
    for pid in ("antigravity_gemini_5h", "antigravity_claude_5h"):
        assert rows[pid]["remaining_percent"] is None and "disabled" in rows[pid]["source"]
    assert "agy" in rows["antigravity_week"]["source"] and "RobWorkflow" not in rows["antigravity_week"]["source"]


def test_usage_native_agy_failures_stay_unknown(tmp_path, exe):
    tok = write_token(tmp_path / "t", refresh_token="r")

    def boom(cmd, timeout):
        raise TimeoutError("25 s")
    cases = [runner_returning(AGY_TSV, code=1), runner_returning(""), runner_returning("garbage\nlines\n"), boom]
    for runner in cases:
        rows = usage.agy_source(runner=runner, token_path=tok, exe=exe, now=NOW)
        assert [r["pool"] for r in rows] == list(usage.AGY_POOLS)
        assert all(r["remaining_percent"] is None for r in rows)


def test_usage_native_agy_login_prompt_output_is_signed_out(tmp_path, exe):
    tok = write_token(tmp_path / "t", refresh_token="r")
    for text in ("Please sign in to continue: https://accounts.google.com/o/oauth2/auth?x=1",
                 "You are not logged in. Run agy login."):
        rows = usage.agy_source(runner=runner_returning(text), token_path=tok, exe=exe, now=NOW)
        assert all(r["remaining_percent"] is None and "signed out" in r["source"] for r in rows)


def test_usage_native_agy_default_runner_closes_stdin_and_kills_on_timeout(monkeypatch):
    seen = {}

    class Proc:
        pid = 4242
        returncode = 0

        def communicate(self, inp=None, timeout=None):
            seen["input"], seen["timeout"] = inp, timeout
            return AGY_TSV, None

    def fake_popen(argv, **kw):
        seen.update(kw, argv=argv)
        return Proc()
    monkeypatch.setattr(usage.subprocess, "Popen", fake_popen)
    code, out = usage._default_agy_runner(["agy.exe", "--print", "/usage"], 35)
    assert code == 0 and out == AGY_TSV
    assert seen["stdin"] is usage.subprocess.PIPE and seen["input"] == "" and seen["timeout"] == 35

    killed = []

    class Hung(Proc):
        def communicate(self, inp=None, timeout=None):
            if timeout:
                raise usage.subprocess.TimeoutExpired("agy", timeout)
            return "", None
    monkeypatch.setattr(usage.subprocess, "Popen", lambda argv, **kw: Hung())
    monkeypatch.setattr(usage.subprocess, "run", lambda argv, **kw: killed.append(argv))
    with pytest.raises(usage.subprocess.TimeoutExpired):
        usage._default_agy_runner(["agy.exe"], 1)
    assert killed and "/T" in killed[0] or not usage.os.name == "nt"


def test_usage_native_default_fetchers_use_native_sources():
    f = usage.default_fetchers()
    assert set(f) == {"claude", "codex", "antigravity"}
    assert f["codex"] is usage.codex_source and f["antigravity"] is usage.agy_source


def test_usage_native_refresher_stores_agy_unknown_when_signed_out(tmp_path, exe):
    cache = tmp_path / "usage_cache.json"
    ref = usage.UsageRefresher(cache, fetchers={"antigravity": lambda: usage.agy_source(
        runner=runner_returning(AGY_TSV), token_path=tmp_path / "none", exe=exe, now=NOW)}, clock=lambda: NOW)
    ref.refresh("antigravity")
    rows = by_pool(usage.read_usage(cache, now=NOW))
    assert all(rows[p]["status"] == "UNKNOWN" and rows[p]["remaining_percent"] is None for p in usage.AGY_POOLS)
    assert "signed out" in rows["antigravity_week"]["source"]
