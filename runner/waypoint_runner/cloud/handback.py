"""Hand a node back from the cloud to the PC, and keep both sides' work when PC and cloud both wrote
(Waypoint 1.2, node cloud-handback). Protocol: core/docs/cloud/PROTOCOL.md section 4 (reconcile).

Rules:

* One claim token owns a node at a time. ``acquire`` goes through the shared claim protocol (waypoint_data Claims);
  a live claim by another token answers ``held``. ``hand_back`` acts only with the token that holds the claim.
* Receipts are append-only and numbered per run: ``<project>.<node>.json``, then ``.2.json``, ``.3.json``. A run
  writes once (a second write by the same run returns its first file). Nothing here edits or deletes a receipt.
* Divergent writes (a PC receipt written after the node moved to the cloud, a Drive/git conflicted copy of a receipt,
  or one run's receipts that disagree) keep every receipt, write ``reviews/<project>__<node>.json`` (kind
  ``needs-review``, created once, never replaced) and set the node to ``needs_you``. The newer write never wins.
* A cloud ``done`` comes home as ``submitted``; the PC runs the node's own done-check before it is ``done``.

Stdlib only.
"""
import json
import os
import re
import time

from waypoint_runner import recovery as _recovery  # noqa: F401  puts core/waypoint-data/py on sys.path

from waypoint_data import io as wio  # noqa: E402
from waypoint_data.claims import Claims  # noqa: E402
from waypoint_data.store import RevConflict, Store, iso, parse_iso, serialize  # noqa: E402

__all__ = ["acquire", "hand_back", "append_receipt", "next_receipt_name", "list_receipts", "find_divergence",
           "mark_needs_review"]

WRITER = "cloud-handback"
RECEIPT_SCHEMA = "waypoint.receipt.v1"
REVIEW_SCHEMA = "waypoint.needs_review.v1"
_HOME_STATUS = {"done": "submitted", "error": "error", "needs_you": "needs_you"}


def _pat(project, node):
    return re.compile(rf"^{re.escape(project)}\.{re.escape(node)}(?:\.(\d+))?\.json$")


def _rdir(root):
    return os.path.join(os.path.abspath(root), "receipts")


def _names(root):
    try:
        return sorted(os.listdir(_rdir(root)))
    except OSError:
        return []


def _num(name, pat):
    m = pat.match(name)
    return None if not m else int(m.group(1) or 1)


def list_receipts(root, project, node):
    """Receipt file names of the node in run order (1, 2, 3 ...). Conflicted copies are not listed here."""
    pat = _pat(project, node)
    found = [(n, _num(n, pat)) for n in _names(root) if pat.match(n)]
    return [n for n, _ in sorted(found, key=lambda t: t[1])]


def _conflicted(root, project, node):
    pat = _pat(project, node)
    prefix = f"{project}.{node}"
    return [n for n in _names(root) if n.startswith(prefix) and n.endswith(".json") and not pat.match(n)
            and wio.is_conflicted_copy_name(n) and n[len(prefix):len(prefix) + 1] in (" ", "(")]


def next_receipt_name(root, project, node):
    pat = _pat(project, node)
    top = max([_num(n, pat) for n in _names(root) if pat.match(n)] or [0])
    return f"{project}.{node}.json" if top == 0 else f"{project}.{node}.{top + 1}.json"


def _read(root, name):
    return wio.read_json(os.path.join(_rdir(root), name))


def append_receipt(root, project, node, session, fields):
    """Write the next numbered receipt exactly once per ``session`` (the run). Returns its path. Never edits."""
    os.makedirs(_rdir(root), exist_ok=True)
    for name in list_receipts(root, project, node):
        r = _read(root, name)
        if isinstance(r, dict) and r.get("session") == session:
            return os.path.join(_rdir(root), name)
    body = {"schema": RECEIPT_SCHEMA, "project": project, "node": node, "status": fields.get("status", "done"),
            "summary": str(fields.get("summary") or "Run finished.")[:300],
            "evidence": list(fields.get("evidence") or [])[:20], "outputs": list(fields.get("outputs") or [])[:20],
            "todo": fields.get("todo"), "session": session}
    while True:
        path = os.path.join(_rdir(root), next_receipt_name(root, project, node))
        try:
            wio.create_exclusive(path, serialize(body), writer=WRITER)
            return path
        except FileExistsError:
            continue  # another writer took that number: take the next


def _is_cloud(session):
    return str(session or "").startswith("cloud:")


def find_divergence(root, project, node, moved_to_cloud_at=None):
    """None, or {"reasons": [...], "receipts": [names]} when PC and cloud (or a merge) wrote divergent receipts."""
    names = list_receipts(root, project, node)
    reasons = []
    copies = _conflicted(root, project, node)
    if copies:
        reasons.append("conflicted_copy")
    docs = {}
    for n in names:
        r = _read(root, n)
        if isinstance(r, dict):
            docs[n] = r
    by_session = {}
    for n, r in docs.items():
        by_session.setdefault(r.get("session"), []).append(n)
    for session, ns in by_session.items():
        if session and len(ns) > 1 and any(docs[a] != docs[ns[0]] for a in ns[1:]):
            reasons.append("same_run_differs")
            break
    if moved_to_cloud_at is not None and any(_is_cloud(r.get("session")) for r in docs.values()):
        for n, r in docs.items():
            if _is_cloud(r.get("session")):
                continue
            try:
                late = os.path.getmtime(os.path.join(_rdir(root), n)) > moved_to_cloud_at
            except OSError:
                late = False
            if late:
                reasons.append("pc_wrote_after_cloud_move")
                break
    if not reasons:
        return None
    return {"reasons": reasons, "receipts": sorted(names + copies)}


def mark_needs_review(root, project, node, divergence, now=time.time):
    """Create reviews/<project>__<node>.json once. An existing mark is kept as it is. Returns its path."""
    path = os.path.join(os.path.abspath(root), "reviews", f"{project}__{node}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = {"schema": REVIEW_SCHEMA, "kind": "needs-review", "project": project, "node": node,
            "reasons": list(divergence["reasons"]), "receipts": list(divergence["receipts"]), "at": iso(now())}
    try:
        wio.create_exclusive(path, serialize(body), writer=WRITER)
    except FileExistsError:
        pass
    return path


def acquire(root, project, node, owner, location, token, now=time.time, claimed_by=None):
    """Take the node's one claim token through the shared protocol (``held`` while another live token owns it)."""
    return Claims(root, now=now, writer=WRITER).claim(project, node, owner, location, token,
                                                      claimed_by=claimed_by or owner)


def _update_node(store, project, node, now, **change):
    rel = f"projects/{project}/nodes/{node}.json"
    for _ in range(3):
        cur = store.read(rel)
        if not isinstance(cur, dict):
            return None
        new = dict(cur, **change)
        try:
            store.write(rel, new, expected_rev=cur.get("rev", 0))
            return new
        except RevConflict:
            continue
    raise RuntimeError("rev conflict")


def _event(store, kind, project, node, summary, data=None):
    try:
        store.append_event(WRITER, {"kind": kind, "project": project, "node": node, "summary": summary,
                                    "data": data or {}})
    except OSError:
        pass


def hand_back(root, project, node, token, now=time.time, moved_to_cloud_at=None):
    """Return a cloud node to the PC with the claim token that holds it.

    Returns {"status": handed_back | needs_review | not_owner | no_receipt | missing, ...}. Receipts are never touched.
    """
    claims = Claims(root, now=now, writer=WRITER)
    claim = claims.read(project, node)
    if not claim or claim.get("token") != token:
        return {"status": "not_owner", "node": node}
    store = Store(root, writer=WRITER, now=now)
    cur = store.read(f"projects/{project}/nodes/{node}.json")
    if not isinstance(cur, dict):
        return {"status": "missing", "node": node}
    if moved_to_cloud_at is None:
        loc = cur.get("location") or {}
        if loc.get("where") == "cloud":
            moved_to_cloud_at = parse_iso(loc.get("changed_at"))
    div = find_divergence(root, project, node, moved_to_cloud_at)
    if div:
        mark = mark_needs_review(root, project, node, div, now=now)
        _update_node(store, project, node, now, status="needs_you")
        _event(store, "cloud.needs_review", project, node, "PC and cloud both wrote; both receipts kept",
               {"reasons": div["reasons"], "receipts": div["receipts"]})
        return {"status": "needs_review", "node": node, "reasons": div["reasons"], "mark": mark}
    names = list_receipts(root, project, node)
    last = _read(root, names[-1]) if names else None
    if not isinstance(last, dict):
        return {"status": "no_receipt", "node": node}
    status = _HOME_STATUS.get(last.get("status"), "needs_you")
    _update_node(store, project, node, now, status=status,
                 location={"where": "pc", "changed_at": iso(now())})
    claims.release(project, node, token)
    _event(store, "cloud.handed_back", project, node, f"returned to the PC as {status}",
           {"receipt": names[-1], "receipt_status": last.get("status")})
    return {"status": "handed_back", "node": node, "node_status": status, "receipt": names[-1]}
