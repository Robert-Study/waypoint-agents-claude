"""Google AI Studio (Gemini API) key pool: list the slots, show which one dsh uses, switch it.

This is the free Gemini API key pool. It is NOT Antigravity (``agy``), which signs in on its own
and has its own usage pools; nothing here touches Antigravity.

Same mechanism as the existing tooling (read 4 Nov 2026):
- The pool is ``GEMINI_API_KEY_A``..``_E`` and ``_1``..``_10``, then plain ``GEMINI_API_KEY``, from
  the process environment over the workspace ``.env`` (cc_engine.config.ENV_FILE); the Windows user registry
  ``GEMINI_API_KEY`` only when nothing else is set. That is ``gemini_subagent.py``'s
  ``resolve_api_key_pool`` order; it rotates through the pool on 429s by itself.
- dsh's ``google`` provider reads ``apiKeyEnv: GEMINI_API_KEY``, which ``setup_deepseek_harness.py``
  writes as ``refs.GEMINI_API_KEY`` in ``~/.dsh/.credentials.yaml`` (from ``GEMINI_API_KEY_A`` first).
  Switching rewrites exactly that one line with another slot's key and leaves every other byte alone.

Key values are compared in memory only. Nothing returned, raised or logged ever contains one: slots
are named by letter and env-var name. A switch is refused when dsh's current key is not in the pool
(it would be lost) or when the credentials file or its ``refs.GEMINI_API_KEY`` line is missing.
"""
from __future__ import annotations

import hmac
import os
import re
from pathlib import Path
from . import config

ENV_FILE = Path(config.ENV_FILE)
CREDENTIALS = Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".dsh" / ".credentials.yaml"
SUFFIXES = ("A", "B", "C", "D", "E") + tuple(str(i) for i in range(1, 11))
PLAIN = "GEMINI_API_KEY"
DSH_TARGET = "~/.dsh/.credentials.yaml refs.GEMINI_API_KEY (dsh's Google AI Studio provider)"
SEPARATE_FROM = ("Antigravity (agy) is a different product with its own sign-in and usage pools; "
                 "this pool is only the Google AI Studio Gemini API keys.")
RESTART_HINT = "dsh reads its credentials when it starts: restart dsh for a switch to reach the chat picker."
_REF_LINE = re.compile(r"^(?P<indent>[ \t]+)GEMINI_API_KEY:(?P<sp>[ \t]*)(?P<val>.*?)(?P<eol>\r?\n?)$")


class SwitchRefused(ValueError):
    """The switch would lose a key or has nothing to write to."""


def read_env_file(path) -> dict:
    """``KEY=value`` lines, quotes stripped (``setup_deepseek_harness.load_local_env`` rules)."""
    out = {}
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("\"'")
    return out


def _registry_value(name):
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
            val, _ = winreg.QueryValueEx(key, name)
            return str(val).strip() or None
    except Exception:
        return None


def _pool(env_file=None, environ=None, registry_reader=None):
    """``[(slot, env_name, value, origin)]`` in rotation order. Internal: values never leave this module."""
    environ = os.environ if environ is None else environ
    file_vals = read_env_file(env_file or ENV_FILE)
    out = []
    for slot, name in [(s, f"{PLAIN}_{s}") for s in SUFFIXES] + [("Primary", PLAIN)]:
        val, origin = (environ.get(name) or "").strip(), "environment"
        if not val:
            val, origin = (file_vals.get(name) or "").strip(), ".env"
        if val:
            out.append((slot, name, val, origin))
    if not out:
        val = (registry_reader or _registry_value)(PLAIN)
        if val:
            out.append(("Registry", PLAIN, str(val).strip(), "registry"))
    return out


def _unquote(raw):
    raw = raw.strip()
    if " #" in raw and not raw.startswith(("'", '"')):
        raw = raw.split(" #", 1)[0].rstrip()
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1].replace("''", "'")
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        return raw[1:-1]
    return raw


def _quote(value):
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9._\-]*", value):
        return value
    return "'" + value.replace("'", "''") + "'"


def _ref_line(lines):
    """Index and match of the ``GEMINI_API_KEY:`` line inside the top-level ``refs:`` block."""
    in_refs = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line[:1].isspace():
            in_refs = stripped.rstrip() == "refs:"
            continue
        if in_refs:
            m = _REF_LINE.match(line)
            if m:
                return i, m
    return None, None


def _dsh_key(credentials):
    try:
        lines = Path(credentials or CREDENTIALS).read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        return None, "credentials_missing"
    _, m = _ref_line(lines)
    if m is None:
        return None, "no_dsh_key"
    val = _unquote(m.group("val"))
    return (val, "ok") if val else (None, "no_dsh_key")


def _same(a, b):
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def status(env_file=None, credentials=None, environ=None, registry_reader=None) -> dict:
    pool = _pool(env_file, environ, registry_reader)
    current, state = _dsh_key(credentials)
    slots, first_by_value, active = [], [], None
    for slot, name, val, origin in pool:
        same_as = next((s for s, v in first_by_value if _same(v, val)), None)
        if same_as is None:
            first_by_value.append((slot, val))
        is_active = bool(current) and same_as is None and active is None and _same(val, current)
        if is_active:
            active = slot
        slots.append({"slot": slot, "label": "Key " + slot if len(slot) <= 2 else slot, "env": name,
                      "present": True, "active": is_active, "same_as": same_as, "from": origin})
    if state == "ok":
        state = "matched" if active else "not_in_pool"
    return {
        "provider": "Google AI Studio (Gemini API)",
        "separate_from": SEPARATE_FROM,
        "slots": slots,
        "active": active,
        "active_state": state,
        "dsh_target": DSH_TARGET,
        "subagent_rotation": [s["slot"] for s in slots if s["same_as"] is None],
        "rotation_note": "The Gemini subagent (gemini_subagent.py) starts at the first slot and moves to the next on a 429.",
        "restart_hint": RESTART_HINT,
        "source": f"{Path(env_file or ENV_FILE).name} + environment (names only); dsh credentials compared in memory",
    }


def switch(slot, env_file=None, credentials=None, environ=None, registry_reader=None) -> dict:
    """Point dsh's ``refs.GEMINI_API_KEY`` at ``slot``'s key. Owner-only at the HTTP layer."""
    pool = _pool(env_file, environ, registry_reader)
    chosen = next((p for p in pool if slot and p[0] == str(slot)), None)
    if chosen is None:
        raise ValueError(f"no Gemini API key slot named {str(slot)[:20]!r}; slots: "
                         + ", ".join(p[0] for p in pool))
    path = Path(credentials or CREDENTIALS)
    try:
        raw = path.read_bytes()
    except OSError:
        raise SwitchRefused("dsh's credentials file is missing; run setup_deepseek_harness.py once first")
    text = raw.decode("utf-8")
    lines = text.splitlines(keepends=True)
    idx, m = _ref_line(lines)
    if m is None:
        raise SwitchRefused("dsh's credentials have no refs.GEMINI_API_KEY line to switch")
    current = _unquote(m.group("val"))
    if current and not any(_same(current, p[2]) for p in pool):
        raise SwitchRefused("dsh's current Gemini key is not in the pool; switching would lose it")
    lines[idx] = f"{m.group('indent')}GEMINI_API_KEY:{m.group('sp') or ' '}{_quote(chosen[2])}{m.group('eol')}"
    new = "".join(lines)
    if new != text:
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                f.write(new)
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()
    return status(env_file, credentials, environ, registry_reader)
