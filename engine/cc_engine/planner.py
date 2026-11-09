"""Plan mode: turn "what do you want to make?" into a checked, drafted plan graph.

Flow (one planning *session* per new project):
1. ``POST /api/planner/start {title, goal, cwd?}``: a Claude Code job (strong model) writes at most
   ``MAX_QUESTIONS`` sharp discovery questions. Each becomes a needs-you question: purpose
   ``preference`` (Owner's general working preferences, captured to his notes when answered) or
   ``generic`` (specifics of this project). Options never carry a preselected recommendation.
2. Owner answers through ``POST /api/question/answer`` (Likert / select-all / text).
3. ``POST /api/planner/draft {session, note?}``: a second Claude Code job drafts the plan graph
   (the ``plans/build_plan.json`` shape, ``edges_v2``). Every node needs a done-check of type
   command / files_exist / text_present / human, a kind (splitter / worker / code / gate; a task that
   only moves data with a fixed check must be ``code``), and every edge names what ``crosses`` it.
   Accepted constraints from earlier work that bear on the goal go into the prompt
   (``constraints.relevant``). The result is validated with
   ``plan.validate_plan`` plus the planner's own rules, repaired once by the same job when it fails,
   and imported as a DRAFT project. Each task carries the planner's ``recommended`` model id
   (one of ``recommend_options()``; unknown ids become null). Sprints may still route elsewhere
   because they follow live usage; the recommendation is the planned first choice (``store.project_runnable`` stays false until Owner accepts it
   with ``POST /api/plan/accept``). A redraft archives the previous unaccepted draft and imports a
   fresh project id, so stale tasks never linger.
4. ``GET /api/planner/session?id=`` returns status, questions (with answers), the draft summary
   and validation problems. ``GET /api/planner/sessions`` lists recent sessions.

Claude always runs as a Claude Code job (``claude -p --output-format json``, Max subscription)
built by ``dispatch.Dispatcher.claude_argv`` with metered API keys stripped by
``dispatch.child_env``; never a metered API. Jobs run in a background thread; tests inject a fake
``runner(argv, cwd, stdin_text, timeout) -> (exit_code, stdout, stderr)`` and a synchronous spawner.
Prompts live in ``prompts/planner_*.md`` (editable). Sessions are kept in the ``meta`` table
(``planner:session:<id>``) and never bump the plan revision.
"""
from __future__ import annotations

import json
import re
import secrets
import shutil
import subprocess
import threading
from datetime import date
from pathlib import Path

from . import dispatch, needs, node_kinds, plan
from .api import HttpError
from .checks import CHECK_TYPES
from .store import now_iso

MAX_QUESTIONS = 6
MAX_NODES = 40
MIN_SELECT_OPTIONS, MAX_SELECT_OPTIONS = 2, 8
LIKERT_SCALE = needs.DEFAULT_SCALE  # canonical 1-9
PURPOSES = ("preference", "generic")
KINDS = ("likert", "select", "text")
# The strong model writes the brief, checks and graph (owner rule).
PLANNER_NODE = {"model": "opus", "effort": "high"}
# A planning job reads; it never edits files or runs commands.
CLAUDE_EXTRA_ARGS = ["--disallowedTools", "Edit,Write,NotebookEdit,Bash"]
QUESTION_TIMEOUT = 10 * 60
DRAFT_TIMEOUT = 20 * 60
REPAIR_ATTEMPTS = 1
PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
PREFERENCES_DIR = needs.DRIVE_ROOT / needs.config.EXCHANGE_DIR / "06 System & Governance" / "Preferences"
META_PREFIX = "planner:session:"
ACTOR = "planner"

BUSY = ("asking", "drafting")
STATUSES = ("asking", "questions", "drafting", "draft", "problems", "accepted", "error")
NODE_FIELDS = ("id", "title", "goal", "check", "deps", "owner", "lane", "recommended", "kind", "script",
               "risk", "scope")
# Models the planner may recommend when the models catalogue (``models.py``) is unavailable.
# Codex CLI is never a planned or automatic route, so it is never offered here.
FALLBACK_MODELS = (
    {"id": "claude-opus", "label": "Claude Code, Opus", "note": "hardest design, integration and review work"},
    {"id": "claude-sonnet", "label": "Claude Code, Sonnet", "note": "everyday coding with tests"},
    {"id": "claude-haiku", "label": "Claude Code, Haiku", "note": "small mechanical edits"},
    {"id": "freebuff", "label": "Freebuff (free)", "note": "simple text-only tasks"},
    {"id": "gemini-app", "label": "Gemini App (Owner pastes the prompt)", "note": "research and long reading"},
    {"id": "chatgpt-web", "label": "ChatGPT Web (Owner pastes the prompt)", "note": "bounded code or text tasks"},
    {"id": "antigravity", "label": "Antigravity (Owner drives it)", "note": "agentic coding Owner runs himself"},
)
RECOMMEND = re.compile(r"\s*[\(\[]\s*(recommended|default|suggested)\s*[\)\]]\s*", re.I)

# Injection points (tests replace these; api.App may also carry ``planner_runner``).
RUNNER = None
SPAWN = None

_lock = threading.RLock()
_live: set = set()


# ---------------------------------------------------------------- Claude Code job
def default_runner(argv, cwd, stdin_text, timeout):
    """Run one ``claude -p`` job; returns (exit_code, stdout, stderr). Never metered API."""
    try:
        proc = subprocess.run(argv, cwd=cwd or None, input=stdin_text, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=dispatch.child_env(), timeout=timeout)
    except FileNotFoundError:
        return 127, "", "Claude Code (the claude command) was not found on this computer."
    except subprocess.TimeoutExpired:
        return 124, "", f"The planning job took longer than {timeout // 60} minutes."
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def claude_argv(model=None, effort=None, extra_args=None, add_dirs=()):
    """The ``claude -p`` argv for a planning job. With no arguments: the strong planner model,
    read-only tools and Owner's preference notes. ``deep_plan`` passes its chosen model, effort,
    tool rules and extra read-only folders."""
    exe = shutil.which("claude") or "claude"
    extra = list(CLAUDE_EXTRA_ARGS if extra_args is None else extra_args)
    dirs = [PREFERENCES_DIR] if PREFERENCES_DIR.is_dir() else []
    for d in add_dirs or ():
        if d and Path(d).is_dir() and Path(d) not in dirs:
            dirs.append(Path(d))
    for d in dirs:
        extra += ["--add-dir", str(d)]
    node = {"model": model or PLANNER_NODE["model"], "effort": effort or PLANNER_NODE["effort"]}
    return dispatch.Dispatcher(claude_exe=exe).claude_argv(node, {"claude_extra_args": extra})


def _runner_for(app=None):
    return RUNNER or getattr(app, "planner_runner", None) or default_runner


def _spawn(fn):
    if SPAWN is not None:
        return SPAWN(fn)
    threading.Thread(target=fn, daemon=True, name="cc-planner").start()
    return None


def parse_reply(stdout: str) -> dict:
    """The JSON object a planning job returned. ``--output-format json`` wraps the model's text
    as ``{"result": "...", "is_error": bool}``; the text may carry a code fence."""
    text = (stdout or "").strip()
    try:
        outer = json.loads(text)
    except ValueError:
        outer = None
    if isinstance(outer, dict) and "result" in outer:
        if outer.get("is_error"):
            raise ValueError("Claude Code reported an error: " + str(outer.get("result"))[:300])
        text = str(outer.get("result") or "")
    elif isinstance(outer, dict):
        return outer
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("the planning job did not return a JSON object")
    try:
        value = json.loads(text[start:end + 1])
    except ValueError as exc:
        raise ValueError(f"the planning job returned malformed JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("the planning job did not return a JSON object")
    return value


def run_claude(prompt: str, cwd, timeout, app=None) -> dict:
    runner = _runner_for(app)
    workdir = cwd if cwd and Path(cwd).is_dir() else None
    code, out, err = runner(claude_argv(), workdir, prompt, timeout)
    if int(code) != 0:
        detail = (err or out or "").strip().splitlines()
        raise RuntimeError(f"the planning job stopped (exit {code})" + (f": {detail[-1][:300]}" if detail else ""))
    return parse_reply(out)


def render_prompt(name: str, values: dict, prefix: str = "planner") -> str:
    text = (PROMPT_DIR / f"{prefix}_{name}.md").read_text(encoding="utf-8")
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", str(v))
    return text


# ---------------------------------------------------------------- sessions
def _key(sid):
    return META_PREFIX + sid


def load_session(store, sid):
    raw = store.get_meta(_key(sid)) if sid else None
    return json.loads(raw) if raw else None


def save_session(store, sess):
    sess["updated_at"] = now_iso()
    with _lock:
        store.set_meta(_key(sess["id"]), json.dumps(sess, ensure_ascii=False))
    try:
        store.notify("planner", {"id": sess["id"], "status": sess["status"]})
    except Exception:
        pass
    return sess


def _update(store, sid, **fields):
    with _lock:
        sess = load_session(store, sid)
        sess.update(fields)
        return save_session(store, sess)


def list_sessions(store, limit=20):
    rows = store._q("SELECT value FROM meta WHERE key LIKE ?", (META_PREFIX + "%",))
    out = [json.loads(r[0]) for r in rows]
    out.sort(key=lambda s: s.get("created_at") or "", reverse=True)
    return out[:limit]


def slug(text, limit=32):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s[:limit].strip("-") or "project")


def _clean(text):
    return RECOMMEND.sub(" ", str(text or "")).strip()


# ---------------------------------------------------------------- recommended models
def _models_module():
    try:
        from . import models  # worker A's catalogue; optional
    except Exception:
        return None
    return models


def _is_codex(m) -> bool:
    text = " ".join(str(m.get(k) or "") for k in ("id", "route", "label")).lower()
    return "codex" in text


def recommend_options() -> list:
    """``[{id, label, note}]`` the planner may recommend: enabled catalogue rows that name a task
    route (Claude Code, Freebuff, Antigravity, Gemini App, ChatGPT Web; never Codex, never a dsh chat
    picker model), or ``FALLBACK_MODELS`` when the catalogue is missing or empty."""
    mod = _models_module()
    rows = None
    for name in ("build_registry", "list_models", "catalogue", "catalog", "load_models"):
        fn = getattr(mod, name, None) if mod is not None else None
        if callable(fn):
            try:
                rows = fn()
            except Exception:
                rows = None
            break
    if rows is None and mod is not None and isinstance(getattr(mod, "MODELS", None), (list, tuple)):
        rows = mod.MODELS
    if isinstance(rows, dict):
        rows = rows.get("models")
    out = []
    for m in rows if isinstance(rows, (list, tuple)) else []:
        if not isinstance(m, dict) or not m.get("id") or m.get("enabled") is False or _is_codex(m):
            continue
        if m.get("kind") == "chat" or not m.get("route") or m.get("route") == "manual":
            continue  # chat picker models and the generic manual route are not task routes
        note = m.get("note") or m.get("badge") or " · ".join(str(x) for x in (m.get("provider"), m.get("kind")) if x)
        out.append({"id": str(m["id"]), "label": str(m.get("label") or m["id"]), "note": note})
    return out or [dict(m) for m in FALLBACK_MODELS]


def _models_text(options) -> str:
    return "\n".join(f"  - {m['id']}: {m['label']}" + (f" ({m['note']})" if m.get("note") else "") for m in options)


# ---------------------------------------------------------------- learned constraints
def constraints_text(store, text, limit=8) -> str:
    """Accepted constraints from earlier work that share keywords with ``text`` (for prompts)."""
    try:
        from . import constraints
        lines = [c["line"] for c in constraints.relevant(store, text, limit=limit)]
    except Exception:  # the learning edge is optional; planning never fails on it
        lines = []
    return "\n".join(f"- {x}" for x in lines) or "(none yet)"


# ---------------------------------------------------------------- questions
def normalise_questions(raw) -> list:
    """Keep at most MAX_QUESTIONS well-formed questions; drop anything that would preselect."""
    items = raw.get("questions") if isinstance(raw, dict) else raw
    out = []
    for q in items if isinstance(items, list) else []:
        if not isinstance(q, dict):
            continue
        kind, prompt = q.get("kind"), _clean(q.get("prompt"))
        if kind not in KINDS or not prompt:
            continue
        item = {"kind": kind, "prompt": prompt,
                "purpose": q.get("purpose") if q.get("purpose") in PURPOSES else "generic",
                "why": _clean(q.get("why"))[:200] or None}
        if kind == "likert":
            labels = [_clean(x) for x in (q.get("labels") or []) if _clean(x)]
            item["labels"] = labels[:1] + labels[-1:] if len(labels) >= 2 else ["Not at all", "Very much"]
            item["scale"] = LIKERT_SCALE
        elif kind == "select":
            opts, seen = [], set()
            for o in q.get("options") or []:
                label = _clean(o.get("label") or o.get("value") if isinstance(o, dict) else o)
                if label and label.lower() not in seen:
                    seen.add(label.lower())
                    opts.append(label)
            if len(opts) < MIN_SELECT_OPTIONS:
                continue
            item["options"] = opts[:MAX_SELECT_OPTIONS]
        out.append(item)
        if len(out) >= MAX_QUESTIONS:
            break
    return out


def _ask_questions(store, sid, app=None):
    sess = load_session(store, sid)
    try:
        prompt = render_prompt("questions", {
            "title": sess["title"], "goal": sess["goal"], "cwd": sess.get("cwd") or "(not set)",
            "preferences": PREFERENCES_DIR, "max_questions": MAX_QUESTIONS})
        items = normalise_questions(run_claude(prompt, sess.get("cwd"), QUESTION_TIMEOUT, app))
        stored = []
        for item in items:
            options = item.get("options") or []
            qid = needs.ask(store, None, None, item["kind"], item["prompt"], options=options,
                            scale=item.get("scale"), purpose=item["purpose"])
            stored.append(dict(item, id=qid))
        _update(store, sid, status="questions", questions=stored, error=None)
        store.event(None, None, "planner", ACTOR,
                    f"The planner asked {len(stored)} question{'s' if len(stored) != 1 else ''} about '{sess['title']}'.",
                    {"session": sid})
    except Exception as exc:  # recorded on the session; never raised into the server thread
        _update(store, sid, status="error", error=_plain(exc), phase="questions")
    finally:
        _live.discard(sid)


def start(store, title, goal, cwd=None, actor=ACTOR, app=None) -> dict:
    title, goal = (title or "").strip(), (goal or "").strip()
    if not title or not goal:
        raise ValueError("a plan needs a title and a goal")
    if len(title) > 120 or len(goal) > 4000:
        raise ValueError("keep the title under 120 and the goal under 4000 characters")
    cwd = (cwd or "").strip() or None
    sid = "ps-" + secrets.token_hex(4)
    sess = {"id": sid, "title": title, "goal": goal, "cwd": cwd, "status": "asking", "phase": "questions",
            "project_base": f"{slug(title)}-{secrets.token_hex(2)}", "project": None, "drafts": 0,
            "previous": [], "questions": [], "draft": None, "problems": [], "error": None,
            "requested_by": actor, "created_at": now_iso()}
    _live.add(sid)
    save_session(store, sess)
    _spawn(lambda: _ask_questions(store, sid, app))
    return session_view(store, sid)


# ---------------------------------------------------------------- draft
def _answer_lines(store, sess) -> str:
    lines = []
    for item in sess.get("questions") or []:
        q = needs.get_question(store, item["id"]) or {}
        ans = q.get("answer")
        if not ans or ans.get("type") == "ignore":
            text = "(skipped)"
        else:
            text = needs.render_answer(q, ans)
            if item["kind"] == "likert" and item.get("labels"):
                text += f" on 1-{item.get('scale', LIKERT_SCALE)} ({item['labels'][0]} to {item['labels'][-1]})"
        lines.append(f"- [{item['purpose']}] {item['prompt']}\n  Answer: {text}")
    return "\n".join(lines) or "(no questions were needed)"


def human_gate_problems(nodes) -> list:
    """W8: Owner approves one step (final ship) plus any closed-risk node; more human gates are waits."""
    extra = [n.get("id") for n in nodes or [] if isinstance(n, dict)
             and isinstance(n.get("check"), dict) and n["check"].get("type") == "human"
             and n.get("risk") != "closed"]
    if len(extra) > 1:
        return [f"human gates on {', '.join(map(str, extra))}: keep one human gate (the final ship step); "
                "use a program check elsewhere or mark the node risk 'closed'"]
    return []


def default_template(title="Project", brief="", cwd=None) -> dict:
    """A plan skeleton with exactly one human gate: the final ship step."""
    return {"id": slug(title) or "project", "title": title, "brief": brief, "cwd": cwd, "edges_v2": True,
            "nodes": [
                {"id": "S1", "title": "Split the work", "kind": "splitter", "owner": ACTOR,
                 "goal": "Decide how the work divides into bounded tasks.",
                 "check": {"type": "files_exist", "paths": ["plan.md"]}, "deps": []},
                {"id": "W1", "title": "Do the work", "kind": "worker", "owner": ACTOR,
                 "goal": "Implement the tasks the split names.",
                 "check": {"type": "command", "run": "exit 0"}, "deps": [{"id": "S1", "crosses": "plan.md"}]},
                {"id": "G1", "title": "Ship", "kind": "gate", "owner": ACTOR,
                 "goal": "Owner approves the final result.",
                 "check": {"type": "human", "prompt": "Ship it?"}, "deps": [{"id": "W1", "crosses": "the finished work"}]}]}


def with_ship_gate(nodes) -> list:
    """W8 at import: a draft with no human gate gets exactly one final ship gate (as in
    ``default_template``) after every sink task, so ``human_gate_problems`` still holds."""
    from . import loops  # lazy: loops imports the scheduler
    nodes = list(nodes or [])
    if not nodes or any(isinstance(n.get("check"), dict) and n["check"].get("type") == "human"
                        and n.get("risk") != "closed" for n in nodes):
        return nodes
    used = {node_kinds.dep_id(d) for n in nodes for d in n.get("deps") or []}
    sinks = [n["id"] for n in nodes if n.get("id") not in used]
    ids = {n.get("id") for n in nodes}
    gid = next(g for g in ("G1", "SHIP", "SHIP1", "SHIP2", "SHIP3") + tuple(f"G{i}" for i in range(2, 999))
               if g not in ids)
    return nodes + [{"id": gid, "title": "Ship", "kind": "gate", "owner": ACTOR, "risk": loops.DEFAULT_RISK,
                     "goal": "Owner approves the final result.",
                     "check": {"type": "human", "prompt": "Ship it?"},
                     "deps": [{"id": s, "crosses": "the finished work"} for s in sinks]}]


def problems(raw) -> list:
    """Every reason this drafted plan cannot be imported (empty when it is valid)."""
    out = []
    if not isinstance(raw, dict):
        return ["the draft is not a JSON object"]
    if not str(raw.get("brief") or "").strip():
        out.append("the plan needs a brief")
    nodes = raw.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return out + ["the plan has no tasks"]
    if len(nodes) > MAX_NODES:
        out.append(f"the plan has {len(nodes)} tasks; keep it to {MAX_NODES}")
    ids = [n.get("id") if isinstance(n, dict) else None for n in nodes]
    dupes = sorted({str(i) for i in ids if i and ids.count(i) > 1})
    if dupes:
        out.append("task ids must be unique: " + ", ".join(dupes))
    known = {i for i in ids if isinstance(i, str) and i}
    for pos, n in enumerate(nodes):
        if not isinstance(n, dict):
            out.append(f"task {pos + 1} is not an object")
            continue
        nid = n.get("id")
        if not isinstance(nid, str) or not nid.strip():
            out.append(f"task {pos + 1} needs an id")
            continue
        if not isinstance(n.get("goal"), str) or not n["goal"].strip():
            out.append(f"task {nid} needs goal text")
        c = n.get("check")
        if not isinstance(c, dict) or c.get("type") not in CHECK_TYPES:
            out.append(f"task {nid} needs a done-check of type {'/'.join(CHECK_TYPES)}")
        elif c["type"] == "command" and not str(c.get("run") or "").strip():
            out.append(f"task {nid}: a command check needs 'run'")
        elif c["type"] == "files_exist" and not (isinstance(c.get("paths"), list) and c["paths"]):
            out.append(f"task {nid}: a files_exist check needs 'paths'")
        elif c["type"] == "text_present" and not (c.get("path") and c.get("text")):
            out.append(f"task {nid}: a text_present check needs 'path' and 'text'")
        elif c["type"] == "human" and not str(c.get("prompt") or "").strip():
            out.append(f"task {nid}: a human check needs a 'prompt' saying what Owner confirms")
        out += node_kinds.kind_problems(n)
        if isinstance(c, dict) and c.get("type") in CHECK_TYPES:
            out += node_kinds.lint_node(n)
        deps = n.get("deps") or []
        if not isinstance(deps, list):
            out.append(f"task {nid}: deps must be a list")
            continue
        ids = [node_kinds.dep_id(d) for d in deps]
        if any(not isinstance(i, str) or not i for i in ids):
            out.append(f"task {nid}: each dependency needs an id")
            continue
        unknown = [d for d in ids if d not in known]
        if unknown:
            out.append(f"task {nid} depends on unknown {unknown}")
        if nid in ids:
            out.append(f"task {nid} depends on itself")
        out += node_kinds.edge_problems(n)
    out += human_gate_problems(nodes)
    if not out:  # the engine's own validator has the final word (cycles included)
        try:
            plan.validate_plan(dict(raw, id=raw.get("id") or "draft"))
        except plan.PlanError as exc:
            out.append(str(exc))
    return out


def build_plan(sess, raw, project_id, allowed_models=None) -> dict:
    from . import loops  # lazy: loops imports the scheduler
    allowed = {m["id"] for m in (allowed_models if allowed_models is not None else recommend_options())}
    nodes = []
    for n in raw.get("nodes") or []:
        item = {k: n[k] for k in NODE_FIELDS if isinstance(n, dict) and n.get(k) is not None}
        rec = item.get("recommended")
        if not isinstance(rec, str) or rec.strip() not in allowed:
            item.pop("recommended", None)  # unknown or Codex: no recommendation rather than a wrong one
        else:
            item["recommended"] = rec.strip()
        ids, crosses = node_kinds.split_deps(item.get("deps"))
        item["deps"] = node_kinds.join_deps(ids, crosses)
        if item.get("kind") not in node_kinds.KINDS:
            item.pop("kind", None)
        item.setdefault("title", item.get("id"))
        item.setdefault("owner", ACTOR)
        if item.get("risk") not in loops.RISKS:
            item["risk"] = loops.DEFAULT_RISK  # LIVE2: drafts arrive with risk null
        nodes.append(item)
    nodes = with_ship_gate(nodes)
    return {"id": project_id, "title": sess["title"], "brief": str(raw.get("brief") or "").strip(),
            "cwd": sess.get("cwd"), "created": date.today().isoformat(), "edges_v2": True, "nodes": nodes}


def summarise(p) -> dict:
    counts = {t: 0 for t in CHECK_TYPES}
    for n in p["nodes"]:
        counts[n["check"]["type"]] = counts.get(n["check"]["type"], 0) + 1
    return {"project": p["id"], "title": p["title"], "brief": p["brief"], "tasks": len(p["nodes"]),
            "checks": counts, "nodes": [{"id": n["id"], "title": n.get("title") or n["id"], "goal": n["goal"],
                                         "check": n["check"], "deps": node_kinds.split_deps(n["deps"])[0],
                                         "crosses": node_kinds.split_deps(n["deps"])[1],
                                         "kind": node_kinds.kind_of(n), "lane": n.get("lane"),
                                         "recommended": n.get("recommended")}
                                        for n in p["nodes"]]}


def _archive_draft(store, project_id):
    if not project_id or store.project_status(project_id) != "draft":
        return

    def fn(c):
        c.execute("UPDATE projects SET archived=1 WHERE id=? AND status='draft'", (project_id,))

    store.mutate(None, fn, ACTOR, "The planner set an earlier draft aside for a redraft.",
                 project_id=project_id, kind="planner")


def _draft(store, sid, note=None, app=None):
    sess = load_session(store, sid)
    try:
        options = recommend_options()
        values = {"title": sess["title"], "goal": sess["goal"], "cwd": sess.get("cwd") or "(not set)",
                  "answers": _answer_lines(store, sess), "max_nodes": MAX_NODES, "models": _models_text(options),
                  "note": f"\nWhat Owner wants changed from the last draft: {note.strip()}\n" if note else "",
                  "problems": "", "constraints": constraints_text(store, f"{sess['title']} {sess['goal']}")}
        raw = run_claude(render_prompt("draft", values), sess.get("cwd"), DRAFT_TIMEOUT, app)
        found = problems(raw)
        for _ in range(REPAIR_ATTEMPTS):
            if not found:
                break
            values["problems"] = ("\nYour previous draft could not be imported. Fix every one of these:\n"
                                  + "\n".join(f"- {p}" for p in found) + "\nPrevious draft:\n"
                                  + json.dumps(raw, ensure_ascii=False)[:20000] + "\n")
            raw = run_claude(render_prompt("draft", values), sess.get("cwd"), DRAFT_TIMEOUT, app)
            found = problems(raw)
        if found:
            _update(store, sid, status="problems", problems=found, error=None, phase="draft")
            return
        n = int(sess.get("drafts") or 0) + 1
        pid = sess["project_base"] + ("" if n == 1 else f"-r{n}")
        built = build_plan(sess, raw, pid, allowed_models=options)
        old = sess.get("project")
        plan.import_plan(store, built, actor=ACTOR, draft=True)
        _archive_draft(store, old)
        previous = list(sess.get("previous") or []) + ([old] if old else [])
        _update(store, sid, status="draft", project=pid, drafts=n, previous=previous, problems=[],
                draft=summarise(built), error=None, phase="draft")
    except Exception as exc:
        _update(store, sid, status="error", error=_plain(exc), phase="draft")
    finally:
        _live.discard(sid)


def draft(store, sid, note=None, app=None) -> dict:
    with _lock:
        sess = load_session(store, sid)
        if sess is None:
            raise KeyError(f"no planning session {sid}")
        status = session_status(store, sess)
        if status in BUSY:
            raise HttpError(409, {"error": "busy", "detail": "the planner is still working on this session"})
        if status == "accepted":
            raise HttpError(409, {"error": "accepted", "detail": "this plan is accepted; start a new plan instead"})
        if status == "error" and sess.get("phase") == "questions":
            raise HttpError(409, {"error": "no_questions", "detail": "the questions never arrived; start planning again"})
        sess.update(status="drafting", phase="draft", error=None)
        _live.add(sid)
        save_session(store, sess)
    _spawn(lambda: _draft(store, sid, note=(note or "").strip()[:2000] or None, app=app))
    return session_view(store, sid)


# ---------------------------------------------------------------- views
def _plain(exc) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text[0].upper() + text[1:] if text else text


def session_status(store, sess) -> str:
    status = sess.get("status")
    if status in BUSY and sess["id"] not in _live:
        # the engine restarted mid-job: say so instead of spinning forever
        status = "error"
        sess.update(status="error", error="The engine restarted while planning; try again.")
        save_session(store, sess)
    if status == "draft" and sess.get("project") and store.project_status(sess["project"]) == "active":
        return "accepted"
    return status


def session_view(store, sid) -> dict:
    sess = load_session(store, sid)
    if sess is None:
        raise KeyError(f"no planning session {sid}")
    status = session_status(store, sess)
    questions = []
    for item in sess.get("questions") or []:
        q = needs.get_question(store, item["id"]) or {}
        questions.append(dict(item, answer=q.get("answer"), answered_at=q.get("answered_at"),
                              allowed=q.get("allowed")))
    answered = sum(1 for q in questions if q.get("answered_at"))
    return {"id": sess["id"], "status": status, "title": sess["title"], "goal": sess["goal"],
            "cwd": sess.get("cwd"), "phase": sess.get("phase"), "project": sess.get("project"),
            "runnable": bool(sess.get("project")) and store.project_runnable(sess["project"]),
            "questions": questions, "answered": answered, "draft": sess.get("draft"),
            "problems": sess.get("problems") or [], "error": sess.get("error"),
            "drafts": sess.get("drafts") or 0, "created_at": sess.get("created_at"),
            "updated_at": sess.get("updated_at")}


# ---------------------------------------------------------------- routes
def p_start(h, q):
    b = h._json_body()
    for k in ("title", "goal"):
        if not str(b.get(k) or "").strip():
            raise HttpError(400, {"error": "bad_request", "detail": f"missing {k}"})
    cwd = b.get("cwd")
    if cwd and not Path(str(cwd)).is_dir():
        raise HttpError(400, {"error": "bad_request", "detail": "that folder does not exist"})
    return {"ok": True, "session": start(h.app.store, b["title"], b["goal"], cwd,
                                         actor=getattr(h, "actor", ACTOR), app=h.app)}


def p_draft(h, q):
    b = h._json_body()
    sid = b.get("session") or b.get("id")
    if not sid:
        raise HttpError(400, {"error": "bad_request", "detail": "missing session"})
    return {"ok": True, "session": draft(h.app.store, sid, note=b.get("note"), app=h.app)}


def g_session(h, q):
    sid = q.get("id") or q.get("session")
    if not sid:
        raise HttpError(400, {"error": "bad_request", "detail": "missing id"})
    return session_view(h.app.store, sid)


def g_sessions(h, q):
    store = h.app.store
    out = []
    for s in list_sessions(store):
        out.append({"id": s["id"], "title": s["title"], "status": session_status(store, s),
                    "project": s.get("project"), "created_at": s.get("created_at")})
    return {"sessions": out}


ROUTES = {
    "POST /api/planner/start": p_start,
    "POST /api/planner/draft": p_draft,
    "GET /api/planner/session": g_session,
    "GET /api/planner/sessions": g_sessions,
}
