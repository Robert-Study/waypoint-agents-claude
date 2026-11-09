"""LIVE2 (Waypoint v3 wave 4) engine bugs: Claude edits, draft gates/risk, stale questions, archive."""
import json

from cc_engine import api, checks, dispatch, failure, needs, plan, planner
from cc_engine import scheduler as S

from test_api import ALLOWED, call, server, tok  # noqa: F401  (server is a fixture)


# ---------------------------------------------------------------- 1. Claude Code may edit files
def test_live2_claude_dispatch_defaults_to_accept_edits():
    argv = dispatch.Dispatcher(claude_exe="claude").claude_argv({"model": "haiku"}, {})
    i = argv.index("--permission-mode")
    assert argv[i + 1] == "acceptEdits"
    assert not any("bypass" in a.lower() or "dangerously" in a.lower() for a in argv)
    assert "--permission-mode" in dispatch.Dispatcher(claude_exe="claude").claude_argv({"model": "haiku"})


def test_live2_claude_extra_args_from_settings_win():
    argv = dispatch.Dispatcher(claude_exe="claude").claude_argv({"model": "haiku"},
                                                              {"claude_extra_args": ["--verbose"]})
    assert "--verbose" in argv and "acceptEdits" not in argv
    assert "acceptEdits" not in planner.claude_argv()  # the planner stays read-only


DENIED = {"type": "result", "is_error": False, "result": "I could not write result.txt.",
          "permission_denials": [{"tool_name": "Write", "tool_use_id": "t1",
                                  "tool_input": {"file_path": "C:\\work\\result.txt", "content": "PONG"}}]}


def test_live2_permission_denials_read_from_output(tmp_path):
    out = tmp_path / "out.txt"
    out.write_text(json.dumps(DENIED), encoding="utf-8")
    line = failure.permission_denials({"route": "claude_code", "output_path": str(out)})
    assert line.startswith("Claude was not allowed to edit files: ")
    assert "Write" in line and "result.txt" in line
    assert failure.permission_denials({"route": "claude_code", "output_tail": json.dumps(DENIED)})
    assert failure.permission_denials({"route": "claude_code", "output_tail": "{}"}) is None
    assert failure.permission_denials({"route": "codex", "output_tail": json.dumps(DENIED)}) is None


class DeniedDispatcher:
    def __init__(self, out):
        self.out = out

    def route(self, node, settings):
        return "claude_code"

    def dispatch(self, node, settings, project):
        return {"status": "running", "job": "j", "summary": f"Task {node['id']} started."}

    def poll(self, project_id, node_id, now=None):
        return {"state": "exited", "exit_code": 0, "route": "claude_code", "job": "j",
                "output_path": str(self.out), "output_tail": self.out.read_text(encoding="utf-8")}


def test_live2_failed_check_names_the_permission_denial(store, tmp_path):
    out = tmp_path / "out.txt"
    out.write_text(json.dumps(DENIED), encoding="utf-8")
    plan.import_plan(store, {"id": "pd", "title": "PD", "brief": "b", "cwd": str(tmp_path), "nodes": [
        {"id": "n2", "title": "Write", "goal": "Write PONG to result.txt.", "deps": [], "pool": "claude_code",
         "model": "haiku", "check": {"type": "text_present", "path": "result.txt", "text": "PONG"}}]})
    store.create_run("r1", "pd", {"pools": ["claude_code"],
                                  "advanced": {"model_ladder": {"claude_code": ["haiku", "sonnet"]}}})
    usage = lambda: [{"pool": p, "status": "FRESH", "remaining_percent": 100} for p in ("claude_5h", "claude_week")]
    d = DeniedDispatcher(out)
    for i in range(2):
        S.tick(store, now=2_000_000_000 + 60 * i, dispatcher=d, project_id="pd", usage_reader=usage)
    returned = [e for e in store.events("pd") if e["kind"] == "returned"]
    assert returned and "Claude was not allowed to edit files" in returned[0]["summary"]
    assert "result.txt" in returned[0]["summary"]


# ---------------------------------------------------------------- 2. draft risk + one ship gate
def _raw(nodes):
    return {"brief": "b", "nodes": nodes}


def _n(nid, deps=(), check=None, **kw):
    return dict({"id": nid, "title": nid, "goal": f"Do {nid}.", "check": check or {"type": "command", "run": "exit 0"},
                 "deps": [{"id": d, "crosses": "x"} for d in deps], "risk": None}, **kw)


def test_live2_draft_gets_risk_and_one_ship_gate(store):
    built = planner.build_plan({"title": "T"}, _raw([_n("A"), _n("B", ["A"]), _n("C", ["A"])]), "p1",
                               allowed_models=[])
    assert [n["risk"] for n in built["nodes"][:3]] == ["contained"] * 3
    gates = [n for n in built["nodes"] if n["check"]["type"] == "human"]
    assert len(gates) == 1 and gates[0] is built["nodes"][-1]
    assert sorted(d["id"] for d in gates[0]["deps"]) == ["B", "C"]
    assert all(d.get("crosses") for d in gates[0]["deps"])
    assert planner.problems(built) == []
    plan.import_plan(store, built, actor="planner", draft=True)
    assert len(store.list_nodes("p1")) == 4


def test_live2_draft_with_a_gate_is_left_alone():
    raw = _raw([_n("A", risk="wide"), _n("G", ["A"], {"type": "human", "prompt": "Ship it?"})])
    built = planner.build_plan({"title": "T"}, raw, "p1", allowed_models=[])
    assert [n["id"] for n in built["nodes"]] == ["A", "G"]
    assert built["nodes"][0]["risk"] == "wide" and built["nodes"][1]["risk"] == "contained"


# ---------------------------------------------------------------- 3. stale questions close on done
def _one(store, tmp_path, pid="sq"):
    plan.import_plan(store, {"id": pid, "title": "SQ", "brief": "b", "cwd": str(tmp_path), "nodes": [
        {"id": "n2", "title": "W", "goal": "Work.", "deps": [], "check": {"type": "command", "run": "exit 0"}},
        {"id": "n3", "title": "X", "goal": "Other.", "deps": [], "check": {"type": "command", "run": "exit 0"}}]})


def test_live2_done_by_check_closes_replan_and_confirm_questions(store, tmp_path):
    _one(store, tmp_path)
    rq = needs.ask(store, "sq", "n2", "select", "Re-plan n2?", options=["Re-plan", "Leave"], purpose="replan")
    cq = needs.ask(store, "sq", "n2", "text", "Is n2 done?", purpose="confirm")
    gq = needs.ask(store, "sq", "n2", "text", "Colour?")
    other = needs.ask(store, "sq", "n3", "text", "Is n3 done?", purpose="confirm")
    res = checks.complete_node(store, "sq", "n2", runner=lambda cmd, cwd, timeout: (0, "ok", ""),
                               cwd=str(tmp_path))
    assert res.ok
    open_ids = {q["id"] for q in needs.list_questions(store, open_only=True)}
    assert rq not in open_ids and cq not in open_ids
    assert gq in open_ids and other in open_ids
    closed = [e for e in store.events("sq") if e["kind"] == "questions_closed"]
    assert len(closed) == 1 and "\n" not in closed[0]["summary"] and closed[0]["node_id"] == "n2"


def test_live2_done_by_owner_closes_failure_question(store, tmp_path):
    _one(store, tmp_path)
    fq = needs.ask(store, "sq", "n2", "text", "n2 failed: what next?", purpose="failure_choice")
    store.set_node_status("sq", "n2", "done", "owner")
    assert needs.get_question(store, fq)["answered_at"]
    assert needs.get_question(store, fq)["answer"]["type"] == "ignore"


# ---------------------------------------------------------------- 4. archive route
def test_live2_archive_route_owner_only(server, store, tmp_path):  # noqa: F811
    _one(store, tmp_path, pid="arc")
    store.create_run("r-arc", "arc", {"pools": ["claude_code"]})
    q = needs.ask(store, "arc", None, "text", "Anything?")
    resp, body = call(server, "POST", "/api/project/archive", {"project": "arc"}, token=tok(server))
    assert resp.status == 403 and body["error"] == "owner_only"
    resp, body = call(server, "POST", "/api/project/archive", {"project": "nope"}, origin=ALLOWED)
    assert resp.status == 404
    resp, body = call(server, "POST", "/api/project/archive", {"project": "arc"}, origin=ALLOWED)
    assert resp.status == 200 and body["ok"] is True and body["archived"] is True
    assert body["cancelled_runs"] == ["r-arc"] and body["closed_questions"] == [q]
    assert store.get_project("arc")["archived"] == 1
    assert store.get_run("r-arc")["status"] == "cancelled"
    assert needs.get_question(store, q)["answered_at"]
    _, listed = call(server, "GET", "/api/projects")
    assert "arc" not in [p["id"] for p in listed["projects"]]
    resp, body = call(server, "POST", "/api/project/unarchive", {"project": "arc"}, origin=ALLOWED)
    assert resp.status == 200 and body["archived"] is False
    _, listed = call(server, "GET", "/api/projects")
    assert "arc" in [p["id"] for p in listed["projects"]]


def test_board_reports_an_archived_project_as_archived(tmp_path):
    """Live test: after archiving, the board still said project.status 'active'."""
    from cc_engine import api, plan
    from cc_engine.store import Store
    st = Store(str(tmp_path / "cc.sqlite"))
    plan.import_plan(st, {"id": "arch-x", "title": "T", "nodes": [
        {"id": "A", "title": "a", "goal": "g", "check": {"type": "human", "prompt": "ok?"}}]})
    assert api.build_board(st, "arch-x")["project"]["archived"] is False
    st._q("UPDATE projects SET archived=1 WHERE id=?", ("arch-x",))
    b = api.build_board(st, "arch-x")["project"]
    assert b["archived"] is True and b["status"] == "archived"


def test_board_carries_node_kind_and_crosses(tmp_path):
    """The board route dropped kind/crosses (annotate got rows, not dicts), so code-node cues never showed."""
    from cc_engine import api, plan
    from cc_engine.store import Store
    st = Store(str(tmp_path / "cc.sqlite"))
    plan.import_plan(st, {"id": "kinds-x", "title": "T", "nodes": [
        {"id": "A", "title": "merge", "goal": "merge files", "kind": "code", "script": "python -c 0",
         "check": {"type": "command", "run": "python -c 0"}},
        {"id": "B", "title": "b", "goal": "judge it", "deps": [{"id": "A", "crosses": "merged.json"}],
         "check": {"type": "human", "prompt": "ok?"}}]})
    board = api.with_loop_fields(st, "kinds-x", api.build_board(st, "kinds-x"))
    by = {n["id"]: n for n in api.decode_nodes(board)}
    assert by["A"]["kind"] == "code"
    assert by["B"]["crosses"] == {"A": "merged.json"}
