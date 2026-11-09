"""One switch per agent, route and model (Owner, 4 Nov 2026).

"enable and disable things in 1 tab and then once they are enabled or disabled then throughout the
entire program they remain greyed out and won't be called as a subagent."

This module is the single source of truth. Everything that can call an agent asks it:
dispatch, the scheduler tick, the failure ladder, the Run sheet, the MCP/Router tools, the
Super-Agent ticket server and the plugin (``GET /api/toggles``).

Ids
- Routes / agents (one switch each, several names): ``claude_code``, ``freebuff``,
  ``gemini_app``, ``chatgpt_web``, ``gemini_studio``, ``groq``, ``antigravity_manual``, ``codex``, ``manual``.
  Aliases such as ``agent:codex``, ``runner:claude`` or the pool name ``claude`` resolve to the
  same switch (``canonical``).
- Registry model ids from ``GET /api/models`` (``subagent:claude/opus-5.5``,
  ``chat:freebuff/...``, ``agent:...``, ``manual:...``). A route switched off greys every
  ``subagent:``/``agent:``/``manual:`` id under it.
- Codex is off until Owner switches it on; every other id is on by default.

Storage: table ``toggles(id, enabled, updated_at, updated_by)`` in the engine database
(``data/cc.sqlite``; ``db=`` takes a ``Store``, a path, or None for ``cc_engine.default_db_path``).
On first use the old Models-tab file (``models_disabled.json`` beside the database) is migrated
once; afterwards the table wins. A missing database reads the defaults and is never created by a
read.

API
- ``is_enabled(id, db=None) -> bool``
- ``set_enabled(id, on, actor, db=None) -> row``
- ``list(db=None, registry=None) -> [row]``
- ``refusal(route, model=None, db=None) -> str | None`` (plain-English reason, for callers)
- ``Snapshot`` (``snapshot(db)``): the same answers from one read, for loops.
- HTTP: ``GET /api/toggles``, ``POST /api/toggles/set {id, enabled}`` (owner only).
"""
from __future__ import annotations

import builtins
import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import retired_ids

SCHEMA = """
CREATE TABLE IF NOT EXISTS toggles(id TEXT PRIMARY KEY, enabled INTEGER NOT NULL,
                                   updated_at TEXT, updated_by TEXT);
CREATE TABLE IF NOT EXISTS toggles_meta(key TEXT PRIMARY KEY, value TEXT);
"""
MIGRATED_KEY = "migrated_models_disabled"
MODELS_STATE_NAME = "models_disabled.json"
ID_RE = re.compile(r"^[A-Za-z0-9_:./@+\-]{1,200}$")

# canonical id -> label, kind, default, aliases (every name the program uses for it), runner.
GROUPS = {
    "claude_code": {"label": "Claude Code", "kind": "agent", "default": True, "runner": "claude",
                    "aliases": ("agent:claude_code", "route:claude_code", "runner:claude", "claude",
                                "claude_max")},
    "codex": {"label": "Codex", "kind": "agent", "default": False, "runner": "codex",
              "aliases": ("agent:codex", "route:codex", "runner:codex", "codex_cli",
                          "delegate_codex_metered_subagent")},
    "freebuff": {"label": "Freebuff", "kind": "agent", "default": True, "runner": "freebuff",
                 "aliases": ("agent:freebuff", "route:freebuff", "runner:freebuff", "deepseek")},
    "antigravity_manual": {"label": "Antigravity", "kind": "agent", "default": True,
                           "runner": "antigravity",
                           "aliases": ("antigravity", "agent:antigravity", "route:antigravity",
                                       "runner:antigravity", "route:antigravity_manual", "agy",
                                       "agy_cli", "route:agy_cli", "runner:agy_cli")},
    "gemini_studio": {"label": "Google AI Studio (Gemini API)", "kind": "route", "default": True,
                      "runner": "gemini",
                      "aliases": ("gemini", "route:gemini", "runner:gemini", "route:gemini_studio")},
    "groq": {"label": "Groq LPU", "kind": "route", "default": True, "runner": "groq",
             "aliases": ("route:groq", "runner:groq")},
    "copilot": {"label": "GitHub Copilot", "kind": "route", "default": True, "runner": "copilot",
                "aliases": ("agent:copilot", "route:copilot", "runner:copilot", "github_copilot",
                            "gh_copilot")},
    "deepseek_dsh": {"label": "Built-in agent (dsh)", "kind": "route", "default": True, "runner": "dsh",
                     "aliases": ("agent:deepseek_dsh", "route:deepseek_dsh", "runner:dsh", "dsh",
                                 "runner:deepseek_dsh")},
    "gemini_app": {"label": "Gemini App", "kind": "route", "default": True, "runner": None,
                   "aliases": ("manual:gemini_app", "route:gemini_app")},
    "chatgpt_web": {"label": "ChatGPT Web", "kind": "route", "default": True, "runner": None,
                    "aliases": ("manual:chatgpt_web", "route:chatgpt_web", "chatgpt")},
    "manual": {"label": "Manual", "kind": "route", "default": True, "runner": None,
               "aliases": ("manual:manual", "route:manual")},
}
ROUTE_IDS = ("claude_code", "freebuff", "gemini_app", "chatgpt_web", "gemini_studio", "groq",
             "antigravity_manual", "codex", "copilot", "deepseek_dsh", "manual")
AGENT_IDS = ("claude_code", "codex", "freebuff", "antigravity")
ALIASES = {a: cid for cid, g in GROUPS.items() for a in g["aliases"]}
# Registry ``chat:<provider>/...`` providers -> route switch (for model matching only).
PROVIDER_GROUP = {"anthropic": "claude_code", "openai-codex": "codex",
                  "freebuff": "freebuff", "google": "gemini_studio", "groq": "groq",
                  "antigravity": "antigravity_manual",
                  "github-copilot": "copilot"}
CASCADE_KINDS = ("subagent", "agent", "manual")
# ``<prefix>:<switch>`` names any caller may use for a route/agent switch.
ALIAS_PREFIXES = ("route", "runner", "agent", "manual", "pool")

_LOCK = threading.Lock()
_READY: set = set()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical(ident) -> str:
    s = str(ident or "").strip()
    low = s.lower()
    if low in GROUPS:
        return low
    if low in ALIASES:
        return ALIASES[low]
    prefix, sep, rest = low.partition(":")
    if sep and prefix in ALIAS_PREFIXES and "/" not in rest:  # route:x, runner:x, agent:x, manual:x
        if rest in GROUPS:
            return rest
        if rest in ALIASES:
            return ALIASES[rest]
        if ("runner:" + rest) in ALIASES:
            return ALIASES["runner:" + rest]
    return s


def _group_of(ident):
    """Route switch an id lives under (``subagent:claude/x`` -> ``claude_code``), else None."""
    cid = canonical(ident)
    if cid in GROUPS:
        return cid
    kind, sep, rest = str(ident).partition(":")
    if not sep:
        return None
    head = rest.split("/", 1)[0].lower()
    if kind == "chat":
        return PROVIDER_GROUP.get(head)
    return canonical(head) if canonical(head) in GROUPS else ALIASES.get("runner:" + head)


# ---------------------------------------------------------------- database
def db_path(db=None) -> Path:
    if db is None:
        from . import default_db_path
        return Path(default_db_path())
    if hasattr(db, "path"):
        return Path(db.path)
    return Path(db)


def _connect(path: Path):
    con = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    con.execute("PRAGMA busy_timeout=30000")
    return con


def _ensure(con, path: Path):
    key = str(path.resolve())
    if key in _READY:
        return
    with _LOCK:
        con.executescript(SCHEMA)
        _migrate(con, path)
        _READY.add(key)


def _migrate(con, path: Path):
    """Copy the Models tab's old ``models_disabled.json`` into the table, once."""
    if con.execute("SELECT 1 FROM toggles_meta WHERE key=?", (MIGRATED_KEY,)).fetchone():
        return
    ids = []
    try:
        data = json.loads((path.parent / MODELS_STATE_NAME).read_text(encoding="utf-8"))
        ids = [str(i) for i in (data.get("disabled") or []) if isinstance(i, str) and ID_RE.match(i)]
    except (OSError, ValueError, AttributeError):
        ids = []
    con.execute("BEGIN IMMEDIATE")
    try:
        for i in ids:
            con.execute("INSERT OR IGNORE INTO toggles(id, enabled, updated_at, updated_by) VALUES(?,?,?,?)",
                        (canonical(i), 0, _now(), "migration:models_disabled.json"))
        con.execute("INSERT OR REPLACE INTO toggles_meta(key, value) VALUES(?,?)",
                    (MIGRATED_KEY, json.dumps({"at": _now(), "ids": ids})))
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")


def _rows(db=None) -> dict:
    """``{id: {enabled, updated_at, updated_by}}`` as stored. A missing database is empty."""
    path = db_path(db)
    if not path.exists():
        return {}
    con = _connect(path)
    try:
        try:
            _ensure(con, path)
        except sqlite3.OperationalError:  # read-only or locked: read what is there
            pass
        try:
            got = con.execute("SELECT id, enabled, updated_at, updated_by FROM toggles").fetchall()
        except sqlite3.OperationalError:
            return {}
        # A stored row for a retired id (see retired_ids) is ignored: it never greys or enables anything.
        return {r[0]: {"enabled": bool(r[1]), "updated_at": r[2], "updated_by": r[3]}
                for r in got if not retired_ids.is_retired(r[0])}
    finally:
        con.close()


# ---------------------------------------------------------------- model names
_FACTS = None


def _aliases(model):
    """Lower-case names ``model`` is known by (registry aliases when ``cc_engine.models`` loads)."""
    name = str(model or "").strip().lower()
    out = {name}
    try:
        import importlib
        mod = importlib.import_module("cc_engine.models")
        global _FACTS
        if _FACTS is None:
            _FACTS = mod.load_facts()
        out |= {str(a).lower() for a in mod.aliases_for(_FACTS, name)}
    except Exception:  # registry absent or broken: exact names only
        pass
    return out


def _model_part(ident):
    """``subagent:claude/opus-5.5`` -> ``opus-5.5``; ``chat:freebuff/deepseek/x`` -> ``deepseek/x``."""
    kind, sep, rest = str(ident).partition(":")
    if not sep or "/" not in rest:
        return None
    return rest.split("/", 1)[1]


class Snapshot:
    """Every answer from one read of the table (use inside loops)."""

    def __init__(self, rows=None):
        self.rows = dict(rows or {})

    def explicit(self, ident):
        r = self.rows.get(canonical(ident))
        if r is None and canonical(ident) != str(ident):
            r = self.rows.get(str(ident))
        return None if r is None else r["enabled"]

    def is_enabled(self, ident) -> bool:
        cid = canonical(ident)
        if cid in GROUPS:
            got = self.explicit(cid)
            return GROUPS[cid]["default"] if got is None else got
        kind = str(ident).partition(":")[0]
        group = _group_of(ident)
        if group and kind in CASCADE_KINDS and not self.is_enabled(group):
            return False
        got = self.explicit(ident)
        return True if got is None else got

    def disabled_models(self):
        return [i for i, r in self.rows.items() if not r["enabled"] and i not in GROUPS]

    def refusal(self, route=None, model=None):
        """Plain-English reason ``model`` on ``route`` may not run, else None."""
        group = canonical(route) if route else None
        if group in GROUPS and not self.is_enabled(group):
            return f"{GROUPS[group]['label']} is switched off in Agents"
        if not model:
            return None
        if self.explicit(model) is False or (":" in str(model) and not self.is_enabled(model)):
            return f"{model} is switched off in Agents"
        names = None
        for ident in self.disabled_models():
            own = _group_of(ident)
            if group in GROUPS and own and own != group:
                continue
            part = _model_part(ident)
            if part is None:
                if str(ident).lower() != str(model).lower():
                    continue
                return f"{model} is switched off in Agents"
            if names is None:
                names = _aliases(model)
            mine = _aliases(part) | {part.lower(), part.lower().split("/")[-1]}
            if names & mine:
                return f"{model} is switched off in Agents"
        return None

    def allows(self, route=None, model=None) -> bool:
        return self.refusal(route, model) is None


def snapshot(db=None) -> Snapshot:
    try:
        return Snapshot(_rows(db))
    except Exception:  # a broken database never makes Codex (default off) look on
        return Snapshot({})


def is_enabled(ident, db=None) -> bool:
    return snapshot(db).is_enabled(ident)


def refusal(route=None, model=None, db=None):
    return snapshot(db).refusal(route, model)


def resolve(toggles=None) -> Snapshot:
    """A ``Snapshot`` from what callers pass: a Snapshot, a Store, a path, or None (live db)."""
    if isinstance(toggles, Snapshot):
        return toggles
    return snapshot(toggles)


def set_enabled(ident, on, actor="owner", db=None) -> dict:
    if not isinstance(ident, str) or not ID_RE.match(ident.strip()):
        raise ValueError("id must be a short agent, route or model id")
    if not isinstance(on, bool):
        raise ValueError("enabled must be true or false")
    if retired_ids.is_retired(ident):
        raise ValueError("that agent was removed")
    cid = canonical(ident.strip())
    path = db_path(db)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = _connect(path)
    try:
        _ensure(con, path)
        con.execute("INSERT INTO toggles(id, enabled, updated_at, updated_by) VALUES(?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled, "
                    "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                    (cid, int(on), _now(), str(actor)))
        if cid in GROUPS:  # one switch: drop rows an older writer stored under an alias
            for a in GROUPS[cid]["aliases"]:
                con.execute("DELETE FROM toggles WHERE id=?", (a,))
    finally:
        con.close()
    return _row_for(cid, snapshot(db))


def _row_for(ident, snap, meta=None):
    meta = meta or {}
    cid = canonical(ident)
    stored = snap.rows.get(cid) or {}
    if cid in GROUPS:
        g = GROUPS[cid]
        return {"id": cid, "label": g["label"], "kind": g["kind"], "enabled": snap.is_enabled(cid),
                "default": g["default"], "aliases": builtins.list(g["aliases"]), "route": cid,
                "updated_at": stored.get("updated_at"), "updated_by": stored.get("updated_by")}
    return {"id": ident, "label": meta.get("label") or ident, "kind": meta.get("kind") or "model",
            "enabled": snap.is_enabled(ident), "default": True, "aliases": [],
            "route": _group_of(ident), "updated_at": stored.get("updated_at"),
            "updated_by": stored.get("updated_by")}


def _registry_rows():
    try:
        import importlib
        mod = importlib.import_module("cc_engine.models")
        return [r for r in mod.build_registry(usage_rows=[]) if isinstance(r, dict) and r.get("id")]
    except Exception:
        return []


def list_toggles(db=None, registry=None) -> list:
    snap = snapshot(db)
    out = [_row_for(cid, snap) for cid in ROUTE_IDS]
    seen = {r["id"] for r in out}
    rows = _registry_rows() if registry is None else registry
    for r in rows:
        rid = r.get("id")
        if not rid or rid in seen or canonical(rid) in seen:
            continue
        seen.add(rid)
        out.append(_row_for(rid, snap, r))
    for rid in sorted(snap.rows):
        if rid not in seen and canonical(rid) not in seen:
            seen.add(rid)
            out.append(_row_for(rid, snap))
    return out


# ``list`` is the brief's name; inside this module use ``builtins.list``.
list = list_toggles  # noqa: A001


def _addons() -> dict:
    """Add-on switches from the settings file (``{"addons": {"brain_learn": false}}``); {} when none."""
    from . import config
    return config.addon_settings()


def _home_workspace():
    """Settings key ``home_workspace`` (env ``WAYPOINT_HOME_WORKSPACE``): where new chats start, or None."""
    from . import config
    return config.home_workspace()


def payload(db=None, registry=None) -> dict:
    rows = list_toggles(db, registry)
    enabled = {r["id"]: r["enabled"] for r in rows}
    for cid, g in GROUPS.items():
        for a in g["aliases"]:
            enabled.setdefault(a, enabled.get(cid, g["default"]))
    return {"toggles": rows, "enabled": enabled, "aliases": dict(ALIASES),
            "disabled": sorted(i for i, on in enabled.items() if not on),
            "defaults_off": sorted(c for c, g in GROUPS.items() if not g["default"]),
            "cascade_kinds": builtins.list(CASCADE_KINDS),
            "addons": _addons(), "home_workspace": _home_workspace()}


# ---------------------------------------------------------------- HTTP
def _http_error(status, body):
    from .api import HttpError  # lazy: api loads this module through EXTENSIONS
    return HttpError(status, body)


def _store(h):
    return getattr(getattr(h, "app", None), "store", None)


def g_toggles(h, q):
    reg = None
    src = getattr(getattr(h, "app", None), "models_sources", None)
    if src is not None:
        try:
            from . import models
            reg = models.build_registry(src, [])
        except Exception:
            reg = None
    return payload(_store(h), reg)


def p_set(h, q):
    if getattr(h, "actor", None) != "owner":
        raise _http_error(403, {"error": "owner_only", "detail": "only Owner may switch agents on or off"})
    body = h._json_body()
    ident, on = body.get("id"), body.get("enabled")
    try:
        row = set_enabled(ident, on, actor=h.actor, db=_store(h))
    except ValueError as e:
        raise _http_error(400, {"error": "bad_request", "detail": str(e)})
    store = _store(h)
    word = "on" if on else "off"
    try:
        store.event(kind="toggles", actor="owner", summary=f"Owner switched {row['label']} {word}.",
                    data={"id": row["id"], "enabled": on})
        store.notify("toggles", {"id": row["id"], "enabled": on})
    except Exception:
        pass
    return {"ok": True, "toggle": row}


ROUTES = {
    "GET /api/toggles": g_toggles,
    "POST /api/toggles/set": p_set,
}
