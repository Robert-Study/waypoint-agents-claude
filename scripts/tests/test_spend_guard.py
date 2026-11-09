"""Tests for the standalone Waypoint zero-spend guard."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import time

import pytest


GUARD_PATH = (
    Path(__file__).resolve().parent.parent
    / "waypoint_spend_guard.py"
)

spec = importlib.util.spec_from_file_location(
    "waypoint_spend_guard",
    GUARD_PATH,
)

assert spec is not None
assert spec.loader is not None

guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def event(command=None, **extra):
    tool_input = dict(extra)

    if command is not None:
        tool_input["command"] = command

    return {
        "tool_name": "Bash",
        "tool_input": tool_input,
    }


# Every command pattern from the original hook is exercised.

BLOCKED_COMMANDS = [
    # Direct OpenAI/Anthropic CLI calls.
    "openai responses.create",
    "openai responses",
    "openai api responses.create",
    "openai chat completions",
    "anthropic messages.create",
    "anthropic messages",
    "anthropic api messages",
    "openai.exe responses.create",
    "anthropic.cmd messages.create",

    # Direct Python module invocation.
    "python -m openai",
    "python -m anthropic",
    "python.exe -m openai",

    # Raw paid API endpoints.
    "curl https://api.openai.com/v1/responses",
    "curl https://api.openai.com/v1/chat/completions",
    "curl https://api.anthropic.com/v1/messages",

    # Inline paid options.
    "useG1Credits:true",
    "allow_paid=true",
    "paid_fallback:1",
    "extra_usage=true",
    '"allow_paid":true',
    "'useG1Credits'=1",

    # Explicit paid CLI flags.
    "--allow-paid",
    "--allow_paid",
    "--paid",
    "--paid-fallback",
    "--paid_fallback",
    "--enable-extra-usage",
    "--use-g1-credits",
    "-paid",
    "/paid",
    "--paid=true",
    "--paid=1",
    "--paid $true",
    "--allow-paid true",
]


@pytest.mark.parametrize("command", BLOCKED_COMMANDS)
def test_spend_guard_blocks_metered_commands(command):
    blocked, reason = guard.decide(event(command))

    assert blocked is True
    assert isinstance(reason, str)
    assert reason
    assert "\n" not in reason


def test_spend_guard_every_block_pattern_is_exercised():
    """Ensure all registered regex patterns have a blocking example."""
    for pattern, reason in guard.BLOCK_PATTERNS:
        matches = [
            command
            for command in BLOCKED_COMMANDS
            if pattern.search(command)
        ]

        assert matches, f"No test example for {pattern.pattern}"

        assert any(
            guard.decide(event(command))[0]
            for command in matches
        )

        assert reason


@pytest.mark.parametrize(
    "field",
    [
        "allowPaid",
        "allow_paid",
        "allow-paid",
        "paid",
        "paidFallback",
        "paid_fallback",
        "enableExtraUsage",
        "enable_extra_usage",
        "extraUsage",
        "extra_usage",
        "useG1Credits",
        "use_g1_credits",
    ],
)
def test_spend_guard_blocks_paid_structured_fields(field):
    blocked, reason = guard.decide(
        event(options={field: True})
    )

    assert blocked is True
    assert reason


@pytest.mark.parametrize(
    "field",
    [
        "extraPaidSpendGbp",
        "extra_paid_spend_gbp",
        "paidBudget",
        "paid_budget",
        "maxPaidSpend",
        "max_paid_spend",
    ],
)
def test_spend_guard_blocks_positive_paid_budgets(field):
    blocked, _ = guard.decide(
        event(config={field: 1})
    )

    assert blocked is True


@pytest.mark.parametrize(
    "route",
    [
        "paid",
        "paid_fallback",
        "extra_usage",
        "g1_credits",
        "anthropic_api",
        "openai_api",
        "gemini_paid",
    ],
)
def test_spend_guard_blocks_paid_provider_routes(route):
    blocked, _ = guard.decide(
        event(settings={"provider_route": route})
    )

    assert blocked is True


@pytest.mark.parametrize(
    "field",
    [
        "route",
        "route_id",
        "provider_route",
        "billing_tier",
    ],
)
def test_spend_guard_blocks_all_paid_route_fields(field):
    blocked, _ = guard.decide(
        event(config={field: "openai_api"})
    )

    assert blocked is True


def test_spend_guard_blocks_nested_paid_configuration():
    payload = event(
        config={
            "providers": [
                {
                    "name": "example",
                    "settings": {
                        "allowPaid": True,
                    },
                }
            ]
        }
    )

    assert guard.decide(payload)[0] is True


def test_spend_guard_blocks_nested_command_fields():
    payload = event(
        config={
            "steps": [
                {
                    "script": "python -m anthropic",
                }
            ]
        }
    )

    assert guard.decide(payload)[0] is True


def test_spend_guard_blocks_commandline_and_cmd():
    for key in ("command", "commandline", "cmd", "script"):
        payload = {
            "tool_name": "Bash",
            "tool_input": {
                key: "--allow-paid",
            },
        }

        assert guard.decide(payload)[0] is True


def test_spend_guard_allows_git_status():
    blocked, reason = guard.decide(
        event("git status")
    )

    assert blocked is False
    assert reason == ""


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git diff",
        "pytest -q",
        "python --version",
        "python -m pytest",
        "npm test",
        "echo hello",
        "echo allow_paid",
        "--paid=false",
        "--paid=0",
        "--paid false",
        "--paid $false",
        "--allow-paid=false",
        "--paid-fallback=0",
        "--enable-extra-usage false",
        "--use-g1-credits=0",
        "echo https://example.com/v1/responses",
    ],
)
def test_spend_guard_allows_safe_commands(command):
    blocked, reason = guard.decide(event(command))

    assert blocked is False
    assert reason == ""


@pytest.mark.parametrize(
    "value",
    [
        False,
        0,
        0.0,
        "false",
        "False",
        "0",
        None,
        "",
    ],
)
def test_spend_guard_allows_disabled_paid_options(value):
    payload = event(
        config={
            "allowPaid": value,
            "extraPaidSpendGbp": 0,
        }
    )

    assert guard.decide(payload) == (False, "")


def test_spend_guard_allows_zero_paid_budget():
    assert guard.decide(
        event(
            config={
                "extra_paid_spend_gbp": 0,
                "paid_budget": 0,
                "max_paid_spend": 0,
            }
        )
    ) == (False, "")


def test_spend_guard_allows_entitled_provider_route():
    assert guard.decide(
        event(
            config={
                "route": "claude_subscription",
                "allowPaid": False,
            }
        )
    ) == (False, "")


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "",
        [],
        42,
        {},
        {"tool_name": "Bash"},
        {"tool_input": {"command": "--paid"}},
        {"tool_name": None, "tool_input": {}},
        {"tool_name": "Bash", "tool_input": None},
        {"tool_name": "Bash", "tool_input": []},
        {"tool_name": "", "tool_input": {}},
    ],
)
def test_spend_guard_malformed_events_block(payload):
    assert guard.decide(payload) == (True, guard.MALFORMED)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "{",
        "{broken",
        "not JSON",
        "null",
        "[]",
        "42",
        '{"tool_name":"Bash"}',
        '{"tool_name":"Bash","tool_input":null}',
        '{"tool_name":"Bash","tool_input":[]}',
    ],
)
def test_spend_guard_main_malformed_json_blocks(raw, capsys):
    status = guard.main(io.StringIO(raw))

    assert status == 2
    assert json.loads(capsys.readouterr().out)["decision"] == "block"


def test_spend_guard_main_allows_git_status(capsys):
    raw = json.dumps(event("git status"))

    status = guard.main(io.StringIO(raw))

    assert status == 0
    assert capsys.readouterr().out == ""


def test_spend_guard_main_blocks_paid_command(capsys):
    raw = json.dumps(event("--allow-paid"))

    status = guard.main(io.StringIO(raw))
    captured = capsys.readouterr()

    assert status == 2

    result = json.loads(captured.out)

    assert result["decision"] == "block"
    assert isinstance(result["reason"], str)
    assert result["reason"]
    assert "\n" not in result["reason"]
    assert captured.out.count("\n") == 1
    assert captured.err.startswith("Blocked by Waypoint spend guard: ") and result["reason"] in captured.err


def test_spend_guard_main_blocks_raw_api(capsys):
    raw = json.dumps(
        event("curl https://api.openai.com/v1/responses")
    )

    status = guard.main(io.StringIO(raw))

    assert status == 2

    result = json.loads(capsys.readouterr().out)

    assert result["decision"] == "block"


def test_spend_guard_main_accepts_bytes(capsys):
    raw = json.dumps(
        event("--paid=true")
    ).encode("utf-8")

    status = guard.main(io.BytesIO(raw))

    assert status == 2
    assert json.loads(
        capsys.readouterr().out
    )["decision"] == "block"


def test_spend_guard_main_oversized_input_blocks(capsys):
    raw = "x" * (guard.MAX_INPUT_BYTES + 1)

    status = guard.main(io.StringIO(raw))

    assert status == 2
    assert json.loads(capsys.readouterr().out)["decision"] == "block"


def test_spend_guard_decide_1000_calls_under_one_second():
    safe = event("git status")

    blocked = event(
        config={
            "providers": [
                {
                    "route": "openai_api",
                }
            ]
        }
    )

    started = time.perf_counter()

    for _ in range(500):
        assert guard.decide(safe) == (False, "")

    for _ in range(500):
        assert guard.decide(blocked)[0] is True

    elapsed = time.perf_counter() - started

    assert elapsed < 1.0, (
        f"1000 decide() calls took {elapsed:.4f}s"
    )
