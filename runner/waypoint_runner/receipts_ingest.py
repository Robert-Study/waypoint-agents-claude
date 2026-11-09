"""Receipt and question ingestion for the runner (Waypoint 1.2, node run-ingest).

``scan_inbox(store, roots)`` reads the receipt and question files agents wrote under ``<cwd>/.waypoint`` (or the
inbox folder), validates each one and writes the result into the data folder through the folder store
(``core/waypoint-data/py``, never SQLite). The validation rules are the engine's (``engine/cc_engine/receipts.py``,
checked for parity by the tests); only its SQLite writes are left behind.

Rules
- A receipt never marks a node done. It is stored as a write-once ``receipts/<name>.json`` and the node check decides.
- Each file is ingested once, by SHA-256 of its bytes (``ingest/<digest>.json`` is the ledger entry, written last;
  every other write uses a deterministic id, so a crash between the two just repeats harmless work).
- A bad file is moved to ``_invalid/`` beside it, reported in the result and in a Review to-do (subtype failure,
  shown red). It is never dropped.
- The producer sets the to-do category; no reader classifies:
  invalid receipt or failed check -> Review/failure; pending question -> Decisions/approval; a Owner-only action
  (a receipt to-do of either section) -> Hands-on/own.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

_PY = Path(__file__).resolve().parents[2] / "core" / "waypoint-data" / "py"
if str(_PY) not in sys.path and _PY.is_dir():
    sys.path.insert(0, str(_PY))

from waypoint_data import io as wio  # noqa: E402
from waypoint_data.store import RevConflict, iso  # noqa: E402

SCHEMA_RECEIPT = "waypoint.receipt.v1"
SCHEMA_QUESTION = "waypoint.question.v1"
SCHEMA_INGEST = "waypoint.ingest.v1"
MAX_BYTES = 64 * 1024
MAX_SUMMARY = 300
MAX_LIST = 20
MAX_ITEM = 500
MAX_QUESTIONS = 3
MAX_OPTIONS = 20
MAX_PROMPT = 500
MAX_TODO = 2000
MAX_FILES = 500
STATUSES = ("done", "error", "needs_you")
KINDS = ("select", "likert", "text")
SECTIONS = ("human", "computer")
PRESELECT_KEYS = ("selected", "default", "checked", "preselected")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.json$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
INVALID_DIR = "_invalid"
ACTOR = "runner"


# ---------------------------------------------------------------- validation (same rules as the engine)
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
    for key in ("project", "node", "session"):
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


def validate_loose_question(obj):
    """Like validate_question but without the schema marker (a question inside a receipt or sync item)."""
    return _question(obj, False)


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


def parse(raw):
    """``(object, None)`` or ``(None, reason)`` for the bytes of one file."""
    if len(raw) > MAX_BYTES:
        return None, "the file is over 64 KB"
    try:
        return json.loads(raw.decode("utf-8-sig")), None
    except (UnicodeDecodeError, ValueError):
        return None, "the file is not valid JSON"


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------- store helpers
def _safe(name):
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:80] or "x"


def _create(store, rel, entity):
    """Create one entity if absent. True if this call created it, False if it already existed."""
    try:
        store.write(rel, entity, expected_rev=0)
        return True
    except RevConflict:
        return False


def _known_node(store, project, node):
    if not (isinstance(project, str) and isinstance(node, str) and ID_RE.match(project) and ID_RE.match(node)):
        return False
    return store.exists(f"projects/{project}/nodes/{node}.json")


def _next_order(store, category):
    return 1 + sum(1 for t in store.list_entities("todos") if isinstance(t, dict) and t.get("category") == category)


def _add_todo(store, key, text, category, subtype, project=None, node=None, link=None, next_click=None):
    """Write one to-do with a deterministic id (so a repeat is a no-op). Returns its id."""
    tid = f"td-{_safe(key)}"[:64]
    node_ref = {"project": project, "id": node} if project and node and _known_node(store, project, node) else None
    proj = project if project and isinstance(project, str) and ID_RE.match(project) else None
    _create(store, f"todos/{tid}.json", {
        "schema": "waypoint.todo.v1", "id": tid, "text": text[:MAX_TODO] or "(no text)", "status": "open",
        "category": category, "subtype": subtype, "link": link, "node": node_ref, "project": proj,
        "order": _next_order(store, category), "merged_into": None, "click_count": 0, "next_click": next_click,
        "created_at": iso(store.now()), "created_by": ACTOR})
    return tid


def add_review_todo(store, key, text, project=None, node=None, link=None):
    """Review to-do, subtype failure (shown red)."""
    return _add_todo(store, key, text, "review", "failure", project, node, link, "Open the failure")


def add_decision_todo(store, key, text, project=None, node=None, link=None):
    """Decisions to-do, subtype approval."""
    return _add_todo(store, key, text, "decisions", "approval", project, node, link, "Answer the question")


def add_hands_on_todo(store, text, project=None, node=None, link=None, key=None):
    """Hands-on to-do for an action only Owner can do (subtype own)."""
    return _add_todo(store, key or "own-" + digest(text.encode("utf-8"))[:12], text, "hands_on", "own",
                     project, node, link, "Do it, then tick it off")


def report_check_failure(store, project, node, summary, link=None):
    """A node's check failed: write the Review/failure to-do (once per project, node and summary)."""
    key = "fail-" + digest(f"{project}\0{node}\0{summary}".encode("utf-8"))[:12]
    return add_review_todo(store, key, f"{project}/{node}: {summary}", project, node, link)


def _event(store, kind, summary, project=None, node=None, data=None):
    ev = {"kind": kind, "summary": summary[:300], "data": data}
    if project and node and _known_node(store, project, node):
        ev["project"], ev["node"] = project, node
    try:
        store.append_event(ACTOR, ev)
    except Exception:
        pass  # an event is a trace, never a reason to lose the ingest


def write_question(store, qid, q, project=None, node=None, session=None):
    """Write questions/<qid>.json and its Decisions to-do. Returns qid. Options are plain text, none preselected.

    ``session`` is the asking session (the receipt's or the question file's), kept so an answer can go back to it."""
    known = _known_node(store, project, node)
    opts = None
    if q.get("options"):
        opts = [o if isinstance(o, str) else str(o.get("label") or o.get("value")) for o in q["options"]]
    ent = {"schema": SCHEMA_QUESTION, "id": qid, "project": project if known else None,
           "node": node if known else None, "kind": q["kind"], "prompt": q["prompt"]}
    if opts is not None:
        ent["options"] = opts
    ent.update({"scale": q.get("scale"), "answer": None, "answered_at": None, "captured": False,
                "purpose": None, "session": session or q.get("session") or None, "created_at": iso(store.now())})
    _create(store, f"questions/{qid}.json", ent)
    add_decision_todo(store, "q-" + qid, q["prompt"], project, node, link=f"questions/{qid}.json")
    return qid


# ---------------------------------------------------------------- ingest of one file
def _ledger_has(store, dig):
    return store.exists(f"ingest/{dig}.json")


def _ledger_add(store, dig, kind, name, outcome, reason=None, project=None, node=None):
    _create(store, f"ingest/{dig}.json", {
        "schema": SCHEMA_INGEST, "id": dig, "kind": kind, "name": name, "outcome": outcome, "reason": reason,
        "project": project if isinstance(project, str) and ID_RE.match(project) else None,
        "node": node if isinstance(node, str) and ID_RE.match(node) else None,
        "created_at": iso(store.now())})


def _reject(store, dig, kind, name, reason, obj):
    project = obj.get("project") if isinstance(obj, dict) else None
    node = obj.get("node") if isinstance(obj, dict) else None
    add_review_todo(store, f"bad-{dig[:12]}", f"The {kind} file {name} was rejected: {reason}. "
                    f"It was moved to {INVALID_DIR}/ beside the original.", project, node)
    _event(store, "ingest.invalid", f"Rejected {kind} file {name}: {reason}", project, node,
           {"digest": dig, "name": name})
    _ledger_add(store, dig, kind, name, "invalid", reason, project, node)


def ingest_receipt(store, raw, name="receipt.json"):
    """Process the bytes of one receipt file. Returns ``{"id", "status", "reason"?}`` (accepted, invalid, duplicate)."""
    raw = bytes(raw)
    dig = digest(raw)
    if _ledger_has(store, dig):
        return {"id": dig, "status": "duplicate", "kind": "receipt", "name": name}
    obj, reason = parse(raw)
    clean = None
    if reason is None:
        clean, reason = validate(obj)
    if reason:
        _reject(store, dig, "receipt", name, reason, obj)
        return {"id": dig, "status": "invalid", "reason": reason, "kind": "receipt", "name": name}
    project, node = clean.get("project"), clean.get("node")
    stem = f"{project}.{node}" if project else Path(name).stem
    out = f"receipts/{_safe(stem)}.{dig[:8]}.json"
    try:
        wio.create_exclusive(store.path(out), store.serialize(clean), writer=ACTOR)
    except FileExistsError:
        pass
    t = clean.get("todo")
    if t:  # the section is advice from the agent; either way it is something for Owner to do or hand off
        add_hands_on_todo(store, t["text"], project, node, t.get("link"), key=f"rt-{dig[:12]}")
    asked = 0
    for i, q in enumerate(clean.get("questions") or []):
        write_question(store, f"q-{dig[:12]}-{i}", q, q.get("project") or project, q.get("node") or node,
                       clean.get("session"))
        asked += 1
    if clean["status"] == "needs_you" and not asked:
        write_question(store, f"q-{dig[:12]}-0", {"kind": "text", "prompt": clean["summary"]}, project, node,
                       clean.get("session"))
    _event(store, "receipt", f"Receipt for {project}/{node}: {clean['status']}. {clean['summary']}" if project
           else f"Receipt: {clean['status']}. {clean['summary']}", project, node,
           {"receipt": out, "claimed": clean["status"], "digest": dig})
    _ledger_add(store, dig, "receipt", name, "accepted", None, project, node)
    return {"id": dig, "status": "accepted", "kind": "receipt", "name": name, "stored": out}


def ingest_question(store, raw, name="question.json"):
    """Process the bytes of one question file. Returns ``{"id", "status", "reason"?}``."""
    raw = bytes(raw)
    dig = digest(raw)
    if _ledger_has(store, dig):
        return {"id": dig, "status": "duplicate", "kind": "question", "name": name}
    obj, reason = parse(raw)
    clean = None
    if reason is None:
        clean, reason = validate_question(obj)
    if reason:
        _reject(store, dig, "question", name, reason, obj)
        return {"id": dig, "status": "invalid", "reason": reason, "kind": "question", "name": name}
    qid = f"q-{dig[:12]}"
    write_question(store, qid, clean, clean.get("project"), clean.get("node"))
    _event(store, "question", f"Question queued: {clean['prompt']}", clean.get("project"), clean.get("node"),
           {"question": qid, "digest": dig})
    _ledger_add(store, dig, "question", name, "accepted", None, clean.get("project"), clean.get("node"))
    return {"id": dig, "status": "accepted", "kind": "question", "name": name, "stored": f"questions/{qid}.json"}


# ---------------------------------------------------------------- folder scan
def move_aside(path, folder=INVALID_DIR):
    """Move a file into ``<its folder>/_invalid/`` without overwriting. Returns the new path, or None if it failed."""
    src = Path(path)
    dest_dir = src.parent / folder
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        i = 0
        while dest.exists():
            i += 1
            dest = dest_dir / f"{src.stem}.{i}{src.suffix}"
        os.replace(src, dest)
        return str(dest)
    except OSError:
        return None


def _resolve(root):
    p = Path(root)
    return p / ".waypoint" if (p / ".waypoint").is_dir() else p


def _files(folder):
    names = [n for n in wio.list_names(str(folder)) if NAME_RE.match(n)]
    out = []
    for n in names[:MAX_FILES]:
        p = folder / n
        if not p.is_symlink():
            out.append(p)
    return out


def scan_inbox(store, roots):
    """Ingest every receipt and question file under the given ``.waypoint`` folders (or their parent folders).

    Returns one result dict per file seen: ``{name, path, kind, status, reason?, moved_to?}``. Safe to call on a timer.
    """
    results = []
    for root in roots or []:
        base = _resolve(root)
        for sub, fn, kind in (("receipts", ingest_receipt, "receipt"), ("questions", ingest_question, "question")):
            for path in _files(base / sub):
                try:
                    with open(path, "rb") as f:
                        raw = f.read(MAX_BYTES + 1)  # an oversize file is rejected by parse() without reading all of it
                    res = fn(store, raw, path.name)
                except OSError as exc:
                    results.append({"name": path.name, "path": str(path), "kind": kind, "status": "unreadable",
                                    "reason": str(exc)})
                    continue
                res["path"] = str(path)
                if res["status"] == "invalid" or (res["status"] == "duplicate" and _was_invalid(store, res["id"])):
                    res["moved_to"] = move_aside(path)
                results.append(res)
    return results


def _was_invalid(store, dig):
    try:
        e = store.read(f"ingest/{dig}.json")
    except Exception:
        return False
    return isinstance(e, dict) and e.get("outcome") == "invalid"
