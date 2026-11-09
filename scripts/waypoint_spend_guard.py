"""Standalone Claude Code PreToolUse guard enforcing zero extra paid spend.

Blocks the same explicit paid routes as the legacy self_heal_hooks.py:

1. Truthy paid options anywhere in nested tool arguments:
   allowPaid, paid, paidFallback, enableExtraUsage, extraUsage,
   useG1Credits (including underscore/hyphen variants).
2. Positive extraPaidSpendGbp, paidBudget or maxPaidSpend values.
3. Explicit paid route selections:
   paid, paid_fallback, extra_usage, g1_credits, anthropic_api,
   openai_api, gemini_paid.
4. Raw OpenAI/Anthropic CLI or API endpoint commands.
5. Inline paid assignments and paid command-line flags.

Explicit false/zero values are allowed.

No network, subprocess, filesystem, logging or external dependencies.
Malformed, oversize or unreadable input fails closed (blocks), like the old
RobWorkflow hook (audit 1.1, 5 Nov 2026). A block prints JSON and exits 2.
An allowed call prints nothing and exits 0.
"""

from __future__ import annotations

import json
import re
import sys
from typing import TextIO


MAX_INPUT_BYTES = 2 * 1024 * 1024
MALFORMED = "Malformed or unreadable hook event; the spend guard fails closed."

PAID_FIELDS = frozenset({
    "allowpaid",
    "paid",
    "paidfallback",
    "enableextrausage",
    "extrausage",
    "useg1credits",
})

PAID_BUDGET_FIELDS = frozenset({
    "extrapaidspendgbp",
    "paidbudget",
    "maxpaidspend",
})

PAID_ROUTES = frozenset({
    "paid",
    "paid_fallback",
    "extra_usage",
    "g1_credits",
    "anthropic_api",
    "openai_api",
    "gemini_paid",
})

ROUTE_FIELDS = frozenset({
    "route",
    "route_id",
    "provider_route",
    "billing_tier",
})

COMMAND_FIELDS = frozenset({
    "command",
    "commandline",
    "cmd",
    "script",
})


# Preserve the legacy hook's three command-matching expressions.
# Each entry is (compiled regex, human-readable block reason).

RAW_API_PATTERN = re.compile(
    r"""(?i)(?:\b(?:openai|anthropic)(?:\.exe|\.cmd|\.bat|\.ps1)?["']?\s+(?:api\s+)?(?:responses(?:[.]create)?|messages(?:[.]create)?|chat\s+completions)\b|\bpython(?:\.exe)?\s+-m\s+(?:openai|anthropic)\b|https://api\.(?:openai\.com/[^\s"']*(?:responses|chat/completions)|anthropic\.com/[^\s"']*messages))"""
)

INLINE_PAID_PATTERN = re.compile(
    r"""(?i)["']?(?:useG1Credits|allow_paid|paid_fallback|extra_usage)["']?\s*[:=]\s*(?:\$?true|1)\b"""
)

PAID_FLAG_PATTERN = re.compile(
    r"""(?i)(?:--?|/)(?:allow[-_]?paid|paid[-_]?fallback|enable[-_]?extra[-_]?usage|use[-_]?g1[-_]?credits|paid)(?![\w-])(?:\s*[:=]\s*|\s+)?(?P<value>\$?(?:true|false)|[01])?"""
)

BLOCK_PATTERNS = [
    (
        RAW_API_PATTERN,
        "Unapproved raw OpenAI or Anthropic API route.",
    ),
    (
        INLINE_PAID_PATTERN,
        "Explicit paid option enabled in command.",
    ),
    (
        PAID_FLAG_PATTERN,
        "Explicit paid command-line flag enabled.",
    ),
]


def _truthy(value: object) -> bool:
    """Match the legacy hook's paid-value interpretation."""
    if value is True:
        return True

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value > 0

    if isinstance(value, str):
        return value.lower().lstrip("$") in {"true", "yes", "1"}

    return False


def _normalized_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _structured_paid(value: object) -> bool:
    """Detect paid settings recursively within structured arguments."""
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                continue

            normalized = _normalized_key(key)

            if normalized in PAID_FIELDS and _truthy(item):
                return True

            if normalized in PAID_BUDGET_FIELDS and _truthy(item):
                return True

            if (
                key.lower() in ROUTE_FIELDS
                and isinstance(item, str)
                and item.lower() in PAID_ROUTES
            ):
                return True

            if _structured_paid(item):
                return True

    elif isinstance(value, list):
        return any(_structured_paid(item) for item in value)

    return False


def _command_strings(value: object):
    """Yield only command-bearing strings, as in the legacy hook."""
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                continue

            if key.lower() in COMMAND_FIELDS and isinstance(item, str):
                yield item

            elif isinstance(item, (dict, list)):
                yield from _command_strings(item)

    elif isinstance(value, list):
        for item in value:
            yield from _command_strings(item)


def _check_command(command: str) -> tuple[bool, str]:
    """Apply legacy command patterns, including false/zero exceptions."""
    for pattern, reason in BLOCK_PATTERNS:
        for match in pattern.finditer(command):

            if pattern is PAID_FLAG_PATTERN:
                value = (match.group("value") or "true").lower()
                value = value.lstrip("$")

                if value in {"false", "0"}:
                    continue

            return True, reason

    return False, ""


def decide(event: dict) -> tuple[bool, str]:
    """Return (should_block, reason).

    Only a structurally valid PreToolUse event can be allowed.
    Unknown or malformed input is blocked (fail closed).

    This function never intentionally raises an exception.
    """
    try:
        if not isinstance(event, dict):
            return True, MALFORMED

        tool_name = event.get("tool_name")
        tool_input = event.get("tool_input")

        if not isinstance(tool_name, str) or not tool_name:
            return True, MALFORMED

        if not isinstance(tool_input, dict):
            return True, MALFORMED

        if _structured_paid(tool_input):
            return (
                True,
                "Explicit paid option, paid budget or paid provider route blocked.",
            )

        for command in _command_strings(tool_input):
            blocked, reason = _check_command(command)

            if blocked:
                return True, reason

        return False, ""

    except Exception:
        # Fail closed for malformed or unexpected input.
        return True, MALFORMED


def main(stdin: TextIO | None = None) -> int:
    """Read one JSON event and return the Claude Code hook exit status."""
    stream = stdin if stdin is not None else sys.stdin

    try:
        raw = stream.read(MAX_INPUT_BYTES + 1)

        if len(raw) > MAX_INPUT_BYTES:
            blocked, reason = True, "Tool input too large for the spend guard to check (fail closed)."
        else:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8-sig")
            try:
                event = json.loads(raw)
            except ValueError:
                event = None
            blocked, reason = decide(event)

        if not blocked:
            return 0

        response = {
            "decision": "block",
            "reason": reason.replace("\n", " ").replace("\r", " "),
        }

        print(
            json.dumps(
                response,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        # Claude Code shows a PreToolUse hook's stderr (not stdout) to the model when it exits 2.
        print("Blocked by Waypoint spend guard: " + response["reason"], file=sys.stderr)

        return 2

    except Exception:
        print("Blocked by Waypoint spend guard: " + MALFORMED, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
