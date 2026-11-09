"""End-to-end plan, dispatch, return, escalation, rework, and learning loop."""

from cc_engine import checks, constraints, history, loops, plan
from cc_engine import scheduler as S
from cc_engine.store import Store


SETTINGS = {
    "pools": ["claude_code"],
    "advanced": {
        "model_ladder": {"claude_code": ["sonnet", "opus"]},
        "effort": "high",
        "max_concurrency": 1,
    },
}


class FakeDispatcher:
    """Record worker routes and return successful process exits, without providers."""

    def __init__(self, store, project_id):
        self.store = store
        self.project_id = project_id
        self.sent = []

    def route(self, node, settings):
        return "claude_code"

    def dispatch(self, node, settings, project):
        packet = loops.latest_packet(self.store, self.project_id, node["id"])
        prompt = loops.worker_prompt(node, project, packet=packet)
        self.sent.append({
            "id": node["id"],
            "model": node["model"],
            "effort": node["effort"],
            "attempts": node["attempts"],
            "packet": packet,
            "prompt": prompt,
        })
        job = f"fake-{node['id']}-{node['attempts']}"
        return {"status": "running", "job": job, "summary": f"Task {node['id']} started {job}."}

    def poll(self, project_id, node_id, now=None):
        return {"state": "exited", "exit_code": 0, "job": f"fake-{node_id}", "output_path": "fake-output"}


def test_e2e_loop_full_cycle(tmp_path):
    store = Store(tmp_path / "e2e-loop.sqlite")
    project_id = "e2e-small"
    node_id = "build"
    imported = plan.import_plan(store, {
        "id": project_id,
        "title": "Small routed project",
        "brief": "Prepare and verify a feature through a correction loop.",
        "cwd": str(tmp_path),
        "nodes": [
            {
                "id": "prepare", "title": "Prepare", "goal": "Prepare the feature outline.",
                "deps": [], "pool": "claude_code", "model": "sonnet", "effort": "high",
                "check": {"type": "command", "run": "python -m pytest tests/test_outline.py"},
            },
            {
                "id": node_id, "title": "Build", "goal": "Build and verify the feature.",
                "deps": ["prepare"], "pool": "claude_code", "model": "sonnet", "effort": "high",
                "check": {"type": "command", "run": "python -m pytest tests/test_feature.py"},
            },
        ],
    })
    assert imported == project_id
    assert any(e["kind"] == "import" for e in store.events(project_id))
    store.create_run("e2e-run", project_id, SETTINGS)

    dispatcher = FakeDispatcher(store, project_id)
    check_calls = []

    def fake_checker(db, pid, node, project):
        check_calls.append(node["id"])
        first_build_check = node["id"] == node_id and check_calls.count(node_id) == 1
        if first_build_check:
            runner = lambda cmd, cwd, timeout: (1, "", "feature assertion failed: expected ready")
        else:
            runner = lambda cmd, cwd, timeout: (0, "2 passed", "")
        result = checks.complete_node(db, pid, node["id"], runner=runner, cwd=str(tmp_path))
        return result.ok, result.detail

    def fake_usage():
        return [{"pool": pool, "status": "FRESH", "remaining_percent": 100}
                for pool in ("claude_5h", "claude_week")]

    escalated = False
    for i in range(10):
        tick_result = S.tick(store, now=2_000_000_000 + 60 * i, dispatcher=dispatcher,
                             project_id=project_id, usage_reader=fake_usage, checker=fake_checker)
        escalated |= any(item["id"] == node_id and item["action"] == "escalate"
                         for item in tick_result["failed"])
        if S.get_node(store, project_id, node_id)["status"] == "done":
            break

    builds = [item for item in dispatcher.sent if item["id"] == node_id]
    events = store.events(project_id)
    returns = [e for e in events if e["node_id"] == node_id and e["kind"] == "returned"]
    assert len(returns) == 1
    assert returns[0]["summary"].startswith("Returned: ")
    assert "feature assertion failed" in returns[0]["summary"]

    assert escalated
    assert len(builds) == 2
    assert (builds[1]["model"], builds[1]["effort"]) != (builds[0]["model"], builds[0]["effort"]) \
        or builds[1]["attempts"] > builds[0]["attempts"]
    assert builds[1]["packet"]["unit"] == node_id
    assert "Returned for rework" in builds[1]["prompt"]
    assert "feature assertion failed" in builds[1]["prompt"]

    assert check_calls == ["prepare", "build", "build"]
    assert S.get_node(store, project_id, "prepare")["status"] == "done"
    assert S.get_node(store, project_id, node_id)["status"] == "done"
    assert store.get_run("e2e-run")["status"] == "done"

    timeline = history.node_history(store, project_id, node_id)
    kinds = [row["kind"] for row in timeline]
    assert "prereqs_met" in kinds
    assert kinds.count("started") == 2
    assert "failed" in kinds
    assert kinds.index("returned") < kinds.index("completed")
    assert timeline[kinds.index("returned")]["text"].startswith("Returned: ")

    accepted = constraints.record_accepted(store, project_id, node_id)
    stored = constraints.list_constraints(store, project_id)
    assert accepted["line"].startswith("ACCEPTED: build")
    assert any(row["node"] == node_id and row["line"] == accepted["line"] for row in stored)
