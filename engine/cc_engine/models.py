"""Model registry for the Models tab: every agent, subagent and model dsh is configured with.

Discovery (read-only, every request; a missing or broken source just contributes nothing):
- ``~/.dsh/cordis.patch.yml``: the ``llm-pi-ai`` provider blocks that fill dsh's chat model picker
  (Freebuff, GitHub Copilot Student, Google AI Studio, Groq, Codex OAuth, Claude OAuth),
  the ``agent-default-model`` and the ``DSH_SUBAGENT_DENY`` list. ``setup_deepseek_harness.py``
  writes that file, so it is the one place to read.
- ``~/.dsh/.credentials.yaml``: only credential *names* (which refs/records exist) and the GitHub
  Copilot ``availableModelIds`` list, to mark models Owner has no access to. Values are never kept.
- ``waypoint-workspace/tools/dsh_subagent_mcp_server.py`` (source text, not imported): the
  subagent runners the chat can prompt and which take a ``model`` argument.
- ``providers.ADAPTERS``: Meridian's own dispatch routes. Codex is listed as Owner's own agent with
  ``route: null`` and no roles; it is never a subagent or dispatch route.
- ``usage.read_usage()`` (cache only) for remaining % and resets.
- ``model_facts.json`` (curated, every value sourced) for Elo, context window and speed. A provider
  config that states ``contextWindow`` wins. Unknown stays null / "n/a"; nothing is estimated.

Row kinds: ``chat`` (dsh picker models), ``subagent`` (MCP runner models with no picker entry, e.g.
Claude Code CLI and Antigravity), ``agent`` (Meridian routes + Codex), ``manual`` (Copy Prompt).
``roles`` says which dsh picker a row belongs to: ``agent`` (orchestrator table) and/or
``subagent`` (select-all list). Claude appears only as a subagent; Codex in neither.
``strongest`` marks one model per multi-model pool by a fixed rule (highest known Elo, then a sourced
ranking in model_facts.json, then dsh's configured default model, then config order), so it keeps
working as pool lineups change.

v3 (4 Nov): chat rows are the FULL lineup from ``setup_deepseek_harness.py``'s ``PROVIDER_CATALOG``
(read as source text with ``ast``, never imported) merged with the trimmed patch; ``in_picker`` marks
the rows dsh's chat picker shows now. Free chat rows carry ``health`` from ``model_health.py``'s
cache (``{ok, reason, checked_at, stale, replacement}``; ``ok`` null = never checked) and
``strongest`` is the strongest WORKING model (switched-off and freshly failed rows skipped).
On/off goes through ``toggles.py`` when it is importable (local file values migrated into it once);
the local file below stays as the fallback and keeps the derived refusal lists.

Enable/disable lives in ``data/models_disabled.json`` (``$CC_DATA_DIR`` honoured), toggled owner-only.
Besides the disabled ids it carries derived lists so consumers need no registry logic:
``routes[route]`` for Meridian dispatch (``is_disabled``) and ``runners[runner]`` for the dsh MCP
server, each ``{"all": bool, "default": bool, "models": [ids and aliases]}``.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import providers
from . import config

HOME = Path(os.environ.get("USERPROFILE", str(Path.home())))
CORDIS = HOME / ".dsh" / "cordis.patch.yml"
CREDENTIALS = HOME / ".dsh" / ".credentials.yaml"
MCP_SERVER = Path(config.TOOLS_DIR) / "dsh_subagent_mcp_server.py"
FACTS = Path(__file__).with_name("model_facts.json")
SETUP_SCRIPT = Path(config.TOOLS_DIR) / "setup_deepseek_harness.py"
STATE_NAME = "models_disabled.json"
STATE_SCHEMA = "models_disabled.v1"
KINDS = ["chat", "subagent", "agent", "manual"]
EFFORTS = ("low", "medium", "high", "xhigh", "max")

# dsh provider -> Meridian dispatch route / dsh MCP runner that serve the same models.
PROVIDER_ROUTE = {"freebuff": "freebuff"}
PROVIDER_RUNNER = {"freebuff": "freebuff", "groq": "groq", "google": "gemini"}
# Owner (4 Nov): Claude and Codex never as the dsh orchestrator. Claude is a subagent through the
# Claude Code CLI runner; Codex is never a subagent.
NOT_AGENT_PROVIDERS = ("anthropic", "openai-codex")
PROVIDER_POOLS = {"anthropic": ("claude_5h", "claude_week"), "openai-codex": ("codex_5h", "codex_week")}
BANNED_RUNNERS = ()  # 4 Nov: Codex is allowed again; its switch (off by default) decides
RUNNER_LABELS = {"freebuff": "Freebuff", "groq": "Groq LPU",
                 "gemini": "Google AI Studio (Gemini API)", "antigravity": "Antigravity (agy CLI)",
                 "claude": "Claude Code CLI"}
RUNNER_ROUTE = {"claude": "claude_code", "antigravity": "antigravity", "freebuff": "freebuff"}
# Model a runner uses when the call names none (read from each runner's code, 4 Nov 2026).
RUNNER_DEFAULTS = {"freebuff": providers.FREEBUFF_DEFAULT_MODEL,
                   "antigravity": providers.AGY_DEFAULT_MODEL, "gemini": "gemini-3.8-flash",
                   "groq": "openai/gpt-oss-120b"}
ROUTE_DEFAULTS = {"claude_code": "sonnet", "freebuff": providers.FREEBUFF_DEFAULT_MODEL,
                  "antigravity": providers.AGY_DEFAULT_MODEL}

AGENT_ROUTES = {
    "claude_code": {"label": "Claude Code", "provider": "anthropic", "runner": "claude",
                    "pools": ("claude_5h", "claude_week"), "note": "Claude Max subscription through the claude CLI."},
    "freebuff": {"label": "Freebuff", "provider": "freebuff", "runner": "freebuff", "pools": (),
                 "note": "Free pool through the local proxy, DeepSeek V4.1 Flash first."},
    "antigravity": {"label": "Antigravity", "provider": "antigravity", "runner": "antigravity",
                    "pools": ("antigravity_week", "antigravity_gemini_5h", "antigravity_claude_week",
                              "antigravity_claude_5h", "antigravity_argon_week"),
                    "note": "Antigravity (agy CLI), an agentic coder. Not the Gemini API key pool."},
}
MANUAL_ROUTES = {
    "gemini_app": {"label": "Gemini app", "provider": "google", "pools": ("gemini_app",),
                   "note": "Copy Prompt hand-off to the Gemini app; never automated."},
    "chatgpt_web": {"label": "ChatGPT Web", "provider": "openai", "pools": (),
                    "note": "Copy Prompt hand-off to ChatGPT in the browser; never automated."},
    "manual": {"label": "Manual", "provider": "manual", "pools": (),
               "note": "Copy Prompt hand-off for Owner to run anywhere."},
}
CODEX_NOTE = "Codex CLI: a subagent and dispatch route when its switch is on (off by default)."


@dataclass
class Sources:
    cordis: Path = CORDIS
    credentials: Path = CREDENTIALS
    mcp_server: Path = MCP_SERVER
    facts: Path = FACTS
    state: Path | None = None
    env_file: Path | None = None  # Gemini key pool .env (gemini_keys.ENV_FILE when None)
    environ: dict | None = None
    registry_reader: object = None
    extra: dict = field(default_factory=dict)
    # v3: the setup script whose PROVIDER_CATALOG is the FULL chat lineup (the patch is trimmed),
    # the model_health cache, whether GET /api/models may start a background probe, and the
    # toggles module (single source of truth for on/off) when it exists. None = not used.
    lineup: Path | None = None
    health: Path | None = None
    auto_probe: bool = False
    toggles: object = None

    def state_path(self) -> Path:
        return Path(self.state) if self.state else default_state_path()


def default_state_path() -> Path:
    return config.data_dir() / STATE_NAME


def _toggles_module():
    try:
        from . import toggles  # worker-i's single on/off store; optional
    except Exception:
        return None
    return toggles if callable(getattr(toggles, "is_enabled", None)) else None


def default_sources() -> Sources:
    from . import model_health
    return Sources(lineup=SETUP_SCRIPT, health=model_health.default_path(), auto_probe=True,
                   toggles=_toggles_module())


# ---------------------------------------------------------------- tiny YAML subset reader
def _scalar(text):
    t = text.strip()
    if not t:
        return None
    if t[0] in "'\"":
        q = t[0]
        end = t.find(q, 1)
        while q == "'" and end != -1 and t[end + 1:end + 2] == "'":
            end = t.find(q, end + 2)
        body = t[1:end] if end != -1 else t[1:]
        return body.replace("''", "'") if q == "'" else body
    if " #" in t:
        t = t.split(" #", 1)[0].rstrip()
    if t.startswith("[") and t.endswith("]"):
        return [_scalar(p) for p in t[1:-1].split(",") if p.strip()]
    low = t.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "~"):
        return None
    if re.fullmatch(r"-?\d+", t):
        return int(t)
    return t


_KEY = re.compile(r"^(?P<k>'[^']*'|\"[^\"]*\"|[^:#'\"\s][^:#]*?)\s*:(?:\s+(?P<v>.*)|\s*)$")


def parse_yaml(text):
    """Block mappings, sequences, scalars, ``>``/``|`` block scalars and ``[a, b]`` flow lists.

    Enough for dsh's patch and credentials files; anything it cannot read becomes None, never an error.
    """
    rows = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        rows.append([len(raw) - len(raw.lstrip(" ")), raw.strip()])
    pos = 0

    def is_item(st):
        return st == "-" or st.startswith("- ")

    def block(indent):
        nonlocal pos
        if pos >= len(rows) or rows[pos][0] < indent:
            return None
        ind = rows[pos][0]
        return seq(ind) if is_item(rows[pos][1]) else mapping(ind)

    def child(ind, allow_same_seq):
        if pos < len(rows) and rows[pos][0] > ind:
            return block(rows[pos][0])
        if allow_same_seq and pos < len(rows) and rows[pos][0] == ind and is_item(rows[pos][1]):
            return seq(ind)
        return None

    def seq(ind):
        nonlocal pos
        out = []
        while pos < len(rows) and rows[pos][0] == ind and is_item(rows[pos][1]):
            rest = rows[pos][1][1:].strip()
            if not rest:
                pos += 1
                out.append(child(ind, False))
            elif _KEY.match(rest):
                rows[pos] = [ind + 2, rest]  # "- k: v" opens a mapping one level in
                out.append(mapping(ind + 2))
            else:
                pos += 1
                out.append(_scalar(rest))
        return out

    def mapping(ind):
        nonlocal pos
        out = {}
        while pos < len(rows) and rows[pos][0] == ind and not is_item(rows[pos][1]):
            m = _KEY.match(rows[pos][1])
            pos += 1
            if not m:
                while pos < len(rows) and rows[pos][0] > ind:
                    pos += 1
                continue
            key = _scalar(m.group("k"))
            val = (m.group("v") or "").strip()
            if val[:1] in (">", "|") and re.fullmatch(r"[>|][+-]?\d*", val):
                parts = []
                while pos < len(rows) and rows[pos][0] > ind:
                    parts.append(rows[pos][1])
                    pos += 1
                out[key] = (" " if val[0] == ">" else "\n").join(parts)
            elif val:
                out[key] = _scalar(val)
                while pos < len(rows) and rows[pos][0] > ind:  # malformed continuation: skip
                    pos += 1
            else:
                out[key] = child(ind, True)
        return out

    try:
        return block(0)
    except Exception:  # never let a config quirk break the Models tab
        return None


def _read_yaml(path):
    try:
        return parse_yaml(Path(path).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None


def _items(tree):
    """Every patch item at the top level and inside ``- insert:`` lists."""
    for item in tree if isinstance(tree, list) else []:
        if not isinstance(item, dict):
            continue
        yield item
        for sub in item.get("insert") or [] if isinstance(item.get("insert"), list) else []:
            if isinstance(sub, dict):
                yield sub


def _item(tree, item_id):
    return next((i for i in _items(tree) if i.get("id") == item_id), None)


def cordis_providers(tree) -> dict:
    item = _item(tree, "llm-pi-ai") or {}
    prov = ((item.get("config") or {}).get("providers")) if isinstance(item.get("config"), dict) else None
    return {k: v for k, v in prov.items() if isinstance(v, dict)} if isinstance(prov, dict) else {}


def cordis_default(tree):
    item = _item(tree, "agent-default-model") or {}
    cfg = item.get("config") if isinstance(item.get("config"), dict) else {}
    return {"provider": cfg.get("provider"), "model": cfg.get("model")} if cfg.get("model") else None


def cordis_deny(tree) -> set:
    item = _item(tree, "mcp-rob-subagents") or {}
    env = ((item.get("config") or {}).get("env")) if isinstance(item.get("config"), dict) else None
    raw = env.get("DSH_SUBAGENT_DENY") if isinstance(env, dict) else None
    return {s.strip() for s in str(raw or "").split(",") if s.strip()}


# ---------------------------------------------------------------- full lineup (setup script source)
def _literal(node, names):
    """Literal evaluator for the catalog: constants, lists/tuples/dicts, ``**name`` spreads and
    references to earlier module-level literals. Anything else raises ValueError."""
    import ast
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple)):
        vals = [_literal(e, names) for e in node.elts]
        return vals if isinstance(node, ast.List) else tuple(vals)
    if isinstance(node, ast.Dict):
        out = {}
        for k, v in zip(node.keys, node.values):
            if k is None:
                spread = _literal(v, names)
                if not isinstance(spread, dict):
                    raise ValueError("non-dict spread")
                out.update(spread)
            else:
                out[_literal(k, names)] = _literal(v, names)
        return out
    if isinstance(node, ast.Name) and node.id in names:
        return names[node.id]
    raise ValueError(f"not a literal: {type(node).__name__}")


def setup_lineup(path) -> dict:
    """``{provider: cfg}`` from the setup script's ``PROVIDER_CATALOG`` (source text, never imported).

    This is the full chat lineup dsh can be configured with; ``cordis.patch.yml`` holds only the
    trimmed picker set. A missing or unreadable script gives ``{}``.
    """
    import ast
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError, ValueError):
        return {}
    names = {}
    for stmt in tree.body:
        target, value = None, None
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            target, value = stmt.targets[0].id, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
            target, value = stmt.target.id, stmt.value
        if target is None:
            continue
        try:
            names[target] = _literal(value, names)
        except (ValueError, TypeError, KeyError):
            continue
    out = {}
    for item in names.get("PROVIDER_CATALOG") or []:
        if isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[1], dict):
            out[str(item[0])] = item[1]
    return out


def chat_lineup(sources=None) -> dict:
    """Full lineup merged with the generated patch: ``{provider: cfg}`` with every model either
    names (lineup order first, then any patch-only provider/model such as a renamed id)."""
    src = sources or default_sources()
    full = setup_lineup(src.lineup) if src.lineup else {}
    patch = cordis_providers(_read_yaml(src.cordis))
    out = {}
    for prov in list(full) + [p for p in patch if p not in full]:
        base = dict(full.get(prov) or {})
        base.update({k: v for k, v in (patch.get(prov) or {}).items() if k != "models"})
        in_patch = {str(m["id"]): m for m in patch.get(prov, {}).get("models") or []
                    if isinstance(m, dict) and m.get("id")}
        listed = [in_patch.get(str(m.get("id")), m) for m in full.get(prov, {}).get("models") or []
                  if isinstance(m, dict)]
        seen, models_ = set(), []
        for m in listed + list(in_patch.values()):
            if isinstance(m, dict) and m.get("id") and str(m["id"]) not in seen:
                seen.add(str(m["id"]))
                models_.append(m)
        base["models"] = models_
        out[prov] = base
    return out


def credential_names(tree):
    """``(ref names, record names, copilot model ids or None)``; values are dropped here."""
    if not isinstance(tree, dict):
        return set(), set(), None
    refs = set(tree["refs"]) if isinstance(tree.get("refs"), dict) else set()
    recs = tree.get("records") if isinstance(tree.get("records"), dict) else {}
    copilot = None
    gh = recs.get("llm-pi-ai/github-copilot")
    if isinstance(gh, dict) and isinstance(gh.get("payload"), dict):
        ids = gh["payload"].get("availableModelIds")
        if isinstance(ids, list):
            copilot = {str(i) for i in ids if i}
    return refs, set(recs), copilot


# ---------------------------------------------------------------- MCP server source
_QUOTED_MODEL = re.compile(r"'([a-z][a-z0-9]*(?:[-./][a-z0-9]+)+)'")


def mcp_runners(path, deny=()):
    """``{runner: {"tools": [...], "model_param": bool, "models": [quoted ids]}}`` from the server source."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    blocks = {}
    starts = [(m.start(), m.group(1)) for m in re.finditer(r'"name":\s*"([a-z_]+)"', text)]
    for i, (at, name) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(text)
        blocks[name] = text[at:end]
    out = {}
    for tool, runner in re.findall(r'"(delegate_\w+?)":\s*\(\s*"(\w+)"\s*,\s*\d+\s*\)', text):
        if runner in BANNED_RUNNERS or tool in deny or f"runner:{runner}" in deny:
            continue
        block = blocks.get(tool, "")
        found = []
        for mid in _QUOTED_MODEL.findall(block):
            if mid not in found:
                found.append(mid)
        out[runner] = {"tool": tool, "model_param": '"model":' in block, "models": found}
    return out


# ---------------------------------------------------------------- facts
def load_facts(path=None) -> dict:
    try:
        data = json.loads(Path(path or FACTS).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _split_effort(model_id):
    m = re.fullmatch(r"(.+?)-(low|medium|high|xhigh|max)", str(model_id or ""))
    return (m.group(1), m.group(2)) if m else (str(model_id or ""), None)


def fact_for(facts, model_id):
    """``(canonical key, fact dict, effort)`` for a model id or alias, else ``(None, {}, effort)``."""
    base, effort = _split_effort(model_id)
    table = facts.get("models") if isinstance(facts.get("models"), dict) else {}
    for candidate in (str(model_id or "").lower(), base.lower()):
        for key, fact in table.items():
            names = {key.lower()} | {str(a).lower() for a in fact.get("aliases") or []}
            if candidate in names:
                return key, fact, effort if candidate == base.lower() else None
    return None, {}, effort


def aliases_for(facts, model_id):
    key, fact, _ = fact_for(facts, model_id)
    names = {str(model_id).lower()}
    if key:
        names |= {key.lower()} | {str(a).lower() for a in fact.get("aliases") or []}
        names |= {f"{n}-{e}" for n in list(names) for e in EFFORTS if "/" not in n}
    return sorted(names)


def _match_rule(rule, row, canon):
    m = rule.get("match") if isinstance(rule.get("match"), dict) else {}
    if "provider" in m and m["provider"] != row.get("provider"):
        return False
    if "runner" in m and m["runner"] != row.get("runner"):
        return False
    if "kind" in m and m["kind"] != row.get("kind"):
        return False
    if "models" in m and canon not in (m.get("models") or []):
        return False
    return bool(m)


# ---------------------------------------------------------------- usage
def _usage(pools, rows):
    if not pools:
        return None
    mine = [r for r in rows or [] if isinstance(r, dict) and r.get("pool") in pools]
    known = [r for r in mine if isinstance(r.get("remaining_percent"), (int, float))
             and r.get("status") in ("FRESH", "STALE")]
    pick = min(known, key=lambda r: r["remaining_percent"]) if known else None
    if pick is None:
        return {"remaining_percent": None, "resets_at": None, "status": "UNKNOWN",
                "pool": mine[0]["pool"] if mine else pools[0], "label": mine[0].get("label") if mine else None}
    return {"remaining_percent": pick["remaining_percent"], "resets_at": pick.get("resets_at"),
            "status": pick.get("status"), "pool": pick["pool"], "label": pick.get("label")}


def _agy_pools(model_id):
    m = str(model_id or "")
    if "argon" in m:
        return ("antigravity_argon_week",)
    if m.startswith("claude") or "gpt" in m:
        return ("antigravity_claude_week", "antigravity_claude_5h")
    return ("antigravity_week", "antigravity_gemini_5h")


# ---------------------------------------------------------------- registry
def _fmt_ctx(n):
    if not n:
        return None
    if n >= 1000000:
        v = n / (1048576 if n % 1048576 == 0 else 1000000)
        return (f"{v:.1f}".rstrip("0").rstrip(".")) + "M"
    return f"{n // 1024 if n % 1024 == 0 else round(n / 1000)}K"


def _base_row(rid, kind, label, provider, **kw):
    row = {"id": rid, "label": label, "provider": provider, "provider_label": kw.pop("provider_label", None),
           "kind": kind, "model": kw.pop("model", None), "route": kw.pop("route", None),
           "runner": kw.pop("runner", None), "roles": kw.pop("roles", []), "pool": kw.pop("pool", None),
           "enabled": True, "access": kw.pop("access", None), "elo": None,
           "elo_by_effort": {e: "n/a" for e in EFFORTS}, "effort": None, "context_window": None, "speed": None,
           "usage": None, "run_natively": False, "run_natively_reason": None, "note": kw.pop("note", None),
           "strongest": True, "strongest_rule": None, "pool_size": 1, "source": kw.pop("source"),
           "sources": {}, "dsh": kw.pop("dsh", None), "canonical": None, "badge": None,
           "in_picker": False, "health": None}
    row.update(kw)
    return row


def _apply_facts(row, facts, ctx_from_config=None, ctx_source=None):
    canon, fact, effort = fact_for(facts, row["model"]) if row["model"] else (None, {}, None)
    row["canonical"] = canon
    row["effort"] = effort
    if isinstance(fact.get("elo"), int):
        row["elo"] = fact["elo"]
        row["sources"]["elo"] = fact.get("elo_source") or "model_facts.json"
    for e, v in (fact.get("elo_by_effort") or {}).items():
        if e in row["elo_by_effort"] and v:
            row["elo_by_effort"][e] = str(v)
    if isinstance(ctx_from_config, int) and ctx_from_config > 0:
        row["context_window"] = ctx_from_config
        row["sources"]["context_window"] = ctx_source
    elif isinstance(fact.get("context_window"), int):
        row["context_window"] = fact["context_window"]
        row["sources"]["context_window"] = fact.get("context_source") or "model_facts.json"
    if fact.get("speed"):
        row["speed"] = str(fact["speed"])
        row["sources"]["speed"] = fact.get("speed_source") or "model_facts.json"
    for rule in facts.get("run_natively") or []:
        if isinstance(rule, dict) and _match_rule(rule, row, canon):
            row["run_natively"] = True
            row["run_natively_reason"] = rule.get("reason")
            row["sources"]["run_natively"] = rule.get("source")
            break
    for rule in facts.get("notes") or []:
        if isinstance(rule, dict) and _match_rule(rule, row, canon) and not row["note"]:
            row["note"] = rule.get("note")


def _chat_rows(src, facts, tree, creds, runners, usage_rows):
    refs, records, copilot = creds
    out = []
    patch = cordis_providers(tree)
    shown = {(p, str(m.get("id"))) for p, c in patch.items() for m in c.get("models") or [] if isinstance(m, dict)}
    for prov, cfg in chat_lineup(src).items():
        key_env = cfg.get("apiKeyEnv")
        if key_env:
            have = key_env in refs
        else:
            have = f"llm-pi-ai/{prov}" in records
        runner = PROVIDER_RUNNER.get(prov)
        rinfo = runners.get(runner) if runner else None
        for m in cfg.get("models") or []:
            if not isinstance(m, dict) or not m.get("id"):
                continue
            mid = str(m["id"])
            access = have
            if prov == "github-copilot" and copilot is not None:
                access = mid in copilot
            roles = [] if prov in NOT_AGENT_PROVIDERS else ["agent"]
            sub = bool(rinfo) and (rinfo["model_param"] or mid == RUNNER_DEFAULTS.get(runner))
            if sub:
                roles.append("subagent")
            row = _base_row(f"chat:{prov}/{mid}", "chat", str(m.get("name") or mid), prov,
                            provider_label=cfg.get("displayName"), model=mid, route=PROVIDER_ROUTE.get(prov),
                            runner=runner if sub else None, roles=roles, pool=f"provider:{prov}", access=access,
                            source=f"cordis.patch.yml llm-pi-ai providers.{prov}",
                            dsh={"provider": prov, "model": mid})
            # Full lineup on the Models tab; ``in_picker`` = dsh's chat picker shows it right now.
            row["in_picker"] = (prov, mid) in shown
            if (prov, mid) not in shown:
                row["source"] = "setup_deepseek_harness.py PROVIDER_CATALOG (not in the trimmed picker)"
            _apply_facts(row, facts, m.get("contextWindow"), f"cordis.patch.yml providers.{prov} contextWindow")
            row["usage"] = _usage(PROVIDER_POOLS.get(prov), usage_rows)
            out.append(row)
    return out


def _subagent_rows(facts, runners, usage_rows, chat_providers):
    out = []
    for runner, info in runners.items():
        prov_for_runner = next((p for p, r in PROVIDER_RUNNER.items() if r == runner), None)
        if prov_for_runner and prov_for_runner in chat_providers:
            continue  # its models are chat rows with the subagent role
        label = RUNNER_LABELS.get(runner, runner)
        provider = {"claude": "anthropic", "antigravity": "antigravity", "gemini": "google"}.get(runner, runner)
        common = {"route": RUNNER_ROUTE.get(runner), "runner": runner, "roles": ["subagent"],
                  "pool": f"runner:{runner}", "access": None,
                  "source": f"dsh_subagent_mcp_server.py {info['tool']}", "provider_label": label}
        models = info["models"] or ([RUNNER_DEFAULTS[runner]] if runner in RUNNER_DEFAULTS else [])
        if not models:
            row = _base_row(f"subagent:{runner}", "subagent", label, provider, **common)
            row["usage"] = _usage(AGENT_ROUTES.get(RUNNER_ROUTE.get(runner), {}).get("pools"), usage_rows)
            out.append(row)
            continue
        for mid in models:
            canon, fact, effort = fact_for(facts, mid)
            name = fact.get("label") or mid
            if effort:
                name += f" ({effort})"
            row = _base_row(f"subagent:{runner}/{mid}", "subagent", f"{name} · {label}", provider,
                            model=mid, **common)
            _apply_facts(row, facts)
            pools = _agy_pools(mid) if runner == "antigravity" else AGENT_ROUTES.get(
                RUNNER_ROUTE.get(runner), {}).get("pools")
            row["usage"] = _usage(pools, usage_rows)
            out.append(row)
    return out


def _route_rows(usage_rows):
    out = []
    for route in providers.ADAPTERS:
        if route in AGENT_ROUTES:
            a = AGENT_ROUTES[route]
            row = _base_row(f"agent:{route}", "agent", a["label"], a["provider"], route=route,
                            runner=a["runner"],
                            note=a["note"], source=f"providers.ADAPTERS['{route}'] (Meridian dispatch route)")
            row["usage"] = _usage(a["pools"], usage_rows)
            out.append(row)
    codex = _base_row("agent:codex", "agent", "Codex", "openai-codex", note=CODEX_NOTE,
                      source="agents.py Codex agent (Owner's own use; providers.build refuses Codex)")
    codex["run_natively"] = True
    codex["run_natively_reason"] = "Use Codex yourself in its own app or the Agents tab."
    codex["usage"] = _usage(("codex_5h", "codex_week"), usage_rows)
    out.append(codex)
    for route in providers.ADAPTERS:
        if route in MANUAL_ROUTES:
            a = MANUAL_ROUTES[route]
            row = _base_row(f"manual:{route}", "manual", a["label"], a["provider"], route=route, note=a["note"],
                            source=f"providers.ADAPTERS['{route}'] (Copy Prompt hand-off)")
            row["usage"] = _usage(a["pools"], usage_rows)
            out.append(row)
    return out


def _mark_strongest(rows, facts, default):
    pools = {}
    for r in rows:
        if r["kind"] in ("chat", "subagent") and r.get("pool"):
            pools.setdefault(r["pool"], []).append(r)
    rankings = facts.get("pool_rankings") if isinstance(facts.get("pool_rankings"), dict) else {}
    for pool, members in pools.items():
        for r in members:
            r["pool_size"] = len(members)
        usable = [r for r in members if r.get("access") is not False]
        # v3: the strongest WORKING model: skip ones Owner switched off and fresh failed probes.
        working = [r for r in usable if r.get("enabled", True) is not False
                   and not (isinstance(r.get("health"), dict) and r["health"].get("ok") is False
                            and not r["health"].get("stale"))]
        eligible = working or usable or members
        pick, rule = None, None
        with_elo = [r for r in eligible if isinstance(r.get("elo"), int)]
        if with_elo:
            pick = max(with_elo, key=lambda r: r["elo"])
            rule = "highest known Elo in this pool"
        if pick is None:
            ranking = rankings.get(pool) if isinstance(rankings.get(pool), dict) else None
            order = [str(x).lower() for x in (ranking or {}).get("order") or []]
            ranked = [r for r in eligible if (r.get("canonical") or str(r.get("model")).lower()) in order
                      or str(r.get("model")).lower() in order]
            if ranked:
                pick = min(ranked, key=lambda r: min(order.index(n) for n in
                                                     (str(r.get("model")).lower(), (r.get("canonical") or "")) if n in order))
                rule = "sourced ranking: " + str((ranking or {}).get("source") or "model_facts.json")
        if pick is None and default:
            pick = next((r for r in eligible if r.get("dsh") == default), None)
            if pick is not None:
                rule = "dsh's configured default model (no Elo or ranking known)"
        if pick is None:
            pick = eligible[0]
            rule = "first in the configured list (no Elo or ranking known)" if len(members) > 1 else "only model in pool"
        for r in members:
            r["strongest"] = r is pick
            r["strongest_rule"] = rule if r is pick else None


def _attach_health(rows, src):
    """``health`` on free chat rows from the model_health cache (never probes here)."""
    from . import model_health
    cache = model_health.load(src.health) if src.health else {}
    now = datetime.now(timezone.utc)
    for r in rows:
        d = r.get("dsh")
        if r["kind"] != "chat" or not d or d.get("provider") not in model_health.FREE_PROVIDERS:
            continue
        r["health"] = model_health.health_for(cache, d["provider"], d["model"], now) or {
            "ok": None, "reason": None, "checked_at": None, "stale": False, "replacement": None}


def disabled_ids(src, rows) -> set:
    """Ids Owner switched off: toggles.py when present (local file migrated into it once), else
    ``models_disabled.json``."""
    local = load_state(src.state_path())
    mine = {str(i) for i in local.get("disabled") or []}
    tg = src.toggles
    if tg is None:
        return mine
    try:
        if mine and not local.get("migrated_to_toggles"):
            for rid in sorted(mine):
                tg.set_enabled(rid, False)
            with _LOCK:
                fresh = load_state(src.state_path())
                fresh["migrated_to_toggles"] = True
                _write_state(src.state_path(), fresh)
        snap = tg.snapshot() if callable(getattr(tg, "snapshot", None)) else tg  # one read for the loop
        return {r["id"] for r in rows if snap.is_enabled(r["id"]) is False}
    except Exception:
        return mine


def build_registry(sources=None, usage_rows=None) -> list:
    src = sources or default_sources()
    facts = load_facts(src.facts)
    tree = _read_yaml(src.cordis)
    creds = credential_names(_read_yaml(src.credentials))
    runners = mcp_runners(src.mcp_server, cordis_deny(tree))
    chat = _chat_rows(src, facts, tree, creds, runners, usage_rows)
    chat_providers = {r["provider"] for r in chat}
    rows = chat + _subagent_rows(facts, runners, usage_rows, chat_providers) + _route_rows(usage_rows)
    _attach_health(rows, src)
    disabled = disabled_ids(src, rows)
    for r in rows:
        r["enabled"] = r["id"] not in disabled
    _mark_strongest(rows, facts, cordis_default(tree))
    for r in rows:
        bits = []
        if r["elo"]:
            bits.append(f"~{r['elo']}")
        if r["context_window"]:
            bits.append(_fmt_ctx(r["context_window"]))
        if r["speed"]:
            bits.append(r["speed"])
        r["badge"] = " · ".join(bits) or None
    return rows


def lookup(provider, model, sources=None, usage_rows=None):
    """The chat row for dsh's picker selection ``(provider, model)``, for badges; None if unknown."""
    for r in build_registry(sources, usage_rows):
        if r.get("dsh") == {"provider": provider, "model": model}:
            return r
    return None


# ---------------------------------------------------------------- enable / disable state
_LOCK = threading.RLock()


def load_state(path=None) -> dict:
    try:
        data = json.loads(Path(path or default_state_path()).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def _write_state(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    for attempt in range(5):  # Windows: a reader holding the file briefly blocks os.replace
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.05 * (attempt + 1))


def derive(rows, disabled, facts) -> dict:
    """Per-route and per-runner refusal lists for the disabled ids."""
    routes, runners = {}, {}
    by_id = {r["id"]: r for r in rows}

    def entry(table, name):
        return table.setdefault(name, {"all": False, "default": False, "models": []})

    for rid in sorted(disabled):
        r = by_id.get(rid)
        if not r:
            continue
        names = aliases_for(facts, r["model"]) if r["model"] else []
        for table, name, defaults in ((routes, r.get("route"), ROUTE_DEFAULTS),
                                      (runners, r.get("runner"), RUNNER_DEFAULTS)):
            if not name:
                continue
            e = entry(table, name)
            if not r["model"]:
                e["all"] = True
                continue
            e["models"] = sorted(set(e["models"]) | set(names))
            if defaults.get(name) and str(defaults[name]).lower() in names:
                e["default"] = True
    return {"routes": routes, "runners": runners}


def set_enabled(ids, enabled, sources=None, actor="owner", usage_rows=None) -> list:
    src = sources or default_sources()
    ids = [str(i) for i in ids]
    with _LOCK:
        rows = build_registry(src, usage_rows or [])
        known = {r["id"] for r in rows}
        unknown = [i for i in ids if i not in known]
        if unknown:
            raise ValueError("unknown model id: " + ", ".join(unknown[:5]))
        disabled = {r["id"] for r in rows if r["enabled"] is False}
        disabled = (disabled - set(ids)) if enabled else (disabled | set(ids))
        if src.toggles is not None:  # the single source of truth when present
            for i in ids:
                try:
                    src.toggles.set_enabled(i, bool(enabled))
                except Exception:
                    pass
        data = {"schema": STATE_SCHEMA, "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "updated_by": actor, "disabled": sorted(disabled)}
        if src.toggles is not None:
            data["migrated_to_toggles"] = True
        data.update(derive(rows, disabled, load_facts(src.facts)))
        _write_state(src.state_path(), data)  # kept as the fallback store and for is_disabled
    return [r for r in build_registry(src, usage_rows or []) if r["id"] in set(ids)]


def is_disabled(route=None, model=None, runner=None, path=None) -> bool:
    """True when Owner turned this off on the Models tab. Never raises; a bad file means False.

    ``route`` (Meridian dispatch) or ``runner`` (dsh MCP). Pass the ``model`` you will actually run
    (resolve your own default first). With no model only a whole-route switch-off counts.
    """
    # toggles.py (when present) is asked directly by every caller (dispatch, scheduler, runners);
    # this answers only for the Models-tab file, which set_enabled keeps in step with toggles.
    try:
        return _state_hit(load_state(path), route, model, runner)
    except Exception:
        return False


def _state_hit(state, route, model, runner) -> bool:
    try:
        m = str(model).strip().lower() if model else None

        def hit(entry):
            if not isinstance(entry, dict):
                return False
            if entry.get("all"):
                return True
            return bool(m) and m in {str(x).lower() for x in entry.get("models") or []}

        tables = []
        for key, name in (("routes", route), ("runners", runner)):
            table = state.get(key) if isinstance(state.get(key), dict) else {}
            if name:
                tables.append(table.get(name))
            elif route is None and runner is None and m:
                tables.extend(table.values())
        return any(hit(e) for e in tables)
    except Exception:
        return False


# ---------------------------------------------------------------- HTTP
def _http_error(status, body):
    from .api import HttpError  # lazy: api loads this module through EXTENSIONS
    return HttpError(status, body)


def _owner(h, what):
    if getattr(h, "actor", None) != "owner":
        raise _http_error(403, {"error": "owner_only", "detail": f"only Owner may {what}"})


def _sources(h):
    return getattr(getattr(h, "app", None), "models_sources", None) or default_sources()


def _usage_rows(h):
    reader = getattr(getattr(h, "app", None), "usage_reader", None)
    if reader is None:
        from . import usage
        reader = usage.read_usage
    try:
        return [r for r in (reader() or []) if isinstance(r, dict)]
    except Exception:
        return []


def _notify(h, payload):
    store = getattr(getattr(h, "app", None), "store", None)
    try:
        store.notify("models", payload)
    except Exception:
        pass


def g_models(h, q):
    src = _sources(h)
    try:  # hourly background probe of the free chat routes; never blocks this request
        from . import model_health
        model_health.maybe_refresh(src, http=getattr(getattr(h, "app", None), "model_health_http", None))
    except Exception:
        pass
    return {"models": build_registry(src, _usage_rows(h)), "kinds": list(KINDS),
            "efforts": list(EFFORTS), "state_file": str(_sources(h).state_path().name)}


def g_lookup(h, q):
    row = lookup(q.get("provider"), q.get("model"), _sources(h), _usage_rows(h))
    if row is None:
        raise _http_error(404, {"error": "not_found", "detail": "no such dsh provider/model"})
    return {"model": row}


def p_toggle(h, q):
    _owner(h, "turn models on or off")
    body = h._json_body()
    ids = body.get("ids") if isinstance(body.get("ids"), list) else ([body["id"]] if body.get("id") else [])
    if not ids or not all(isinstance(i, str) and i for i in ids):
        raise _http_error(400, {"error": "bad_request", "detail": "id (or ids) required"})
    if not isinstance(body.get("enabled"), bool):
        raise _http_error(400, {"error": "bad_request", "detail": "enabled must be true or false"})
    try:
        changed = set_enabled(ids, body["enabled"], _sources(h), actor=h.actor, usage_rows=_usage_rows(h))
    except ValueError as e:
        raise _http_error(404, {"error": "not_found", "detail": str(e)})
    _notify(h, {"ids": ids, "enabled": body["enabled"]})
    out = {"ok": True, "models": changed}
    if len(changed) == 1:
        out["model"] = changed[0]
    return out


def _key_kwargs(h):
    src = _sources(h)
    kw = {"credentials": src.credentials, "environ": src.environ, "registry_reader": src.registry_reader}
    if src.env_file:
        kw["env_file"] = src.env_file
    return kw


def g_gemini_keys(h, q):
    from . import gemini_keys
    return gemini_keys.status(**_key_kwargs(h))


def p_gemini_switch(h, q):
    from . import gemini_keys
    _owner(h, "switch the Gemini API key")
    slot = h._json_body().get("slot")
    if not isinstance(slot, str) or not slot:
        raise _http_error(400, {"error": "bad_request", "detail": "slot required"})
    try:
        out = gemini_keys.switch(slot, **_key_kwargs(h))
    except gemini_keys.SwitchRefused as e:
        raise _http_error(409, {"error": "refused", "detail": str(e)})
    except ValueError as e:
        raise _http_error(400, {"error": "bad_request", "detail": str(e)})
    store = getattr(h.app, "store", None)
    try:
        store.event(kind="models", actor="owner",
                    summary=f"Owner pointed dsh's Google AI Studio (Gemini API) key at slot {slot}.")
    except Exception:
        pass
    _notify(h, {"gemini_key": slot})
    return dict(out, ok=True)


ROUTES = {
    "GET /api/models": g_models,
    "GET /api/models/lookup": g_lookup,
    "POST /api/models/toggle": p_toggle,
    "GET /api/models/gemini_keys": g_gemini_keys,
    "POST /api/models/gemini_keys/switch": p_gemini_switch,
}
