"""P2 model_health: tiny probes of the free chat routes through an injected HTTP function.

No test touches the network, ``~/.dsh`` or ``data/``: every path is under ``tmp_path`` and every
request goes to a fake that records it.
"""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from cc_engine import model_health as mh
from cc_engine import models
from cc_engine.api import HttpError

NOW = datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc)

LINEUP = {
    "freebuff": {"apiKeyEnv": "FREEBUFF_API_KEY", "api": "openai-completions", "baseURL": "http://127.0.0.1:1455/v1",
                 "models": [{"id": "deepseek/deepseek-v4-flash", "name": "DeepSeek V4.1 Flash"},
                            {"id": "openai/gpt-6-luna", "name": "GPT-6 Luna"}]},
    "github-copilot": {"models": [{"id": "kimi-k3", "name": "Kimi K3"}, {"id": "gpt-6-luna", "name": "GPT-6 Luna"}]},
    "google": {"apiKeyEnv": "GEMINI_API_KEY", "models": [{"id": "gemini-3.8-flash", "name": "Gemini 3.8 Flash"}]},
    "groq": {"apiKeyEnv": "GROQ_API_KEY", "models": [{"id": "llama-3.3-70b-versatile", "name": "Llama 3.3 70B"},
                                                     {"id": "qwen/qwen3.8-27b", "name": "Qwen 3.8 27B"}]},
    "openai-codex": {"models": [{"id": "gpt-6.1-sol", "name": "GPT-6.1 Sol"}]},
    "anthropic": {"models": [{"id": "claude-opus-5-5", "name": "Claude Opus 5.5"}]},
    "antigravity": {"models": [{"id": "gemini-4-argon", "name": "Gemini 4 Argon"}]},
}

COPILOT_TOKEN = "tid=abc;exp=1999999999;proxy-ep=proxy.individual.githubcopilot.com;sku=student"


def creds(expires_ms=4102444800000):
    return {"refs": {"FREEBUFF_API_KEY": "freebuff-local",
                     "GEMINI_API_KEY": "SECRET-GEMINI-123", "GROQ_API_KEY": "SECRET-GROQ-456"},
            "records": {"llm-pi-ai/github-copilot": {"kind": "grant", "payload": {
                "access": COPILOT_TOKEN, "refresh": "SECRET-GH-REFRESH", "expires": expires_ms,
                "availableModelIds": ["kimi-k3", "gpt-6-luna"]}}}}


class FakeHttp:
    """Answers by (method, url suffix); records every call."""

    def __init__(self, answers=None):
        self.calls = []
        self.answers = answers or {}

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append(SimpleNamespace(method=method, url=url, headers=dict(headers),
                                          body=json.loads(body) if body else None, timeout=timeout))
        for (m, needle), ans in self.answers.items():
            if m == method and needle in url and (not isinstance(ans, dict) or "model" not in ans
                                                   or (body and json.loads(body).get("model") == ans["model"])):
                ans = ans["answer"] if isinstance(ans, dict) and "answer" in ans else ans
                if isinstance(ans, Exception):
                    raise ans
                return ans
        if method == "GET":
            return 200, json.dumps({"data": []})
        return 200, json.dumps({"choices": [{"message": {"content": "o"}}]})


def ok_lists():
    return {
        ("GET", "1455/v1/models"): (200, json.dumps({"data": [{"id": "deepseek/deepseek-v4-flash"},
                                                              {"id": "openai/gpt-6-luna"}]})),
        ("GET", "api.groq.com/openai/v1/models"): (200, json.dumps({"data": [{"id": "llama-3.3-70b-specdec"},
                                                                             {"id": "qwen/qwen3.8-27b"}]})),
        ("GET", "generativelanguage.googleapis.com/v1beta/openai/models"): (
            200, json.dumps({"data": [{"id": "models/gemini-3.8-flash"}]})),
        ("GET", "githubcopilot.com/models"): (200, json.dumps({"data": [{"id": "kimi-k3"}, {"id": "gpt-6-luna"}]})),
    }


def run(tmp_path, http, now=NOW, **kw):
    return mh.refresh(LINEUP, creds(), http=http, path=tmp_path / "model_health.json", now=now, **kw)


# ---------------------------------------------------------------- probing
def test_model_health_probes_only_free_routes_with_one_token(tmp_path):
    http = FakeHttp(ok_lists())
    run(tmp_path, http)
    urls = " ".join(c.url for c in http.calls)
    for banned in ("anthropic", "claude", "codex", "antigravity", "chatgpt.com", "openai.com"):
        assert banned not in urls
    posts = [c for c in http.calls if c.method == "POST"]
    probed = {c.body["model"] for c in posts}
    assert "gpt-6.1-sol" not in probed and "claude-opus-5-5" not in probed and "gemini-4-argon" not in probed
    assert {"deepseek/deepseek-v4-flash", "openai/gpt-6-luna", "kimi-k3",
            "gemini-3.8-flash", "qwen/qwen3.8-27b"} <= probed
    for c in posts:
        if "/responses" in c.url:
            assert c.body["max_output_tokens"] <= 16
        else:
            assert c.body["max_tokens"] == 1
        assert c.timeout <= 60


def test_model_health_at_most_hourly_then_again_after_an_hour(tmp_path):
    http = FakeHttp(ok_lists())
    run(tmp_path, http)
    first = len(http.calls)
    assert first
    run(tmp_path, http, now=NOW + timedelta(minutes=59))
    assert len(http.calls) == first  # nothing re-probed inside the hour
    run(tmp_path, http, now=NOW + timedelta(minutes=61))
    assert len(http.calls) > first
    n = len(http.calls)
    run(tmp_path, http, now=NOW + timedelta(minutes=62), force=True)
    assert len(http.calls) > n


def test_model_health_cache_has_no_secrets(tmp_path):
    http = FakeHttp(ok_lists())
    run(tmp_path, http)
    text = (tmp_path / "model_health.json").read_text(encoding="utf-8")
    data = json.loads(text)
    assert data["schema"] == mh.SCHEMA
    for secret in ("SECRET-GEMINI-123", "SECRET-GROQ-456", "SECRET-GH-REFRESH", "tid=abc", "freebuff-local"):
        assert secret not in text
    # keys travel in headers only, never in a URL
    for c in http.calls:
        assert "SECRET" not in c.url
    gem = next(c for c in http.calls if "googleapis" in c.url)
    assert gem.headers["Authorization"] == "Bearer SECRET-GEMINI-123"


def test_model_health_records_failure_reasons(tmp_path):
    answers = ok_lists()
    answers[("POST", "1455/v1/chat/completions")] = {"model": "deepseek/deepseek-v4-flash", "answer": (
        500, json.dumps({"error": {"message": "refresh iteration budget exhausted"}}))}
    answers[("POST", "generativelanguage")] = (503, json.dumps(
        [{"error": {"code": 503, "message": "The model is overloaded. High demand.", "status": "UNAVAILABLE"}}]))
    answers[("POST", "githubcopilot.com/chat/completions")] = {"model": "kimi-k3", "answer": (
        400, json.dumps({"error": {"message": "The requested model is not supported.", "code": "model_not_supported"}}))}
    cache = run(tmp_path, FakeHttp(answers))
    r = cache["routes"]
    assert r["freebuff/deepseek/deepseek-v4-flash"]["ok"] is False
    assert "budget exhausted" in r["freebuff/deepseek/deepseek-v4-flash"]["reason"]
    assert r["freebuff/openai/gpt-6-luna"]["ok"] is True and r["freebuff/openai/gpt-6-luna"]["reason"] is None
    assert "503" in r["google/gemini-3.8-flash"]["reason"] or "demand" in r["google/gemini-3.8-flash"]["reason"]
    assert r["github-copilot/kimi-k3"]["ok"] is False and "not supported" in r["github-copilot/kimi-k3"]["reason"]
    assert r["github-copilot/gpt-6-luna"]["ok"] is True
    for v in r.values():
        assert v["checked_at"] and len(v["reason"] or "") <= 200


def test_model_health_corrects_ids_against_the_live_list(tmp_path):
    http = FakeHttp(ok_lists())
    cache = run(tmp_path, http)
    llama = cache["routes"]["groq/llama-3.3-70b-versatile"]
    assert llama["ok"] is False and "live model list" in llama["reason"]
    assert llama["replacement"] == "llama-3.3-70b-specdec"
    # a model missing from the live list is not chat-probed
    assert not [c for c in http.calls if c.method == "POST" and c.body.get("model") == "llama-3.3-70b-versatile"]
    assert cache["routes"]["groq/qwen/qwen3.8-27b"]["ok"] is True
    assert mh.suggest_id("llama-3.3-70b-versatile", ["llama-3.3-70b-specdec", "llama-4-scout"]) == "llama-3.3-70b-specdec"
    assert mh.suggest_id("kimi-k3", ["gpt-6-luna"]) is None
    assert mh.suggest_id("Gemini-3.8-Flash", ["models/gemini-3.8-flash"]) == "gemini-3.8-flash"


def test_model_health_unreachable_route_is_a_failure_not_a_crash(tmp_path):
    answers = {("GET", "1455"): OSError("connection refused"), ("POST", "1455"): OSError("connection refused")}
    cache = run(tmp_path, FakeHttp(answers))
    fb = cache["routes"]["freebuff/deepseek/deepseek-v4-flash"]
    assert fb["ok"] is False and "unreachable" in fb["reason"].lower()


def test_model_health_copilot_uses_its_token_base_and_headers(tmp_path):
    http = FakeHttp(ok_lists())
    run(tmp_path, http)
    cop = [c for c in http.calls if "githubcopilot.com" in c.url]
    assert cop and all(c.url.startswith("https://api.individual.githubcopilot.com/") for c in cop)
    assert all(c.headers.get("Copilot-Integration-Id") == "vscode-chat" for c in cop)
    assert all(c.headers["Authorization"] == "Bearer " + COPILOT_TOKEN for c in cop)


def test_model_health_copilot_expired_token_is_skipped(tmp_path):
    http = FakeHttp(ok_lists())
    cache = mh.refresh(LINEUP, creds(expires_ms=1000), http=http, path=tmp_path / "h.json", now=NOW)
    assert not [c for c in http.calls if "githubcopilot.com" in c.url]
    assert "github-copilot/kimi-k3" not in cache["routes"]  # unknown stays unknown


def test_model_health_health_for_reports_stale(tmp_path):
    cache = run(tmp_path, FakeHttp(ok_lists()))
    h = mh.health_for(cache, "freebuff", "openai/gpt-6-luna", now=NOW + timedelta(minutes=10))
    assert h["ok"] is True and h["stale"] is False and h["checked_at"]
    h2 = mh.health_for(cache, "freebuff", "openai/gpt-6-luna", now=NOW + timedelta(hours=7))
    assert h2["stale"] is True
    assert mh.health_for(cache, "anthropic", "claude-opus-5-5", now=NOW) is None
    assert mh.health_for({}, "freebuff", "x", now=NOW) is None


def test_model_health_bad_cache_file_never_raises(tmp_path):
    p = tmp_path / "model_health.json"
    p.write_text("{not json", encoding="utf-8")
    assert mh.load(p) == {}
    assert mh.load(tmp_path / "missing.json") == {}


# ---------------------------------------------------------------- registry + API
CORDIS = """\
- id: agent-default-model
  config:
    provider: freebuff
    model: deepseek/deepseek-v4-flash
- id: llm-pi-ai
  config:
    providers:
      freebuff:
        apiKeyEnv: FREEBUFF_API_KEY
        baseURL: http://127.0.0.1:1455/v1
        models:
          - id: deepseek/deepseek-v4-flash
            name: DeepSeek V4.1 Flash
          - id: openai/gpt-6-luna
            name: GPT-6 Luna
      anthropic:
        models:
          - id: claude-opus-5-5
            name: Claude Opus 5.5
"""


@pytest.fixture
def src(tmp_path):
    (tmp_path / "cordis.patch.yml").write_text(CORDIS, encoding="utf-8")
    (tmp_path / "credentials.yaml").write_text("version: 1\nrefs:\n  FREEBUFF_API_KEY: freebuff-local\n",
                                               encoding="utf-8")
    return models.Sources(cordis=tmp_path / "cordis.patch.yml", credentials=tmp_path / "credentials.yaml",
                          mcp_server=tmp_path / "none.py", state=tmp_path / "data" / "models_disabled.json",
                          health=tmp_path / "data" / "model_health.json", environ={},
                          registry_reader=lambda n: None)


def write_health(path, entries, at=None):
    at = (at or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": mh.SCHEMA, "routes": {
        k: dict({"ok": ok, "reason": reason, "checked_at": at}) for k, (ok, reason) in entries.items()}}),
        encoding="utf-8")


def test_model_health_registry_rows_carry_health_and_strongest_skips_failures(src):
    write_health(src.health, {"freebuff/deepseek/deepseek-v4-flash": (False, "refresh iteration budget exhausted"),
                              "freebuff/openai/gpt-6-luna": (True, None)})
    rows = {r["id"]: r for r in models.build_registry(src, usage_rows=[])}
    fb = rows["chat:freebuff/deepseek/deepseek-v4-flash"]
    assert fb["health"]["ok"] is False and "budget" in fb["health"]["reason"] and fb["health"]["checked_at"]
    luna = rows["chat:freebuff/openai/gpt-6-luna"]
    assert luna["health"]["ok"] is True
    assert luna["strongest"] is True and fb["strongest"] is False  # strongest WORKING model per pool
    assert rows["chat:anthropic/claude-opus-5-5"]["health"] is None  # never probed
    assert rows["agent:codex"]["health"] is None


def test_model_health_unknown_health_is_null_not_invented(src):
    rows = {r["id"]: r for r in models.build_registry(src, usage_rows=[])}
    h = rows["chat:freebuff/openai/gpt-6-luna"]["health"]
    assert h == {"ok": None, "reason": None, "checked_at": None, "stale": False, "replacement": None}


class H:
    def __init__(self, src, actor="owner", body=None, http=None):
        self.app = SimpleNamespace(models_sources=src, usage_reader=lambda: [], store=None, model_health_http=http)
        self.actor = actor
        self._body = body or {}

    def _json_body(self):
        return self._body


def test_model_health_routes_and_api_models_expose_health(src):
    write_health(src.health, {"freebuff/openai/gpt-6-luna": (True, None)})
    assert "GET /api/model_health" in mh.ROUTES and "POST /api/model_health/probe" in mh.ROUTES
    out = mh.ROUTES["GET /api/model_health"](H(src), {})
    assert out["routes"]["freebuff/openai/gpt-6-luna"]["ok"] is True
    rows = models.g_models(H(src), {})["models"]
    assert all("health" in r for r in rows)
    with pytest.raises(HttpError) as e:
        mh.ROUTES["POST /api/model_health/probe"](H(src, actor="router"), {})
    assert e.value.status == 403


def test_model_health_owner_probe_runs_in_background_with_injected_http(src):
    http = FakeHttp({("GET", "1455/v1/models"): (200, json.dumps({"data": [{"id": "deepseek/deepseek-v4-flash"},
                                                                          {"id": "openai/gpt-6-luna"}]}))})
    out = mh.ROUTES["POST /api/model_health/probe"](H(src, body={"force": True}, http=http), {})
    assert out["ok"] is True and out["started"] is True
    mh.wait_idle(10)
    data = json.loads(src.health.read_text(encoding="utf-8"))
    assert data["routes"]["freebuff/openai/gpt-6-luna"]["ok"] is True
    assert not [c for c in http.calls if "claude" in json.dumps(c.body or {})]


# ---------------------------------------------------------------- lessons from the first live run (4 Nov)
def test_model_health_copilot_probes_each_model_on_its_own_api(tmp_path):
    lineup = {"github-copilot": {"models": [{"id": "kimi-k3"}, {"id": "gpt-6-luna"}, {"id": "claude-haiku-4.5"}]}}
    answers = ok_lists()
    answers[("GET", "githubcopilot.com/models")] = (200, json.dumps({"data": [{"id": "kimi-k3"}, {"id": "gpt-6-luna"},
                                                                             {"id": "claude-haiku-4.5"}]}))
    http = FakeHttp(answers)
    mh.refresh(lineup, creds(), http=http, path=tmp_path / "h.json", now=NOW)
    by_model = {c.body["model"]: c for c in http.calls if c.method == "POST"}
    assert by_model["kimi-k3"].url.endswith("/chat/completions")
    assert by_model["gpt-6-luna"].url.endswith("/responses")
    assert by_model["claude-haiku-4.5"].url.endswith("/v1/messages")
    assert by_model["claude-haiku-4.5"].body["max_tokens"] == 1
    data = tmp_path / "pi.json"
    data.write_text(json.dumps({"openai-responses": {"chat:x": {"id": "odd-model"}}}), encoding="utf-8")
    assert mh.copilot_api("odd-model", data_path=data) == "openai-responses"
    assert mh.copilot_api("claude-x", data_path=tmp_path / "missing.json") == "anthropic-messages"


def test_model_health_sends_a_user_agent_cloudflare_accepts(tmp_path):
    http = FakeHttp(ok_lists())
    run(tmp_path, http)
    groq = [c for c in http.calls if "groq.com" in c.url]
    assert groq and all("python" not in c.headers.get("User-Agent", "python").lower() for c in groq)


def test_model_health_retries_once_when_freebuff_says_retry(tmp_path):
    seq = [(502, json.dumps({"error": {"message": "Session expired or model changed - retry immediately."}})),
           (200, json.dumps({"choices": [{"message": {"content": "o"}}]}))]
    calls = []

    def http(method, url, headers, body, timeout):
        calls.append(url)
        if method == "GET":
            return 200, json.dumps({"data": [{"id": "deepseek/deepseek-v4-flash"}]})
        return seq.pop(0) if seq else (200, "{}")
    lineup = {"freebuff": LINEUP["freebuff"] | {"models": [{"id": "deepseek/deepseek-v4-flash"}]}}
    slept = []
    cache = mh.refresh(lineup, creds(), http=http, path=tmp_path / "h.json", now=NOW, sleep=slept.append)
    assert cache["routes"]["freebuff/deepseek/deepseek-v4-flash"]["ok"] is True
    assert len(slept) == 1


def test_model_health_listed_but_refused_tries_the_closest_live_variant(tmp_path):
    lineup = {"github-copilot": {"models": [{"id": "kimi-k3"}]}}
    answers = {
        ("GET", "githubcopilot.com/models"): (200, json.dumps({"data": [{"id": "kimi-k3"}, {"id": "kimi-k3-base"},
                                                                       {"id": "kimi-k3-copilot"}]})),
        ("POST", "githubcopilot.com/chat/completions"): {"model": "kimi-k3", "answer": (
            400, json.dumps({"error": {"message": "The requested model is not supported."}}))},
    }
    cache = mh.refresh(lineup, creds(), http=FakeHttp(answers), path=tmp_path / "h.json", now=NOW)
    k = cache["routes"]["github-copilot/kimi-k3"]
    assert k["ok"] is False and k["replacement"] == "kimi-k3-copilot"
    assert mh.alternatives("kimi-k3", ["kimi-k3-base", "kimi-k3-copilot", "kimi-k2"], "github-copilot")[0] == "kimi-k3-copilot"
