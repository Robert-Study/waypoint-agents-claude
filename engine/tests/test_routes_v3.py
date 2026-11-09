"""Waypoint v3: every model connection is a toggle-able, real route. Fakes only; tmp_path databases."""
import json
import stat
import sys
import time
from pathlib import Path

import pytest

from cc_engine import agents, dispatch as D, providers, toggles

IDS = ("claude_code", "codex", "freebuff", "groq", "gemini_studio", "gemini_app", "chatgpt_web",
       "copilot", "deepseek_dsh", "antigravity_manual")
WIN = sys.platform == "win32"


def fake_exe(tmp_path, name, lines, code, sleep=0):
    """A runnable fake CLI that prints ``lines`` to stderr and exits ``code`` (optionally sleeping first)."""
    if WIN:
        p = tmp_path / (name + ".bat")
        body = ["@echo off"]
        if sleep:
            body.append(f"ping -n {sleep + 1} 127.0.0.1 >nul")
        body += [f"echo {l} 1>&2" for l in lines] + [f"exit /b {code}"]
        p.write_text("\r\n".join(body) + "\r\n")
    else:
        p = tmp_path / name
        body = ["#!/bin/sh"] + ([f"sleep {sleep}"] if sleep else []) + [f"echo '{l}' >&2" for l in lines] + [f"exit {code}"]
        p.write_text("\n".join(body) + "\n")
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def node(pool, nid="T1"):
    return {"id": nid, "title": "t", "goal": "Do it.", "check": {"type": "command", "run": "exit 0"},
            "deps": [], "pool": pool, "model": None, "cwd": None, "attempts": 1}


def make(tmp_path, **kw):
    launched = []
    kw.setdefault("agy_signed_in", lambda: True)
    kw.setdefault("usage_reader", lambda: [])
    kw.setdefault("which", lambda n: "C:/bin/" + n)
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    for s in D.TOOL_SCRIPTS.values():
        (tools / s).write_text("")
    kw.setdefault("tools_dir", str(tools))
    launcher = kw.pop("launcher", None) or (lambda spec: launched.append(spec) or {"pid": 1})
    d = D.Dispatcher(data_dir=tmp_path / "data", launcher=launcher, **kw)
    return d, launched


def project():
    return {"id": "p1", "title": "P"}


def spec_of(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def test_every_connection_is_in_the_toggle_registry_with_the_right_defaults(tmp_path):
    rows = {r["id"]: r for r in toggles.list_toggles(tmp_path / "cc.sqlite", registry=[])}
    for cid in IDS:
        assert cid in rows, cid
        assert rows[cid]["enabled"] is (cid != "codex")
    for alias in ("agy", "agy_cli", "route:agy_cli", "dsh", "runner:dsh", "copilot", "gh_copilot", "groq", "gemini"):
        assert toggles.canonical(alias) in IDS


def test_agents_payload_lists_every_connection(tmp_path):
    rt = agents.Runtime(which=lambda n: "C:/bin/" + n, port_open=lambda p: True)
    got = {c["id"]: c for c in agents.connections(rt, rows=[], db=tmp_path / "cc.sqlite",
                                                  tools_dir=str(tmp_path))}
    assert tuple(got) == IDS
    assert got["codex"]["enabled"] is False and got["codex"]["dispatchable"] is False
    assert got["copilot"]["enabled"] is True
    assert got["gemini_app"]["state"] == "ready" and got["chatgpt_web"]["state"] == "ready"
    assert got["antigravity_manual"]["agent_id"] == "antigravity"


def test_connection_states_in_one_line(tmp_path):
    rt = agents.Runtime(which=lambda n: None if n == "copilot" else "x", port_open=lambda p: p != 1455)
    got = {c["id"]: c for c in agents.connections(rt, rows=[], db=tmp_path / "cc.sqlite", tools_dir=str(tmp_path))}
    assert got["copilot"]["state"] == "not_installed" and "not installed" in got["copilot"]["line"]
    assert got["freebuff"]["state"] == "bridge_down" and got["freebuff"]["line"].startswith("Bridge down")
    assert got["chatgpt_web"]["state"] == "ready" and got["chatgpt_web"]["line"] == "Manual: Copy Prompt"
    for c in got.values():
        assert "\n" not in c["line"]


@pytest.mark.parametrize("route", D.CLI_ROUTES)
def test_a_disabled_route_is_refused(tmp_path, route):
    d, launched = make(tmp_path)
    toggles.set_enabled(route, False, "test", db=tmp_path / "data" / "cc.sqlite")
    out = d.dispatch(node(route), {}, project())
    assert out["status"] == "refused" and "switched off" in out["reason"]
    assert launched == []


def test_copilot_absent_is_not_installed_and_not_dispatchable(tmp_path):
    d, launched = make(tmp_path, which=lambda n: None)
    out = d.dispatch(node("copilot"), {}, project())
    assert out["status"] == "refused" and out["unavailable"] is True
    assert "not installed" in out["reason"] and launched == []


def test_copilot_present_builds_the_non_interactive_command(tmp_path):
    d, launched = make(tmp_path, copilot_exe="C:/bin/copilot.cmd")
    out = d.dispatch(node("copilot"), {}, project())
    assert out["status"] == "running"
    argv = spec_of(launched[0])["argv"]
    assert argv[0] == "C:/bin/copilot.cmd" and "-p" in argv and "-s" in argv


def test_dsh_groq_gemini_build_commands(tmp_path):
    d, launched = make(tmp_path, dsh_exe="dsh")
    for route in ("deepseek_dsh", "groq", "gemini_studio"):
        assert d.dispatch(node(route, "N" + route), {}, project())["status"] == "running"
    specs = [spec_of(p) for p in launched]
    assert specs[0]["argv"][:2] == ["dsh", "headless"]
    assert specs[1]["argv"][1].endswith("groq_subagent.py") and "--file" in specs[1]["argv"]
    assert specs[2]["argv"][1].endswith("gemini_subagent.py")


def run_agy(tmp_path, exe, **kw):
    def launcher(spec):
        D.run_job(spec, heartbeat=False)
        return {"pid": 1}
    # credits are known to be left, so the CLI launches and its own quota error is what we test
    kw.setdefault("usage_reader", lambda: [{"pool": "antigravity_week", "remaining_percent": 50.0,
                                             "status": "FRESH", "resets_at": "2099-01-02T00:00:00+00:00"}])
    d, _ = make(tmp_path, agy_exe=exe, launcher=launcher, **kw)
    t0 = time.time()
    out = d.dispatch(node("agy_cli"), {}, project())
    polled = d.poll("p1", "T1")
    return out, polled, time.time() - t0


def test_agy_quota_error_is_a_clean_one_line_failure_within_a_timeout(tmp_path):
    exe = fake_exe(tmp_path, "agy", ["Error 429: RESOURCE_EXHAUSTED you are out of credits"], 1)
    out, polled, took = run_agy(tmp_path, exe)
    assert out["status"] == "running" and took < 30
    assert polled["state"] == "exited" and polled["exit_code"] == 1
    assert polled["reason"].startswith("Out of weekly credits") and "\n" not in polled["reason"]
    assert polled["error"] == polled["reason"]


def test_agy_never_hangs(tmp_path):
    exe = fake_exe(tmp_path, "agy", [], 0, sleep=30)
    spec = {"kind": "exec", "route": "agy_cli", "argv": [exe], "cwd": None, "stdin_file": None,
            "timeout_seconds": 2}
    job = tmp_path / "job"
    job.mkdir()
    (job / "spec.json").write_text(json.dumps(spec))
    t0 = time.time()
    res = D.run_job(job / "spec.json", heartbeat=False)
    assert time.time() - t0 < 20
    assert res["exit_code"] == 124 and res["reason"].startswith("agy timed out")


def test_agy_out_of_credits_is_detected_before_launch(tmp_path):
    row = {"pool": "antigravity_week", "remaining_percent": 0.0, "status": "FRESH",
           "resets_at": "2099-01-02T00:00:00+00:00"}
    exe = fake_exe(tmp_path, "agy", [], 0)
    d, launched = make(tmp_path, agy_exe=exe, usage_reader=lambda: [row])
    out = d.dispatch(node("agy_cli"), {}, project())
    assert out["status"] == "refused" and out["reason"].startswith("Out of weekly credits")
    assert "resets" in out["reason"] and launched == []
    rt = agents.Runtime(which=lambda n: exe, port_open=lambda p: True)
    got = {c["id"]: c for c in agents.connections(rt, rows=[row], db=tmp_path / "x.sqlite", tools_dir=str(tmp_path))}
    assert got["antigravity_manual"]["state"] in ("out_of_credits", "signed_out", "not_installed")


def test_agy_signed_out_never_starts_the_cli(tmp_path):
    exe = fake_exe(tmp_path, "agy", [], 0)
    d, launched = make(tmp_path, agy_exe=exe, agy_signed_in=lambda: False)
    out = d.dispatch(node("agy_cli"), {}, project())
    assert out["status"] == "refused" and out["reason"].startswith("Not signed in") and launched == []


def test_agy_invocation_is_print_mode_with_closed_stdin_and_no_usage_call():
    launch = providers.agy_cli("do it", exe="agy")
    assert launch.argv[:3] == ("agy", "--print", "do it") and "--print-timeout" in launch.argv
    assert launch.stdin == "" and "/usage" not in launch.argv


def test_existing_manual_antigravity_route_is_unchanged():
    for name in ("antigravity", "agy"):
        assert D.route_for_pool(name) == "antigravity_manual"
    assert D.route_for_pool("agy_cli") == "agy_cli"
    assert toggles.canonical("agy_cli") == "antigravity_manual"


def test_agy_usage_unknown_fails_closed():
    """Usage probes that failed (no known row) never read as credits left (live smoke finding)."""
    from cc_engine import dispatch as d
    st = d.route_state("agy_cli", which=lambda _x: "agy", usage_rows=[], agy_exe="agy")
    assert st["state"] == "usage_unknown" and st["ok"] is False
    rows = [{"pool": p, "remaining_percent": 40.0, "status": "FRESH"} for p in d.USAGE_POOLS["agy_cli"]]
    assert d.route_state("agy_cli", which=lambda _x: "agy", usage_rows=rows, agy_exe="agy")["state"] == "ready"


def test_claude_model_ids_reach_the_cli_in_its_spelling():
    from cc_engine import agents
    assert agents.claude_model_id("opus-5.5") == "claude-opus-5-5"
    assert agents.claude_model_id("haiku-5.5") == "claude-haiku-5-5"
    assert agents.claude_model_id("claude-sonnet-5-5") == "claude-sonnet-5-5"
    argv = agents.claude_argv("claude", {"_new_ext_id": "x"}, model="sonnet-5.5")
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5-5"


def test_bridge_port_open_but_502_is_down_not_ready():
    """A bridge that held its port while answering 502 all evening used to read Ready in Agents."""
    from cc_engine import dispatch as d
    down = d.route_state("freebuff", port_open=lambda p: True, bridge_ok=lambda p: (False, "HTTP 502"))
    assert down["state"] == "bridge_down" and down["line"] == "Bridge not answering (HTTP 502)" and not down["ok"]
    up = d.route_state("freebuff", port_open=lambda p: True, bridge_ok=lambda p: (True, "HTTP 404"))
    assert up["state"] == "ready"
    # injected port probe without a bridge probe keeps old behaviour (no live HTTP in tests)
    assert d.route_state("freebuff", port_open=lambda p: True)["state"] == "ready"
