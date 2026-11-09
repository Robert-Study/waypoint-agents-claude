"""Control Centre MCP stdio server: lets the dsh Router ("Orchestrate" mode) draft plans into the graph.

Same protocol style as ``waypoint-workspace/tools/dsh_subagent_mcp_server.py`` (newline-delimited
JSON-RPC on stdin/stdout, stdlib only). Exactly three small tools, each a thin HTTP call to the engine
(default ``http://127.0.0.1:8751``, override with ``CC_ENGINE_URL``):

- ``cc_draft_plan``  validate locally, then POST ``/api/plan/import`` with an inline plan dict.
- ``cc_board``       compact text summary of ``/api/board`` (<= 1,500 chars).
- ``cc_needs_you``   read-only list of open questions. Answering is owner-only and deliberately absent.

There is deliberately no run-start tool (Owner, 4 Nov 2026): the Router cannot start runs; Owner
starts them from the Autonomous tab.

Agents switches: ``cc_draft_plan`` refuses a node pinned to a pool switched off in Agents
(``GET /api/toggles``), and ``tools/list`` leaves switched-off pools out of the pool enum. When the
engine has no ``/api/toggles`` the draft goes through; dispatch refuses it later anyway.

Every POST carries the ``X-CC-Token`` header read from ``data/engine.token`` (override with
``CC_TOKEN_FILE``); the engine then records the actor as ``router``. Drafted plans land as
``draft`` and do not run until Owner accepts them in the Control Centre.

Run: ``python -u -m cc_engine.mcp_server`` from ``engine/``.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import quote

from . import DATA_DIR, graph, plan, runsheet

TOKEN_HEADER = "X-CC-Token"


def default_token_path():
    return os.environ.get("CC_TOKEN_FILE") or str(DATA_DIR / "engine.token")


def read_token(path=None):
    try:
        with open(path or default_token_path(), encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18", "2026-07-28"}
DEFAULT_ENGINE = "http://127.0.0.1:8751"
CHECK_TYPES = ("command", "files_exist", "text_present", "human")
BOARD_LIMIT = 1500
TEXT_LIMIT = 1500
STATUS_ORDER = ("ready", "running", "submitted", "error", "needs_you", "not_started", "done", "skipped", "carried")
LISTED = ("ready", "running", "error", "needs_you")

_NODE_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "title": {"type": "string"},
        "goal": {"type": "string"},
        "check": {"type": "object", "description": "{type:command,run} | {type:files_exist,paths} | "
                                                   "{type:text_present,path,text} | {type:human,prompt}"},
        "deps": {"type": "array", "items": {"type": "string"}},
        "pool": {"type": "string", "enum": list(runsheet.POOLS)},
    },
    "required": ["id", "title", "goal", "check", "deps"],
}

TOOLS = [
    {
        "name": "cc_draft_plan",
        "description": "Draft a new task graph in the Control Centre. Every node needs a done-check and known deps.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "brief": {"type": "string"},
                "nodes": {"type": "array", "items": _NODE_SCHEMA},
                "id": {"type": "string", "description": "Optional project id (default: from title)."},
                "cwd": {"type": "string", "description": "Optional working directory."},
            },
            "required": ["title", "brief", "nodes"],
        },
    },
    {
        "name": "cc_board",
        "description": "Compact board summary: status counts and ready/running/error/needs_you node ids.",
        "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}}, "required": ["project"]},
    },
    {
        "name": "cc_needs_you",
        "description": "Read-only list of open questions waiting for Owner.",
        "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}}},
    },
    {
        "name": "cc_where",
        "description": "Where a task should run: main, worktree, new chat, fork or subagent, and why.",
        "inputSchema": {"type": "object", "properties": {
            "repo": {"type": "string"}, "project": {"type": "string"},
            "read_only": {"type": "boolean"}, "bounded": {"type": "boolean"},
            "needs_conversation": {"type": "boolean"}, "context_tokens": {"type": "number"}}},
    },
]


class EngineError(RuntimeError):
    pass


class UrlHttp:
    """Real HTTP layer. ``request`` returns ``(status, json_body)``; tests inject a fake with the same shape."""

    def __init__(self, base=None, timeout=10.0, token=None, token_path=None):
        self.base = (base or os.environ.get("CC_ENGINE_URL") or DEFAULT_ENGINE).rstrip("/")
        self.timeout = timeout
        self.token = token
        self.token_path = token_path

    def request(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"} if data else {}
        if method != "GET":
            token = self.token or read_token(self.token_path)  # re-read: the engine may have restarted
            if not token:
                raise EngineError(f"engine token not found at {self.token_path or default_token_path()}; "
                                  "start the engine with scripts/Start-Engine.ps1")
            headers[TOKEN_HEADER] = token
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, _decode(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, _decode(e.read())
        except (urllib.error.URLError, OSError) as e:
            raise EngineError(f"Control Centre engine not reachable at {self.base} ({e}); "
                              "start it with scripts/Start-Engine.ps1") from e


def _decode(raw):
    try:
        return json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"error": "non_json", "detail": raw[:200].decode("utf-8", "replace")}


def _ok(http, method, path, body=None):
    status, out = http.request(method, path, body)
    if status != 200:
        out = out if isinstance(out, dict) else {}
        detail = out.get("detail") or out.get("error") or "no detail"
        raise EngineError(f"engine {method} {path.split('?')[0]} -> {status}: {detail}")
    return out


def _clip(text, limit=TEXT_LIMIT):
    return text if len(text) <= limit else text[: limit - 15].rstrip() + "\n...(truncated)"


def slugify(title):
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:40].strip("-")
    return s or "plan"


# ---------------------------------------------------------------- Agents switches
def fetch_toggles(http):
    """``GET /api/toggles`` payload, or None when the engine cannot say."""
    try:
        status, out = http.request("GET", "/api/toggles")
    except Exception:
        return None
    return out if status == 200 and isinstance(out, dict) else None


def pool_off(pool, toggles):
    """Plain-English reason ``pool`` is switched off, else None (unknown means allowed)."""
    if not toggles:
        return None
    from . import toggles as T
    from .dispatch import route_for_pool
    enabled = toggles.get("enabled") if isinstance(toggles.get("enabled"), dict) else {}
    for ident in (pool, route_for_pool(pool), T.canonical(pool), T.canonical(route_for_pool(pool))):
        if enabled.get(ident) is False:
            label = T.GROUPS.get(T.canonical(ident), {}).get("label") or pool
            return f"{label} is switched off in Agents"
    return None


def tools_for(http=None):
    """``TOOLS`` with switched-off pools left out of the node pool enum."""
    toggles = fetch_toggles(http) if http is not None else None
    if not toggles:
        return TOOLS
    out = json.loads(json.dumps(TOOLS))
    for t in out:
        if t["name"] == "cc_draft_plan":
            pool = t["inputSchema"]["properties"]["nodes"]["items"]["properties"]["pool"]
            pool["enum"] = [p for p in pool["enum"] if not pool_off(p, toggles)]
    return out


# ---------------------------------------------------------------- cc_draft_plan
def build_plan(args):
    """Turn tool arguments into an engine plan dict, or raise ``ValueError`` with a fix-it message."""
    title = str(args.get("title") or "").strip()
    brief = str(args.get("brief") or "").strip()
    nodes_in = args.get("nodes")
    if not title or not brief:
        raise ValueError("title and brief are required")
    if not isinstance(nodes_in, list) or not nodes_in:
        raise ValueError("nodes must be a non-empty list")
    nodes = []
    for i, n in enumerate(nodes_in):
        if not isinstance(n, dict):
            raise ValueError(f"node #{i} must be an object")
        nid = str(n.get("id") or "").strip()
        label = nid or f"#{i}"
        for key in ("title", "goal"):
            if not isinstance(n.get(key), str) or not n[key].strip():
                raise ValueError(f"node {label} needs {key}")
        check = n.get("check")
        if not isinstance(check, dict) or check.get("type") not in CHECK_TYPES:
            raise ValueError(f"node {label} needs a check of type {'/'.join(CHECK_TYPES)}")
        if check["type"] == "human" and not str(check.get("prompt") or "").strip():
            raise ValueError(f"node {label} human check needs 'prompt'")
        deps = n.get("deps")
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            raise ValueError(f"node {label} deps must be a list of node ids ([] for none)")
        item = {"id": nid, "title": n["title"].strip(), "goal": n["goal"].strip(), "check": check,
                "deps": deps}
        pool = n.get("pool")
        if pool not in (None, ""):
            if pool not in runsheet.POOLS:
                raise ValueError(f"node {label} pool must be one of {list(runsheet.POOLS)}")
            item["pool"] = pool
        nodes.append(item)
    out = {"id": str(args.get("id") or "").strip() or slugify(title), "title": title, "brief": brief,
           "cwd": args.get("cwd") or None, "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "nodes": nodes}
    try:  # same rules the engine applies: unique ids, check fields, known deps, no cycles
        plan.validate_plan(out)
    except plan.PlanError as e:
        raise ValueError(str(e)) from e
    return out


def draft_plan(http, args):
    p = build_plan(args)
    toggles = fetch_toggles(http) if any(n.get("pool") for n in p["nodes"]) else None
    for n in p["nodes"]:
        off = pool_off(n.get("pool"), toggles) if n.get("pool") else None
        if off:
            raise ValueError(f"node {n['id']}: {off}; pick another pool or leave it unpinned")
    projects = _ok(http, "GET", "/api/projects?all=1").get("projects") or []
    if any(x.get("id") == p["id"] for x in projects):
        raise ValueError(f"project {p['id']!r} already exists; drafts never overwrite. Pass a new id.")
    for attempt in (1, 2):  # one retry if another writer bumped the revision in between
        rev = _ok(http, "GET", "/health").get("revision", 0)
        status, out = http.request("POST", "/api/plan/import", {"plan": p, "revision": rev})
        if status == 409 and attempt == 1:
            continue
        if status != 200:
            detail = (out or {}).get("detail") or (out or {}).get("error") or "no detail"
            raise EngineError(f"engine POST /api/plan/import -> {status}: {detail}")
        break
    waves = graph.waves(p["nodes"])
    kinds = {}
    for n in p["nodes"]:
        kinds[n["check"]["type"]] = kinds.get(n["check"]["type"], 0) + 1
    checks = ", ".join(f"{v} {k}" for k, v in sorted(kinds.items()))
    roots = sum(1 for n in p["nodes"] if not n["deps"])
    pid = out.get("project") or p["id"]
    status = out.get("status") or "draft"
    tail = (" It is a draft: nothing runs until Owner accepts it in the Control Centre."
            if status == "draft" else "")
    return (f"project={pid}\nDrafted '{p['title']}': {len(p['nodes'])} tasks, {roots} ready once accepted, "
            f"{max(waves.values()) + 1} waves; checks {checks}. Revision {out.get('revision')}.{tail}")


# ---------------------------------------------------------------- cc_board
def _ids_line(label, ids, budget):
    if not ids:
        return ""
    line, shown = f"{label}: ", 0
    for i in ids:
        piece = ("" if shown == 0 else ",") + str(i)
        rest = len(ids) - shown - 1
        tail = f" (+{rest} more)" if rest else ""
        if len(line) + len(piece) + len(tail) > budget:
            break
        line += piece
        shown += 1
    if shown < len(ids):
        line += f" (+{len(ids) - shown} more)"
    return line


def board(http, args):
    pid = str(args.get("project") or "").strip()
    q = f"?project={quote(pid)}" if pid else ""
    b = _ok(http, "GET", "/api/board" + q)
    if not b.get("project"):
        return "No such project."
    fields = b.get("fields") or []
    rows = [dict(zip(fields, r)) for r in b.get("nodes") or []]
    by = {}
    for n in rows:
        by.setdefault(n.get("display_status") or n.get("status"), []).append(n["id"])
    counts = b.get("counts") or {}
    total = counts.get("total", len(rows))
    parts = [f"{s} {counts.get(s, len(by.get(s, [])))}" for s in STATUS_ORDER if counts.get(s, len(by.get(s, [])))]
    flags = ""
    if b["project"].get("draft"):
        flags += " | DRAFT (waits for Owner to accept)"
    if b.get("integrity_ok") is False:
        flags += " | INTEGRITY MISMATCH"
    head = f"{b['project'].get('title')} ({b['project']['id']}) rev {b.get('revision')}{flags} | {total} tasks: " + (
        ", ".join(parts) or "empty")
    lines = [_clip(head, 400)]
    remaining = BOARD_LIMIT - len(lines[0])
    share = max(60, remaining // max(1, sum(1 for s in LISTED if by.get(s))) - 2)
    for s in LISTED:
        line = _ids_line(s, by.get(s, []), min(share, remaining - 1))
        if line:
            lines.append(line)
            remaining -= len(line) + 1
    return _clip("\n".join(lines), BOARD_LIMIT)


# ---------------------------------------------------------------- cc_needs_you
def needs_you(http, args):
    pid = str(args.get("project") or "").strip()
    q = "?open=1" + (f"&project={quote(pid)}" if pid else "")
    qs = _ok(http, "GET", "/api/questions" + q).get("questions") or []
    if not qs:
        return "No open questions."
    lines = [f"{len(qs)} open question(s) (Owner answers in the Control Centre):"]
    for item in qs:
        where = "/".join(x for x in (item.get("project_id"), item.get("node_id")) if x) or "-"
        extra = ""
        if item.get("kind") == "likert":
            extra = f" [1-{item.get('scale')}]"
        elif item.get("kind") == "select":
            opts = [str(o.get("label") or o.get("value")) for o in item.get("options") or []]
            extra = " [" + " / ".join(opts) + "]"
        prompt = " ".join(str(item.get("prompt") or "").split())
        lines.append(f"- {item.get('id')} {where}: {prompt[:200]}{extra}")
    return _clip("\n".join(lines))


# ---------------------------------------------------------------- cc_where
def where(http, args):
    from . import env_policy
    task = {k: args[k] for k in ("repo", "read_only", "bounded", "needs_conversation", "context_tokens")
            if k in args}
    pid = str(args.get("project") or "").strip()
    writers = []
    if pid:
        try:
            claims = _ok(http, "GET", f"/api/claims?project={quote(pid)}").get("claims") or {}
            writers = env_policy.writers_from_claims(claims, task.get("repo"))
        except EngineError:
            pass  # no claims view: decide as if the repo were free
    pick = env_policy.choose(task, {"writers": writers})
    return _clip(f"{pick['env']} ({pick['workdir']}): {pick['why']}" + "\nInstructions: " + pick["instructions"])


# No run-start handler: runs start only from the Autonomous tab, never from the Router.
HANDLERS = {"cc_draft_plan": draft_plan, "cc_board": board, "cc_needs_you": needs_you, "cc_where": where}


def call_tool(name, args, http=None):
    fn = HANDLERS.get(name)
    if fn is None:
        raise ValueError(f"Unknown tool: {name}")
    return fn(http or UrlHttp(), args if isinstance(args, dict) else {})


def handle(message, http=None):
    method, msg_id = message.get("method"), message.get("id")
    if msg_id is None:
        return None
    params = message.get("params") or {}
    try:
        if method == "initialize":
            asked = params.get("protocolVersion")
            result = {
                "protocolVersion": asked if asked in SUPPORTED_VERSIONS else PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "rob-control-centre", "version": "0.1.0"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": tools_for(http)}
        elif method == "tools/call":
            try:
                text = call_tool(str(params.get("name", "")), params.get("arguments") or {}, http)
                result = {"content": [{"type": "text", "text": text}], "isError": False}
            except Exception as exc:
                result = {"content": [{"type": "text", "text": f"Control Centre error: {exc}"}], "isError": True}
        else:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    except Exception as exc:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": str(exc)}}
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def main(stdin=None, stdout=None, http=None):
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    http = http or UrlHttp()
    while True:
        raw = stdin.readline()
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            response = handle(message, http) if isinstance(message, dict) else {
                "jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
        if response is not None:
            stdout.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
            stdout.flush()


if __name__ == "__main__":
    main()
