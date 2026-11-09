"""free_runners MCP server: lets a strong model hand work to the free runners without spending its own usage.

Stdio JSON-RPC (newline-delimited, stdlib only), the same protocol style as ``mcp_server.py``. Exactly three
short tools, each a thin wrapper over code that already exists:

- ``run_freebuff``       Freebuff local gateway (``providers.freebuff`` request, posted in-process).
- ``run_gemini_studio``  ``gemini_subagent.py`` (Google AI Studio free key pool).
- ``run_groq``           ``groq_subagent.py`` (Groq LPU).

There is no runner argument and no way to reach a metered or agentic tool from here: the tool list is the
whole surface. A runner or model that Owner switched off on the Models tab is refused. Output is capped at
4,000 characters; longer replies are saved in full to a log file whose path is appended.

Run: ``python -u -m cc_engine.free_runners_mcp`` (the registration script sets PYTHONPATH to ``engine/``).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from . import providers
from . import config

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18", "2026-07-28"}
TOOLS_DIR = Path(config.TOOLS_DIR)
PINNED_PYTHON = config.PYTHON
DEFAULT_CWD = config.WORKSPACE_DIR
MAX_CHARS = 4000
# Freebuff models that need a paid plan (freebuff_api_client.PAID_MODELS): zero extra spend, so refused.
PAID_FREEBUFF = ("google/gemini-3.8-flash", "google/gemini-4-argon", "openai/gpt-6.1-sol")
ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

_PROPS = {
    "prompt": {"type": "string", "description": "Self-contained task brief."},
    "model": {"type": "string", "description": "Optional model id; the runner's default when omitted."},
    "file_path": {"type": "string", "description": "Optional context file."},
    "output_path": {"type": "string", "description": "Optional file to save the reply to."},
}


def _schema(**extra):
    return {"type": "object", "properties": dict(_PROPS, **extra), "required": ["prompt"]}


TOOLS = [
    {"name": "run_freebuff", "description": "Free Freebuff models (DeepSeek V4.1 Flash, GPT-6 Luna) for quick reviews and drafts.",
     "inputSchema": _schema()},
    {"name": "run_gemini_studio", "description": "Gemini on the free Google AI Studio key pool for large-context reading.",
     "inputSchema": _schema()},
    {"name": "run_groq", "description": "Groq LPU for fast syntax checks, small code and analysis.",
     "inputSchema": _schema()},
]
RUNNERS = {"run_freebuff": "freebuff", "run_gemini_studio": "gemini", "run_groq": "groq"}
TIMEOUTS = {"freebuff": 180, "gemini": 300, "groq": 120}


class Backends:
    """Everything with side effects; tests replace ``run_cmd`` and ``http_post``."""

    def __init__(self, spill_dir=None):
        self.spill_dir = Path(spill_dir or os.environ.get("FREE_RUNNERS_SPILL_DIR")
                              or Path.home() / ".dsh" / "free_runners")

    def run_cmd(self, argv, stdin=None, cwd=None, timeout=600):
        proc = subprocess.run(list(argv), input=stdin, cwd=cwd or DEFAULT_CWD, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, creationflags=CREATE_NO_WINDOW)
        out, err = (proc.stdout or "").strip(), (proc.stderr or "").strip()
        if proc.returncode != 0 and not out:
            raise RuntimeError(f"exit code {proc.returncode}: {err[-600:]}")
        return out or err or "(completed with empty output)"

    def http_post(self, url, body, timeout=180):
        return providers.post_json(url, body, timeout=timeout)


def _python() -> str:
    return PINNED_PYTHON if Path(PINNED_PYTHON).is_file() else sys.executable


# Ids the single enable/disable switch (toggles.is_enabled) may know a runner by.
TOGGLE_IDS = {"freebuff": ("freebuff", "route:freebuff", "runner:freebuff"),
              "gemini": ("gemini", "gemini_studio", "route:gemini", "runner:gemini"),
              "groq": ("groq", "route:groq", "runner:groq")}


def _toggle_reason(runner):
    """Refusal when the runner's route is switched off in toggles.py; allowed when that module is absent."""
    try:
        from . import toggles
    except Exception:
        return None
    for ident in TOGGLE_IDS.get(runner, (runner,)):
        try:
            if toggles.is_enabled(ident) is False:
                return f"Runner '{runner}' is switched off ({ident}); turn it on in Waypoint first."
        except Exception:  # an unknown id or a broken module never blocks a call
            continue
    return None


def _disabled_reason(runner, model):
    reason = _toggle_reason(runner)
    if reason:
        return reason
    try:
        from . import models
        if models.is_disabled(runner=runner, model=model or None):
            what = f"Model '{model}' on runner '{runner}'" if model else f"Runner '{runner}'"
            return f"{what} is turned off on the Meridian Models tab."
    except Exception:  # a missing or broken registry must never break a call
        return None
    return None


def _cap(text, spill_dir, label):
    cleaned = ANSI_RE.sub("", text or "").strip() or "(completed with empty output)"
    if len(cleaned) <= MAX_CHARS:
        return cleaned
    spill_dir.mkdir(parents=True, exist_ok=True)
    path = spill_dir / f"{label}_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.log"
    path.write_text(cleaned, encoding="utf-8")
    head, tail = cleaned[: int(MAX_CHARS * 0.65)], cleaned[-int(MAX_CHARS * 0.25):]
    return f"{head}\n\n...[{len(cleaned) - len(head) - len(tail)} characters omitted; full reply saved to {path}]...\n\n{tail}"


def _save(path, text):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _freebuff(be, args, prompt):
    model = str(args.get("model") or "").strip() or providers.FREEBUFF_DEFAULT_MODEL
    if model in PAID_FREEBUFF:
        raise ValueError(f"Model '{model}' needs a paid Freebuff plan; zero extra spend, so it is not used.")
    launch = providers.freebuff(prompt=providers.prompt_with_file(prompt, args.get("file_path")), model=model)
    try:
        status, payload = be.http_post(launch.url, launch.body, timeout=TIMEOUTS["freebuff"])
    except OSError as exc:
        raise RuntimeError("Freebuff gateway not reachable at 127.0.0.1:1455 "
                           f"(start it with launch_freebuff.ps1): {exc}") from exc
    if status != 200:
        detail = (payload or {}).get("error") if isinstance(payload, dict) else payload
        raise RuntimeError(f"Freebuff returned {status}: {str(detail)[:300]}")
    try:
        reply = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("Freebuff returned an unexpected reply shape.") from exc
    if args.get("output_path"):
        _save(args["output_path"], reply)
    return reply


def _script_runner(script, key):
    def run(be, args, prompt):
        argv = [_python(), str(TOOLS_DIR / script), "--prompt", prompt]
        if args.get("file_path"):
            argv += ["--file", str(args["file_path"])]
        if args.get("model"):
            argv += ["--model", str(args["model"])]
        if args.get("output_path"):
            argv += ["--output", str(args["output_path"])]
        out = be.run_cmd(argv, timeout=TIMEOUTS[key])
        if args.get("output_path") and out == "(completed with empty output)":
            return f"Saved to {args['output_path']}."
        return out
    return run


IMPL = {"freebuff": _freebuff,
        "groq": _script_runner("groq_subagent.py", "groq"),
        "gemini": _script_runner("gemini_subagent.py", "gemini")}


def call_tool(name, args, backends=None):
    runner = RUNNERS.get(name)
    if runner is None:
        raise ValueError(f"Unknown tool: {name}")
    args = args if isinstance(args, dict) else {}
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("Missing required 'prompt' argument.")
    be = backends or Backends()
    refusal = _disabled_reason(runner, str(args.get("model") or "").strip())
    if refusal:
        raise ValueError(refusal)
    return _cap(IMPL[runner](be, args, prompt), be.spill_dir, runner)


def handle(message, backends=None):
    method, msg_id = message.get("method"), message.get("id")
    if msg_id is None:
        return None
    params = message.get("params") or {}
    try:
        if method == "initialize":
            asked = params.get("protocolVersion")
            result = {"protocolVersion": asked if asked in SUPPORTED_VERSIONS else PROTOCOL_VERSION,
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "free_runners", "version": "0.1.0"}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            try:
                text = call_tool(str(params.get("name", "")), params.get("arguments") or {}, backends)
                result = {"content": [{"type": "text", "text": text}], "isError": False}
            except Exception as exc:
                result = {"content": [{"type": "text", "text": f"free_runners error: {exc}"}], "isError": True}
        else:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    except Exception as exc:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": str(exc)}}
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def main(stdin=None, stdout=None, backends=None):
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    while True:
        raw = stdin.readline()
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            response = handle(message, backends) if isinstance(message, dict) else {
                "jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
        if response is not None:
            stdout.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
            stdout.flush()


if __name__ == "__main__":
    main()
