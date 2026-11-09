"""Which free chat models actually answer right now (Meridian v3 P2).

Each free chat route (Freebuff, GitHub Copilot Student, Google AI Studio, Groq) gets a
1-token request at most once an hour per model, through an injectable ``http`` function so tests
never touch the network. Claude, Codex and Antigravity are never probed (``NEVER_PROBE``).

Where a provider publishes a live ``/models`` list (Freebuff proxy, Groq, AI Studio, Copilot) the
configured id is checked against it first. An id missing from the list is a failure with a
``replacement`` suggestion from the same model family, and is not chat-probed.

Results live in ``data/model_health.json`` (``$CC_DATA_DIR`` honoured):
``{"schema", "updated_at", "routes": {"<provider>/<model>": {"ok", "reason", "checked_at",
"replacement"}}, "lists": {provider: {"ids", "checked_at", "error"}}}``. Keys and tokens are read from
``~/.dsh/.credentials.yaml`` in memory only; they travel in request headers, never in a URL, the
cache or a reason string.

Consumers: ``models.build_registry`` adds ``health`` to chat rows and picks the strongest WORKING
model per pool; ``setup_deepseek_harness.py`` drops (or renames) failed models from dsh's picker;
``pick.py`` avoids them.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from . import config

SCHEMA = "model_health.v1"
FILE_NAME = "model_health.json"
PROBE_EVERY_S = 3600          # at most hourly per model
STALE_AFTER_S = 6 * 3600      # older results are shown but no longer hide a model
REASON_MAX = 200

FREE_PROVIDERS = ("freebuff", "github-copilot", "google", "groq")
NEVER_PROBE = ("anthropic", "openai-codex", "antigravity", "claude", "codex")

COPILOT_RECORD = "llm-pi-ai/github-copilot"
COPILOT_HEADERS = {"User-Agent": "GitHubCopilotChat/0.35.0", "Editor-Version": "vscode/1.107.0",
                   "Editor-Plugin-Version": "copilot-chat/0.35.0", "Copilot-Integration-Id": "vscode-chat",
                   "X-Initiator": "user", "Openai-Intent": "conversation-edits"}
# Groq sits behind Cloudflare, which refuses Python's default agent (error 1010).
USER_AGENT = "Waypoint-model-health/1.0"
# Copilot serves each model on one API (pi-ai's catalog in the dsh install, read only).
PI_AI_COPILOT = Path(config.NPM_ROOT + r"\node_modules\@deepseek-ai\dsh\node_modules"
                     r"\@earendil-works\pi-ai\dist\providers\data\github-copilot.json")
RETRY_SLEEP_S = 1.0
UNSUPPORTED = re.compile(r"not supported|not_supported|model_not_found|not found|does not exist|unknown model", re.I)

# Default endpoints; a ``baseURL`` in the dsh provider config wins.
ENDPOINTS = {
    "freebuff": {"base": "http://127.0.0.1:1455/v1", "api": "openai-completions", "key": "FREEBUFF_API_KEY",
                 "list": True, "timeout": 45},
    "groq": {"base": "https://api.groq.com/openai/v1", "api": "openai-completions", "key": "GROQ_API_KEY",
             "list": True, "timeout": 20},
    "google": {"base": "https://generativelanguage.googleapis.com/v1beta/openai", "api": "openai-completions",
               "key": "GEMINI_API_KEY", "list": True, "timeout": 30},
    "github-copilot": {"base": None, "api": "openai-completions", "key": None, "list": True, "timeout": 30},
}


# ---------------------------------------------------------------- paths / cache
def default_path() -> Path:
    return config.data_dir() / FILE_NAME


def load(path=None) -> dict:
    try:
        data = json.loads(Path(path or default_path()).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.05 * (attempt + 1))


def _iso(dt) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(text):
    try:
        dt = datetime.fromisoformat(str(text))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _age(entry, now):
    at = _parse((entry or {}).get("checked_at"))
    return (now - at).total_seconds() if at else None


def key(provider, model) -> str:
    return f"{provider}/{model}"


def health_for(cache, provider, model, now=None):
    """``{ok, reason, checked_at, stale, replacement}`` for a probed model, else None."""
    entry = ((cache or {}).get("routes") or {}).get(key(provider, model))
    if not isinstance(entry, dict):
        return None
    now = now or datetime.now(timezone.utc)
    age = _age(entry, now)
    return {"ok": entry.get("ok") if isinstance(entry.get("ok"), bool) else None,
            "reason": entry.get("reason"), "checked_at": entry.get("checked_at"),
            "stale": age is None or age > STALE_AFTER_S, "replacement": entry.get("replacement")}


def failed(health) -> bool:
    """A fresh failed probe: the only health that hides a model."""
    return isinstance(health, dict) and health.get("ok") is False and not health.get("stale")


# ---------------------------------------------------------------- ids
def _norm(mid):
    m = str(mid or "").strip().lower()
    return m[len("models/"):] if m.startswith("models/") else m


def suggest_id(configured, live_ids):
    """Same id (case/``models/`` prefix aside), else the closest id of the same family, else None.

    Family = the id with its last ``-word`` dropped (``llama-3.3-70b-versatile`` -> ``llama-3.3-70b``).
    """
    want = _norm(configured)
    live = sorted({_norm(i) for i in live_ids or [] if i})
    if want in live:
        return want
    stem = want.rsplit("-", 1)[0] if "-" in want else want
    if not stem or stem == want:
        return None
    family = [i for i in live if i == stem or i.startswith(stem + "-")]
    return min(family, key=lambda i: (len(i), i)) if family else None


# ---------------------------------------------------------------- secrets (memory only)
def _secrets(creds_tree):
    refs = creds_tree.get("refs") if isinstance(creds_tree, dict) and isinstance(creds_tree.get("refs"), dict) else {}
    recs = creds_tree.get("records") if isinstance(creds_tree, dict) and isinstance(creds_tree.get("records"), dict) else {}
    return {str(k): str(v) for k, v in refs.items() if v}, recs


def _copilot(recs, now):
    payload = (recs.get(COPILOT_RECORD) or {}).get("payload") if isinstance(recs.get(COPILOT_RECORD), dict) else None
    if not isinstance(payload, dict) or not payload.get("access"):
        return None
    exp = payload.get("expires")
    if isinstance(exp, (int, float)) and exp / 1000.0 <= now.timestamp() + 60:
        return None  # expired: dsh refreshes it on the next chat; we never use the refresh token
    token = str(payload["access"])
    m = re.search(r"proxy-ep=([^;]+)", token)
    base = "https://" + re.sub(r"^proxy\.", "api.", m.group(1)) if m else "https://api.individual.githubcopilot.com"
    return token, base


def _redact(text, secrets):
    out = str(text or "")
    for s in secrets:
        if s and len(s) >= 6:
            out = out.replace(s, "***")
    out = re.sub(r"(?i)(bearer\s+)\S+", r"\1***", out)
    out = re.sub(r"[A-Za-z0-9_\-]{40,}", "***", out)
    out = " ".join(out.split())
    return out[:REASON_MAX]


def _reason(status, body, secrets):
    msg = None
    try:
        data = json.loads(body)
        if isinstance(data, list) and data:
            data = data[0]
        if isinstance(data, dict):
            err = data.get("error", data)
            if isinstance(err, dict):
                msg = err.get("message") or err.get("code") or err.get("type")
            elif isinstance(err, str):
                msg = err
            msg = msg or data.get("message") or data.get("detail")
    except (TypeError, ValueError):
        msg = (body or "")[:160]
    return _redact(f"HTTP {status}: {msg}" if msg else f"HTTP {status}", secrets)


# ---------------------------------------------------------------- HTTP
def default_http(method, url, headers, body, timeout):
    """``(status, text)``; raises OSError when the route cannot be reached at all."""
    import urllib.error
    import urllib.request
    handlers = [urllib.request.ProxyHandler({})] if url.startswith("http://127.0.0.1") else []
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            text = e.read(65536).decode("utf-8", "replace")
        except Exception:
            text = ""
        return e.code, text
    except urllib.error.URLError as e:
        raise OSError(str(e.reason)) from None


# ---------------------------------------------------------------- probing
def _route(provider, cfg, secrets, recs, now):
    """``(base, api, headers, timeout, list?)`` or None when this route cannot be probed now."""
    ep = ENDPOINTS.get(provider)
    if not ep or provider in NEVER_PROBE:
        return None
    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT}
    if provider == "github-copilot":
        cop = _copilot(recs, now)
        if not cop:
            return None
        token, base = cop
        headers.update(COPILOT_HEADERS)
        headers["Authorization"] = "Bearer " + token
        return base, ep["api"], headers, ep["timeout"], ep["list"], [token]
    key_name = (cfg or {}).get("apiKeyEnv") or ep["key"]
    value = secrets.get(key_name)
    if not value:
        return None  # no key: the picker already hides this route
    headers["Authorization"] = "Bearer " + value
    base = str((cfg or {}).get("baseURL") or ep["base"]).rstrip("/")
    api = (cfg or {}).get("api") or ep["api"]
    return base, api, headers, ep["timeout"], ep["list"], [value]


_COPILOT_APIS = None


def _copilot_table(data_path=None) -> dict:
    table = {}
    try:
        data = json.loads(Path(data_path or PI_AI_COPILOT).read_text(encoding="utf-8"))
        for api, entries in (data.items() if isinstance(data, dict) else []):
            for v in (entries.values() if isinstance(entries, dict) else []):
                if isinstance(v, dict) and v.get("id"):
                    table[str(v["id"])] = str(api)
    except (OSError, ValueError, AttributeError):
        return {}
    return table


def copilot_api(model, data_path=None) -> str:
    """The API GitHub Copilot serves ``model`` on: pi-ai's catalog, else a name rule."""
    global _COPILOT_APIS
    if data_path:
        return _copilot_table(data_path).get(str(model)) or _api_by_name(model)
    if _COPILOT_APIS is None:
        _COPILOT_APIS = _copilot_table()
    return _COPILOT_APIS.get(str(model)) or _api_by_name(model)


def _api_by_name(model):
    m = str(model or "").lower()
    if m.startswith("claude"):
        return "anthropic-messages"
    if m.startswith(("gpt-", "mai-", "grok", "o1", "o3", "o4")):
        return "openai-responses"
    return "openai-completions"


def _probe_body(api, model):
    if api == "openai-responses":
        return {"model": model, "input": "ping", "max_output_tokens": 16, "store": False}
    if api == "anthropic-messages":
        return {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
    return {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1, "stream": False}


def _probe_url(base, api):
    if api == "openai-responses":
        return base + "/responses"
    if api == "anthropic-messages":
        return base + "/v1/messages"
    return base + "/chat/completions"


def _probe(http, base, api, headers, timeout, model, secrets, sleep=None):
    url = _probe_url(base, api)
    body = json.dumps(_probe_body(api, model)).encode("utf-8")
    hdrs = dict(headers, **({"anthropic-version": "2023-06-01"} if api == "anthropic-messages" else {}))
    try:
        status, text = http("POST", url, hdrs, body, timeout)
        if int(status) in (502, 503) and "retry" in str(text or "").lower():  # "retry immediately": once
            (sleep or time.sleep)(RETRY_SLEEP_S)
            status, text = http("POST", url, hdrs, body, timeout)
    except Exception as e:  # unreachable, timeout, refused
        return False, _redact(f"unreachable: {type(e).__name__}: {e}", secrets)
    if 200 <= int(status) < 300:
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            data = None
        if isinstance(data, dict) and data.get("error"):
            return False, _reason(status, text, secrets)
        return True, None
    return False, _reason(status, text, secrets)


def _live_ids(http, base, headers, timeout, secrets):
    try:
        status, text = http("GET", base + "/models", headers, None, timeout)
    except Exception as e:
        return None, _redact(f"unreachable: {type(e).__name__}: {e}", secrets)
    if not 200 <= int(status) < 300:
        return None, _reason(status, text, secrets)
    try:
        data = json.loads(text)
        items = data.get("data") if isinstance(data, dict) else data
        ids = [str(i.get("id")) for i in items if isinstance(i, dict) and i.get("id")]
    except (TypeError, ValueError, AttributeError):
        return None, "model list unreadable"
    return ids, None


def targets(lineup):
    """``[(provider, model_id, cfg)]`` for every free chat model in ``lineup`` ({provider: cfg})."""
    out = []
    for prov, cfg in (lineup or {}).items():
        if prov not in FREE_PROVIDERS or not isinstance(cfg, dict):
            continue
        for m in cfg.get("models") or []:
            if isinstance(m, dict) and m.get("id"):
                out.append((prov, str(m["id"]), cfg))
    return out


def alternatives(configured, live_ids, provider=""):
    """Live ids that extend ``configured`` (``kimi-k3`` -> ``kimi-k3-copilot``), provider-named first."""
    want = _norm(configured)
    token = str(provider or "").split("-")[-1].lower()
    alts = {_norm(i) for i in live_ids or [] if _norm(i).startswith(want + "-")}
    return sorted(alts, key=lambda i: ((token not in i) if token else True, len(i), i))


def refresh(lineup, creds_tree, http=None, path=None, now=None, force=False, max_age=PROBE_EVERY_S,
            sleep=None) -> dict:
    """Probe what is due, write the cache and return it. Never raises for a provider problem."""
    http = http or default_http
    now = now or datetime.now(timezone.utc)
    path = Path(path or default_path())
    cache = load(path)
    routes = dict(cache.get("routes") or {})
    lists = dict(cache.get("lists") or {})
    secrets, recs = _secrets(creds_tree)
    by_provider = {}
    for prov, mid, cfg in targets(lineup):
        by_provider.setdefault(prov, (cfg, []))[1].append(mid)
    for prov, (cfg, mids) in by_provider.items():
        due = [m for m in mids if force or (_age(routes.get(key(prov, m)), now) or max_age + 1) > max_age]
        if not due:
            continue
        route = _route(prov, cfg, secrets, recs, now)
        if route is None:
            continue
        base, api, headers, timeout, has_list, secret_vals = route
        hide = list(secrets.values()) + secret_vals
        live = None
        if has_list:
            ids, err = _live_ids(http, base, headers, timeout, hide)
            lists[prov] = {"ids": sorted(ids) if ids is not None else None, "checked_at": _iso(now), "error": err}
            live = {_norm(i) for i in ids} if ids else None  # an empty or failed list proves nothing
        for mid in due:
            entry = {"ok": None, "reason": None, "checked_at": _iso(now), "replacement": None,
                     "provider": prov, "model": mid}
            if live is not None and _norm(mid) not in live:
                entry["ok"] = False
                entry["reason"] = f"not in {prov}'s live model list"
                entry["replacement"] = suggest_id(mid, lists[prov]["ids"])
            else:
                m_api = copilot_api(mid) if prov == "github-copilot" else api
                entry["ok"], entry["reason"] = _probe(http, base, m_api, headers, timeout, mid, hide, sleep)
                # Listed but refused: try the closest live variant once (correct the id).
                if entry["ok"] is False and UNSUPPORTED.search(entry["reason"] or "") and live is not None:
                    for alt in alternatives(mid, lists[prov]["ids"], prov)[:1]:
                        a_api = copilot_api(alt) if prov == "github-copilot" else api
                        if _probe(http, base, a_api, headers, timeout, alt, hide, sleep)[0]:
                            entry["replacement"] = alt
                            entry["reason"] = _redact(f"{entry['reason']}; {alt} answers", hide)
            routes[key(prov, mid)] = entry
    data = {"schema": SCHEMA, "updated_at": _iso(now), "routes": routes, "lists": lists}
    try:
        _write(path, data)
    except OSError:
        pass
    return data


def due(lineup, cache, now=None, max_age=PROBE_EVERY_S) -> bool:
    now = now or datetime.now(timezone.utc)
    routes = (cache or {}).get("routes") or {}
    return any((_age(routes.get(key(p, m)), now) or max_age + 1) > max_age for p, m, _ in targets(lineup))


# ---------------------------------------------------------------- background
_LOCK = threading.Lock()
_THREAD = None


def running() -> bool:
    return _THREAD is not None and _THREAD.is_alive()


def start_background(lineup, creds_tree, http=None, path=None, force=False) -> bool:
    """One refresh at a time in a daemon thread; False when one is already running."""
    global _THREAD
    with _LOCK:
        if running():
            return False
        t = threading.Thread(target=lambda: refresh(lineup, creds_tree, http=http, path=path, force=force),
                             name="model-health", daemon=True)
        _THREAD = t
        t.start()
        return True


def wait_idle(timeout=30.0) -> bool:
    t = _THREAD
    if t is not None:
        t.join(timeout)
    return not running()


def _inputs(sources):
    from . import models
    return models.chat_lineup(sources), models._read_yaml(sources.credentials) or {}


def maybe_refresh(sources, http=None) -> bool:
    """Called by GET /api/models: starts a background refresh when something is due."""
    path = getattr(sources, "health", None)
    if not path or not getattr(sources, "auto_probe", False) or running():
        return False
    if http is None and (os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("CC_NO_MODEL_PROBE")):
        return False  # belt and braces: a test or an opted-out engine never reaches a real provider
    try:
        lineup, creds = _inputs(sources)
        if not due(lineup, load(path)):
            return False
        return start_background(lineup, creds, http=http, path=path)
    except Exception:
        return False


# ---------------------------------------------------------------- HTTP routes
def _sources(h):
    from . import models
    return getattr(getattr(h, "app", None), "models_sources", None) or models.default_sources()


def _http_error(status, body):
    from .api import HttpError
    return HttpError(status, body)


def g_health(h, q):
    src = _sources(h)
    cache = load(getattr(src, "health", None) or default_path())
    return {"routes": cache.get("routes") or {}, "lists": {p: {"checked_at": v.get("checked_at"), "error": v.get("error"),
                                                               "count": len(v.get("ids") or [])}
                                                           for p, v in (cache.get("lists") or {}).items()},
            "updated_at": cache.get("updated_at"), "running": running(), "probe_every_s": PROBE_EVERY_S}


def p_probe(h, q):
    if getattr(h, "actor", None) != "owner":
        raise _http_error(403, {"error": "owner_only", "detail": "only Owner may start a model check"})
    body = h._json_body() or {}
    src = _sources(h)
    lineup, creds = _inputs(src)
    http = getattr(getattr(h, "app", None), "model_health_http", None)
    started = start_background(lineup, creds, http=http, path=getattr(src, "health", None) or default_path(),
                               force=bool(body.get("force")))
    return {"ok": True, "started": started, "running": running()}


ROUTES = {
    "GET /api/model_health": g_health,
    "POST /api/model_health/probe": p_probe,
}
