"""E3 done-check: engine dispatch and the Super-Agent ticket server share one adapter per provider.

Run: ``python -m pytest engine/tests -k shared_adapters -q``. No provider is ever called: every
command is built, compared and thrown away; HTTP and processes are fakes.
"""
import ast
import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cc_engine import dispatch, providers
from cc_engine.dispatch import Dispatcher

SERVER_PATH = Path(os.environ.get(
    "DSH_SUBAGENT_SERVER",
    r"~\workspace\waypoint-workspace\tools\dsh_subagent_mcp_server.py"))

CORDIS = """\
- id: llm-pi-ai
  config:
    providers:
      freebuff:
        baseURL: http://127.0.0.1:1455/v1
        models:
          - id: deepseek/deepseek-v4-flash
          - id: openai/gpt-6-luna
"""


@pytest.fixture
def server(monkeypatch):
    """The real ticket server module, loaded from its file (it imports the shared adapters itself)."""
    if not SERVER_PATH.is_file():
        pytest.skip(f"ticket server not found at {SERVER_PATH}")
    monkeypatch.delenv("DSH_SUBAGENT_DENY", raising=False)
    spec = importlib.util.spec_from_file_location("dsh_subagent_server_under_test", SERVER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "QUIET_INSTRUCTION_SUFFIX", "")  # same prompt text in both callers
    return mod


@pytest.fixture
def spy(monkeypatch):
    """Record every call into the shared adapters, whichever caller makes it."""
    calls = []
    for name in ("claude_code", "freebuff", "freebuff_exec", "antigravity", "manual", "gemini_app", "chatgpt_web"):
        real = getattr(providers, name)

        def wrapper(*a, _real=real, _name=name, **kw):
            calls.append(_name)
            return _real(*a, **kw)

        monkeypatch.setattr(providers, name, wrapper)
    return calls


def node(**kw):
    d = {"id": "N1", "title": "Title", "goal": "Goal.", "check": {"type": "text_present", "path": "a", "text": "b"},
         "deps": [], "pool": "claude", "model": "sonnet", "effort": "high"}
    d.update(kw)
    return d


def disp(tmp_path, **kw):
    cfg = tmp_path / "cordis.patch.yml"
    cfg.write_text(CORDIS, encoding="utf-8")
    return Dispatcher(data_dir=tmp_path / "data", launcher=lambda p: {"pid": 1}, freebuff_config=cfg, **kw)


# ---------------------------------------------------------------- the module itself
def test_shared_adapters_module_is_stdlib_only_and_standalone():
    tree = ast.parse(Path(providers.__file__).read_text(encoding="utf-8"))
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            assert n.level == 0, "providers.py must run as a plain script (no relative imports)"
            names = [n.module]
        elif isinstance(n, ast.Import):
            names = [a.name for a in n.names]
        else:
            continue
        for name in names:
            top = name.split(".")[0]
            assert top == "__future__" or top in sys.stdlib_module_names, name


def test_shared_adapters_cover_each_provider_and_never_codex():
    assert set(providers.ADAPTERS) == {"claude_code", "freebuff", "antigravity", "antigravity_manual",
                                      "gemini_app", "chatgpt_web", "manual"}
    for banned in ("codex", "codex_cli", "Codex", "openai-codex"):
        with pytest.raises(providers.ProviderRefused):
            providers.build(banned, prompt="x")
    with pytest.raises(providers.ProviderRefused):
        providers.build("some-metered-api", prompt="x")
    assert not [n for n in dir(providers) if "codex" in n.lower() and callable(getattr(providers, n))]
    assert "codex" not in json.dumps([providers.build(p, prompt="x").argv for p in providers.ADAPTERS]).lower()


def test_shared_adapters_endpoints_match_the_contract():
    assert providers.FREEBUFF_BASE_URL == "http://127.0.0.1:1455/v1"
    assert providers.FREEBUFF_CHAT_URL == "http://127.0.0.1:1455/v1/chat/completions"
    launch = providers.gemini_app()
    assert launch.kind == "manual" and launch.label == "Gemini App" and not launch.argv
    web = providers.chatgpt_web()
    assert web.kind == "manual" and web.label == "ChatGPT Web" and not web.argv and web.url is None
    assert providers.manual().kind == "manual"


# ---------------------------------------------------------------- both callers, one adapter
def test_shared_adapters_server_imports_the_same_module(server):
    assert server._providers is providers


def test_shared_adapters_freebuff_request_identical_from_both_callers(server, spy, tmp_path):
    d = disp(tmp_path)
    prompt = "Summarise the plan in five lines."
    engine_url, engine_body = d.freebuff_url, d.freebuff_body(node(pool="freebuff", model="openai/gpt-6-luna"), prompt)
    cmd, stdin, env = server._build_runner_command("freebuff", {"prompt": prompt, "model": "openai/gpt-6-luna"})
    assert spy == ["freebuff", "freebuff_exec"]  # engine body, then the server's process form
    # The ticket server runs the shared adapter's own CLI; rebuild what that CLI would send.
    assert cmd[1:4] == ["-I", "-u", providers.__file__] and stdin == prompt
    sent = providers.launch_from_cli(cmd[4:], stdin)
    assert (sent.url, sent.body) == (engine_url, engine_body)
    assert sent == providers.freebuff(prompt=prompt, model="openai/gpt-6-luna")


def test_shared_adapters_claude_code_from_one_adapter(server, spy, tmp_path):
    d = disp(tmp_path)
    engine_argv = d.claude_argv(node(model="sonnet", effort="high"))
    cmd, stdin, env = server._build_runner_command("claude", {"prompt": "Audit the module.", "model": "sonnet"})
    assert spy == ["claude_code", "claude_code"]  # one call from each caller
    assert engine_argv == list(providers.claude_code(model="sonnet", effort="high", output_format="json",
                                                         extra_args=dispatch.CLAUDE_WORKER_ARGS).argv)
    assert engine_argv[0] == providers.claude_exe()
    dirs = [p for p in server.CLAUDE_ADD_DIRS if Path(p).is_dir()]
    assert cmd == list(providers.claude_code(model="sonnet", output_format="text", add_dirs=dirs).argv)
    assert cmd[:4] == engine_argv[:4]  # same executable, -p, --model sonnet
    assert stdin == "Audit the module." and "Audit the module." not in cmd  # prompt on stdin, never argv
    for key in providers.METERED_ENV:  # neither caller can fall onto a metered API key
        assert env[key] == ""
        assert key in dispatch.METERED_ENV


def test_shared_adapters_claude_context_file_is_folded_into_stdin(server, tmp_path):
    ctx = tmp_path / "ctx.txt"
    ctx.write_text("alpha", encoding="utf-8")
    _, stdin, _ = server._build_runner_command("claude", {"prompt": "Do it.", "file_path": str(ctx)})
    assert stdin == providers.prompt_with_file("Do it.", str(ctx))
    assert stdin.startswith(f"### Context File: {ctx}") and "alpha" in stdin and stdin.endswith("### Task:\nDo it.")


def test_shared_adapters_antigravity_from_one_adapter(server, spy):
    cmd, stdin, env = server._build_runner_command("antigravity", {"prompt": "Look around.", "model": "gemini-4-argon-high"})
    assert spy == ["antigravity"]
    assert cmd == [providers.AGY_EXE, "-p", "Look around.", "--model", "gemini-4-argon-high",
                   "--dangerously-skip-permissions"]


def test_shared_adapters_gemini_app_and_manual_go_through_the_adapter(spy, tmp_path):
    d = disp(tmp_path)
    res = d.dispatch(node(pool="gemini_app"), {}, {"id": "p1"})
    assert res["status"] == "needs_you" and "Gemini App" in res["summary"]
    res = d.dispatch(node(id="N2", pool="manual"), {}, {"id": "p1"})
    assert res["status"] == "needs_you" and "manual" in res["summary"]
    res = d.dispatch(node(id="N3", pool="chatgpt_web"), {}, {"id": "p1"})
    assert res["status"] == "needs_you" and "ChatGPT Web" in res["summary"]
    assert spy == ["gemini_app", "manual", "chatgpt_web"]


def test_shared_adapters_codex_off_until_switched_on(server, monkeypatch, tmp_path):
    # Codex is a dispatch route now (G2), refused while the Agents switch is off (its default).
    d = disp(tmp_path)
    assert d.route(node(pool="codex")) == "codex"
    assert "Codex" in d.refusal(node(pool="codex"))
    monkeypatch.setattr(server, "DENY", {"delegate_codex_metered_subagent", "runner:codex"})
    with pytest.raises(ValueError, match="disabled"):
        server.dispatch_ticket({"runner": "codex", "prompt": "x"})
    names = [t["name"] for t in server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]]
    assert "delegate_codex_metered_subagent" not in names


# ---------------------------------------------------------------- fallback and CLI
def test_shared_adapters_server_falls_back_when_import_fails(server, monkeypatch):
    monkeypatch.setattr(server, "_providers", None)
    cmd, _, _ = server._build_runner_command("freebuff", {"prompt": "hi"})
    assert cmd[1].endswith("freebuff_api_client.py")


def test_shared_adapters_server_starts_without_the_engine(tmp_path):
    """A broken or missing engine checkout never stops the ticket server from starting."""
    if not SERVER_PATH.is_file():
        pytest.skip("ticket server not present")
    env = dict(os.environ, CC_ENGINE_DIR=str(tmp_path / "no-engine-here"))
    env.pop("PYTHONPATH", None)
    probe = ("import importlib.util,json,sys;"
             f"s=importlib.util.spec_from_file_location('srv',{str(SERVER_PATH)!r});m=importlib.util.module_from_spec(s);"
             "s.loader.exec_module(m);"
             "r=m.handle({'jsonrpc':'2.0','id':1,'method':'tools/list'});"
             "print(json.dumps([m._providers is None,[t['name'] for t in r['result']['tools']]]))")
    out = subprocess.run([sys.executable, "-c", probe], cwd=str(tmp_path), env=env, capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    fell_back, names = json.loads(out.stdout.strip().splitlines()[-1])
    # Codex is off until switched on (nothing answers here), so its tool is hidden: 8 of the 9 tools.
    assert fell_back is True and len(names) >= 8 and "delegate_codex_metered_subagent" not in names


def test_shared_adapters_cli_posts_the_adapter_request(monkeypatch):
    sent = []

    def fake_post(url, body, timeout=0):
        sent.append((url, body))
        return 200, {"choices": [{"message": {"content": "pong"}}]}

    out = io.StringIO()
    code = providers.main(["freebuff", "--model", "openai/gpt-6-luna"], stdin=io.StringIO("ping"), stdout=out,
                          http_post=fake_post)
    assert code == 0 and out.getvalue().strip() == "pong"
    launch = providers.freebuff(prompt="ping", model="openai/gpt-6-luna")
    assert sent == [(launch.url, launch.body)]
    err = io.StringIO()
    code = providers.main(["freebuff"], stdin=io.StringIO("ping"), stdout=io.StringIO(), stderr=err,
                          http_post=lambda u, b, timeout=0: (403, {"error": "cap"}))
    assert code == 1 and "403" in err.getvalue()
    with pytest.raises(SystemExit):
        providers.main(["codex"], stdin=io.StringIO("x"), stdout=io.StringIO(), stderr=io.StringIO())
