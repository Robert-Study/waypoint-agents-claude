"""Revision-checked SQLite store for the Control Centre engine (stdlib only).

One shared connection guarded by a lock serves this process; other processes
(the Task Scheduler tick) open their own connections on ``Store.path``. WAL mode
and ``BEGIN IMMEDIATE`` make the revision compare-and-swap safe across both.

``meta.revision`` increments on plan/graph mutations only (``mutate`` and the
node helpers built on it). Events, telemetry, runs, sprints and questions never
bump it.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

STATUSES = ("not_started", "ready", "running", "submitted", "done", "error", "needs_you", "skipped", "carried")
# Versions: a task is unfinished (open) unless it is done, deliberately skipped or carried to a later
# version. Only ``done`` satisfies a dependency.
CLOSED_STATUSES = ("done", "skipped", "carried")
# ``skipped`` and ``carried`` are written only by ``versions`` (inside its own ``mutate``), never through
# ``set_node_status`` / ``update_node``: they close a task for good, so no caller may set them.
VERSION_ONLY_STATUSES = ("skipped", "carried")
# Check types whose pass may set ``done`` (``human`` never auto-passes).
AUTO_CHECK_TYPES = ("command", "files_exist", "text_present")
GOAL_FIELDS = frozenset({"goal", "goal_hash"})
# Done-checks are hash-locked like goals: only the owner may change them after import.
CHECK_FIELDS = frozenset({"check", "check_json", "check_hash"})
NODE_COLUMNS = ("title", "goal", "goal_hash", "check_json", "check_hash", "deps_json", "owner", "lane",
                "pool", "model", "effort", "status", "attempts", "actor", "evidence", "recommended", "files_json")
OWNER = "owner"
# Project status: a router-imported plan is ``draft`` (nothing runs) until Owner accepts it.
PROJECT_STATUSES = ("draft", "active")
# Statuses that mean automatic work is happening; refused in a draft project for non-owners.
WORK_STATUSES = ("running", "submitted", "done")
# Open questions about a task that its reaching ``done`` settles (``needs.PURPOSES``).
DONE_CLOSES_PURPOSES = ("replan", "confirm", "failure_choice", "manual_result")
# A claim (a lead session or worker saying "I am on this task") is live while its task is in one
# of these statuses, and shows as stale once it has gone this long without a refresh.
CLAIM_LIVE_STATUSES = ("running", "submitted")
CLAIM_STALE_SECONDS = 2 * 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS projects(
  id TEXT PRIMARY KEY, title TEXT, brief TEXT, goal_hash TEXT, cwd TEXT,
  created_at TEXT, archived INT DEFAULT 0, status TEXT DEFAULT 'active');
CREATE TABLE IF NOT EXISTS nodes(
  project_id TEXT NOT NULL, id TEXT NOT NULL, title TEXT, goal TEXT, goal_hash TEXT,
  check_json TEXT, check_hash TEXT, deps_json TEXT, owner TEXT, lane TEXT, pool TEXT, model TEXT, effort TEXT,
  status TEXT DEFAULT 'not_started', attempts INT DEFAULT 0, actor TEXT, evidence TEXT,
  updated_at TEXT, recommended TEXT, PRIMARY KEY(project_id, id));
CREATE TABLE IF NOT EXISTS events(
  seq INTEGER PRIMARY KEY, at TEXT, project_id TEXT, node_id TEXT, kind TEXT, actor TEXT,
  summary TEXT, data_json TEXT);
CREATE INDEX IF NOT EXISTS events_project ON events(project_id, seq);
CREATE INDEX IF NOT EXISTS events_node ON events(project_id, node_id, seq);
CREATE TABLE IF NOT EXISTS runs(
  id TEXT PRIMARY KEY, project_id TEXT, settings_json TEXT, status TEXT, created_at TEXT,
  started_at TEXT, ended_at TEXT);
CREATE TABLE IF NOT EXISTS sprints(
  id TEXT PRIMARY KEY, run_id TEXT, pool TEXT, start_at TEXT, end_at TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS questions(
  id TEXT PRIMARY KEY, project_id TEXT, node_id TEXT, kind TEXT, prompt TEXT, options_json TEXT,
  scale INT, answer_json TEXT, answered_at TEXT, captured INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS families(id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT);
CREATE TABLE IF NOT EXISTS claims(
  project_id TEXT NOT NULL, node_id TEXT NOT NULL, claimed_by TEXT NOT NULL, worktree TEXT, cwd TEXT,
  environment TEXT, since TEXT, refreshed_at TEXT, PRIMARY KEY(project_id, node_id));
"""
_SCHEMA_TABLES = tuple(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA))
_SCHEMA_INDEXES = tuple(re.findall(r"CREATE INDEX IF NOT EXISTS (\w+)", SCHEMA))


class StaleRevision(Exception):
    """The caller's revision is behind the store; ``.revision`` is the current one."""

    def __init__(self, revision: int):
        self.revision = revision
        super().__init__(f"stale revision; current revision is {revision}")


class NotAllowed(Exception):
    """A change the engine rules forbid (e.g. an agent marking a node done)."""


class GoalLocked(Exception):
    """Goal text, done-checks and the brief are hash-locked; only the owner may change them."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def goal_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def check_hash(check) -> str:
    """Canonical hash of a done-check (dict or its JSON text); key order does not matter."""
    if isinstance(check, str):
        try:
            check = json.loads(check or "{}")
        except json.JSONDecodeError:
            return goal_hash(check)
    return goal_hash(json.dumps(check or {}, sort_keys=True, separators=(",", ":")))


def brief_hash(text: str) -> str:
    """The project brief lock (stored in ``projects.goal_hash``)."""
    return goal_hash(text)


def may_set_done(actor) -> bool:
    """Done is set only by the owner or by a passing automatic check."""
    if actor == "owner":
        return True
    if isinstance(actor, str) and actor.startswith("check:"):
        return actor[len("check:"):] in AUTO_CHECK_TYPES
    return False


def _parse_time(text):
    if not text:
        return None
    try:
        t = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def claim_view(row, now=None) -> dict:
    """The payload shape of one claim row (``now`` is a datetime; default the current time)."""
    now = now or datetime.now(timezone.utc)
    last = _parse_time(row.get("refreshed_at")) or _parse_time(row.get("since"))
    stale = last is None or (now - last).total_seconds() > CLAIM_STALE_SECONDS
    return {"by": row.get("claimed_by"), "worktree": row.get("worktree"), "cwd": row.get("cwd"),
            "environment": row.get("environment"), "since": row.get("since"),
            "refreshed_at": row.get("refreshed_at"), "stale": bool(stale)}


FILES_MAX = 50
FILES_MAX_LEN = 200


def clean_files(value):
    """Validate a node's ``files`` globs: None or a list of up to 50 strings (<= 200 chars, no ``..``).

    Returns the cleaned list (or None); raises ``ValueError`` with a plain reason.
    """
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("files must be a list of path globs")
    if len(value) > FILES_MAX:
        raise ValueError(f"files may list at most {FILES_MAX} globs")
    out = []
    for g in value:
        if not isinstance(g, str) or not g.strip():
            raise ValueError("every files entry must be a non-empty string")
        if len(g) > FILES_MAX_LEN:
            raise ValueError(f"a files entry is longer than {FILES_MAX_LEN} characters")
        if ".." in g.replace("\\", "/").split("/"):
            raise ValueError("files entries must not contain '..' segments")
        out.append(g)
    return out


def files_json(value) -> str | None:
    value = clean_files(value)
    return json.dumps(value) if value else None


def node_from_row(row) -> dict:
    d = dict(row)
    try:
        d["files"] = json.loads(d.pop("files_json", None) or "[]")
    except ValueError:
        d["files"] = []
    d["check"] = json.loads(d.pop("check_json") or "{}")
    d["deps"] = json.loads(d.pop("deps_json") or "[]")
    d["attempts"] = int(d.get("attempts") or 0)
    return d


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._subs: list = []
        self._subs_lock = threading.Lock()
        self._db_conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False,
                                        isolation_level=None)
        self._db_conn.row_factory = sqlite3.Row
        with self._lock:
            c = self._db_conn
            c.execute("PRAGMA busy_timeout=30000")
            if str(c.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "wal":
                c.execute("PRAGMA journal_mode=WAL")
            # A current database opens with reads only: another process (the scheduled tick, a CLI,
            # an engine shutting down) may hold the write lock, and start-up must not wait on it.
            if self._needs_setup():
                c.executescript(SCHEMA)
                c.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('revision', '0')")
                self._migrate()

    def _needs_setup(self) -> bool:
        """True when any table, index, column or the revision row the engine needs is missing."""
        c = self._db_conn
        have = {(r[0], r[1]) for r in c.execute("SELECT type, name FROM sqlite_master")}
        want = {("table", n) for n in _SCHEMA_TABLES} | {("index", n) for n in _SCHEMA_INDEXES}
        if not want <= have:
            return True
        if not c.execute("SELECT 1 FROM meta WHERE key='revision'").fetchone():
            return True
        pcols = {r[1] for r in c.execute("PRAGMA table_info(projects)")}
        ncols = {r[1] for r in c.execute("PRAGMA table_info(nodes)")}
        if not {"status", "family", "version"} <= pcols or not {"check_hash", "recommended", "files_json"} <= ncols:
            return True
        return c.execute("SELECT 1 FROM nodes WHERE check_hash IS NULL LIMIT 1").fetchone() is not None

    def _migrate(self):
        """Add integrity/draft/recommended/family/version columns to databases created before they existed.

        Existing projects stay runnable (``active``); existing checks are locked at their current
        text; existing tasks have no recommended model (null). Additive only (``ADD COLUMN``), in
        one transaction, so the live database keeps every row. Never bumps the revision.
        """
        c = self._db_conn
        c.execute("BEGIN IMMEDIATE")
        try:
            pcols = {r[1] for r in c.execute("PRAGMA table_info(projects)")}
            if "status" not in pcols:
                c.execute("ALTER TABLE projects ADD COLUMN status TEXT DEFAULT 'active'")
            if "family" not in pcols:
                c.execute("ALTER TABLE projects ADD COLUMN family TEXT")
            if "version" not in pcols:
                c.execute("ALTER TABLE projects ADD COLUMN version INT")
            ncols = {r[1] for r in c.execute("PRAGMA table_info(nodes)")}
            if "check_hash" not in ncols:
                c.execute("ALTER TABLE nodes ADD COLUMN check_hash TEXT")
            if "recommended" not in ncols:
                c.execute("ALTER TABLE nodes ADD COLUMN recommended TEXT")
            if "files_json" not in ncols:
                c.execute("ALTER TABLE nodes ADD COLUMN files_json TEXT")
            for r in c.execute("SELECT project_id, id, check_json FROM nodes WHERE check_hash IS NULL").fetchall():
                c.execute("UPDATE nodes SET check_hash=? WHERE project_id=? AND id=?",
                          (check_hash(r[2] or "{}"), r[0], r[1]))
        except BaseException:
            c.execute("ROLLBACK")
            raise
        c.execute("COMMIT")

    # ------------------------------------------------------------ basics
    def close(self):
        with self._lock:
            self._db_conn.close()

    def _q(self, sql, params=()):
        with self._lock:
            return self._db_conn.execute(sql, params).fetchall()

    def revision(self) -> int:
        row = self._q("SELECT value FROM meta WHERE key='revision'")
        return int(row[0][0]) if row else 0

    @contextmanager
    def tx(self):
        """A write transaction that does NOT bump the revision (runs, questions, meta)."""
        with self._lock:
            c = self._db_conn
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
            except BaseException:
                c.execute("ROLLBACK")
                raise
            c.execute("COMMIT")

    def mutate(self, expected_revision, fn, actor, summary, project_id=None, node_id=None,
               kind="plan", data=None) -> int:
        """Apply ``fn(conn)`` if ``expected_revision`` matches; bump revision; log one event.

        ``expected_revision=None`` means "whatever is current" (engine-internal callers).
        Returns the new revision. Raises ``StaleRevision`` on mismatch.
        """
        with self._lock:
            c = self._db_conn
            c.execute("BEGIN IMMEDIATE")
            try:
                cur = int(c.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0])
                if expected_revision is not None and int(expected_revision) != cur:
                    raise StaleRevision(cur)
                fn(c)
                new = cur + 1
                c.execute("UPDATE meta SET value=? WHERE key='revision'", (str(new),))
                c.execute(
                    "INSERT INTO events(at, project_id, node_id, kind, actor, summary, data_json)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (now_iso(), project_id, node_id, kind, actor, summary,
                     json.dumps(dict(data or {}, revision=new))))
            except BaseException:
                c.execute("ROLLBACK")
                raise
            c.execute("COMMIT")
        self.notify("state", {"project": project_id, "revision": new})
        return new

    def event(self, project_id=None, node_id=None, kind="note", actor="engine", summary="",
              data=None) -> int:
        """Log one plain-English event. Never bumps the revision."""
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO events(at, project_id, node_id, kind, actor, summary, data_json)"
                " VALUES(?,?,?,?,?,?,?)",
                (now_iso(), project_id, node_id, kind, actor, summary, json.dumps(data or {})))
            seq = cur.lastrowid
        self.notify("event", {"seq": seq, "project": project_id, "node": node_id, "kind": kind,
                              "summary": summary})
        return seq

    # ------------------------------------------------------------ subscribers (SSE)
    def subscribe(self, callback):
        """Register ``callback(kind, payload)``; returns an unsubscribe function."""
        with self._subs_lock:
            self._subs.append(callback)

        def unsubscribe():
            with self._subs_lock:
                if callback in self._subs:
                    self._subs.remove(callback)

        return unsubscribe

    def notify(self, kind, payload):
        with self._subs_lock:
            subs = list(self._subs)
        for cb in subs:
            try:
                cb(kind, payload)
            except Exception:  # a broken subscriber must not break a mutation
                pass

    # ------------------------------------------------------------ meta
    def get_meta(self, key, default=None):
        row = self._q("SELECT value FROM meta WHERE key=?", (key,))
        return row[0][0] if row else default

    def set_meta(self, key, value):
        if key == "revision":
            raise NotAllowed("revision changes only through mutate()")
        with self.tx() as c:
            c.execute("INSERT INTO meta(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, value))

    # ------------------------------------------------------------ projects / nodes
    def get_project(self, project_id):
        row = self._q("SELECT * FROM projects WHERE id=?", (project_id,))
        return dict(row[0]) if row else None

    def list_projects(self, include_archived=False):
        sql = "SELECT * FROM projects" + ("" if include_archived else " WHERE COALESCE(archived,0)=0")
        return [dict(r) for r in self._q(sql + " ORDER BY created_at, id")]

    def default_project_id(self):
        rows = self._q("SELECT id FROM projects WHERE COALESCE(archived,0)=0 ORDER BY created_at DESC, rowid DESC LIMIT 1")
        return rows[0][0] if rows else None

    def project_status(self, project_id):
        """``draft`` or ``active``; None when the project does not exist."""
        row = self._q("SELECT status FROM projects WHERE id=?", (project_id,))
        if not row:
            return None
        return row[0][0] or "active"

    def is_draft(self, project_id) -> bool:
        return self.project_status(project_id) == "draft"

    def project_runnable(self, project_id) -> bool:
        """True when the scheduler may dispatch this project's nodes: it exists, is not archived,
        and is not a draft waiting for Owner's acceptance."""
        row = self._q("SELECT status, archived FROM projects WHERE id=?", (project_id,))
        if not row:
            return False
        status, archived = row[0][0] or "active", int(row[0][1] or 0)
        return status != "draft" and not archived

    def accept_project(self, project_id, actor, expected_revision=None) -> int:
        """Owner accepts a draft plan so its nodes become runnable. Owner only."""
        if actor != OWNER:
            raise NotAllowed(f"only Owner may accept a plan; {actor!r} is not the owner")
        project = self.get_project(project_id)
        if project is None:
            raise KeyError(f"no project {project_id}")

        def fn(c):
            c.execute("UPDATE projects SET status='active' WHERE id=?", (project_id,))

        return self.mutate(expected_revision, fn, OWNER,
                           f"Owner accepted the plan '{project.get('title') or project_id}', so its tasks may run.",
                           project_id=project_id, kind="accept")

    def verify_integrity(self, project_id) -> list:
        """Locked text that no longer matches its hash: ``[{kind: goal|check|brief, node?}]``."""
        out = []
        project = self.get_project(project_id)
        if project is None:
            return out
        if (project.get("goal_hash") or "") != brief_hash(project.get("brief") or ""):
            out.append({"kind": "brief"})
        rows = self._q("SELECT id, goal, goal_hash, check_json, check_hash FROM nodes WHERE project_id=?"
                       " ORDER BY rowid", (project_id,))
        for r in rows:
            if r["goal_hash"] != goal_hash(r["goal"] or ""):
                out.append({"kind": "goal", "node": r["id"]})
            if r["check_hash"] != check_hash(r["check_json"] or "{}"):
                out.append({"kind": "check", "node": r["id"]})
        return out

    def list_nodes(self, project_id):
        rows = self._q("SELECT * FROM nodes WHERE project_id=? ORDER BY rowid", (project_id,))
        return [node_from_row(r) for r in rows]

    def get_node(self, project_id, node_id):
        rows = self._q("SELECT * FROM nodes WHERE project_id=? AND id=?", (project_id, node_id))
        return node_from_row(rows[0]) if rows else None

    def set_node_status(self, project_id, node_id, status, actor, evidence=None, summary=None,
                        expected_revision=None, data=None) -> int:
        """Change a node's status. ``done`` only for actor ``owner`` or ``check:<auto type>``.

        ``data`` adds fields to the status event (``checks.complete_node`` records ``check_hash``).
        """
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}")
        if status in VERSION_ONLY_STATUSES:
            raise NotAllowed(f"status {status!r} is set only by the versions module "
                             "(use waypoint version new/decide)")
        if status == "done" and not may_set_done(actor):
            raise NotAllowed(f"actor {actor!r} may not mark a node done; only a passing check or the owner")
        if self.get_node(project_id, node_id) is None:
            raise KeyError(f"no node {project_id}/{node_id}")
        if status in WORK_STATUSES and actor != OWNER and self.is_draft(project_id):
            raise NotAllowed(f"project {project_id} is a draft; Owner must accept it before tasks run")
        summary = summary or f"Task {node_id} is now {status.replace('_', ' ')}."

        def fn(c):
            c.execute("UPDATE nodes SET status=?, actor=?, evidence=COALESCE(?, evidence), updated_at=?"
                      " WHERE project_id=? AND id=?",
                      (status, actor, evidence, now_iso(), project_id, node_id))

        rev = self.mutate(expected_revision, fn, actor, summary, project_id=project_id,
                          node_id=node_id, kind="status", data=dict(data or {}, status=status))
        if status == "done":  # LIVE2: a finished task no longer needs its re-plan/confirm/check question
            self.close_questions(project_id, node_id, purposes=DONE_CLOSES_PURPOSES, actor=actor,
                                 why=f"task {node_id} is done")
        return rev

    def close_questions(self, project_id, node_id=None, purposes=None, actor="engine", why="") -> list:
        """Close open questions of a project (one node, some purposes) as dismissed; one event. Ids."""
        cols = {r[1] for r in self._q("PRAGMA table_info(questions)")}
        sql, params = "SELECT id FROM questions WHERE project_id=? AND answered_at IS NULL", [project_id]
        if node_id is not None:
            sql += " AND node_id=?"
            params.append(node_id)
        if purposes is not None:
            if "purpose" not in cols:
                return []
            sql += " AND purpose IN (%s)" % ",".join("?" * len(purposes))
            params += list(purposes)
        ids = [r[0] for r in self._q(sql + " ORDER BY rowid", params)]
        if not ids:
            return []
        at = now_iso()
        with self.tx() as c:
            c.executemany("UPDATE questions SET answer_json=?, answered_at=? WHERE id=? AND answered_at IS NULL",
                          [(json.dumps({"type": "ignore", "value": None}), at, q) for q in ids])
        n = len(ids)
        self.event(project_id, node_id, "questions_closed", actor,
                   f"Closed {n} open question{'s' if n != 1 else ''}" + (f": {why}." if why else "."),
                   {"questions": ids})
        return ids

    def check_counts(self, project_id) -> dict:
        """Done-check results per node: ``{node_id: [passed, failed, last]}`` (last ``pass``/``fail``).

        A result is a ``status`` event by actor ``check:<type>`` that set ``done`` (pass) or
        ``error`` (fail). Events stamped with a ``check_hash`` other than the node's current one
        were against an older check and are skipped. Nodes with no results are absent.
        """
        current = {r[0]: r[1] for r in self._q("SELECT id, check_hash FROM nodes WHERE project_id=?",
                                               (project_id,))}
        rows = self._q("SELECT node_id, data_json FROM events WHERE project_id=? AND kind='status'"
                       " AND actor LIKE 'check:%' ORDER BY seq", (project_id,))
        out: dict = {}
        for node_id, data_json in rows:
            if node_id not in current:
                continue
            try:
                data = json.loads(data_json or "{}")
            except json.JSONDecodeError:
                continue
            outcome = {"done": "pass", "error": "fail"}.get(data.get("status"))
            if outcome is None:
                continue
            stamped = data.get("check_hash")
            if stamped and stamped != current[node_id]:
                continue
            c = out.setdefault(node_id, [0, 0, None])
            c[0 if outcome == "pass" else 1] += 1
            c[2] = outcome
        return out

    def update_node(self, project_id, node_id, fields, actor, summary, expected_revision=None) -> int:
        """Change non-goal node fields (model, effort, pool, deps, attempts, ...).

        The done-check is hash-locked: only actor ``owner`` may change it (its hash moves with it).
        """
        fields = dict(fields)
        if GOAL_FIELDS & set(fields):
            raise GoalLocked("goal text is hash-locked; use plan.change_goal as the owner")
        if CHECK_FIELDS & set(fields):
            if actor != OWNER:
                raise GoalLocked(f"the done-check of {node_id} is locked; {actor!r} is not the owner")
            if "check_hash" in fields:
                raise GoalLocked("check_hash is derived from the check; set 'check' instead")
            if "check" in fields:
                fields["check_json"] = json.dumps(fields.pop("check"))
            fields["check_hash"] = check_hash(fields["check_json"])
        if "deps" in fields:
            fields["deps_json"] = json.dumps(list(fields.pop("deps")))
        if "files" in fields:
            fields["files_json"] = files_json(fields.pop("files"))
        bad = set(fields) - set(NODE_COLUMNS)
        if bad:
            raise ValueError(f"unknown node fields {sorted(bad)}")
        if "status" in fields:
            if fields["status"] not in STATUSES:
                raise ValueError(f"unknown status {fields['status']!r}")
            if fields["status"] == "done":
                raise NotAllowed("use set_node_status/complete_node to mark done")
            if fields["status"] in VERSION_ONLY_STATUSES:
                raise NotAllowed(f"status {fields['status']!r} is set only by the versions module "
                                 "(use waypoint version new/decide)")
        old = self.get_node(project_id, node_id)
        if old is None:
            raise KeyError(f"no node {project_id}/{node_id}")
        if actor != OWNER and self.is_draft(project_id) and (
                fields.get("status") in WORK_STATUSES
                or int(fields.get("attempts", old["attempts"]) or 0) > old["attempts"]):
            raise NotAllowed(f"project {project_id} is a draft; Owner must accept it before tasks run")
        cols = sorted(fields)

        def fn(c):
            c.execute(f"UPDATE nodes SET {', '.join(k + '=?' for k in cols)}, updated_at=?"
                      " WHERE project_id=? AND id=?",
                      (*[fields[k] for k in cols], now_iso(), project_id, node_id))

        return self.mutate(expected_revision, fn, actor, summary, project_id=project_id,
                           node_id=node_id, kind="update", data={"fields": cols})

    def add_node(self, project_id, node, actor, summary, expected_revision=None) -> int:
        """Add a node (e.g. a fix-up node). Status always starts ``not_started``."""
        if self.get_node(project_id, node["id"]) is not None:
            raise ValueError(f"node {node['id']} already exists")

        def fn(c):
            c.execute(
                "INSERT INTO nodes(project_id, id, title, goal, goal_hash, check_json, check_hash, deps_json,"
                " owner, lane, pool, model, effort, status, attempts, updated_at, recommended, files_json)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'not_started',0,?,?,?)",
                (project_id, node["id"], node.get("title"), node.get("goal"), goal_hash(node.get("goal", "")),
                 json.dumps(node.get("check") or {}), check_hash(node.get("check") or {}),
                 json.dumps(list(node.get("deps") or [])),
                 node.get("owner"), node.get("lane"), node.get("pool"), node.get("model"),
                 node.get("effort"), now_iso(), node.get("recommended"), files_json(node.get("files"))))

        return self.mutate(expected_revision, fn, actor, summary, project_id=project_id,
                           node_id=node["id"], kind="add_node")

    # ------------------------------------------------------------ claims (no revision)
    def claim_row(self, project_id, node_id):
        rows = self._q("SELECT * FROM claims WHERE project_id=? AND node_id=?", (project_id, node_id))
        return dict(rows[0]) if rows else None

    def put_claim(self, project_id, node_id, by, worktree=None, cwd=None, environment=None, since=None,
                  refreshed_at=None):
        """Insert or replace the claim row. Status changes go through ``set_node_status``."""
        stamp = refreshed_at or now_iso()
        with self.tx() as c:
            c.execute("INSERT INTO claims(project_id, node_id, claimed_by, worktree, cwd, environment, since,"
                      " refreshed_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(project_id, node_id) DO UPDATE SET"
                      " claimed_by=excluded.claimed_by, worktree=excluded.worktree, cwd=excluded.cwd,"
                      " environment=excluded.environment, since=excluded.since, refreshed_at=excluded.refreshed_at",
                      (project_id, node_id, by, worktree, cwd, environment, since or stamp, stamp))

    def drop_claim(self, project_id, node_id) -> bool:
        with self.tx() as c:
            cur = c.execute("DELETE FROM claims WHERE project_id=? AND node_id=?", (project_id, node_id))
            return cur.rowcount > 0

    def claims(self, project_id, now=None) -> dict:
        """Live claims for a project: ``{node_id: {by, worktree, cwd, environment, since,
        refreshed_at, stale}}``. A claim on a task that is no longer running/submitted (a check
        finished it, Owner reopened it) is not live and is left out."""
        rows = self._q("SELECT c.*, n.status AS node_status FROM claims c JOIN nodes n"
                       " ON n.project_id=c.project_id AND n.id=c.node_id WHERE c.project_id=? ORDER BY c.since",
                       (project_id,))
        return {r["node_id"]: claim_view(dict(r), now) for r in rows if r["node_status"] in CLAIM_LIVE_STATUSES}

    def node_claim(self, project_id, node_id, now=None):
        return self.claims(project_id, now).get(node_id)

    # ------------------------------------------------------------ events
    def events(self, project_id=None, since=0, limit=500, latest=None):
        """Events after ``since`` in ascending order: the oldest ``limit``, or with ``latest=N``
        the newest N (still ascending), so a long history never cuts off recent events."""
        sql, params = "SELECT * FROM events WHERE seq>?", [int(since or 0)]
        if project_id:
            sql += " AND project_id=?"
            params.append(project_id)
        if latest:
            rows = list(reversed(self._q(sql + " ORDER BY seq DESC LIMIT ?", (*params, int(latest)))))
        else:
            rows = self._q(sql + " ORDER BY seq LIMIT ?", (*params, int(limit)))
        out = []
        for r in rows:
            d = dict(r)
            d["data"] = json.loads(d.pop("data_json") or "{}")
            out.append(d)
        return out

    def node_changed_since(self, project_id, node_id, base_revision) -> bool:
        """True if the node has an event that bumped the revision past ``base_revision``, or any
        ``version`` event (a carry or skip; older ones carry no revision). One indexed query."""
        rows = self._q(
            "SELECT 1 FROM events WHERE project_id=? AND node_id=?"
            " AND (CAST(json_extract(data_json, '$.revision') AS INTEGER) > ?"
            "      OR (kind='version' AND json_extract(data_json, '$.revision') IS NULL))"
            " LIMIT 1", (project_id, node_id, int(base_revision)))
        return bool(rows)

    def node_events(self, project_id, node_id, limit=10):
        rows = self._q("SELECT * FROM events WHERE project_id=? AND node_id=? ORDER BY seq DESC LIMIT ?",
                       (project_id, node_id, int(limit)))
        out = []
        for r in reversed(rows):
            d = dict(r)
            d["data"] = json.loads(d.pop("data_json") or "{}")
            out.append(d)
        return out

    # ------------------------------------------------------------ runs / sprints (no revision)
    def create_run(self, run_id, project_id, settings, status="queued"):
        with self.tx() as c:
            c.execute("INSERT INTO runs(id, project_id, settings_json, status, created_at) VALUES(?,?,?,?,?)",
                      (run_id, project_id, json.dumps(settings), status, now_iso()))
        return self.get_run(run_id)

    def get_run(self, run_id):
        rows = self._q("SELECT * FROM runs WHERE id=?", (run_id,))
        if not rows:
            return None
        d = dict(rows[0])
        d["settings"] = json.loads(d.get("settings_json") or "{}")
        return d

    def update_run(self, run_id, **fields):
        cols = sorted(fields)
        with self.tx() as c:
            c.execute(f"UPDATE runs SET {', '.join(k + '=?' for k in cols)} WHERE id=?",
                      (*[fields[k] for k in cols], run_id))

    def list_runs(self, limit=50):
        rows = self._q("SELECT * FROM runs ORDER BY created_at DESC, rowid DESC LIMIT ?", (int(limit),))
        out = []
        for r in rows:
            d = dict(r)
            d["settings"] = json.loads(d.pop("settings_json") or "{}")
            d["sprints"] = self.list_sprints(d["id"])
            out.append(d)
        return out

    def create_sprint(self, sprint_id, run_id, pool, start_at, end_at=None, status="scheduled"):
        with self.tx() as c:
            c.execute("INSERT INTO sprints(id, run_id, pool, start_at, end_at, status) VALUES(?,?,?,?,?,?)",
                      (sprint_id, run_id, pool, start_at, end_at, status))

    def get_sprint(self, sprint_id):
        rows = self._q("SELECT * FROM sprints WHERE id=?", (sprint_id,))
        return dict(rows[0]) if rows else None

    def update_sprint(self, sprint_id, **fields):
        cols = sorted(fields)
        with self.tx() as c:
            c.execute(f"UPDATE sprints SET {', '.join(k + '=?' for k in cols)} WHERE id=?",
                      (*[fields[k] for k in cols], sprint_id))

    def list_sprints(self, run_id=None):
        if run_id is None:
            return [dict(r) for r in self._q("SELECT * FROM sprints ORDER BY start_at")]
        return [dict(r) for r in self._q("SELECT * FROM sprints WHERE run_id=? ORDER BY start_at", (run_id,))]
