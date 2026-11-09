"""Deep plan mode: planning as a staged job that researches, decides a framework, compares graphs
and writes each task's brief before Owner sees anything.

Stages (``STAGES``), each streamed as one line through ``store.notify("deep_plan", ...)``:
1. ``brief``: the planner model sharpens the goal into a brief and names 2-4 research topics
   (where: code, drive or web).
2. ``research``: 2-4 read-only research jobs run in parallel (``claude -p`` with edit tools
   blocked, or the free runner when chosen). Each returns findings, sources and unknowns.
3. ``questions``: only what research could not find becomes a question for Owner: 1-9 Likert or
   tick-box, nothing preselected (``planner.normalise_questions``). No unknowns, no question call.
   With questions the job waits; ``POST /api/plan/deep/continue`` resumes it.
4. ``framework``: a decomposition, work streams with the files each owns, and the contracts
   between them.
5. ``graph``: up to ``MAX_CANDIDATES`` drafted graphs. Each is validated (``planner.problems``),
   scored and repaired by ``plan_metrics`` (no two parallel tasks write one file); later drafts are
   told how earlier ones scored. The best by ``plan_metrics.rank_key`` is kept. Every task has a
   kind (a script-only step is ``code``) and every edge names what ``crosses`` it; an edge the
   repair adds names the shared files.
6. ``prompts``: every task gets a bounded brief: inputs, owned files, hand-offs, done-check and
   exit criteria. Owned files and the check always come from the graph itself. Accepted
   constraints from earlier work (``constraints``) that share keywords with a task, or come from
   its upstream tasks, go into its brief as "Rules learned"; ``task_brief`` adds ones accepted
   after the plan was drafted.
7. ``critique``: a second model reviews the plan; ``revise`` triggers one revision by the planner
   model, kept only when it is valid.
Then the plan is imported as a DRAFT project (status ``ready``); Owner accepts it with the
existing ``POST /api/plan/accept``. Task briefs: ``task_brief(store, project, id)``.

Model and effort are chosen per plan (``options()``): Claude Opus, Sonnet or Fable through
``claude --model``, or the free Freebuff runner; the default comes from worker C's
``GET /api/pick?kind=plan`` when that module exists. All model calls go through one injectable
``runner(argv, cwd, stdin_text, timeout) -> (code, stdout, stderr)`` (``RUNNER``, then
``app.deep_plan_runner``, then the planner's runner). Sessions live in ``meta``
(``deep_plan:session:<id>``) and never bump the plan revision; only the final import does.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import needs, node_kinds, plan, plan_metrics, planner, providers
from .api import HttpError, _owner_only
from .store import now_iso

STAGES = ("brief", "research", "questions", "framework", "graph", "prompts", "critique")
STAGE_LABELS = {"brief": "Brief", "research": "Research", "questions": "Questions", "framework": "Framework",
                "graph": "Graph", "prompts": "Prompts", "critique": "Critique"}
MODEL_OPTIONS = (
    {"id": "claude-opus", "label": "Claude Opus", "route": "claude_code", "model": "opus", "free": False},
    {"id": "claude-sonnet", "label": "Claude Sonnet", "route": "claude_code", "model": "sonnet", "free": False},
    {"id": "claude-fable", "label": "Claude Fable", "route": "claude_code", "model": "fable", "free": False},
    {"id": "freebuff", "label": "Freebuff (free)", "route": "freebuff", "model": providers.FREEBUFF_DEFAULT_MODEL,
     "free": True},
)
EFFORTS = list(providers.CLAUDE_EFFORTS)
DEFAULT = {"model": "claude-opus", "effort": "high"}
CRITIC_FOR = {"claude-opus": "claude-sonnet", "claude-sonnet": "claude-opus", "claude-fable": "claude-opus",
              "freebuff": "claude-sonnet"}
RESEARCH_DEFAULT = {"model": "claude-sonnet", "effort": "medium"}
MIN_TOPICS, MAX_TOPICS = 2, 4
WHERE = ("code", "drive", "web")
MAX_CANDIDATES = 3
MAX_BRIEF_CHARS = 3500
MAX_QUESTIONS = planner.MAX_QUESTIONS
TIMEOUTS = {"brief": 10 * 60, "research": 15 * 60, "questions": 10 * 60, "framework": 15 * 60,
            "graph": 20 * 60, "prompts": 20 * 60, "critique": 15 * 60, "revise": 20 * 60}
# Research reads only: no edits, no shell. Web and file reading stay allowed.
RESEARCH_TOOLS = ["--disallowedTools", "Edit,Write,NotebookEdit,Bash",
                  "--allowedTools", "Read,Glob,Grep,WebSearch,WebFetch"]
DRIVE_DIR = needs.DRIVE_ROOT / needs.config.EXCHANGE_DIR
META_PREFIX = "deep_plan:session:"
BRIEFS_PREFIX = "deep_plan:briefs:"
ACTOR = "planner"
RULES_HEADING = "Rules learned (accepted earlier; keep them true):"
TREE_SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", "data", ".pytest_cache", "dist", "build"}

# Injection points (tests replace these).
RUNNER = None
SPAWN = None
PICK = None       # callable(app) -> {"model", "effort", "why"} | None; default asks pick.py
DISABLED = None   # callable(option) -> bool; default asks models.is_disabled

_lock = threading.RLock()
_live: set = set()


# ---------------------------------------------------------------- options
def option(mid):
    for m in MODEL_OPTIONS:
        if m["id"] == mid:
            return dict(m)
    return None


def _disabled(opt) -> bool:
    if DISABLED is not None:
        return bool(DISABLED(opt))
    try:
        from . import models
        return bool(models.is_disabled(route=opt["route"], model=opt["model"]))
    except Exception:
        return False


def _match(model_id):
    """A model option for an id from pick.py or the UI ("sonnet", "claude-opus", "freebuff/...")."""
    if not isinstance(model_id, str) or not model_id.strip():
        return None
    text = model_id.strip().lower()
    if option(text):
        return option(text)
    for key, mid in (("opus", "claude-opus"), ("sonnet", "claude-sonnet"), ("fable", "claude-fable"),
                     ("freebuff", "freebuff"), ("deepseek", "freebuff")):
        if key in text:
            return option(mid)
    return None


class _PickH:
    def __init__(self, app):
        self.app, self.actor = app, None

    def _json_body(self):
        return {}


def _pick(app):
    if PICK is not None:
        return PICK(app)
    try:
        from . import pick
        fn = (getattr(pick, "ROUTES", None) or {}).get("GET /api/pick")
        return fn(_PickH(app), {"kind": "plan"}) if callable(fn) else None
    except Exception:
        return None


def default_choice(app=None) -> dict:
    try:
        got = _pick(app)
    except Exception:
        got = None
    opt = _match(got.get("model")) if isinstance(got, dict) else None
    if opt and not _disabled(opt):
        effort = got.get("effort") if got.get("effort") in EFFORTS else DEFAULT["effort"]
        return {"model": opt["id"], "effort": effort, "why": str(got.get("why") or "")[:160] or None,
                "source": "pick"}
    return {"model": DEFAULT["model"], "effort": DEFAULT["effort"], "why": None, "source": "fixed"}


def options(app=None) -> dict:
    rows = [{"id": m["id"], "label": m["label"], "free": m["free"], "enabled": not _disabled(m)}
            for m in MODEL_OPTIONS]
    return {"models": rows, "efforts": list(EFFORTS), "default": default_choice(app)}


# ---------------------------------------------------------------- model calls
def _runner(app=None):
    return RUNNER or getattr(app, "deep_plan_runner", None) or planner._runner_for(app)


def _argv(opt, effort, prompt, research=False, cwd=None):
    if opt["route"] == "freebuff":
        return list(providers.freebuff_exec(prompt, model=opt["model"]).argv)
    extra = list(RESEARCH_TOOLS) if research else None
    dirs = [DRIVE_DIR] if research else []
    return planner.claude_argv(model=opt["model"], effort=effort, extra_args=extra, add_dirs=dirs)


def call(sess, role, prompt, stage, app=None) -> dict:
    """One model call for ``role`` (planner | research | critic). Returns the parsed JSON object."""
    mid, effort = {"planner": (sess["model"], sess["effort"]),
                   "research": (sess["research_model"], sess["research_effort"]),
                   "critic": (sess["critic"], sess["effort"])}[role]
    opt = option(mid) or option(DEFAULT["model"])
    cwd = sess.get("cwd")
    workdir = cwd if cwd and Path(cwd).is_dir() else None
    code, out, err = _runner(app)(_argv(opt, effort, prompt, research=role == "research"), workdir, prompt,
                                  TIMEOUTS.get(stage, 15 * 60))
    if int(code) != 0:
        detail = (err or out or "").strip().splitlines()
        raise RuntimeError(f"the {stage} step stopped (exit {code})" + (f": {detail[-1][:300]}" if detail else ""))
    return planner.parse_reply(out)


def render(name, values) -> str:
    return planner.render_prompt(name, values, prefix="deep")


# ---------------------------------------------------------------- sessions
def _key(sid):
    return META_PREFIX + sid


def load_session(store, sid):
    raw = store.get_meta(_key(sid)) if sid else None
    return json.loads(raw) if raw else None


def _stage_rows(sess):
    return [{"id": s["id"], "label": s["label"], "state": s["state"], "line": s["line"]} for s in sess["stages"]]


def save_session(store, sess):
    sess["updated_at"] = now_iso()
    with _lock:
        store.set_meta(_key(sess["id"]), json.dumps(sess, ensure_ascii=False))
    try:
        store.notify("deep_plan", {"id": sess["id"], "status": sess["status"], "stage": sess.get("stage"),
                                   "stages": _stage_rows(sess)})
    except Exception:
        pass
    return sess


def _stage(store, sid, stage, state, line, **fields):
    """Set one stage's state and line (and any session fields) and stream it."""
    with _lock:
        sess = load_session(store, sid)
        for s in sess["stages"]:
            if s["id"] == stage:
                s["state"], s["line"] = state, _one_line(line)
                if state == "running":
                    s["started_at"] = s.get("started_at") or now_iso()
                elif state in ("done", "skipped", "error", "waiting"):
                    s["ended_at"] = now_iso()
        sess["stage"] = stage
        sess.update(fields)
        return save_session(store, sess)


def _update(store, sid, **fields):
    with _lock:
        sess = load_session(store, sid)
        sess.update(fields)
        return save_session(store, sess)


def _one_line(text, limit=140):
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"


def list_sessions(store, limit=20):
    rows = store._q("SELECT value FROM meta WHERE key LIKE ?", (META_PREFIX + "%",))
    out = [json.loads(r[0]) for r in rows]
    out.sort(key=lambda s: s.get("created_at") or "", reverse=True)
    return out[:limit]


def _plain(exc) -> str:
    return planner._plain(exc)


# ---------------------------------------------------------------- helpers for prompts
def _bounded_list(items, limit=12, chars=300):
    out = []
    for x in items if isinstance(items, list) else []:
        s = _one_line(x if isinstance(x, str) else json.dumps(x, ensure_ascii=False), chars)
        if s and s not in out:
            out.append(s)
        if len(out) >= limit:
            break
    return out


def _bullets(items, empty="(none)"):
    return "\n".join(f"- {x}" for x in items) or empty


def folder_listing(cwd, limit=150, depth=3) -> str:
    """A short file tree for runners that cannot read files themselves."""
    if not cwd or not Path(cwd).is_dir():
        return "(no folder)"
    root, out = Path(cwd), []
    for base, dirs, files in os.walk(root):
        rel = Path(base).relative_to(root)
        dirs[:] = sorted(d for d in dirs if d not in TREE_SKIP and not d.startswith("."))
        if len(rel.parts) >= depth:
            dirs[:] = []
        for f in sorted(files):
            out.append((rel / f).as_posix())
            if len(out) >= limit:
                return "\n".join(out) + "\n…"
    return "\n".join(out) or "(empty folder)"


def _research_text(sess) -> str:
    parts = []
    for r in sess.get("research") or []:
        if r.get("error"):
            continue
        parts.append(f"## {r['topic']} ({r['where']})\n" + _bullets(r.get("findings"))
                     + ("\nSources: " + ", ".join(r["sources"]) if r.get("sources") else ""))
    return "\n\n".join(parts) or "(no research findings)"


def _framework_text(fw) -> str:
    if not fw:
        return "(none)"
    lines = [f"Decomposition: {fw.get('decomposition') or '(not given)'}"]
    for s in fw.get("streams") or []:
        lines.append(f"- stream {s.get('id')}: {s.get('name')} owns {', '.join(s.get('owns') or []) or '(nothing listed)'}")
    for c in fw.get("contracts") or []:
        lines.append(f"- contract {c.get('name')} between {', '.join(map(str, c.get('between') or []))}: {c.get('shape')}")
    return "\n".join(lines)


def _base_values(store, sess):
    return {"title": sess["title"], "goal": sess["goal"], "cwd": sess.get("cwd") or "(not set)",
            "brief": sess.get("brief") or sess["goal"], "research": _research_text(sess),
            "answers": planner._answer_lines(store, sess), "framework": _framework_text(sess.get("framework")),
            "max_nodes": planner.MAX_NODES, "max_topics": MAX_TOPICS, "min_topics": MIN_TOPICS,
            "max_questions": MAX_QUESTIONS, "preferences": planner.PREFERENCES_DIR, "drive": DRIVE_DIR,
            "models": planner._models_text(planner.recommend_options()),
            "constraints": planner.constraints_text(store, f"{sess['title']} {sess['goal']} {sess.get('brief') or ''}")}


# ---------------------------------------------------------------- stages
def _topics(raw):
    out = []
    for t in raw.get("topics") if isinstance(raw.get("topics"), list) else []:
        if not isinstance(t, dict) or not str(t.get("topic") or "").strip():
            continue
        where = t.get("where") if t.get("where") in WHERE else "code"
        out.append({"topic": _one_line(t["topic"], 80), "where": where,
                    "ask": _one_line(t.get("ask") or t["topic"], 300)})
        if len(out) >= MAX_TOPICS:
            break
    defaults = [{"topic": "The existing code and files", "where": "code",
                 "ask": "What already exists that this plan must build on or must not break?"},
                {"topic": "Owner's notes and preferences", "where": "drive",
                 "ask": "What has Owner already decided or asked for that bears on this goal?"}]
    for d in defaults:
        if len(out) >= MIN_TOPICS:
            break
        if not any(t["where"] == d["where"] for t in out):
            out.append(d)
    return out


def stage_brief(store, sid, app):
    sess = load_session(store, sid)
    raw = call(sess, "planner", render("brief", _base_values(store, sess)), "brief", app)
    brief = str(raw.get("brief") or "").strip() or sess["goal"]
    topics = _topics(raw)
    _stage(store, sid, "brief", "done", f"Brief written · {len(topics)} research topics",
           brief=brief[:4000], topics=topics, unknowns=_bounded_list(raw.get("unknowns"), 10))


def stage_research(store, sid, app):
    sess = load_session(store, sid)
    topics = sess.get("topics") or _topics({})
    base = _base_values(store, sess)
    free = (option(sess["research_model"]) or {}).get("free")
    listing = folder_listing(sess.get("cwd")) if free else ""
    done = {"n": 0, "failed": 0}

    def one(t):
        values = dict(base, topic=t["topic"], where=t["where"], ask=t["ask"],
                      listing=("\nFolder listing (you cannot open files; use this tree):\n" + listing + "\n")
                      if listing else "")
        try:
            raw = call(sess, "research", render("research", values), "research", app)
            row = dict(t, findings=_bounded_list(raw.get("findings")), sources=_bounded_list(raw.get("sources"), 12, 200),
                       unknowns=_bounded_list(raw.get("unknowns"), 6), error=None)
        except Exception as exc:  # one topic failing never stops the others
            row = dict(t, findings=[], sources=[], unknowns=[], error=_plain(exc))
        with _lock:
            done["n"] += 1
            done["failed"] += 1 if row["error"] else 0
            _stage(store, sid, "research", "running", f"{done['n']} of {len(topics)} researched")
        return row

    with ThreadPoolExecutor(max_workers=max(1, len(topics)), thread_name_prefix="cc-deep-research") as pool:
        rows = list(pool.map(one, topics))
    failed = sum(1 for r in rows if r["error"])
    if failed == len(rows):
        raise RuntimeError("every research job failed: " + (rows[0]["error"] if rows else "no topics"))
    unknowns = list(sess.get("unknowns") or [])
    for r in rows:
        for u in r["unknowns"]:
            if u not in unknowns:
                unknowns.append(u)
    found = sum(len(r["findings"]) for r in rows)
    line = f"{len(rows) - failed} of {len(rows)} researched · {found} findings"
    if failed:
        line += f" · {failed} failed"
    _stage(store, sid, "research", "done", line, research=rows, unknowns=unknowns[:12])


def stage_questions(store, sid, app):
    """Ask only what research could not find. Returns True when the job must wait for Owner."""
    sess = load_session(store, sid)
    unknowns = sess.get("unknowns") or []
    if not unknowns:
        _stage(store, sid, "questions", "skipped", "Nothing to ask")
        return False
    values = dict(_base_values(store, sess), unknowns=_bullets(unknowns))
    raw = call(sess, "planner", render("questions", values), "questions", app)
    items = [q for q in planner.normalise_questions(raw) if q["kind"] in ("likert", "select")]
    if not items:
        _stage(store, sid, "questions", "skipped", "Nothing to ask")
        return False
    stored = []
    for item in items[:MAX_QUESTIONS]:
        qid = needs.ask(store, None, None, item["kind"], item["prompt"], options=item.get("options") or [],
                        scale=item.get("scale"), purpose=item["purpose"])
        stored.append(dict(item, id=qid))
    n = len(stored)
    _stage(store, sid, "questions", "waiting", f"{n} question{'s' if n != 1 else ''} for you",
           questions=stored, status="questions")
    store.event(None, None, "planner", ACTOR,
                f"The deep planner asked {n} question{'s' if n != 1 else ''} about '{sess['title']}'.",
                {"deep_plan": sid})
    return True


def stage_framework(store, sid, app):
    sess = load_session(store, sid)
    if any(s["id"] == "questions" and s["state"] == "waiting" for s in sess["stages"]):
        answered = sum(1 for q in sess.get("questions") or []
                       if (needs.get_question(store, q["id"]) or {}).get("answered_at"))
        _stage(store, sid, "questions", "done", f"{answered} of {len(sess['questions'])} answered")
        sess = load_session(store, sid)
    raw = call(sess, "planner", render("framework", _base_values(store, sess)), "framework", app)
    fw = {"decomposition": _one_line(raw.get("decomposition") or "", 300),
          "streams": [s for s in raw.get("streams") or [] if isinstance(s, dict)][:12],
          "contracts": [c for c in raw.get("contracts") or [] if isinstance(c, dict)][:12]}
    line = fw["decomposition"] or f"{len(fw['streams'])} streams"
    if fw["contracts"]:
        line += f" · {len(fw['contracts'])} contracts"
    _stage(store, sid, "framework", "done", line, framework=fw)


def _files(n):
    return [str(f).strip() for f in (n.get("files") or []) if isinstance(f, str) and str(f).strip()][:30]


def evaluate(raw) -> dict:
    """Validate, score and repair one drafted graph. ``{"valid", "problems", "raw", "score",
    "changes"}``; ``raw`` is the repaired graph when valid."""
    found = planner.problems(raw)
    if found:
        return {"valid": False, "problems": found, "raw": raw, "score": None, "changes": []}
    crosses = {n["id"]: node_kinds.split_deps(n.get("deps"))[1] for n in raw["nodes"]}
    nodes = [dict(n, files=_files(n), deps=node_kinds.split_deps(n.get("deps"))[0]) for n in raw["nodes"]]
    before = plan_metrics.score(nodes)
    if before["cycle"]:
        return {"valid": False, "problems": before["problems"], "raw": raw, "score": before, "changes": []}
    fixed = plan_metrics.repair(nodes)
    out = dict(raw, nodes=_with_crosses(fixed["nodes"], crosses))
    again = planner.problems(out)
    if again:
        return {"valid": False, "problems": again, "raw": raw, "score": before, "changes": fixed["changes"]}
    notes = [f"{i} declares no files it writes" for i in fixed["score"]["undeclared"]]
    return {"valid": True, "problems": notes, "raw": out, "score": fixed["score"], "changes": fixed["changes"],
            "before": before}


def _with_crosses(nodes, crosses):
    """Object deps again; an edge the repair added names the files both tasks write."""
    by = {n["id"]: n for n in nodes}
    out = []
    for n in nodes:
        named = dict(crosses.get(n["id"]) or {})
        for d in n.get("deps") or []:
            if not named.get(d):
                shared = [f for f in _files(n) if f in _files(by.get(d) or {})]
                named[d] = ("the finished " + ", ".join(shared)) if shared else f"{d}'s finished change"
        out.append(dict(n, deps=node_kinds.join_deps(n.get("deps") or [], named)))
    return out


def _feedback(cands) -> str:
    if not cands:
        return ""
    lines = ["\nEarlier drafts and how they scored. Draft a different, better decomposition: shorter "
             "critical path, more tasks able to run at once, and no two parallel tasks writing one file."]
    for c in cands:
        if c["valid"]:
            lines.append(f"- draft {c['n']}: {c['line']}" + (f"; overlaps that had to be repaired: "
                                                              + " ".join(c["changes"]) if c["changes"] else ""))
        else:
            lines.append(f"- draft {c['n']} was rejected: " + "; ".join(c["problems"][:6]))
    return "\n".join(lines) + "\n"


def stage_graph(store, sid, app):
    sess = load_session(store, sid)
    total = max(1, min(MAX_CANDIDATES, int(sess.get("candidates_wanted") or MAX_CANDIDATES)))
    base = _base_values(store, sess)
    cands, graphs = [], []
    for k in range(1, total + 1):
        values = dict(base, n=k, total=total, feedback=_feedback(cands))
        try:
            raw = call(sess, "planner", render("graph", values), "graph", app)
            ev = evaluate(raw)
        except plan_metrics.CycleError as exc:
            ev = {"valid": False, "problems": [str(exc)], "raw": None, "score": None, "changes": []}
        line = plan_metrics.line(ev["score"]) if ev["score"] else "rejected"
        cands.append({"n": k, "valid": ev["valid"], "line": line, "problems": ev["problems"][:8],
                      "changes": ev["changes"][:8], "repairs": len(ev["changes"]), "chosen": False,
                      "score": _score_view(ev["score"]) if ev["valid"] else None})
        graphs.append(ev)
        _stage(store, sid, "graph", "running", f"Candidate {k} of {total}: {line}")
    valid = [i for i, g in enumerate(graphs) if g["valid"]]
    if not valid:
        problems = []
        for c in cands:
            for p in c["problems"]:
                if p not in problems:
                    problems.append(p)
        _stage(store, sid, "graph", "error", f"No usable graph in {total} candidate{'s' if total != 1 else ''}",
               candidates=cands, status="problems", problems=problems[:12])
        return False
    pick = min(valid, key=lambda i: (plan_metrics.rank_key(graphs[i]["score"]), i))
    cands[pick]["chosen"] = True
    chosen = graphs[pick]
    _stage(store, sid, "graph", "done",
           f"Best of {total} candidate{'s' if total != 1 else ''}: " + plan_metrics.line(chosen["score"]),
           candidates=cands, graph=chosen["raw"], scores=_score_view(chosen["score"]))
    return True


def _score_view(s):
    if not s:
        return None
    return {"tasks": s["tasks"], "critical_path": s["critical_path"], "critical_path_ids": s["critical_path_ids"],
            "peak_width": s["peak_width"], "waves": s["waves"], "width_by_wave": s["width_by_wave"],
            "overlaps": len(s["overlaps"]), "undeclared": len(s["undeclared"]), "line": plan_metrics.line(s)}


# ---------------------------------------------------------------- task briefs
def check_text(c) -> str:
    c = c or {}
    t = c.get("type")
    if t == "command":
        return f"`{c.get('run')}` exits 0" + (f" and prints exactly {c['expect_stdout']!r}" if c.get("expect_stdout") else "")
    if t == "files_exist":
        return "these files exist: " + ", ".join(map(str, c.get("paths") or []))
    if t == "text_present":
        return f"{c.get('path')} contains {c.get('text')!r}"
    if t == "human":
        return f"Owner confirms: {c.get('prompt')}"
    return "its check passes"


def compose_brief(n, by_id, extra, project_title, project_brief, framework=None, rules=None) -> str:
    """One task's bounded brief. Files, hand-offs and the check come from the graph; inputs and exit
    criteria from the prompt stage (or the node itself), with sensible defaults."""
    extra = extra or {}
    inputs = _bounded_list(n.get("inputs") or extra.get("inputs"), 8)
    ids, crosses = node_kinds.split_deps(n.get("deps"))
    for d in ids:
        dep = by_id.get(d) or {}
        inputs.append(f"The output of {d} ({dep.get('title') or d}): "
                      + (crosses.get(d) or ", ".join(_files(dep)) or "see its goal"))
    exit_items = _bounded_list(n.get("exit") or extra.get("exit"), 8)
    exit_items += ["The done-check passes exactly as written.", "No file outside your list was changed."]
    files = _files(n)
    lines = [f"Task {n['id']}: {n.get('title') or n['id']}",
             f"Project: {project_title}. {_one_line(project_brief, 400)}",
             f"Goal: {n['goal']}"]
    if framework and framework.get("decomposition"):
        lines.append(f"Plan shape: {_one_line(framework['decomposition'], 200)}")
    lines += ["", "Inputs:", _bullets(inputs, "- Nothing beyond the folder and this brief."), "",
              "You own (write only these):", _bullets(files, "- No files: report your result in your reply.")]
    hand = [h for h in n.get("handoff") or [] if isinstance(h, dict)]
    if hand:
        lines += ["", "Hand off (do not edit; put the exact diff in your report for its owner):",
                  _bullets([f"{h.get('file')} → task {h.get('to')}" for h in hand])]
    if extra.get("notes"):
        lines += ["", "Notes: " + _one_line(extra["notes"], 500)]
    if node_kinds.kind_of(n) == "code":
        lines += ["", "Kind: code. Run the script; no model judgement: " + (node_kinds.code_script(n) or "(none)")]
    if rules:
        lines += ["", RULES_HEADING, _bullets([_one_line(r, 300) for r in rules])]
    lines += ["", "Done-check: " + check_text(n.get("check")), "", "Exit criteria:", _bullets(exit_items), "",
              "Write the failing test first, then the code. Do not commit. Report: files changed and the check output."]
    text = "\n".join(lines)
    return text if len(text) <= MAX_BRIEF_CHARS else text[:MAX_BRIEF_CHARS - 1] + "…"


def _extras(raw):
    out = {}
    for t in (raw or {}).get("tasks") or []:
        if isinstance(t, dict) and isinstance(t.get("id"), str):
            out[t["id"]] = {"inputs": _bounded_list(t.get("inputs"), 8), "exit": _bounded_list(t.get("exit"), 8),
                            "notes": _one_line(t.get("notes") or "", 500) or None}
    return out


def _node_text(n):
    return " ".join([str(n.get("title") or ""), str(n.get("goal") or "")] + _files(n))


def _rules_for(store, n, project=None):
    """Learned constraints for one task: those from its upstream tasks, then keyword matches."""
    if store is None:
        return []
    try:
        from . import constraints
        return [c["line"] for c in constraints.for_task(store, n, project=project, text=_node_text(n))]
    except Exception:  # the learning edge is optional
        return []


def _compose_all(sess, graph, extras, store=None):
    by_id = {n["id"]: n for n in graph["nodes"]}
    return {n["id"]: {"text": compose_brief(n, by_id, extras.get(n["id"]), sess["title"],
                                            graph.get("brief") or sess.get("brief") or "", sess.get("framework"),
                                            rules=_rules_for(store, n)),
                      "files": _files(n), "handoff": n.get("handoff") or []}
            for n in graph["nodes"]}


def _graph_json(graph, limit=30000):
    keep = ("id", "title", "goal", "kind", "script", "check", "deps", "files", "handoff", "lane", "recommended")
    slim = {"brief": graph.get("brief"), "nodes": [{k: n[k] for k in keep if k in n} for n in graph["nodes"]]}
    return json.dumps(slim, ensure_ascii=False)[:limit]


def stage_prompts(store, sid, app):
    sess = load_session(store, sid)
    values = dict(_base_values(store, sess), graph=_graph_json(sess["graph"]))
    extras = _extras(call(sess, "planner", render("prompts", values), "prompts", app))
    briefs = _compose_all(sess, sess["graph"], extras, store)
    covered = sum(1 for i in briefs if i in extras)
    _stage(store, sid, "prompts", "done", f"{len(briefs)} task briefs written"
           + (f" · {len(briefs) - covered} from defaults" if covered < len(briefs) else ""),
           extras=extras, briefs=briefs)


def stage_critique(store, sid, app):
    sess = load_session(store, sid)
    briefs_text = "\n\n".join(b["text"] for b in (sess.get("briefs") or {}).values())[:30000]
    values = dict(_base_values(store, sess), graph=_graph_json(sess["graph"]),
                  scores=(sess.get("scores") or {}).get("line") or "", briefs=briefs_text)
    raw = call(sess, "critic", render("critique", values), "critique", app)
    verdict = raw.get("verdict") if raw.get("verdict") in ("ready", "revise") else "ready"
    issues = [{"task": i.get("task"), "issue": _one_line(i.get("issue"), 300), "fix": _one_line(i.get("fix"), 300)}
              for i in raw.get("issues") or [] if isinstance(i, dict) and i.get("issue")][:12]
    critique = {"verdict": verdict, "summary": _one_line(raw.get("summary") or "", 200), "issues": issues}
    n = len(issues)
    if verdict != "revise" or not issues:
        _stage(store, sid, "critique", "done", critique["summary"] or f"Sound · {n} issue{'s' if n != 1 else ''}",
               critique=critique)
        return
    _stage(store, sid, "critique", "running", f"Revising after {n} issue{'s' if n != 1 else ''}", critique=critique)
    issue_text = _bullets([f"{i['task'] or 'plan'}: {i['issue']} Fix: {i['fix']}" for i in issues])
    values = dict(values, issues=issue_text)
    try:
        ev = evaluate(call(sess, "planner", render("revise", values), "revise", app))
    except (plan_metrics.CycleError, RuntimeError, ValueError):
        ev = {"valid": False}
    if not ev.get("valid"):
        _stage(store, sid, "critique", "done", f"{n} issue{'s' if n != 1 else ''} noted; the revision was not usable, "
               "kept the original")
        return
    sess = load_session(store, sid)
    extras = dict(sess.get("extras") or {})
    briefs = _compose_all(sess, ev["raw"], extras, store)
    _stage(store, sid, "critique", "done", f"Revised after {n} issue{'s' if n != 1 else ''} · "
           + plan_metrics.line(ev["score"]), graph=ev["raw"], scores=_score_view(ev["score"]), briefs=briefs)


def _import(store, sid):
    sess = load_session(store, sid)
    n = int(sess.get("drafts") or 0) + 1
    pid = sess["project_base"] + ("" if n == 1 else f"-r{n}")
    options_ = planner.recommend_options()
    built = planner.build_plan(sess, sess["graph"], pid, allowed_models=options_)
    old = sess.get("project")
    plan.import_plan(store, built, actor=ACTOR, draft=True)
    planner._archive_draft(store, old)
    briefs = sess.get("briefs") or {}
    store.set_meta(BRIEFS_PREFIX + pid, json.dumps(briefs, ensure_ascii=False))
    summary = planner.summarise(built)
    for item in summary["nodes"]:
        b = briefs.get(item["id"]) or {}
        item["files"] = b.get("files") or []
        item["handoff"] = b.get("handoff") or []
    _update(store, sid, status="ready", stage=None, project=pid, drafts=n,
            previous=list(sess.get("previous") or []) + ([old] if old else []), draft=summary, problems=[],
            error=None)
    store.event(pid, None, "planner", ACTOR,
                f"The deep planner drafted '{sess['title']}' with {len(built['nodes'])} tasks.", {"deep_plan": sid})


RUNNERS = {"brief": stage_brief, "research": stage_research, "questions": stage_questions,
           "framework": stage_framework, "graph": stage_graph, "prompts": stage_prompts, "critique": stage_critique}


def _run(store, sid, app=None, from_stage="brief"):
    stage = from_stage
    try:
        for stage in STAGES[STAGES.index(from_stage):]:
            _stage(store, sid, stage, "running", "Working…", status="running")
            result = RUNNERS[stage](store, sid, app)
            if stage == "questions" and result:
                return  # waits for Owner; /api/plan/deep/continue resumes at framework
            if stage == "graph" and result is False:
                return  # status problems
        _import(store, sid)
    except Exception as exc:  # recorded on the session; never raised into the server thread
        _stage(store, sid, stage, "error", _plain(exc), status="error", error=_plain(exc), failed_stage=stage)
    finally:
        _live.discard(sid)


def _spawn(fn):
    if SPAWN is not None:
        return SPAWN(fn)
    threading.Thread(target=fn, daemon=True, name="cc-deep-plan").start()
    return None


# ---------------------------------------------------------------- start / resume
def _choose(model, default_id):
    if model in (None, ""):
        return option(default_id)
    opt = option(model) if isinstance(model, str) else None
    if opt is None:
        raise ValueError(f"unknown planner model {model!r}; choose one of "
                         + ", ".join(m["id"] for m in MODEL_OPTIONS))
    return opt


def start(store, title, goal, cwd=None, model=None, effort=None, critic=None, research_model=None,
          candidates=MAX_CANDIDATES, actor=ACTOR, app=None) -> dict:
    title, goal = (title or "").strip(), (goal or "").strip()
    if not title or not goal:
        raise ValueError("a plan needs a title and a goal")
    if len(title) > 120 or len(goal) > 4000:
        raise ValueError("keep the title under 120 and the goal under 4000 characters")
    default = default_choice(app) if not model or not effort else None
    opt = _choose(model, (default or DEFAULT)["model"])
    if effort not in (None, "") and effort not in EFFORTS:
        raise ValueError(f"effort must be one of {', '.join(EFFORTS)}")
    effort = effort or (default or DEFAULT)["effort"]
    crit = _choose(critic, CRITIC_FOR.get(opt["id"], "claude-sonnet"))
    if crit["id"] == opt["id"]:
        raise ValueError("the critique needs a second model, different from the planner")
    res = _choose(research_model, "freebuff" if opt["free"] else RESEARCH_DEFAULT["model"])
    for o in (opt, crit, res):
        if _disabled(o):
            raise ValueError(f"{o['label']} is switched off on the Models tab")
    try:
        wanted = max(1, min(MAX_CANDIDATES, int(candidates)))
    except (TypeError, ValueError):
        wanted = MAX_CANDIDATES
    cwd = (cwd or "").strip() or None
    sid = "dp-" + secrets.token_hex(4)
    sess = {"id": sid, "title": title, "goal": goal, "cwd": cwd, "status": "running", "stage": "brief",
            "model": opt["id"], "effort": effort, "critic": crit["id"], "research_model": res["id"],
            "research_effort": RESEARCH_DEFAULT["effort"], "candidates_wanted": wanted,
            "stages": [{"id": s, "label": STAGE_LABELS[s], "state": "pending", "line": ""} for s in STAGES],
            "project_base": f"{planner.slug(title)}-{secrets.token_hex(2)}", "project": None, "drafts": 0,
            "previous": [], "questions": [], "draft": None, "problems": [], "error": None,
            "requested_by": actor, "created_at": now_iso()}
    _live.add(sid)
    save_session(store, sess)
    _spawn(lambda: _run(store, sid, app, "brief"))
    return view(store, sid)


def resume(store, sid, app=None) -> dict:
    """Continue after Owner's answers, or retry from the stage that failed."""
    with _lock:
        sess = load_session(store, sid)
        if sess is None:
            raise KeyError(f"no deep plan {sid}")
        status = session_status(store, sess)
        if status == "questions":
            frm = "framework"
        elif status == "error":
            frm = sess.get("failed_stage") or "brief"
            if frm == "questions" and not sess.get("unknowns"):
                frm = "framework"
        elif status == "problems":
            frm = "graph"
        else:
            raise HttpError(409, {"error": "not_waiting", "detail": f"this plan is {status}; nothing to continue"})
        sess.update(status="running", error=None, problems=[])
        _live.add(sid)
        save_session(store, sess)
    _spawn(lambda: _run(store, sid, app, frm))
    return view(store, sid)


# ---------------------------------------------------------------- views
def session_status(store, sess) -> str:
    status = sess.get("status")
    if status == "running" and sess["id"] not in _live:
        status = "error"
        msg = "The engine restarted while planning; try again."
        for s in sess["stages"]:
            if s["state"] == "running":
                s["state"], s["line"] = "error", msg
                sess["failed_stage"] = s["id"]
        sess.update(status="error", error=msg)
        save_session(store, sess)
    if status == "ready" and sess.get("project") and store.project_status(sess["project"]) == "active":
        return "accepted"
    return status


def view(store, sid) -> dict:
    sess = load_session(store, sid)
    if sess is None:
        raise KeyError(f"no deep plan {sid}")
    status = session_status(store, sess)
    questions = []
    for item in sess.get("questions") or []:
        q = needs.get_question(store, item["id"]) or {}
        questions.append(dict(item, answer=q.get("answer"), answered_at=q.get("answered_at"),
                              allowed=q.get("allowed")))
    crit = sess.get("critique")
    labels = {m["id"]: m["label"] for m in MODEL_OPTIONS}
    return {"id": sess["id"], "status": status, "title": sess["title"], "goal": sess["goal"], "cwd": sess.get("cwd"),
            "model": sess["model"], "model_label": labels.get(sess["model"]), "effort": sess["effort"],
            "critic": sess["critic"], "critic_label": labels.get(sess["critic"]), "stage": sess.get("stage"),
            "stages": _stage_rows(sess), "questions": questions,
            "answered": sum(1 for q in questions if q.get("answered_at")),
            "scores": sess.get("scores"),
            "candidates": [{k: c[k] for k in ("n", "valid", "line", "chosen", "repairs", "changes", "problems")}
                           for c in sess.get("candidates") or []],
            "critique": {"verdict": crit["verdict"], "summary": crit["summary"], "issues": len(crit["issues"])}
            if crit else None,
            "project": sess.get("project"),
            "runnable": bool(sess.get("project")) and store.project_runnable(sess["project"]),
            "draft": sess.get("draft"), "problems": sess.get("problems") or [], "error": sess.get("error"),
            "drafts": sess.get("drafts") or 0, "created_at": sess.get("created_at"),
            "updated_at": sess.get("updated_at")}


def task_brief(store, project, node_id) -> dict:
    raw = store.get_meta(BRIEFS_PREFIX + str(project)) if project else None
    briefs = json.loads(raw) if raw else {}
    b = briefs.get(node_id)
    if not b:
        raise KeyError(f"no brief for task {node_id} in {project}")
    text = b["text"]
    node = store.get_node(project, node_id) or {"id": node_id}
    rules = [r for r in _rules_for(store, dict(node, files=b.get("files") or []), project=project)
             if r not in text]
    if rules:
        text += "\n\n" + RULES_HEADING + "\n" + _bullets([_one_line(r, 300) for r in rules])
    return {"project": project, "id": node_id, "text": text, "files": b.get("files") or [],
            "handoff": b.get("handoff") or [], "rules": rules}


# ---------------------------------------------------------------- routes
def p_start(h, q):
    _owner_only(h, "start a deep plan")
    b = h._json_body()
    for k in ("title", "goal"):
        if not str(b.get(k) or "").strip():
            raise HttpError(400, {"error": "bad_request", "detail": f"missing {k}"})
    cwd = b.get("cwd")
    if cwd and not Path(str(cwd)).is_dir():
        raise HttpError(400, {"error": "bad_request", "detail": "that folder does not exist"})
    try:
        sess = start(h.app.store, b["title"], b["goal"], cwd, model=b.get("model"), effort=b.get("effort"),
                     critic=b.get("critic"), research_model=b.get("research_model"),
                     candidates=b.get("candidates", MAX_CANDIDATES), actor=getattr(h, "actor", ACTOR), app=h.app)
    except ValueError as exc:
        raise HttpError(400, {"error": "bad_request", "detail": str(exc)})
    return {"ok": True, "session": sess}


def p_continue(h, q):
    _owner_only(h, "continue a deep plan")
    b = h._json_body()
    sid = b.get("id") or b.get("session")
    if not sid:
        raise HttpError(400, {"error": "bad_request", "detail": "missing id"})
    return {"ok": True, "session": resume(h.app.store, sid, app=h.app)}


def g_session(h, q):
    sid = q.get("id") or q.get("session")
    if not sid:
        raise HttpError(400, {"error": "bad_request", "detail": "missing id"})
    return view(h.app.store, sid)


def g_sessions(h, q):
    store = h.app.store
    return {"sessions": [{"id": s["id"], "title": s["title"], "status": session_status(store, s),
                          "model": s.get("model"), "project": s.get("project"), "created_at": s.get("created_at")}
                         for s in list_sessions(store)]}


def g_options(h, q):
    return options(h.app)


def g_brief(h, q):
    if not q.get("project") or not q.get("id"):
        raise HttpError(400, {"error": "bad_request", "detail": "missing project or id"})
    return task_brief(h.app.store, q["project"], q["id"])


ROUTES = {
    "GET /api/plan/deep": g_session,
    "GET /api/plan/deep/sessions": g_sessions,
    "GET /api/plan/deep/options": g_options,
    "GET /api/plan/deep/brief": g_brief,
    "POST /api/plan/deep/start": p_start,
    "POST /api/plan/deep/continue": p_continue,
}
