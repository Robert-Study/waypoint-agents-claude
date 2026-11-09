"""Run sheet: pool ticks plus a Frugal/Balanced/Push/Max spend dial -> concrete settings.

``expand(settings)`` input::

    {"preset": "balanced",
     "pools": ["claude_code", "freebuff"] | {"claude_code": True, ...},
     "use_pool_fully": bool | [pools] | {pool: bool},      # optional; ticked pools only
     "auto_resume": bool, "power_off": bool, "merge_resets": bool,   # optional
     "advanced": {"model_ladder": {pool: [...]}, "effort": ..., "max_concurrency": ...,
                  "native_external_ratio": [n, e], "poll_seconds": ...},   # behind the gear
     "sprints": [{"pool": "claude_code", "start_at": "2026-11-04T12:01:00Z"}, ...],  # punch clock
     "efforts": {"claude_code": "high"}}     # optional per-pool effort (Autonomous sliders)

Advanced values must be JSON numbers (or omitted to keep the preset); strings are rejected.
``sprints`` is optional: each entry names a ticked pool and a timezone-aware ISO 8601
``start_at``; valid entries come back unchanged under ``sprints`` (``[]`` when absent) for the
scheduler to honour.

``efforts`` (optional) maps ticked pools to an effort; ``effort`` then comes back as a
``{pool: effort}`` map (untouched pools keep the preset/advanced effort). Without it ``effort``
stays one word, as before.

Pools: ``POOLS`` are the Create run sheet's pools; ``RUN_POOLS`` adds ``antigravity_manual``
(Antigravity as a manual agentic coder: Copy Prompt packet plus a needs-you question, never
unattended), offered from the Autonomous tab, and ``codex`` (``codex exec``, gated on Codex usage
like Claude), which runs only while Codex is switched on in Agents.

Agents switches: ``expand(settings, toggles=None)`` drops every pool and ladder model that
``cc_engine.toggles`` has switched off, whatever the UI sent (``toggles``: a Snapshot, Store or
path; None reads the live engine database). A pool left with no allowed model is dropped.

Unknown top-level keys are ignored (the scheduler may store its own); unknown advanced keys
raise ``ValueError``. Only zero-extra-spend pools exist here: no metered API routes.
"""
from __future__ import annotations

from datetime import datetime

from . import retired_ids

POOLS = ("claude_code", "freebuff", "gemini_app", "chatgpt_web")
# Manual agentic-coder pools offered from Autonomous only (never automated).
MANUAL_POOLS = ("antigravity_manual",)
# Pools offered from Autonomous that run only while switched on in Agents (Codex is off by default).
SWITCHED_POOLS = ("codex",)
RUN_POOLS = POOLS + MANUAL_POOLS + SWITCHED_POOLS
AGY_LADDER = ["gemini-3.8-flash-high"]  # providers.AGY_DEFAULT_MODEL
CODEX_LADDER = ["gpt-6.1-sol"]  # ~/.codex/config.toml default model (4 Nov 2026)
# Pool -> the dispatch route whose Agents switch governs it.
POOL_ROUTE = {"claude_code": "claude_code", "freebuff": "freebuff",
              "gemini_app": "gemini_app", "chatgpt_web": "chatgpt_web", "antigravity_manual": "antigravity_manual", "codex": "codex"}
EFFORTS = ("low", "medium", "high", "xhigh", "max")

# Model ladders climb from cheapest to strongest; failure policy moves one rung at a time.
LADDERS = {
    "frugal": {"claude_code": ["haiku", "sonnet"],
               "freebuff": ["deepseek-chat", "deepseek-reasoner"], "gemini_app": ["gemini-4-argon"],
               "chatgpt_web": ["gpt-6-sol"],
               "antigravity_manual": list(AGY_LADDER), "codex": list(CODEX_LADDER)},
    "balanced": {"claude_code": ["sonnet", "opus"],
                 "freebuff": ["deepseek-chat", "deepseek-reasoner"], "gemini_app": ["gemini-4-argon"],
                 "chatgpt_web": ["gpt-6-sol"],
                 "antigravity_manual": list(AGY_LADDER), "codex": list(CODEX_LADDER)},
    "push": {"claude_code": ["sonnet", "opus"],
             "freebuff": ["deepseek-reasoner"], "gemini_app": ["gemini-4-argon"], "chatgpt_web": ["gpt-6-sol"],
             "antigravity_manual": list(AGY_LADDER), "codex": list(CODEX_LADDER)},
    "max": {"claude_code": ["opus"],
            "freebuff": ["deepseek-reasoner"], "gemini_app": ["gemini-4-argon"], "chatgpt_web": ["gpt-6-sol"],
            "antigravity_manual": list(AGY_LADDER), "codex": list(CODEX_LADDER)},
}

PRESETS = {
    "frugal": {"effort": "low", "max_concurrency": 1, "native_external_ratio": [1, 3],
               "poll_seconds": 900, "use_pools_fully": False},
    "balanced": {"effort": "medium", "max_concurrency": 2, "native_external_ratio": [1, 1],
                 "poll_seconds": 600, "use_pools_fully": False},
    "push": {"effort": "high", "max_concurrency": 4, "native_external_ratio": [2, 1],
             "poll_seconds": 300, "use_pools_fully": True},
    "max": {"effort": "xhigh", "max_concurrency": 6, "native_external_ratio": [3, 1],
            "poll_seconds": 120, "use_pools_fully": True},
}

ADVANCED_KEYS = {"model_ladder", "effort", "max_concurrency", "native_external_ratio", "poll_seconds"}


def _ticked(pools) -> list:
    if pools is None:
        return []
    if isinstance(pools, dict):
        names = [p for p, on in pools.items() if on]
    elif isinstance(pools, (list, tuple, set)):
        names = list(pools)
    else:
        raise ValueError("pools must be a list or a {pool: bool} map")
    bad = [p for p in names if p not in RUN_POOLS]
    if bad:
        raise ValueError(f"unknown pools {bad}; allowed {list(RUN_POOLS)}")
    return [p for p in RUN_POOLS if p in names]


def _iso(value, label) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be an ISO 8601 date-time string")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value)
    except ValueError:
        raise ValueError(f"{label} {value!r} is not an ISO 8601 date-time") from None
    if parsed.tzinfo is None:
        raise ValueError(f"{label} {value!r} needs a timezone (for example Z)")


def _sprints(sprints, ticked) -> list:
    if sprints is None:
        return []
    if not isinstance(sprints, list):
        raise ValueError("sprints must be a list of {pool, start_at}")
    out = []
    for i, row in enumerate(sprints):
        if not isinstance(row, dict):
            raise ValueError(f"sprints[{i}] must be an object with pool and start_at")
        pool = row.get("pool")
        if pool not in RUN_POOLS:
            raise ValueError(f"unknown pool {pool!r} in sprints; allowed {list(RUN_POOLS)}")
        if pool not in ticked:
            raise ValueError(f"sprints[{i}] pool {pool!r} is not ticked")
        _iso(row.get("start_at"), f"sprints[{i}].start_at")
        if row.get("end_at") is not None:
            _iso(row["end_at"], f"sprints[{i}].end_at")
        out.append(dict(row))
    return out


def _bool(value, default):
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError("toggles must be true or false")
    return value


def _switches(toggles):
    from . import toggles as T
    try:
        return T.resolve(toggles)
    except Exception:  # unreadable switches: only the defaults (Codex off) apply
        return T.Snapshot({})


def _without_retired(settings) -> dict:
    """``settings`` minus anything stored under a retired pool name (an old run may still hold one)."""
    out = dict(settings or {})
    pools = out.get("pools")
    if isinstance(pools, dict):
        out["pools"] = retired_ids.drop_retired(pools)
    elif isinstance(pools, (list, tuple, set)):
        out["pools"] = [p for p in pools if not retired_ids.is_retired(p)]
    for key in ("efforts", "use_pool_fully"):
        if isinstance(out.get(key), dict):
            out[key] = retired_ids.drop_retired(out[key])
        elif isinstance(out.get(key), (list, tuple, set)):
            out[key] = [p for p in out[key] if not retired_ids.is_retired(p)]
    if isinstance(out.get("sprints"), list):
        out["sprints"] = [r for r in out["sprints"]
                          if not (isinstance(r, dict) and retired_ids.is_retired(r.get("pool")))]
    adv = out.get("advanced")
    if isinstance(adv, dict) and isinstance(adv.get("model_ladder"), dict):
        out["advanced"] = dict(adv, model_ladder=retired_ids.drop_retired(adv["model_ladder"]))
    return out


def expand(settings, toggles=None) -> dict:
    settings = _without_retired(settings)
    preset_name = settings.get("preset", "balanced")
    if preset_name not in PRESETS:
        raise ValueError(f"unknown preset {preset_name!r}; choose {sorted(PRESETS)}")
    preset = PRESETS[preset_name]
    switches = _switches(toggles)
    asked = _ticked(settings.get("pools"))
    ticked = [p for p in asked if switches.allows(POOL_ROUTE.get(p, p))]

    advanced = settings.get("advanced") or {}
    if not isinstance(advanced, dict):
        raise ValueError("advanced must be an object")
    extra = set(advanced) - ADVANCED_KEYS
    if extra:
        raise ValueError(f"unknown advanced settings {sorted(extra)}")

    ladder = {p: list(LADDERS[preset_name][p]) for p in ticked}
    for pool, models in (advanced.get("model_ladder") or {}).items():
        if pool not in RUN_POOLS:
            raise ValueError(f"unknown pool {pool!r} in model_ladder")
        if not isinstance(models, list) or not models or not all(isinstance(m, str) and m for m in models):
            raise ValueError(f"model_ladder for {pool} must be a non-empty list of model names")
        if pool in ladder:
            ladder[pool] = list(models)
    for pool in list(ticked):  # ladder models switched off in Agents never run
        ladder[pool] = [m for m in ladder[pool] if switches.allows(POOL_ROUTE.get(pool, pool), m)]
        if not ladder[pool]:  # nothing left to run on: the pool is dropped, never defaulted
            del ladder[pool]
            ticked.remove(pool)

    effort = advanced.get("effort", preset["effort"])
    if effort not in EFFORTS:
        raise ValueError(f"effort must be one of {EFFORTS}")
    per_pool = settings.get("efforts")
    if per_pool is not None:
        if not isinstance(per_pool, dict):
            raise ValueError("efforts must be a {pool: effort} map")
        for pool, value in per_pool.items():
            if pool not in RUN_POOLS:
                raise ValueError(f"unknown pool {pool!r} in efforts; allowed {list(RUN_POOLS)}")
            if value not in EFFORTS:
                raise ValueError(f"efforts[{pool}] must be one of {EFFORTS}")
        if per_pool:
            effort = {p: per_pool.get(p, effort) for p in ticked}
    conc = advanced.get("max_concurrency", preset["max_concurrency"])
    if not isinstance(conc, int) or isinstance(conc, bool) or not 1 <= conc <= 32:
        raise ValueError("max_concurrency must be an integer 1-32")
    poll = advanced.get("poll_seconds", preset["poll_seconds"])
    if not isinstance(poll, int) or isinstance(poll, bool) or not 30 <= poll <= 3600:
        raise ValueError("poll_seconds must be an integer 30-3600")
    ratio = advanced.get("native_external_ratio", preset["native_external_ratio"])
    if (not isinstance(ratio, (list, tuple)) or len(ratio) != 2
            or not all(isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in ratio)
            or sum(ratio) == 0):
        raise ValueError("native_external_ratio must be [native, external] non-negative integers")

    fully = settings.get("use_pool_fully")
    if fully is None:
        fully_map = {p: preset["use_pools_fully"] for p in ticked}
    elif isinstance(fully, bool):
        fully_map = {p: fully for p in ticked}
    elif isinstance(fully, dict):
        fully_map = {p: bool(fully.get(p, False)) for p in ticked}
    elif isinstance(fully, (list, tuple, set)):
        fully_map = {p: p in fully for p in ticked}
    else:
        raise ValueError("use_pool_fully must be a bool, a list or a map")
    sprints = [s for s in _sprints(settings.get("sprints"), asked) if s["pool"] in ticked]

    return {
        "model_ladder": ladder,
        "effort": effort,
        "max_concurrency": conc,
        "native_external_ratio": list(ratio),
        "poll_seconds": poll,
        "use_pool_fully": fully_map,
        "auto_resume": _bool(settings.get("auto_resume"), True),
        "power_off": _bool(settings.get("power_off"), False),
        "merge_resets": _bool(settings.get("merge_resets"), True),
        "sprints": sprints,
    }
