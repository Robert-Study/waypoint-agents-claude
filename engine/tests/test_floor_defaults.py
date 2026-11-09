from cc_engine import agents, pick, scheduler


def _row(pool, left):
    return {"pool": pool, "remaining_percent": left, "status": "FRESH", "resets_at": None}


def test_default_floors_map():
    assert pick.floor_for("claude_5h", 9) == 0 and pick.floor_for("claude_week", 9) == 0
    assert pick.floor_for("codex_5h", 9) == 20 and pick.floor_for("codex_week", 9) == 20
    assert pick.floor_for("antigravity_week", 9) == 0
    assert pick.floor_for("chat_web", 9) == 9


def test_scheduler_gate_uses_pool_defaults_and_explicit_wins():
    now = 1.0
    assert scheduler.reserve_percent({}) is None
    assert scheduler.reserve_percent({"reserve_percent": 5}) == 5.0
    claude = {"claude_5h": _row("claude_5h", 1), "claude_week": _row("claude_week", 1)}
    assert scheduler.usage_gate("claude_code", claude, None, now)[0] == "open"
    assert scheduler.usage_gate("claude_code", claude, 10.0, now)[0] == "low"
    zero = {"claude_5h": _row("claude_5h", 0), "claude_week": _row("claude_week", 50)}
    assert scheduler.usage_gate("claude_code", zero, None, now)[0] == "low"
    codex = {"codex_5h": _row("codex_5h", 19), "codex_week": _row("codex_week", 80)}
    assert scheduler.usage_gate("codex", codex, None, now)[0] == "low"
    codex["codex_5h"] = _row("codex_5h", 25)
    assert scheduler.usage_gate("codex", codex, None, now)[0] == "open"


def test_agent_send_floor_hit():
    assert agents.usage_floor_hit({"usage": [_row("claude_5h", 1)]}) is None
    assert agents.usage_floor_hit({"usage": [_row("claude_5h", 0)]}) is not None
    assert agents.usage_floor_hit({"usage": [_row("codex_5h", 20)]})[1] == 20
