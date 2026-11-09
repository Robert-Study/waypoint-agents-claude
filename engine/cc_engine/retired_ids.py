"""Names that were removed from the program and may still sit in stored data (read-only guard).

ChatMode (the ChatGPT web loopback bridge) was removed on 5 Nov 2026 (Owner; preference note 161).
The live database may still hold a toggle row, a run-sheet pool or a run setting under the old name.
Readers call :func:`is_retired` and skip such entries; nothing here writes to a database, and a
retired name never maps to ChatGPT Web (that is a separate, manual route: ``chatgpt_web``).

This is the one code file allowed to spell the old name (``test_no_chatmode.py`` allowlist).
"""
from __future__ import annotations

RETIRED_TOGGLE_ID = "chatmode"
RETIRED = frozenset({"chatmode", "chatmode-web"})


def is_retired(ident) -> bool:
    """True for a retired id, or a prefixed form of one (``agent:chatmode``, ``route:chatmode``)."""
    low = str(ident or "").strip().lower()
    if low in RETIRED:
        return True
    prefix, sep, rest = low.partition(":")
    return bool(sep) and rest.split("/", 1)[0] in RETIRED


def drop_retired(mapping):
    """Copy of ``mapping`` without retired keys."""
    return {k: v for k, v in dict(mapping or {}).items() if not is_retired(k)}
