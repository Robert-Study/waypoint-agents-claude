"""One adapter per provider, shared by engine dispatch and the Super-Agent ticket server.

Each adapter is a pure function that returns a ``Launch``: the exact command (``exec``), the exact
loopback request (``http``) or a Copy Prompt hand-off (``manual``). Callers own running it, so the
engine (``cc_engine.dispatch``) and ``waypoint-workspace/tools/dsh_subagent_mcp_server.py``
build identical commands for the same inputs.

Adapters
- ``claude_code``: ``claude -p [--model m] --output-format <fmt> [--effort e] [--add-dir d]...``,
  prompt on stdin; metered API keys are dropped from the child environment.
- ``freebuff``: OpenAI-compatible POST to ``http://127.0.0.1:1455/v1/chat/completions``.
  ``freebuff_exec`` is the same request run as a subprocess (``python -I -u providers.py freebuff``)
  for callers that need a process, such as background tickets.
- ``antigravity``: ``agy.exe -p <prompt> --model <m> --dangerously-skip-permissions``, for the
  Super-Agent ticket server only. The engine never routes to it.
- ``antigravity_manual``: Antigravity as a manual agentic coder (Owner, 4 Nov 2026): a ready
  packet (prompt, cwd, checks) that Owner opens in Antigravity himself; never run unattended.
  Antigravity (``agy``) is not the Gemini API (Google AI Studio keys); they stay separate.
- ``agy_cli``: native Antigravity dispatch: ``agy --print <short prompt> --output-format text
  --print-timeout 60m --dangerously-skip-permissions [--model m]`` (stdin closed; the full brief is
  a file the short prompt names). The dispatcher checks sign-in and credits first, so agy is
  never started when it would ask for a browser sign-in or has no weekly credits.
- ``copilot``: ``copilot -p <prompt> -s --allow-all-tools`` (GitHub Copilot CLI, only when on PATH).
- ``deepseek_dsh``: ``dsh headless <prompt>`` (dsh's own DeepSeek profile).
- ``groq`` / ``gemini_studio``: ``python groq_subagent.py|gemini_subagent.py --prompt .. --file ..``.
- ``gemini_app`` / ``chatgpt_web`` / ``manual``: never automated; the caller writes a Copy Prompt packet.

There is no Codex adapter: Codex CLI is banned as a subagent, and ``build`` refuses it.

Stdlib only and no package-relative imports, so this file also runs as a plain script.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
import importlib.util as _ilu

def _load_config():
    """config.py loaded by path: this module also runs as a plain script, so no relative import."""
    spec = _ilu.spec_from_file_location("_wp_config", Path(__file__).with_name("config.py"))
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


config = _load_config()

POWERSHELL = "powershell.exe"
FREEBUFF_BASE_URL = "http://127.0.0.1:1455/v1"
FREEBUFF_CHAT_URL = FREEBUFF_BASE_URL + "/chat/completions"
FREEBUFF_DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
AGY_EXE = config.AGY_EXE
AGY_DEFAULT_MODEL = "gemini-3.8-flash-high"
TOOLS_DIR = config.TOOLS_DIR
AGY_PRINT_TIMEOUT = "60m"
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Environment variables that would turn a subscription route into metered spend.
METERED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
               "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX")
# Names that never get an adapter, whatever a caller asks for.
REFUSED_PROVIDERS = ("codex", "codex_cli", "codex_5h", "codex_week", "openai-codex")


class ProviderRefused(ValueError):
    """Raised for Codex and for any provider without an adapter."""


@dataclass(frozen=True)
class Launch:
    provider: str
    kind: str  # "exec" | "http" | "manual"
    argv: tuple = ()
    stdin: str | None = None
    url: str | None = None
    body: dict | None = field(default=None, hash=False)
    env_drop: tuple = ()
    label: str | None = None


def claude_exe():
    """The Claude Code CLI on PATH (``claude.cmd`` on Windows), else plain ``claude``."""
    return shutil.which("claude") or "claude"


def prompt_with_file(prompt, file_path=None):
    """Fold a context file into a prompt, in the layout ``claude_subagent.ps1`` used."""
    if not file_path:
        return prompt
    path = Path(str(file_path))
    if not path.is_file():
        return prompt
    text = path.read_text(encoding="utf-8", errors="replace")
    return f"### Context File: {file_path}\n```\n{text}\n```\n\n### Task:\n{prompt}"


# ---------------------------------------------------------------- adapters
def claude_code(prompt=None, model=None, effort=None, output_format="json", add_dirs=(), extra_args=(),
                exe=None):
    argv = [exe or claude_exe(), "-p"]
    if model:
        argv += ["--model", str(model)]
    argv += ["--output-format", output_format]
    if effort in CLAUDE_EFFORTS:
        argv += ["--effort", effort]
    for d in add_dirs or ():
        argv += ["--add-dir", str(d)]
    argv += [str(a) for a in extra_args or ()]
    return Launch("claude_code", "exec", tuple(argv), stdin=prompt, env_drop=METERED_ENV)


def freebuff(prompt, model=None, models=None, url=None):
    """``models``: the ids Freebuff offers (from cordis.patch.yml); an unknown ``model`` falls back to the first."""
    if models:
        chosen = model if model in models else models[0]
    else:
        chosen = model or FREEBUFF_DEFAULT_MODEL
    body = {"model": chosen, "messages": [{"role": "user", "content": prompt}], "stream": False}
    return Launch("freebuff", "http", url=url or FREEBUFF_CHAT_URL, body=body)


def freebuff_exec(prompt, model=None, python=None):
    """The ``freebuff`` request as a process: this file's CLI rebuilds it with ``freebuff()`` and posts it."""
    argv = [python or sys.executable, "-I", "-u", str(Path(__file__).resolve()), "freebuff",
            "--model", model or FREEBUFF_DEFAULT_MODEL]
    return Launch("freebuff", "exec", tuple(argv), stdin=prompt)


def antigravity(prompt, model=None, exe=None):
    argv = [exe or AGY_EXE, "-p", str(prompt), "--model", model or AGY_DEFAULT_MODEL,
            "--dangerously-skip-permissions"]
    return Launch("antigravity", "exec", tuple(argv))


def agy_exe():
    return shutil.which("agy") or AGY_EXE


def agy_cli(prompt, model=None, exe=None, print_timeout=None):
    argv = [exe or agy_exe(), "--print", str(prompt), "--output-format", "text",
            "--print-timeout", print_timeout or AGY_PRINT_TIMEOUT, "--dangerously-skip-permissions"]
    if model:
        argv += ["--model", str(model)]
    return Launch("agy_cli", "exec", tuple(argv), stdin="")


def copilot(prompt, model=None, exe=None):
    argv = [exe or shutil.which("copilot") or "copilot", "-p", str(prompt), "-s", "--allow-all-tools"]
    if model:
        argv += ["--model", str(model)]
    return Launch("copilot", "exec", tuple(argv), stdin="")


def deepseek_dsh(prompt, exe=None):
    return Launch("deepseek_dsh", "exec", (exe or shutil.which("dsh") or "dsh", "headless", str(prompt)),
                  stdin="")


def _tool_script(provider, script, prompt, file_path=None, model=None, python=None, tools_dir=None):
    argv = [python or sys.executable, str(Path(tools_dir or TOOLS_DIR) / script), "--prompt", str(prompt)]
    if file_path:
        argv += ["--file", str(file_path)]
    if model:
        argv += ["--model", str(model)]
    return Launch(provider, "exec", tuple(argv))


def groq(prompt, file_path=None, model=None, python=None, tools_dir=None):
    return _tool_script("groq", "groq_subagent.py", prompt, file_path, model, python, tools_dir)


def gemini_studio(prompt, file_path=None, model=None, python=None, tools_dir=None):
    return _tool_script("gemini_studio", "gemini_subagent.py", prompt, file_path, model, python, tools_dir)


def gemini_app(prompt=None, **_):
    return Launch("gemini_app", "manual", stdin=prompt, label="Gemini App")


def chatgpt_web(prompt=None, **_):
    """ChatGPT Web: one-click manual hand-off (https://chatgpt.com/); Owner sends the prompt himself."""
    return Launch("chatgpt_web", "manual", stdin=prompt, label="ChatGPT Web")


def antigravity_manual(prompt=None, **_):
    return Launch("antigravity_manual", "manual", stdin=prompt, label="Antigravity")


def manual(prompt=None, **_):
    return Launch("manual", "manual", stdin=prompt, label="manual")


ADAPTERS = {"claude_code": claude_code, "freebuff": freebuff,
            "antigravity": antigravity, "antigravity_manual": antigravity_manual,
            "gemini_app": gemini_app, "chatgpt_web": chatgpt_web, "manual": manual}


# v3 CLI routes the engine dispatches (kept apart from the Super-Agent ticket server's ADAPTERS).
CLI_ADAPTERS = {"agy_cli": agy_cli, "copilot": copilot, "deepseek_dsh": deepseek_dsh, "groq": groq,
                "gemini_studio": gemini_studio}


def build(provider, **kwargs):
    name = str(provider or "").strip().lower()
    if name in REFUSED_PROVIDERS or "codex" in name:
        raise ProviderRefused(f"'{provider}' is never a subagent route (Codex CLI is banned).")
    fn = ADAPTERS.get(name) or CLI_ADAPTERS.get(name)
    if fn is None:
        raise ProviderRefused(f"No adapter for '{provider}'; known: {', '.join(ADAPTERS)}.")
    return fn(**kwargs)


# ---------------------------------------------------------------- CLI (used by freebuff_exec)
def post_json(url, body, timeout=600):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # loopback only
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8", "replace"))
        except ValueError:
            payload = {"error": str(exc)}
        return exc.code, payload


def _parser():
    ap = argparse.ArgumentParser(prog="providers.py", description="Run one shared provider request; prompt on stdin.")
    ap.add_argument("provider", choices=["freebuff"])
    ap.add_argument("--model", default=None)
    ap.add_argument("--timeout", type=float, default=600)
    return ap


def launch_from_cli(argv, stdin_text):
    """The ``Launch`` this CLI sends for ``argv`` (everything after the script path) and its stdin."""
    args = _parser().parse_args(list(argv))
    return freebuff(prompt=stdin_text, model=args.model)


def main(argv=None, stdin=None, stdout=None, stderr=None, http_post=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if stdin is None:  # callers write UTF-8 pipes; do not decode them with the Windows code page
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    args = _parser().parse_args(argv)
    launch = launch_from_cli(argv, stdin.read())
    try:
        status, payload = (http_post or post_json)(launch.url, launch.body, timeout=args.timeout)
    except (urllib.error.URLError, OSError) as exc:
        print(f"ERROR: Freebuff not reachable at {launch.url}: {exc}", file=stderr)
        return 1
    if not 200 <= int(status) < 300:
        print(f"ERROR: Freebuff returned HTTP {status}: {json.dumps(payload)[:500]}", file=stderr)
        return 1
    try:
        text = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        text = json.dumps(payload)
    print(text, file=stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
