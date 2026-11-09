#!/usr/bin/env python3
"""waypoint: command line over the Waypoint data folder (stdlib only, Python 3.10+). No engine is needed.

Commands read and write the data folder through the data client (core/waypoint-data/py). Owner actions that need
the runner (run, version new/family/decide) write a run or an action file and start the runner on demand.

Exit codes: 0 ok, 1 other error, 2 usage, 3 stale revision, 4 forbidden (owner action without --owner or without
owner.token), 5 engine unreachable (the engine is retired: ``waypoint engine`` always answers 5), 6 unused (kept so
the numbers other lanes rely on never change meaning).

Data folder: CC_DATA_DIR or WAYPOINT_DATA_DIR, else ``data_dir`` in ~/.waypoint/config.json (or the file named by
WAYPOINT_CONFIG), else the data-paths resolver (~/.waypoint/data-dir pointer, else ~/.waypoint/data).
``--owner`` acts as the owner and needs owner.token beside CC_TOKEN_FILE (else in the data folder). Token values are
never printed.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
_PY_CLIENT = REPO / "core" / "waypoint-data" / "py"
if str(_PY_CLIENT) not in sys.path:
    sys.path.insert(0, str(_PY_CLIENT))

from waypoint_data import Claims, RevConflict, Store, compute_float, resolve_data_root  # noqa: E402
from waypoint_data import io as wio  # noqa: E402
from waypoint_data import nodes as wn  # noqa: E402
from waypoint_data.store import iso, serialize  # noqa: E402

__version__ = "1.2.0"
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
STATUSES = ("not_started", "ready", "running", "submitted", "done", "error", "needs_you", "skipped", "carried")
VERSION_ONLY = ("skipped", "carried")  # a version roll-over closes tasks this way; nobody sets them by hand
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


def _read_file(path):
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return None


def data_dir():
    """The data folder: CC_DATA_DIR, WAYPOINT_DATA_DIR, ``data_dir`` in the settings file, then the data-paths
    resolver (``~/.waypoint/data-dir`` pointer, else ``~/.waypoint/data``)."""
    for name in ("CC_DATA_DIR", "WAYPOINT_DATA_DIR"):
        if os.environ.get(name, "").strip():
            return Path(os.environ[name].strip())
    value = load_config().get("data_dir")
    if isinstance(value, str) and value.strip():
        return Path(value.strip())
    return Path(resolve_data_root({}, str(Path.home()), _read_file, "win32" if os.name == "nt" else "posix")["root"])


# ---- before the 1.2 cutover the live board is still the 1.1 engine ----
CUTOVER_RECEIPT = ("backups", "cutover-receipt.json")


def cut_over():
    """True once ship-cutover has migrated the 1.1 board into the data folder (it writes backups/cutover-receipt.json)."""
    return data_dir().joinpath(*CUTOVER_RECEIPT).is_file()


def _legacy():
    """The 1.1 CLI (scripts/waypoint_cli_engine.py) while the live board is not cut over yet, else None.

    WAYPOINT_CLI_MODE=folder or engine forces a side; an explicit CC_DATA_DIR or WAYPOINT_DATA_DIR means a folder."""
    mode = os.environ.get("WAYPOINT_CLI_MODE", "").strip().lower()
    if mode == "folder":
        return None
    if mode != "engine" and (cut_over() or any(os.environ.get(n, "").strip() for n in ("CC_DATA_DIR", "WAYPOINT_DATA_DIR"))):
        return None
    mod = sys.modules.get("waypoint_cli_engine")
    if mod is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("waypoint_cli_engine", SCRIPTS / "waypoint_cli_engine.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["waypoint_cli_engine"] = mod
        spec.loader.exec_module(mod)
    return mod


def call(method, path, body=None, owner=False):
    """The 1.1 helper, kept for callers that import this module; it answers only before the cutover."""
    legacy = _legacy()
    if legacy is None:
        raise CliExit(EXIT_DOWN, ENGINE_RETIRED)
    return legacy.call(method, path, body=body, owner=owner)


def owner_token_path():
    """The owner credential file: beside CC_TOKEN_FILE when set, else <data folder>/owner.token. Only its presence
    is checked now (nothing is sent anywhere); it keeps ``--owner`` a deliberate act of the machine's user."""
    named = os.environ.get("CC_TOKEN_FILE")
    return Path(named).with_name("owner.token") if named else data_dir() / "owner.token"


def _read(path):
    try:
        return Path(path).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def need_owner(a, what):
    """Exit 4 unless ``--owner`` was given and the owner.token file exists. The token value is never printed."""
    if not getattr(a, "owner", False):
        raise CliExit(EXIT_FORBIDDEN, "forbidden: only the owner may %s; add --owner" % what)
    if not _read(owner_token_path()):
        raise CliExit(EXIT_FORBIDDEN, "--owner needs the owner token file %s, which is missing or empty"
                      % owner_token_path())


def writer_id(a):
    return "owner" if getattr(a, "owner", False) else "cli"


def open_store(a):
    """(Store, Claims) on the data folder. Only the data client touches the folder."""
    root = str(data_dir())
    if not os.path.isdir(root):
        raise CliExit(EXIT_ERROR, "data folder %s does not exist; set WAYPOINT_DATA_DIR (or data_dir in %s)"
                      % (root, config_path()))
    w = writer_id(a)
    return Store(root, writer=w), Claims(root, writer=w)


def check_id(value, what):
    if not isinstance(value, str) or not ID_RE.match(value):
        raise CliExit(EXIT_USAGE, "%s must be letters, digits, '.', '_' or '-' (got %r)" % (what, value))
    return value


def project_dirs(store):
    try:
        return sorted(n for n in os.listdir(store.path("projects")) if os.path.isdir(store.path("projects/" + n)))
    except OSError:
        return []


def get_project(store, pid):
    check_id(pid, "project")
    p = store.read("projects/%s/project.json" % pid)
    if p is None:
        raise CliExit(EXIT_ERROR, "no project %s" % pid)
    return p


def get_node(store, pid, nid):
    get_project(store, pid)
    check_id(nid, "task id")
    n = store.read("projects/%s/nodes/%s.json" % (pid, nid))
    if n is None:
        raise CliExit(EXIT_ERROR, "no task %s in %s" % (nid, pid))
    return n


def live_claim(claims, project, node):
    c = claims.read(project, node)
    return c if isinstance(c, dict) and not claims.is_stale(c) else None


def event(store, kind, summary, project=None, node=None, data=None):
    ev = {"kind": kind, "summary": summary[:300], "data": data}
    if project:
        ev["project"] = project
    if node:
        ev["node"] = node
    try:
        store.append_event(store.writer or "cli", ev)
    except (OSError, ValueError):
        pass  # a trace line is never a reason to lose the change


def out(args, data, text):
    print(json.dumps(data, indent=2) if args.json else text)


def body_of(entity):
    return {k: v for k, v in entity.items() if k not in ("rev", "updated_at", "updated_by")}


def board_rows(store, claims, project):
    nodes = store.list_entities("projects/%s/nodes" % project)
    try:
        fl = {f["id"]: f for f in compute_float(nodes)}
    except ValueError:  # a dependency cycle: the board still shows, without float
        fl = {}
    rows = []
    for n in sorted(nodes, key=lambda x: x["id"]):
        f = fl.get(n["id"]) or {}
        c = live_claim(claims, project, n["id"])
        status = n.get("status")
        rows.append({"id": n["id"], "title": n.get("title"), "status": status,
                     "display_status": "running" if c and status in ("not_started", "ready") else status,
                     "rev": n.get("rev"), "slot": n.get("slot"),
                     "deps": [d if isinstance(d, str) else d.get("id") for d in n.get("deps") or []],
                     "float": {k: f.get(k) for k in ("early", "late", "float", "critical")},
                     "claim": ({"owner": c.get("owner"), "claimed_by": c.get("claimed_by"), "since": c.get("since")}
                               if c else None)})
    return rows


def version_display(p, family_size):
    """The version a board shows (pref 222): the exact range (version_range, e.g. V10.1-V10.2) when set; else the
    version name only when the family has more than one version; a lone version shows nothing."""
    if p.get("version_range"):
        return p["version_range"]
    return version_name(p) if family_size > 1 else None


def _family_sizes(projects):
    sizes = {}
    for p in projects:
        fam = p.get("family") or p.get("id")
        sizes[fam] = sizes.get(fam, 0) + 1
    return sizes


def _all_projects(store):
    out = []
    for n in project_dirs(store):
        p = store.read("projects/%s/project.json" % n)
        if isinstance(p, dict) and not p.get("merged_into"):
            out.append(dict(p, id=p.get("id", n)))
    return out


def cmd_board(a):
    store, claims = open_store(a)
    projects = _all_projects(store)
    sizes = _family_sizes(projects)

    def shown(p):
        return version_display(p, sizes.get(p.get("family") or p.get("id"), 1))
    if not a.project:
        rows = [{"id": p["id"], "title": p.get("title", ""), "status": p.get("status"),
                 "archived": p.get("archived"), "rev": p.get("rev"), "version_label": p.get("version_label"),
                 "version_range": p.get("version_range"), "version_display": shown(p)} for p in projects]
        out(a, {"projects": rows}, "\n".join("%s  %s%s" % (p["id"], "[%s] " % p["version_display"]
                                                               if p["version_display"] else "", p["title"])
                                             for p in rows) or "no projects")
        return
    p = get_project(store, a.project)
    rows = board_rows(store, claims, a.project)
    open_rows = [n for n in rows if n["status"] not in CLOSED]
    d = {"project": p, "revision": p.get("rev"), "nodes": rows, "nodes_unpacked": rows,
         "version_display": shown(dict(p, id=p.get("id", a.project)))}
    head = "%s%s rev %s: %d nodes, %d open" % (a.project, " [%s]" % d["version_display"] if d["version_display"] else "",
                                              p.get("rev"), len(rows), len(open_rows))
    out(a, d, "\n".join([head] + ["  %s [%s/%s] %s" % (n["id"], n["status"], n["display_status"], n["title"])
                                  for n in open_rows]))


def node_events(store, project, node, limit=10):
    """The last events naming this task, from every writer's newest log files."""
    found = []
    try:
        writers = sorted(n for n in os.listdir(store.path("events")) if os.path.isdir(store.path("events/" + n)))
    except OSError:
        writers = []
    for w in writers:
        for name in sorted(n for n in store.list_names("events/" + w) if n.endswith(".ndjson"))[-3:]:
            found += [e for e in store.read_log("events/%s/%s" % (w, name))
                      if e.get("project") == project and e.get("node") == node]
    return sorted(found, key=lambda e: e.get("at") or "")[-limit:]


def cmd_node(a):
    store, claims = open_store(a)
    n = get_node(store, a.project, a.id)
    evs = node_events(store, a.project, a.id)
    d = {"revision": n.get("rev"), "node": n, "events": evs, "claim": live_claim(claims, a.project, a.id)}
    lines = ["%s/%s rev %s [%s]" % (a.project, a.id, n.get("rev"), n.get("status")),
             "title: %s" % n.get("title"), "goal: %s" % n.get("goal"),
             "check: %s" % json.dumps(n.get("check")), "evidence: %s" % n.get("evidence")]
    lines += ["  event: %s" % json.dumps(e) for e in evs]
    out(a, d, "\n".join(lines))


# ---- node add / node edit: planning in edit mode (rules in waypoint_data.nodes) ----
NODE_SUBCOMMANDS = ("add", "edit")  # ``waypoint node add|edit ...``; ``waypoint node <project> <id>`` still shows


def _check_arg(a):
    """The done-check from --check JSON or one of the shortcut flags; None when none was given."""
    if getattr(a, "check", None):
        try:
            return json.loads(a.check)
        except ValueError as e:
            raise CliExit(EXIT_USAGE, "--check must be JSON: %s" % e)
    if getattr(a, "check_cmd", None):
        return {"type": "command", "run": a.check_cmd}
    if getattr(a, "check_human", None):
        return {"type": "human", "prompt": a.check_human}
    if getattr(a, "check_files", None):
        return {"type": "files_exist", "paths": a.check_files}
    return None


def _dep(text):
    nid, _, crosses = text.partition("=")
    return {"id": nid, "crosses": crosses} if crosses else nid


def _node_fields(a):
    f = {k: getattr(a, k) for k in ("title", "goal", "kind", "lane", "effort", "recommended", "parent", "estimate")
         if getattr(a, k, None) is not None}
    if getattr(a, "role", None) is not None:
        f["owner"] = a.role
    check = _check_arg(a)
    if check is not None:
        f["check"] = check
    if getattr(a, "dep", None):
        f["deps"] = [_dep(d) for d in a.dep]
    if getattr(a, "file", None):
        f["files"] = list(a.file)
    return f


def _graph_ok(store, project, entity):
    nodes = [n for n in store.list_entities("projects/%s/nodes" % project) if n["id"] != entity["id"]] + [entity]
    known = {n["id"] for n in nodes}
    unknown = [wn.dep_id(d) for d in entity.get("deps") or [] if wn.dep_id(d) not in known]
    if entity.get("parent") and entity["parent"] not in known:
        unknown.append(entity["parent"])
    if unknown:
        raise CliExit(EXIT_ERROR, "unknown dependency or parent in %s: %s" % (project, ", ".join(unknown)))
    try:
        compute_float(nodes)
    except ValueError as e:
        raise CliExit(EXIT_ERROR, "refused: that change makes a %s" % e)


def cmd_node_add(a):
    fields = _node_fields(a)
    if "check" not in fields:
        raise CliExit(EXIT_USAGE, "a new task needs a done-check: --check JSON, --check-cmd, --check-human or --check-files")
    if a.status:
        fields["status"] = a.status
    if a.evidence:
        fields["evidence"] = a.evidence
    if a.status in wn.OWNER_ONLY_STATUSES:
        need_owner(a, "add a task as %s" % a.status)
    store, _claims = open_store(a)
    get_project(store, a.project)
    check_id(a.id, "task id")
    try:
        entity = wn.build_node(a.project, a.id, fields, is_owner=bool(getattr(a, "owner", False)))
    except wn.NodeError as e:
        raise CliExit(EXIT_ERROR, str(e))
    rel = "projects/%s/nodes/%s.json" % (a.project, a.id)
    if store.exists(rel):
        raise CliExit(EXIT_ERROR, "task %s already exists in %s; use waypoint node edit" % (a.id, a.project))
    _graph_ok(store, a.project, entity)
    try:
        d = store.write(rel, dict(entity, actor=store.writer), expected_rev=0)
    except RevConflict:
        raise CliExit(EXIT_ERROR, "task %s already exists in %s; use waypoint node edit" % (a.id, a.project))
    event(store, "node.add", "%s added task %s (%s)." % (store.writer, a.id, d["status"].replace("_", " ")),
          a.project, a.id, {"status": d["status"], "evidence": a.evidence})
    out(a, {"ok": True, "revision": d.get("rev"), "node": d}, "%s/%s added [%s]" % (a.project, a.id, d["status"]))


def cmd_node_edit(a):
    changes = _node_fields(a)
    if a.no_deps:
        changes["deps"] = []
    store, _claims = open_store(a)

    def post():
        n = get_node(store, a.project, a.id)
        if a.rev is not None and a.rev != n.get("rev"):
            raise CliExit(EXIT_STALE, "stale: the task is at revision %s, you read %s" % (n.get("rev"), a.rev))
        ch = dict(changes)
        if a.add_dep:
            ch["deps"] = list(ch.get("deps", n.get("deps") or [])) + [_dep(d) for d in a.add_dep]
        if a.add_file:
            ch["files"] = list(ch.get("files", n.get("files") or [])) + list(a.add_file)
        if not ch:
            raise CliExit(EXIT_USAGE, "nothing to change; see waypoint help node edit")
        try:
            new = wn.apply_edit(n, ch, is_owner=bool(getattr(a, "owner", False)))
        except wn.GoalLocked as e:
            raise CliExit(EXIT_FORBIDDEN, "forbidden: %s; add --owner" % e)
        except wn.NodeError as e:
            raise CliExit(EXIT_ERROR, str(e))
        if new.get("goal") != n.get("goal"):
            need_owner(a, "change a goal")
        _graph_ok(store, a.project, new)
        try:
            return n, ch, store.write("projects/%s/nodes/%s.json" % (a.project, a.id), new, expected_rev=n["rev"])
        except RevConflict as e:
            cur = e.current if isinstance(e.current, dict) else {}
            raise CliExit(EXIT_STALE, "stale: the task is at revision %s; run the command again (or add --retry)"
                          % cur.get("rev"))
    try:
        old, ch, d = post()
    except CliExit as e:
        if e.code == EXIT_STALE and a.retry and a.rev is None:
            old, ch, d = post()
        else:
            raise
    reset = old.get("status") != d.get("status")
    event(store, "node.edit", "%s edited task %s (%s)%s." % (store.writer, a.id, ", ".join(sorted(ch)),
                                                            "; the new check sends it back to not started" if reset else ""),
          a.project, a.id, {"fields": sorted(ch), "reset": reset})
    out(a, {"ok": True, "revision": d.get("rev"), "node": d, "reset": reset},
        "%s/%s edited (rev %s)%s" % (a.project, a.id, d.get("rev"), ", reset to not_started" if reset else ""))


def set_status(a, status):
    if status not in STATUSES:
        raise CliExit(EXIT_ERROR, "status must be one of %s" % ", ".join(STATUSES))
    if status in VERSION_ONLY:
        raise CliExit(EXIT_ERROR, "status %r is set only by a version roll-over (use waypoint version new/decide)"
                      % status)
    if status == "done":
        need_owner(a, "mark a task done")
    store, _claims = open_store(a)

    def post():
        n = get_node(store, a.project, a.id)
        new = body_of(n)
        new.update(status=status, evidence=a.evidence, actor=store.writer)
        try:
            return store.write("projects/%s/nodes/%s.json" % (a.project, a.id), new, expected_rev=n["rev"])
        except RevConflict as e:
            cur = e.current if isinstance(e.current, dict) else {}
            raise CliExit(EXIT_STALE, "stale: the task is at revision %s; run the command again (or add --retry)"
                          % cur.get("rev"))
    try:
        d = post()
    except CliExit as e:
        if e.code == EXIT_STALE and a.retry:
            d = post()
        else:
            raise
    event(store, "node.status", "%s set task %s to %s." % (store.writer, a.id, status.replace("_", " ")),
          a.project, a.id, {"status": status, "evidence": a.evidence})
    out(a, {"ok": True, "revision": d.get("rev"), "node": d}, "%s/%s -> %s" % (a.project, a.id, status))


def cmd_done(a):
    set_status(a, "done")


def cmd_status(a):
    set_status(a, a.status)


TODO_CATEGORIES = (("review", "Review"), ("decisions", "Decisions"), ("hands_on", "Hands-on"))
SECTION_ALIAS = {"human": "hands_on"}  # the 1.1 section name for Owner's own to-dos
OPEN_TODO = ("open", "handed_off")  # what the app and the mod list as open
CATEGORY_SUBTYPE = {"review": "failure", "decisions": "approval", "hands_on": "own"}


def todo_status(t):
    return t.get("status") or "open"


def todo_category(name):
    cat = SECTION_ALIAS.get(name, name)
    if cat not in CATEGORY_SUBTYPE:
        raise CliExit(EXIT_USAGE, "to-do category must be one of: %s (human = hands_on)" % ", ".join(CATEGORY_SUBTYPE))
    return cat


def todo_rows(todos, section=None, show_all=False):
    """Open to-dos (or every one with ``show_all``), grouped in category order, each in position order."""
    order = {c: i for i, (c, _label) in enumerate(TODO_CATEGORIES)}
    cat = todo_category(section) if section else None
    rows = [t for t in todos if (not cat or t.get("category") == cat) and (show_all or todo_status(t) in OPEN_TODO)]
    return sorted(rows, key=lambda t: (order.get(t.get("category"), len(order)), t.get("order") or 0))


def todo_text(rows, show_all=False):
    """Grouped lines: a heading per category, then numbered rows; a status tag on every row with --all."""
    if not rows:
        return "no to-dos" if show_all else "no open to-dos (waypoint todo list --all shows every one)"
    labels = dict(TODO_CATEGORIES)
    extra = sorted({t.get("category") or "" for t in rows} - set(labels))
    lines = []
    for sec in [c for c, _label in TODO_CATEGORIES] + extra:
        group = [t for t in rows if (t.get("category") or "") == sec]
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
    store, _claims = open_store(a)
    s = a.todo_cmd
    if s == "list":
        ts = todo_rows(store.list_entities("todos"), section=a.section, show_all=a.all)
        out(a, {"todos": ts}, todo_text(ts, show_all=a.all))
    elif s == "add":
        cat = todo_category("human" if a.human else (a.category or "hands_on"))
        project = node_ref = None
        if a.node:
            if "/" not in a.node:
                raise CliExit(EXIT_USAGE, "--node must be project/id")
            project, nid = a.node.split("/", 1)
            check_id(project, "--node project")
            check_id(nid, "--node id")
            node_ref = {"project": project, "id": nid}
        text = " ".join(a.text.split())
        if not text:
            raise CliExit(EXIT_USAGE, "the to-do text must not be empty")
        existing = [t for t in store.list_entities("todos") if t.get("category") == cat]
        order = a.position if a.position is not None else 1 + max([t.get("order") or 0 for t in existing] or [0])
        tid = "td-" + secrets.token_hex(4)
        entity = {"schema": "waypoint.todo.v1", "id": tid, "text": text, "status": "open",
                  "category": cat, "subtype": CATEGORY_SUBTYPE[cat], "link": a.link or None, "node": node_ref,
                  "project": project, "order": order, "merged_into": None, "click_count": 0, "next_click": None,
                  "created_at": iso(time.time()), "created_by": store.writer}
        d = store.write("todos/%s.json" % tid, entity, expected_rev=0)
        out(a, {"ok": True, "todo": d}, "added %s" % tid)
    elif s == "tick":
        check_id(a.id, "to-do id")
        rel = "todos/%s.json" % a.id
        t = store.read(rel)
        if t is None:
            raise CliExit(EXIT_ERROR, "no to-do %s" % a.id)
        if t.get("status") != "done":
            if t.get("status") not in OPEN_TODO:
                raise CliExit(EXIT_ERROR, "to-do %s is %s; only an open to-do can be ticked" % (a.id, t.get("status")))
            new = body_of(t)
            new.update(status="done", done_by=store.writer, archived_at=iso(time.time()))
            try:
                t = store.write(rel, new, expected_rev=t["rev"])
            except RevConflict:
                raise CliExit(EXIT_STALE, "stale: the to-do changed; run the command again")
        out(a, {"ok": True, "todo": t}, "ticked %s" % a.id)
    elif s == "reorder":
        cat = todo_category(a.section)
        for n, tid in enumerate(a.ids, 1):
            check_id(tid, "to-do id")
            rel = "todos/%s.json" % tid
            t = store.read(rel)
            if t is None or t.get("category") != cat:
                raise CliExit(EXIT_ERROR, "no %s to-do %s" % (cat, tid))
            if t.get("order") == n:
                continue
            new = body_of(t)
            new["order"] = n
            try:
                store.write(rel, new, expected_rev=t["rev"])
            except RevConflict:
                raise CliExit(EXIT_STALE, "stale: to-do %s changed; run the command again" % tid)
        out(a, {"ok": True}, "reordered %d in %s" % (len(a.ids), cat))


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
    store, _claims = open_store(a)
    rows = []
    for name in store.list_names("receipts"):
        if not name.endswith(".json"):
            continue
        r = store.read("receipts/" + name)
        if isinstance(r, dict):
            try:
                at = iso(os.path.getmtime(store.path("receipts/" + name)))
            except OSError:
                at = ""
            rows.append(dict(r, ingested_at=at, file=name))
    rows.sort(key=lambda r: r["ingested_at"], reverse=True)
    rows = rows[:max(a.limit, 0)]
    lines = []
    for r in rows:
        who = ("%s/%s" % (r.get("project"), r.get("node"))) if r.get("node") else "session"
        lines.append("%s  %-9s %s  %s" % ((r.get("ingested_at") or "")[:19], r.get("status"), who,
                                          r.get("summary") or ""))
    out(a, {"receipts": rows}, "\n".join(lines) or "no receipts")


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
    store, _claims = open_store(a)
    agents = []
    for s in store.list_entities("sessions"):
        if isinstance(s, dict) and s.get("id"):
            agents.append({"id": s["id"], "parent": s.get("parent"), "title": s.get("title"), "tool": s.get("agent"),
                           "status": s.get("status")})
    d = {"agents": agents, "roots": [x["id"] for x in agents if not x["parent"]]}
    out(a, d, "\n".join(tree_lines(d)) or "no agents")


def claim_token(by):
    """The same claimer always gets the same token, so a refresh and a later release by name work with no state."""
    return "cli-" + hashlib.sha256(by.encode("utf-8")).hexdigest()[:16]


def cmd_claim(a):
    store, claims = open_store(a)
    get_node(store, a.project, a.node)
    location = a.worktree or a.cwd or os.getcwd()
    try:
        r = claims.claim(a.project, a.node, "mod", location, claim_token(a.by), claimed_by=a.by, environment=a.env)
    except ValueError as e:
        raise CliExit(EXIT_USAGE, str(e))
    if not r.ok:
        h = r.holder or {}
        raise CliExit(EXIT_ERROR, "claim refused: %s/%s is held by %s since %s" % (
            a.project, a.node, h.get("claimed_by") or h.get("owner") or "someone", h.get("since") or "?"))
    if r.reclaimed_from:
        event(store, "claim.reclaimed", "Reclaimed %s from the stale claim of %s." % (
            a.node, r.reclaimed_from.get("claimed_by") or r.reclaimed_from.get("owner")), a.project, a.node)
    c = r.claim or {}
    refreshed = c.get("since") != c.get("heartbeat_at")
    out(a, {"ok": True, "claim": dict(c, by=a.by), "refreshed": refreshed},
        "%s %s/%s for %s" % ("refreshed" if refreshed else "claimed", a.project, a.node, a.by))


def cmd_release(a):
    store, claims = open_store(a)
    check_id(a.project, "project")
    check_id(a.node, "task id")
    cur = claims.read(a.project, a.node)
    if not isinstance(cur, dict):
        out(a, {"released": False}, "%s/%s had no claim" % (a.project, a.node))
        return
    token = claim_token(a.by) if a.by else cur.get("token")
    if not claims.release(a.project, a.node, token):
        raise CliExit(EXIT_ERROR, "release refused: %s/%s is held by %s, not %s" % (
            a.project, a.node, cur.get("claimed_by") or cur.get("owner"), a.by))
    n = store.read("projects/%s/nodes/%s.json" % (a.project, a.node)) or {}
    out(a, {"released": True, "status": n.get("status")}, "released %s/%s; it is now %s" % (
        a.project, a.node, str(n.get("status")).replace("_", " ")))


def runner_command():
    """The command that starts the runner on demand (core/docs/runner/RUNNER.md): WAYPOINT_PYTHON or ``python`` in
    the settings file (the pinned interpreter), else the one running this CLI; cwd is the repo's runner folder."""
    py = os.environ.get("WAYPOINT_PYTHON") or load_config().get("python") or sys.executable
    return [str(py), "-m", "waypoint_runner", "--folder", str(data_dir())], REPO / "runner"


def start_runner():
    """Start the runner detached. It takes the lock itself and exits 3 (ignored here) when one is already up.
    Returns None, or a plain reason it could not start."""
    cmd, cwd = runner_command()
    flags = 0
    if os.name == "nt":
        flags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
                 | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        subprocess.Popen(cmd, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
    except OSError as e:
        return "could not start the runner (%s)" % e
    return None


def cmd_run(a):
    need_owner(a, "start a run")
    try:
        settings = json.loads(a.settings) if a.settings else {}
    except ValueError:
        raise CliExit(EXIT_USAGE, "--settings must be valid JSON")
    if not isinstance(settings, dict):
        raise CliExit(EXIT_USAGE, "--settings must be a JSON object")
    store, _claims = open_store(a)
    get_project(store, a.project)
    rid = "run-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(2)
    run = store.write("runs/%s.json" % rid, {"schema": "waypoint.run.v1", "id": rid, "project": a.project,
                                             "settings": settings, "status": "queued", "created_at": iso(time.time()),
                                             "started_at": None, "ended_at": None, "sprints": []}, expected_rev=0)
    event(store, "run", "owner queued a run of %s." % a.project, a.project, None, {"run": rid})
    note = None if a.no_runner else start_runner()
    d = {"ok": True, "id": rid, "status": "queued", "run": run, "runner_started": not a.no_runner and note is None}
    out(a, d, "run %s" % rid + ("; " + note if note else ""))


def start_usage_refresh():
    """Start the usage collector detached (it writes usage/<pool>.json, ~45 s). None, or a plain reason."""
    cmd, cwd = runner_command()
    cmd = cmd[:2] + ["waypoint_runner.usage"] + cmd[3:]  # python -m waypoint_runner.usage --folder DIR
    flags = 0
    if os.name == "nt":
        flags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
                 | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        subprocess.Popen(cmd, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
    except OSError as e:
        return "could not start the usage refresh (%s)" % e
    return None


def _usage_fresh_minutes(store):
    try:
        v = (store.read("settings/settings.json") or {}).get("usage_fresh_minutes")
    except Exception:
        v = None
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else 30


def _read_age_minutes(read_at):
    try:
        t = datetime.datetime.fromisoformat(str(read_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.timezone.utc)
    return (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds() / 60


def cmd_usage(a):
    """Stored readings. Nothing else collects usage in 1.2, so when no pool has a fresh reading this starts the
    collector in the background (a later call shows the numbers) and says so; it never blocks on a slow source."""
    store, _claims = open_store(a)
    rows = [u for u in store.list_entities("usage") if isinstance(u, dict) and u.get("pool")]
    window = _usage_fresh_minutes(store)
    ages = [_read_age_minutes(u.get("read_at")) for u in rows if u.get("remaining_percent") is not None]
    refreshing = not any(m is not None and m <= window for m in ages)
    note = None
    if refreshing:
        note = start_usage_refresh()
        refreshing = note is None
    lines = []
    for p in sorted(rows, key=lambda u: u["pool"]):
        r = p.get("remaining_percent")
        lines.append("%s: %s, %s" % (p["pool"], "unknown" if r is None else "%g%% left" % r,
                                     "read %s" % p["read_at"] if p.get("read_at") else "never read"))
    if refreshing:
        lines.append("refreshing usage in the background (about 1-2 min); run 'waypoint usage' again")
    elif note:
        lines.append(note)
    out(a, {"pools": rows, "refreshing": refreshing}, "\n".join(lines))


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
    """Mirror of the cloud sync importer's expectations; returns an error string or None."""
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


LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9 ._-]{0,30}[A-Za-z0-9])?$")  # as the runner's version_label


def version_name(p):
    """How a version shows: the product's own label (V10, 1.2) if set, else the family ordinal (v2)."""
    return p.get("version_label") or "v%s" % (p.get("version") if p.get("version") is not None else "?")


def check_label(value):
    text = " ".join(str(value).split())
    if not LABEL_RE.match(text):
        raise CliExit(EXIT_USAGE, "label must be a short version name such as V10 or 1.2 (letters, digits, "
                                  "space . _ -; at most 32 characters)")
    return text


def family_of(store, target):
    """The family entity named ``target``, or None."""
    if not ID_RE.match(str(target)):
        return None
    return store.read("families/%s.json" % target)


def version_files(store, project):
    """The version records of one project, oldest first."""
    try:
        names = [n for n in store.list_names("versions/%s" % project) if n.endswith(".json")]
    except (OSError, ValueError):
        return []
    recs = [r for r in (store.read("versions/%s/%s" % (project, n)) for n in names) if isinstance(r, dict)]
    return sorted(recs, key=lambda r: r.get("n") or 0)


def node_counts(store, project):
    c = {"done": 0, "open": 0, "skipped": 0, "carried": 0}
    for n in store.list_entities("projects/%s/nodes" % project):
        st = n.get("status")
        c[st if st in ("done", "skipped", "carried") else "open"] += 1
    return c


def cmd_history_data(store, target):
    fam = family_of(store, target)
    if fam is None:
        p = get_project(store, target)
        fam = family_of(store, p.get("family")) if p.get("family") else None
        members = fam.get("projects") if fam else [target]
    else:
        members = fam.get("projects") or []
    versions, skipped = [], []
    for m in members:
        p = store.read("projects/%s/project.json" % m) or {}
        versions.append({"version": p.get("version"), "version_label": p.get("version_label"),
                         "name": version_name(p), "project": m, "title": p.get("title"),
                         "counts": node_counts(store, m), "archived": bool(p.get("archived"))})
        for rec in version_files(store, m):
            for d in rec.get("decisions") or []:
                if d.get("action") == "skip":
                    skipped.append({"version": rec.get("n"), "version_label": p.get("version_label"),
                                    "name": version_name(p), "project": m,
                                    "id": d.get("node") or d.get("id"), "title": d.get("title"),
                                    "reason": d.get("reason")})
    current = members[-1] if members else None
    versions.sort(key=lambda v: (v["version"] if v["version"] is not None else -1), reverse=True)  # newest first
    skipped.sort(key=lambda k: (k["version"] if k["version"] is not None else -1), reverse=True)
    return {"family": fam, "current": current, "versions": versions, "skipped": skipped}


def _new_version_source(store, target):
    """``(project to roll over, family id or None)``. A family id rolls over its current version; naming an earlier
    version of a family is a usage error that names the current one."""
    fam = family_of(store, target)
    if fam is not None:
        members = fam.get("projects") or []
        if not members:
            raise CliExit(EXIT_ERROR, "family %s has no projects" % target)
        return members[-1], target
    p = get_project(store, target)
    f = family_of(store, p.get("family")) if p.get("family") else None
    if f and f.get("projects") and f["projects"][-1] != target:
        raise CliExit(EXIT_USAGE, "only the current version (%s) can roll over; %s is an earlier version of %s"
                      % (f["projects"][-1], target, f["id"]))
    return target, p.get("family")


def _kv_skips(items):
    res = {}
    for it in items or []:
        nid, sep, reason = it.partition("=")
        if not sep or not nid.strip() or not reason.strip():
            raise CliExit(EXIT_USAGE, "--skip needs ID=reason with a non-empty reason (got %r)" % it)
        res[nid.strip()] = {"action": "skip", "reason": reason.strip()}
    return res


def _decisions(a):
    dec = _kv_skips(a.skip)
    for nid in a.carry or []:
        if nid in dec:
            raise CliExit(EXIT_USAGE, "%s is both --carry and --skip" % nid)
        dec[nid] = {"action": "carry"}
    return dec


def _unfinished(store, project):
    return [n for n in store.list_entities("projects/%s/nodes" % project) if n.get("status") not in CLOSED]


def _check_ids(dec, unfinished):
    bad = sorted(set(dec) - {n["id"] for n in unfinished})
    if bad:
        raise CliExit(EXIT_USAGE, "not an unfinished task: %s" % ", ".join(bad))


def queue_action(store, a, typ, payload):
    """Write actions/<id>.json for the runner to handle (owner actions carry by=owner). Returns the action id."""
    aid = "act-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(3)
    entity = {"schema": "waypoint.action.v1", "id": aid, "type": typ, "status": "queued", "payload": payload,
              "created_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "by": "owner" if a.owner else "cli"}
    try:
        wio.create_exclusive(store.path("actions/%s.json" % aid), serialize(entity), writer=store.writer or "cli")
    except FileExistsError:
        raise CliExit(EXIT_ERROR, "action id clash; run the command again")
    return aid


def cmd_version(a):
    s = a.version_cmd
    store, _claims = open_store(a)
    if s == "history":
        d = cmd_history_data(store, a.target)
        fam = d.get("family") or {}
        lines = ["%s (%s)" % (fam.get("title"), fam.get("id") or "no family")]
        for v in d["versions"]:  # newest first (cmd_history_data orders them)
            c = v["counts"]
            lines.append("  %s %s%s  %s  done %s, open %s, skipped %s, carried %s%s" % (
                v["name"], v["project"], " *current*" if v["project"] == d["current"] else "",
                v.get("title") or "", c["done"], c["open"], c["skipped"], c["carried"],
                " [archived]" if v.get("archived") else ""))
        if d["skipped"]:
            lines.append("skipped ideas:")
            lines += ["  %s %s/%s %s: %s" % (k["name"], k["project"], k.get("id"), k.get("title"),
                                             k.get("reason")) for k in d["skipped"]]
        out(a, d, "\n".join(lines))
    elif s == "new":
        dec = _decisions(a)
        if a.id:
            raise CliExit(EXIT_USAGE, "--id is not supported in 1.2: the new project id comes from the family and "
                                      "version number")
        source, _fam = _new_version_source(store, a.target)
        unfinished = _unfinished(store, source)
        _check_ids(dec, unfinished)
        if a.label:
            check_label(a.label)
        if a.dry_run:
            plan_rows = [{"id": n["id"], "title": n.get("title"), **(dec.get(n["id"]) or {"action": "carry"})}
                         for n in unfinished]
            lines = ["dry run for %s (%d unfinished); nothing written" % (source, len(plan_rows))]
            lines += ["  %s %s  %s%s" % (r["action"], r["id"], r["title"],
                                         "  (%s)" % r["reason"] if r.get("reason") else "") for r in plan_rows]
            out(a, {"dry_run": True, "source": source, "decisions": plan_rows}, "\n".join(lines))
            return
        need_owner(a, "start a new version")
        payload = {"project": source, "decisions": dec}
        if a.label:
            payload["label"] = check_label(a.label)
        if a.title:
            payload["title"] = a.title
        aid = queue_action(store, a, "version_new", payload)
        note = start_runner() if not a.no_runner else None
        out(a, {"ok": True, "action": aid, "status": "queued", "source": source},
            "queued %s: new version of %s, %d unfinished%s" % (aid, source, len(unfinished), "; " + note if note else ""))
    elif s == "label":
        p = get_project(store, a.project)
        if a.label is None and not a.clear:
            out(a, {"project": a.project, "version": p.get("version"), "version_label": p.get("version_label"),
                    "name": version_name(p)}, "%s %s" % (a.project, version_name(p)))
            return
        if a.label is not None and a.clear:
            raise CliExit(EXIT_USAGE, "label: give a label or --clear, not both")
        label = None if a.clear else check_label(a.label)
        need_owner(a, "change a version label")
        aid = queue_action(store, a, "version_label", {"project": a.project, "label": label})
        note = start_runner() if not a.no_runner else None
        out(a, {"ok": True, "action": aid, "status": "queued", "version_label": label},
            "queued %s: %s version label %s%s" % (aid, a.project, label or "cleared", "; " + note if note else ""))
    elif s == "merge":
        get_project(store, a.keep)
        if get_project(store, a.drop).get("merged_into"):
            raise CliExit(EXIT_USAGE, "%s was already merged" % a.drop)
        need_owner(a, "merge two versions")
        aid = queue_action(store, a, "version_merge", {"keep": a.keep, "drop": a.drop})
        note = start_runner() if not a.no_runner else None
        out(a, {"ok": True, "action": aid, "status": "queued"},
            "queued %s: merge %s into %s%s" % (aid, a.drop, a.keep, "; " + note if note else ""))
    elif s == "family":
        need_owner(a, "group projects into a family")
        check_id(a.family, "family")
        for m in a.members:
            get_project(store, m)
        aid = queue_action(store, a, "family_group", {"family": a.family, "projects": list(a.members)})
        note = start_runner() if not a.no_runner else None
        out(a, {"ok": True, "action": aid, "status": "queued"},
            "queued %s: family %s: %s (current %s)%s" % (aid, a.family, ", ".join(a.members), a.members[-1],
                                                         "; " + note if note else ""))
    elif s == "decide":
        dec = _decisions(a)
        if not dec:
            raise CliExit(EXIT_USAGE, "give at least one --carry ID or --skip ID=reason")
        _check_ids(dec, _unfinished(store, a.project))
        need_owner(a, "change a version decision")
        target = None
        for pid in project_dirs(store):
            for rec in version_files(store, pid):
                if rec.get("from_project") == a.project:
                    target = (pid, rec.get("n"))
        if target is None:
            raise CliExit(EXIT_ERROR, "no later version grew from %s; start one with: waypoint version new" % a.project)
        ids = []
        for nid, d in dec.items():
            payload = {"project": target[0], "version": str(target[1]), "node": nid,
                       "decision": "keep" if d["action"] == "carry" else "discard"}
            if d.get("reason"):
                payload["reason"] = d["reason"]
            ids.append(queue_action(store, a, "version_decide", payload))
        note = start_runner() if not a.no_runner else None
        out(a, {"ok": True, "actions": ids, "status": "queued"},
            "queued %d decision(s) for %s%s" % (len(ids), a.project, "; " + note if note else ""))


# ---- versions, engine (retired), doctor ----
ENGINE_RETIRED = ("the Waypoint engine is retired in 1.2: the CLI reads and writes the data folder directly "
                  "and the runner starts on demand")


def version_line():
    folder = str(data_dir())
    return ({"cli": __version__, "engine": None, "api": None, "engine_retired": True, "data_dir": folder},
            "waypoint %s (engine retired; data folder %s)" % (__version__, folder))


BRIEF_MAX = 64 * 1024  # characters; the brief lives inside projects/<id>/project.json


def cmd_brief(a):
    """Show or edit a project's brief (``brief`` in projects/<id>/project.json): direction, decisions, open state.

    Every edit is a compare-and-swap on the project's revision: ``--rev N`` names the revision you read (else the
    one read now), and a project changed in between is refused with exit 3."""
    store, _claims = open_store(a)
    p = get_project(store, a.project)
    if a.append is not None and a.set_file is not None:
        raise CliExit(EXIT_USAGE, "brief: give --append or --set-file, not both")
    if a.append is None and a.set_file is None:
        if a.rev is not None:
            raise CliExit(EXIT_USAGE, "brief: --rev goes with --append or --set-file")
        text = p.get("brief") or ""
        out(a, {"project": a.project, "rev": p.get("rev"), "brief": text},
            "%s rev %s\n%s" % (a.project, p.get("rev"), text or "(no brief yet)"))
        return
    if a.rev is not None and a.rev != p.get("rev"):
        raise CliExit(EXIT_STALE, "stale: %s is at revision %s, not %s; read the brief again"
                      % (a.project, p.get("rev"), a.rev))
    old = p.get("brief") or ""
    if a.set_file is not None:
        try:
            new = Path(a.set_file).read_text(encoding="utf-8-sig")
        except OSError as e:
            raise CliExit(EXIT_ERROR, "brief: cannot read %s (%s)" % (a.set_file, e))
        what = "replaced"
    else:
        line = a.append.strip()
        if not line:
            raise CliExit(EXIT_USAGE, "brief: --append text must not be empty")
        new = (old.rstrip("\n") + "\n" + line) if old.strip() else line
        what = "appended to"
    if len(new) > BRIEF_MAX:
        raise CliExit(EXIT_USAGE, "brief: the brief would be over %d characters" % BRIEF_MAX)
    try:
        saved = store.write("projects/%s/project.json" % a.project, dict(body_of(p), brief=new),
                            expected_rev=p.get("rev"))
    except RevConflict as e:
        cur = getattr(e, "current", None) or {}
        raise CliExit(EXIT_STALE, "stale: %s changed (now revision %s); run the command again"
                      % (a.project, cur.get("rev") if isinstance(cur, dict) else "?"))
    event(store, "project.brief", "%s %s the brief of %s." % (writer_id(a), what, a.project), a.project, None,
          {"rev": saved.get("rev"), "chars": len(new)})
    out(a, {"ok": True, "project": a.project, "rev": saved.get("rev"), "brief": new},
        "%s the brief of %s (rev %s)" % (what, a.project, saved.get("rev")))


def cmd_version_flag(a):
    data, text = version_line()
    out(a, data, text)


def cmd_engine(a):
    out(a, {"running": False, "retired": True, "action": a.engine_cmd},
        "engine: %s; there is nothing to %s. Check the folder and runner with: waypoint doctor"
        % (ENGINE_RETIRED, a.engine_cmd if a.engine_cmd != "status" else "check"))
    return EXIT_DOWN


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
    add("skip", "engine", ENGINE_RETIRED)
    ddir = data_dir()
    if not ddir.is_dir():
        add("FAIL", "folder", "data folder %s does not exist; set WAYPOINT_DATA_DIR or data_dir in %s"
            % (ddir, config_path()))
    else:
        projects = len(project_dirs(Store(str(ddir))))
        add("ok", "folder", "%s (%d project%s)" % (ddir, projects, "" if projects == 1 else "s"))
    cmd, cwd = runner_command()
    if (cwd / "waypoint_runner" / "__main__.py").is_file():
        add("ok", "runner", "starts on demand with: %s (in %s)" % (" ".join(cmd), cwd))
    else:
        add("FAIL", "runner", "waypoint_runner not found in %s; run the CLI from a full clone" % cwd)
    add("ok" if _read(owner_token_path()) else "skip", "owner",
        "owner.token present at %s" % owner_token_path() if _read(owner_token_path())
        else "no owner.token at %s, so --owner actions (done, run, version) are refused" % owner_token_path())
    state, detail = mod_check()
    add(state, "mod", detail)
    failed = [c for c in checks if c["state"] == "FAIL"]
    text = "\n".join("%-4s  %-6s  %s" % (c["state"], c["check"], c["detail"]) for c in checks)
    text += "\n" + ("all checks passed" if not failed else "%d check(s) failed" % len(failed))
    out(a, {"ok": not failed, "engine_retired": True, "checks": checks}, text)
    return EXIT_ERROR if failed else EXIT_OK


# ---- help and argument parsing ----
SHORT_HELP = """waypoint {version}: work with the Waypoint data folder from the command line.

Usage: waypoint <command> [options]

Commands:
{commands}

Examples:
  waypoint board                         list projects
  waypoint board my-project              show a project's open tasks
  waypoint done my-project T3 --evidence "tests pass"
  waypoint todo add "Review the draft" --human
  waypoint doctor

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
                        help="act as the owner (needs owner.token in the data folder)")
    top = Parser(add_help=False)
    top.add_argument("--json", action="store_true", help="print JSON")
    top.add_argument("--owner", action="store_true", help="act as the owner (needs owner.token in the data folder)")
    top.add_argument("--version", action="store_true", dest="show_version", help="print the CLI version and data folder")
    ap = Parser(prog="waypoint", parents=[top], description="Work with the Waypoint data folder from the command line.")

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
    p = add(sub, "node", "Show one task (node <project> <id>), or add / edit one: node add|edit <project> <id>",
            ["waypoint node my-project T3", 'waypoint node add my-project T9 --goal "Fix the parser." --check-cmd "pytest -q"',
             'waypoint node edit my-project T9 --title "Parser fix" --add-dep T3 --rev 4'])
    p.add_argument("project", help="project id"); p.add_argument("id", help="task id"); p.set_defaults(fn=cmd_node)
    for name in NODE_SUBCOMMANDS:  # not listed on their own; main() maps 'node add' / 'node edit' to them
        line = ("Add a task to a project now, with a goal and a done-check (planning in edit mode)" if name == "add"
                else "Edit a task in place: title, check, deps, files and more (goal: owner only)")
        ex = (['waypoint node add my-project T9 --goal "Fix the parser." --check-cmd "pytest -q" --dep T3',
               'waypoint node add my-project T10 --goal "Shipped V2." --check-human "ok" --status done --evidence "abc123" --owner']
              if name == "add" else
              ['waypoint node edit my-project T9 --title "Parser fix" --add-file src/parse.py',
               'waypoint node edit my-project T9 --check-cmd "pytest tests/test_parse.py -q" --rev 4'])
        p = sub.add_parser("node-" + name, parents=[common], prog="waypoint node " + name, description=line,
                           epilog=_examples(*ex))
        p.add_argument("project", help="project id"); p.add_argument("id", help="task id")
        p.add_argument("--title", help="short task title")
        p.add_argument("--goal", required=(name == "add"), help="one sentence: what is true when it is done"
                       + ("" if name == "add" else " (owner only)"))
        g = p.add_mutually_exclusive_group()
        g.add_argument("--check", metavar="JSON", help='done-check as JSON, e.g. {"type": "command", "run": "pytest -q"}')
        g.add_argument("--check-cmd", metavar="CMD", help="done-check: this command passes")
        g.add_argument("--check-human", metavar="TEXT", help="done-check: Owner confirms this")
        g.add_argument("--check-files", metavar="PATH", nargs="+", help="done-check: these files exist")
        p.add_argument("--dep", action="append", metavar="ID[=CROSSES]",
                       help="a task it needs first (repeatable; on edit it replaces the list)")
        p.add_argument("--file", action="append", metavar="PATH",
                       help="a file it writes (repeatable; on edit it replaces the list)")
        p.add_argument("--estimate", type=float, help="duration units (default 1)")
        p.add_argument("--kind", help="worker, code, gate, splitter, discovery ...")
        p.add_argument("--lane", help="workstream label"); p.add_argument("--role", help="node owner role, e.g. worker, human")
        p.add_argument("--effort", help="low / medium / high"); p.add_argument("--recommended", help="model id")
        p.add_argument("--parent", help="parent task id")
        if name == "add":
            p.add_argument("--status", choices=[s for s in STATUSES if s not in VERSION_ONLY],
                           help="starting status (default not_started; done needs --owner)")
            p.add_argument("--evidence", help="what shows the status, e.g. a commit")
            p.set_defaults(fn=cmd_node_add)
        else:
            p.add_argument("--add-dep", action="append", metavar="ID[=CROSSES]", help="add one dependency (repeatable)")
            p.add_argument("--no-deps", action="store_true", help="remove every dependency")
            p.add_argument("--add-file", action="append", metavar="PATH", help="add one file (repeatable)")
            p.add_argument("--rev", type=int, help="the task revision you read; refused (exit 3) if it changed")
            p.add_argument("--retry", action="store_true", help="read the task again and retry once if it changed")
            p.set_defaults(fn=cmd_node_edit)
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
    p.add_argument("--section", choices=["review", "decisions", "hands_on", "human"],
                   help="only this category (human = hands_on)")
    p.add_argument("--all", action="store_true", help="also show done, dropped and merged to-dos, each with its status")
    p.set_defaults(fn=cmd_todo)
    p = add(ts, "add", "Add a to-do (hands_on unless --category; --human = hands_on)",
            ['waypoint todo add "Review the draft" --human --node my-project/T3'])
    p.add_argument("text", help="what to do"); p.add_argument("--human", action="store_true", help="hands_on (Owner's own); same as --category hands_on")
    p.add_argument("--category", choices=["review", "decisions", "hands_on"], help="category (default hands_on)")
    p.add_argument("--position", type=int, help="place it at this position (0 = top)")
    p.add_argument("--link", help="a link to attach"); p.add_argument("--node", help="the task it belongs to, as project/id")
    p.set_defaults(fn=cmd_todo)
    p = add(ts, "tick", "Tick a to-do off", ["waypoint todo tick td12"])
    p.add_argument("id", help="to-do id"); p.set_defaults(fn=cmd_todo)
    p = add(ts, "reorder", "Put a section's to-dos in a new order", ["waypoint todo reorder --section human td3 td1 td2"])
    p.add_argument("--section", required=True, choices=["review", "decisions", "hands_on", "human"], help="which category (human = hands_on)")
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
    p.add_argument("--no-runner", action="store_true", help="queue the run but do not start the runner")
    p.set_defaults(fn=cmd_run)
    p = add(sub, "usage", "Show how much usage is left in each pool", ["waypoint usage"]); p.set_defaults(fn=cmd_usage)
    p = add(sub, "brief", "Show or edit a project's brief: direction, decisions and open state",
            ["waypoint brief my-project", 'waypoint brief my-project --append "Decided: keep the CLI stdlib only"',
             "waypoint brief my-project --set-file brief.md --rev 7"])
    p.add_argument("project", help="project id")
    p.add_argument("--append", metavar="TEXT", help="add one line at the end of the brief")
    p.add_argument("--set-file", metavar="PATH", help="replace the brief with this UTF-8 file")
    p.add_argument("--rev", type=int, help="the project revision you read; refused (exit 3) if it changed")
    p.set_defaults(fn=cmd_brief)

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
        if name == "decide":
            p.add_argument("--no-runner", action="store_true", help="queue the actions but do not start the runner")
        if name == "new":
            p.add_argument("--label", help="the product's own version name, e.g. V10 or 1.2; it names the new project "
                                           "id (my-family-v10) and shows instead of the ordinal")
            p.add_argument("--title", help="full title of the new version (default: family title and label)")
            p.add_argument("--id", help="not supported in 1.2")
            p.add_argument("--no-runner", action="store_true", help="queue the action but do not start the runner")
            p.add_argument("--dry-run", action="store_true", help="show what would happen and write nothing")
        p.set_defaults(fn=cmd_version)
    p = add(vs, "label", "Show or set a version's own label, e.g. V10 (setting is an owner action)",
            ["waypoint version label my-project-v2", "waypoint version label my-project-v2 V10 --owner"])
    p.add_argument("project", help="project id"); p.add_argument("label", nargs="?", help="the new label, e.g. V10")
    p.add_argument("--clear", action="store_true", help="remove the label (the ordinal shows again)")
    p.add_argument("--no-runner", action="store_true", help="queue the action but do not start the runner")
    p.set_defaults(fn=cmd_version)
    p = add(vs, "merge", "Fold the tasks of a duplicate version into its twin (owner action)",
            ["waypoint version merge my-project-v1 my-project-v1-copy --owner"])
    p.add_argument("keep", help="project that keeps the version number"); p.add_argument("drop", help="project merged away")
    p.add_argument("--no-runner", action="store_true", help="queue the action but do not start the runner")
    p.set_defaults(fn=cmd_version)
    p = add(vs, "family", "Group projects as versions of one product (owner action)",
            ['waypoint version family my-family --title "My product" my-project-v1 my-project-v2'])
    p.add_argument("family", help="family id"); p.add_argument("--title", help="family title (the runner uses the id)")
    p.add_argument("--no-runner", action="store_true", help="queue the action but do not start the runner")
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

    e = add(sub, "engine", "The engine is retired in 1.2 (answers exit 5); use doctor",
            ["waypoint engine status", "waypoint engine start", "waypoint engine stop --port 8752"])
    es = group(e, "engine_cmd", "engine commands")
    for name, line in (("start", "Retired: there is no engine to start"),
                       ("stop", "Retired: there is no engine to stop"),
                       ("status", "Retired: reports that the engine is retired")):
        p = add(es, name, line, ["waypoint engine %s" % name, "waypoint engine %s --port 8752" % name])
        p.add_argument("--port", type=int, help="ignored: the engine is retired")
        p.set_defaults(fn=cmd_engine)

    p = add(sub, "doctor", "Check Python, the data folder, the runner, owner.token and the Claude Code mod",
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


def utf8_output():
    """Print UTF-8 whatever the console code page (cp1252 cannot encode an arrow in a title)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass  # not a text stream we can change (a test capture or a closed pipe)


def main(argv=None):
    utf8_output()
    argv = list(sys.argv[1:] if argv is None else argv)
    legacy = _legacy()
    if legacy is not None:
        return legacy.main(argv)
    if argv[:1] == ["node"] and argv[1:2] and argv[1] in NODE_SUBCOMMANDS:
        argv = ["node-" + argv[1]] + argv[2:]
    elif argv[:2] == ["help", "node"] and argv[2:3] and argv[2] in NODE_SUBCOMMANDS:
        argv = ["node-" + argv[2], "--help"]
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
