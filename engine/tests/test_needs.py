import json
from pathlib import Path

import pytest

from cc_engine import needs


class FakeRunner:
    def __init__(self, returncode=0):
        self.calls = []
        self.payloads = []
        self.returncode = returncode

    def __call__(self, argv):
        self.calls.append(list(argv))
        self.payloads.append(json.loads(Path(argv[-1]).read_text(encoding="utf-8")))
        return self.returncode, "ok", ""


def test_needs_options_never_carry_preselect(store):
    qid = needs.ask(store, "p", "N1", "select", "Which pools may this run use?",
                    options=[{"value": "claude_code", "label": "Claude", "selected": True},
                             {"value": "groq", "default": True, "checked": True}, "freebuff"])
    q = needs.get_question(store, qid)
    for opt in q["options"]:
        assert set(opt) <= {"value", "label", "description"}
    assert [o["value"] for o in q["options"]] == ["claude_code", "groq", "freebuff"]
    assert q["answer"] is None


def test_needs_likert_validation(store):
    qid = needs.ask(store, "p", None, "likert", "How calm should the board feel?", scale=9)
    with pytest.raises(ValueError):
        needs.answer(store, qid, 10, capture=False)
    with pytest.raises(ValueError):
        needs.answer(store, qid, True, capture=False)
    needs.answer(store, qid, 7, capture=False)
    assert needs.get_question(store, qid)["answer"] == {"type": "answer", "value": 7}


def test_needs_select_answer_must_be_subset(store):
    qid = needs.ask(store, "p", None, "select", "Pick", options=["a", "b"])
    with pytest.raises(ValueError):
        needs.answer(store, qid, ["c"], capture=False)
    needs.answer(store, qid, ["a", "b"], capture=False)


def test_needs_answer_invokes_capture_runner_with_owner_text(store, tmp_path):
    runner = FakeRunner()
    qid = needs.ask(store, "proj", "N1", "text", "What should Learn show first?", purpose="preference")
    needs.answer(store, qid, "Show me one small step at a time.", runner=runner, capture_dir=tmp_path)
    assert len(runner.calls) == 1
    argv = runner.calls[0]
    assert "--capture" in argv
    assert argv[argv.index("--capture") - 1].endswith("sync_preferences.py")
    payload = runner.payloads[0]
    assert "Show me one small step at a time." in payload["quote"]
    assert payload["speaker"] == "user" and payload["origin"] == "direct_user_dialogue"
    assert 2 <= len(payload["paragraphs"]) <= 3 and payload["tags"]
    assert payload["scope"] == "project:proj"
    q = needs.get_question(store, qid)
    assert q["captured"] == 1 and q["answered_at"]
    assert not list(tmp_path.glob("*.json")), "temporary capture payload should be removed after success"


def test_needs_capture_false_does_not_run(store, tmp_path):
    runner = FakeRunner()
    qid = needs.ask(store, "p", None, "text", "Anything?")
    needs.answer(store, qid, "No.", capture=False, runner=runner, capture_dir=tmp_path)
    assert runner.calls == []
    assert needs.get_question(store, qid)["captured"] == 0


def test_needs_failed_capture_keeps_answer_uncaptured(store, tmp_path):
    runner = FakeRunner(returncode=1)
    qid = needs.ask(store, "p", None, "likert", "Rate it", scale=5, purpose="preference")
    needs.answer(store, qid, 4, runner=runner, capture_dir=tmp_path)
    q = needs.get_question(store, qid)
    assert q["answer"] == {"type": "answer", "value": 4} and q["captured"] == 0


def test_needs_questions_do_not_bump_revision(store, tmp_path):
    qid = needs.ask(store, "p", None, "text", "Anything?")
    needs.answer(store, qid, "Fine.", runner=FakeRunner(), capture_dir=tmp_path)
    assert store.revision() == 0
    assert [q["id"] for q in needs.list_questions(store, open_only=True)] == []
    assert [q["id"] for q in needs.list_questions(store)] == [qid]


def test_needs_locate_capture_script_reads_skill(tmp_path):
    skill = tmp_path / "organising-preferences"
    skill.mkdir()
    script = tmp_path / "tools" / "sync_preferences.py"
    script.parent.mkdir()
    script.write_text("# fake", encoding="utf-8")
    (skill / "SKILL.md").write_text("Run & 'py.exe' '%s' --capture 'x.json'\n" % script, encoding="utf-8")
    assert Path(needs.locate_capture_script(skill)) == script
    local = skill / "scripts" / "sync_preferences.py"
    local.parent.mkdir()
    local.write_text("# fake", encoding="utf-8")
    assert Path(needs.locate_capture_script(skill)) == local


def test_needs_bad_kind_rejected(store):
    with pytest.raises(ValueError):
        needs.ask(store, "p", None, "radio", "x")


# ---------------------------------------------------------------- answer types (C9)
PROPOSAL = {"pool": "claude", "model": "opus", "effort": "high"}


def test_answer_types_defaults_by_kind(store):
    text = needs.get_question(store, needs.ask(store, "p", None, "text", "Anything?"))
    assert text["allowed"] == ["reply", "ignore"] and text["proposal"] is None
    likert = needs.get_question(store, needs.ask(store, "p", None, "likert", "Rate", scale=5))
    assert likert["allowed"] == ["ignore", "answer"]
    proposed = needs.get_question(store, needs.ask(store, "p", "N1", "text", "Run N1 like this?", proposal=PROPOSAL))
    assert proposed["allowed"] == ["accept", "edit", "reply", "ignore"]
    assert proposed["proposal"] == PROPOSAL


def test_answer_types_allowed_validated_and_ordered(store):
    qid = needs.ask(store, "p", None, "text", "Go?", allowed=["ignore", "edit", "accept", "accept"], proposal=PROPOSAL)
    assert needs.get_question(store, qid)["allowed"] == ["accept", "edit", "ignore"]
    for kwargs in ({"allowed": ["approve"]}, {"allowed": ["accept"]}, {"allowed": []}, {"proposal": {}},
                   {"allowed": "reply"}):
        with pytest.raises(ValueError):
            needs.ask(store, "p", None, "text", "Go?", **kwargs)


def test_answer_types_accept_records_proposal_and_captures(store, tmp_path):
    runner = FakeRunner()
    qid = needs.ask(store, "proj", "N1", "text", "Run N1 like this?", proposal=PROPOSAL, purpose="preference")
    q = needs.answer(store, qid, {"type": "accept"}, runner=runner, capture_dir=tmp_path)
    assert q["answer"] == {"type": "accept", "value": PROPOSAL}
    assert q["answered_at"] and q["captured"] == 1
    assert len(runner.calls) == 1 and "accepted as proposed" in runner.payloads[0]["quote"]


def test_answer_types_edit_changes_only_proposal_fields(store, tmp_path):
    runner = FakeRunner()
    qid = needs.ask(store, "proj", "N1", "text", "Run N1 like this?", proposal=PROPOSAL, purpose="preference")
    for bad in ({"budget": 5}, {}, "medium"):
        with pytest.raises(ValueError):
            needs.answer(store, qid, {"type": "edit", "value": bad}, runner=runner, capture_dir=tmp_path)
    assert needs.get_question(store, qid)["answered_at"] is None and runner.calls == []
    q = needs.answer(store, qid, {"type": "edit", "value": {"effort": "medium"}}, runner=runner, capture_dir=tmp_path)
    assert q["answer"] == {"type": "edit", "value": dict(PROPOSAL, effort="medium")}
    assert "effort = medium" in runner.payloads[0]["quote"]


def test_answer_types_reply_quotes_owner_text(store, tmp_path):
    runner = FakeRunner()
    qid = needs.ask(store, "proj", None, "text", "What next?", purpose="preference")
    q = needs.answer(store, qid, {"type": "reply", "value": "Finish the strip first."}, runner=runner,
                     capture_dir=tmp_path)
    assert q["answer"] == {"type": "reply", "value": "Finish the strip first."}
    assert runner.payloads[0]["quote"] == "Finish the strip first."
    with pytest.raises(ValueError):
        needs.answer(store, needs.ask(store, "p", None, "text", "x"), {"type": "reply", "value": "  "}, capture=False)


def test_answer_types_ignore_closes_without_capture(store, tmp_path):
    runner = FakeRunner()
    qid = needs.ask(store, "proj", None, "text", "Test question - ignore me")
    q = needs.answer(store, qid, {"type": "ignore"}, runner=runner, capture_dir=tmp_path)
    assert q["answer"] == {"type": "ignore", "value": None}
    assert q["answered_at"] and q["captured"] == 0
    assert runner.calls == [] and not list(tmp_path.glob("*.json"))
    assert needs.list_questions(store, open_only=True) == []
    with pytest.raises(ValueError):  # closed questions stay closed
        needs.answer(store, qid, {"type": "reply", "value": "late"}, runner=runner, capture_dir=tmp_path)


def test_answer_types_rejects_type_not_allowed(store):
    qid = needs.ask(store, "p", None, "likert", "Rate", scale=5)
    for bad in ({"type": "accept"}, {"type": "reply", "value": "hi"}, {"type": "shout"},
                {"type": "answer", "value": 3, "extra": 1}, {"type": "ignore", "value": "why"}):
        with pytest.raises(ValueError):
            needs.answer(store, qid, bad, capture=False)
    q = needs.answer(store, qid, {"type": "answer", "value": 3}, capture=False)
    assert q["answer"] == {"type": "answer", "value": 3}


def test_answer_types_bare_value_still_works(store):
    sel = needs.ask(store, "p", None, "select", "Pick", options=["a", "b"])
    assert needs.answer(store, sel, ["b"], capture=False)["answer"] == {"type": "answer", "value": ["b"]}
    txt = needs.ask(store, "p", None, "text", "Say")
    assert needs.answer(store, txt, "Calm.", capture=False)["answer"] == {"type": "reply", "value": "Calm."}


def test_answer_types_only_owner_answers_are_captured(store, tmp_path):
    runner = FakeRunner()
    qid = needs.ask(store, "p", None, "text", "What next?", purpose="preference")
    q = needs.answer(store, qid, {"type": "reply", "value": "Agent guess."}, runner=runner,
                     capture_dir=tmp_path, actor="agent:worker")
    assert q["answer"]["type"] == "reply" and q["captured"] == 0 and runner.calls == []


def test_answer_types_legacy_rows_are_wrapped(store):
    needs.list_questions(store)  # adds the new columns
    with store.tx() as c:
        c.execute("INSERT INTO questions(id, project_id, node_id, kind, prompt, options_json, scale, answer_json,"
                  " answered_at, captured) VALUES('q-old','p',NULL,'likert','Old?','[]',9,'6','2026-01-01',0)")
    q = needs.get_question(store, "q-old")
    assert q["answer"] == {"type": "answer", "value": 6} and q["allowed"] == ["ignore", "answer"]
    assert q["purpose"] == "generic"


def test_answer_types_api_routes(tmp_path):
    import http.client
    import threading

    from cc_engine import api
    from cc_engine.store import Store

    st = Store(tmp_path / "cc.sqlite")
    runner = FakeRunner()
    srv = api.make_server(st, host="127.0.0.1", port=0, usage_reader=lambda: [], capture_runner=runner,
                          capture_dir=tmp_path, heartbeat=0.5, poll_interval=0.2)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def call(method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        headers = {"Origin": "http://127.0.0.1:3080", "Content-Type": "application/json"}
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        r = conn.getresponse()
        out = (r.status, json.loads(r.read() or b"null"))
        conn.close()
        return out

    try:
        status, body = call("POST", "/api/question", {"project": "p", "kind": "text", "prompt": "Run it?",
                                                       "proposal": PROPOSAL})
        assert status == 200 and body["question"]["allowed"] == ["accept", "edit", "reply", "ignore"]
        qid = body["question"]["id"]
        status, body = call("GET", "/api/questions?open=1")
        assert [q["id"] for q in body["questions"]] == [qid] and body["questions"][0]["proposal"] == PROPOSAL
        status, body = call("POST", "/api/question/answer", {"id": qid, "answer": {"type": "answer", "value": "x"}})
        assert status == 400
        status, body = call("POST", "/api/question/answer", {"id": qid, "type": "ignore"})
        assert status == 200 and body["question"]["answer"] == {"type": "ignore", "value": None}
        assert runner.calls == []
        status, body = call("GET", "/api/questions?open=1")
        assert body["questions"] == []
        status, body = call("POST", "/api/question", {"kind": "text", "prompt": "Pick", "allowed": ["edit"]})
        assert status == 400
        status, body = call("POST", "/api/question/answer", {"id": "q-missing", "type": "ignore"})
        assert status == 404
    finally:
        srv.shutdown()
        srv.server_close()
        st.close()


# ---------------------------------------------------------------- answer effects (review item 7)
from cc_engine.store import now_iso  # noqa: E402

LADDER = {"model_ladder": {"claude_code": ["sonnet", "opus"]}, "effort": "high"}


def make_project(store, cwd, nodes, pid="p1"):
    """Project + nodes through the store's own helpers. ``nodes``: (id, deps, status, extra)."""
    with store.tx() as c:
        c.execute("INSERT INTO projects(id, title, brief, cwd, created_at, archived) VALUES(?,?,?,?,?,0)",
                  (pid, "Effects", "Answers must act.", str(cwd), now_iso()))
    for nid, deps, status, extra in nodes:
        row = {"id": nid, "title": nid, "goal": f"Goal of {nid}.", "deps": deps,
               "check": {"type": "command", "run": "exit 0"}}
        row.update(extra)
        store.add_node(pid, row, "test", f"Added {nid}.")
        if status != "not_started":
            store.set_node_status(pid, nid, status, "owner" if status == "done" else "test")
    return pid


def node(store, nid, pid="p1"):
    return store.get_node(pid, nid)


def goals(store, pid="p1"):
    return {n["id"]: (n["goal"], n["goal_hash"]) for n in store.list_nodes(pid)}


def check_runner(code=0):
    calls = []

    def run(cmd, cwd, timeout):
        calls.append(cmd)
        return code, "out", "err"

    run.calls = calls
    return run


def test_answer_effects_generic_is_recorded_only(store, tmp_path):
    make_project(store, tmp_path, [("A", [], "needs_you", {})])
    runner = FakeRunner()
    qid = needs.ask(store, "p1", "A", "text", "Anything to add?")
    assert needs.get_question(store, qid)["purpose"] == "generic"
    rev = store.revision()
    q = needs.answer(store, qid, "Looks fine.", runner=runner, capture_dir=tmp_path)
    assert q["captured"] == 0 and runner.calls == [] and q["effects"] == []
    assert store.revision() == rev and node(store, "A")["status"] == "needs_you"


def test_answer_effects_preference_is_the_only_captured_purpose(store, tmp_path):
    make_project(store, tmp_path, [("A", [], "needs_you", {"check": {"type": "human", "prompt": "ok"}}),
                                   ("B", [], "needs_you", {})])
    runner = FakeRunner()
    pref = needs.ask(store, "p1", None, "likert", "How calm should the board feel?", purpose="preference")
    assert needs.answer(store, pref, 8, runner=runner, capture_dir=tmp_path)["captured"] == 1
    assert len(runner.calls) == 1
    # each is asked just before its answer: a task reaching done closes its own open questions
    operational = [
        (lambda: needs.ask(store, "p1", "A", "text", "Is A done?", purpose="confirm"), {"type": "accept"}),
        (lambda: needs.ask(store, "p1", "B", "text", "B keeps failing", purpose="failure_choice",
                           proposal={"action": "skip"}), {"type": "accept"}),
        (lambda: needs.ask(store, "p1", "B", "text", "Paste B's result", purpose="manual_result"),
         {"type": "reply", "value": "result"}),
        (lambda: needs.ask(store, "p1", None, "text", "Note?"), {"type": "reply", "value": "a note"}),
    ]
    for ask, ans in operational:
        q = needs.answer(store, ask(), ans, runner=runner, capture_dir=tmp_path, check_runner=check_runner(),
                         settings=LADDER)
        assert q["captured"] == 0, q["purpose"]
    assert len(runner.calls) == 1, "operational answers must never reach the preference registry"
    assert not list(tmp_path.glob("cc_capture_*.json"))


def test_answer_effects_purpose_validated(store, tmp_path):
    make_project(store, tmp_path, [("A", [], "needs_you", {})])
    bad = [("A", "text", {"purpose": "chat"}), (None, "text", {"purpose": "confirm"}),
           ("A", "likert", {"purpose": "manual_result"}),
           ("A", "text", {"purpose": "failure_choice", "proposal": {"action": "rewrite"}}),
           ("A", "text", {"purpose": "failure_choice", "proposal": {"action": "retry", "goal": "Easier goal."}})]
    for node_id, kind, kwargs in bad:
        with pytest.raises(ValueError):
            needs.ask(store, "p1", node_id, kind, "Question?", **kwargs)
    q = needs.get_question(store, needs.ask(store, "p1", "A", "text", "Is A done?", purpose="confirm"))
    assert q["allowed"] == ["accept", "reply", "ignore"] and q["proposal"] is None


def test_answer_effects_manual_result_writes_output_file_and_passes(store, tmp_path):
    make_project(store, tmp_path, [("M", [], "needs_you",
                                    {"pool": "gemini", "check": {"type": "text_present", "path": "out/m.md",
                                                                 "text": "VERDICT"}})])
    qid = needs.ask(store, "p1", "M", "text", "Paste the Gemini App result for M.", purpose="manual_result")
    q = needs.answer(store, qid, {"type": "reply", "value": "Analysis...\nVERDICT: fine"}, runner=FakeRunner())
    assert (tmp_path / "out" / "m.md").read_text(encoding="utf-8") == "Analysis...\nVERDICT: fine"
    n = node(store, "M")
    assert n["status"] == "done" and n["actor"] == "check:text_present"
    assert any("saved to m.md" in e for e in q["effects"]) and any("is done" in e for e in q["effects"])
    assert "manual_result" in [e["kind"] for e in store.events("p1")]


def test_answer_effects_manual_result_without_output_is_evidence(store, tmp_path):
    make_project(store, tmp_path, [("K", [], "needs_you", {"pool": "manual"})])
    run = check_runner(0)
    qid = needs.ask(store, "p1", "K", "text", "Paste K's result.", purpose="manual_result")
    q = needs.answer(store, qid, "Pasted output for K.", check_runner=run)
    assert run.calls == ["exit 0"]
    assert node(store, "K")["status"] == "done" and node(store, "K")["actor"] == "check:command"
    assert any("stored as its evidence" in e for e in q["effects"])
    assert any(e["summary"] == "Owner's pasted result for task K was stored as its evidence."
               for e in store.events("p1"))


def test_answer_effects_manual_result_failure_goes_to_failure_policy(store, tmp_path):
    make_project(store, tmp_path, [("F", [], "needs_you", {"pool": "claude_code", "model": "sonnet",
                                                           "effort": "high"})])
    before = goals(store)
    qid = needs.ask(store, "p1", "F", "text", "Paste F's result.", purpose="manual_result")
    q = needs.answer(store, qid, "Wrong output.", check_runner=check_runner(1), settings=LADDER)
    n = node(store, "F")
    assert n["status"] == "not_started" and (n["model"], n["effort"]) == ("sonnet", "xhigh")
    assert "escalate" in [e["kind"] for e in store.events("p1")]
    assert any("try again" in e for e in q["effects"])
    # ladder exhausted: the policy asks a failure_choice question
    store.update_node("p1", "F", {"model": "opus", "effort": "xhigh", "status": "needs_you"}, "test", "x")
    qid = needs.ask(store, "p1", "F", "text", "Paste F's result again.", purpose="manual_result")
    needs.answer(store, qid, "Still wrong.", check_runner=check_runner(1), settings=LADDER)
    assert node(store, "F")["status"] == "needs_you"
    asked = [x for x in needs.list_questions(store, open_only=True) if x["node_id"] == "F"]
    assert [x["purpose"] for x in asked] == ["failure_choice"]
    after = goals(store)  # goals never change; the failure doctor may add F-fix tasks
    assert {k: after[k] for k in before} == before and all(k in before or k.startswith("F-fix") for k in after)


def test_answer_effects_manual_result_waits_for_deps(store, tmp_path):
    make_project(store, tmp_path, [("A", [], "running", {}), ("W", ["A"], "needs_you", {"pool": "manual"})])
    run = check_runner(0)
    qid = needs.ask(store, "p1", "W", "text", "Paste W's result.", purpose="manual_result")
    q = needs.answer(store, qid, "W output", check_runner=run)
    assert run.calls == [] and node(store, "W")["status"] == "needs_you"
    assert any("once A is done" in e for e in q["effects"])


def test_answer_effects_failure_choice_accept_retry(store, tmp_path):
    make_project(store, tmp_path, [("R", [], "needs_you", {"pool": "claude_code", "model": "sonnet",
                                                           "effort": "high"})])
    before = goals(store)
    qid = needs.ask(store, "p1", "R", "text", "R keeps failing. Retry on a stronger model?",
                    purpose="failure_choice", proposal={"action": "retry", "model": "opus", "effort": "max"})
    q = needs.answer(store, qid, {"type": "accept"}, runner=FakeRunner())
    n = node(store, "R")
    assert (n["status"], n["model"], n["effort"]) == ("not_started", "opus", "max")
    assert q["effects"] == ["Task R will try again with opus at max effort, as Owner chose."]
    assert goals(store) == before


def test_answer_effects_failure_choice_edit_and_default_stronger_model(store, tmp_path):
    make_project(store, tmp_path, [("R", [], "needs_you", {"pool": "claude_code", "model": "sonnet",
                                                           "effort": "high"}),
                                   ("S", [], "needs_you", {"pool": "claude_code", "model": "sonnet",
                                                           "effort": "high"})])
    qid = needs.ask(store, "p1", "R", "text", "Retry?", purpose="failure_choice",
                    proposal={"action": "retry", "model": "opus", "effort": "max"})
    needs.answer(store, qid, {"type": "edit", "value": {"effort": "xhigh"}})
    assert (node(store, "R")["model"], node(store, "R")["effort"]) == ("opus", "xhigh")
    qid = needs.ask(store, "p1", "S", "select", "S keeps failing. What next?", purpose="failure_choice",
                    options=["Add a fix-up step before this task", "Try a different model",
                             "Do this task manually", "Skip for now and continue the rest"])
    with pytest.raises(ValueError):  # exactly one choice
        needs.answer(store, qid, ["Try a different model", "Do this task manually"], settings=LADDER)
    needs.answer(store, qid, ["Try a different model"], settings=LADDER)
    n = node(store, "S")
    assert (n["status"], n["model"], n["effort"]) == ("not_started", "opus", "xhigh")


def test_answer_effects_failure_choice_fixup_never_changes_goal(store, tmp_path):
    make_project(store, tmp_path, [("A", [], "done", {}), ("B", [], "done", {}),
                                   ("X", ["A"], "needs_you", {"pool": "claude_code"})])
    before = goals(store)
    fix_check = {"type": "files_exist", "paths": ["fixed.txt"]}
    qid = needs.ask(store, "p1", "X", "text", "Add a fix-up step?", purpose="failure_choice",
                    proposal={"action": "fixup", "title": "Repair the fixture", "check": fix_check,
                              "deps": ["A", "B"]})
    q = needs.answer(store, qid, {"type": "accept"})
    fix = node(store, "X-fix1")
    assert fix is not None and fix["check"] == fix_check and fix["deps"] == ["A", "B"]
    x = node(store, "X")
    assert x["deps"] == ["A", "X-fix1"] and x["status"] == "not_started"
    assert {k: v for k, v in goals(store).items() if k != "X-fix1"} == before
    assert any("Added fix-up step X-fix1" in e for e in q["effects"])
    with pytest.raises(ValueError):  # edit can never smuggle in a goal
        needs.answer(store, needs.ask(store, "p1", "X", "text", "Again?", purpose="failure_choice",
                                      proposal={"action": "fixup", "title": "t"}),
                     {"type": "edit", "value": {"goal": "Easier."}})


def test_answer_effects_failure_choice_manual_and_skip(store, tmp_path):
    make_project(store, tmp_path, [("M", [], "needs_you", {"pool": "claude_code"}),
                                   ("S", [], "needs_you", {"pool": "claude_code"})])
    opts = ["Do this task manually", "Skip for now and continue the rest"]
    needs.answer(store, needs.ask(store, "p1", "M", "select", "What next?", purpose="failure_choice",
                                  options=opts), ["Do this task manually"])
    assert (node(store, "M")["pool"], node(store, "M")["status"]) == ("manual", "not_started")
    q = needs.answer(store, needs.ask(store, "p1", "S", "select", "What next?", purpose="failure_choice",
                                      options=opts), ["Skip for now and continue the rest"])
    assert node(store, "S")["status"] == "error"
    assert q["effects"] == ["Owner chose to skip task S for now; the other tasks carry on."]


def test_answer_effects_confirm_accept_sets_done_by_owner(store, tmp_path):
    make_project(store, tmp_path, [("H", [], "needs_you", {"check": {"type": "human", "prompt": "Looks right"}}),
                                   ("G", [], "needs_you", {"check": {"type": "human", "prompt": "ok"}})])
    q = needs.answer(store, needs.ask(store, "p1", "H", "text", "Is H done?", purpose="confirm"),
                     {"type": "accept"}, runner=FakeRunner())
    n = node(store, "H")
    assert (n["status"], n["actor"]) == ("done", "owner")
    assert q["answer"] == {"type": "accept", "value": None}
    assert q["effects"] == ["Owner confirmed task H is done."]
    assert any(e["summary"] == "Owner confirmed task H is done." for e in store.events("p1"))
    # an agent cannot confirm a human check
    needs.answer(store, needs.ask(store, "p1", "G", "text", "Is G done?", purpose="confirm"),
                 {"type": "accept"}, actor="agent:worker")
    assert node(store, "G")["status"] == "needs_you"


def test_answer_effects_confirm_reply_keeps_open_with_note(store, tmp_path):
    make_project(store, tmp_path, [("H", [], "needs_you", {"check": {"type": "human", "prompt": "ok"}})])
    q = needs.answer(store, needs.ask(store, "p1", "H", "text", "Is H done?", purpose="confirm"),
                     {"type": "reply", "value": "Not yet: the legend overlaps the chart."})
    n = node(store, "H")
    assert n["status"] == "needs_you" and n["evidence"] == "Not yet: the legend overlaps the chart."
    assert q["effects"] == ["Owner kept task H open: Not yet: the legend overlaps the chart."]


def test_answer_effects_ignore_has_no_side_effects(store, tmp_path):
    make_project(store, tmp_path, [("H", [], "needs_you", {"check": {"type": "human", "prompt": "ok"}})])
    asked = [needs.ask(store, "p1", "H", "text", "Is H done?", purpose="confirm"),
             needs.ask(store, "p1", "H", "text", "Paste H", purpose="manual_result"),
             needs.ask(store, "p1", "H", "text", "Fail?", purpose="failure_choice", proposal={"action": "skip"})]
    rev, before = store.revision(), node(store, "H")
    for qid in asked:
        q = needs.answer(store, qid, {"type": "ignore"}, check_runner=check_runner())
        assert q["effects"] == [] and q["answered_at"]
    assert store.revision() == rev and node(store, "H") == before


def test_answer_effects_purpose_asker_tags_questions(store, tmp_path):
    make_project(store, tmp_path, [("A", [], "needs_you", {})])
    qid = needs.purpose_asker(store, "manual_result")(project_id="p1", node_id="A", kind="text",
                                                       prompt="Paste A", options=None)
    assert needs.get_question(store, qid)["purpose"] == "manual_result"
