"""Receipts and question files (FILE_CONTRACT.md): validation, ingest, the watcher and the routes."""
import json

import pytest

from cc_engine import api, needs, plan, receipts, todo


def _pass(cmd, cwd, timeout):
    return 0, "fine", ""


def _fail(cmd, cwd, timeout):
    return 1, "", "boom"


@pytest.fixture
def loaded(store, fixture_plan, tmp_path):
    fixture_plan["cwd"] = str(tmp_path)
    plan.import_plan(store, fixture_plan)
    return store


def rc(**kw):
    base = {"schema": "waypoint.receipt.v1", "status": "done", "summary": "The root is laid."}
    base.update(kw)
    return base


def raw(obj):
    return json.dumps(obj).encode()


def node_status(store, nid="A"):
    return store.get_node("fx", nid)["status"]


# ---------------------------------------------------------------- validation
@pytest.mark.parametrize("patch,word", [
    ({"schema": "x"}, "schema"),
    ({"status": "finished"}, "status"),
    ({"summary": ""}, "summary"),
    ({"summary": "x" * 301}, "300"),
    ({"evidence": ["a"] * 21}, "evidence"),
    ({"evidence": ["a" * 501]}, "evidence"),
    ({"outputs": ["p"] * 21}, "outputs"),
    ({"outputs": "p"}, "outputs"),
    ({"project": "fx"}, "both"),
    ({"todo": {"text": ""}}, "todo"),
    ({"todo": {"text": "t", "section": "me"}}, "section"),
    ({"todo": "x"}, "todo"),
    ({"questions": [{"kind": "text", "prompt": "q"}] * 4}, "questions"),
    ({"questions": [{"kind": "pick", "prompt": "q"}]}, "kind"),
    ({"questions": [{"kind": "select", "prompt": "q"}]}, "options"),
    ({"questions": [{"kind": "select", "prompt": "q", "options": [{"value": "a", "selected": True}]}]},
     "preselected"),
    ({"metrics": [1]}, "metrics"),
])
def test_validate_rejects(patch, word):
    clean, reason = receipts.validate(rc(**patch))
    assert clean is None and word in reason


def test_validate_accepts_and_keeps_unknown_fields():
    clean, reason = receipts.validate(rc(project="fx", node="A", extra={"k": 1}, outputs=["a.md"]))
    assert reason is None and clean["extra"] == {"k": 1} and clean["todo"] is None
    assert receipts.validate(rc(evidence=["a" * 500] * 20, outputs=["o"] * 20))[1] is None


def test_validate_question_rules():
    ok = {"schema": "waypoint.question.v1", "kind": "likert", "prompt": "How clear?", "scale": 9}
    assert receipts.validate_question(ok)[1] is None
    assert receipts.validate_question(dict(ok, scale=1))[1]
    assert receipts.validate_question(dict(ok, schema="x"))[1]
    assert receipts.validate_question({"schema": "waypoint.question.v1", "kind": "text", "prompt": ""})[1]


def test_oversize_and_bad_json_are_invalid(loaded):
    out = receipts.ingest(loaded, b"x" * (64 * 1024 + 1))
    assert out["status"] == "invalid" and "64 KB" in out["reason"]
    out = receipts.ingest(loaded, b"{nope")
    assert out["status"] == "invalid" and "JSON" in out["reason"]
    assert {r["status"] for r in receipts.list_receipts(loaded)} == {"invalid"}


# ---------------------------------------------------------------- ingest
def test_done_with_passing_check_sets_done_and_ticks_computer_todo_only(loaded):
    mine = todo.add_todo(loaded, "Machine follow-up", section="computer", node={"project": "fx", "id": "A"})
    yours = todo.add_todo(loaded, "Read it", section="human", node={"project": "fx", "id": "A"})
    out = receipts.ingest(loaded, raw(rc(project="fx", node="A")), runner=_pass)
    assert out["status"] == "accepted"
    assert node_status(loaded) == "done"
    assert loaded.get_node("fx", "A")["actor"].startswith("check:")
    assert todo.get_todo(loaded, mine["id"])["status"] == "done"
    assert todo.get_todo(loaded, yours["id"])["status"] == "open"
    row = receipts.get(loaded, out["id"])
    assert row["claimed"] == "done" and row["outcome"] == "check passed" and row["project"] == "fx"


def test_failing_check_sets_error(loaded):
    out = receipts.ingest(loaded, raw(rc(project="fx", node="A")), runner=_fail)
    assert node_status(loaded) == "error"
    assert receipts.get(loaded, out["id"])["outcome"].startswith("check failed")


def test_done_claim_never_sets_done_without_the_check(loaded):
    calls = []

    def fake(store, project, node, runner=None, cwd=None):
        calls.append((project, node))
        return type("R", (), {"ok": False, "detail": "no"})()

    receipts.ingest(loaded, raw(rc(project="fx", node="A")), complete=fake)
    assert calls == [("fx", "A")] and node_status(loaded) != "done"
    # a human check never passes by claim either
    receipts.ingest(loaded, raw(rc(project="fx", node="E", summary="Looks fine.")), runner=_pass)
    assert node_status(loaded, "E") != "done"


def test_done_releases_the_claim(loaded):
    from cc_engine import claims
    claims.claim(loaded, "fx", "A", "worker-x")
    receipts.ingest(loaded, raw(rc(project="fx", node="A", session="worker-x")), runner=_pass)
    assert loaded.claim_row("fx", "A") is None and node_status(loaded) == "done"


def test_duplicate_is_ignored(loaded):
    b = raw(rc(project="fx", node="A", todo={"text": "Look", "section": "human"}))
    first = receipts.ingest(loaded, b, runner=_pass)
    second = receipts.ingest(loaded, b, runner=_pass)
    assert second == {"id": first["id"], "status": "duplicate"}
    assert len(receipts.list_receipts(loaded)) == 1
    assert len(todo.list_todos(loaded)) == 1


def test_error_receipt_sets_error_with_evidence(loaded):
    receipts.ingest(loaded, raw(rc(status="error", project="fx", node="A", evidence=["pytest: 2 failed"])))
    n = loaded.get_node("fx", "A")
    assert n["status"] == "error" and n["actor"] == "receipt" and "2 failed" in n["evidence"]


def test_needs_you_creates_questions(loaded):
    q = {"kind": "select", "prompt": "Which style?", "options": ["Tables", "Examples"]}
    receipts.ingest(loaded, raw(rc(status="needs_you", project="fx", node="A", questions=[q])))
    assert node_status(loaded) == "needs_you"
    assert "Which style?" in [x["prompt"] for x in needs.list_questions(loaded)]


def test_needs_you_without_questions_asks_one(loaded):
    receipts.ingest(loaded, raw(rc(status="needs_you", project="fx", node="A", summary="Blocked on a key.")))
    assert [x["prompt"] for x in needs.list_questions(loaded)] == ["Blocked on a key."]


def test_session_receipt_logs_and_adds_one_todo(loaded):
    rev = loaded.revision()
    out = receipts.ingest(loaded, raw(rc(summary="Wrote the notes.", outputs=["notes.md", "https://x.test/a"],
                                         todo={"text": "Check the notes", "section": "human", "link": "notes.md"})))
    assert out["status"] == "accepted" and loaded.revision() == rev
    ts = todo.list_todos(loaded)
    assert len(ts) == 1 and ts[0]["section"] == "human" and ts[0]["created_by"] == "receipt"
    assert [d["label"] for d in ts[0]["deliverables"]] == ["notes.md", "https://x.test/a"]
    ev = [e for e in loaded.events() if e["kind"] == "receipt"]
    assert ev and ev[-1]["summary"].startswith("A session reported")


def test_unknown_node_changes_no_plan(loaded):
    rev = loaded.revision()
    out = receipts.ingest(loaded, raw(rc(project="nope", node="Z")), runner=_pass)
    assert out["status"] == "accepted" and loaded.revision() == rev
    assert receipts.get(loaded, out["id"])["outcome"] == "unknown node"


def test_notify_receipt_event(loaded):
    seen = []
    loaded.subscribe(lambda k, p: seen.append(k))
    receipts.ingest(loaded, raw(rc()))
    assert "receipt" in seen


# ---------------------------------------------------------------- question files
def test_question_file_is_queued_once(loaded):
    q = raw({"schema": "waypoint.question.v1", "project": "fx", "node": "A", "kind": "select",
             "prompt": "Pick one?", "options": ["a", "b"]})
    first = receipts.ingest_question(loaded, q, "x/questions/q.json")
    assert first["status"] == "accepted"
    assert receipts.ingest_question(loaded, q)["status"] == "duplicate"
    assert [x["prompt"] for x in needs.list_questions(loaded)] == ["Pick one?"]
    bad = receipts.ingest_question(loaded, raw({"schema": "waypoint.question.v1", "kind": "select", "prompt": "p"}))
    assert bad["status"] == "invalid"


# ---------------------------------------------------------------- watcher
def test_watch_roots_collects_sources(loaded, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    extra = tmp_path / "traced"
    extra.mkdir()
    roots = receipts.watch_roots(loaded, trace_cwds=[str(extra), str(tmp_path / "missing")], inbox=inbox)
    assert tmp_path in roots and extra in roots and inbox in roots
    assert all(r.is_dir() for r in roots)


def test_scan_writes_gitignore_and_ingests_changed_file_once(loaded, tmp_path):
    root = tmp_path / "work"
    d = root / ".waypoint" / "receipts"
    d.mkdir(parents=True)
    f = d / "fx.A.json"
    f.write_text(json.dumps(rc(summary="First pass.")), encoding="utf-8")
    seen = {}
    inbox = tmp_path / "no-inbox"
    assert receipts.scan_once(loaded, [root], seen, inbox=inbox) == 1
    assert (root / ".waypoint" / ".gitignore").read_text() == "*\n"
    assert receipts.scan_once(loaded, [root], seen, inbox=inbox) == 0
    f.write_text(json.dumps(rc(summary="Second pass, longer.")), encoding="utf-8")
    assert receipts.scan_once(loaded, [root], seen, inbox=inbox) == 1
    assert len(receipts.list_receipts(loaded)) == 2
    # an existing .gitignore is never overwritten
    (root / ".waypoint" / ".gitignore").write_text("custom\n")
    receipts.scan_once(loaded, [root], {}, inbox=inbox)
    assert (root / ".waypoint" / ".gitignore").read_text() == "custom\n"
    assert len(receipts.list_receipts(loaded)) == 2  # same bytes: duplicates


def test_scan_inbox_and_questions_and_bad_file(loaded, tmp_path):
    inbox = tmp_path / "inbox"
    (inbox / "receipts").mkdir(parents=True)
    (inbox / "questions").mkdir()
    (inbox / "receipts" / "a.json").write_text(json.dumps(rc()), encoding="utf-8")
    (inbox / "receipts" / "bad.json").write_text("{half", encoding="utf-8")
    (inbox / "questions" / "q.json").write_text(json.dumps(
        {"schema": "waypoint.question.v1", "kind": "text", "prompt": "Why?"}), encoding="utf-8")
    assert receipts.scan_once(loaded, [inbox], {}, inbox=inbox) == 3
    assert not (inbox / ".gitignore").exists()
    statuses = sorted(r["status"] for r in receipts.list_receipts(loaded))
    assert statuses == ["accepted", "accepted", "invalid"]


def test_scan_runs_receipts_on_executor(loaded, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    root = tmp_path / "w"
    d = root / ".waypoint" / "receipts"
    d.mkdir(parents=True)
    (d / "s.json").write_text(json.dumps(rc()), encoding="utf-8")
    with ThreadPoolExecutor(max_workers=1) as ex:
        receipts.scan_once(loaded, [root], {}, executor=ex, inbox=tmp_path / "x")
    assert len(receipts.list_receipts(loaded)) == 1


# ---------------------------------------------------------------- routes
class FakeApp:
    def __init__(self, store):
        self.store = store


class FakeH:
    actor = "router"

    def __init__(self, store, body):
        self.app, self._b = FakeApp(store), body

    def _json_body(self):
        return self._b


def test_route_receipt_object_and_list(loaded):
    out = receipts.p_receipt(FakeH(loaded, {"receipt": rc(), "session": "s1"}), {})
    assert out["ok"] and out["status"] == "accepted"
    again = receipts.p_receipt(FakeH(loaded, {"receipt": rc(), "session": "s1"}), {})
    assert again["status"] == "duplicate"
    bad = receipts.p_receipt(FakeH(loaded, {"receipt": {"schema": "x"}}), {})
    assert bad["ok"] is False and bad["status"] == "invalid" and bad["reason"]
    lst = receipts.g_receipts(FakeH(loaded, {}), {"limit": ["1"]})
    assert len(lst["receipts"]) == 1 and lst["receipts"][0]["status"] == "invalid"  # newest first
    assert receipts.ROUTES["GET /api/receipts"] and receipts.ROUTES["POST /api/receipt"]


def test_route_path_must_be_json_inside_waypoint_folder(loaded, tmp_path):
    outside = tmp_path / "r.json"
    outside.write_text(json.dumps(rc()))
    with pytest.raises(api.HttpError) as e:
        receipts.p_receipt(FakeH(loaded, {"path": str(outside)}), {})
    assert e.value.status == 400
    d = tmp_path / "w" / ".waypoint" / "receipts"
    d.mkdir(parents=True)
    (d / "ok.json").write_text(json.dumps(rc()))
    (d / "note.txt").write_text("x")
    with pytest.raises(api.HttpError):
        receipts.p_receipt(FakeH(loaded, {"path": str(d / "note.txt")}), {})
    out = receipts.p_receipt(FakeH(loaded, {"path": str(d / "ok.json")}), {})
    assert out["status"] == "accepted"
    with pytest.raises(api.HttpError):  # L4: a relative path is refused
        receipts.p_receipt(FakeH(loaded, {"path": ".waypoint/receipts/ok.json", "cwd": str(tmp_path / "w")}), {})
    with pytest.raises(api.HttpError):
        receipts.p_receipt(FakeH(loaded, {}), {})


def test_route_missing_file_is_404(loaded, tmp_path):
    with pytest.raises(api.HttpError) as e:
        receipts.p_receipt(FakeH(loaded, {"path": str(tmp_path / ".waypoint" / "gone.json")}), {})
    assert e.value.status == 404


# ---------------------------------------------------------------- review fixes (H1, H2, M3, M5, M6, M12, L3-L6)
import os  # noqa: E402
import threading  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

from cc_engine import checks, claims  # noqa: E402


def _done_a(store):
    checks.complete_node(store, "fx", "A", runner=_pass)


def _with_left(tmp_path, name):
    d = tmp_path / name
    d.mkdir()
    (d / "left.txt").write_text("x")
    return d


def test_h1_check_folder_is_never_the_receipt_cwd(loaded, tmp_path):
    _done_a(loaded)
    other = _with_left(tmp_path, "other")  # holds left.txt; the project folder does not
    receipts.ingest(loaded, raw(rc(project="fx", node="B")), cwd=str(other))
    assert node_status(loaded, "B") == "error"
    # with no claim the project cwd decides, even when the receipt's cwd is wrong
    (tmp_path / "left.txt").write_text("x")
    empty = tmp_path / "empty"
    empty.mkdir()
    receipts.ingest(loaded, raw(rc(project="fx", node="B", summary="Again, with more.")), cwd=str(empty))
    assert node_status(loaded, "B") == "done"


def test_h2_check_runs_in_the_claim_worktree_even_without_cwd(loaded, tmp_path):
    _done_a(loaded)
    wt = _with_left(tmp_path, "wt")  # the project folder has no left.txt
    claims.claim(loaded, "fx", "B", "w1", worktree=str(wt))
    out = receipts.ingest(loaded, raw(rc(project="fx", node="B")), session="w1")
    assert node_status(loaded, "B") == "done"
    assert receipts.get(loaded, out["id"])["outcome"] == "check passed"
    assert loaded.claim_row("fx", "B") is None


def test_h2_claim_cwd_used_when_no_worktree(loaded, tmp_path):
    _done_a(loaded)
    d = _with_left(tmp_path, "cw")
    claims.claim(loaded, "fx", "B", "w1", cwd=str(d))
    receipts.ingest(loaded, raw(rc(project="fx", node="B")), session="w1", cwd=str(tmp_path / "nowhere"))
    assert node_status(loaded, "B") == "done"


def test_outputs_resolve_against_cwd(loaded, tmp_path):
    receipts.ingest(loaded, raw(rc(todo={"text": "Read", "section": "human"}, outputs=["out/a.md"])),
                    cwd=str(tmp_path))
    d = todo.list_todos(loaded)[0]["deliverables"][0]
    assert os.path.normcase(d["path"]) == os.path.normcase(str(tmp_path / "out" / "a.md"))


def test_m3_same_receipt_after_a_fresh_claim_is_processed_again(loaded):
    b = raw(rc(project="fx", node="A", session="w1"))
    t1 = datetime.now(timezone.utc)
    claims.claim(loaded, "fx", "A", "w1", now=t1)
    first = receipts.ingest(loaded, b, runner=_fail)
    assert first["status"] == "accepted" and node_status(loaded) == "error"
    loaded.set_node_status("fx", "A", "not_started", "test", "retry")
    claims.claim(loaded, "fx", "A", "w1", now=t1 + timedelta(seconds=5))
    second = receipts.ingest(loaded, b, runner=_pass)
    assert second["status"] == "accepted" and second["id"] != first["id"]
    assert node_status(loaded) == "done"


def test_m3_same_content_same_claim_or_no_claim_stays_duplicate(loaded):
    claims.claim(loaded, "fx", "A", "holder")
    b = raw(rc(project="fx", node="A", session="stranger"))
    first = receipts.ingest(loaded, b, runner=_fail)
    assert receipts.ingest(loaded, b, runner=_fail)["status"] == "duplicate" and first["status"] == "accepted"
    # processed under a claim that was then released: the same bytes with no claim are a duplicate too
    c = raw(rc(project="fx", node="E", summary="Seen under a claim."))
    claims.claim(loaded, "fx", "E", "w9")
    receipts.ingest(loaded, c, session="w9", runner=_fail)
    assert loaded.claim_row("fx", "E") is None
    assert receipts.ingest(loaded, c, session="w9", runner=_fail)["status"] == "duplicate"


def test_m5_receipt_from_a_stranger_keeps_the_claim(loaded):
    claims.claim(loaded, "fx", "A", "worker-2")
    out = receipts.ingest(loaded, raw(rc(project="fx", node="A", session="someone-else")), runner=_pass)
    assert loaded.claim_row("fx", "A")["claimed_by"] == "worker-2"
    assert "claim kept: held by worker-2" in receipts.get(loaded, out["id"])["outcome"]
    assert node_status(loaded) == "done"  # the status is still processed


def test_m5_holder_by_agent_path_or_stale_releases(loaded, tmp_path):
    claims.claim(loaded, "fx", "A", "worker-2")
    receipts.ingest(loaded, raw(rc(project="fx", node="A", agent="worker-2")), runner=_pass)
    assert loaded.claim_row("fx", "A") is None
    wt = tmp_path / "wt"
    (wt / ".waypoint" / "receipts").mkdir(parents=True)
    claims.claim(loaded, "fx", "B", "worker-3", worktree=str(wt))
    f = wt / ".waypoint" / "receipts" / "fx.B.json"
    receipts.ingest(loaded, raw(rc(project="fx", node="B", status="error")), path=str(f))
    assert loaded.claim_row("fx", "B") is None
    old = datetime.now(timezone.utc) - timedelta(days=1)
    claims.claim(loaded, "fx", "C", "worker-4", now=old)
    receipts.ingest(loaded, raw(rc(project="fx", node="C", status="error")), session="nobody")
    assert loaded.claim_row("fx", "C") is None


def test_m6_error_receipt_never_overwrites_a_done_node(loaded):
    _done_a(loaded)
    out = receipts.ingest(loaded, raw(rc(project="fx", node="A", status="error", summary="Late.",
                                         todo={"text": "Look at A", "section": "human"})))
    assert node_status(loaded) == "done"
    assert receipts.get(loaded, out["id"])["outcome"] == "node already done; status ignored"
    assert len(todo.list_todos(loaded)) == 1
    out2 = receipts.ingest(loaded, raw(rc(project="fx", node="A", status="needs_you", summary="Late too.",
                                          questions=[{"kind": "text", "prompt": "Still ok?"}])))
    assert node_status(loaded) == "done"
    assert receipts.get(loaded, out2["id"])["outcome"] == "node already done; status ignored"
    assert "Still ok?" in [x["prompt"] for x in needs.list_questions(loaded)]


def _receipt_files(root, n, tag):
    d = root / ".waypoint" / "receipts"
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (d / f"{tag}{i}.json").write_text(json.dumps(rc(summary=f"{tag} {i}")), encoding="utf-8")


def test_m12_unchanged_files_do_not_use_the_budget(loaded, tmp_path, monkeypatch):
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    _receipt_files(r1, 3, "a")
    seen = {}
    inbox = tmp_path / "no-inbox"
    assert receipts.scan_once(loaded, [r1, r2], seen, inbox=inbox) == 3
    monkeypatch.setattr(receipts, "MAX_FILES", 2)
    _receipt_files(r2, 1, "b")
    assert receipts.scan_once(loaded, [r1, r2], seen, inbox=inbox) == 1
    # the cap still limits reads, and what was skipped is picked up on the next pass
    _receipt_files(r2, 4, "c")
    assert receipts.scan_once(loaded, [r1, r2], seen, inbox=inbox) == 2
    assert receipts.scan_once(loaded, [r1, r2], seen, inbox=inbox) == 2


def _symlink(link, target, directory=False):
    try:
        os.symlink(target, link, target_is_directory=directory)
        return
    except (OSError, NotImplementedError):
        pass
    if directory and os.name == "nt":  # a junction needs no privilege
        import subprocess
        done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        if done.returncode == 0:
            return
    pytest.skip("cannot create symlinks here")


def test_l3_symlinked_file_is_skipped(loaded, tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(rc(summary="From outside.")))
    root = tmp_path / "w"
    d = root / ".waypoint" / "receipts"
    d.mkdir(parents=True)
    _symlink(d / "link.json", outside)
    assert receipts.scan_once(loaded, [root], {}, inbox=tmp_path / "x") == 0
    assert receipts.list_receipts(loaded) == []


def test_l3_linked_waypoint_folder_is_skipped(loaded, tmp_path):
    real = tmp_path / "w2"
    (real / "receipts").mkdir(parents=True)
    (real / "receipts" / "r.json").write_text(json.dumps(rc(summary="Behind a link.")))
    root2 = tmp_path / "w3"
    root2.mkdir()
    _symlink(root2 / ".waypoint", real, directory=True)
    assert receipts.scan_once(loaded, [root2], {}, inbox=tmp_path / "x") == 0
    assert receipts.list_receipts(loaded) == []


def test_l3_oversize_file_is_refused_from_its_size(loaded, tmp_path, monkeypatch):
    root = tmp_path / "w"
    d = root / ".waypoint" / "receipts"
    d.mkdir(parents=True)
    (d / "big.json").write_bytes(b" " * (64 * 1024 + 10))

    def no_read(*a, **k):
        raise AssertionError("must not read an oversize file")
    monkeypatch.setattr(receipts.Path, "read_bytes", no_read)
    assert receipts.scan_once(loaded, [root], {}, inbox=tmp_path / "x") == 1
    rows = receipts.list_receipts(loaded)
    assert len(rows) == 1 and rows[0]["status"] == "invalid" and "64 KB" in rows[0]["reason"]


@pytest.mark.parametrize("bad", ["\\\\server\\share\\.waypoint\\r.json", "//server/share/.waypoint/r.json",
                                 "\\\\?\\C:\\x\\.waypoint\\r.json", ".waypoint/r.json"])
def test_l4_unc_and_relative_paths_are_refused(loaded, bad):
    with pytest.raises(api.HttpError) as e:
        receipts.p_receipt(FakeH(loaded, {"path": bad, "cwd": "."}), {})
    assert e.value.status == 400


def test_l5_gitignore_not_written_through_a_dangling_link(loaded, tmp_path):
    folder = tmp_path / "w" / ".waypoint"
    folder.mkdir(parents=True)
    target = tmp_path / "nowhere.txt"
    _symlink(folder / ".gitignore", target)
    receipts._guard_folder(folder)
    assert not target.exists()
    other = tmp_path / "w2" / ".waypoint"
    other.mkdir(parents=True)
    receipts._guard_folder(other)
    assert (other / ".gitignore").read_text() == "*\n"


def test_l6_pool_has_two_workers_and_keeps_node_order(loaded):
    w = receipts.Watcher(FakeApp(loaded))
    assert w.pool.workers == 2
    w.pool.shutdown(wait=True)
    pool = receipts.KeyedPool(2)
    gate, order, started = threading.Event(), [], []

    def task(tag):
        started.append(tag)
        if tag == "a1":
            gate.wait(5)
        order.append(tag)

    pool.submit(task, "a1", key="fx/A")
    pool.submit(task, "a2", key="fx/A")
    pool.submit(task, "b1", key="fx/B").result(5)
    assert order == ["b1"] and "a2" not in started  # B ran while A1 was busy; A2 waits for A1
    gate.set()
    pool.shutdown(wait=True)
    assert order == ["b1", "a1", "a2"]
