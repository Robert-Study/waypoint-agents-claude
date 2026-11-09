"""Checks and failure doctor.

- ``draft_check(goal, runner=None)``: a machine-checkable done-check for a goal: ``command``,
  ``files_exist`` or ``text_present``, never a human check or a "looks good". An optional model
  ``runner(prompt) -> str`` may propose one; a vague, trivial or malformed proposal is dropped
  and the goal's own signals are used instead (a backticked command, "<file> mentions `text`",
  named file paths), with a pytest selection named after the goal's key word as the last resort.
- ``diagnose(node, detail, runner=None)``: one plain line saying why a check failed.
- ``on_check_failed(store, project, node, detail, runner=None)``: writes that cause as a
  ``diagnosis`` event and adds a fix-up task (``<id>-fix<n>``, via ``failure.add_fixup``) whose
  goal sits within the task's own goal; the task's goal is never changed. At most ``MAX_FIXUPS``
  fix-ups per task, after which only the cause is written.

Everything is pure or store-only: no provider is called unless a runner is passed in.
``POST /api/check/draft {goal}`` serves ``draft_check`` with no model and no state change.
"""
from __future__ import annotations

import json
import re

CAUSE_LIMIT = 140
TITLE_LIMIT = 80
MAX_FIXUPS = 2
MACHINE_TYPES = ("command", "files_exist", "text_present")
VAGUE = re.compile(r"looks?\s+(?:good|fine|ok(?:ay)?|right)|seems?\s+(?:good|fine|ok)|lgtm|verify\s+manually"
                   r"|manually\s+(?:check|verify)|works\s+(?:fine|well)|eyeball", re.I)
TRIVIAL_COMMAND = re.compile(r"^\s*(?:@?echo\b|rem\b|true\s*$|:\s*$|exit\s+0\s*$|cmd(?:\.exe)?\s+/c\s+exit\s+0\s*$"
                             r"|python\s+-c\s+['\"]?pass['\"]?\s*$)", re.I)
COMMAND_START = re.compile(r"^(?:python|py|pytest|node|npm|npx|pnpm|yarn|uv|pwsh|powershell|make|cargo|go|dotnet"
                           r"|git|bash|sh|ruff|mypy|tsc|deno|bun|java|mvn|gradle|\.?[\\/])\b", re.I)
BACKTICK = re.compile(r"`([^`\n]+)`")
TEXT_PRESENT = re.compile(r"(?<![\w/\\.-])((?:[\w.-]+[\\/])*[\w.-]+\.[A-Za-z0-9]{1,6})\s+(?:mentions|contains|includes"
                          r"|says|has|shows|lists)\s+(?:the\s+(?:text|word|line)\s+)?[`\"']([^`\"'\n]+)[`\"']", re.I)
PATH = re.compile(r"(?<![\w/\\.-])((?:[\w.-]+[\\/])+[\w.-]+\.[A-Za-z0-9]{1,6})(?![\w/\\])")
STOP = frozenset("""a an and the of to for in on at by with from into onto that this these those it its is are be
make makes made add adds added create creates build builds write writes update updates change changes fix fixes
should must will can so as or not no all each every any more less than then when where which who what how
owner task tasks goal goals work works done check checks pass passes test tests calmer better""".split())


# ---------------------------------------------------------------- validation
def _nonempty(s) -> bool:
    return isinstance(s, str) and bool(s.strip())


def is_machine_checkable(check) -> bool:
    """True for a well-formed command / files_exist / text_present check that is not vague or trivial."""
    if not isinstance(check, dict) or check.get("type") not in MACHINE_TYPES:
        return False
    if VAGUE.search(json.dumps(check)):
        return False
    kind = check["type"]
    if kind == "command":
        run = check.get("run")
        return _nonempty(run) and not TRIVIAL_COMMAND.match(run)
    if kind == "files_exist":
        paths = check.get("paths")
        return isinstance(paths, list) and bool(paths) and all(_nonempty(p) for p in paths)
    return _nonempty(check.get("path")) and _nonempty(check.get("text"))


def _clean(check) -> dict:
    """Only the keys each check type uses."""
    kind = check["type"]
    if kind == "command":
        out = {"type": "command", "run": check["run"].strip()}
        if check.get("expect_stdout") is not None:
            out["expect_stdout"] = str(check["expect_stdout"])
        return out
    if kind == "files_exist":
        return {"type": "files_exist", "paths": [p.strip() for p in check["paths"]]}
    return {"type": "text_present", "path": check["path"].strip(), "text": check["text"]}


def _parse_json_object(text):
    text = str(text or "")
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [fenced.group(1)] if fenced else []
    a, b = text.find("{"), text.rfind("}")
    if a >= 0 and b > a:
        candidates.append(text[a:b + 1])
    for c in candidates:
        try:
            obj = json.loads(c)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


# ---------------------------------------------------------------- drafting
def draft_prompt(goal) -> str:
    return ("Write the done-check for this task as one JSON object and nothing else.\n"
            "Allowed shapes:\n"
            '  {"type": "command", "run": "<a command that exits 0 only when the goal is met>"}\n'
            '  {"type": "files_exist", "paths": ["<relative path>", ...]}\n'
            '  {"type": "text_present", "path": "<relative path>", "text": "<exact text>"}\n'
            "Never a human check, never 'looks good', never a command that always passes (echo, true, exit 0).\n"
            f"Goal: {goal}\n")


def _key_word(goal) -> str:
    for w in re.findall(r"[A-Za-z][A-Za-z0-9_]{3,}", goal or ""):
        if w.lower() not in STOP:
            return w.lower()
    return "task"


def heuristic_check(goal) -> dict:
    """The goal's own signals, in order: a backticked command, "<file> mentions `text`", file paths."""
    goal = str(goal or "")
    for span in BACKTICK.findall(goal):
        span = span.strip()
        if COMMAND_START.match(span) and not TRIVIAL_COMMAND.match(span):
            return {"type": "command", "run": span}
    m = TEXT_PRESENT.search(goal)
    if m:
        return {"type": "text_present", "path": m.group(1).replace("\\", "/"), "text": m.group(2)}
    paths = list(dict.fromkeys(p.rstrip(".,;:").replace("\\", "/") for p in PATH.findall(goal)))
    if paths:
        return {"type": "files_exist", "paths": paths}
    return {"type": "command", "run": f"python -m pytest -k {_key_word(goal)} -q"}


def draft_check(goal, runner=None) -> dict:
    """A machine-checkable done-check for ``goal`` (model proposal first when a runner is given)."""
    if runner is not None:
        try:
            proposal = _parse_json_object(runner(draft_prompt(goal)))
        except Exception:
            proposal = None
        if proposal is not None and is_machine_checkable(proposal):
            return _clean(proposal)
    return heuristic_check(goal)


# ---------------------------------------------------------------- one-line cause
def one_line(text, limit=CAUSE_LIMIT) -> str:
    first = next((ln.strip() for ln in str(text or "").splitlines() if ln.strip()), "")
    first = " ".join(first.split())
    if len(first) > limit:
        first = first[: limit - 1].rstrip() + "…"
    return first


def heuristic_cause(detail) -> str:
    detail = str(detail or "").strip()
    if not detail:
        return "The check failed without saying why"
    lines = [ln.strip() for ln in detail.splitlines() if ln.strip()]
    head = lines[0]
    m = re.match(r"missing:\s*(.+)$", head, re.I)
    if m:
        files = [f.strip() for f in m.group(1).split(",") if f.strip()]
        return one_line(("Missing file: " if len(files) == 1 else "Missing files: ") + ", ".join(files))
    m = re.match(r"command timed out after (\S+): (.+)$", head, re.I)
    if m:
        return one_line(f"The check timed out after {m.group(1)}: {m.group(2)}")
    m = re.match(r"exit (-?\d+): (.+)$", head)
    if m:
        code, cmd = m.group(1), m.group(2)
        why = next((re.sub(r"^E\s+", "", ln) for ln in lines[1:] if re.match(r"^E\s+\S", ln)), None)
        if why is None:
            why = next((ln for ln in reversed(lines[1:])
                        if re.search(r"error|assert|failed|not found|cannot|traceback", ln, re.I)), None)
        return one_line(f"`{cmd}` failed: {why}" if why else f"`{cmd}` exited with code {code}")
    m = re.match(r"Needs Owner:\s*(.+)$", head)
    if m:
        return one_line("Waiting for Owner: " + m.group(1))
    return one_line(head)


def cause_prompt(node, detail) -> str:
    return ("In one plain sentence, say why this task's done-check failed. No advice, no 'looks good'.\n"
            f"Task {node.get('id')}: {node.get('goal') or ''}\n"
            f"Check: {json.dumps(node.get('check') or {})}\n"
            f"Check output:\n{str(detail or '')[:4000]}\n")


def diagnose(node, detail, runner=None) -> str:
    """One line (at most ``CAUSE_LIMIT`` characters) naming why the check failed."""
    if runner is not None:
        try:
            line = one_line(runner(cause_prompt(node or {}, detail)))
        except Exception:
            line = ""
        if line and not VAGUE.search(line):
            return line
    return heuristic_cause(detail)


# ---------------------------------------------------------------- fix-up within the goal
def _clip(text, limit):
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def on_check_failed(store, project_id, node_id, detail, runner=None, actor="failure_doctor") -> dict:
    """Write the one-line cause and add a fix-up task within the goal. Returns ``{cause, fixup}``."""
    from . import failure
    node = store.get_node(project_id, node_id)
    if node is None:
        raise KeyError(f"no node {project_id}/{node_id}")
    cause = diagnose(node, detail, runner)
    pattern = re.compile(rf"^{re.escape(node_id)}-fix\d+$")
    existing = [n["id"] for n in store.list_nodes(project_id) if pattern.match(n["id"])]
    fid = None
    if len(existing) < MAX_FIXUPS:
        check = node.get("check") or {}
        if not is_machine_checkable(check):
            check = draft_check(node.get("goal"), runner)
        goal = (f"Within the goal of task {node_id} ({node.get('goal') or ''}), remove this cause so its "
                f"check can pass: {cause}")
        fid = failure.add_fixup(store, project_id, node_id, _clip("Fix: " + cause, TITLE_LIMIT), check,
                                goal=goal, actor=actor)
    store.event(project_id, node_id, "diagnosis", actor, f"Task {node_id} failed: {cause}",
                {"cause": cause, "fixup": fid})
    return {"cause": cause, "fixup": fid}


# ---------------------------------------------------------------- HTTP
def p_check_draft(h, q):
    """{goal} -> {check, machine_checkable}; drafts only, changes nothing."""
    body = h._json_body()
    goal = str(body.get("goal") or "")
    app = getattr(h, "app", None)
    check = draft_check(goal, runner=getattr(app, "doctor_runner", None))
    return {"check": check, "machine_checkable": is_machine_checkable(check)}


ROUTES = {"POST /api/check/draft": p_check_draft}
