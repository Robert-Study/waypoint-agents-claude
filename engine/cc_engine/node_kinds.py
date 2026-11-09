"""Node kinds and edge ``crosses`` for loop-shaped workflows (stdlib only, works without dsh).

Kinds (``KINDS``):
- ``splitter``: a model breaks work into tasks (the planner's job; strong model).
- ``worker``: a model does one bounded task (cheap pools first).
- ``code``: a script, never a model: merge, rank, dedupe, compare. It runs ``script`` (or its
  command check when no script is given) and costs nothing.
- ``gate``: the step that accepts work (a human check, or the strong final review).
A node without a kind is a ``gate`` when its check is ``human``, otherwise a ``worker``.

Planner rule: a model node (anything but ``code``) whose goal names no judgement (judge, decide,
assess, summarise and close synonyms) and whose check is a pure transform should be ``code``;
``lint_node`` says so.

Every edge carries ``crosses``: the named output the downstream task reads. Plans with
``edges_v2: true`` (or any dependency written as ``{"id", "crosses"}``) must name it on every edge;
older plans with plain string deps still import. ``nodes.deps_json`` keeps plain ids so the graph,
scheduler and board read it unchanged; kind, script and crosses live in the ``node_loops`` table.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

KINDS = ("splitter", "worker", "code", "gate")
MODEL_KINDS = ("splitter", "worker", "gate")
DETERMINISTIC_CHECKS = ("command", "files_exist", "text_present")
CROSSES_MAX = 200
SCRIPT_MAX = 2000
CODE_ROUTE = "code"
CODE_TIMEOUT = 30 * 60

SCHEMA = ("CREATE TABLE IF NOT EXISTS node_loops(project_id TEXT NOT NULL, node_id TEXT NOT NULL, kind TEXT,"
          " script TEXT, crosses_json TEXT, PRIMARY KEY(project_id, node_id))")

JUDGE = re.compile(
    r"\b(judg\w*|decid\w*|decision\w*|assess\w*|summar\w*|evaluat\w*|review\w*|critiq\w*|critic\w*|"
    r"apprais\w*|weigh\w*|interpret\w*|recommend\w*|prioriti[sz]\w*|triag\w*|choos\w*|chose\w*|"
    r"grad(?:e|es|ed|ing)|rat(?:e|es|ed|ing)|analy[sz]\w*|synthes\w*|conclu\w*|classif\w*|verdict\w*|"
    r"explain\w*|condens\w*|reason\w*)\b", re.I)
TRANSFORM = re.compile(
    r"\b(merg(?:e|es|ed|ing)|rank(?:s|ed|ing)?|sort(?:s|ed|ing)?|de-?dup\w*|compar(?:e|es|ed|ing)|"
    r"diff(?:s|ed|ing)?|concatenat\w*|combin(?:e|es|ed|ing)|join(?:s|ed|ing)?|filter(?:s|ed|ing)?|"
    r"count(?:s|ed|ing)?|tall(?:y|ies|ied)|sum(?:s|med|ming)?|aggregat\w*|collat\w*|cop(?:y|ies|ied)|"
    r"mov(?:e|es|ed|ing)|renam\w*|convert(?:s|ed|ing)?|reformat\w*|normali[sz]\w*|extract(?:s|ed|ing)?|"
    r"collect(?:s|ed|ing)?|gather(?:s|ed|ing)?|index(?:es|ed|ing)?|transform(?:s|ed|ing)?|"
    r"pars(?:e|es|ed|ing)|export(?:s|ed|ing)?|bundl(?:e|es|ed|ing)|zip(?:s|ped|ping)?|split(?:s|ting)?)\b", re.I)
# Words that mean real authoring work a script cannot do (so not a pure transform).
AUTHORING = re.compile(
    r"\b(implement\w*|build\w*|built|creat\w*|add(?:s|ed|ing)?|fix\w*|design\w*|refactor\w*|develop\w*|"
    r"debug\w*|draft\w*|research\w*|investigat\w*|plan(?:s|ned|ning)?|tests?|tested|testing|migrat\w*|"
    r"improv\w*|extend\w*|support\w*|integrat\w*|wire\w*|rewrit\w*)\b", re.I)


class KindError(ValueError):
    pass


# ---------------------------------------------------------------- edges
def dep_id(d):
    return d.get("id") if isinstance(d, dict) else d


def split_deps(deps):
    """``(ids, {id: crosses})`` from a deps list of ids and/or ``{"id", "crosses"}`` objects."""
    ids, crosses = [], {}
    for d in deps or []:
        i = dep_id(d)
        if not isinstance(i, str) or not i:
            continue
        if i not in ids:
            ids.append(i)
        if isinstance(d, dict) and isinstance(d.get("crosses"), str) and d["crosses"].strip():
            crosses[i] = d["crosses"].strip()[:CROSSES_MAX]
    return ids, crosses


def join_deps(ids, crosses):
    return [{"id": i, "crosses": (crosses or {}).get(i) or ""} for i in ids]


def is_v2(plan_or_nodes) -> bool:
    """True when edges must carry ``crosses``: ``edges_v2: true`` or any object dependency."""
    if isinstance(plan_or_nodes, dict):
        if plan_or_nodes.get("edges_v2") is True:
            return True
        nodes = plan_or_nodes.get("nodes") or []
    else:
        nodes = plan_or_nodes or []
    return any(isinstance(d, dict) for n in nodes if isinstance(n, dict) for d in (n.get("deps") or []))


def edge_problems(node) -> list:
    """Every edge into ``node`` that does not name what crosses it (strict, v2 rules)."""
    nid = node.get("id")
    out = []
    for d in node.get("deps") or []:
        i = dep_id(d)
        if isinstance(d, dict):
            text = d.get("crosses")
            if not isinstance(text, str) or not text.strip():
                out.append(f"task {nid}: the edge from {i} needs 'crosses' (the named output {nid} reads)")
            elif len(text) > CROSSES_MAX:
                out.append(f"task {nid}: 'crosses' on the edge from {i} is longer than {CROSSES_MAX} characters")
        else:
            out.append(f"task {nid}: the edge from {i} needs 'crosses'; write it as "
                       f"{{\"id\": \"{i}\", \"crosses\": \"<the output {nid} reads>\"}}")
    return out


# ---------------------------------------------------------------- kinds
def default_kind(node) -> str:
    check = node.get("check") or {}
    return "gate" if isinstance(check, dict) and check.get("type") == "human" else "worker"


def kind_of(node) -> str:
    k = node.get("kind")
    return k if k in KINDS else default_kind(node)


def code_script(node):
    """The command a code node runs: ``script``, else its command check, else None."""
    s = node.get("script")
    if isinstance(s, str) and s.strip():
        return s.strip()
    check = node.get("check") or {}
    if isinstance(check, dict) and check.get("type") == "command" and str(check.get("run") or "").strip():
        return str(check["run"]).strip()
    return None


def kind_problems(node) -> list:
    """Shape rules every plan must meet (import and drafts)."""
    nid, k = node.get("id"), node.get("kind")
    out = []
    if k is not None and k not in KINDS:
        out.append(f"task {nid}: kind must be one of {'/'.join(KINDS)}")
        return out
    s = node.get("script")
    if s is not None and (not isinstance(s, str) or not s.strip() or len(s) > SCRIPT_MAX):
        out.append(f"task {nid}: script must be a command of 1-{SCRIPT_MAX} characters")
    if k == "code" and not code_script(node):
        out.append(f"task {nid}: a code task needs a 'script' or a command check to run")
    return out


def pure_transform(node) -> bool:
    """The check is deterministic and the goal only moves data (merge, rank, dedupe, compare...)."""
    check = node.get("check") or {}
    if not isinstance(check, dict) or check.get("type") not in DETERMINISTIC_CHECKS:
        return False
    goal = str(node.get("goal") or "")
    return bool(TRANSFORM.search(goal)) and not AUTHORING.search(goal)


def needs_judgement(node) -> bool:
    return bool(JUDGE.search(" ".join(str(node.get(k) or "") for k in ("title", "goal"))))


def lint_node(node) -> list:
    """The planner rule: a model node that needs no judgement and checks a pure transform is code."""
    if kind_of(node) not in MODEL_KINDS or needs_judgement(node) or not pure_transform(node):
        return []
    verb = TRANSFORM.search(str(node.get("goal") or "")).group(0).lower()
    return [f"task {node.get('id')} only transforms data ({verb}) and its check is deterministic: make it "
            f"kind \"code\" with a script (no model), or name the judgement it needs (judge, decide, assess, "
            f"summarise)"]


# ---------------------------------------------------------------- storage
def ensure_schema(conn):
    conn.execute(SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(node_loops)").fetchall()}
    for col in ("risk", "scope_json"):
        if col not in have:
            conn.execute(f"ALTER TABLE node_loops ADD COLUMN {col} TEXT")


def write_rows(conn, project_id, nodes):
    """Upsert the loop fields of imported ``nodes`` (plan shape); clear rows for nodes without any."""
    ensure_schema(conn)
    for n in nodes:
        _, crosses = split_deps(n.get("deps"))
        kind, script = n.get("kind"), n.get("script")
        risk = n.get("risk") if n.get("risk") in ("contained", "wide", "closed") else None
        scope = [str(x) for x in n.get("scope") or [] if str(x).strip()] if isinstance(n.get("scope"), list) else []
        if kind is None and script is None and not crosses and risk is None and not scope:
            conn.execute("DELETE FROM node_loops WHERE project_id=? AND node_id=?", (project_id, n["id"]))
            continue
        conn.execute("INSERT INTO node_loops(project_id, node_id, kind, script, crosses_json, risk, scope_json)"
                     " VALUES(?,?,?,?,?,?,?)"
                     " ON CONFLICT(project_id, node_id) DO UPDATE SET kind=excluded.kind, script=excluded.script,"
                     " crosses_json=excluded.crosses_json, risk=excluded.risk, scope_json=excluded.scope_json",
                     (project_id, n["id"], kind, script, json.dumps(crosses, ensure_ascii=False), risk,
                      json.dumps(scope, ensure_ascii=False)))


def read_row(conn, project_id, node_id):
    try:
        r = conn.execute("SELECT kind, script, crosses_json FROM node_loops WHERE project_id=? AND node_id=?",
                         (project_id, node_id)).fetchone()
    except sqlite3.OperationalError:  # table not created yet: no loop fields anywhere
        return None
    if r is None:
        return None
    return {"kind": r[0], "script": r[1], "crosses": json.loads(r[2] or "{}")}


def rows(db, project_id) -> dict:
    """``{node_id: {kind, script, crosses}}`` as imported (``db`` is a Store or a database path)."""
    sql = "SELECT node_id, kind, script, crosses_json, risk, scope_json FROM node_loops WHERE project_id=?"
    old = "SELECT node_id, kind, script, crosses_json, NULL, NULL FROM node_loops WHERE project_id=?"
    out = []
    for q in (sql, old):  # the second form reads a table not yet migrated
        try:
            if hasattr(db, "_q"):
                out = db._q(q, (project_id,))
            else:
                with sqlite3.connect(str(db), timeout=10) as c:
                    out = c.execute(q, (project_id,)).fetchall()
            break
        except sqlite3.OperationalError:
            out = []
    return {r[0]: {"kind": r[1], "script": r[2], "crosses": json.loads(r[3] or "{}"), "risk": r[4],
                   "scope": json.loads(r[5] or "[]")} for r in out}


def annotate(db, project_id, nodes) -> list:
    """Copies of store ``nodes`` with ``kind``, ``script`` and ``crosses`` ({dep: text}) filled in.
    ``db`` is a ``Store`` or a database path."""
    stored = rows(db, project_id) if db is not None else {}
    out = []
    for n in nodes:
        r = stored.get(n["id"]) or {}
        item = dict(n)
        item["kind"] = r.get("kind") if r.get("kind") in KINDS else kind_of(n)
        item["script"] = r.get("script") or n.get("script")
        item["crosses"] = dict(r.get("crosses") or {})
        item["risk"] = r.get("risk") or n.get("risk")
        item["scope"] = list(r.get("scope") or n.get("scope") or [])
        out.append(item)
    return out


def node_kind(node, db=None) -> str:
    """The kind of a stored node; reads ``node_loops`` when the node dict does not carry it."""
    if node.get("kind") in KINDS:
        return node["kind"]
    pid = node.get("project_id")
    if db is not None and pid and node.get("id"):
        r = rows(db, pid).get(node["id"]) or {}
        if r.get("kind") in KINDS:
            return r["kind"]
    return default_kind(node)


def is_code(node, db=None) -> bool:
    return node_kind(node, db) == "code"


# ---------------------------------------------------------------- running code nodes
def shell_argv(script):
    if os.name == "nt":
        return [os.environ.get("COMSPEC") or "cmd.exe", "/d", "/s", "/c", script]
    return ["/bin/sh", "-c", script]


def default_runner(cmd, cwd, timeout):
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
    p = subprocess.run(cmd, shell=True, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                       env=env, timeout=timeout, encoding="utf-8", errors="replace")
    return p.returncode, p.stdout, p.stderr


def _with_script(node, db):
    if code_script(node) or db is None or not node.get("project_id"):
        return node
    stored = (rows(db, node["project_id"]).get(node.get("id")) or {}).get("script")
    return dict(node, script=stored) if stored else node


def code_runner(node, cwd=None, runner=None, timeout=CODE_TIMEOUT, db=None) -> dict:
    """Run a code node's script, never a model. ``{route: 'code', ok, exit_code, script, stdout,
    stderr, summary}``; ``runner(cmd, cwd, timeout) -> (code, stdout, stderr)`` is injectable."""
    if not is_code(node, db):
        raise KindError(f"task {node.get('id')} is a {node_kind(node, db)} task, not code")
    script = code_script(_with_script(node, db))
    nid = node.get("id")
    if not script:
        return {"route": CODE_ROUTE, "ok": False, "exit_code": None, "script": None, "stdout": "", "stderr": "",
                "summary": f"Task {nid} is a code task with no script to run."}
    try:
        code, out, err = (runner or default_runner)(script, cwd, timeout)
    except subprocess.TimeoutExpired:
        code, out, err = 124, "", f"timed out after {timeout}s"
    except OSError as exc:
        code, out, err = 127, "", f"could not start: {exc}"
    ok = int(code) == 0
    return {"route": CODE_ROUTE, "ok": ok, "exit_code": int(code), "script": script, "stdout": out or "",
            "stderr": err or "", "summary": f"Task {nid} ran its script (no model): exit {int(code)}."}


def code_spec(node, cwd=None, db=None) -> dict:
    """A job-wrapper spec (``kind: exec``) that runs the script: what dispatch launches for code."""
    script = code_script(_with_script(node, db))
    if not script:
        raise KindError(f"task {node.get('id')} is a code task with no script to run")
    return {"kind": "exec", "route": CODE_ROUTE, "argv": shell_argv(script), "cwd": cwd, "stdin_file": None}


def dispatch_code(dispatcher, node, project=None, settings=None):
    """Start a code node through ``dispatcher``'s own job wrapper (route ``code``, no model, no
    toggles: a script costs nothing). Same return shape as ``Dispatcher.dispatch``."""
    project = project or {}
    get = node.get if isinstance(node, dict) else (lambda k, d=None: getattr(node, k, d))
    project_id = project.get("id") or get("project_id")
    node_id = get("id")
    cwd = get("cwd") or project.get("cwd")
    db = getattr(dispatcher, "toggles_db", None)
    spec = code_spec(dict(node, project_id=project_id) if isinstance(node, dict) else node, cwd, db)
    job = dispatcher._new_job_dir(project_id, node_id)
    timeout = dispatcher.timeout_seconds(settings)
    clock = getattr(dispatcher, "clock", None) or (lambda: 0.0)
    spec.update(node=node_id, project=project_id, attempt=get("attempts"), timeout_seconds=timeout,
                created_at=clock())
    spec_path = Path(job) / "spec.json"
    spec_path.write_text(json.dumps(spec, indent=1), encoding="utf-8")
    (Path(job) / "heartbeat").write_text(json.dumps({"at": clock(), "active": 0.0}), encoding="utf-8")
    started = clock()
    info = dispatcher.launcher(spec_path) or {}
    (Path(job) / "launch.json").write_text(json.dumps({"pid": info.get("pid"), "started_at": started,
                                                       "timeout_seconds": timeout, "route": CODE_ROUTE,
                                                       "attempt": spec["attempt"]}), encoding="utf-8")
    return {"route": CODE_ROUTE, "status": "running", "job": str(job), "launch": info, "pid": info.get("pid"),
            "summary": f"Task {node_id} started its script (code task, no model)."}
