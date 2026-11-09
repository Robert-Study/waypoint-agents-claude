import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cc_engine import dispatch
from cc_engine.dispatch import Dispatcher, build_prompt, run_job


class FakeClock:
    def __init__(self, t=1_000_000.0):
        self.t = t
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def node(id="N1", pool="claude", model="sonnet", effort="high", **kw):
    d = {"id": id, "title": f"Title {id}", "goal": f"Goal of {id}.",
         "check": {"type": "command", "run": "python -c 0"}, "deps": [],
         "pool": pool, "model": model, "effort": effort}
    d.update(kw)
    return d


PROJECT = {"id": "p1", "title": "Proj", "brief": "Brief.", "cwd": "C:/work"}

CORDIS = """\
- id: agent-default-model
  config:
    provider: freebuff
    model: deepseek/deepseek-v4-flash
- id: llm-pi-ai
  config:
    providers:
      freebuff:
        displayName: 1. Freebuff
        baseURL: http://127.0.0.1:1455/v1
        models:
          - id: deepseek/deepseek-v4-flash
            name: Flash
          - id: openai/gpt-6-luna
            name: Luna
"""


class FakeProcs:
    """Fake process table: pids in ``alive`` are running; ``killed`` records tree kills."""

    def __init__(self):
        self.alive = set()
        self.killed = []

    def pid_alive(self, pid, started_at=None):
        return pid in self.alive

    def kill(self, pid):
        self.killed.append(pid)
        self.alive.discard(pid)
        return 0


@pytest.fixture
def env(tmp_path):
    clock = FakeClock()
    launched = []
    asked = []
    procs = FakeProcs()
    cfg = tmp_path / "cordis.patch.yml"
    cfg.write_text(CORDIS, encoding="utf-8")

    def launcher(spec_path):
        launched.append(json.loads(Path(spec_path).read_text(encoding="utf-8")))
        procs.alive.add(4242)
        return {"pid": 4242}

    def asker(**kw):
        asked.append(kw)
        return f"q{len(asked)}"

    d = Dispatcher(data_dir=tmp_path, launcher=launcher, asker=asker, clock=clock,
                   sleep=clock.sleep, claude_exe="claude", powershell_exe="powershell",
                   pid_alive=procs.pid_alive, killer=procs.kill, freebuff_config=cfg)
    d.procs = procs
    return d, clock, launched, asked


def test_route_by_pool(env):
    d = env[0]
    assert d.route(node(pool="claude")) == "claude_code"
    assert d.route(node(pool="chatgpt_web")) == "chatgpt_web"
    assert d.route(node(pool="chatgpt")) == "chatgpt_web"
    assert d.route(node(pool="codex")) == "codex"  # refused while switched off in Agents
    assert d.route(node(pool="freebuff")) == "freebuff"
    assert d.route(node(pool="gemini_app")) == "gemini_app"
    assert d.route(node(pool="manual")) == "manual"
    assert d.route(node(pool="unknown-paid-api")) == "manual"  # never a metered route
    assert d.route(node(pool=None), {"default_pool": "freebuff"}) == "freebuff"


def test_claude_code_command_exact(env):
    d, clock, launched, _ = env
    res = d.dispatch(node(), {}, PROJECT)
    assert res["status"] == "running" and res["route"] == "claude_code"
    spec = launched[0]
    assert spec["argv"] == ["claude", "-p", "--model", "sonnet", "--output-format", "json",
                            "--effort", "high", "--permission-mode", "acceptEdits"]
    assert spec["cwd"] == "C:/work"
    assert Path(spec["stdin_file"]).read_text(encoding="utf-8").startswith("# Task N1")


def test_child_env_strips_metered_keys():
    env = dispatch.child_env({"ANTHROPIC_API_KEY": "x", "ANTHROPIC_BASE_URL": "y", "PATH": "p"})
    assert env == {"PATH": "p"}


def test_freebuff_models_read_from_cordis_patch(tmp_path):
    cfg = tmp_path / "c.yml"
    cfg.write_text(CORDIS, encoding="utf-8")
    assert dispatch.freebuff_models(cfg) == ["deepseek/deepseek-v4-flash", "openai/gpt-6-luna"]
    assert dispatch.freebuff_models(tmp_path / "missing.yml") == list(dispatch.FREEBUFF_FALLBACK_MODELS)


def test_freebuff_is_loopback_openai_post_with_real_model_ids(env):
    d, clock, launched, _ = env
    d.dispatch(node(pool="freebuff", model="deepseek-chat"), {}, PROJECT)  # stale id
    d.dispatch(node(id="N2", pool="freebuff", model="openai/gpt-6-luna"), {}, PROJECT)
    spec = launched[0]
    assert spec["kind"] == "http"
    assert spec["url"] == "http://127.0.0.1:1455/v1/chat/completions"
    assert spec["body"]["model"] == "deepseek/deepseek-v4-flash"
    assert spec["body"]["messages"][0]["role"] == "user"
    assert launched[1]["body"]["model"] == "openai/gpt-6-luna"


def test_freebuff_eligible_only_for_text_work():
    text = node(check={"type": "text_present", "path": "a.md", "text": "x"})
    human = node(check={"type": "human", "prompt": "ok"})
    files = node(check={"type": "files_exist", "paths": ["a"]})
    cmd = node()
    said = node(goal="Draft the summary (text-only).")
    assert dispatch.freebuff_eligible(text) and dispatch.freebuff_eligible(human)
    assert dispatch.freebuff_eligible(said)
    assert not dispatch.freebuff_eligible(files) and not dispatch.freebuff_eligible(cmd)
    assert dispatch.freebuff_eligible(cmd, {"freebuff_file_work": True})


@pytest.mark.parametrize("pool,label", [("gemini_app", "Gemini App"), ("chatgpt_web", "ChatGPT Web"), ("manual", "manual")])
def test_manual_routes_never_launch(env, pool, label):
    d, clock, launched, asked = env
    res = d.dispatch(node(pool=pool), {}, PROJECT)
    assert launched == []
    assert res["status"] == "needs_you"
    assert res["question_id"] == "q1"
    assert asked[0]["node_id"] == "N1" and asked[0]["project_id"] == "p1"
    assert asked[0]["purpose"] == "manual_result"
    assert label in asked[0]["prompt"]
    assert Path(res["packet"]).exists()
    assert "Goal of N1." in Path(res["packet"]).read_text(encoding="utf-8")


def test_prompt_packet_quotes_goal_and_check():
    text = build_prompt(node(), PROJECT)
    assert "Goal of N1." in text
    assert '"type": "command"' in text
    assert "Brief." in text


def test_run_job_exec_and_poll(env):
    d, clock, launched, _ = env
    d.dispatch(node(attempts=2), {}, PROJECT)
    job = d.latest_job("p1", "N1")
    assert d.poll("p1", "N1")["state"] == "running"
    calls = []

    def fake_exec(argv, cwd, stdin_text, out, err):
        calls.append((argv, cwd, stdin_text))
        Path(out).write_text('{"result": "ok"}', encoding="utf-8")
        return 0

    res = run_job(job / "spec.json", exec_runner=fake_exec, clock=clock, heartbeat=False)
    assert res["exit_code"] == 0 and res["attempt"] == 2
    assert calls[0][2].startswith("# Task N1")
    p = d.poll("p1", "N1")
    assert p["state"] == "exited" and p["exit_code"] == 0
    assert Path(p["output_path"]).read_text(encoding="utf-8") == '{"result": "ok"}'


def test_run_job_http_with_fake_post(env):
    d, clock, launched, _ = env
    d.dispatch(node(pool="freebuff"), {}, PROJECT)
    job = d.latest_job("p1", "N1")
    posted = []

    def fake_post(url, body):
        posted.append((url, body))
        return 200, {"choices": [{"message": {"content": "done"}}]}

    res = run_job(job / "spec.json", http_post=fake_post, clock=clock, heartbeat=False)
    assert res["exit_code"] == 0
    assert posted[0][0] == dispatch.FREEBUFF_URL
    assert (job / "out.txt").read_text(encoding="utf-8") == "done"


def test_transient_http_403_is_recorded(env):
    d, clock, launched, _ = env
    d.dispatch(node(pool="freebuff"), {}, PROJECT)
    job = d.latest_job("p1", "N1")
    res = run_job(job / "spec.json", http_post=lambda u, b: (403, {"error": "forbidden"}),
                  clock=clock, heartbeat=False)
    assert res["exit_code"] == 1 and res["http_status"] == 403
    p = d.poll("p1", "N1")
    assert p["state"] == "exited" and p["http_status"] == 403


def test_run_job_records_failure_without_raising(env):
    d, clock, launched, _ = env
    d.dispatch(node(), {}, PROJECT)
    job = d.latest_job("p1", "N1")

    def boom(*a):
        raise OSError("no exe")

    res = run_job(job / "spec.json", exec_runner=boom, clock=clock, heartbeat=False)
    assert res["exit_code"] == 1 and "no exe" in res["error"]
    assert d.poll("p1", "N1")["state"] == "exited"


def test_lost_job_records_pid_and_start(env):
    d, clock, launched, _ = env
    d.dispatch(node(), {"job_timeout_minutes": 30}, PROJECT)
    launch = json.loads((d.latest_job("p1", "N1") / "launch.json").read_text(encoding="utf-8"))
    assert launch["pid"] == 4242 and launch["started_at"] == clock()
    assert launch["timeout_seconds"] == 1800


def test_lost_job_only_when_pid_dead_never_by_heartbeat_age(env):
    d, clock, launched, _ = env
    d.dispatch(node(), {}, PROJECT)
    # A machine asleep for 8 hours: heartbeat is ancient, but the pid is alive -> running.
    later = clock() + 8 * 3600
    p = d.poll("p1", "N1", now=later)
    assert p["state"] == "running" and d.procs.killed == []
    d.procs.alive.discard(4242)
    assert d.poll("p1", "N1", now=later)["state"] == "lost"


def test_lost_job_timeout_kills_process_tree(env):
    d, clock, launched, _ = env
    d.dispatch(node(), {"job_timeout_minutes": 1}, PROJECT)
    job = d.latest_job("p1", "N1")
    # The wrapper's heartbeat says it has actively run for 61 s.
    (job / "heartbeat").write_text(json.dumps({"at": clock() + 61, "active": 61}), encoding="utf-8")
    p = d.poll("p1", "N1", now=clock() + 61)
    assert p["state"] == "timeout" and d.procs.killed == [4242]
    assert (job / "killed.json").exists()
    assert d.poll("p1", "N1", now=clock() + 62)["state"] == "timeout"  # stays decided


def test_lost_job_sleep_gap_does_not_count_toward_timeout(env):
    d, clock, launched, _ = env
    d.dispatch(node(), {"job_timeout_minutes": 2}, PROJECT)
    job = d.latest_job("p1", "N1")
    (job / "heartbeat").write_text(json.dumps({"at": clock() + 10, "active": 10}), encoding="utf-8")
    # 3 hours later (asleep): only up to two beats of the gap count.
    assert d.poll("p1", "N1", now=clock() + 3 * 3600)["state"] == "running"
    assert d.procs.killed == []


def test_lost_job_run_job_heartbeat_counts_active_time_only(env, monkeypatch):
    d, clock, launched, _ = env
    d.dispatch(node(), {}, PROJECT)
    job = d.latest_job("p1", "N1")
    res = run_job(job / "spec.json", exec_runner=lambda *a: 0, clock=clock, heartbeat=False)
    assert res["exit_code"] == 0
    hb = json.loads((job / "heartbeat").read_text(encoding="utf-8"))
    assert set(hb) == {"at", "active"}


def test_lost_job_kill_job_and_live_jobs(env):
    d, clock, launched, _ = env
    d.dispatch(node(), {}, PROJECT)
    d.dispatch(node(id="N2"), {}, PROJECT)
    assert {j["node"] for j in d.live_jobs()} == {"N1", "N2"}
    assert d.kill_job("p1", "N1", reason="run cancelled") is True
    assert d.procs.killed == [4242]
    assert d.poll("p1", "N1")["state"] == "cancelled"


def test_lost_job_pid_alive_with_real_processes():
    assert dispatch.pid_alive(os.getpid())
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=60)
    assert not dispatch.pid_alive(proc.pid)
    assert not dispatch.pid_alive(None) and not dispatch.pid_alive(-1)


def test_lost_job_legacy_job_without_pid_waits_for_timeout(env):
    d, clock, launched, _ = env
    d.launcher = lambda spec_path: None  # no pid recorded
    d.dispatch(node(), {}, PROJECT)
    assert d.poll("p1", "N1", now=clock() + 600)["state"] == "running"
    assert d.poll("p1", "N1", now=clock() + dispatch.LOST_AFTER_SECONDS + 1)["state"] == "lost"


def test_each_dispatch_gets_new_job_dir(env):
    d, clock, launched, _ = env
    d.dispatch(node(), {}, PROJECT)
    d.dispatch(node(), {}, PROJECT)
    assert d.latest_job("p1", "N1").name == "2"
    assert d.poll("p1", "N9")["state"] == "none"


def test_job_wrapper_runs_as_real_subprocess(tmp_path):
    """The detached wrapper entry point works end to end (harmless local command only)."""
    job = tmp_path / "jobs" / "p1" / "N1" / "1"
    job.mkdir(parents=True)
    spec = {"kind": "exec", "route": "claude_code", "cwd": str(tmp_path), "stdin_file": None,
            "argv": [sys.executable, "-c", "print('hello from job')"]}
    (job / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    engine = Path(__file__).resolve().parents[1]
    proc = subprocess.run([sys.executable, "-m", "cc_engine.dispatch", "job", str(job / "spec.json")],
                          cwd=str(engine), capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    d = Dispatcher(data_dir=tmp_path, launcher=lambda p: None)
    p = d.poll("p1", "N1")
    assert p["state"] == "exited" and p["exit_code"] == 0
    assert "hello from job" in Path(p["output_path"]).read_text(encoding="utf-8")
