"""M4 / G1: the dsh chat picker and mode menu that setup_deepseek_harness.py generates.

The setup script lives in the workspace repo and is loaded here as a module; its generation is
exercised as pure functions and through ``--out`` against ``tmp_path``. Nothing here writes to
``~/.dsh`` or ``data/``, and no provider is called.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

SETUP = Path(r"~\workspace\waypoint-workspace\tools\setup_deepseek_harness.py")
ENGINE = Path(__file__).resolve().parents[1]

# Insert ids in the patch before M4/G1 (copied from ~/.dsh/cordis.patch.yml on 4 Nov 2026).
PRE_TOP_IDS = {"agent-default-model", "tool-result-pruner", "llm-deepseek", "llm-deepseek-account", "llm-pi-ai"}
PRE_INSERT_IDS = {"mcp-rob-subagents", "mcp-owner-skills", "preset-superagent-router", "preset-superagent-worker"}
REFS = {"FREEBUFF_API_KEY": "x", "GROQ_API_KEY": "x", "GEMINI_API_KEY": "x"}


@pytest.fixture(scope="module")
def setup_mod():
    if not SETUP.is_file():
        pytest.skip("setup_deepseek_harness.py not present")
    spec = importlib.util.spec_from_file_location("setup_deepseek_harness_under_test", SETUP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _doc(text):
    doc = yaml.safe_load(text)
    assert isinstance(doc, list)
    return doc


def _ids(doc):
    top = {i.get("id") for i in doc if isinstance(i, dict) and i.get("id")}
    inserts = [s for i in doc if isinstance(i, dict) for s in (i.get("insert") or [])]
    return top, {s["id"] for s in inserts}, inserts


def _providers(doc):
    item = next(i for i in doc if isinstance(i, dict) and i.get("id") == "llm-pi-ai")
    return item["config"]["providers"]


def _presets(doc):
    _, _, inserts = _ids(doc)
    return {s["config"]["id"]: s["config"] for s in inserts if s.get("name") == "@deepseek-ai/dsh-agent-preset"}


def _plugin_ids(preset):
    out = []

    def walk(entries):
        for e in entries or []:
            out.append(e.get("id"))
            if e.get("group") and isinstance(e.get("config"), list):
                walk(e["config"])
    walk(preset["plugins"])
    return out


def fake_rows(strongest, access=None):
    """Registry chat rows: ``strongest`` = {(provider, model)}, ``access`` = {(provider, model): bool}."""
    def runner(patch_text):
        doc = yaml.safe_load(patch_text)
        rows = []
        for prov, cfg in _providers(doc).items():
            for m in cfg.get("models") or []:
                key = (prov, m["id"])
                rows.append({"dsh": {"provider": prov, "model": m["id"]}, "strongest": key in strongest,
                             "strongest_rule": "test rule" if key in strongest else None,
                             "access": (access or {}).get(key, True), "pool": f"provider:{prov}"})
        return rows
    return runner


# ------------------------------------------------------------------ shape
def test_picker_patch_is_valid_yaml_with_the_same_insert_ids(setup_mod):
    text, _ = setup_mod.build_home_patch(REFS, ranker=lambda t: None)
    doc = _doc(text)
    top, inserts, _ = _ids(doc)
    assert PRE_TOP_IDS <= top
    assert PRE_INSERT_IDS <= inserts
    # G1 adds exactly the Plan and Design presets.
    assert inserts - PRE_INSERT_IDS == {"preset-plan", "preset-design"}


def test_picker_patch_parses_with_meridians_reader(setup_mod):
    sys.path.insert(0, str(ENGINE))
    from cc_engine import models

    text, _ = setup_mod.build_home_patch(REFS, ranker=lambda t: None)
    ours = models.cordis_providers(models.parse_yaml(text))
    theirs = _providers(_doc(text))
    assert set(ours) == set(theirs)
    for prov in theirs:
        assert [m["id"] for m in ours[prov]["models"]] == [m["id"] for m in theirs[prov]["models"]]
    assert models.cordis_default(models.parse_yaml(text)) == {"provider": "freebuff", "model": "deepseek/deepseek-v4-flash"}
    # 4 Nov: Codex is gated by its Waypoint switch, not by a static deny list
    assert "delegate_codex_metered_subagent" not in models.cordis_deny(models.parse_yaml(text))


# ------------------------------------------------------------------ access / Claude / Codex
def test_picker_hides_claude_and_codex_chat_rows_behind_a_constant(setup_mod, monkeypatch):
    text, _ = setup_mod.build_home_patch(REFS, ranker=lambda t: None)
    provs = _providers(_doc(text))
    assert setup_mod.HIDE_CLAUDE_CODEX_CHAT_ROWS is True
    assert "anthropic" not in provs and "openai-codex" not in provs
    monkeypatch.setattr(setup_mod, "HIDE_CLAUDE_CODEX_CHAT_ROWS", False)
    text2, _ = setup_mod.build_home_patch(REFS, ranker=lambda t: None)
    provs2 = _providers(_doc(text2))
    assert "anthropic" in provs2 and "openai-codex" in provs2
    # Fixed label: Owner is on Claude Max, not Pro.
    assert "Max" in provs2["anthropic"]["displayName"]
    assert not any("Pro" in str(m.get("name")) for m in provs2["anthropic"]["models"])


def test_picker_names_are_plain_with_no_baked_badges(setup_mod, monkeypatch):
    monkeypatch.setattr(setup_mod, "HIDE_CLAUDE_CODEX_CHAT_ROWS", False)
    text, _ = setup_mod.build_home_patch(REFS, ranker=lambda t: None)
    for prov, cfg in _providers(_doc(text)).items():
        for m in cfg["models"]:
            name = str(m["name"])
            for baked in ("Context", "$0", "Metered", "OAuth", "Elo", "~", " tok/s", "GitHub Student", "Claude Pro"):
                assert baked not in name, (prov, name)


def test_picker_drops_models_meridian_says_robert_cannot_use(setup_mod):
    ranker = fake_rows({("freebuff", "deepseek/deepseek-v4-flash")},
                       access={("github-copilot", "gpt-5.3-codex"): False})
    text, report = setup_mod.build_home_patch(REFS, ranker=ranker)
    provs = _providers(_doc(text))
    assert all(m["id"] != "gpt-5.3-codex" for m in provs["github-copilot"]["models"])
    assert any("gpt-5.3-codex" in line and "no access" in line for line in report)


def test_picker_drops_key_routes_with_no_key(setup_mod):
    refs = {"FREEBUFF_API_KEY": "x"}  # no Groq, no Gemini key
    text, _ = setup_mod.build_home_patch(refs, ranker=lambda t: None)
    provs = _providers(_doc(text))
    assert "groq" not in provs and "google" not in provs
    assert "freebuff" in provs and "github-copilot" in provs  # sign-in routes are kept


# ------------------------------------------------------------------ strongest per pool
def test_picker_keeps_only_the_strongest_model_per_pool(setup_mod):
    strongest = {("freebuff", "openai/gpt-6-luna"), ("github-copilot", "kimi-k3"),
                 ("google", "gemini-3.8-flash"), ("groq", "qwen/qwen3.8-27b")}
    text, report = setup_mod.build_home_patch(REFS, ranker=fake_rows(strongest))
    provs = _providers(_doc(text))
    kept = {(p, m["id"]) for p, cfg in provs.items() for m in cfg["models"]}
    # dsh's default model always stays so a new chat never opens on a hidden model.
    assert kept == strongest | {("freebuff", "deepseek/deepseek-v4-flash")}
    assert any("strongest" in line for line in report)


def test_picker_strongest_rule_is_reversible(setup_mod, monkeypatch):
    monkeypatch.setattr(setup_mod, "PICKER_STRONGEST_ONLY", False)
    text, _ = setup_mod.build_home_patch(REFS, ranker=fake_rows({("freebuff", "deepseek/deepseek-v4-flash")}))
    assert len(_providers(_doc(text))["freebuff"]["models"]) == 4


@pytest.mark.parametrize("ranker", [
    lambda t: None,
    lambda t: (_ for _ in ()).throw(RuntimeError("engine missing")),
    lambda t: "not a list",
    lambda t: [{"nonsense": True}],
])
def test_picker_falls_back_to_the_full_list_when_meridian_is_missing(setup_mod, ranker):
    text, report = setup_mod.build_home_patch(REFS, ranker=ranker)
    provs = _providers(_doc(text))
    assert len(provs["freebuff"]["models"]) == 4
    assert len(provs["github-copilot"]["models"]) == 5
    assert "anthropic" not in provs  # the Claude/Codex constant does not depend on Meridian
    assert any("full list" in line for line in report)


def test_picker_ranks_with_meridians_real_registry(setup_mod, tmp_path, monkeypatch):
    """The real ranker runs Meridian's registry in a subprocess against tmp files only."""
    creds = tmp_path / "creds.yaml"
    creds.write_text(yaml.safe_dump({"version": 1, "refs": dict.fromkeys(REFS, "x"),
                                     "records": {"llm-pi-ai/github-copilot": {"kind": "grant", "payload": {
                                         "availableModelIds": ["kimi-k3", "gpt-6-luna"]}}}}), encoding="utf-8")
    monkeypatch.setenv("CC_DATA_DIR", str(tmp_path / "data"))
    full = setup_mod.render_home_patch(setup_mod.candidate_providers(REFS))
    rows = setup_mod.meridian_rank(full, credentials=creds)
    assert isinstance(rows, list) and rows
    by = {(r["dsh"]["provider"], r["dsh"]["model"]): r for r in rows}
    assert by[("github-copilot", "gpt-5.3-codex")]["access"] is False
    assert sum(1 for r in rows if r["dsh"]["provider"] == "freebuff" and r["strongest"]) == 1
    text, _ = setup_mod.build_home_patch(REFS, ranker=lambda t: setup_mod.meridian_rank(t, credentials=creds))
    copilot = [m["id"] for m in _providers(_doc(text))["github-copilot"]["models"]]
    assert copilot and "gpt-5.3-codex" not in copilot and len(copilot) == 1


def test_picker_meridian_rank_times_out_or_fails_quietly(setup_mod, tmp_path):
    assert setup_mod.meridian_rank("[]", engine_dir=tmp_path / "missing") is None
    assert setup_mod.meridian_rank("[]", python=str(tmp_path / "no-python.exe")) is None


# ------------------------------------------------------------------ G1 modes
def test_picker_modes_offer_plan_orchestrate_build_design(setup_mod):
    text, _ = setup_mod.build_home_patch(REFS, ranker=lambda t: None)
    presets = _presets(_doc(text))
    names = {pid: p["name"] for pid, p in presets.items()}
    assert names == {"plan": "Plan", "superagent-router": "Orchestrate", "superagent-worker": "Build", "design": "Design"}
    order = sorted(presets, key=lambda pid: presets[pid]["order"])
    assert order == ["plan", "superagent-router", "superagent-worker", "design"]
    assert all(presets[p]["order"] > 4 for p in presets)  # dsh's own modes (Chat etc.) stay first


def test_picker_plan_mode_is_read_only_and_drafts_a_checked_graph(setup_mod):
    text, _ = setup_mod.build_home_patch(REFS, ranker=lambda t: None)
    plan = _presets(_doc(text))["plan"]
    ids = _plugin_ids(plan)
    fs = next(e for e in plan["plugins"] if e.get("id") == "tool-fs")
    assert fs["config"]["write"] is False and fs["config"]["edit"] is False
    for banned in ("tool-pwsh", "tool-bash", "tool-jobs", "tool-subagent", "tool-subagent-fork"):
        assert banned not in ids
    assert "mcp-rob-control-centre" in ids and "tool-ask-user" in ids
    persona = plan["plugins"][0]["config"]["prefix"]
    assert "cc_draft_plan" in persona and "1-9" in persona
    assert "(Recommended)" not in persona and "pre-select" in persona


def test_picker_modes_never_start_runs_and_keep_short_personas(setup_mod):
    text, _ = setup_mod.build_home_patch(REFS, ranker=lambda t: None)
    assert "cc_start_run" not in text
    assert "Sprints tab" not in text  # runs start from the Autonomous tab now
    presets = _presets(_doc(text))
    router = presets["superagent-router"]
    assert "mcp-rob-control-centre" in _plugin_ids(router)
    assert "Autonomous tab" in router["plugins"][0]["config"]["prefix"]
    design = presets["design"]["plugins"][0]["config"]["prefix"]
    assert "screenshot" in design.lower()
    for p in presets.values():
        persona = p["plugins"][0]["config"]["prefix"]
        assert "{{model}}" in persona and "{{{{" not in persona
        assert len(persona) < 900, p["id"]
        assert "(Recommended)" not in persona


# ------------------------------------------------------------------ --out and launch safety
def test_picker_out_writes_only_the_given_path(setup_mod, tmp_path, monkeypatch):
    home = tmp_path / "dsh-home"
    monkeypatch.setattr(setup_mod, "DSH_HOME", home)
    monkeypatch.setattr(setup_mod, "meridian_rank", lambda text, **kw: None)
    out = tmp_path / "out" / "cordis.patch.yml"
    setup_mod.main(["--out", str(out)])
    assert out.is_file()
    _doc(out.read_text(encoding="utf-8"))
    assert not home.exists()  # no credentials, no home patch, no web profile reset


def test_picker_main_without_args_ignores_the_launchers_argv(setup_mod, tmp_path, monkeypatch):
    """start_dsh_app.py imports the module and calls main() while its own argv is set."""
    home = tmp_path / "dsh-home"
    monkeypatch.setattr(setup_mod, "DSH_HOME", home)
    monkeypatch.setattr(setup_mod, "CODEX_AUTH_FILE", tmp_path / "none.json")
    monkeypatch.setattr(setup_mod, "CLAUDE_CRED_FILE", tmp_path / "none2.json")
    monkeypatch.setattr(setup_mod, "ENV_FILE", tmp_path / "none.env")
    monkeypatch.setattr(setup_mod, "resolve_secret", lambda names, env: None)
    monkeypatch.setattr(setup_mod, "meridian_rank", lambda text, **kw: (_ for _ in ()).throw(OSError("down")))
    monkeypatch.setattr(sys, "argv", ["start_dsh_app.py", "--server-only"])
    setup_mod.main()
    doc = _doc((home / "cordis.patch.yml").read_text(encoding="utf-8"))
    assert "freebuff" in _providers(doc)
    assert (home / "profiles" / "web" / "cordis.patch.yml").read_text(encoding="utf-8") == "[]\n"
    creds = yaml.safe_load((home / ".credentials.yaml").read_text(encoding="utf-8"))
    # v4 B2: WAYPOINT_CLI_API_KEY is the CLI bridge's local placeholder (no real key).
    assert set(creds["refs"]) == {"FREEBUFF_API_KEY", "WAYPOINT_CLI_API_KEY"}


STANDARD = Path(r"~\AppData\Roaming\npm\node_modules\@deepseek-ai\dsh\node_modules"
                r"\@deepseek-ai\dsh-web-app\presets\standard.patch.yml")


def _children(entries):
    """Yield every child plugin entry, descending into cordis:group lists."""
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        if e.get("name") == "cordis:group" and isinstance(e.get("config"), list):
            yield from _children(e["config"])
        else:
            yield e


def test_picker_modes_carry_the_config_keys_dsh_requires(setup_mod):
    # 4 Nov: dsh dropped all four Meridian modes because children lacked required keys
    # (maxBytes, sampleOverCapGlobResults, provider, allowParallelInProgress). dsh's own
    # Standard preset is the reference for which keys a child needs (read-only).
    if not STANDARD.is_file():
        pytest.skip("dsh Standard preset not installed")
    class Loader(yaml.SafeLoader):
        pass
    Loader.add_multi_constructor("tag:yaml.org,2002:js", lambda loader, suffix, node: None)
    std = yaml.load(STANDARD.read_text(encoding="utf-8"), Loader=Loader)
    rows = [r for r in std if isinstance(r, dict)]
    rows += [i for r in rows for i in (r.get("insert") or []) if isinstance(i, dict)]
    std_plugins = next(r["config"]["plugins"] for r in rows
                       if isinstance(r.get("config"), dict) and "plugins" in r["config"])
    required = {}
    for e in _children(std_plugins):
        if isinstance(e.get("config"), dict):
            required[e["id"]] = set(e["config"])
    text, _ = setup_mod.build_home_patch(REFS, ranker=lambda t: None)
    missing = []
    for pid, preset in _presets(_doc(text)).items():
        for e in _children(preset["plugins"]):
            need = required.get(e.get("id"), set()) - {"prefix", "section"}
            have = set(e.get("config") or {}) if isinstance(e.get("config"), dict) else set()
            if need - have:
                missing.append(f"{pid}/{e['id']}: {sorted(need - have)}")
    assert not missing, missing


# ------------------------------------------------------------------ v3 P2: only models that answer
def health_rows(strongest, health=None, enabled=None, elo=None):
    """Registry rows like fake_rows plus ``health`` {(p, m): (ok, reason, stale, replacement)} and
    ``enabled`` {(p, m): bool}."""
    def runner(patch_text):
        doc = yaml.safe_load(patch_text)
        rows = []
        for prov, cfg in _providers(doc).items():
            for m in cfg.get("models") or []:
                k = (prov, m["id"])
                h = (health or {}).get(k)
                rows.append({"dsh": {"provider": prov, "model": m["id"]}, "strongest": k in strongest,
                             "strongest_rule": "test rule" if k in strongest else None, "access": True,
                             "pool": f"provider:{prov}", "enabled": (enabled or {}).get(k, True),
                             "elo": (elo or {}).get(k),
                             "health": None if h is None else {"ok": h[0], "reason": h[1], "stale": h[2],
                                                               "replacement": h[3], "checked_at": "2026-11-04T15:00:00+00:00"}})
        return rows
    return runner


def _kept(text):
    return {(p, m["id"]) for p, cfg in _providers(_doc(text)).items() for m in cfg["models"]}


def test_picker_model_health_drops_failed_models_and_reports_why(setup_mod):
    strongest = {("freebuff", "openai/gpt-6-luna"), ("google", "gemini-3.8-flash"), ("groq", "qwen/qwen3.8-27b"),
                 ("github-copilot", "gpt-6-luna")}
    health = {("google", "gemini-3.8-flash"): (False, "HTTP 503: model overloaded", False, None),
              ("github-copilot", "kimi-k3"): (False, "HTTP 400: model_not_supported", False, None),
              ("freebuff", "openai/gpt-6-luna"): (True, None, False, None)}
    text, report = setup_mod.build_home_patch(REFS, ranker=health_rows(strongest, health))
    kept = _kept(text)
    assert ("google", "gemini-3.8-flash") not in kept
    assert any("gemini-3.8-flash" in line and "503" in line for line in report)
    assert ("github-copilot", "kimi-k3") not in kept and ("github-copilot", "gpt-6-luna") in kept
    assert any("kimi-k3" in line and "model_not_supported" in line for line in report)


def test_picker_model_health_stale_or_unknown_health_never_hides(setup_mod):
    strongest = {("google", "gemini-3.8-flash"), ("freebuff", "deepseek/deepseek-v4-flash")}
    health = {("google", "gemini-3.8-flash"): (False, "old failure", True, None)}
    text, _ = setup_mod.build_home_patch(REFS, ranker=health_rows(strongest, health))
    assert ("google", "gemini-3.8-flash") in _kept(text)


def test_picker_model_health_replaces_an_id_the_live_list_renamed(setup_mod):
    strongest = {("groq", "llama-3.3-70b-versatile"), ("freebuff", "deepseek/deepseek-v4-flash")}
    health = {("groq", "llama-3.3-70b-versatile"): (False, "not in groq's live model list", False,
                                                     "llama-3.3-70b-specdec")}
    text, report = setup_mod.build_home_patch(REFS, ranker=health_rows(strongest, health))
    groq = _providers(_doc(text))["groq"]["models"]
    assert [m["id"] for m in groq] == ["llama-3.3-70b-specdec"]
    assert groq[0]["name"] == "Llama 3.3 70B"
    assert any("llama-3.3-70b-specdec" in line for line in report)


def test_picker_model_health_moves_the_default_off_a_failed_model(setup_mod):
    strongest = {("freebuff", "deepseek/deepseek-v4-flash"), ("google", "gemini-3.8-flash"),
                 ("groq", "qwen/qwen3.8-27b")}
    health = {("freebuff", "deepseek/deepseek-v4-flash"): (False, "HTTP 500: refresh iteration budget exhausted", False, None),
              ("freebuff", "openai/gpt-6-luna"): (False, "HTTP 500: refresh iteration budget exhausted", False, None),
              ("freebuff", "mimo/mimo-v2.6-pro"): (False, "HTTP 500", False, None),
              ("freebuff", "upstage/solar-mini4"): (False, "HTTP 500", False, None)}
    elo = {("google", "gemini-3.8-flash"): 1400, ("groq", "qwen/qwen3.8-27b"): 1300}
    text, report = setup_mod.build_home_patch(REFS, ranker=health_rows(strongest, health, elo=elo))
    doc = _doc(text)
    assert "freebuff" not in _providers(doc)
    default = next(i for i in doc if i.get("id") == "agent-default-model")["config"]
    assert default == {"provider": "google", "model": "gemini-3.8-flash"}
    assert any("default" in line.lower() for line in report)


def test_picker_model_health_hides_models_switched_off_in_waypoint(setup_mod):
    strongest = {("google", "gemini-3.8-flash"), ("freebuff", "deepseek/deepseek-v4-flash")}
    text, report = setup_mod.build_home_patch(REFS, ranker=health_rows(
        strongest, enabled={("google", "gemini-3.8-flash"): False}))
    assert ("google", "gemini-3.8-flash") not in _kept(text)
    assert any("switched off" in line for line in report)


def test_picker_model_health_rank_code_passes_health_and_enabled(setup_mod):
    for k in ("health", "enabled", "elo"):
        assert f'"{k}"' in setup_mod._RANK_CODE
    assert "model_health" in setup_mod._RANK_CODE
