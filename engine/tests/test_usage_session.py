"""Claude rate limits posted by the mod ($.session.usage) feed the usage bars and postpone `claude -p /usage`."""
from datetime import datetime, timedelta, timezone

from cc_engine import usage

AT = "2026-11-06T12:00:00+00:00"


def rows_by_pool(path, now):
    return {r["pool"]: r for r in usage.read_usage(path, now=now)}


def test_both_windows_are_stored_and_fresh(tmp_path):
    path = tmp_path / "usage_cache.json"
    n = usage.record_session_rate_limits(
        [{"kind": "five_hour", "percentUsed": 23.5, "resetsAt": "2026-11-06T15:00:00Z"},
         {"kind": "seven_day", "percentUsed": 61, "resetsAt": "2026-11-10T09:00:00Z"},
         {"kind": "spend_limit", "percentUsed": 5}], at=AT, cache_path=path)
    assert n == 2
    got = rows_by_pool(path, datetime(2026, 11, 6, 12, 1, tzinfo=timezone.utc))
    assert got["claude_5h"]["remaining_percent"] == 76.5 or got["claude_5h"]["remaining_percent"] == 76
    assert got["claude_week"]["remaining_percent"] == 39
    assert got["claude_5h"]["status"] == "FRESH"
    assert got["claude_5h"]["source"] == usage.SESSION_SOURCE


def test_partial_reading_keeps_the_other_window(tmp_path):
    path = tmp_path / "usage_cache.json"
    usage.record_session_rate_limits([{"kind": "five_hour", "percentUsed": 10},
                                      {"kind": "seven_day", "percentUsed": 50}], at=AT, cache_path=path)
    later = "2026-11-06T12:05:00+00:00"
    assert usage.record_session_rate_limits([{"kind": "five_hour", "percentUsed": 20}], at=later, cache_path=path) == 1
    got = rows_by_pool(path, datetime(2026, 11, 6, 12, 6, tzinfo=timezone.utc))
    assert got["claude_5h"]["remaining_percent"] == 80
    assert got["claude_week"]["remaining_percent"] == 50


def test_bad_values_are_ignored(tmp_path):
    path = tmp_path / "usage_cache.json"
    assert usage.record_session_rate_limits([{"kind": "five_hour", "percentUsed": "x"},
                                             {"kind": "seven_day", "percentUsed": 140},
                                             {"kind": "five_hour", "percentUsed": True}, "junk"], at=AT,
                                            cache_path=path) == 0
    assert not path.exists()


def test_refresher_waits_while_a_fresher_reading_exists(tmp_path):
    path = tmp_path / "usage_cache.json"
    now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
    usage.record_session_rate_limits([{"kind": "five_hour", "percentUsed": 1},
                                      {"kind": "seven_day", "percentUsed": 2}], at=now.isoformat(), cache_path=path)
    ref = usage.UsageRefresher(path, fetchers={"claude": lambda: []}, clock=lambda: now + timedelta(seconds=60))
    assert ref.first_delay("claude") == ref.intervals["claude"] - 60


def test_trace_post_feeds_the_usage_bars(tmp_path):
    from cc_engine import trace
    path = tmp_path / "usage_cache.json"
    events = [{"event": "turn.complete", "session_id": "s1", "at": AT,
               "usage": {"context_tokens": 1000, "rate_limits": [{"kind": "five_hour", "percentUsed": 40},
                                                                 {"kind": "seven_day", "percentUsed": 70}]}}]
    assert trace.feed_usage(events, path) == 2
    got = rows_by_pool(path, datetime(2026, 11, 6, 12, 1, tzinfo=timezone.utc))
    assert got["claude_week"]["remaining_percent"] == 30
    assert trace.feed_usage([{"event": "heartbeat", "session_id": "s1"}], path) == 0


def test_future_time_is_clamped(tmp_path):
    path = tmp_path / "usage_cache.json"
    usage.record_session_rate_limits([{"kind": "five_hour", "percentUsed": 1},
                                      {"kind": "seven_day", "percentUsed": 2}], at="2099-01-01T00:00:00Z",
                                     cache_path=path)
    row = rows_by_pool(path, datetime.now(timezone.utc))["claude_5h"]
    assert row["observed_at"] < "2099" and row["status"] == "FRESH"


def test_refresher_rests_after_a_run_even_if_the_cache_cannot_be_written(tmp_path):
    import threading, time
    runs = []
    ref = usage.UsageRefresher(tmp_path / "missing" / "deeper" / "x.json",
                               fetchers={"claude": lambda: runs.append(1) or []}, intervals={"claude": 600})
    ref.store_fails = True
    t = threading.Thread(target=ref._loop, args=("claude",), daemon=True)
    orig = usage.store_source
    usage.store_source = lambda *a, **k: (_ for _ in ()).throw(OSError("read-only"))
    try:
        t.start()
        time.sleep(1.0)
    finally:
        ref._stop.set()
        usage.store_source = orig
    assert len(runs) == 1
