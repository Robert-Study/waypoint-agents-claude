import pytest

from cc_engine import runsheet

KEYS = {"model_ladder", "effort", "max_concurrency", "native_external_ratio", "poll_seconds",
        "use_pool_fully", "auto_resume", "power_off", "merge_resets", "sprints"}


def test_runsheet_has_exactly_four_presets():
    assert set(runsheet.PRESETS) == {"frugal", "balanced", "push", "max"}


@pytest.mark.parametrize("preset", ["frugal", "balanced", "push", "max"])
def test_runsheet_presets_expand_to_documented_keys(preset):
    out = runsheet.expand({"preset": preset, "pools": ["claude_code", "freebuff"]})
    assert set(out) == KEYS
    assert set(out["model_ladder"]) == {"claude_code", "freebuff"}
    assert all(isinstance(v, list) and v for v in out["model_ladder"].values())
    assert out["effort"] in runsheet.EFFORTS
    assert isinstance(out["max_concurrency"], int) and out["max_concurrency"] >= 1
    assert isinstance(out["poll_seconds"], int) and out["poll_seconds"] >= 30
    native, external = out["native_external_ratio"]
    assert native >= 0 and external >= 0 and native + external > 0


def test_runsheet_presets_scale_monotonically():
    outs = [runsheet.expand({"preset": p, "pools": ["claude_code"]}) for p in ("frugal", "balanced", "push", "max")]
    conc = [o["max_concurrency"] for o in outs]
    polls = [o["poll_seconds"] for o in outs]
    assert conc == sorted(conc) and conc[0] < conc[-1]
    assert polls == sorted(polls, reverse=True) and polls[0] > polls[-1]


def test_runsheet_use_pool_fully_only_for_ticked_pools():
    out = runsheet.expand({"preset": "max", "pools": {"claude_code": True, "gemini_app": False, "freebuff": True}})
    assert out["use_pool_fully"] == {"claude_code": True, "freebuff": True}
    out = runsheet.expand({"preset": "frugal", "pools": ["claude_code"]})
    assert out["use_pool_fully"] == {"claude_code": False}
    out = runsheet.expand({"preset": "balanced", "pools": ["freebuff"], "use_pool_fully": ["freebuff", "gemini_app"]})
    assert out["use_pool_fully"] == {"freebuff": True}


def test_runsheet_no_pools_means_nothing_runs():
    out = runsheet.expand({"preset": "push"})
    assert out["model_ladder"] == {} and out["use_pool_fully"] == {}


def test_runsheet_toggles_and_defaults():
    out = runsheet.expand({"preset": "balanced", "pools": ["claude_code"]})
    assert out["auto_resume"] is True and out["power_off"] is False and out["merge_resets"] is True
    out = runsheet.expand({"preset": "balanced", "pools": ["claude_code"], "auto_resume": False, "power_off": True})
    assert out["auto_resume"] is False and out["power_off"] is True


def test_runsheet_advanced_overrides():
    out = runsheet.expand({"preset": "frugal", "pools": ["claude_code"],
                           "advanced": {"max_concurrency": 3, "effort": "high", "poll_seconds": 120,
                                        "model_ladder": {"claude_code": ["sonnet"]}}})
    assert out["max_concurrency"] == 3 and out["effort"] == "high" and out["poll_seconds"] == 120
    assert out["model_ladder"]["claude_code"] == ["sonnet"]


@pytest.mark.parametrize("bad", [
    {"preset": "yolo"},
    {"preset": "max", "pools": ["metered_api"]},
    {"preset": "max", "advanced": {"max_concurrency": 0}},
    {"preset": "max", "advanced": {"effort": "ultra"}},
    {"preset": "max", "advanced": {"nope": 1}},
    # UI pool names that once leaked through the Run sheet (Codex is a run pool, ``codex``).
    {"preset": "balanced", "pools": ["claude"]},
    {"preset": "balanced", "pools": ["codex_cli"]},
    # Advanced values arrive as JSON numbers, never strings.
    {"preset": "balanced", "pools": ["claude_code"], "advanced": {"max_concurrency": "3"}},
    {"preset": "balanced", "pools": ["claude_code"], "advanced": {"poll_seconds": "600"}},
    {"preset": "balanced", "pools": ["claude_code"], "advanced": {"max_concurrency": 2.5}},
    {"preset": "balanced", "pools": ["claude_code"], "advanced": {"native_external_ratio": "2:1"}},
    {"preset": "balanced", "pools": ["claude_code"], "advanced": {"max_concurrency": ""}},
    # Sprints: ticked known pools and timezone-aware ISO start times only.
    {"preset": "balanced", "pools": ["claude_code"], "sprints": {"pool": "claude_code"}},
    {"preset": "balanced", "pools": ["claude_code"], "sprints": ["claude_code"]},
    {"preset": "balanced", "pools": ["claude_code"], "sprints": [{"pool": "codex", "start_at": "2026-11-04T12:00:00Z"}]},
    {"preset": "balanced", "pools": ["claude_code"], "sprints": [{"pool": "freebuff", "start_at": "2026-11-04T12:00:00Z"}]},
    {"preset": "balanced", "pools": ["claude_code"], "sprints": [{"pool": "claude_code"}]},
    {"preset": "balanced", "pools": ["claude_code"], "sprints": [{"pool": "claude_code", "start_at": "soon"}]},
    {"preset": "balanced", "pools": ["claude_code"], "sprints": [{"pool": "claude_code", "start_at": "2026-11-04T12:00:00"}]},
    {"preset": "balanced", "pools": ["claude_code"], "sprints": [{"pool": "claude_code", "start_at": 1762257600}]},
])
def test_runsheet_rejects_bad_settings(bad):
    with pytest.raises(ValueError):
        runsheet.expand(bad)


def test_runsheet_sprints_pass_through_unchanged():
    sprints = [{"pool": "claude_code", "start_at": "2026-11-04T12:01:00.000Z"},
               {"pool": "freebuff", "start_at": "2026-11-04T13:00:00+00:00"}]
    out = runsheet.expand({"preset": "balanced", "pools": ["claude_code", "freebuff"], "sprints": sprints})
    assert out["sprints"] == sprints
    assert out["sprints"] is not sprints and out["sprints"][0] is not sprints[0]
    assert runsheet.expand({"preset": "balanced", "pools": ["claude_code"]})["sprints"] == []
    assert runsheet.expand({"preset": "balanced", "pools": ["claude_code"], "sprints": []})["sprints"] == []


def test_runsheet_advanced_numbers_accepted():
    out = runsheet.expand({"preset": "balanced", "pools": ["claude_code"],
                           "advanced": {"max_concurrency": 3, "poll_seconds": 300, "native_external_ratio": [2, 1]}})
    assert out["max_concurrency"] == 3 and out["poll_seconds"] == 300 and out["native_external_ratio"] == [2, 1]
    # Blank advanced fields are omitted by the UI, so the preset stands.
    out = runsheet.expand({"preset": "balanced", "pools": ["claude_code"], "advanced": {}})
    assert out["max_concurrency"] == runsheet.PRESETS["balanced"]["max_concurrency"]


def test_runsheet_pools_exclude_codex():
    # The Create run sheet keeps its four pools; Codex is offered from Autonomous (RUN_POOLS).
    assert "codex" not in runsheet.POOLS and not any(p.startswith("codex") for p in runsheet.POOLS)
    assert "codex" in runsheet.RUN_POOLS


@pytest.fixture(autouse=True)
def _private_switches(tmp_path, monkeypatch):
    """expand() reads the live Agents switches by default; these tests use an empty database."""
    monkeypatch.setenv("CC_DB", str(tmp_path / "cc.sqlite"))
