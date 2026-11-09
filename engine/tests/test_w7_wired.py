"""W7 wired: a node's own return packet reaches the next dispatched prompt; a sibling's never does."""
from cc_engine import loops
from cc_engine import scheduler as S

from test_scheduler import make_env

SETTINGS = {"model_ladder": {"claude_code": ["sonnet", "opus"]}, "effort": "high"}
X = {"pool": "claude_code", "model": "sonnet", "effort": "high"}


def _dispatch(env, nid):
    d = env.new_dispatcher(toggles_db=env.store)
    node = S.get_node(env.store, "p1", nid)
    out = d.dispatch(node, SETTINGS, {"id": "p1", "title": "Proj", "brief": "Brief.", "cwd": None})
    from pathlib import Path
    return out, (Path(out["job"]) / "prompt.md").read_text(encoding="utf-8")


def test_w7_wired_returned_packet_in_next_prompt(tmp_path):
    env = make_env(tmp_path, [("A", [], "error", dict(X))])
    node = S.get_node(env.store, "p1", "A")
    out = loops.on_failed_check(env.store, "p1", node, SETTINGS, verdict="fail",
                                reason="test_auth_redirect failed", evidence="auth.py:42 assert 302 == 200")
    text = loops.packet_text(out["packet"])
    _, prompt = _dispatch(env, "A")
    assert text in prompt
    assert "Returned for rework" in prompt


def test_w7_wired_no_packet_no_rework_section(tmp_path):
    env = make_env(tmp_path, [("A", [], "not_started", dict(X))])
    _, prompt = _dispatch(env, "A")
    assert "Returned for rework" not in prompt


def test_w7_wired_sibling_output_never_in_prompt(tmp_path):
    env = make_env(tmp_path, [("A", [], "error", dict(X)), ("B", [], "error", dict(X))])
    b = S.get_node(env.store, "p1", "B")
    loops.on_failed_check(env.store, "p1", b, SETTINGS, verdict="fail", reason="SIBLING-SECRET-REASON",
                          evidence="SIBLING-SECRET-EVIDENCE")
    a = S.get_node(env.store, "p1", "A")
    loops.on_failed_check(env.store, "p1", a, SETTINGS, verdict="fail", reason="own reason", evidence="own ev")
    _, prompt = _dispatch(env, "A")
    assert "own reason" in prompt
    assert "SIBLING-SECRET" not in prompt


def test_w7_wired_path_db_also_works(tmp_path):
    env = make_env(tmp_path, [("A", [], "error", dict(X))])
    node = S.get_node(env.store, "p1", "A")
    loops.on_failed_check(env.store, "p1", node, SETTINGS, verdict="fail", reason="via path", evidence="e")
    d = env.new_dispatcher(toggles_db=env.db)
    out = d.dispatch(node, SETTINGS, {"id": "p1", "title": "Proj", "brief": "B", "cwd": None})
    from pathlib import Path
    assert "via path" in (Path(out["job"]) / "prompt.md").read_text(encoding="utf-8")
