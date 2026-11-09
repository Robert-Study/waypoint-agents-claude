"""usage.py: cached per-pool usage. Fake runners/fetchers only; never runs claude or the network."""
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from cc_engine import usage

NOW = datetime(2026, 11, 4, 11, 0, 0, tzinfo=timezone.utc)


def _named_zones_load():
    """Named zones need a tz database; Windows has none unless the optional tzdata package is installed."""
    try:
        import zoneinfo
        zoneinfo.ZoneInfo("Europe/London")
        return True
    except Exception:
        return False


needs_tz_database = pytest.mark.skipif(not _named_zones_load(),
                                       reason="no tz database for named zones (pip install tzdata)")

CLAUDE_TEXT = ("Current session: 23% used · resets Nov 4, 2:19pm (Europe/London)\n"
               "Current week (all models): 61% used · resets Nov 8, 7am (Europe/London)\n"
               "Current week (Fable): 10% used · resets Nov 8, 7am (Europe/London)\n")


def envelope(text=CLAUDE_TEXT, **over):
    env = {"type": "result", "is_error": False, "num_turns": 0, "duration_api_ms": 0,
           "total_cost_usd": 0, "result": text, "modelUsage": {},
           "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
                     "cache_read_input_tokens": 0}}
    env.update(over)
    return env


def claude_runner(env, code=0, calls=None):
    def run(cmd):
        if calls is not None:
            calls.append(list(cmd))
        return code, json.dumps(env) if isinstance(env, dict) else env
    return run


def codex_rollout(home, at=None, used5=58.0, used_week=20.0):
    """Write a native Codex rollout fixture carrying one rate_limits event; returns ``home``."""
    at = at or (NOW - timedelta(minutes=1))
    ev = {"timestamp": at.isoformat(), "type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
        "primary": {"used_percent": used5, "window_minutes": 300, "resets_at": int((at + timedelta(hours=2)).timestamp())},
        "secondary": {"used_percent": used_week, "window_minutes": 10080, "resets_at": int((at + timedelta(days=3)).timestamp())}}}}
    d = home / "sessions" / "2026" / "11" / "04"
    d.mkdir(parents=True, exist_ok=True)
    (d / "rollout-fx.jsonl").write_text(json.dumps(ev) + "\n", encoding="utf-8")
    return home


def by_pool(rows):
    return {r["pool"]: r for r in rows}


# ---------------------------------------------------------------- Claude
def test_claude_cli_parses_session_and_week():
    calls = []
    rows = by_pool(usage.claude_source(runner=claude_runner(envelope(), calls=calls), now=NOW))
    assert calls == [usage.CLAUDE_CMD]
    assert "--no-session-persistence" in calls[0] and "/usage" in calls[0]
    assert rows["claude_5h"]["remaining_percent"] == 77.0
    assert rows["claude_week"]["remaining_percent"] == 39.0
    assert rows["claude_5h"]["observed_at"] == NOW.isoformat(timespec="seconds")


@needs_tz_database
def test_claude_cli_parses_reset_times_in_named_zones():
    rows = by_pool(usage.claude_source(runner=claude_runner(envelope()), now=NOW))
    assert rows["claude_5h"]["resets_at"] == "2026-11-04T14:19:00+00:00"  # GMT in November
    assert rows["claude_week"]["resets_at"] == "2026-11-08T07:00:00+00:00"
    assert rows["claude_5h"]["observed_at"] == NOW.isoformat(timespec="seconds")


@pytest.fixture
def no_tz_database(monkeypatch):
    """Named zones fail to load, as on Windows without the tzdata package."""
    import zoneinfo

    def missing(key):
        raise zoneinfo.ZoneInfoNotFoundError("No time zone found with key %s" % key)
    monkeypatch.setattr(zoneinfo, "ZoneInfo", missing)


def test_without_a_tz_database_numbers_stay_and_named_zone_resets_are_unknown(no_tz_database):
    rows = by_pool(usage.claude_source(runner=claude_runner(envelope()), now=NOW))
    assert rows["claude_5h"]["remaining_percent"] == 77.0 and rows["claude_week"]["remaining_percent"] == 39.0
    assert rows["claude_5h"]["resets_at"] is None and rows["claude_week"]["resets_at"] is None


@pytest.mark.parametrize("zone", ["UTC", "GMT", "Etc/UTC", "utc"])
def test_utc_resets_need_no_tz_database(no_tz_database, zone):
    text = "Current session: 23% used \u00b7 resets Nov 4, 2:19pm (" + zone + ")\n"
    rows = by_pool(usage.claude_source(runner=claude_runner(envelope(text=text)), now=NOW))
    assert rows["claude_5h"]["resets_at"] == "2026-11-04T14:19:00+00:00"


def test_claude_refuses_numbers_when_envelope_shows_model_usage():
    for bad in (envelope(num_turns=1), envelope(total_cost_usd=0.01), envelope(modelUsage={"x": {}}),
                envelope(usage={"input_tokens": 5})):
        rows = usage.claude_source(runner=claude_runner(bad), now=NOW)
        assert all(r["remaining_percent"] is None and "refused" in r["source"] for r in rows)


def test_claude_failures_are_unknown_without_numbers():
    cases = [claude_runner(envelope(), code=1), claude_runner("not json"),
             claude_runner(envelope(is_error=True)), claude_runner(envelope(text="nothing here"))]

    def boom(cmd):
        raise TimeoutError("90 s")
    for runner in cases + [boom]:
        rows = usage.claude_source(runner=runner, now=NOW)
        assert [r["pool"] for r in rows] == list(usage.CLAUDE_POOLS)
        assert all(r["remaining_percent"] is None for r in rows)


def test_claude_line_without_reset_keeps_number_and_null_reset():
    rows = by_pool(usage.claude_source(runner=claude_runner(envelope(text="Current session: 5% used")), now=NOW))
    assert rows["claude_5h"]["remaining_percent"] == 95.0 and rows["claude_5h"]["resets_at"] is None
    assert rows["claude_week"]["remaining_percent"] is None


# ---------------------------------------------------------------- Codex








# ---------------------------------------------------------------- Antigravity / Gemini




# ---------------------------------------------------------------- cache + read_usage
def _refresher(tmp_path, clock, **fetchers):
    return usage.UsageRefresher(tmp_path / "usage_cache.json", fetchers=fetchers, clock=lambda: clock[0])


def test_read_usage_without_cache_is_all_unknown(tmp_path):
    rows = usage.read_usage(tmp_path / "none.json", now=NOW)
    pools = {r["pool"] for r in rows}
    assert pools >= set(usage.CLAUDE_POOLS + usage.CODEX_POOLS + usage.AGY_POOLS) | {"gemini_app"}
    for r in rows:
        assert r["status"] == "UNKNOWN" and r["remaining_percent"] is None and r["observed_at"] is None
        assert set(r) >= {"pool", "remaining_percent", "observed_at", "resets_at", "status", "source"}


def test_refresh_then_read_fresh_then_stale(tmp_path):
    clock = [NOW]
    ref = _refresher(tmp_path, clock, claude=lambda: usage.claude_source(claude_runner(envelope()), now=clock[0]))
    ref.refresh("claude")
    assert (tmp_path / "usage_cache.json").exists()
    r = by_pool(usage.read_usage(tmp_path / "usage_cache.json", now=NOW + timedelta(minutes=20)))["claude_5h"]
    assert r["status"] == "FRESH" and r["remaining_percent"] == 77.0 and r["age_seconds"] == 1200
    r = by_pool(usage.read_usage(tmp_path / "usage_cache.json", now=NOW + timedelta(minutes=21)))["claude_5h"]
    assert r["status"] == "STALE" and r["remaining_percent"] == 77.0  # older than 2 x 10 min
    late = by_pool(usage.read_usage(tmp_path / "usage_cache.json", now=NOW + timedelta(hours=4)))["claude_5h"]
    assert late["status"] == "STALE"  # reset time has passed


def test_codex_freshness_window_is_two_intervals(tmp_path):
    clock = [NOW]
    ref = _refresher(tmp_path, clock, codex=lambda: usage.codex_source(codex_home=codex_rollout(tmp_path)))
    ref.refresh("codex")
    cache = tmp_path / "usage_cache.json"
    assert by_pool(usage.read_usage(cache, now=NOW + timedelta(minutes=3)))["codex_5h"]["status"] == "FRESH"
    assert by_pool(usage.read_usage(cache, now=NOW + timedelta(minutes=3, seconds=1)))["codex_5h"]["status"] == "STALE"




def test_failed_refresh_keeps_last_real_number_which_ages_to_stale(tmp_path):
    clock = [NOW]
    good = [True]

    def claude():
        runner = claude_runner(envelope()) if good[0] else claude_runner("boom", code=1)
        return usage.claude_source(runner, now=clock[0])
    ref = _refresher(tmp_path, clock, claude=claude)
    ref.refresh("claude")
    good[0], clock[0] = False, NOW + timedelta(minutes=10)
    ref.refresh("claude")
    cache = tmp_path / "usage_cache.json"
    r = by_pool(usage.read_usage(cache, now=NOW + timedelta(minutes=30)))["claude_5h"]
    assert r["remaining_percent"] == 77.0 and r["status"] == "STALE" and "failed" in r["last_error"]
    assert r["observed_at"] == NOW.isoformat(timespec="seconds")  # never re-stamped
    assert usage.load_cache(cache)["sources"]["claude"]["error"]


def test_fetcher_exception_does_not_invent_rows(tmp_path):
    clock = [NOW]

    def broken():
        raise RuntimeError("bug")
    ref = _refresher(tmp_path, clock, codex=broken)
    rows = ref.refresh("codex")
    assert all(r["remaining_percent"] is None for r in rows)
    out = by_pool(usage.read_usage(tmp_path / "usage_cache.json", now=NOW))
    assert out["codex_5h"]["status"] == "UNKNOWN" and "bug" in out["codex_5h"]["source"]


def test_gemini_app_always_unknown(tmp_path):
    r = by_pool(usage.read_usage(tmp_path / "x.json", now=NOW))["gemini_app"]
    assert r["status"] == "UNKNOWN" and r["remaining_percent"] is None and "manual" in r["source"]


def test_corrupt_cache_is_unknown(tmp_path):
    p = tmp_path / "usage_cache.json"
    p.write_text("{not json", encoding="utf-8")
    assert all(r["status"] == "UNKNOWN" for r in usage.read_usage(p, now=NOW))


def test_reading_usage_never_bumps_store_revision(store, tmp_path):
    rev = store.revision()
    clock = [NOW]
    ref = _refresher(tmp_path, clock, codex=lambda: usage.codex_source(codex_home=codex_rollout(tmp_path)))
    ref.refresh("codex")
    usage.read_usage(tmp_path / "usage_cache.json", now=NOW)
    assert store.revision() == rev


def test_read_usage_is_fast_and_never_runs_sources(tmp_path, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("read_usage must not run a source")
    monkeypatch.setattr(usage, "_default_claude_runner", forbidden)
    monkeypatch.setattr(usage, "_default_agy_runner", forbidden)
    t0 = time.monotonic()
    usage.read_usage(tmp_path / "usage_cache.json", now=NOW)
    assert time.monotonic() - t0 < 1.0


# ---------------------------------------------------------------- background thread
def test_background_refresher_writes_cache_and_stops(tmp_path):
    calls = []

    def codex():
        calls.append(1)
        real = datetime.now(timezone.utc)  # real clock: the refresher reads the cache against wall time
        return usage.codex_source(codex_home=codex_rollout(tmp_path, at=real))
    ref = usage.UsageRefresher(tmp_path / "usage_cache.json", fetchers={"codex": codex}, intervals={"codex": 0.05})
    ref.start()
    try:
        deadline = time.monotonic() + 5
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        ref.stop()
    assert len(calls) >= 2
    assert all(not t.is_alive() for t in ref.threads)
    assert by_pool(usage.read_usage(tmp_path / "usage_cache.json"))["codex_5h"]["status"] == "FRESH"


def test_recent_cache_delays_first_refresh(tmp_path):
    clock = [NOW]
    ref = _refresher(tmp_path, clock, claude=lambda: usage.claude_source(claude_runner(envelope()), now=clock[0]))
    assert ref.first_delay("claude") == 0.0
    ref.refresh("claude")
    clock[0] = NOW + timedelta(minutes=4)
    assert ref.first_delay("claude") == pytest.approx(360.0)


# ---------------------------------------------------------------- resets
def test_merged_resets_sorted_and_skips_past_or_missing():
    rows = [{"pool": "codex_5h", "resets_at": (NOW + timedelta(hours=2)).isoformat()},
            {"pool": "claude_5h", "resets_at": (NOW + timedelta(hours=1)).isoformat()},
            {"pool": "claude_week", "resets_at": (NOW - timedelta(hours=1)).isoformat()},
            {"pool": "gemini_app", "resets_at": None},
            {"pool": "antigravity_week", "resets_at": (NOW + timedelta(days=4)).isoformat()}]
    out = usage.merged_resets(rows, now=NOW)
    assert [r["pool"] for r in out] == ["claude_5h", "codex_5h", "antigravity_week"]
    assert set(out[0]) == {"pool", "resets_at"}
    assert out[0]["resets_at"] == "2026-11-04T12:00:00+00:00"


def test_reset_clock_still_serves_api_shape():
    rows = [{"pool": "codex_5h", "status": "FRESH", "resets_at": (NOW + timedelta(hours=2)).isoformat()},
            {"pool": "claude_5h", "status": "STALE", "resets_at": (NOW + timedelta(hours=1)).isoformat()},
            {"pool": "antigravity_week", "status": "FRESH", "resets_at": (NOW + timedelta(minutes=5)).isoformat()}]
    clock = usage.reset_clock(rows, now=NOW)
    assert [r["pool"] for r in clock["resets"]] == ["claude_5h", "codex_5h"]
    assert clock["next"]["pool"] == "claude_5h" and clock["next"]["in_seconds"] == 3600
