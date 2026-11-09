"""CLI bridge: an OpenAI-compatible loopback server that answers through Owner's own CLIs.

dsh's main chat picker talks to this server (provider ``waypoint-cli`` in the generated patch), so
Claude runs through the ``claude`` CLI on the Max subscription and Codex through ``codex exec`` on
the ChatGPT plan, never through dsh's metered ``anthropic`` / ``openai-codex`` providers.

- ``GET /v1/models``: the Claude models while the Claude Code switch (Agents) is on, plus the
  Codex models while the Codex switch is on (off by default). Switches come from ``toggles.py``
  and the Models-tab file, exactly as the Agents page reads them.
- ``POST /v1/chat/completions``: the messages are flattened into one prompt (stdin), the CLI runs
  one non-interactive turn (``agents.claude_argv`` / ``agents.codex_argv``), and its stream is
  mapped to text by ``agents.ClaudeParser`` / ``agents.CodexParser``. ``stream: true`` returns
  SSE ``chat.completion.chunk`` lines ending ``data: [DONE]``; otherwise one ``chat.completion``.
- Errors are OpenAI-style JSON: unknown model 404, switched-off route 403, missing CLI 503,
  CLI failure 502, timeout 504. The child is killed on timeout and when the client goes away.
- Metered keys (``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY`` ...) are stripped from every child env,
  so the CLIs use their subscription logins. This module never calls a model API itself.
- Loopback only. Every request must name this bridge in its ``Host`` header (``127.0.0.1:<port>``,
  ``localhost:<port>`` or ``[::1]:<port>``; else 403 ``bad_host``, which stops DNS rebinding) and carry
  no browser ``Origin`` at all (403 ``forbidden_origin``). Every route except ``GET /health`` needs the
  engine's router token (``data/engine.token``) as ``X-CC-Token`` or ``Authorization: Bearer <token>``
  (else 401 ``invalid_api_key``), so a web page or another user cannot run the CLIs.
- Transition key, off by default: an older dsh ``waypoint-cli`` provider sends the fixed placeholder key
  in ``LEGACY_KEYS``. It is accepted (still only with no Origin and a good Host) only while the settings
  file says ``"cli_bridge_legacy_key": true`` (read fresh) or ``CC_CLI_BRIDGE_LEGACY_KEY=1``;
  ``CC_CLI_BRIDGE_LEGACY_KEY=0`` turns it off whatever the file says. While it is on, any local process
  (another Windows user included) that knows the documented key can run the CLIs on the owner's
  subscription, so start-up logs one warning line. Turn it off once dsh's ``WAYPOINT_CLI_API_KEY`` holds
  the engine token.

Engine startup calls ``start_background()`` (best effort, logged; ``CC_CLI_BRIDGE=0`` disables,
``CC_CLI_BRIDGE_PORT`` moves it). Standalone: ``python -m cc_engine.cli_bridge --port 8753``.
Every side effect is injectable through ``Bridge`` (``enabled``, ``which``, ``popen``, ``killer``,
``env``); tests never run a real CLI.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import queue
import select
import shutil
import socket
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import agents

DEFAULT_PORT = 8753
DEFAULT_TIMEOUT_S = 900.0
MAX_BODY_BYTES = 8 * 1024 * 1024
POLL_S = 0.5            # how often a waiting request checks the client and the clock
KEEPALIVE_S = 15.0      # SSE comment while the CLI is silent (also detects a closed client)

CLAUDE_MODELS = (("claude-opus-5-5", "Claude Opus 5.5"),
                 ("claude-sonnet-5-5", "Claude Sonnet 5.5"),
                 ("claude-haiku-5-5", "Claude Haiku 5.5"))
CODEX_MODELS = (("gpt-6.1-sol", "GPT-6.1 Sol (Codex)"),
                ("gpt-6-sol", "GPT-6 Sol (Codex)"))
CLAUDE_MODEL_IDS = tuple(m for m, _ in CLAUDE_MODELS)
CODEX_MODEL_IDS = tuple(m for m, _ in CODEX_MODELS)
ROUTES = {"claude_code": {"models": CLAUDE_MODELS, "owner": "anthropic", "name": "Claude Code"},
          "codex": {"models": CODEX_MODELS, "owner": "openai", "name": "Codex"}}
ROUTE_DEFAULTS = {"claude_code": True, "codex": False}  # used only when the switches cannot be read

# Never reach a child: these would turn a subscription CLI into metered API spend.
STRIP_ENV = tuple(dict.fromkeys(agents.METERED_ENV + (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "OPENAI_API_KEY",
    "OPENAI_BASE_URL", "OPENAI_API_BASE", "OPENAI_ORG_ID", "CODEX_API_KEY", "AZURE_OPENAI_API_KEY")))

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")
HOST_NAMES = ("127.0.0.1", "localhost", "[::1]")  # as they appear in a Host header
TOKEN_HEADER = "X-CC-Token"
LEGACY_KEYS = ("waypoint-cli-local",)  # dsh's placeholder WAYPOINT_CLI_API_KEY; see the module docstring
OPEN_PATHS = ("/health",)


def default_token():
    """The engine's router token (``CC_TOKEN_FILE`` or ``data/engine.token``), read on each request."""
    from .api import default_token_path, read_token
    return read_token(default_token_path())


def legacy_keys_allowed():
    """The fixed transition key is accepted: env ``CC_CLI_BRIDGE_LEGACY_KEY`` (1/0) when set, else the
    settings file's ``cli_bridge_legacy_key`` (must be a real ``true``). Off by default."""
    raw = str(os.environ.get("CC_CLI_BRIDGE_LEGACY_KEY", "")).strip().lower()
    if raw:
        return raw in ("1", "true", "on", "yes")
    from . import config
    return config.cli_bridge_legacy_key()


def _warn_if_legacy(bridge):
    """One start-up warning line while the fixed transition key is accepted."""
    legacy = legacy_keys_allowed() if bridge._legacy is None else bool(bridge._legacy)
    if legacy:
        _log(f"warning: the fixed key {LEGACY_KEYS[0]!r} is accepted (settings cli_bridge_legacy_key or "
             "CC_CLI_BRIDGE_LEGACY_KEY=1); any local process can run the CLIs. Turn it off once dsh sends "
             "the engine token.")


def _log(msg):
    print(f"cli-bridge: {msg}", file=sys.stderr, flush=True)


class BridgeError(Exception):
    def __init__(self, status, code, message, etype="invalid_request_error"):
        super().__init__(message)
        self.status, self.code, self.message, self.etype = status, code, message, etype

    def body(self):
        return {"error": {"message": self.message, "type": self.etype, "code": self.code}}


# ---------------------------------------------------------------- prompt
def _content_text(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict) and isinstance(p.get("text"), str):
                parts.append(p["text"])
            elif isinstance(p, dict) and p.get("type") in ("image_url", "image", "input_image"):
                parts.append("[image omitted]")
        return "\n".join(x for x in parts if x)
    return str(content)


def flatten_messages(messages):
    """One prompt for a CLI turn. A lone user message passes through unchanged."""
    msgs = [m for m in (messages or []) if isinstance(m, dict)]
    if len(msgs) == 1 and msgs[0].get("role") == "user":
        return _content_text(msgs[0].get("content"))
    system, convo = [], []
    for m in msgs:
        role = str(m.get("role") or "user")
        text = _content_text(m.get("content"))
        if role in ("system", "developer"):
            if text:
                system.append(text)
            continue
        if role == "assistant":
            calls = m.get("tool_calls") or []
            names = [((c or {}).get("function") or {}).get("name") for c in calls if isinstance(c, dict)]
            if names:
                text = (text + "\n" if text else "") + "[called tools: " + ", ".join(n for n in names if n) + "]"
            label = "Assistant"
        elif role in ("tool", "function"):
            label = "Tool result"
        else:
            label = "User"
        if text:
            convo.append(f"{label}:\n{text}")
    out = []
    if system:
        out.append("<instructions>\n" + "\n\n".join(system) + "\n</instructions>")
    if convo:
        if len(convo) > 1:
            out.append("Conversation so far (reply as the Assistant to the last User message):")
        out.append("\n\n".join(convo))
    return "\n\n".join(out)


# ---------------------------------------------------------------- bridge
class Bridge:
    def __init__(self, enabled=None, which=None, popen=None, killer=None, env=None,
                 timeout=DEFAULT_TIMEOUT_S, cwd="default", toggles_db=None, token=None, legacy_keys=None):
        self._enabled = enabled
        # str or callable -> str: the router token every request must present (default: data/engine.token)
        self._token = token if token is not None else default_token
        self._legacy = legacy_keys  # None = follow legacy_keys_allowed() (env, then settings file)
        self.which = which or shutil.which
        self.popen = popen or agents.default_popen
        self.killer = killer or agents.dispatch.kill_tree
        self.env = env
        self.timeout = float(timeout)
        self.cwd = agents.FOLDERS[0]["path"] if cwd == "default" else cwd
        if self.cwd and not os.path.isdir(self.cwd):
            self.cwd = None
        self.toggles_db = toggles_db

    # -- credential
    def token(self):
        try:
            tok = self._token() if callable(self._token) else self._token
        except Exception:
            return None
        return str(tok).strip() if tok else None

    def key_ok(self, presented):
        """True when ``presented`` is the router token (or, during the transition, a legacy key)."""
        if not presented:
            return False
        got = presented.strip().encode("utf-8")
        tok = self.token()
        if tok and len(tok) >= 16 and hmac.compare_digest(got, tok.encode("utf-8")):
            return True
        legacy = legacy_keys_allowed() if self._legacy is None else bool(self._legacy)
        return legacy and any(hmac.compare_digest(got, k.encode("utf-8")) for k in LEGACY_KEYS)

    # -- switches and models
    def route_enabled(self, route):
        if self._enabled is not None:
            try:
                return bool(self._enabled(route))
            except Exception:
                return ROUTE_DEFAULTS.get(route, False)
        try:
            from . import toggles
            on = toggles.is_enabled(route, db=self.toggles_db)
        except Exception:
            on = ROUTE_DEFAULTS.get(route, False)
        if not on:
            return False
        try:  # the Models-tab file the Agents page also honours
            from . import models
            return not models.is_disabled(route=route)
        except Exception:
            return True

    def models(self):
        now = int(time.time())
        out = []
        for route, info in ROUTES.items():
            if not self.route_enabled(route):
                continue
            for mid, name in info["models"]:
                out.append({"id": mid, "object": "model", "created": now, "owned_by": info["owner"],
                            "name": name, "route": route})
        return out

    @staticmethod
    def route_for(model):
        m = str(model or "").strip()
        if m in CLAUDE_MODEL_IDS or agents.claude_model_id(m) in CLAUDE_MODEL_IDS:
            return "claude_code"
        if m in CODEX_MODEL_IDS:
            return "codex"
        return None

    def prepare(self, model, effort=None):
        """``(route, argv, parser, env)`` or a ``BridgeError``; nothing is started here."""
        route = self.route_for(model)
        if route is None:
            raise BridgeError(404, "model_not_found", f"The model {model!r} is not offered by the CLI bridge.")
        name = ROUTES[route]["name"]
        if not self.route_enabled(route):
            raise BridgeError(403, "route_disabled", f"{name} is turned off in Agents.", "permission_error")
        exe_name = agents.AGENTS[route]["exe"]
        exe = self.which(exe_name)
        if not exe:
            raise BridgeError(503, "cli_unavailable", f"The `{exe_name}` CLI was not found on PATH.",
                              "service_unavailable")
        effort = str(effort or "").strip().lower() or None
        if effort not in agents.AGENT_EFFORTS.get(route, ()):
            effort = None
        session = {"ext_id": None, "_new_ext_id": str(uuid.uuid4())}
        base = dict(os.environ if self.env is None else self.env)
        if route == "claude_code":
            argv = agents.claude_argv(exe, session, agents.claude_model_id(model), effort)
            parser = agents.ClaudeParser()
            env = agents.child_env(base, drop=STRIP_ENV)
        else:
            argv = agents.codex_argv(exe, session, model, effort)
            parser = agents.CodexParser()
            env = agents.child_env(base, drop=STRIP_ENV + ("CODEX_HOME",))
        return route, argv, parser, env

    def start(self, argv, env, prompt):
        return self.popen(argv, self.cwd, env, prompt)

    def kill(self, proc):
        try:
            self.killer(proc.pid)
        except Exception as exc:  # already gone
            _log(f"kill {getattr(proc, 'pid', '?')}: {type(exc).__name__}")


def _pump(proc, parser, q):
    """Reader thread: parser events from the child's stdout, then ``("exit", code)``."""
    try:
        for line in proc.stdout:
            for ev in parser.feed(line):
                q.put(ev)
    except Exception as exc:
        q.put(("error", f"{type(exc).__name__}: {exc}"))
    try:
        code = proc.wait()
    except Exception:
        code = None
    q.put(("exit", code))


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "cc-cli-bridge/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet console
        pass

    @property
    def bridge(self) -> Bridge:
        return self.server.bridge

    # -- helpers
    def _json(self, status, obj, close=False):
        data = json.dumps(obj).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            if close:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            self.wfile.write(data)
        except OSError:  # the client already went away
            self.close_connection = True

    def _error(self, err: BridgeError):
        # Close after an error: the request body may be unread and must not parse as a next request.
        self._json(err.status, err.body(), close=True)

    def _origin_ok(self):
        """No browser may call the bridge: any Origin header (even a loopback one or "null") is refused."""
        return self.headers.get("Origin") is None

    def _host_ok(self):
        host = (self.headers.get("Host") or "").strip().lower()
        try:
            port = int(self.server.server_address[1])
        except (AttributeError, IndexError, TypeError, ValueError):
            return False
        return host in {f"{name}:{port}" for name in HOST_NAMES}

    def _presented_key(self):
        tok = self.headers.get(TOKEN_HEADER)
        if tok:
            return tok
        auth = self.headers.get("Authorization") or ""
        scheme, _, value = auth.strip().partition(" ")
        return value.strip() if scheme.lower() == "bearer" else None

    def _admit(self):
        """Host, Origin and credential checks; sends the refusal and returns False when one fails."""
        if not self._host_ok():
            self._error(BridgeError(403, "bad_host", "The Host header must name this loopback bridge.",
                                    "permission_error"))
            return False
        if not self._origin_ok():
            self._error(BridgeError(403, "forbidden_origin", "Browser origins are not allowed.", "permission_error"))
            return False
        if self._path() in OPEN_PATHS and self.command == "GET":
            return True
        if not self.bridge.key_ok(self._presented_key()):
            self._error(BridgeError(401, "invalid_api_key", "Send the engine token as X-CC-Token or "
                                    "Authorization: Bearer <token>.", "authentication_error"))
            return False
        return True

    def _client_gone(self):
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            if not readable:
                return False
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def _path(self):
        p = urlparse(self.path).path.rstrip("/")
        return p[3:] if p.startswith("/v1") else p

    # -- routes
    def do_GET(self):
        if not self._admit():
            return
        p = self._path()
        if p == "/models":
            return self._json(200, {"object": "list", "data": self.bridge.models()})
        if p in ("", "/health"):
            return self._json(200, {"ok": True, "service": "cli-bridge"})
        self._error(BridgeError(404, "not_found", f"No route {self.path}."))

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        too_large = n < 0 or n > MAX_BODY_BYTES
        # Read the body before any refusal: closing with unread bytes resets the socket on Windows,
        # and the caller would see a connection error instead of the 4xx answer.
        raw = b"" if too_large else self.rfile.read(n)
        if not self._admit():
            return
        if self._path() != "/chat/completions":
            return self._error(BridgeError(404, "not_found", f"No route {self.path}."))
        if too_large:
            return self._error(BridgeError(413, "too_large", "Request body too large."))
        try:
            body = json.loads(raw.decode("utf-8") or "null")
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
        except (ValueError, UnicodeDecodeError) as exc:
            return self._error(BridgeError(400, "invalid_json", f"Invalid JSON body: {exc}"))
        model = str(body.get("model") or "").strip()
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            return self._error(BridgeError(400, "invalid_messages", "messages must be a non-empty list."))
        prompt = flatten_messages(messages)
        if not prompt.strip():
            return self._error(BridgeError(400, "empty_prompt", "The messages contain no text."))
        try:
            route, argv, parser, env = self.bridge.prepare(model, body.get("reasoning_effort"))
        except BridgeError as err:
            return self._error(err)
        stream = bool(body.get("stream"))
        include_usage = bool((body.get("stream_options") or {}).get("include_usage")) if stream else False
        try:
            proc = self.bridge.start(argv, env, prompt)
        except Exception as exc:
            return self._error(BridgeError(503, "cli_unavailable", f"Could not start the CLI: {exc}",
                                           "service_unavailable"))
        q = queue.Queue()
        threading.Thread(target=_pump, args=(proc, parser, q), daemon=True, name="cli-bridge-pump").start()
        cid = "chatcmpl-" + uuid.uuid4().hex[:24]
        if stream:
            self._run_stream(proc, q, cid, model, include_usage)
        else:
            self._run_once(proc, q, cid, model)

    def _events(self, proc, q, on_idle=None):
        """Yield parser events until the child exits; kills it on timeout or a vanished client.

        Ends with ``("exit", code)``, ``("timeout", None)`` or ``("gone", None)``.
        """
        deadline = time.monotonic() + self.bridge.timeout
        last = time.monotonic()
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                self.bridge.kill(proc)
                yield ("timeout", None)
                return
            try:
                ev = q.get(timeout=min(POLL_S, left))
            except queue.Empty:
                if self._client_gone():
                    self.bridge.kill(proc)
                    yield ("gone", None)
                    return
                if on_idle is not None and time.monotonic() - last >= KEEPALIVE_S:
                    last = time.monotonic()
                    if not on_idle():
                        self.bridge.kill(proc)
                        yield ("gone", None)
                        return
                continue
            last = time.monotonic()
            yield ev
            if ev[0] == "exit":
                return

    @staticmethod
    def _result(state, code):
        """``(text, error)`` once the child has exited."""
        text = "".join(state["text"]) or (state["final"] or "")
        err = state["error"]
        if not err and code not in (0, None) and not text:
            err = f"the CLI exited with code {code}"
        return text, err

    def _run_once(self, proc, q, cid, model):
        state = {"text": [], "final": None, "error": None}
        end = None
        for kind, value in self._events(proc, q):
            if kind == "delta":
                state["text"].append(value)
            elif kind == "final":
                state["final"] = value
            elif kind == "error":
                state["error"] = value
            elif kind in ("exit", "timeout", "gone"):
                end = (kind, value)
        kind, code = end or ("exit", None)
        if kind == "gone":
            return
        if kind == "timeout":
            return self._error(BridgeError(504, "timeout", f"The CLI did not finish within {self.bridge.timeout:g}s.",
                                           "timeout_error"))
        text, err = self._result(state, code)
        if err and not text:
            return self._error(BridgeError(502, "cli_error", str(err), "api_error"))
        self._json(200, {
            "id": cid, "object": "chat.completion", "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })

    def _run_stream(self, proc, q, cid, model, include_usage):
        created = int(time.time())

        def chunk(delta=None, finish=None, extra=None):
            obj = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                   "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
            if extra:
                obj.update(extra)
            return obj

        def send(raw):
            try:
                self.wfile.write(raw)
                self.wfile.flush()
                return True
            except (OSError, ValueError):
                return False

        def data(obj):
            return send(b"data: " + json.dumps(obj).encode("utf-8") + b"\n\n")

        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
        except OSError:
            return self.bridge.kill(proc)
        if not data(chunk({"role": "assistant", "content": ""})):
            return self.bridge.kill(proc)
        state = {"text": [], "final": None, "error": None}
        end = ("exit", None)
        for kind, value in self._events(proc, q, on_idle=lambda: send(b": keepalive\n\n")):
            if kind == "delta":
                state["text"].append(value)
                if not data(chunk({"content": value})):
                    self.bridge.kill(proc)
                    return
            elif kind == "final":
                state["final"] = value
            elif kind == "error":
                state["error"] = value
            elif kind in ("exit", "timeout", "gone"):
                end = (kind, value)
        kind, code = end
        if kind == "gone":
            return
        if kind == "timeout":
            data({"error": {"message": f"The CLI did not finish within {self.bridge.timeout:g}s.",
                            "type": "timeout_error", "code": "timeout"}})
            send(b"data: [DONE]\n\n")
            return
        streamed = bool(state["text"])
        text, err = self._result(state, code)
        if text and not streamed:  # nothing arrived as deltas: send the result text once
            data(chunk({"content": text}))
        if err and not text:
            data({"error": {"message": str(err), "type": "api_error", "code": "cli_error"}})
        else:
            data(chunk({}, "stop"))
            if include_usage:
                data({"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                      "choices": [], "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})
        send(b"data: [DONE]\n\n")


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # on Windows SO_REUSEADDR would let two bridges share a port

    def __init__(self, addr, bridge):
        self.bridge = bridge
        super().__init__(addr, Handler)


def make_server(port=DEFAULT_PORT, host="127.0.0.1", bridge=None, **bridge_kwargs) -> BridgeServer:
    if host not in LOOPBACK_HOSTS:
        raise ValueError("the CLI bridge binds loopback only")
    return BridgeServer((host, int(port)), bridge or Bridge(**bridge_kwargs))


def serve(port=DEFAULT_PORT, host="127.0.0.1", bridge=None, **bridge_kwargs):
    """Serve in the foreground until interrupted."""
    srv = make_server(port, host, bridge, **bridge_kwargs)
    _log(f"listening on http://{host}:{srv.server_address[1]}/v1")
    _warn_if_legacy(srv.bridge)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


def start_background(port=None, host="127.0.0.1", **bridge_kwargs):
    """Engine startup hook: serve in a daemon thread. Best effort; returns the server or None."""
    if str(os.environ.get("CC_CLI_BRIDGE", "1")).strip().lower() in ("0", "false", "off", "no"):
        _log("disabled by CC_CLI_BRIDGE")
        return None
    try:
        port = int(port if port is not None else os.environ.get("CC_CLI_BRIDGE_PORT") or DEFAULT_PORT)
        srv = make_server(port, host, **bridge_kwargs)
    except Exception as exc:
        _log(f"not started on port {port}: {type(exc).__name__}: {exc}")
        return None
    threading.Thread(target=srv.serve_forever, daemon=True, name="cli-bridge").start()
    _log(f"listening on http://{host}:{srv.server_address[1]}/v1")
    _warn_if_legacy(srv.bridge)
    return srv


def parse_args(argv=None):
    ap = argparse.ArgumentParser(prog="python -m cc_engine.cli_bridge",
                                 description="OpenAI-compatible loopback bridge to the claude / codex CLIs.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    ap.add_argument("--cwd", default="default", help="working folder for the CLIs (default: Drive)")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.host not in LOOPBACK_HOSTS:
        print("refusing to bind a non-loopback address", file=sys.stderr)
        return 2
    serve(args.port, args.host, timeout=args.timeout, cwd=args.cwd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
