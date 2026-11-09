"""Receipts and question files (Waypoint Core 1.1, core/docs/FILE_CONTRACT.md).

Agents write small JSON files into ``<cwd>/.waypoint/receipts`` and ``.../questions``. This module validates them,
applies them to the plan (code only, no model call) and keeps a ``receipts`` table of what it saw.

Rules
- A file is processed once per content hash and claim state. The same bytes again are a ``duplicate``, unless
  the node was claimed afresh in between (a retry).
- ``done`` is a claim. Only the plan's own done-check (``checks.complete_node``) can set a node done.
- A bad file is recorded as ``invalid`` with a reason and never changes the plan.
- The done-check runs in the live claim's worktree or cwd (read before the claim is released), else the project's
  cwd. Never in a folder the receipt, the request or a trace named.
- A claim is released only when the receipt plausibly comes from its holder; a late error never undoes a done node.
- The watcher polls with ``os.stat`` every few seconds on a daemon thread; slow checks run on two worker threads,
  never two for the same node at once. Symlinks are never followed.

Routes: ``POST /api/receipt`` and ``GET /api/receipts``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat as statmod
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import checks, claims, needs, todo

SCHEMA_RECEIPT = "waypoint.receipt.v1"
SCHEMA_QUESTION = "waypoint.question.v1"
MAX_BYTES = 64 * 1024
MAX_SUMMARY = 300
MAX_LIST = 20
MAX_ITEM = 500
MAX_QUESTIONS = 3
MAX_OPTIONS = 20
MAX_PROMPT = 500
MAX_TODO = 2000
STATUSES = ("done", "error", "needs_you")
KINDS = ("select", "likert", "text")
SECTIONS = ("human", "computer")
PRESELECT_KEYS = ("selected", "default", "checked", "preselected")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.json$")
POLL_SECONDS = 3.0
ROOTS_SECONDS = 30.0
MAX_FILES = 500
CHECK_WORKERS = 2
TRACE_HOURS = 48
FOLDER = ".waypoint"
ACTOR = "receipt"

SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts(
  id TEXT PRIMARY KEY, path TEXT, name TEXT, project TEXT, node TEXT, session TEXT,
  status TEXT NOT NULL, claimed TEXT, outcome TEXT, summary TEXT, reason TEXT, data_json TEXT,
  ingested_at TEXT, content TEXT)
"""

_lock = threading.Lock()


def ensure_schema(store):
    if getattr(store, "_receipts_schema_ok", False):
        return
    with store.tx() as c:
        c.execute(SCHEMA)
        cols = [r[1] for r in c.execute("PRAGMA table_info(receipts)").fetchall()]
        if "content" not in cols:  # a table made before the claim-aware key
            c.execute("ALTER TABLE receipts ADD COLUMN content TEXT")
    store._receipts_schema_ok = True


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def receipt_id(raw: bytes, stamp: str = "") -> str:
    """Key of one receipt: its bytes plus the claim state it arrived under (so a retry is not a duplicate)."""
    return digest(raw + b"\0" + (stamp or "").encode("utf-8"))


# ---------------------------------------------------------------- validation
def _text(v, key, limit, required=False):
    if v is None:
        if required:
            return None, f"'{key}' is required"
        return None, None
    if not isinstance(v, str):
        return None, f"'{key}' must be text"
    v = v.strip()
    if required and not v:
        return None, f"'{key}' must not be empty"
    if len(v) > limit:
        return None, f"'{key}' is over {limit} characters"
    return v, None


def _str_list(v, key, limit=MAX_LIST, item=MAX_ITEM):
    if v in (None, ""):
        return [], None
    if not isinstance(v, list):
        return None, f"'{key}' must be a list"
    if len(v) > limit:
        return None, f"'{key}' has more than {limit} items"
    out = []
    for x in v:
        if not isinstance(x, str):
            return None, f"every item in '{key}' must be text"
        if len(x) > item:
            return None, f"an item in '{key}' is over {item} characters"
        out.append(x)
    return out, None


def _options(v):
    if not isinstance(v, list):
        return None, "'options' must be a list"
    if len(v) > MAX_OPTIONS:
        return None, f"'options' has more than {MAX_OPTIONS} items"
    out = []
    for o in v:
        if isinstance(o, dict):
            if any(o.get(k) for k in PRESELECT_KEYS):
                return None, "options must not be preselected"
            if "value" not in o:
                return None, "each option needs a value"
            item = {k: o[k] for k in ("value", "label", "description") if k in o}
            if not all(isinstance(x, (str, int, float)) and not isinstance(x, bool) for x in item.values()):
                return None, "option fields must be text or numbers"
            out.append(item)
        elif isinstance(o, str) and o.strip() and len(o) <= MAX_ITEM:
            out.append(o)
        else:
            return None, "each option must be short text or an object with a value"
    return out, None


def _question(obj, need_schema):
    if not isinstance(obj, dict):
        return None, "a question must be an object"
    if need_schema and obj.get("schema") != SCHEMA_QUESTION:
        return None, f"'schema' must be {SCHEMA_QUESTION}"
    kind = obj.get("kind")
    if kind not in KINDS:
        return None, "'kind' must be select, likert or text"
    prompt, err = _text(obj.get("prompt"), "prompt", MAX_PROMPT, True)
    if err:
        return None, err
    clean = dict(obj)
    clean["kind"], clean["prompt"] = kind, prompt
    for key in ("project", "node"):
        if obj.get(key) is not None:
            v, err = _text(obj.get(key), key, 200)
            if err:
                return None, err
            clean[key] = v
    if kind == "select":
        opts, err = _options(obj.get("options"))
        if err:
            return None, err
        if not opts:
            return None, "a select question needs options"
        clean["options"] = opts
    elif obj.get("options"):
        opts, err = _options(obj.get("options"))
        if err:
            return None, err
        clean["options"] = opts
    if kind == "likert" and obj.get("scale") is not None:
        s = obj["scale"]
        if not isinstance(s, int) or isinstance(s, bool) or not 2 <= s <= 11:
            return None, "'scale' must be a whole number from 2 to 11"
    return clean, None


def validate_question(obj):
    """``(clean, None)`` or ``(None, reason)`` for a ``waypoint.question.v1`` object."""
    return _question(obj, True)


def validate(obj):
    """``(clean, None)`` or ``(None, reason)`` for a ``waypoint.receipt.v1`` object."""
    if not isinstance(obj, dict):
        return None, "the receipt must be a JSON object"
    if obj.get("schema") != SCHEMA_RECEIPT:
        return None, f"'schema' must be {SCHEMA_RECEIPT}"
    try:
        if len(json.dumps(obj)) > MAX_BYTES:
            return None, "the receipt is over 64 KB"
    except (TypeError, ValueError):
        return None, "the receipt is not plain JSON"
    clean = dict(obj)
    if obj.get("status") not in STATUSES:
        return None, "'status' must be done, error or needs_you"
    summary, err = _text(obj.get("summary"), "summary", MAX_SUMMARY, True)
    if err:
        return None, err
    clean["summary"] = summary
    for key in ("project", "node", "session", "agent"):
        v, err = _text(obj.get(key), key, 200)
        if err:
            return None, err
        if v:
            clean[key] = v
        else:
            clean.pop(key, None)
    if bool(clean.get("project")) != bool(clean.get("node")):
        return None, "give both 'project' and 'node', or neither"
    for key in ("evidence", "outputs"):
        items, err = _str_list(obj.get(key), key)
        if err:
            return None, err
        clean[key] = items
    t = obj.get("todo")
    if t in (None, ""):
        clean["todo"] = None
    else:
        if not isinstance(t, dict):
            return None, "'todo' must be an object"
        text, err = _text(t.get("text"), "todo.text", MAX_TODO, True)
        if err:
            return None, err
        section = t.get("section") or "computer"
        if section not in SECTIONS:
            return None, "'todo.section' must be human or computer"
        link, err = _text(t.get("link"), "todo.link", MAX_ITEM)
        if err:
            return None, err
        clean["todo"] = {"text": text, "section": section, "link": link}
    qs = obj.get("questions")
    if qs in (None, ""):
        clean["questions"] = []
    else:
        if not isinstance(qs, list):
            return None, "'questions' must be a list"
        if len(qs) > MAX_QUESTIONS:
            return None, f"'questions' has more than {MAX_QUESTIONS} items"
        cq = []
        for q in qs:
            one, err = _question(q, False)
            if err:
                return None, "question: " + err
            cq.append(one)
        clean["questions"] = cq
    m = obj.get("metrics")
    if m not in (None, "") and not isinstance(m, dict):
        return None, "'metrics' must be an object"
    clean["metrics"] = m or {}
    return clean, None


# ---------------------------------------------------------------- storage
def _row(r) -> dict:
    d = dict(r)
    raw = d.pop("data_json", None)
    try:
        d["data"] = json.loads(raw) if raw else {}
    except ValueError:
        d["data"] = {}
    return d


def get(store, rid):
    ensure_schema(store)
    rows = store._q("SELECT * FROM receipts WHERE id=?", (rid,))
    return _row(rows[0]) if rows else None


def list_receipts(store, limit=50) -> list:
    ensure_schema(store)
    limit = max(1, min(int(limit or 50), 500))
    rows = store._q("SELECT * FROM receipts ORDER BY ingested_at DESC, rowid DESC LIMIT ?", (limit,))
    return [_row(r) for r in rows]


def _claim_hash(store, rid, content=None, any_state=False, **fields):
    """Insert the row for this id; False when it is already known (a duplicate). With ``any_state`` (no claim)
    the same bytes seen under any earlier claim state also count as known."""
    with _lock:
        if store._q("SELECT 1 FROM receipts WHERE id=?", (rid,)):
            return False
        if any_state and content and store._q("SELECT 1 FROM receipts WHERE content=? OR id=?",
                                              (content, content)):
            return False
        with store.tx() as c:
            c.execute("INSERT INTO receipts(id, path, name, project, node, session, status, claimed, outcome,"
                      " summary, reason, data_json, ingested_at, content) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (rid, fields.get("path"), fields.get("name"), fields.get("project"), fields.get("node"),
                       fields.get("session"), fields.get("status", "accepted"), fields.get("claimed"),
                       fields.get("outcome"), fields.get("summary"), fields.get("reason"),
                       json.dumps(fields.get("data") or {}), _now(), content))
    return True


def _update(store, rid, **fields):
    sets, vals = [], []
    for k, v in fields.items():
        sets.append("data_json=?" if k == "data" else f"{k}=?")
        vals.append(json.dumps(v) if k == "data" else v)
    with store.tx() as c:
        c.execute(f"UPDATE receipts SET {', '.join(sets)} WHERE id=?", (*vals, rid))


def _name_of(path):
    return Path(path).stem if path else None


def _parse(raw):
    if len(raw) > MAX_BYTES:
        return None, "the file is over 64 KB"
    try:
        return json.loads(raw.decode("utf-8-sig")), None
    except (UnicodeDecodeError, ValueError):
        return None, "the file is not valid JSON"


# ---------------------------------------------------------------- applying a receipt
def _deliverables(outputs, cwd=None):
    """Deliverables for the to-do. A relative output path is resolved against ``cwd`` (nothing else uses it)."""
    out = []
    for o in outputs or []:
        key = "url" if o.lower().startswith(("http://", "https://")) else "path"
        value = o
        if key == "path" and cwd and not os.path.isabs(o):
            value = str(Path(str(cwd)) / o)
        out.append({"label": o if key == "url" else (Path(o).name or o), key: value})
    return out


def _ask(store, q, project=None, node=None):
    pid = q.get("project") or project
    nid = q.get("node") or node
    if pid and (store.get_project(pid) is None or not nid or store.get_node(pid, nid) is None):
        pid = nid = None
    return needs.ask(store, project_id=pid, node_id=nid, kind=q["kind"], prompt=q["prompt"],
                     options=q.get("options"), scale=q.get("scale"))


def _live_claim(store, project, node):
    """The node's live claim view, or None. Never raises."""
    try:
        return store.claims(project).get(node)
    except Exception:
        return None


def _stamp(claim) -> str:
    return f"{claim.get('since') or ''}|{claim.get('refreshed_at') or ''}" if claim else ""


def _inside(path, folder) -> bool:
    try:
        a = os.path.normcase(os.path.realpath(str(path)))
        b = os.path.normcase(os.path.realpath(str(folder)))
        return os.path.commonpath([a, b]) == b
    except (ValueError, OSError):
        return False


def _belongs(claim, r, session, path) -> bool:
    """Does this receipt plausibly come from the claim's holder? (same session or agent, a file inside the
    claim's folder, or a claim that went stale)."""
    if claim.get("stale"):
        return True
    holder = str(claim.get("by") or "").strip().casefold()
    for who in (r.get("session"), session, r.get("agent")):
        if who and holder and str(who).strip().casefold() == holder:
            return True
    if path:
        for key in ("worktree", "cwd"):
            if claim.get(key) and _inside(path, claim[key]):
                return True
    return False


def _apply_node(store, r, runner, complete, claim=None, path=None, session=None):
    """Step 2 of the contract for a node receipt. Returns the outcome text."""
    project, node = r["project"], r["node"]
    row = store.get_project(project)
    current = store.get_node(project, node) if row is not None else None
    if row is None or current is None:
        return "unknown node"
    if current["status"] == "done" and r["status"] != "done":
        return "node already done; status ignored"
    # The done-check folder comes from the live claim (read before it is released), else the project.
    folder = ((claim or {}).get("worktree") or (claim or {}).get("cwd")) or row.get("cwd") or None
    kept = ""
    try:
        if claim and not _belongs(claim, r, session, path):
            kept = f"; claim kept: held by {claim.get('by')}"
        else:
            claims.release(store, project, node)
    except Exception:  # no claim, or a claim that cannot be dropped: carry on
        pass
    evidence = "; ".join(r.get("evidence") or []) or r["summary"]
    try:
        if r["status"] == "done":
            res = complete(store, project, node, runner=runner, cwd=folder)
            now = store.get_node(project, node)
            if now and now["status"] == "done":
                return "check passed" + kept
            return "check failed: " + " ".join(str(getattr(res, "detail", "")).split())[:300] + kept
        if r["status"] == "error":
            store.set_node_status(project, node, "error", ACTOR, evidence,
                                  f"A worker reported an error on task {node}.")
            return "logged" + kept
        store.set_node_status(project, node, "needs_you", ACTOR, evidence,
                              f"A worker needs Owner on task {node}.")
        return "logged" + kept
    except Exception as exc:
        return f"could not update the task: {str(exc).strip(chr(39))}"[:300]


def _sentence(r, outcome):
    node = r.get("node")
    if not node:
        return f"A session reported: {r['summary']}"
    if outcome.startswith("node already done"):
        what = "an error" if r["status"] == "error" else "that it needs Owner"
        return f"A worker reported {what} on task {node}, which is already done. Nothing changed. {r['summary']}"[:600]
    if outcome == "unknown node":
        return f"A receipt named task {node} in {r['project']}, which does not exist. {r['summary']}"
    verb = {"done": "done", "error": "an error", "needs_you": "that it needs Owner"}[r["status"]]
    tail = ""
    if r["status"] == "done":
        tail = " The check passed." if outcome.startswith("check passed") else " The check did not pass."
    return f"A worker reported {verb} on task {node}.{tail} {r['summary']}"[:600]


def ingest(store, raw_bytes, path=None, cwd=None, session=None, runner=None, complete=checks.complete_node):
    """Process one receipt file's bytes. Returns ``{"id", "status", "reason"?}`` (status accepted, invalid or
    duplicate). Idempotent by content hash. Synchronous: a done-check runs in this call."""
    ensure_schema(store)
    raw = raw_bytes if isinstance(raw_bytes, (bytes, bytearray)) else str(raw_bytes).encode("utf-8")
    raw = bytes(raw)
    content = digest(raw)
    base = {"path": str(path) if path else None, "name": _name_of(path)}
    obj, reason = _parse(raw)
    clean = None
    if reason is None:
        clean, reason = validate(obj)
    if reason:
        rid = receipt_id(raw)
        proj = obj.get("project") if isinstance(obj, dict) and isinstance(obj.get("project"), str) else None
        node = obj.get("node") if isinstance(obj, dict) and isinstance(obj.get("node"), str) else None
        if not _claim_hash(store, rid, content, True, **base, session=session, project=proj, node=node,
                           status="invalid", reason=reason, outcome="rejected"):
            return {"id": rid, "status": "duplicate"}
        store.notify("receipt", get(store, rid))
        return {"id": rid, "status": "invalid", "reason": reason}
    arg_session = session
    session = clean.get("session") or session
    claim = _live_claim(store, clean["project"], clean["node"]) if clean.get("project") else None
    rid = receipt_id(raw, _stamp(claim))
    if not _claim_hash(store, rid, content, claim is None, **base, session=session, project=clean.get("project"),
                       node=clean.get("node"), status="accepted", claimed=clean["status"], outcome="working",
                       summary=clean["summary"], data=clean):
        return {"id": rid, "status": "duplicate"}
    outcome = "logged"
    if clean.get("project"):
        outcome = _apply_node(store, clean, runner, complete, claim=claim, path=path, session=arg_session)
    known = outcome != "unknown node"
    try:
        todo.housekeep(store)
    except Exception:
        pass
    t = clean.get("todo")
    if t:
        try:
            node_ref = ({"project": clean["project"], "id": clean["node"]}
                        if clean.get("project") and known else None)
            todo.add_todo(store, t["text"], actor=ACTOR, section=t["section"], link=t.get("link"),
                          node=node_ref, deliverables=_deliverables(clean.get("outputs"), cwd))
        except Exception:
            pass
    asked = []
    for q in clean.get("questions") or []:
        try:
            asked.append(_ask(store, q, clean.get("project") if known else None, clean.get("node")))
        except Exception:
            pass
    if clean["status"] == "needs_you" and not asked:
        try:
            asked.append(_ask(store, {"kind": "text", "prompt": clean["summary"]},
                              clean.get("project") if known else None, clean.get("node")))
        except Exception:
            pass
    _update(store, rid, outcome=outcome)
    store.event(clean.get("project") if known else None, clean.get("node") if known else None, "receipt", ACTOR,
                _sentence(clean, outcome), {"receipt": rid, "claimed": clean["status"], "outcome": outcome})
    store.notify("receipt", get(store, rid))
    return {"id": rid, "status": "accepted"}


def ingest_question(store, raw, path=None):
    """Queue a ``waypoint.question.v1`` file with ``needs.ask``. Idempotent by content hash."""
    ensure_schema(store)
    raw = bytes(raw if isinstance(raw, (bytes, bytearray)) else str(raw).encode("utf-8"))
    rid = digest(raw)
    base = {"path": str(path) if path else None, "name": _name_of(path)}
    obj, reason = _parse(raw)
    clean = None
    if reason is None:
        clean, reason = validate_question(obj)
    if reason:
        if not _claim_hash(store, rid, **base, status="invalid", reason=reason, outcome="rejected",
                           data={"kind": "question"}):
            return {"id": rid, "status": "duplicate"}
        store.notify("receipt", get(store, rid))
        return {"id": rid, "status": "invalid", "reason": reason}
    if not _claim_hash(store, rid, **base, project=clean.get("project"), node=clean.get("node"),
                       status="accepted", outcome="question queued", summary=clean["prompt"],
                       data={"kind": "question", "question": clean}):
        return {"id": rid, "status": "duplicate"}
    try:
        qid = _ask(store, clean)
        _update(store, rid, data={"kind": "question", "question": clean, "question_id": qid})
    except Exception as exc:
        _update(store, rid, status="invalid", reason=str(exc).strip(chr(39))[:300], outcome="rejected")
        return {"id": rid, "status": "invalid", "reason": str(exc).strip(chr(39))[:300]}
    store.notify("receipt", get(store, rid))
    return {"id": rid, "status": "accepted"}


# ---------------------------------------------------------------- watcher
def inbox_dir() -> Path:
    return Path.home() / ".waypoint" / "inbox"


def _add(roots, seen, p):
    try:
        p = Path(p)
        key = os.path.normcase(str(p))
        if key not in seen and p.is_dir():
            seen.add(key)
            roots.append(p)
    except OSError:
        pass


def watch_roots(store, trace_cwds=None, inbox=None) -> list:
    """Folders whose ``.waypoint`` subfolder is watched: project cwds, live claim cwd/worktree, recently traced
    cwds and the inbox. Missing folders are skipped."""
    roots, seen = [], set()
    for p in store.list_projects():
        if p.get("cwd"):
            _add(roots, seen, p["cwd"])
        try:
            live = store.claims(p["id"])
        except Exception:
            live = {}
        for c in live.values():
            for key in ("cwd", "worktree"):
                if c.get(key):
                    _add(roots, seen, c[key])
    if trace_cwds is None:
        trace_cwds = []
        try:
            from . import trace  # lazy: may not exist yet
            fn = getattr(trace, "recent_cwds", None)
            if callable(fn):
                trace_cwds = fn(store, TRACE_HOURS) or []
        except Exception:
            trace_cwds = []
    for c in trace_cwds:
        if c:
            _add(roots, seen, c)
    _add(roots, seen, inbox if inbox is not None else inbox_dir())
    return roots


def _is_link(path) -> bool:
    """True for a symlink or a junction (or anything that cannot be inspected). Never follows it."""
    try:
        st = os.lstat(path)
    except OSError:
        return True
    return statmod.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & 0x400)


def _guard_folder(folder: Path):
    """Write ``.waypoint/.gitignore`` (``*``) the first time. Only when nothing is there at all (not even a
    dangling link) and the folder is not a link."""
    g = folder / ".gitignore"
    if _is_link(folder) or os.path.lexists(g):
        return
    try:
        with open(g, "x", encoding="utf-8") as f:
            f.write("*\n")
    except OSError:
        pass


def _is_inbox(root, inbox):
    try:
        return os.path.normcase(str(Path(root))) == os.path.normcase(str(Path(inbox)))
    except OSError:
        return False


def _refuse(store, path, reason, st):
    """Record a file we did not read (too big) as invalid, once per size and time."""
    ensure_schema(store)
    rid = digest(f"{path}|{st.st_size}|{st.st_mtime_ns}".encode("utf-8", "replace"))
    if _claim_hash(store, rid, path=str(path), name=_name_of(path), status="invalid", reason=reason,
                   outcome="rejected"):
        store.notify("receipt", get(store, rid))


def _node_key(raw):
    try:
        obj = json.loads(raw.decode("utf-8-sig"))
        if isinstance(obj.get("project"), str) and isinstance(obj.get("node"), str):
            return f"{obj['project']}/{obj['node']}"
    except (UnicodeDecodeError, ValueError, AttributeError):
        pass
    return None


def scan_once(store, roots, seen, executor=None, inbox=None, session=None) -> int:
    """Look at ``<root>/.waypoint/receipts/*.json`` and ``questions/*.json`` (the inbox holds the two folders
    directly), ingest files whose size or time changed, return how many were handed over. ``seen`` maps a file
    path to ``(mtime_ns, size)``. With an ``executor`` a receipt is processed on its worker thread. Symlinks and
    junctions are skipped. Only changed files count against the ``MAX_FILES`` budget, so a stat of an unchanged
    file is free and later roots are always looked at."""
    handed = 0
    inbox = inbox if inbox is not None else inbox_dir()
    budget = MAX_FILES
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        is_inbox = _is_inbox(root, inbox)
        base = root if is_inbox else root / FOLDER
        if not is_inbox and (_is_link(base) or not base.is_dir()):
            continue
        if not is_inbox:
            _guard_folder(base)
        for sub, fn in (("receipts", "receipt"), ("questions", "question")):
            d = base / sub
            if _is_link(d):
                continue
            try:
                entries = [e for e in os.scandir(d) if e.name.endswith(".json")]
            except OSError:
                continue
            for e in entries:
                try:
                    st = os.lstat(e.path)
                except OSError:
                    continue
                if not statmod.S_ISREG(st.st_mode) or _is_link(e.path):
                    continue
                sig = (st.st_mtime_ns, st.st_size)
                if seen.get(e.path) == sig:
                    continue
                if budget <= 0:  # over the cap: leave it unseen, the next pass takes it
                    continue
                budget -= 1
                if st.st_size > MAX_BYTES:
                    seen[e.path] = sig
                    _refuse(store, e.path, "the file is over 64 KB", st)
                    handed += 1
                    continue
                try:
                    with open(e.path, "rb") as f:
                        raw = f.read(MAX_BYTES + 1)
                except OSError:
                    continue
                seen[e.path] = sig
                cwd = None if is_inbox else str(root)
                if fn == "question":
                    ingest_question(store, raw, e.path)
                elif executor is not None:
                    key = _node_key(raw)
                    if hasattr(executor, "workers") and key:
                        executor.submit(_safe_ingest, store, raw, e.path, cwd, session, key=key)
                    else:
                        executor.submit(_safe_ingest, store, raw, e.path, cwd, session)
                else:
                    ingest(store, raw, path=e.path, cwd=cwd, session=session)
                handed += 1
    return handed


def _safe_ingest(store, raw, path, cwd, session):
    try:
        ingest(store, raw, path=path, cwd=cwd, session=session)
    except Exception:
        pass


class KeyedPool:
    """A small thread pool that never runs two jobs with the same key at once (one node, one check at a time).
    Later jobs for a busy key wait in line and run on the same worker when it is free."""

    def __init__(self, workers=2, prefix="receipt-check"):
        self.workers = workers
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=prefix)
        self._lock = threading.Lock()
        self._busy: dict = {}

    def submit(self, fn, *args, key=None):
        if key is None:
            return self.pool.submit(fn, *args)
        with self._lock:
            queue = self._busy.get(key)
            if queue is not None:
                queue.append((fn, args))
                return None
            self._busy[key] = deque()
        return self.pool.submit(self._run, key, fn, args)

    def _run(self, key, fn, args):
        while True:
            try:
                fn(*args)
            except Exception:
                pass
            with self._lock:
                queue = self._busy[key]
                if not queue:
                    del self._busy[key]
                    return
                fn, args = queue.popleft()

    def shutdown(self, wait=False):
        self.pool.shutdown(wait=wait)


class Watcher:
    def __init__(self, app, poll=POLL_SECONDS, roots_every=ROOTS_SECONDS, inbox=None):
        self.app, self.poll, self.roots_every, self.inbox = app, poll, roots_every, inbox
        self.stopping = threading.Event()
        self.seen: dict = {}
        self.pool = KeyedPool(CHECK_WORKERS)
        self.thread = threading.Thread(target=self._run, name="receipts-watch", daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.stopping.set()
        self.pool.shutdown(wait=False)

    def _run(self):
        roots, at = [], 0.0
        closing = getattr(self.app, "closing", None)
        while not self.stopping.is_set() and not (closing is not None and closing.is_set()):
            try:
                if time.monotonic() - at >= self.roots_every or not roots:
                    roots, at = watch_roots(self.app.store, inbox=self.inbox), time.monotonic()
                scan_once(self.app.store, roots, self.seen, executor=self.pool, inbox=self.inbox)
            except Exception:
                pass
            self.stopping.wait(self.poll)


def start(app):
    return Watcher(app).start()


# ---------------------------------------------------------------- routes
def _http_error(status, body):
    from .api import HttpError  # lazy: api imports extension modules
    return HttpError(status, body)


def _safe_path(path):
    """A local absolute path to a ``.json`` file inside a ``.waypoint`` folder (resolved). UNC and ``//`` refused."""
    text = str(path)
    if text.replace("/", "\\").startswith("\\\\") or not Path(text).is_absolute():
        raise _http_error(400, {"error": "bad_request", "detail": "path must be a local absolute path"})
    try:
        p = Path(text).resolve()
    except (OSError, ValueError):
        raise _http_error(400, {"error": "bad_request", "detail": "that path cannot be read"})
    if str(p).replace("/", "\\").startswith("\\\\"):
        raise _http_error(400, {"error": "bad_request", "detail": "path must be a local absolute path"})
    if p.suffix.lower() != ".json" or FOLDER not in p.parts[:-1]:
        raise _http_error(400, {"error": "bad_request",
                                "detail": "path must be a .json file inside a .waypoint folder"})
    return p


def p_receipt(h, q):
    """{path? | receipt?, cwd?, session?} -> {ok, id, status, reason?}."""
    b = h._json_body()
    store = h.app.store
    cwd = b.get("cwd") or None
    session = b.get("session") or None
    if b.get("path"):
        p = _safe_path(b["path"])
        try:
            with open(p, "rb") as f:
                raw = f.read(MAX_BYTES + 1)
        except OSError:
            raise _http_error(404, {"error": "not_found", "detail": "cannot read that file"})
        out = ingest(store, raw, path=str(p), cwd=cwd, session=session)
    elif isinstance(b.get("receipt"), dict):
        raw = json.dumps(b["receipt"], sort_keys=True).encode("utf-8")
        out = ingest(store, raw, cwd=cwd, session=session)
    else:
        raise _http_error(400, {"error": "bad_request", "detail": "send 'path' or 'receipt'"})
    return dict(out, ok=out["status"] != "invalid")


def g_receipts(h, q):
    v = (q or {}).get("limit")
    v = v[0] if isinstance(v, (list, tuple)) and v else v
    try:
        limit = int(v) if v not in (None, "") else 50
    except (TypeError, ValueError):
        limit = 50
    return {"receipts": list_receipts(h.app.store, limit)}


ROUTES = {
    "POST /api/receipt": p_receipt,
    "GET /api/receipts": g_receipts,
}
