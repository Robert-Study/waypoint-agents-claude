"""Subagent chats v3: stream parsing (steps, +/- counts), resume argv, session list. Fakes only."""
import json

from cc_engine import subagent_chats as sc


def jl(*evs):
    return "\n".join(json.dumps(e) for e in evs)


CLAUDE = jl(
    {"type": "system", "subtype": "init", "session_id": "sess-1"},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "Looking."},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}}]}},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit",
        "input": {"file_path": "a.py", "old_string": "x\ny", "new_string": "x\nz\nw"}}]}},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Write",
        "input": {"file_path": "b.py", "content": "1\n2\n3"}}]}},
    {"type": "result", "session_id": "sess-1", "result": "All done."},
)

CODEX = jl(
    {"type": "thread.started", "thread_id": "th-9"},
    {"type": "item.completed", "item": {"type": "command_execution", "command": "ls"}},
    {"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": "c.py", "kind": "update"}]}},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "Fixed."}},
)


def test_claude_stream_summary():
    s = sc.stream_summary("claude_code", CLAUDE)
    assert s["session"] == "sess-1" and s["reply"] == "All done."
    assert [st["text"].split()[0] for st in s["steps"]] == ["Read", "Edit", "Write"]
    files = {f["path"]: f for f in s["files"]}
    assert (files["a.py"]["added"], files["a.py"]["removed"]) == (3, 2)
    assert (files["b.py"]["added"], files["b.py"]["removed"]) == (3, 0)


def test_codex_stream_summary():
    s = sc.stream_summary("codex", CODEX)
    assert s["session"] == "th-9" and s["reply"] == "Fixed."
    assert len(s["steps"]) == 2
    assert [f["path"] for f in s["files"]] == ["c.py"]


def test_garbage_lines_ignored():
    s = sc.stream_summary("claude_code", "not json\n{bad\n" + CLAUDE)
    assert s["session"] == "sess-1"


def test_resume_argv():
    assert sc.resume_argv("claude_code", "abc", "hi")[:4] == ["claude", "-p", "--resume", "abc"]
    c = sc.resume_argv("codex", "t1", "hi")
    assert c[:4] == ["codex", "exec", "resume", "t1"] and "--json" in c
    assert sc.resume_argv("freebuff", "x", "hi") is None


def test_session_list_filters_and_marks_resumable(tmp_path):
    job = tmp_path / "jobs" / "p" / "n" / "1"
    job.mkdir(parents=True)
    (job / "spec.json").write_text(json.dumps({"route": "claude_code", "argv": ["claude"], "cwd": "C:/w"}))
    (job / "launch.json").write_text(json.dumps({"pid": 1, "started_at": 1_700_000_000.0}))
    (job / "out.txt").write_text(CLAUDE)
    (job / "exit.json").write_text(json.dumps({"exit_code": 0}))
    rows = sc.session_list(tmp_path, tmp_path / "none", None, lambda *a: False, agent="claude_code")
    assert len(rows) == 1 and rows[0]["resumable"] and rows[0]["session"] == "sess-1"
    assert rows[0]["files"] and rows[0]["steps"]
    assert sc.session_list(tmp_path, tmp_path / "none", None, lambda *a: False, agent="codex") == []


def test_review_fixes_robust_parsing_and_dash_safe_resume():
    from cc_engine import subagent_chats as sc
    # non-dict tool input and non-list content never crash
    sc.stream_summary("claude_code", '{"message": {"content": [{"type": "tool_use", "name": "Edit", "input": 123}]}}')
    sc.stream_summary("claude_code", '{"message": {"content": 7}}')
    # codex counts are used when present, paths-only still works
    out = sc.stream_summary("codex", '{"type": "item.completed", "item": {"type": "file_change", '
                                     '"changes": [{"path": "a.py", "additions": 10, "deletions": 2}, {"path": "b.py"}]}}')
    files = {f["path"]: f for f in (out.get("files") if isinstance(out, dict) else out[1])}
    assert files["a.py"]["added"] == 10 and files["a.py"]["removed"] == 2 and files["b.py"]["added"] == 0
    # a message starting with a dash is never read as a flag
    for agent in ("claude_code", "codex"):
        argv = sc.resume_argv(agent, "s1", "--help")
        assert argv[-2:] == ["--", "--help"]
