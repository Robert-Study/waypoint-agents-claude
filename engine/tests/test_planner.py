"""Plan mode: discovery questions, drafted plan graph, draft import. Fake runner only."""
import json

import pytest

from cc_engine import needs, planner
from cc_engine.api import HttpError
from cc_engine.store import OWNER

QUESTIONS = {"questions": [
    {"kind": "likert", "prompt": "How polished should the first version be?", "purpose": "preference",
     "labels": ["Rough sketch", "Finished product"], "why": "sets the bar"},
    {"kind": "select", "prompt": "Which outputs do you want?", "purpose": "generic",
     "options": ["Report (recommended)", {"value": "Chart", "selected": True}, "Table", "Report"]},
    {"kind": "text", "prompt": "Where does the data come from?", "purpose": "generic"},
    {"kind": "select", "prompt": "Too few options", "options": ["one"]},
    {"kind": "bogus", "prompt": "dropped"},
]}

GOOD_PLAN = {"title": "x", "brief": "Build a small report generator. Out of scope: hosting.", "nodes": [
    {"id": "A1", "title": "Tests first", "goal": "Tests describe the report.",
     "check": {"type": "files_exist", "paths": ["tests/test_report.py"]}, "deps": [], "lane": "core",
     "pool": "codex", "model": "gpt"},
    {"id": "A2", "title": "Report", "goal": "The report generator passes its tests.",
     "check": {"type": "command", "run": "python -m pytest -q"},
     "deps": [{"id": "A1", "crosses": "tests/test_report.py"}]},
    {"id": "A3", "title": "Owner looks", "goal": "Owner is happy with the report layout.",
     "check": {"type": "human", "prompt": "The layout reads well"},
     "deps": [{"id": "A2", "crosses": "the rendered report"}]},
]}


class FakeRunner:
    """Answers each call with the next scripted reply, wrapped like ``--output-format json``."""

    def __init__(self, *replies, code=0, err=""):
        self.replies = list(replies)
        self.calls = []
        self.code, self.err = code, err

    def __call__(self, argv, cwd, stdin_text, timeout):
        self.calls.append({"argv": list(argv), "cwd": cwd, "prompt": stdin_text, "timeout": timeout})
        reply = self.replies.pop(0) if self.replies else {}
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return self.code, json.dumps({"type": "result", "is_error": False, "result": text}), self.err


@pytest.fixture
def sync(monkeypatch):
    monkeypatch.setattr(planner, "SPAWN", lambda fn: fn())

    def use(*replies, **kw):
        runner = FakeRunner(*replies, **kw)
        monkeypatch.setattr(planner, "RUNNER", runner)
        return runner
    return use


class App:
    def __init__(self, store):
        self.store = store


class H:
    def __init__(self, store, body=None, actor=OWNER):
        self.app = App(store)
        self.actor = actor
        self._body = body or {}

    def _json_body(self):
        return self._body


def answer_all(store, view):
    for q in view["questions"]:
        value = {"likert": {"type": "answer", "value": 7}, "select": {"type": "answer", "value": ["Chart"]},
                 "text": {"type": "reply", "value": "A CSV export"}}[q["kind"]]
        needs.answer(store, q["id"], value, capture=False)


def test_routes_follow_contract():
    assert set(planner.ROUTES) == {"POST /api/planner/start", "POST /api/planner/draft",
                                   "GET /api/planner/session", "GET /api/planner/sessions"}


def test_argv_is_claude_code_job_with_strong_model():
    argv = planner.claude_argv()
    assert argv[1:6] == ["-p", "--model", "opus", "--output-format", "json"]
    assert "--disallowedTools" in argv
    assert not any("api.anthropic.com" in a for a in argv)


def test_start_asks_at_most_six_questions_without_preselection(store, sync, tmp_path):
    runner = sync(QUESTIONS)
    view = planner.start(store, "Report tool", "Make a weekly report", str(tmp_path))
    assert view["status"] == "questions"
    qs = view["questions"]
    assert [q["kind"] for q in qs] == ["likert", "select", "text"]
    assert [q["purpose"] for q in qs] == ["preference", "generic", "generic"]
    assert qs[0]["labels"] == ["Rough sketch", "Finished product"] and qs[0]["scale"] == 9
    assert qs[1]["options"] == ["Report", "Chart", "Table"]  # recommendation text and dupes dropped
    stored = needs.get_question(store, qs[1]["id"])
    assert stored["purpose"] == "generic"
    assert all(set(o) <= {"value", "label", "description"} for o in stored["options"])
    assert needs.get_question(store, qs[0]["id"])["purpose"] == "preference"
    assert runner.calls[0]["cwd"] == str(tmp_path)
    assert "Make a weekly report" in runner.calls[0]["prompt"]


def test_question_cap(store, sync):
    many = {"questions": [{"kind": "text", "prompt": f"Question {i}?"} for i in range(10)]}
    sync(many)
    view = planner.start(store, "T", "G")
    assert len(view["questions"]) == planner.MAX_QUESTIONS


def test_draft_imports_a_draft_project_that_is_not_runnable(store, sync):
    runner = sync(QUESTIONS, GOOD_PLAN)
    view = planner.start(store, "Report tool", "Make a weekly report")
    answer_all(store, view)
    out = planner.draft(store, view["id"])
    assert out["status"] == "draft" and out["problems"] == []
    pid = out["project"]
    assert store.project_status(pid) == "draft"
    assert store.project_runnable(pid) is False
    nodes = {n["id"]: n for n in store.list_nodes(pid)}
    assert set(nodes) == {"A1", "A2", "A3"}
    assert nodes["A1"]["pool"] is None and nodes["A1"]["model"] is None  # planner never picks pools
    assert out["draft"]["checks"] == {"command": 1, "files_exist": 1, "text_present": 0, "human": 1}
    assert out["draft"]["nodes"][1]["deps"] == ["A1"]
    prompt = runner.calls[1]["prompt"]
    assert "Answer: 7" in prompt and "Chart" in prompt and "A CSV export" in prompt
    # Owner accepts -> runnable, and the session reports accepted
    store.accept_project(pid, OWNER)
    assert planner.session_view(store, view["id"])["status"] == "accepted"
    with pytest.raises(HttpError) as e:
        planner.draft(store, view["id"])
    assert e.value.status == 409


def test_invalid_draft_is_repaired_once_then_reported(store, sync):
    bad = {"brief": "", "nodes": [{"id": "A1", "goal": "x", "check": {"type": "vibes"}, "deps": ["Z9"]}]}
    runner = sync({"questions": []}, bad, bad)
    view = planner.start(store, "T", "G")
    assert view["status"] == "questions" and view["questions"] == []
    out = planner.draft(store, view["id"])
    assert out["status"] == "problems"
    assert any("brief" in p for p in out["problems"])
    assert any("done-check" in p for p in out["problems"])
    assert any("unknown" in p for p in out["problems"])
    assert len(runner.calls) == 3  # questions, draft, one repair
    assert "could not be imported" in runner.calls[2]["prompt"]
    assert out["project"] is None


def test_repair_succeeds(store, sync):
    bad = {"brief": "b", "nodes": [{"id": "A1", "goal": "x", "check": {"type": "command"}}]}
    sync({"questions": []}, bad, GOOD_PLAN)
    view = planner.start(store, "T", "G")
    assert planner.draft(store, view["id"])["status"] == "draft"


def test_cycle_is_a_problem():
    cyc = {"brief": "b", "nodes": [
        {"id": "A", "goal": "a", "check": {"type": "command", "run": "exit 0"}, "deps": ["B"]},
        {"id": "B", "goal": "b", "check": {"type": "command", "run": "exit 0"}, "deps": ["A"]}]}
    assert planner.problems(cyc)
    assert planner.problems(GOOD_PLAN) == []
    assert planner.problems({"brief": "b", "nodes": [
        {"id": "A", "goal": "a", "check": {"type": "human"}}]}) == [
        "task A: a human check needs a 'prompt' saying what Owner confirms"]


def test_redraft_archives_previous_draft(store, sync):
    runner = sync({"questions": []}, GOOD_PLAN, GOOD_PLAN)
    view = planner.start(store, "Report tool", "G")
    first = planner.draft(store, view["id"])["project"]
    second = planner.draft(store, view["id"], note="Fewer tasks please")
    assert second["project"] == first + "-r2" and second["drafts"] == 2
    assert store.get_project(first)["archived"] == 1
    assert store.project_status(second["project"]) == "draft"
    assert "Fewer tasks please" in runner.calls[-1]["prompt"]


def test_job_failure_becomes_plain_error(store, sync):
    sync(code=1, err="boom: not logged in")
    view = planner.start(store, "T", "G")
    assert view["status"] == "error" and "not logged in" in view["error"]
    with pytest.raises(HttpError):  # no questions ever arrived: start again instead
        planner.draft(store, view["id"])


def test_malformed_reply_is_error(store, sync):
    sync("I think you should build it in Rust.")
    view = planner.start(store, "T", "G")
    assert view["status"] == "error"


def test_parse_reply_handles_fences_and_bare_json():
    assert planner.parse_reply(json.dumps({"result": "```json\n{\"a\": 1}\n```"})) == {"a": 1}
    assert planner.parse_reply('{"questions": []}') == {"questions": []}
    with pytest.raises(ValueError):
        planner.parse_reply(json.dumps({"result": "nope", "is_error": True}))


def test_interrupted_session_reports_error(store, sync):
    sync({"questions": []})
    view = planner.start(store, "T", "G")
    sess = planner.load_session(store, view["id"])
    sess["status"] = "drafting"
    planner.save_session(store, sess)  # not live in this process: as after an engine restart
    assert planner.session_view(store, view["id"])["status"] == "error"


def test_session_never_bumps_revision(store, sync):
    sync({"questions": [{"kind": "text", "prompt": "Why?"}]})
    rev = store.revision()
    planner.start(store, "T", "G")
    assert store.revision() == rev


def test_route_handlers(store, sync, tmp_path):
    sync(QUESTIONS, GOOD_PLAN)
    with pytest.raises(HttpError) as e:
        planner.p_start(H(store, {"title": "T"}), {})
    assert e.value.status == 400
    with pytest.raises(HttpError):
        planner.p_start(H(store, {"title": "T", "goal": "G", "cwd": str(tmp_path / "missing")}), {})
    out = planner.p_start(H(store, {"title": "T", "goal": "G", "cwd": str(tmp_path)}), {})
    sid = out["session"]["id"]
    assert planner.g_session(H(store), {"id": sid})["status"] == "questions"
    drafted = planner.p_draft(H(store, {"session": sid}), {})["session"]
    assert drafted["status"] == "draft" and drafted["runnable"] is False
    listed = planner.g_sessions(H(store), {})["sessions"]
    assert listed[0]["id"] == sid and listed[0]["status"] == "draft"
    with pytest.raises(KeyError):
        planner.g_session(H(store), {"id": "ps-none"})


def test_prompts_are_editable_files():
    for name in ("questions", "draft"):
        text = (planner.PROMPT_DIR / f"planner_{name}.md").read_text(encoding="utf-8")
        assert "ONLY this JSON object" in text and len(text) < 4000
