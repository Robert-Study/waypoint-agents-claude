"""deep_plan: the staged planning job (brief, research, questions, framework, graph, prompts,
critique). Fake runners only; every model call is routed by the ``Stage:`` line of its prompt."""
import json
import re
import threading

import pytest

from cc_engine import deep_plan, needs, plan_metrics, planner
from cc_engine.api import HttpError
from cc_engine.store import OWNER

BRIEF = {"brief": "Build a small report generator with tests. Out of scope: hosting.",
         "topics": [{"topic": "Existing report code", "where": "code", "ask": "What report code exists?"},
                    {"topic": "Owner's report notes", "where": "drive", "ask": "What did Owner ask for?"},
                    {"topic": "CSV libraries", "where": "web", "ask": "Which stdlib CSV patterns fit?"}],
         "unknowns": []}
RESEARCH = {"findings": ["engine/report.py does not exist yet", "tests live in tests/"],
            "sources": ["engine/"], "unknowns": []}
FRAMEWORK = {"decomposition": "Split by layer: data, report, UI",
             "streams": [{"id": "D", "name": "Data", "owns": ["data/"]}, {"id": "R", "name": "Report", "owns": ["report/"]}],
             "contracts": [{"name": "rows.json", "between": ["D", "R"], "shape": "list of row objects"}]}


def node(nid, deps=(), files=(), check=None, **kw):
    return dict({"id": nid, "title": f"Task {nid}", "goal": f"Task {nid} is done.",
                 "check": check or {"type": "command", "run": f"python -m pytest -k {nid.lower()} -q"},
                 "deps": [{"id": d, "crosses": f"{d} output"} for d in deps], "files": list(files), "lane": "core",
                 "recommended": "claude-sonnet"}, **kw)


def graph(*nodes):
    return {"title": "Reports", "brief": "Build a small report generator. Out of scope: hosting.", "nodes": list(nodes)}


WIDE = graph(node("A1", files=["tests/test_rows.py", "data/rows.py"]),
             node("A2", files=["tests/test_report.py", "report/render.py"]),
             node("A3", ["A1", "A2"], ["report/cli.py"]))
OVERLAP = graph(node("B1", files=["shared.py"]), node("B2", files=["shared.py"]), node("B3", ["B1", "B2"], ["x.py"]))
CYCLE = graph(node("C1", ["C2"], ["c1.py"]), node("C2", ["C1"], ["c2.py"]))
PROMPTS = {"tasks": [{"id": "A1", "inputs": ["Owner's rows spec"], "exit": ["rows.py has no third-party imports"]},
                     {"id": "A3", "inputs": ["render() from A2"], "exit": ["cli prints a table"]}]}
READY = {"verdict": "ready", "summary": "Sound plan.", "issues": []}


class Fake:
    """Replies by stage; a list reply is consumed one item per call. Thread-safe (research fans out)."""

    def __init__(self, replies, plain_for=()):
        self.replies = {k: (list(v) if isinstance(v, list) else v) for k, v in replies.items()}
        self.calls = []
        self.plain_for = set(plain_for)
        self.lock = threading.Lock()

    def __call__(self, argv, cwd, stdin_text, timeout):
        stage = re.search(r"^Stage: (\w+)", stdin_text, re.M).group(1)
        with self.lock:
            self.calls.append({"stage": stage, "argv": list(argv), "prompt": stdin_text, "cwd": cwd})
            r = self.replies.get(stage, {})
            if isinstance(r, list):
                r = r.pop(0) if len(r) > 1 else r[0]
        if isinstance(r, Exception):
            return 1, "", str(r)
        text = r if isinstance(r, str) else json.dumps(r)
        if "freebuff" in " ".join(argv):
            return 0, text + "\n", ""
        return 0, json.dumps({"type": "result", "is_error": False, "result": text}), ""

    def stages(self):
        return [c["stage"] for c in self.calls]

    def of(self, stage):
        return [c for c in self.calls if c["stage"] == stage]


def replies(**over):
    base = {"brief": BRIEF, "research": RESEARCH, "framework": FRAMEWORK, "graph": [OVERLAP, CYCLE, WIDE],
            "prompts": PROMPTS, "critique": READY}
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def hermetic(monkeypatch):
    monkeypatch.setattr(deep_plan, "SPAWN", lambda fn: fn())
    monkeypatch.setattr(deep_plan, "PICK", lambda app: None)
    monkeypatch.setattr(deep_plan, "DISABLED", lambda option: False)
    monkeypatch.setattr(planner, "recommend_options", lambda: [dict(m) for m in planner.FALLBACK_MODELS])


@pytest.fixture
def fake(monkeypatch):
    def use(**over):
        f = Fake(replies(**over))
        monkeypatch.setattr(deep_plan, "RUNNER", f)
        return f
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


def start(store, **kw):
    args = dict(title="Reports", goal="Owner gets a CSV report from his data.")
    args.update(kw)
    return deep_plan.start(store, **args)


# ---------------------------------------------------------------- contract
def test_deep_plan_routes_follow_contract():
    assert {"GET /api/plan/deep", "POST /api/plan/deep/start", "POST /api/plan/deep/continue",
            "GET /api/plan/deep/options", "GET /api/plan/deep/brief"} <= set(deep_plan.ROUTES)


def test_deep_plan_stage_order():
    assert [s for s in deep_plan.STAGES] == ["brief", "research", "questions", "framework", "graph",
                                             "prompts", "critique"]


def test_deep_plan_prompts_are_editable_files():
    for name in deep_plan.STAGES:
        text = (planner.PROMPT_DIR / f"deep_{name}.md").read_text(encoding="utf-8")
        assert text.startswith(f"Stage: {name}") and "ONLY" in text and len(text) < 6000
    assert (planner.PROMPT_DIR / "deep_revise.md").read_text(encoding="utf-8").startswith("Stage: revise")


def test_deep_plan_keeps_planner_api():
    argv = planner.claude_argv()
    assert argv[1:6] == ["-p", "--model", "opus", "--output-format", "json"]
    argv = planner.claude_argv(model="sonnet", effort="low")
    assert argv[1:4] == ["-p", "--model", "sonnet"] and argv[argv.index("--effort") + 1] == "low"
    assert set(planner.ROUTES) == {"POST /api/planner/start", "POST /api/planner/draft",
                                   "GET /api/planner/session", "GET /api/planner/sessions"}


# ---------------------------------------------------------------- options
def test_deep_plan_options_list_models_and_efforts(monkeypatch):
    out = deep_plan.options()
    ids = [m["id"] for m in out["models"]]
    assert {"claude-opus", "claude-sonnet", "claude-fable", "freebuff"} <= set(ids)
    assert not any("codex" in i for i in ids)
    assert out["efforts"] == ["low", "medium", "high", "xhigh", "max"]
    assert out["default"]["model"] == "claude-opus" and out["default"]["source"] == "fixed"
    monkeypatch.setattr(deep_plan, "PICK", lambda app: {"model": "sonnet", "effort": "medium", "why": "plenty left"})
    d = deep_plan.options()["default"]
    assert d == {"model": "claude-sonnet", "effort": "medium", "why": "plenty left", "source": "pick"}
    monkeypatch.setattr(deep_plan, "PICK", lambda app: {"model": "mystery", "effort": "huge"})
    assert deep_plan.options()["default"]["model"] == "claude-opus"


def test_deep_plan_disabled_models_are_marked_and_refused(store, fake, monkeypatch):
    fake()
    monkeypatch.setattr(deep_plan, "DISABLED", lambda option: option["id"] == "claude-fable")
    rows = {m["id"]: m for m in deep_plan.options()["models"]}
    assert rows["claude-fable"]["enabled"] is False and rows["claude-opus"]["enabled"] is True
    with pytest.raises(ValueError):
        start(store, model="claude-fable")


# ---------------------------------------------------------------- the full pipeline
def test_deep_plan_runs_every_stage_and_imports_a_draft(store, fake):
    f = fake()
    view = start(store, model="claude-sonnet", effort="max")
    assert view["status"] == "ready", view.get("error")
    states = {s["id"]: s["state"] for s in view["stages"]}
    assert states == {"brief": "done", "research": "done", "questions": "skipped", "framework": "done",
                      "graph": "done", "prompts": "done", "critique": "done"}
    assert all(s["line"] and "\n" not in s["line"] for s in view["stages"])
    assert f.stages()[0] == "brief" and f.stages().count("research") == 3 and f.stages().count("graph") == 3
    assert not any("{{" in c["prompt"] for c in f.calls)  # every placeholder filled
    # Draft imported but not runnable until Owner accepts it.
    pid = view["project"]
    assert store.project_status(pid) == "draft" and view["runnable"] is False
    assert {n["id"] for n in store.list_nodes(pid)} == {"A1", "A2", "A3", "G1"}  # + the ship gate
    assert view["draft"]["tasks"] == 4
    assert [n["files"] for n in view["draft"]["nodes"]][0] == ["tests/test_rows.py", "data/rows.py"]
    # Scores of the chosen graph.
    sc = view["scores"]
    assert sc["critical_path"] == 2 and sc["peak_width"] == 2 and sc["overlaps"] == 0 and sc["waves"] == 2
    ids = [dict(n, deps=[d["id"] for d in n["deps"]]) for n in WIDE["nodes"]]  # plan_metrics reads plain ids
    assert sc["line"] == plan_metrics.line(plan_metrics.score(ids))


def test_deep_plan_model_and_effort_reach_the_planner_calls(store, fake):
    f = fake()
    start(store, model="claude-sonnet", effort="max")
    for c in f.of("brief") + f.of("framework") + f.of("graph") + f.of("prompts"):
        argv = c["argv"]
        assert argv[argv.index("--model") + 1] == "sonnet" and argv[argv.index("--effort") + 1] == "max"


def test_deep_plan_research_fans_out_read_only(store, fake):
    f = fake()
    view = start(store)
    calls = f.of("research")
    assert len(calls) == 3
    for c in calls:
        argv = c["argv"]
        blocked = argv[argv.index("--disallowedTools") + 1]
        assert {"Edit", "Write", "NotebookEdit", "Bash"} <= set(blocked.split(","))
        assert "read-only" in c["prompt"].lower()
    asks = sorted(c["prompt"] for c in calls)
    assert any("Existing report code" in p for p in asks) and any("CSV libraries" in p for p in asks)
    line = next(s for s in view["stages"] if s["id"] == "research")["line"]
    assert line.startswith("3 of 3")
    # The findings reach the framework and graph prompts.
    assert "engine/report.py does not exist yet" in f.of("framework")[0]["prompt"]
    assert "engine/report.py does not exist yet" in f.of("graph")[0]["prompt"]


def test_deep_plan_research_topics_are_clamped_2_to_4(store, fake):
    many = dict(BRIEF, topics=[{"topic": f"T{i}", "where": "code", "ask": "?"} for i in range(7)])
    f = fake(brief=many)
    start(store)
    assert len(f.of("research")) == 4
    f = fake(brief=dict(BRIEF, topics=[]))
    start(store)
    assert len(f.of("research")) == 2


def test_deep_plan_critic_is_a_second_model(store, fake):
    f = fake()
    view = start(store, model="claude-opus")
    argv = f.of("critique")[0]["argv"]
    assert argv[argv.index("--model") + 1] != "opus"
    assert view["critic"] != view["model"]
    assert view["critique"]["verdict"] == "ready"
    with pytest.raises(ValueError):
        start(store, model="claude-opus", critic="claude-opus")


def test_deep_plan_free_model_runs_through_freebuff(store, fake):
    f = fake()
    view = start(store, model="freebuff")
    assert view["status"] == "ready", view.get("error")
    for c in f.of("brief") + f.of("graph"):
        assert "freebuff" in " ".join(c["argv"]) and "claude" not in c["argv"][0].lower()
    assert "Folder listing" in f.of("research")[0]["prompt"] or f.of("research")[0]["cwd"] is None


# ---------------------------------------------------------------- graph synthesis
def test_deep_plan_keeps_the_best_of_three_candidates(store, fake):
    f = fake()
    view = start(store)
    cands = view["candidates"]
    assert [c["n"] for c in cands] == [1, 2, 3]
    assert cands[0]["valid"] is True and cands[0]["repairs"] >= 1   # overlap repaired
    assert cands[1]["valid"] is False                               # cycle rejected
    assert [c["chosen"] for c in cands] == [False, False, True]     # wide graph wins
    # Later candidates are told how the earlier ones scored.
    assert "critical path" in f.of("graph")[1]["prompt"] and "cycle" in f.of("graph")[2]["prompt"].lower()
    line = next(s for s in view["stages"] if s["id"] == "graph")["line"]
    assert "3 candidates" in line and "critical path 2" in line


def test_deep_plan_repairs_overlaps_in_the_chosen_graph(store, fake):
    fake(graph=[OVERLAP])
    view = start(store, candidates=1)
    assert view["status"] == "ready", view.get("error")
    assert view["scores"]["overlaps"] == 0
    deps = {n["id"]: n["deps"] for n in view["draft"]["nodes"]}
    assert "B1" in deps["B2"] or "B2" in deps["B1"]
    assert view["candidates"][0]["changes"]


def test_deep_plan_all_candidates_invalid_is_problems(store, fake):
    fake(graph=[CYCLE])
    view = start(store, candidates=2)
    assert view["status"] == "problems" and view["project"] is None
    assert any("cycle" in p for p in view["problems"])
    assert next(s for s in view["stages"] if s["id"] == "graph")["state"] == "error"


# ---------------------------------------------------------------- prompt engineering
def test_deep_plan_writes_a_bounded_brief_per_task(store, fake):
    fake()
    view = start(store)
    pid = view["project"]
    b = deep_plan.task_brief(store, pid, "A1")
    text = b["text"]
    assert "tests/test_rows.py" in text and "data/rows.py" in text         # owned files
    assert "python -m pytest -k a1 -q" in text                              # done-check
    assert "Owner's rows spec" in text and "rows.py has no third-party imports" in text
    assert len(text) <= deep_plan.MAX_BRIEF_CHARS
    a3 = deep_plan.task_brief(store, pid, "A3")["text"]
    assert "A1" in a3 and "A2" in a3                                        # inputs from deps
    # A2 had no prompt-stage entry: it still gets a complete brief.
    a2 = deep_plan.task_brief(store, pid, "A2")["text"]
    assert "report/render.py" in a2 and "Exit criteria" in a2
    out = deep_plan.g_brief(H(store), {"project": pid, "id": "A1"})
    assert out["text"] == text
    with pytest.raises(KeyError):
        deep_plan.g_brief(H(store), {"project": pid, "id": "ZZ"})


def test_deep_plan_handoffs_appear_in_briefs(store, fake):
    two_chains = graph(node("A1", files=["a1.py", "config.json"]), node("A2", ["A1"], ["a2.py"]),
                       node("B1", files=["b1.py", "b2.py", "config.json"]), node("B2", ["B1"], ["b3.py"]))
    fake(graph=[two_chains])
    view = start(store, candidates=1)
    pid = view["project"]
    texts = [deep_plan.task_brief(store, pid, i)["text"] for i in ("A1", "B1")]
    assert sum("Hand off" in t for t in texts) == 1


# ---------------------------------------------------------------- critique
def test_deep_plan_critique_can_revise_the_graph(store, fake):
    revised = graph(node("A1", files=["data/rows.py"]), node("A2", files=["report/render.py"]),
                    node("A3", files=["report/cli.py"]), node("A4", ["A1", "A2", "A3"], ["README.md"],
                                                           inputs=["all three"], exit=["README explains usage"]))
    f = fake(critique={"verdict": "revise", "summary": "Missing docs task.",
                       "issues": [{"task": None, "issue": "No docs", "fix": "Add a docs task"}]},
             revise=revised)
    view = start(store)
    assert view["status"] == "ready"
    assert len(f.of("revise")) == 1
    assert not any("{{" in c["prompt"] for c in f.calls)
    assert view["draft"]["tasks"] == 5 and view["critique"]["issues"] == 1  # 4 + the ship gate
    assert "README explains usage" in deep_plan.task_brief(store, view["project"], "A4")["text"]
    line = next(s for s in view["stages"] if s["id"] == "critique")["line"]
    assert "Revised" in line


def test_deep_plan_bad_revision_keeps_the_original(store, fake):
    fake(critique={"verdict": "revise", "summary": "x", "issues": [{"issue": "y", "fix": "z"}]}, revise=CYCLE)
    view = start(store)
    assert view["status"] == "ready" and view["draft"]["tasks"] == 4  # 3 + the ship gate


# ---------------------------------------------------------------- questions
def test_deep_plan_asks_only_what_research_could_not_find(store, fake):
    research = dict(RESEARCH, unknowns=["How polished must the first version be?"])
    qs = {"questions": [
        {"kind": "likert", "prompt": "How polished should v1 be?", "purpose": "generic", "labels": ["Rough", "Finished"]},
        {"kind": "select", "prompt": "Which outputs?", "purpose": "generic", "options": ["CSV (recommended)", "Chart"]},
        {"kind": "text", "prompt": "Anything else?", "purpose": "generic"}]}
    f = fake(research=research, questions=qs)
    view = start(store)
    assert view["status"] == "questions"
    assert "How polished must the first version be?" in f.of("questions")[0]["prompt"]
    assert "{{" not in f.of("questions")[0]["prompt"]
    assert [q["kind"] for q in view["questions"]] == ["likert", "select"]   # 1-9 or tick-box only
    assert view["questions"][0]["scale"] == 9
    assert view["questions"][1]["options"] == ["CSV", "Chart"]             # nothing marked recommended
    assert not any(q.get("answer") for q in view["questions"])
    assert "framework" not in f.stages()
    st = {s["id"]: s for s in view["stages"]}
    assert st["questions"]["state"] == "waiting" and st["questions"]["line"].startswith("2 questions")
    needs.answer(store, view["questions"][0]["id"], {"type": "answer", "value": 8}, capture=False)
    done = deep_plan.resume(store, view["id"])
    assert done["status"] == "ready", done.get("error")
    assert "Answer: 8" in f.of("framework")[0]["prompt"]


def test_deep_plan_no_unknowns_means_no_question_call(store, fake):
    f = fake()
    start(store)
    assert "questions" not in f.stages()


# ---------------------------------------------------------------- streaming, errors, routes
def test_deep_plan_streams_stage_status(store, fake):
    fake()
    seen = []
    store.subscribe(lambda kind, data: seen.append((kind, data)) if kind == "deep_plan" else None)
    view = start(store)
    stages = [d.get("stage") for k, d in seen]
    assert {"brief", "research", "framework", "graph", "prompts", "critique"} <= set(stages)
    assert seen[-1][1]["status"] == "ready" and seen[-1][1]["id"] == view["id"]
    assert all(set(d) >= {"id", "status", "stage", "stages"} for _, d in seen)


def test_deep_plan_job_failure_is_a_plain_error_and_can_resume(store, fake, monkeypatch):
    f = fake(framework=RuntimeError("boom"))
    view = start(store)
    assert view["status"] == "error" and view["error"]
    assert next(s for s in view["stages"] if s["id"] == "framework")["state"] == "error"
    f.replies["framework"] = FRAMEWORK
    again = deep_plan.resume(store, view["id"])
    assert again["status"] == "ready"
    assert f.stages().count("brief") == 1  # earlier stages were not redone


def test_deep_plan_interrupted_session_reports_error(store, fake, monkeypatch):
    fake()
    monkeypatch.setattr(deep_plan, "SPAWN", lambda fn: None)  # job never runs: like a restart
    view = start(store)
    deep_plan._live.clear()
    view = deep_plan.view(store, view["id"])
    assert view["status"] == "error" and "restarted" in view["error"]


def test_deep_plan_routes_are_owner_only_and_validate(store, fake, tmp_path):
    fake()
    with pytest.raises(HttpError) as e:
        deep_plan.p_start(H(store, {"title": "T", "goal": "G"}, actor="router"), {})
    assert e.value.status == 403
    with pytest.raises(HttpError) as e:
        deep_plan.p_start(H(store, {"title": "T"}), {})
    assert e.value.status == 400
    with pytest.raises(HttpError) as e:
        deep_plan.p_start(H(store, {"title": "T", "goal": "G", "cwd": str(tmp_path / "missing")}), {})
    assert e.value.status == 400
    with pytest.raises(HttpError) as e:
        deep_plan.p_start(H(store, {"title": "T", "goal": "G", "model": "gpt-codex"}), {})
    assert e.value.status == 400
    out = deep_plan.p_start(H(store, {"title": "T", "goal": "G", "cwd": str(tmp_path), "model": "claude-sonnet",
                                      "effort": "low"}), {})
    sid = out["session"]["id"]
    got = deep_plan.g_session(H(store), {"id": sid})
    assert got["status"] == "ready" and got["model"] == "claude-sonnet" and got["effort"] == "low"
    with pytest.raises(HttpError) as e:
        deep_plan.p_continue(H(store, {"id": sid}, actor="router"), {})
    assert e.value.status == 403
    with pytest.raises(HttpError) as e:
        deep_plan.p_continue(H(store, {"id": sid}), {})
    assert e.value.status == 409   # nothing to continue
    with pytest.raises(KeyError):
        deep_plan.g_session(H(store), {"id": "dp-none"})
    assert deep_plan.g_sessions(H(store), {})["sessions"][0]["id"] == sid
    assert deep_plan.g_options(H(store), {})["models"]


def test_deep_plan_session_never_bumps_revision_until_import(store, fake, monkeypatch):
    fake()
    monkeypatch.setattr(deep_plan, "SPAWN", lambda fn: None)
    rev = store.revision()
    start(store)
    assert store.revision() == rev


def test_deep_plan_accepted_draft_reads_accepted(store, fake):
    from cc_engine import plan
    fake()
    view = start(store)
    plan.accept_plan(store, view["project"], OWNER)
    assert deep_plan.view(store, view["id"])["status"] == "accepted"
