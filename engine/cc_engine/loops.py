"""Loops part 2: return packets (W3), re-plan (W4), blast-radius lanes (W5), worker isolation (W7).

All helpers take plain node dicts; ``risk`` and ``scope`` come from the node or, when the node
dict lacks them, from the ``node_loops`` table (``db`` is a Store or a database path).
Only the failing node is ever touched: siblings that passed stay done.
"""
from __future__ import annotations

import json
import os

from . import failure, node_kinds
from . import scheduler as S

RISKS = ("contained", "wide", "closed")
DEFAULT_RISK = "contained"
PACKET_KEYS = ("unit", "verdict", "reason", "evidence", "scope")
REPLAN_OPTIONS = ["Send this branch back to the planner", "Add a fix-up step before this task",
                  "Do this task manually", "Skip for now and continue the rest"]


# ---------------------------------------------------------------- risk and scope
def _row(node, db):
    pid, nid = node.get("project_id"), node.get("id")
    if db is None or not pid or not nid:
        return {}
    return node_kinds.rows(db, pid).get(nid) or {}


def risk_of(node, db=None) -> str:
    """contained | wide | closed. Free-text ``lane`` is a label; a lane that is itself a risk word counts."""
    stored = _row(node, db).get("risk") if node.get("risk") is None else None
    for v in (node.get("risk"), stored, node.get("lane")):
        if v in RISKS:
            return v
    return DEFAULT_RISK


def scope_of(node, db=None) -> list:
    sc = node.get("scope")
    if sc is None:
        sc = _row(node, db).get("scope")
    if isinstance(sc, str):  # a JSON list, or one path / comma-separated paths, never characters
        try:
            sc = json.loads(sc) if sc.strip().startswith("[") else sc.split(",")
        except ValueError:
            sc = sc.split(",")
    return [str(x).strip() for x in sc or [] if str(x).strip()]


def can_complete(node, check_type, check_ok, owner_action=False, db=None):
    """``(allowed, why)``: may a passing/failing check complete this node?"""
    if not check_ok:
        return False, "the check did not pass"
    risk = risk_of(node, db)
    if risk == "closed" and not owner_action:
        return False, "closed-risk work needs an owner action"
    if risk == "wide" and not owner_action and check_type not in node_kinds.DETERMINISTIC_CHECKS:
        return False, "wide-risk work needs a deterministic check"
    return True, ""


# ---------------------------------------------------------------- W3 packets
def _norm(p):
    p = str(p).replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    p = p.rstrip("/")
    return p.lower() if os.name == "nt" else p  # Windows paths are case-insensitive


def diff_check(node, changed_files, db=None):
    """``(ok, outside)``: every changed file must sit inside SCOPE (empty scope = unrestricted)."""
    scope = [_norm(s) for s in scope_of(node, db)]
    if not scope:
        return True, []
    outside = []
    for f in changed_files or []:
        nf = _norm(f)
        if not any(nf == s or nf.startswith(s + "/") for s in scope):
            outside.append(f)
    return not outside, outside


def build_packet(node, verdict, reason, evidence, db=None) -> dict:
    return {"unit": node["id"], "verdict": str(verdict), "reason": str(reason).strip(),
            "evidence": str(evidence or "")[:1500], "scope": scope_of(node, db)}


def packet_summary(pk) -> str:
    scope = ", ".join(pk["scope"]) if pk["scope"] else "any file"
    return f"Returned: {pk['reason']}, scope {scope}"


def packet_text(pk) -> str:
    return "\n".join([f"UNIT: {pk['unit']}", f"VERDICT: {pk['verdict']}", f"REASON: {pk['reason']}",
                      f"EVIDENCE: {pk['evidence']}",
                      f"SCOPE: {', '.join(pk['scope']) if pk['scope'] else 'any file'}"])


def rework_prompt(prompt, pk) -> str:
    """The base prompt plus the return packet verbatim."""
    return (f"{prompt.rstrip()}\n\n## Returned for rework (touch only SCOPE; fix only the REASON)\n"
            f"{packet_text(pk)}\n")


def _k(kind, pid, nid):
    return f"{kind}:{pid}:{nid}"


def latest_packet(store, project_id, node_id):
    raw = S.meta_get(store, _k("packet", project_id, node_id))
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def clear_reason(store, project_id, node_id):
    """A node that passed forgets its failure history."""
    for kind in ("last_reason", "packet"):
        if S.meta_get(store, _k(kind, project_id, node_id)) is not None:
            S.meta_set(store, _k(kind, project_id, node_id), None)


# ---------------------------------------------------------------- W4 re-plan
def request_replan(store, project_id, node, packet, asker=None, why=None):
    nid = node["id"]
    why = why or packet["reason"]
    summary = f"Task {nid} needs a re-plan: {why}."
    S.emit(store, project_id, nid, "replan", summary, actor="failure", data={"packet": packet})
    S.set_status(store, project_id, nid, "needs_you", summary, actor="failure", evidence=packet["evidence"])
    qid = None
    if asker is not None:
        qid = asker(project_id=project_id, node_id=nid, kind="select", purpose="replan",
                    prompt=f"Task {nid} cannot be fixed by retrying ({why}). Send its branch back to the "
                           f"planner for a re-plan?", options=list(REPLAN_OPTIONS))
    return {"action": "replan", "packet": packet, "question_id": qid}


def on_failed_check(store, project_id, node, settings, verdict="fail", reason="", evidence="",
                    asker=None, changed_files=None):
    """A completed attempt failed: return the unit with a packet, then escalate or re-plan."""
    node = S.norm_node(node)
    nid = node["id"]
    if changed_files is not None:
        ok, outside = diff_check(node, changed_files, store)
        if not ok:
            reason = f"touched files outside scope: {', '.join(outside)}"
            evidence = evidence or reason
            verdict = "out_of_scope"
    pk = build_packet(node, verdict, reason or "check failed", evidence, store)
    S.emit(store, project_id, nid, "returned", packet_summary(pk), actor="failure", data={"packet": pk})
    S.meta_set(store, _k("packet", project_id, nid), json.dumps(pk))
    prev = S.meta_get(store, _k("last_reason", project_id, nid))
    S.meta_set(store, _k("last_reason", project_id, nid), pk["reason"])
    if prev is not None and prev == pk["reason"]:
        return request_replan(store, project_id, node, pk, asker, "the same reason came back twice")
    if failure.next_rung(node, settings, toggles=store) is None:
        return request_replan(store, project_id, node, pk, asker, "every model and effort level was tried")
    out = failure.on_check_failed(store, project_id, node, settings, f"{pk['reason']} ({pk['evidence']})",
                                  asker=asker)
    out["packet"] = pk
    return out


# ---------------------------------------------------------------- W7 isolation
def leaks(prompt, others_output) -> list:
    return [o for o in others_output or [] if o and str(o) in prompt]


def isolate(prompt, others_output) -> str:
    for o in sorted((str(x) for x in others_output or [] if x), key=len, reverse=True):
        prompt = prompt.replace(o, "[withheld]")
    return prompt


def worker_prompt(node, project=None, packet=None, others_output=()):
    """The prompt for one worker: its own task (and own return packet) only, never a sibling's output."""
    from . import dispatch
    prompt = dispatch.build_prompt(node, project)
    if packet and packet.get("unit") == node.get("id"):
        prompt = rework_prompt(prompt, packet)
    return isolate(prompt, others_output)
