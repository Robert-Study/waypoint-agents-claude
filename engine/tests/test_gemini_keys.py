"""Google AI Studio (Gemini API) key pool switcher (M3). Fixture files only; no key value ever leaves."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cc_engine import gemini_keys, models
from cc_engine.api import HttpError

SECRETS = ("SECRET-VALUE-AAA", "SECRET-VALUE-BBB", "SECRET-VALUE-CCC", "SECRET-ENV-ONLY", "SECRET-OUTSIDE")

CREDENTIALS = """\
version: 1
refs:
  FREEBUFF_API_KEY: freebuff-local
  GEMINI_API_KEY: SECRET-VALUE-AAA
  GROQ_API_KEY: gsk-fixture
records:
  llm-pi-ai/anthropic:
    kind: grant
    payload:
      access: keep-me-exactly
"""


@pytest.fixture
def paths(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# fixture\nGEMINI_API_KEY_A=SECRET-VALUE-AAA\nGEMINI_API_KEY_B='SECRET-VALUE-BBB'\n"
                   "GEMINI_API_KEY_C=\"SECRET-VALUE-CCC\"\nGEMINI_API_KEY=SECRET-VALUE-AAA\nOTHER=1\n",
                   encoding="utf-8")
    cred = tmp_path / ".credentials.yaml"
    cred.write_text(CREDENTIALS, encoding="utf-8")
    return SimpleNamespace(env=env, cred=cred, tmp=tmp_path)


def kw(paths, environ=None):
    return {"env_file": paths.env, "credentials": paths.cred, "environ": environ or {},
            "registry_reader": lambda name: None}


def no_secret(obj):
    text = json.dumps(obj)
    for s in SECRETS:
        assert s not in text, s


def test_gemini_keys_pool_lists_slots_without_values(paths):
    st = gemini_keys.status(**kw(paths))
    no_secret(st)
    assert [s["slot"] for s in st["slots"]] == ["A", "B", "C", "Primary"]
    assert [s["env"] for s in st["slots"]] == ["GEMINI_API_KEY_A", "GEMINI_API_KEY_B", "GEMINI_API_KEY_C",
                                                "GEMINI_API_KEY"]
    assert st["active"] == "A" and st["active_state"] == "matched"
    assert [s["active"] for s in st["slots"]] == [True, False, False, False]
    assert st["slots"][3]["same_as"] == "A"
    assert st["subagent_rotation"] == ["A", "B", "C"]
    assert "Antigravity" in st["separate_from"]
    for s in st["slots"]:
        assert set(s) <= {"slot", "label", "env", "present", "active", "same_as", "from"}


def test_gemini_keys_process_env_wins_like_existing_tooling(paths):
    st = gemini_keys.status(**kw(paths, environ={"GEMINI_API_KEY_B": "SECRET-ENV-ONLY", "GEMINI_API_KEY_D": "SECRET-ENV-ONLY"}))
    no_secret(st)
    b = next(s for s in st["slots"] if s["slot"] == "B")
    assert b["from"] == "environment"
    assert next(s for s in st["slots"] if s["slot"] == "D")["same_as"] == "B"


def test_gemini_keys_switch_rewrites_only_dsh_ref(paths):
    before = paths.cred.read_text(encoding="utf-8")
    st = gemini_keys.switch("B", **kw(paths))
    no_secret(st)
    after = paths.cred.read_text(encoding="utf-8")
    assert st["active"] == "B"
    assert "  GEMINI_API_KEY: SECRET-VALUE-BBB\n" in after
    # Every other line is byte-identical.
    assert [l for l in before.splitlines() if "GEMINI_API_KEY" not in l] == \
           [l for l in after.splitlines() if "GEMINI_API_KEY" not in l]
    assert gemini_keys.status(**kw(paths))["active"] == "B"
    gemini_keys.switch("A", **kw(paths))
    assert paths.cred.read_text(encoding="utf-8") == before
    assert not list(paths.tmp.glob("*.tmp"))


def test_gemini_keys_switch_refusals_never_leak(paths):
    for bad in ("Z", "", None, "GEMINI_API_KEY_A"):
        with pytest.raises(ValueError) as e:
            gemini_keys.switch(bad, **kw(paths))
        no_secret(str(e.value))
    # dsh holds a key that is not in the pool: switching would lose it, so refuse.
    paths.cred.write_text(CREDENTIALS.replace("SECRET-VALUE-AAA", "SECRET-OUTSIDE"), encoding="utf-8")
    st = gemini_keys.status(**kw(paths))
    assert st["active"] is None and st["active_state"] == "not_in_pool"
    with pytest.raises(gemini_keys.SwitchRefused) as e:
        gemini_keys.switch("B", **kw(paths))
    no_secret(str(e.value))
    assert "SECRET-OUTSIDE" in paths.cred.read_text(encoding="utf-8")


def test_gemini_keys_missing_files(paths, tmp_path):
    st = gemini_keys.status(env_file=tmp_path / "none.env", credentials=tmp_path / "none.yaml", environ={},
                            registry_reader=lambda n: None)
    assert st["slots"] == [] and st["active"] is None and st["active_state"] == "credentials_missing"
    with pytest.raises(gemini_keys.SwitchRefused):
        gemini_keys.switch("A", env_file=paths.env, credentials=tmp_path / "none.yaml", environ={},
                           registry_reader=lambda n: None)
    assert not (tmp_path / "none.yaml").exists()


def test_gemini_keys_registry_fallback_only_when_pool_empty(tmp_path, paths):
    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")
    st = gemini_keys.status(env_file=empty, credentials=paths.cred, environ={},
                            registry_reader=lambda name: "SECRET-VALUE-AAA" if name == "GEMINI_API_KEY" else None)
    no_secret(st)
    assert [s["slot"] for s in st["slots"]] == ["Registry"] and st["active"] == "Registry"


class H:
    def __init__(self, src, body=None, actor="owner"):
        events = []
        self.events = events
        self.app = SimpleNamespace(models_sources=src, usage_reader=lambda: [],
                                   store=SimpleNamespace(event=lambda **k: events.append(k),
                                                         notify=lambda *a: None))
        self.actor = actor
        self._body = body or {}

    def _json_body(self):
        return self._body


def test_gemini_keys_routes(paths, tmp_path):
    src = models.Sources(cordis=tmp_path / "c.yml", credentials=paths.cred, mcp_server=tmp_path / "m.py",
                         state=tmp_path / "s.json", env_file=paths.env, environ={}, registry_reader=lambda n: None)
    out = models.ROUTES["GET /api/models/gemini_keys"](H(src), {})
    no_secret(out)
    assert out["active"] == "A"
    with pytest.raises(HttpError) as e:
        models.ROUTES["POST /api/models/gemini_keys/switch"](H(src, {"slot": "B"}, actor="router"), {})
    assert e.value.status == 403
    assert gemini_keys.status(**kw(paths))["active"] == "A"
    h = H(src, {"slot": "B"})
    out = models.ROUTES["POST /api/models/gemini_keys/switch"](h, {})
    no_secret(out)
    no_secret(h.events)
    assert out["ok"] is True and out["active"] == "B"
    assert h.events and "slot B" in h.events[0]["summary"]
    for body in ({"slot": "Q"}, {}):
        with pytest.raises(HttpError) as e:
            models.ROUTES["POST /api/models/gemini_keys/switch"](H(src, body), {})
        assert e.value.status in (400, 409)
