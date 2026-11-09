"""Adapter: free pools (Groq, Gemini key pool, local Ollama), each toggled and floored on its own.

One ``FreePools`` engine holds the three backends; ``FreePoolAdapter`` exposes one backend to the dispatcher
(``register_free_pools`` registers all three). Every call asks ``policy.allowed`` for that backend's route first,
so a switched-off backend is never called and a set floor is honoured. No floor value lives here.

Outcome: ``PoolResult`` is either ``ok`` with the reply, or a classified failure (``FAILURES``). A rate limit moves
to the next Gemini key, then to the next free backend in the order; nothing here can reach a paid route.

Keys: read from the existing stores only (environment, user registry; Gemini via the engine's ``gemini_keys``
pool reader). They are held in memory, sent in a header, redacted from every error and never written to disk.
Python touches the data folder only through core/waypoint-data/py.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .. import dispatch_core as dc  # first: puts core/waypoint-data/py on sys.path
from .. import policy
from waypoint_data import io as wio  # noqa: E402
from waypoint_data.store import serialize  # noqa: E402

BACKENDS = ("groq", "gemini", "ollama")
FAILURES = ("disabled", "rate_limit", "no_key", "auth", "unreachable", "timeout", "bad_reply", "error")
DEFAULT_MODELS = {"groq": "openai/gpt-oss-120b", "gemini": "gemini-3.8-flash",
                  "ollama": "huihui_ai/qwen3.5-abliterated:4B-q4_K"}
TIMEOUTS = {"groq": 120, "gemini": 300, "ollama": 600}
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
_SECRET = re.compile(r"(AIza[0-9A-Za-z_\-]{20,}|gsk_[0-9A-Za-z]{16,}|key=[^&\s]+|Bearer\s+\S+)")


def redact(text, secrets_=()) -> str:
    out = str(text)
    for s in secrets_:
        if s:
            out = out.replace(s, "[key]")
    return _SECRET.sub("[key]", out)


@dataclass
class Attempt:
    backend: str
    ok: bool
    failure: str | None = None
    detail: str = ""
    key_slot: str | None = None


@dataclass
class PoolResult:
    ok: bool
    backend: str | None = None
    text: str = ""
    failure: str | None = None  # classified failure of the last attempt when not ok
    attempts: list = field(default_factory=list)

    def as_dict(self):
        return {"schema": "waypoint.free_result.v1", "ok": self.ok, "backend": self.backend, "text": self.text,
                "failure": self.failure,
                "attempts": [{"backend": a.backend, "ok": a.ok, "failure": a.failure, "detail": a.detail,
                              "key_slot": a.key_slot} for a in self.attempts]}


class Transport:
    """The only network code; tests replace it. ``post`` returns (status, payload) and raises OSError/TimeoutError."""

    def post(self, url, body, headers=None, timeout=120):
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers=dict({"Content-Type": "application/json"}, **(headers or {})))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw, status = resp.read(), resp.status
        except urllib.error.HTTPError as exc:
            raw, status = exc.read(), exc.code
        try:
            return status, json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            return status, {"raw": raw[:300].decode("utf-8", errors="replace")}


class KeyStores:
    """Existing key stores, read-only. ``gemini()`` -> [(slot, key)] in rotation order; ``groq()`` -> key or None."""

    def gemini(self):
        engine = str(dc._REPO / "engine")  # noqa: SLF001
        if engine not in sys.path:
            sys.path.append(engine)
        try:
            from cc_engine import gemini_keys  # noqa: PLC0415
            return [(slot, value) for slot, _name, value, _origin in gemini_keys._pool()]  # noqa: SLF001
        except Exception:  # noqa: BLE001 - no store means no key, never a crash
            return []

    def groq(self):
        val = (os.environ.get("GROQ_API_KEY") or "").strip()
        if val:
            return val
        try:
            import winreg  # noqa: PLC0415
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
                return str(winreg.QueryValueEx(key, "GROQ_API_KEY")[0]).strip() or None
        except Exception:  # noqa: BLE001
            return None


def classify_status(status):
    if status == 429:
        return "rate_limit"
    if status in (401, 403):
        return "auth"
    if status in (408, 504):
        return "timeout"
    return "error"


class FreePools:
    """``root`` is the Waypoint data folder (toggles, floors, usage). ``order`` is the backend fallback order."""

    def __init__(self, root, transport=None, keys=None, order=BACKENDS, models=None, now=None):
        self.root = Path(root)
        self.transport = transport or Transport()
        self.keys = keys or KeyStores()
        self.order = tuple(b for b in order if b in BACKENDS)
        self.models = dict(DEFAULT_MODELS, **(models or {}))
        self.now = now

    def decision(self, backend):
        now = datetime.fromtimestamp(self.now(), timezone.utc) if self.now else None
        return policy.allowed(self.root, backend, now)

    def run(self, prompt, backends=None, model=None, on_attempt=None) -> PoolResult:
        """Try ``backends`` (default: the configured order) until one answers. ``model`` applies only when exactly
        one backend is named; otherwise each backend uses its own default."""
        names = [b for b in (backends or self.order) if b in BACKENDS]
        if not names:
            return PoolResult(False, failure="error", attempts=[Attempt("-", False, "error", "no backend")])
        attempts, last = [], "error"
        for b in names:
            if on_attempt:
                on_attempt(b)
            d = self.decision(b)
            if not d.allowed:
                attempts.append(Attempt(b, False, "disabled", d.reason))
                last = "disabled"
                continue
            text, made = getattr(self, f"_{b}")(prompt, (model if len(names) == 1 else None) or self.models[b])
            attempts.extend(made)
            if text is not None:
                return PoolResult(True, b, text, None, attempts)
            last = attempts[-1].failure or "error"
        return PoolResult(False, None, "", last, attempts)

    # -- backends return (text or None, [Attempt, ...])
    def _guard(self, backend, slot, secrets_, fn):
        try:
            return fn(), None
        except TimeoutError as exc:
            return None, Attempt(backend, False, "timeout", redact(exc, secrets_), slot)
        except OSError as exc:
            return None, Attempt(backend, False, "unreachable", redact(exc, secrets_), slot)

    def _groq(self, prompt, model):
        key = self.keys.groq()
        if not key:
            return None, [Attempt("groq", False, "no_key", "GROQ_API_KEY not found in the key stores")]
        body = {"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False}
        out, bad = self._guard("groq", None, [key], lambda: self.transport.post(
            GROQ_URL, body, {"Authorization": f"Bearer {key}"}, TIMEOUTS["groq"]))
        if bad:
            return None, [bad]
        return openai_reply("groq", out, [key])

    def _gemini(self, prompt, model):
        pool = self.keys.gemini()
        if not pool:
            return None, [Attempt("gemini", False, "no_key", "no Gemini key in the key stores")]
        attempts = []
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
        for slot, key in pool:  # rate limit or a dead key: next key in the pool
            out, bad = self._guard("gemini", slot, [key], lambda k=key: self.transport.post(
                GEMINI_URL.format(model=model), body, {"x-goog-api-key": k}, TIMEOUTS["gemini"]))
            if bad:
                attempts.append(bad)
                if bad.failure == "unreachable":
                    break  # same host for every key
                continue
            status, payload = out
            if status != 200:
                attempts.append(Attempt("gemini", False, classify_status(status),
                                        redact(f"HTTP {status}: {str(payload)[:200]}", [key]), slot))
                continue
            try:
                text = "".join(p.get("text", "") for p in payload["candidates"][0]["content"]["parts"])
            except (KeyError, IndexError, TypeError, AttributeError):
                attempts.append(Attempt("gemini", False, "bad_reply", "unexpected reply shape", slot))
                continue
            if not text.strip():
                attempts.append(Attempt("gemini", False, "bad_reply", "empty reply", slot))
                continue
            attempts.append(Attempt("gemini", True, None, "", slot))
            return text, attempts
        return None, attempts

    def _ollama(self, prompt, model):
        body = {"model": model, "prompt": prompt, "stream": False}
        out, bad = self._guard("ollama", None, [], lambda: self.transport.post(
            OLLAMA_URL, body, None, TIMEOUTS["ollama"]))
        if bad:
            return None, [bad]
        status, payload = out
        if status != 200:
            return None, [Attempt("ollama", False, classify_status(status), f"HTTP {status}")]
        text = payload.get("response") if isinstance(payload, dict) else None
        if not isinstance(text, str) or not text.strip():
            return None, [Attempt("ollama", False, "bad_reply", "empty reply")]
        return text, [Attempt("ollama", True)]


def openai_reply(backend, out, secrets_=(), slot=None):
    """Parse an OpenAI-compatible chat reply into (text or None, [Attempt])."""
    status, payload = out
    if status != 200:
        return None, [Attempt(backend, False, classify_status(status),
                              redact(f"HTTP {status}: {str(payload)[:200]}", secrets_), slot)]
    try:
        text = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None, [Attempt(backend, False, "bad_reply", "unexpected reply shape", slot)]
    if not isinstance(text, str) or not text.strip():
        return None, [Attempt(backend, False, "bad_reply", "empty reply", slot)]
    return text, [Attempt(backend, True, None, "", slot)]


# ---------------------------------------------------------------- dispatcher adapter

class FreePoolAdapter(dc.Adapter):
    """One free backend for the dispatcher. ``route`` is the backend name that policy decides on. The call runs on
    a thread (heartbeat written before each attempt); the outcome lands in ``result.json`` and, when it worked,
    ``reply.md`` in the job folder. ``fallbacks`` are other free backends tried after a failure such as a rate
    limit; each is still checked against its own toggle and floor, and none is a paid route."""

    def __init__(self, backend, pools, fallbacks=None, inline=False):
        if backend not in BACKENDS:
            raise ValueError(f"unknown free backend {backend!r}")
        self.backend, self.pools, self.inline = backend, pools, inline
        self.name, self.route = f"free_{backend}", backend
        self.priority = BACKENDS.index(backend)
        self.fallbacks = tuple(f for f in (fallbacks or ()) if f in BACKENDS and f != backend)

    def start(self, spec, job_dir):
        job_dir = Path(job_dir)
        prompt = spec.get("brief") or ""
        if not str(prompt).strip():
            raise ValueError("job spec has no brief")

        def work():
            res = self.pools.run(prompt, [self.backend, *self.fallbacks],
                                 on_attempt=lambda _b: dc.write_heartbeat(job_dir))
            if res.ok:
                wio.atomic_write(str(job_dir / "reply.md"), res.text, writer=dc.WRITER)
            wio.atomic_write(str(job_dir / "result.json"), serialize(res.as_dict()), writer=dc.WRITER)

        if self.inline:
            work()
        else:
            threading.Thread(target=work, daemon=True, name=f"free-{self.backend}").start()
        return {"pid": None, "backend": self.backend}

    def poll(self, job, job_dir):
        res = wio.read_json(str(Path(job_dir) / "result.json"))
        if not isinstance(res, dict):
            return {"state": "running"}
        if res.get("ok"):
            return {"state": "finished", "exit_code": 0}
        return {"state": "failed", "reason": res.get("failure") or "error", "exit_code": 1}

    def stop(self, job, job_dir):
        pass  # a thread call cannot be killed; the job is marked stopped


def register_free_pools(registry, pools, fallback=True, inline=False):
    """Register the three backends, each with its own toggle and floor."""
    out = []
    for b in BACKENDS:
        others = [x for x in pools.order if x != b] if fallback else []
        out.append(registry.register(FreePoolAdapter(b, pools, others, inline)))
    return out
