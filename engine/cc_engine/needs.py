"""Needs-you queue: Likert, select-all and text questions for Owner.

Every question carries ``allowed`` answer types, modelled on LangChain Agent Inbox:
``accept`` (approve the proposal as proposed), ``edit`` (change the proposal's fields),
``reply`` (free text), ``ignore`` (dismiss) and ``answer`` (the likert/select/text value).
An answer is recorded as ``{"type", "value"}``; ``ignore`` closes the question without
capture or any other side effect. Options never carry a preselected flag.

Every question also carries a ``purpose`` that decides what its answer *does*:

- ``preference``: Owner's own views. The ONLY purpose whose (owner) answers go to preference
  capture through ``sync_preferences.py --capture <payload.json>`` via an injectable runner.
- ``manual_result``: a Gemini App / manual run. A ``reply`` with the pasted output is written to
  the node's output file (``proposal.output_path``, else the node's ``output``, else the single
  path its ``text_present`` / ``files_exist`` check names) or, failing that, stored as evidence;
  then ``checks.complete_node`` runs. Pass -> done; fail -> the failure policy, as a normal
  failed attempt.
- ``failure_choice``: what to do after a task keeps failing. ``accept``/``edit`` apply the
  proposal ``{"action": "retry"|"fixup"|"manual"|"skip", ...}``; a select answer applies the one
  picked option. Retry resets the node to ready with the chosen model/effort; fixup adds
  ``<id>-fix<n>`` (optionally with ``deps``). No goal is ever changed.
- ``confirm``: a human done-check. ``accept`` by the owner sets the node done (actor ``owner``);
  ``reply`` keeps it open with the note as evidence.
- ``generic`` (default, and for rows written before purposes existed): recorded only.

Every side effect writes one plain-English summary event.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

from .store import now_iso
from . import config

KINDS = ("likert", "select", "text")
OPTION_KEYS = ("value", "label", "description")
DEFAULT_SCALE = 9  # canonical 1-9 Likert
ANSWER_TYPES = ("accept", "edit", "reply", "ignore", "answer")
NEEDS_PROPOSAL = ("accept", "edit")
VERB = {"accept": "accepted", "edit": "edited", "reply": "replied to", "ignore": "dismissed", "answer": "answered"}
PURPOSES = ("preference", "manual_result", "failure_choice", "confirm", "replan", "generic")
DEFAULT_PURPOSE = "generic"
NODE_PURPOSES = ("manual_result", "failure_choice", "confirm", "replan")  # about a node, so need one
FAILURE_ACTIONS = ("retry", "fixup", "manual", "skip")
GOAL_KEYS = ("goal", "goal_hash")
EVIDENCE_LIMIT = 20000

DRIVE_ROOT = Path(config.DRIVE_ROOT)
SKILL_DIR = DRIVE_ROOT / config.EXCHANGE_DIR / "07 Skills Registry" / "organising-preferences"
CAPTURE_DIR = DRIVE_ROOT / config.EXCHANGE_DIR / "05 Subagent Pipeline" / "Tasks"
FALLBACK_SCRIPT = Path(config.TOOLS_DIR) / "sync_preferences.py"
PINNED_PYTHON = Path(config.PYTHON)


def locate_capture_script(skill_dir=SKILL_DIR):
    """Find ``sync_preferences.py``: inside the skill folder first, then the path its SKILL.md names."""
    skill_dir = Path(skill_dir)
    for found in sorted(skill_dir.glob("**/sync_preferences.py")):
        return found
    try:
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    for m in re.finditer(r"['\"]([^'\"]*sync_preferences\.py)['\"]", text):
        p = Path(m.group(1))
        if p.exists():
            return p
    return FALLBACK_SCRIPT


def default_runner(argv):
    p = subprocess.run(argv, capture_output=True, text=True, timeout=180, encoding="utf-8", errors="replace")
    return p.returncode, p.stdout, p.stderr


def _options(kind, options):
    out = []
    for o in options or []:
        if isinstance(o, dict):
            if "value" not in o:
                raise ValueError("each option needs a value")
            item = {k: o[k] for k in OPTION_KEYS if k in o}  # drops selected/default/checked
            item.setdefault("label", str(item["value"]))
        else:
            item = {"value": o, "label": str(o)}
        out.append(item)
    values = [o["value"] for o in out]
    if len(set(map(json.dumps, values))) != len(values):
        raise ValueError("option values must be unique")
    if kind == "select" and not out:
        raise ValueError("a select question needs options")
    return out


def _ensure_schema(store):
    """Add the ``allowed_json`` / ``proposal_json`` / ``purpose`` columns to older databases (idempotent)."""
    if getattr(store, "_needs_schema_ok", False):
        return
    cols = {r["name"] if hasattr(r, "keys") else r[1] for r in store._q("PRAGMA table_info(questions)")}
    missing = [c for c in ("allowed_json", "proposal_json", "purpose") if c not in cols]
    if missing:
        with store.tx() as c:
            for col in missing:
                c.execute(f"ALTER TABLE questions ADD COLUMN {col} TEXT")
    store._needs_schema_ok = True


def default_allowed(kind, proposal=None, purpose=DEFAULT_PURPOSE):
    """Answer types offered when the asker does not say: the kind's own answer plus ignore,
    and accept/edit/reply as well when there is a proposal to act on. A ``confirm`` question
    offers accept (confirm done), reply (keep open with a note) and ignore."""
    base = {"ignore", "reply" if kind == "text" else "answer"}
    if proposal:
        base |= {"accept", "edit", "reply"}
    if purpose == "confirm":
        base |= {"accept", "reply"}
    return [t for t in ANSWER_TYPES if t in base]


def _allowed(kind, allowed, proposal, purpose=DEFAULT_PURPOSE):
    if allowed is None:
        return default_allowed(kind, proposal, purpose)
    if isinstance(allowed, str) or not isinstance(allowed, (list, tuple)) or not allowed:
        raise ValueError(f"allowed must be a non-empty list drawn from {ANSWER_TYPES}")
    bad = [t for t in allowed if t not in ANSWER_TYPES]
    if bad:
        raise ValueError(f"unknown answer types {bad}; use {list(ANSWER_TYPES)}")
    needs_proposal = [t for t in allowed if t in NEEDS_PROPOSAL and not (t == "accept" and purpose == "confirm")]
    if not proposal and needs_proposal:
        raise ValueError("accept and edit need a proposal to act on")
    return [t for t in ANSWER_TYPES if t in allowed]  # canonical order, duplicates dropped


def _check_failure_proposal(proposal):
    """A failure_choice proposal names one action and never touches a goal."""
    if proposal.get("action") not in FAILURE_ACTIONS:
        raise ValueError(f"a failure_choice proposal needs an action from {list(FAILURE_ACTIONS)}")
    touched = [k for k in GOAL_KEYS if k in proposal]
    if touched:
        raise ValueError(f"a failure_choice may never change a goal ({touched})")
    if "deps" in proposal and (not isinstance(proposal["deps"], list)
                               or not all(isinstance(d, str) for d in proposal["deps"])):
        raise ValueError("deps must be a list of task ids")


def _wrap(stored):
    """Older rows stored the bare value; present every answer as {type, value}."""
    if stored is None:
        return None
    if isinstance(stored, dict) and stored.get("type") in ANSWER_TYPES and set(stored) <= {"type", "value"}:
        return {"type": stored["type"], "value": stored.get("value")}
    return {"type": "answer", "value": stored}


def _row(r):
    d = dict(r)
    d["options"] = json.loads(d.pop("options_json") or "[]")
    proposal = d.pop("proposal_json", None)
    d["proposal"] = json.loads(proposal) if proposal else None
    d["purpose"] = d.get("purpose") if d.get("purpose") in PURPOSES else DEFAULT_PURPOSE
    allowed = d.pop("allowed_json", None)
    d["allowed"] = json.loads(allowed) if allowed else default_allowed(d.get("kind"), d["proposal"], d["purpose"])
    d["answer"] = _wrap(json.loads(d.pop("answer_json")) if d.get("answer_json") else None)
    d["captured"] = int(d.get("captured") or 0)
    return d


def ask(store, project_id=None, node_id=None, kind="text", prompt="", options=None, scale=None,
        allowed=None, proposal=None, purpose=None, **_ignored):
    """Queue a question for Owner. Returns the question id. Never bumps the revision.

    ``proposal`` is the agent's proposed action as an object of fields; ``accept`` approves it
    unchanged and ``edit`` changes its fields. ``allowed`` lists the answer types the UI may offer.
    ``purpose`` (one of ``PURPOSES``, default ``generic``) decides what the answer does.
    """
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("a question needs a prompt")
    if proposal is not None and (not isinstance(proposal, dict) or not proposal):
        raise ValueError("proposal must be a non-empty object of fields")
    purpose = DEFAULT_PURPOSE if purpose in (None, "") else purpose
    if purpose not in PURPOSES:
        raise ValueError(f"purpose must be one of {list(PURPOSES)}")
    if purpose in NODE_PURPOSES and not (project_id and node_id):
        raise ValueError(f"a {purpose} question must name its project and task")
    if purpose in ("manual_result", "confirm") and kind != "text":
        raise ValueError(f"a {purpose} question is a text question")
    if purpose == "failure_choice" and proposal is not None:
        _check_failure_proposal(proposal)
    allowed = _allowed(kind, allowed, proposal, purpose)
    if kind == "likert":
        scale = DEFAULT_SCALE if scale is None else scale
        if not isinstance(scale, int) or isinstance(scale, bool) or not 2 <= scale <= 11:
            raise ValueError("likert scale must be an integer 2-11")
    else:
        scale = None
    opts = _options(kind, options)
    _ensure_schema(store)
    qid = "q-" + secrets.token_hex(5)
    with store.tx() as c:
        c.execute("INSERT INTO questions(id, project_id, node_id, kind, prompt, options_json, scale, captured,"
                  " allowed_json, proposal_json, purpose) VALUES(?,?,?,?,?,?,?,0,?,?,?)",
                  (qid, project_id, node_id, kind, prompt, json.dumps(opts), scale, json.dumps(allowed),
                   json.dumps(proposal) if proposal is not None else None, purpose))
    q = get_question(store, qid)
    store.event(project_id, node_id, "question", "engine", f"A question is waiting for Owner: {prompt}",
                {"question": qid})
    store.notify("question", q)
    return qid


def get_question(store, question_id):
    _ensure_schema(store)
    rows = store._q("SELECT * FROM questions WHERE id=?", (question_id,))
    return _row(rows[0]) if rows else None


def list_questions(store, open_only=False, project_id=None):
    _ensure_schema(store)
    sql, params = "SELECT * FROM questions WHERE 1=1", []
    if open_only:
        sql += " AND answered_at IS NULL"
    if project_id:
        sql += " AND project_id=?"
        params.append(project_id)
    return [_row(r) for r in store._q(sql + " ORDER BY rowid", params)]


def _validate(q, answer):
    if q["kind"] == "likert":
        if isinstance(answer, bool) or not isinstance(answer, int) or not 1 <= answer <= q["scale"]:
            raise ValueError(f"answer must be an integer 1-{q['scale']}")
        return answer
    if q["kind"] == "select":
        if not isinstance(answer, list):
            raise ValueError("select answer must be a list of option values")
        allowed = [o["value"] for o in q["options"]]
        if any(a not in allowed for a in answer) or len(set(map(json.dumps, answer))) != len(answer):
            raise ValueError("select answer must be distinct option values")
        return answer
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("text answer must be non-empty")
    return answer


def _typed(q, answer):
    """Normalise ``answer`` to {type, value}. A bare value is the kind's own answer
    (``answer``; for a text question that only allows ``reply``, a reply)."""
    if isinstance(answer, dict) and "type" in answer:
        if set(answer) - {"type", "value"}:
            raise ValueError("a typed answer has only type and value")
        kind, value = answer["type"], answer.get("value")
    else:
        kind = "reply" if q["kind"] == "text" and "answer" not in q["allowed"] else "answer"
        value = answer
    if kind not in ANSWER_TYPES:
        raise ValueError(f"answer type must be one of {list(ANSWER_TYPES)}")
    if kind not in q["allowed"]:
        raise ValueError(f"this question does not accept '{kind}'; it allows {q['allowed']}")
    if kind in ("accept", "ignore"):
        if value not in (None, "", {}):
            raise ValueError(f"{kind} takes no value")
        return {"type": kind, "value": dict(q["proposal"]) if kind == "accept" and q["proposal"] else None}
    if kind == "edit":
        if not isinstance(value, dict) or not value:
            raise ValueError("edit needs an object of changed fields")
        unknown = [k for k in value if k not in q["proposal"]]
        if unknown:
            raise ValueError(f"edit can only change the proposal's fields; unknown {unknown}")
        return {"type": "edit", "value": dict(q["proposal"], **value)}
    if kind == "reply":
        if not isinstance(value, str) or not value.strip():
            raise ValueError("reply must be non-empty text")
        return {"type": "reply", "value": value}
    return {"type": "answer", "value": _validate(q, value)}


def _fields(obj) -> str:
    return "; ".join(f"{k} = {v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)}"
                     for k, v in (obj or {}).items())


def render_answer(q, answer) -> str:
    """Plain-English answer. ``answer`` is {type, value}; a bare value means type ``answer``."""
    typed = _wrap(answer) or {"type": "answer", "value": None}
    kind, value = typed["type"], typed["value"]
    if kind == "accept":
        return "accepted as proposed (" + _fields(value) + ")" if value else "confirmed"
    if kind == "edit":
        return "changed the proposal to " + _fields(value)
    if kind == "ignore":
        return "dismissed"
    if kind == "reply":
        return value
    if q["kind"] == "likert":
        return f"{value} of {q['scale']}"
    if q["kind"] == "select":
        labels = {json.dumps(o["value"]): o.get("label", str(o["value"])) for o in q["options"]}
        picked = [labels[json.dumps(a)] for a in value]
        return ", ".join(picked) if picked else "none of the options"
    return value


def _slug(text, limit=40):
    s = re.sub(r"[^a-z0-9_-]+", "-", str(text).lower()).strip("-_")
    return (s or "x")[:limit].strip("-_") or "x"


def capture_payload(q, answer, answered_at) -> dict:
    """Capture object in the organising-preferences format, quoting Owner's own answer."""
    typed = _wrap(answer)
    rendered = render_answer(q, typed)
    free_text = typed["type"] == "reply" or (typed["type"] == "answer" and q["kind"] == "text")
    quote = rendered if free_text else f"{q['prompt']} Answer: {rendered}"
    today = date.today()
    digest = hashlib.sha256(f"{q['id']}|{answered_at}|{json.dumps(typed)}".encode()).hexdigest()[:8]
    scope = f"project:{q['project_id']}" if q.get("project_id") else "global"
    where = f"project {q['project_id']}" if q.get("project_id") else "no project"
    if q.get("node_id"):
        where += f", task {q['node_id']}"
    return {
        "speaker": "user",
        "origin": "direct_user_dialogue",
        "source_id": f"user-{today:%Y%m%d}-cc-{_slug(q['id'], 20)}-{digest}",
        "source_locator": f"dsh Control Centre Needs-you question {q['id']} ({where}), answered {answered_at}",
        "stated_at": today.isoformat(),
        "quote": quote,
        "note_id": f"cc-answers-{_slug(q.get('project_id') or 'global', 60)}",
        "title": f"Control Centre answers for {q.get('project_id') or 'general work'}",
        "paragraphs": [
            f"Owner answered the Control Centre question '{q['prompt'].strip()}' with: {rendered}.",
            f"This answer is scoped to {scope} and should guide later work there until Owner changes it.",
        ],
        "tags": ["preferences", "control-centre"],
        "related": [],
        "scope": scope,
    }


def answer(store, question_id, answer, capture=True, runner=None, script=None, python=None, capture_dir=None,
           actor="owner", check_runner=None, settings=None, asker=None):
    """Record an answer as {type, value}, close the question and apply its purpose's effect.

    ``answer`` is ``{"type": ..., "value": ...}`` or a bare value (the kind's own answer).
    ``ignore`` closes the question with no side effect. Only ``preference`` answers are handed
    to ``sync_preferences.py --capture``, and only when ``capture`` is on and ``actor`` is
    ``owner``. Operational purposes act on their node (see the module docstring);
    ``check_runner`` feeds ``checks.complete_node``, ``settings`` / ``asker`` feed the failure
    policy (defaults: the project's active run settings and a needs-backed asker). The returned
    question carries ``effects``: the plain-English summary of each side effect.
    """
    q = get_question(store, question_id)
    if q is None:
        raise KeyError(f"no question {question_id}")
    if q.get("answered_at"):
        raise ValueError("this question is already closed")
    value = _typed(q, answer)
    effect = _plan_effect(store, q, value)  # validates before anything is written
    at = now_iso()
    with store.tx() as c:
        c.execute("UPDATE questions SET answer_json=?, answered_at=?, captured=0 WHERE id=?",
                  (json.dumps(value), at, question_id))
    should_capture = (bool(capture) and actor == "owner" and value["type"] != "ignore"
                      and q["purpose"] == "preference")
    captured = False
    if value["type"] == "ignore":
        detail = "dismissed, not captured"
    elif q["purpose"] != "preference":
        detail = f"{q['purpose']} answer, not a preference, not captured"
    elif actor != "owner":
        detail = "not an owner answer, not captured"
    else:
        detail = "capture off"
    if should_capture:
        captured, detail = _capture(q, value, at, runner, script, python, capture_dir)
        if captured:
            with store.tx() as c:
                c.execute("UPDATE questions SET captured=1 WHERE id=?", (question_id,))
    who = "Owner" if actor == "owner" else actor
    store.event(q["project_id"], q["node_id"], "answer", actor,
                f"{who} {VERB[value['type']]}: {q['prompt'].strip()}"
                + (" (capture failed)" if should_capture and not captured else ""),
                {"question": question_id, "type": value["type"], "purpose": q["purpose"], "captured": captured,
                 "detail": detail[:300]})
    effects = []
    if effect is not None:
        ctx = _Ctx(store, q, actor, check_runner, settings, asker)
        try:
            effect(ctx)
        except Exception as exc:  # the answer stays recorded; say plainly what did not happen
            ctx.note("answer_effect_failed",
                     f"{ctx.who}'s answer about task {q['node_id']} could not be applied: {exc}",
                     {"error": str(exc)[:500]})
        effects = ctx.effects
    out = get_question(store, question_id)
    out["effects"] = effects
    store.notify("question", out)
    return out


# ---------------------------------------------------------------- answer effects

class _Ctx:
    """What an effect needs, plus the list of summaries it produced (one event each)."""

    def __init__(self, store, q, actor, check_runner, settings, asker):
        self.store, self.q, self.actor = store, q, actor
        self.pid, self.nid = q["project_id"], q["node_id"]
        self.who = "Owner" if actor == "owner" else actor
        self.check_runner, self._settings, self._asker = check_runner, settings, asker
        self.effects = []

    def note(self, kind, summary, data=None):
        """An effect that is not a store mutation: write its one summary event."""
        self.store.event(self.pid, self.nid, kind, self.actor, summary,
                         dict(data or {}, question=self.q["id"]))
        self.effects.append(summary)

    def mutated(self, summary):
        """A store mutation already wrote its summary event; just report it."""
        self.effects.append(summary)

    def node(self):
        n = self.store.get_node(self.pid, self.nid)
        if n is None:
            raise KeyError(f"no task {self.pid}/{self.nid}")
        return n

    def settings(self):
        if self._settings is None:
            from . import scheduler as S
            runs = S.active_runs(self.store, self.pid)
            self._settings = S.expand_settings(S.run_settings(runs[-1]) if runs else {})
        return self._settings

    def asker(self):
        return self._asker if self._asker is not None else purpose_asker(self.store, "failure_choice")


def purpose_asker(store, purpose):
    """``asker(project_id, node_id, kind, prompt, options=None, **kw)`` that tags its questions
    with ``purpose`` (for dispatch/failure/scheduler callers)."""
    def asker(project_id=None, node_id=None, kind="text", prompt="", options=None, **kw):
        kw.setdefault("purpose", purpose)
        return ask(store, project_id, node_id, kind, prompt, options=options, **kw)

    return asker


def _plan_effect(store, q, value):
    """Validate the answer against its purpose and return ``effect(ctx)``, or None (record only)."""
    purpose, kind = q["purpose"], value["type"]
    if kind == "ignore" or purpose in ("generic", "preference"):
        return None
    if store.get_node(q["project_id"], q["node_id"]) is None:
        raise ValueError(f"task {q['node_id']} no longer exists, so this answer cannot act")
    if purpose == "manual_result":
        if kind in ("reply", "answer") and isinstance(value["value"], str):
            return lambda ctx: _apply_manual_result(ctx, value["value"])
        return None
    if purpose == "confirm":
        if kind == "accept":
            return _apply_confirm_done
        if kind == "reply":
            return lambda ctx: _apply_keep_open(ctx, value["value"])
        return None
    if purpose == "failure_choice":
        decision = _failure_decision(q, value)
        return None if decision is None else (lambda ctx: _apply_failure_choice(ctx, decision))
    return None


def _action_from_text(text):
    t = str(text).lower()
    for needle, action in (("fix-up", "fixup"), ("fix up", "fixup"), ("fixup", "fixup"), ("skip", "skip"),
                           ("manual", "manual"), ("by hand", "manual"), ("model", "retry"),
                           ("retry", "retry"), ("stronger", "retry"), ("again", "retry")):
        if needle in t:
            return action
    raise ValueError(f"cannot tell what option {text!r} should do; give the option a proposal value")


def _failure_decision(q, value):
    kind = value["type"]
    if kind in ("accept", "edit"):
        decision = dict(value["value"] or {})
    elif kind == "answer" and q["kind"] == "select":
        picked = value["value"]
        if len(picked) != 1:
            raise ValueError("pick exactly one option for what should happen next")
        opt = picked[0]
        chosen = dict(opt) if isinstance(opt, dict) else {"action": _action_from_text(opt)}
        base = dict(q.get("proposal") or {})
        decision = dict(base, **chosen) if base.get("action") in (None, chosen.get("action")) else chosen
    else:
        return None  # a reply or rating is recorded only
    _check_failure_proposal(decision)
    return decision


def _output_path(ctx, node):
    """The node's output file, if it defines exactly one; resolved against the project cwd."""
    q, check = ctx.q, node.get("check") or {}
    raw = (q.get("proposal") or {}).get("output_path") or node.get("output")
    if not raw and check.get("type") == "text_present":
        raw = check.get("path")
    if not raw and check.get("type") == "files_exist" and len(check.get("paths") or []) == 1:
        raw = check["paths"][0]
    if not raw:
        return None
    p = Path(raw)
    if not p.is_absolute():
        cwd = (ctx.store.get_project(ctx.pid) or {}).get("cwd") or "."
        p = Path(cwd) / p
    return p


def _apply_manual_result(ctx, text):
    from . import checks
    node = ctx.node()
    path = _output_path(ctx, node)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        ctx.note("manual_result", f"{ctx.who}'s pasted result for task {ctx.nid} was saved to {path.name}.",
                 {"path": str(path), "chars": len(text)})
    else:
        summary = f"{ctx.who}'s pasted result for task {ctx.nid} was stored as its evidence."
        ctx.store.update_node(ctx.pid, ctx.nid, {"evidence": text[:EVIDENCE_LIMIT]}, ctx.actor, summary)
        ctx.mutated(summary)
    if node["status"] == "done":
        return
    status = {n["id"]: n["status"] for n in ctx.store.list_nodes(ctx.pid)}
    waiting = [d for d in node.get("deps") or [] if status.get(d) != "done"]
    if waiting:
        ctx.note("manual_result", f"Task {ctx.nid} will be checked once {', '.join(waiting)} "
                                  f"{'is' if len(waiting) == 1 else 'are'} done.", {"waiting": waiting})
        return
    kind = (node.get("check") or {}).get("type") or "unknown"
    res = checks.complete_node(ctx.store, ctx.pid, ctx.nid, runner=ctx.check_runner)
    if res.ok:
        ctx.mutated(f"Task {ctx.nid} passed its {kind} check and is done.")
        return
    if kind == "human":  # complete_node left it waiting; ask the confirm question it needs
        ctx.note("manual_result", f"Task {ctx.nid} has its result and now waits for Owner to confirm it.")
        ask(ctx.store, ctx.pid, ctx.nid, "text", f"Is task {ctx.nid} done now that its result is in?",
            purpose="confirm")
        return
    if (ctx.store.get_node(ctx.pid, ctx.nid) or {}).get("status") == "needs_you":
        # W5 lane: the check passed but closed/wide work needs Owner; never a failure to retry
        ctx.note("manual_result", f"Task {ctx.nid} passed its {kind} check and now waits for Owner.")
        ask(ctx.store, ctx.pid, ctx.nid, "text", f"Task {ctx.nid} passed its check and needs your approval: "
                                                 f"{res.detail}", purpose="confirm")
        return
    ctx.mutated(f"Task {ctx.nid} failed its {kind} check.")
    from . import failure
    out = failure.on_check_failed(ctx.store, ctx.pid, ctx.node(), ctx.settings(), res.detail,
                                  asker=ctx.asker())
    if out.get("action") == "escalate":
        ctx.effects.append(f"Task {ctx.nid} will try again with {out.get('model')} at {out.get('effort')} effort.")
    else:
        ctx.effects.append(f"Task {ctx.nid} needs a decision on what to do next.")


def _apply_confirm_done(ctx):
    if ctx.actor != "owner":
        ctx.note("answer_effect", f"Task {ctx.nid} stays open: only Owner can confirm it is done.")
        return
    ctx.node()
    summary = f"Owner confirmed task {ctx.nid} is done."
    ctx.store.set_node_status(ctx.pid, ctx.nid, "done", "owner",
                              f"Confirmed by Owner (question {ctx.q['id']}).", summary)
    ctx.mutated(summary)


def _apply_keep_open(ctx, note):
    ctx.node()
    short = note.strip() if len(note.strip()) <= 160 else note.strip()[:157] + "..."
    summary = f"{ctx.who} kept task {ctx.nid} open: {short}"
    ctx.store.update_node(ctx.pid, ctx.nid, {"evidence": note[:EVIDENCE_LIMIT]}, ctx.actor, summary)
    ctx.mutated(summary)


def _apply_failure_choice(ctx, d):
    from . import failure
    node = ctx.node()
    action = d["action"]
    if action == "retry":
        model, effort = d.get("model"), d.get("effort")
        if not model:  # "try a stronger model": the top rung of this pool's ladder
            rungs = [r for r in failure.ladder(ctx.settings(), d.get("pool") or node.get("pool"),
                                               node.get("model")) if r[0]]
            model, top = rungs[-1] if rungs else (node.get("model"), node.get("effort"))
            if not effort:
                same = (model, top) == (node.get("model"), node.get("effort"))
                effort = failure._raise_effort(top) if same else top
        effort = effort or node.get("effort")
        fields = {"status": "not_started"}
        for k, v in (("model", model), ("effort", effort), ("pool", d.get("pool"))):
            if v and v != node.get(k):
                fields[k] = v
        how = " at ".join(x for x in (model, f"{effort} effort" if effort else None) if x)
        summary = f"Task {ctx.nid} will try again" + (f" with {how}" if how else "") + f", as {ctx.who} chose."
        ctx.store.update_node(ctx.pid, ctx.nid, fields, ctx.actor, summary)
        ctx.mutated(summary)
    elif action == "fixup":
        title = d.get("title") or f"Fix what blocks task {ctx.nid}"
        fid = failure.add_fixup(ctx.store, ctx.pid, ctx.nid, title, d.get("check") or node.get("check") or {},
                                pool=d.get("pool"), model=d.get("model"), effort=d.get("effort"), actor=ctx.actor)
        ctx.mutated(f"Added fix-up step {fid} ({title}) before task {ctx.nid}.")
        if "deps" in d:
            failure.reorder_deps(ctx.store, ctx.pid, fid, d["deps"], actor=ctx.actor)
            ctx.mutated(f"Fix-up step {fid} now waits for {', '.join(d['deps']) or 'nothing'}.")
    elif action == "manual":
        summary = f"Task {ctx.nid} will be done by hand next, as {ctx.who} chose; a Copy Prompt packet will be made."
        ctx.store.update_node(ctx.pid, ctx.nid, {"pool": "manual", "status": "not_started"}, ctx.actor, summary)
        ctx.mutated(summary)
    else:  # skip
        summary = f"{ctx.who} chose to skip task {ctx.nid} for now; the other tasks carry on."
        ctx.store.set_node_status(ctx.pid, ctx.nid, "error", ctx.actor, None, summary)
        ctx.mutated(summary)


def _capture(q, value, answered_at, runner, script, python, capture_dir):
    payload = capture_payload(q, value, answered_at)
    folder = Path(capture_dir) if capture_dir else CAPTURE_DIR
    if not folder.is_dir():
        folder = Path(tempfile.gettempdir())
    path = folder / f"cc_capture_{q['id']}_{secrets.token_hex(3)}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    py = str(python or (PINNED_PYTHON if PINNED_PYTHON.exists() else sys.executable))
    argv = [py, str(script or locate_capture_script()), "--capture", str(path)]
    try:
        code, out, err = (runner or default_runner)(argv)
    except Exception as exc:
        return False, f"capture runner failed: {exc}"
    if code == 0:
        try:
            path.unlink()
        except OSError:
            pass
        return True, (out or "").strip()
    return False, f"exit {code}: {(err or out or '').strip()}"
