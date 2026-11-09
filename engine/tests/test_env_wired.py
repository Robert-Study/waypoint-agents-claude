"""T2 wired: env_policy.choose decides where each job runs, recorded in the job dir, an event and cc_where."""
import json
from pathlib import Path

from cc_engine import claims, dispatch, env_policy
from cc_engine import mcp_server as m
from cc_engine import scheduler as S

from test_scheduler import make_env

SETTINGS = {"model_ladder": {"claude_code": ["sonnet", "opus"]}, "effort": "high"}
X = {"pool": "claude_code", "model": "sonnet", "effort": "high"}
PROJECT = {"id": "p1", "title": "Proj", "brief": "Brief.", "cwd": "C:/work"}


def _go(env, nid):
    d = env.new_dispatcher(toggles_db=env.store)
    return d.dispatch(S.get_node(env.store, "p1", nid), SETTINGS, PROJECT)


def test_env_wired_dispatch_records_choice_in_meta_spec_and_event(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started", dict(X))])
    out = _go(env, "A")
    job = Path(out["job"])
    meta = json.loads((job / "meta.json").read_text(encoding="utf-8"))
    assert meta["where"]["env"] in env_policy.ENVS and meta["where"]["why"]
    assert out["env"] == meta["where"]
    assert json.loads((job / "spec.json").read_text(encoding="utf-8"))["env"] == meta["where"]
    evs = [e for e in env.store.events("p1") if e["kind"] == "env"]
    assert len(evs) == 1 and evs[0]["summary"].startswith("Task A runs on ")


def test_env_wired_exit_json_carries_env(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started", dict(X))])
    out = _go(env, "A")
    job = Path(out["job"])
    spec = json.loads((job / "spec.json").read_text(encoding="utf-8"))
    spec.update(kind="exec", argv=["x"], stdin_file=None)
    (job / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    res = dispatch.run_job(job / "spec.json", exec_runner=lambda *a: 0, heartbeat=False)
    assert res["env"] == out["env"]
    assert json.loads((job / "exit.json").read_text(encoding="utf-8"))["env"] == out["env"]


def test_env_wired_other_live_writer_gets_a_worktree(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started", dict(X)), ("B", [], "not_started", dict(X))])
    claims.claim(env.store, "p1", "B", "other-session", cwd="C:/work")
    out = _go(env, "A")
    assert out["env"]["env"] == "worktree" and out["env"]["workdir"] == "worktree"


def test_env_wired_read_only_node_is_a_subagent(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started", dict(X, read_only=True))])
    node = S.get_node(env.store, "p1", "A")
    node["read_only"] = True
    d = env.new_dispatcher(toggles_db=env.store)
    assert d.dispatch(node, SETTINGS, PROJECT)["env"]["env"] == "subagent"


def test_env_wired_claim_fills_environment_when_unset(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started", dict(X))])
    got = claims.claim(env.store, "p1", "A", "lead-session", cwd="C:/work")
    assert got["claim"]["environment"] in {e.replace("_", " ") for e in env_policy.ENVS}
    env2 = make_env(tmp_path / "x", [("A", [], "not_started", dict(X))])
    got = claims.claim(env2.store, "p1", "A", "lead-session", environment="my chat")
    assert got["claim"]["environment"] == "my chat"


class FakeHttp:
    def __init__(self, claims_body):
        self.body = claims_body

    def request(self, method, path, body=None):
        return 200, {"claims": self.body}


def test_env_wired_cc_where_tool(tmp_path):
    names = [t["name"] for t in m.TOOLS]
    assert "cc_where" in names
    text = m.call_tool("cc_where", {"repo": "C:/work", "read_only": True}, FakeHttp({}))
    assert text.startswith("subagent") and "AGENTS.md" in text
    busy = {"X": {"by": "someone", "worktree": None, "cwd": "C:/work", "stale": False}}
    text = m.call_tool("cc_where", {"repo": "C:/work", "project": "p1"}, FakeHttp(busy))
    assert text.startswith("worktree")
    text = m.call_tool("cc_where", {"needs_conversation": True, "context_tokens": 90000}, FakeHttp({}))
    assert text.startswith("new_chat")
