#!/usr/bin/env python3
"""waypoint: command line over the Waypoint engine API (stdlib only, Python 3.10+).

Exit codes: 0 ok, 1 other error, 2 usage, 3 stale revision, 4 forbidden, 5 engine unreachable,
6 engine speaks another major API version.

Base URL: env WAYPOINT_URL, else http://127.0.0.1:<WAYPOINT_PORT or 8751>.
Data folder: CC_DATA_DIR or WAYPOINT_DATA_DIR, else ``data_dir`` in ~/.waypoint/config.json (or the file named by
WAYPOINT_CONFIG), else <repo>/data. Writes send the router token (CC_TOKEN_FILE or <data>/engine.token);
``--owner`` sends the owner token instead: owner.token beside the router token file, as the engine writes it
(so beside CC_TOKEN_FILE when set, else <data>/owner.token). Token values are never printed.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

__version__ = "1.1.0"
API_MAJOR = 1  # "Waypoint API 1"; the engine's major must match
DEFAULT_PORT = 8751
SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
TOKEN_HEADER = "X-CC-Token"
OWNER_HEADER = "X-CC-Owner-Token"
API_HEADER = "X-Waypoint-API"
EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_STALE, EXIT_FORBIDDEN, EXIT_DOWN, EXIT_API = 0, 1, 2, 3, 4, 5, 6


class CliExit(Exception):
    def __init__(self, code, msg=""):
        super().__init__(msg)
        self.code, self.msg = code, msg


# ---- settings ----
def config_path():
    named = os.environ.get("WAYPOINT_CONFIG")
    return Path(named) if named else Path.home() / ".waypoint" / "config.json"


def load_config():
    """The optional settings file: WAYPOINT_CONFIG, else ~/.waypoint/config.json. Bad or missing = {}."""
    try:
        data = json.loads(config_path().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def data_dir():
    for name in ("CC_DATA_DIR", "WAYPOINT_DATA_DIR"):
        if os.environ.get(name):
            return Path(os.environ[name])
    value = load_config().get("data_dir")
    return Path(str(value)) if value else REPO / "data"


def env_port():
    raw = os.environ.get("WAYPOINT_PORT", "").strip()
    if not raw:
        return DEFAULT_PORT
    if not raw.isdigit() or not 0 < int(raw) < 65536:
        raise CliExit(EXIT_USAGE, "WAYPOINT_PORT must be a port number (got %r); fix or unset it" % raw)
    return int(raw)


def base_url():
    url = os.environ.get("WAYPOINT_URL", "").strip()
    return url.rstrip("/") if url else "http://127.0.0.1:%d" % env_port()


def router_token_path():
    return Path(os.environ.get("CC_TOKEN_FILE") or data_dir() / "engine.token")


def owner_token_path():
    """Where the engine writes owner.token (engine/cc_engine/api.py): beside the router token file, so beside
    CC_TOKEN_FILE when that is set, else <data dir>/owner.token."""
    return router_token_path().with_name("owner.token")


def mod_owner_token_path():
    """The owner.token path the Claude Code mod resolves, or None when it has no data folder. The mod runs from
    Claude Code's plugin cache, so unlike the CLI it has no <repo>/data fallback: it needs CC_TOKEN_FILE,
    CC_DATA_DIR / WAYPOINT_DATA_DIR or data_dir in the settings file (install.ps1 writes data_dir)."""
    if os.environ.get("CC_TOKEN_FILE"):
        return Path(os.environ["CC_TOKEN_FILE"]).with_name("owner.token")
    for name in ("CC_DATA_DIR", "WAYPOINT_DATA_DIR"):
        if os.environ.get(name, "").strip():
            return Path(os.environ[name].strip()) / "owner.token"
    value = load_config().get("data_dir")
    if isinstance(value, str) and value.strip():
        return Path(value.strip()) / "owner.token"
    return None


def _read(path):
    try:
        return Path(path).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def read_token():
    return _read(router_token_path())


def read_owner_token():
    path = owner_token_path()
    tok = _read(path)
    if not tok:
        raise CliExit(EXIT_FORBIDDEN, "--owner needs the owner token file %s, which is missing or empty; start the "
                                      "engine once (waypoint engine start) or set CC_DATA_DIR to its data folder"
                                      % path)
    return tok


# ---- HTTP ----
def _api_major(value):
    """Major number of an API version like "1.0" or 1; None when it cannot be read."""
    try:
        return int(str(value).strip().split(".")[0])
    except (TypeError, ValueError):
        return None


def check_api(value):
    """Raise exit 6 when the engine's API major differs from ours. Missing or unreadable = API 1."""
    major = _api_major(value) if value not in (None, "") else API_MAJOR
    if major is not None and major != API_MAJOR:
        raise CliExit(EXIT_API, "the engine speaks Waypoint API %s but this CLI speaks API %d; update the CLI and "
                                "engine to the same release (waypoint doctor shows both)" % (value, API_MAJOR))


def _opener(url):
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host in ("127.0.0.1", "localhost", "::1"):  # never send loopback calls through a proxy
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def request(method, path, body=None, owner=False, base=None, timeout=15, check=True):
    """(status, JSON) of one call. ``check`` raises exit 6 when the answer's X-Waypoint-API major differs."""
    base = base or base_url()
    check_fn = check_api if check else (lambda _v: None)
    headers = {"Accept": "application/json"}
    data = None
    if method == "POST":
        data = json.dumps(body or {}).encode("utf-8")
        headers["Content-Type"] = "application/json"
        if owner:
            headers[OWNER_HEADER] = read_owner_token()
        else:
            tok = read_token()
            if tok:
                headers[TOKEN_HEADER] = tok
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with _opener(base).open(req, timeout=timeout) as r:
            check_fn(r.headers.get(API_HEADER))
            raw = r.read().decode("utf-8")
            try:
                return r.status, json.loads(raw or "{}")
            except ValueError:
                raise CliExit(EXIT_ERROR, "%s did not answer with JSON; is WAYPOINT_URL pointing at the Waypoint "
                                          "engine?" % base)
    except urllib.error.HTTPError as e:
        check_fn(e.headers.get(API_HEADER) if e.headers else None)
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"error": raw[:200]}
    except (urllib.error.URLError, OSError) as e:
        raise CliExit(EXIT_DOWN, "engine unreachable at %s (%s); start it with: waypoint engine start"
                      % (base, getattr(e, "reason", e)))


def call(method, path, body=None, owner=False):
    status, data = request(method, path, body, owner)
    if status == 409 and isinstance(data, dict) and data.get("error") == "stale":
        raise CliExit(EXIT_STALE, "stale: engine is at revision %s; run the command again (or add --retry)"
                      % data.get("revision"))
    if status == 403:
        who = "owner" if owner else "router"
        hint = "check the owner.token file in the data folder" if owner else "try --owner if this is an owner action"
        raise CliExit(EXIT_FORBIDDEN, "forbidden: the %s credential was refused; %s" % (who, hint))
    if status >= 400:
        raise CliExit(EXIT_ERROR, "HTTP %s: %s" % (status, json.dumps(data)[:400]))
    return data


def health(base=None, timeout=3):
    """The engine's /health answer, or None when nothing healthy answers. The API version is not enforced
    here; callers compare ``api_major(h)`` themselves so they can still show both versions."""
    try:
        status, data = request("GET", "/health", base=base, timeout=timeout, check=False)
    except CliExit as e:
        if e.code in (EXIT_DOWN, EXIT_ERROR):
            return None
        raise
    return data if status == 200 and isinstance(data, dict) else None


def api_major(h):
    """The engine's API major from a health answer; a missing ``api`` field means 1."""
    if h.get("api") is not None:
        return _api_major(h["api"])
    if h.get("api_version"):
        return _api_major(h["api_version"])
    return API_MAJOR


def api_text(h):
    """'1.0' style API version from a health answer; a missing field means API 1."""
    if h.get("api_version"):
        return str(h["api_version"])
    return "%s.0" % (h.get("api") if h.get("api") is not None else API_MAJOR)


def q(**kw):
    return urllib.parse.urlencode({k: v for k, v in kw.items() if v is not None})


def out(args, data, text):
    print(json.dumps(data, indent=2) if args.json else text)


def unpack(board):
    fields = board.get("fields") or []
    return [dict(zip(fields, row)) for row in board.get("nodes", [])]


def cmd_board(a):
    if not a.project:
        d = call("GET", "/api/projects")
        ps = d.get("projects", [])
        out(a, d, "\n".join("%s  %s" % (p.get("id"), p.get("title", "")) for p in ps) or "no projects")
        return
    d = call("GET", "/api/board?" + q(project=a.project))
    nodes = unpack(d)
    d["nodes_unpacked"] = nodes
    done = ("done", "complete", "completed", "skipped", "carried")
    openn = [n for n in nodes if n.get("status") not in done]
    head = "%s rev %s: %d nodes, %d open" % (a.project, d.get("revision"), len(nodes), len(openn))
    lines = [head] + ["  %s [%s/%s] %s" % (n["id"], n.get("status"), n.get("display_status"), n.get("title")) for n in openn]
    out(a, d, "\n".join(lines))


def cmd_node(a):
    d = call("GET", "/api/node?" + q(project=a.project, id=a.id))
    n = d.get("node", {})
    lines = ["%s/%s rev %s [%s]" % (a.project, a.id, d.get("revision"), n.get("status")),
             "title: %s" % n.get("title"), "goal: %s" % n.get("goal"),
             "check: %s" % json.dumps(n.get("check")), "evidence: %s" % n.get("evidence")]
    for e in (d.get("events") or [])[-10:]:
        lines.append("  event: %s" % json.dumps(e))
    out(a, d, "\n".join(lines))


def cur_revision(project):
    return call("GET", "/api/board?" + q(project=project)).get("revision")


def set_status(a, status):
    def post():
        rev = cur_revision(a.project)
        return call("POST", "/api/node/status", {"project": a.project, "id": a.id, "status": status,
                                                 "evidence": a.evidence, "revision": rev}, owner=a.owner)
    try:
        d = post()
    except CliExit as e:
        if e.code == EXIT_STALE and a.retry:
            d = post()
        else:
            raise
    out(a, d, "%s/%s -> %s" % (a.project, a.id, status))


def cmd_done(a):
    set_status(a, "done")


def cmd_status(a):
    set_status(a, a.status)


TODO_SECTIONS = (("human", "For you"), ("computer", "Queued for agents"))  # the app's and the mod's order and names
OPEN_TODO = ("open", "handed_off")  # what the app and the mod list as open


def todo_status(t):
    """The to-do's status; an older engine without ``status`` gives done or open."""
    return t.get("status") or ("done" if t.get("done") else "open")


def todo_rows(todos, section=None, show_all=False):
    """Open to-dos (or every one with ``show_all``), human first then computer, each in position order."""
    order = {sec: i for i, (sec, _label) in enumerate(TODO_SECTIONS)}
    rows = [t for t in todos if (not section or t.get("section") == section)
            and (show_all or todo_status(t) in OPEN_TODO)]
    return sorted(rows, key=lambda t: (order.get(t.get("section"), len(order)), t.get("position") is None,
                                       t.get("position") or 0))


def todo_text(rows, show_all=False):
    """Grouped lines: a heading per section, then numbered rows; a status tag on every row with --all."""
    if not rows:
        return "no to-dos" if show_all else "no open to-dos (waypoint todo list --all shows every one)"
    labels = dict(TODO_SECTIONS)
    extra = sorted({t.get("section") or "" for t in rows} - set(labels))
    lines = []
    for sec in [s for s, _label in TODO_SECTIONS] + extra:
        group = [t for t in rows if (t.get("section") or "") == sec]
        if not group:
            continue
        if lines:
            lines.append("")
        lines.append("%s (%d)" % (labels.get(sec, sec or "other"), len(group)))
        for n, t in enumerate(group, 1):
            st = todo_status(t)
            tag = "[%s] " % st.replace("_", " ") if show_all or st != "open" else ""
            lines.append("  %d. %s  %s%s" % (n, t["id"], tag, " ".join(str(t.get("text") or "").split())))
    return "\n".join(lines)


def cmd_todo(a):
    s = a.todo_cmd
    if s == "list":
        d = call("GET", "/api/todos")
        ts = todo_rows(d.get("todos", []), section=a.section, show_all=a.all)
        out(a, dict(d, todos=ts), todo_text(ts, show_all=a.all))
    elif s == "add":
        body = {"text": a.text, "section": "human" if a.human else "computer"}
        if a.position is not None:
            body["position"] = a.position
        if a.link:
            body["link"] = a.link
        if a.node:
            if "/" not in a.node:
                raise CliExit(EXIT_USAGE, "--node must be project/id")
            p, i = a.node.split("/", 1)
            body["node"] = {"project": p, "id": i}
        d = call("POST", "/api/todos", body, owner=a.owner)
        out(a, d, "added %s" % d.get("todo", {}).get("id"))
    elif s == "tick":
        d = call("POST", "/api/todos/update", {"id": a.id, "done": True}, owner=a.owner)
        out(a, d, "ticked %s" % a.id)
    elif s == "reorder":
        d = call("POST", "/api/todos/reorder", {"section": a.section, "ids": a.ids}, owner=a.owner)
        out(a, d, "reordered %d in %s" % (len(a.ids), a.section))


RECEIPT_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


def build_receipt(a):
    """The receipt object for ``waypoint receipt write``, checked by the same limits as the engine (FILE_CONTRACT.md).
    Kept here so the CLI needs nothing from the engine package. Raises CliExit(EXIT_USAGE) with a plain reason."""
    def bad(msg):
        raise CliExit(EXIT_USAGE, "receipt: " + msg)
    summary = (a.summary or "").strip()
    if not summary:
        bad("--summary must not be empty")
    if len(summary) > 300:
        bad("--summary is over 300 characters (%d)" % len(summary))
    if bool(a.project) != bool(a.node):
        bad("give both --project and --node, or neither (a session receipt)")
    evidence, outputs = list(a.evidence or []), list(a.output or [])
    for label, items in (("--evidence", evidence), ("--output", outputs)):
        if len(items) > 20:
            bad("%s has more than 20 items" % label)
        if any(len(x) > 500 for x in items):
            bad("an item of %s is over 500 characters" % label)
    obj = {"schema": "waypoint.receipt.v1", "status": a.status, "summary": summary}
    if a.project:
        obj["project"], obj["node"] = a.project, a.node
    if evidence:
        obj["evidence"] = evidence
    if outputs:
        obj["outputs"] = outputs
    if a.todo:
        text = a.todo.strip()
        if not text or len(text) > 2000:
            bad("--todo must be 1 to 2000 characters")
        t = {"text": text, "section": a.todo_section or "computer"}
        if a.todo_link:
            t["link"] = a.todo_link
        obj["todo"] = t
    elif a.todo_section or a.todo_link:
        bad("--todo-section and --todo-link need --todo")
    return obj


def receipt_path(a):
    name = a.name or ("%s.%s" % (a.project, a.node) if a.project and a.node else "session")
    if name.lower().endswith(".json"):
        name = name[:-5]
    if not RECEIPT_NAME.match(name):
        raise CliExit(EXIT_USAGE, "receipt: the name may use only letters, digits, '.', '_' and '-' (got %r)" % name)
    return Path(a.cwd or os.getcwd()) / ".waypoint" / "receipts" / (name + ".json")


def write_receipt(path, obj):
    """Write the file whole, then rename it into place, so a watcher never reads half a file."""
    raw = json.dumps(obj, indent=2, ensure_ascii=False) + "\n"
    if len(raw.encode("utf-8")) > 64 * 1024:
        raise CliExit(EXIT_USAGE, "receipt: the file would be over 64 KB")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".%s.%s.tmp" % (path.name, uuid.uuid4().hex[:8]))
    try:
        tmp.write_text(raw, encoding="utf-8")
        os.replace(str(tmp), str(path))
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise CliExit(EXIT_ERROR, "receipt: cannot write %s (%s)" % (path, e))


def cmd_receipt(a):
    if a.receipt_cmd == "write":
        path = receipt_path(a)
        write_receipt(path, build_receipt(a))
        out(a, {"ok": True, "path": str(path)}, str(path))
        return
    d = call("GET", "/api/receipts?" + q(limit=a.limit))
    rows = d.get("receipts", [])
    lines = []
    for r in rows:
        who = ("%s/%s" % (r.get("project"), r.get("node"))) if r.get("node") else "session"
        what = r.get("reason") or r.get("outcome") or ""
        lines.append("%s  %-9s %-9s %s  %s%s" % ((r.get("ingested_at") or "")[:19], r.get("status"),
                                                r.get("claimed") or "-", who, r.get("summary") or "",
                                                (" (%s)" % what) if what else ""))
    out(a, d, "\n".join(lines) or "no receipts")


TREE_GLYPH = {"running": "*", "idle": ".", "waiting": "?", "done": "+", "error": "x", "ended": "-"}


def tree_lines(d):
    """Indented lines, one per agent: glyph title  tool  tokens  context%  last line."""
    agents = {x.get("id"): x for x in d.get("agents", [])}
    kids = {}
    for x in agents.values():
        p = x.get("parent")
        kids.setdefault(p if p in agents else None, []).append(x["id"])
    roots = [r for r in d.get("roots", []) if r in agents] or kids.get(None, [])
    lines, shown = [], set()

    def line(x, depth):
        tokens = (x.get("tokens_in") or 0) + (x.get("tokens_out") or 0)
        win = x.get("context_window") or 0
        ctx = "%d%%" % round(100.0 * (x.get("context") or 0) / win) if win else "-"
        last = " ".join(str(x.get("last_line") or "").split())[:80]
        return "%s%s %s  %s  %s  %s  %s" % ("  " * depth, TREE_GLYPH.get(x.get("status"), "?"),
                                          x.get("title") or x.get("id"), x.get("tool") or "-", tokens, ctx, last)

    def walk(aid, depth):
        if aid in shown:
            return
        shown.add(aid)
        lines.append(line(agents[aid], depth).rstrip())
        for c in kids.get(aid, []):
            walk(c, depth + 1)

    for r in roots:
        walk(r, 0)
    for aid in agents:  # anything not reached from a root (a cycle or a missing root list)
        walk(aid, 0)
    return lines


def cmd_tree(a):
    d = call("GET", "/api/trace/tree")
    out(a, d, "\n".join(tree_lines(d)) or "no agents")


def cmd_claim(a):
    body = {"project": a.project, "node": a.node, "by": a.by}
    for key, val in (("cwd", a.cwd), ("worktree", a.worktree), ("environment", a.env)):
        if val:
            body[key] = val
    status, d = request("POST", "/api/claim", body)
    if status == 409 and isinstance(d, dict) and d.get("error") == "claimed":
        raise CliExit(EXIT_ERROR, "claim refused: %s" % d.get("detail", "someone else holds this task"))
    if status == 403:
        raise CliExit(EXIT_FORBIDDEN, "forbidden: the router credential was refused; check the engine token file")
    if status >= 400:
        raise CliExit(EXIT_ERROR, "HTTP %s: %s" % (status, json.dumps(d)[:400]))
    c = d.get("claim") or {}
    out(a, d, "%s %s/%s for %s" % ("refreshed" if d.get("refreshed") else "claimed", a.project, a.node,
                                   c.get("by") or a.by))


def cmd_release(a):
    body = {"project": a.project, "node": a.node}
    if a.by:
        body["by"] = a.by
    d = call("POST", "/api/release", body)
    out(a, d, ("released %s/%s; it is now %s" % (a.project, a.node, str(d.get("status")).replace("_", " ")))
        if d.get("released") else "%s/%s had no claim" % (a.project, a.node))


def cmd_run(a):
    try:
        settings = json.loads(a.settings) if a.settings else {}
    except ValueError:
        raise CliExit(EXIT_USAGE, "--settings must be valid JSON")
    d = call("POST", "/api/run", {"project": a.project, "settings": settings}, owner=a.owner)
    rid = d.get("id") or (d.get("run") or {}).get("id") or d.get("run_id")
    out(a, d, "run %s" % rid)


def cmd_usage(a):
    d = call("GET", "/api/usage")
    lines = []
    for p in d.get("pools", []):
        r = p.get("remaining_percent")
        lines.append("%s: %s %s" % (p.get("pool"), "unknown" if r is None else "%g%% left" % r, p.get("status")))
    out(a, d, "\n".join(lines) or "no pools")


# ---- sync write: works offline from the engine (cloud sessions), stdlib only ----
SYNC_SCHEMA = "waypoint.sync.v1"
SYNC_STATUSES = ("not_started", "ready", "needs_you", "error", "running", "submitted", "done")


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo)] + list(args), capture_output=True, text=True)


def sync_repo(a):
    if getattr(a, "repo", None):
        return Path(a.repo)
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else Path.cwd()


def sync_session(a):
    s = getattr(a, "session", None) or os.environ.get("WAYPOINT_SESSION") or uuid.uuid4().hex[:6]
    s = re.sub(r"[^A-Za-z0-9_]+", "_", s).strip("_")[:40]  # no '-' so the file name parses
    if not s:
        raise CliExit(EXIT_USAGE, "empty session id")
    return s


def validate_item(it):
    """Mirror of cc_engine.cloud_sync expectations; returns an error string or None."""
    k = it.get("kind")
    if k == "node_status":
        b = it.get("base_revision")
        if not (it.get("project") and it.get("id")) or it.get("status") not in SYNC_STATUSES:
            return "node_status needs project, id and a known status"
        if isinstance(b, bool) or not isinstance(b, int):
            return "node_status needs an integer base_revision"
    elif k == "todo_add":
        if not " ".join(str(it.get("text") or "").split()):
            return "todo_add needs text"
    elif k == "receipt":
        if not (it.get("project") and it.get("id")):
            return "receipt needs project and id"
    else:
        return "unknown item kind %r" % k
    return None


def append_sync_item(a, item):
    err = validate_item(item)
    if err:
        raise CliExit(EXIT_USAGE, err)
    repo, session = sync_repo(a), sync_session(a)
    inbox = repo / "sync" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    day = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")
    pat = re.compile(r"^%s-%s-(\d+)\.json$" % (day, re.escape(session)))
    mine = sorted(((int(m.group(1)), f) for f in inbox.iterdir() if (m := pat.match(f.name))), key=lambda t: t[0])
    path = None
    if mine:  # keep appending to the newest file until it has been committed
        last = mine[-1][1]
        if _git(repo, "ls-files", "--error-unmatch", str(last)).returncode != 0:
            path = last
    if path is None:
        path = inbox / ("%s-%s-%d.json" % (day, session, (mine[-1][0] + 1) if mine else 1))
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if path.exists():
        doc = json.loads(path.read_text(encoding="utf-8"))
    else:
        doc = {"schema": SYNC_SCHEMA, "session": session, "written_at": now, "items": []}
    doc["items"].append(item)
    doc["written_at"] = now
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    print("queued %s in %s" % (item["kind"], path))
    return path


def cmd_sync(a):
    s = a.sync_cmd
    if s == "status":
        append_sync_item(a, {"kind": "node_status", "project": a.project, "id": a.id, "status": a.status,
                             "evidence": a.evidence, "base_revision": a.base_revision})
    elif s == "todo":
        it = {"kind": "todo_add", "text": a.text, "section": "human" if a.human else "computer"}
        if a.node:
            if "/" not in a.node:
                raise CliExit(EXIT_USAGE, "--node must be project/id")
            p, i = a.node.split("/", 1)
            it["node"] = {"project": p, "id": i}
        append_sync_item(a, it)
    elif s == "receipt":
        append_sync_item(a, {"kind": "receipt", "project": a.project, "id": a.id, "summary": a.summary,
                             "links": list(a.link or [])})
    elif s == "push":
        sync_push(a)


def sync_push(a):
    repo = sync_repo(a)
    if _git(repo, "rev-parse", "--git-dir").returncode != 0:
        raise CliExit(EXIT_USAGE, "%s is not a git repository" % repo)
    if _git(repo, "config", "backup.github").returncode != 0:
        raise CliExit(EXIT_FORBIDDEN, "refusing to push: git config backup.github is not set, so this is not "
                                      "the private repo (never push to a public remote)")
    inbox = repo / "sync" / "inbox"
    files = sorted(f.name for f in inbox.glob("*.json")) if inbox.is_dir() else []
    if not files:
        raise CliExit(EXIT_USAGE, "no files in sync/inbox to push")
    rel = ["sync/inbox/" + f for f in files]
    if _git(repo, "add", "--", *rel).returncode != 0:
        raise CliExit(1, "git add failed")
    if _git(repo, "diff", "--cached", "--quiet", "--", *rel).returncode == 0:
        print("nothing new to commit")
    else:
        r = _git(repo, "commit", "-m", "Waypoint cloud sync: %d inbox file(s)" % len(files), "--", *rel)
        if r.returncode != 0:
            raise CliExit(1, "git commit failed: " + (r.stderr or r.stdout).strip())
    r = _git(repo, "push")
    if r.returncode != 0:
        raise CliExit(1, "git push failed: " + r.stderr.strip())
    print("pushed %d inbox file(s)" % len(files))


# ---- versions: one product, many versions (docs/v5/versions_contract.md) ----
CLOSED = ("done", "skipped", "carried")


def _history(target):
    """History of a family id, else of a project id; raises CliExit(1) with the engine's 404."""
    try:
        return call("GET", "/api/history?" + q(family=target))
    except CliExit as e:
        if "HTTP 404" not in e.msg:
            raise
    return call("GET", "/api/history?" + q(project=target))


def _new_version_source(target):
    """``(request body, source project)`` for ``version new <family|project>``.

    A family id rolls over its current version. A project id that is in a family is resolved through
    ``GET /api/history?project=``: only the family's current version may roll over, so naming an earlier
    one is a usage error naming the current one (the engine refuses it too).
    """
    try:
        d = call("GET", "/api/history?" + q(family=target))
        return {"family": target}, d.get("current")
    except CliExit as e:
        if "HTTP 404" not in e.msg:
            raise
    d = call("GET", "/api/history?" + q(project=target))
    current = d.get("current")
    if (d.get("family") or {}).get("id") and current and current != target:
        raise CliExit(EXIT_USAGE, "only the current version (%s) can roll over; %s is an earlier version of %s"
                      % (current, target, d["family"]["id"]))
    return {"project": target}, current or target


def _kv_skips(items):
    out = {}
    for it in items or []:
        nid, sep, reason = it.partition("=")
        if not sep or not nid.strip() or not reason.strip():
            raise CliExit(EXIT_USAGE, "--skip needs ID=reason with a non-empty reason (got %r)" % it)
        out[nid.strip()] = {"action": "skip", "reason": reason.strip()}
    return out


def _decisions(a):
    dec = _kv_skips(a.skip)
    for nid in a.carry or []:
        if nid in dec:
            raise CliExit(EXIT_USAGE, "%s is both --carry and --skip" % nid)
        dec[nid] = {"action": "carry"}
    return dec


def _unfinished(project):
    d = call("GET", "/api/board?" + q(project=project))
    return d.get("revision"), [n for n in unpack(d) if n.get("status") not in CLOSED]


def _check_ids(dec, unfinished):
    known = {n["id"] for n in unfinished}
    bad = sorted(set(dec) - known)
    if bad:
        raise CliExit(EXIT_USAGE, "not an unfinished task: %s" % ", ".join(bad))


def cmd_version(a):
    s = a.version_cmd
    if s == "history":
        d = _history(a.target)
        fam = d.get("family") or {}
        lines = ["%s (%s)" % (fam.get("title"), fam.get("id") or "no family")]
        for v in d.get("versions", []):
            c = v.get("counts") or {}
            lines.append("  v%s %s%s  %s  done %s, open %s, skipped %s, carried %s%s" % (
                v.get("version"), v.get("project"), " *current*" if v.get("project") == d.get("current") else "",
                v.get("title") or "", c.get("done", 0), c.get("open", 0), c.get("skipped", 0), c.get("carried", 0),
                " [archived]" if v.get("archived") else ""))
        if d.get("skipped"):
            lines.append("skipped ideas:")
            lines += ["  v%s %s/%s %s: %s" % (k.get("version"), k.get("project"), k.get("id"), k.get("title"),
                                              k.get("reason")) for k in d["skipped"]]
        out(a, d, "\n".join(lines))
    elif s == "new":
        dec = _decisions(a)
        body, source = _new_version_source(a.target)
        if a.title:
            body["title"] = a.title
        if a.id:
            body["id"] = a.id
        rev, unfinished = _unfinished(source)
        _check_ids(dec, unfinished)
        if a.dry_run:
            plan_rows = [{"id": n["id"], "title": n.get("title"), **(dec.get(n["id"]) or {"action": "carry"})}
                         for n in unfinished]
            lines = ["dry run for %s (%d unfinished); nothing written" % (source, len(plan_rows))]
            lines += ["  %s %s  %s%s" % (r["action"], r["id"], r["title"],
                                         "  (%s)" % r["reason"] if r.get("reason") else "") for r in plan_rows]
            out(a, {"dry_run": True, "source": source, "decisions": plan_rows}, "\n".join(lines))
            return
        body.update(decisions=dec, revision=rev)
        d = call("POST", "/api/version/new", body, owner=a.owner)
        out(a, d, "%s is version %s: carried %d, skipped %d" % (d.get("project"), d.get("version"),
                                                                 len(d.get("carried", [])), len(d.get("skipped", []))))
    elif s == "family":
        rev = call("GET", "/api/board").get("revision")
        d = call("POST", "/api/version/family", {"family": a.family, "title": a.title, "members": a.members,
                                                  "revision": rev}, owner=True)
        out(a, d, "family %s: %s (current %s)" % (a.family, ", ".join(a.members), a.members[-1]))
    elif s == "decide":
        dec = _decisions(a)
        if not dec:
            raise CliExit(EXIT_USAGE, "give at least one --carry ID or --skip ID=reason")
        rev, unfinished = _unfinished(a.project)
        _check_ids(dec, unfinished)
        d = call("POST", "/api/version/decide", {"project": a.project, "decisions": dec, "revision": rev},
                 owner=a.owner)
        out(a, d, "decided %d: carried %d, skipped %d" % (len(dec), len(d.get("carried", [])), len(d.get("skipped", []))))


# ---- versions, engine control, doctor ----
def version_line():
    h = health()
    if h is None:
        return {"cli": __version__, "engine": None, "api": None}, "waypoint %s (engine not running)" % __version__
    eng, api = str(h.get("version") or "unknown"), api_text(h)
    return {"cli": __version__, "engine": eng, "api": api}, "waypoint %s (engine %s, API %s)" % (__version__, eng, api)


def cmd_version_flag(a):
    data, text = version_line()
    out(a, data, text)


def engine_port(a):
    if getattr(a, "port", None):
        return a.port
    return urllib.parse.urlparse(base_url()).port or DEFAULT_PORT


def run_engine_script(name, *args):
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        raise CliExit(EXIT_ERROR, "waypoint engine needs PowerShell (powershell or pwsh); start the engine by hand "
                                  "instead: cd engine && python -m cc_engine serve")
    script = SCRIPTS / name
    if not script.is_file():
        raise CliExit(EXIT_ERROR, "%s is missing; reinstall Waypoint or run it from a full clone" % script)
    cmd = [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)] + [str(x) for x in args]
    return subprocess.run(cmd).returncode


def cmd_engine(a):
    port = engine_port(a)
    if a.engine_cmd == "start":
        return run_engine_script("Start-Engine.ps1", "-Port", port, "-Python", sys.executable)
    if a.engine_cmd == "stop":
        return run_engine_script("Stop-Engine.ps1", "-Port", port)
    url = "http://127.0.0.1:%d" % a.port if a.port else base_url()
    h = health(url)
    if h is None:
        out(a, {"running": False, "url": url}, "engine: not running at %s; start it with: waypoint engine start" % url)
        return EXIT_DOWN
    data = {"running": True, "url": url, "version": h.get("version"), "api": api_text(h), "revision": h.get("revision")}
    out(a, data, "engine: running at %s (engine %s, API %s, revision %s)"
        % (url, h.get("version"), api_text(h), h.get("revision")))
    return EXIT_OK


def mod_check():
    """(state, detail) for the Claude Code mod. Reads Claude Code's plugin list file; falls back to the CLI."""
    root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    hint = "install it with install.ps1, or: claude plugin install waypoint@waypoint-local --scope user"
    try:
        d = json.loads((root / "plugins" / "installed_plugins.json").read_text(encoding="utf-8"))
        names = list((d.get("plugins") or {}) if isinstance(d, dict) else [])
        found = [n for n in names if str(n).split("@")[0] == "waypoint"]
        return ("ok", "installed (%s)" % found[0]) if found else ("FAIL", "not installed; " + hint)
    except (OSError, ValueError, AttributeError):
        pass
    claude = shutil.which("claude")
    if not claude:
        return "skip", "Claude Code not found, so the mod is not checked (it is optional)"
    try:
        r = subprocess.run([claude, "plugin", "list"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return "skip", "could not run 'claude plugin list'"
    return ("ok", "installed") if "waypoint@" in (r.stdout or "") else ("FAIL", "not installed; " + hint)


def cmd_doctor(a):
    checks = []

    def add(state, name, detail):
        checks.append({"check": name, "state": state, "detail": detail})

    v = sys.version_info
    add("ok" if v >= (3, 10) else "FAIL", "python", "%d.%d.%d at %s%s" % (
        v[0], v[1], v[2], sys.executable, "" if v >= (3, 10) else "; Waypoint needs 3.10 or newer (set WAYPOINT_PYTHON)"))
    url = base_url()
    h = health(url)
    if h is None:
        add("FAIL", "engine", "not reachable at %s; start it with: waypoint engine start" % url)
        add("skip", "api", "engine not running")
    else:
        add("ok", "engine", "running at %s (engine %s, revision %s)" % (url, h.get("version"), h.get("revision")))
        major = api_major(h)
        if major == API_MAJOR:
            add("ok", "api", "engine API %s, CLI API %d" % (api_text(h), API_MAJOR))
        else:
            add("FAIL", "api", "engine API %s but CLI API %d; update both to the same release" % (api_text(h), API_MAJOR))
    ddir = data_dir()
    missing = [str(p) for p in (router_token_path(), owner_token_path()) if not _read(p)]
    if missing:
        hint = ("the engine is running, so it likely uses another data folder: set CC_DATA_DIR (or data_dir in "
                "~/.waypoint/config.json) to it" if h is not None
                else "start the engine once to create them (waypoint engine start)")
        add("FAIL", "tokens", "missing: %s; %s" % (", ".join(missing), hint))
    else:
        add("ok", "tokens", "engine.token and owner.token present in %s" % router_token_path().parent)
    state, detail = mod_check()
    add(state, "mod", detail)
    if state != "ok":
        add("skip", "mod-token", "the mod is not installed, so its token path is not checked")
    else:
        mod_tok = mod_owner_token_path()
        if mod_tok is None:
            add("FAIL", "mod-token", "the mod cannot find the data folder: no data_dir in %s and no CC_DATA_DIR, so "
                "its writes fail with 'owner token missing'; run install.ps1 again (it adds data_dir) or add "
                "\"data_dir\": \"%s\" to that file" % (config_path(), str(ddir).replace("\\", "\\\\")))
        elif not _read(mod_tok):
            add("FAIL", "mod-token", "owner.token missing at %s, where the mod looks; start the engine with that "
                "data folder (waypoint engine start) or fix data_dir in %s" % (mod_tok, config_path()))
        else:
            add("ok", "mod-token", "owner.token readable at %s, where the mod looks" % mod_tok)
    failed = [c for c in checks if c["state"] == "FAIL"]
    text = "\n".join("%-4s  %-6s  %s" % (c["state"], c["check"], c["detail"]) for c in checks)
    text += "\n" + ("all checks passed" if not failed else "%d check(s) failed" % len(failed))
    out(a, {"ok": not failed, "checks": checks}, text)
    return EXIT_ERROR if failed else EXIT_OK


# ---- help and argument parsing ----
SHORT_HELP = """waypoint {version}: work with the Waypoint engine from the command line.

Usage: waypoint <command> [options]

Commands:
{commands}

Examples:
  waypoint board                         list projects
  waypoint board my-project              show a project's open tasks
  waypoint done my-project T3 --evidence "tests pass"
  waypoint todo add "Review the draft" --human
  waypoint engine status

Every command takes --json (print JSON) and --owner (act as the owner).
Run 'waypoint help <command>' for details, or 'waypoint --version' for versions."""


class Parser(argparse.ArgumentParser):
    """argparse with one-line errors that name the next step."""

    def __init__(self, *args, **kw):
        kw.setdefault("formatter_class", argparse.RawDescriptionHelpFormatter)
        super().__init__(*args, **kw)

    def error(self, message):
        cmd = self.prog.split(" ", 1)[1] if " " in self.prog else ""
        m = re.match(r"argument <command>: invalid choice: '([^']*)'", message)
        if m:
            raise CliExit(EXIT_USAGE, "%s: unknown command '%s'. Run 'waypoint help%s' for the list."
                          % (self.prog, m.group(1), " " + cmd if cmd else ""))
        raise CliExit(EXIT_USAGE, "%s: %s. Run 'waypoint help%s' for usage." % (self.prog, message,
                                                                              " " + cmd if cmd else ""))


def _examples(*lines):
    return "Examples:\n" + "\n".join("  " + x for x in lines)


def build_parser():
    # Subcommand copies use SUPPRESS so they never overwrite a flag given before the subcommand.
    common = Parser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print JSON")
    common.add_argument("--owner", action="store_true", default=argparse.SUPPRESS,
                        help="act as the owner (sends the owner token from the data folder)")
    top = Parser(add_help=False)
    top.add_argument("--json", action="store_true", help="print JSON")
    top.add_argument("--owner", action="store_true", help="act as the owner (sends the owner token from the data folder)")
    top.add_argument("--version", action="store_true", dest="show_version", help="print the CLI and engine versions")
    ap = Parser(prog="waypoint", parents=[top], description="Work with the Waypoint engine from the command line.")

    def group(parent, dest, title):
        s = parent.add_subparsers(dest=dest, metavar="<command>", title=title, parser_class=Parser)
        parent._wp_sub = s
        return s

    def add(subs, name, line, examples, parents=(common,)):
        p = subs.add_parser(name, parents=list(parents), help=line, description=line, epilog=_examples(*examples))
        return p

    sub = group(ap, "cmd", "commands")
    p = add(sub, "board", "List projects, or show one project's open tasks",
            ["waypoint board", "waypoint board my-project", "waypoint board my-project --json"])
    p.add_argument("project", nargs="?", help="project id (leave out to list projects)"); p.set_defaults(fn=cmd_board)
    p = add(sub, "node", "Show one task: goal, check, evidence and recent events", ["waypoint node my-project T3"])
    p.add_argument("project", help="project id"); p.add_argument("id", help="task id"); p.set_defaults(fn=cmd_node)
    p = add(sub, "done", "Mark a task done, with evidence",
            ['waypoint done my-project T3 --evidence "tests pass"', 'waypoint done my-project T3 --evidence "ok" --retry'])
    p.add_argument("project", help="project id"); p.add_argument("id", help="task id")
    p.add_argument("--evidence", required=True, help="what shows the task is done")
    p.add_argument("--retry", action="store_true", help="read the board again and retry once if it changed")
    p.set_defaults(fn=cmd_done)
    p = add(sub, "status", "Set a task's status, with evidence",
            ['waypoint status my-project T3 needs_you --evidence "blocked on a key"'])
    p.add_argument("project", help="project id"); p.add_argument("id", help="task id")
    p.add_argument("status", help="new status, for example ready, running, needs_you, done")
    p.add_argument("--evidence", required=True, help="why the status changed")
    p.add_argument("--retry", action="store_true", help="read the board again and retry once if it changed")
    p.set_defaults(fn=cmd_status)

    t = add(sub, "todo", "List, add, tick or reorder to-dos", ["waypoint todo list", 'waypoint todo add "Call the bank" --human'])
    ts = group(t, "todo_cmd", "todo commands")
    p = add(ts, "list", "List open to-dos (--all: every one, with its status)",
            ["waypoint todo list", "waypoint todo list --section human", "waypoint todo list --all"])
    p.add_argument("--section", choices=["human", "computer"], help="only this section")
    p.add_argument("--all", action="store_true", help="also show done, dropped and merged to-dos, each with its status")
    p.set_defaults(fn=cmd_todo)
    p = add(ts, "add", "Add a to-do (computer section unless --human)",
            ['waypoint todo add "Review the draft" --human --node my-project/T3'])
    p.add_argument("text", help="what to do"); p.add_argument("--human", action="store_true", help="put it in the human section")
    p.add_argument("--position", type=int, help="place it at this position (0 = top)")
    p.add_argument("--link", help="a link to attach"); p.add_argument("--node", help="the task it belongs to, as project/id")
    p.set_defaults(fn=cmd_todo)
    p = add(ts, "tick", "Tick a to-do off", ["waypoint todo tick td12"])
    p.add_argument("id", help="to-do id"); p.set_defaults(fn=cmd_todo)
    p = add(ts, "reorder", "Put a section's to-dos in a new order", ["waypoint todo reorder --section human td3 td1 td2"])
    p.add_argument("--section", required=True, choices=["human", "computer"], help="which section")
    p.add_argument("ids", nargs="+", help="to-do ids in the new order"); p.set_defaults(fn=cmd_todo)

    r = add(sub, "receipt", "Write a receipt file for finished work, or list recent receipts",
            ['waypoint receipt write --status done --summary "Guide written" --project docs --node B3',
             "waypoint receipt list --limit 10"])
    rs = group(r, "receipt_cmd", "receipt commands")
    p = add(rs, "write", "Write .waypoint/receipts/<name>.json (no engine needed)",
            ['waypoint receipt write --status done --project docs --node B3 --summary "Guide covers all routes" --output core/docs/API.md',
             'waypoint receipt write --status needs_you --summary "Need a decision" --name release-notes'])
    p.add_argument("--status", required=True, choices=["done", "error", "needs_you"], help="what you believe happened")
    p.add_argument("--summary", required=True, help="one plain sentence, up to 300 characters")
    p.add_argument("--project", help="the project of the task"); p.add_argument("--node", help="the task id")
    p.add_argument("--evidence", action="append", metavar="X", help="a proof line or file (repeatable, up to 20)")
    p.add_argument("--output", action="append", metavar="PATH", help="a deliverable path or https link (repeatable, up to 20)")
    p.add_argument("--todo", metavar="TEXT", help="one to-do to leave for the board")
    p.add_argument("--todo-section", choices=["human", "computer"], help="where the to-do goes (default computer)")
    p.add_argument("--todo-link", metavar="L", help="a link for the to-do")
    p.add_argument("--cwd", help="working folder (default: the current folder)")
    p.add_argument("--name", help="file name without .json (default: project.node, else session)")
    p.set_defaults(fn=cmd_receipt)
    p = add(rs, "list", "List recent receipts, newest first", ["waypoint receipt list", "waypoint receipt list --json"])
    p.add_argument("--limit", type=int, default=50, help="how many (default 50)"); p.set_defaults(fn=cmd_receipt)
    p = add(sub, "tree", "Show running agents as a tree: tool, tokens, context and last line",
            ["waypoint tree", "waypoint tree --json"])
    p.set_defaults(fn=cmd_tree)

    p = add(sub, "claim", "Say you are working on a task (it shows as running)",
            ["waypoint claim my-project T3 --by worker-1 --cwd C:/work/wt"])
    p.add_argument("project", help="project id"); p.add_argument("node", help="task id")
    p.add_argument("--by", required=True, help="who is working (session or worker name)")
    p.add_argument("--cwd", help="working folder"); p.add_argument("--worktree", help="worktree folder")
    p.add_argument("--env", help="environment, for example 'Claude Code desktop'")
    p.set_defaults(fn=cmd_claim)
    p = add(sub, "release", "Stop working on a claimed task", ["waypoint release my-project T3 --by worker-1"])
    p.add_argument("project", help="project id"); p.add_argument("node", help="task id")
    p.add_argument("--by", help="who is releasing (default: the claimer)")
    p.set_defaults(fn=cmd_release)
    p = add(sub, "run", "Start a run of a project", ["waypoint run my-project", "waypoint run my-project --settings '{\"max\": 2}'"])
    p.add_argument("project", help="project id"); p.add_argument("--settings", help="run settings as JSON")
    p.set_defaults(fn=cmd_run)
    p = add(sub, "usage", "Show how much usage is left in each pool", ["waypoint usage"]); p.set_defaults(fn=cmd_usage)

    v = add(sub, "version", "Roll a project over to a new version, or see its history",
            ["waypoint version history my-family", "waypoint version new my-family --dry-run"])
    vs = group(v, "version_cmd", "version commands")
    p = add(vs, "history", "Show every version of a family or project", ["waypoint version history my-family"])
    p.add_argument("target", help="family id or project id"); p.set_defaults(fn=cmd_version)
    for name, line, ex in (
            ("new", "Start the next version; unfinished tasks carry over unless skipped",
             ['waypoint version new my-family --dry-run', 'waypoint version new my-family --skip T4="not needed"']),
            ("decide", "Carry or skip unfinished tasks of an earlier version",
             ['waypoint version decide my-project-v1 --carry T2 --skip T5="done elsewhere"'])):
        p = add(vs, name, line, ex)
        p.add_argument("target" if name == "new" else "project",
                       help="family id or project id" if name == "new" else "the earlier version's project id")
        p.add_argument("--skip", action="append", metavar="ID=reason", help="skip a task, with a reason (repeatable)")
        p.add_argument("--carry", action="append", metavar="ID", help="carry a task over (repeatable)")
        if name == "new":
            p.add_argument("--title", help="title of the new version"); p.add_argument("--id", help="project id of the new version")
            p.add_argument("--dry-run", action="store_true", help="show what would happen and write nothing")
        p.set_defaults(fn=cmd_version)
    p = add(vs, "family", "Group projects as versions of one product (owner action)",
            ['waypoint version family my-family --title "My product" my-project-v1 my-project-v2'])
    p.add_argument("family", help="family id"); p.add_argument("--title", required=True, help="family title")
    p.add_argument("members", nargs="+", help="project ids, oldest first (the last is current)"); p.set_defaults(fn=cmd_version)

    sy = add(sub, "sync", "Queue changes offline in sync/inbox (cloud sessions), then push them",
             ['waypoint sync status my-project T3 done --evidence "ok" --base-revision 42', "waypoint sync push"])
    ss = group(sy, "sync_cmd", "sync commands")
    shared = Parser(add_help=False)
    shared.add_argument("--session", help="session id used in the file name (default: random)")
    shared.add_argument("--repo", help="repository folder (default: the current git repo)")
    p = add(ss, "status", "Queue a task status change", ['waypoint sync status my-project T3 done --evidence "ok" --base-revision 42'],
            parents=(common, shared))
    p.add_argument("project", help="project id"); p.add_argument("id", help="task id"); p.add_argument("status", help="new status")
    p.add_argument("--evidence", required=True, help="why the status changed")
    p.add_argument("--base-revision", type=int, required=True, help="the board revision you read")
    p.set_defaults(fn=cmd_sync)
    p = add(ss, "todo", "Queue a new to-do", ['waypoint sync todo "Review the draft" --human'], parents=(common, shared))
    p.add_argument("text", help="what to do"); p.add_argument("--human", action="store_true", help="put it in the human section")
    p.add_argument("--node", help="the task it belongs to, as project/id"); p.set_defaults(fn=cmd_sync)
    p = add(ss, "receipt", "Queue a receipt for a task", ['waypoint sync receipt my-project T3 --summary "done" --link https://example.com'],
            parents=(common, shared))
    p.add_argument("project", help="project id"); p.add_argument("id", help="task id")
    p.add_argument("--summary", required=True, help="one-line summary"); p.add_argument("--link", nargs="*", help="links")
    p.set_defaults(fn=cmd_sync)
    p = add(ss, "push", "Commit and push the queued files (private repo only)", ["waypoint sync push"], parents=(common, shared))
    p.set_defaults(fn=cmd_sync)

    e = add(sub, "engine", "Start, stop or check the local engine",
            ["waypoint engine status", "waypoint engine start", "waypoint engine stop --port 8752"])
    es = group(e, "engine_cmd", "engine commands")
    for name, line in (("start", "Start the engine in the background (reuses a running one)"),
                       ("stop", "Stop the engine on this port (only a Waypoint engine is stopped)"),
                       ("status", "Say whether the engine answers, and its versions")):
        p = add(es, name, line, ["waypoint engine %s" % name, "waypoint engine %s --port 8752" % name])
        p.add_argument("--port", type=int, help="engine port (default: WAYPOINT_PORT, else 8751)")
        p.set_defaults(fn=cmd_engine)

    p = add(sub, "doctor", "Check Python, the engine, its API version, token files and the Claude Code mod",
            ["waypoint doctor", "waypoint doctor --json"])
    p.set_defaults(fn=cmd_doctor)
    p = add(sub, "help", "Show help for a command", ["waypoint help", "waypoint help todo add"], parents=())
    p.add_argument("topic", nargs="*", help="command name, for example: todo add")
    return ap


def short_help(ap):
    rows = ["  %-9s  %s" % (name, act.help) for act in ap._wp_sub._choices_actions for name in [act.dest]]
    return SHORT_HELP.format(version=__version__, commands="\n".join(rows))


def help_for(ap, topic):
    parser, path = ap, []
    for word in topic:
        subs = getattr(parser, "_wp_sub", None)
        if subs is None or word not in subs.choices:
            where = "waypoint help" + "".join(" " + p for p in path)
            raise CliExit(EXIT_USAGE, "no command '%s'. Run '%s' for the list." % (" ".join(path + [word]), where))
        parser, path = subs.choices[word], path + [word]
    return short_help(ap) if parser is ap else parser.format_help().rstrip()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        ap = build_parser()
        if not argv or argv[0] in ("-h", "--help"):
            print(short_help(ap))
            return EXIT_OK
        opts = argv[:argv.index("--")] if "--" in argv else argv
        if "--version" in opts:  # before or after the command: print the versions, run nothing else
            cmd_version_flag(argparse.Namespace(json="--json" in opts))
            return EXIT_OK
        try:
            a = ap.parse_args(argv)
        except SystemExit as e:  # -h on a subcommand
            return EXIT_USAGE if e.code else EXIT_OK
        if a.cmd == "help":
            print(help_for(ap, a.topic))
            return EXIT_OK
        if not a.cmd:
            print(short_help(ap))
            return EXIT_OK
        fn = getattr(a, "fn", None)
        if fn is None:  # a group such as "todo" without its subcommand
            names = ", ".join(ap._wp_sub.choices[a.cmd]._wp_sub.choices)
            raise CliExit(EXIT_USAGE, "waypoint %s: choose one of %s. Run 'waypoint help %s' for usage."
                          % (a.cmd, names, a.cmd))
        code = fn(a)
        return EXIT_OK if code is None else code
    except CliExit as e:
        if e.msg:
            print(e.msg, file=sys.stderr)
        return e.code
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
