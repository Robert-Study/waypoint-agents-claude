"""Transcript tailer: incremental, deduped meters, bounded reads. Fixture files only."""
import json
import os
import time

from cc_engine import transcripts
from cc_engine.transcripts import Tailer

PRICES = {"context_window_default": 200000, "context_window_1m": 1000000,
          "families": {"claude-opus": {"input": 10, "output": 50, "cache_read": 1, "cache_write": 12.5},
                       "claude-sonnet": {"input": None, "output": None, "cache_read": None, "cache_write": None}}}


def asst(rid, out, text=None, tool=None, model="claude-opus-9", inp=10, cr=100, cc=50, ts="2026-11-05T10:00:00.000Z",
         uid=None):
    content = []
    if text:
        content.append({"type": "text", "text": text})
    if tool:
        content.append({"type": "tool_use", "id": tool[0], "name": tool[1], "input": {}})
    return {"type": "assistant", "requestId": rid, "timestamp": ts, "cwd": "C:/work/demo", "uuid": uid or f"u{rid}{out}",
            "message": {"model": model, "role": "assistant", "content": content,
                        "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr,
                                  "cache_creation_input_tokens": cc}}}


def result(tool_id, ts="2026-11-05T10:00:05.000Z"):
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}]}}


def write(path, rows, end="\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + end, encoding="utf-8")


def append(path, rows, end="\n"):
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(r) for r in rows) + end)


def tailer(root, **kw):
    kw.setdefault("prices", PRICES)
    kw.setdefault("recent_days", None)
    return Tailer(roots=[root], **kw)


def test_meters_dedupe_by_request_id_and_largest_output(tmp_path):
    f = tmp_path / "slug" / "sess-1.jsonl"
    write(f, [asst("r1", 5, text="Starting the work.\nSecond line"), asst("r1", 40, tool=("t1", "Bash")),
              asst("r2", 20, text="x" * 300, cr=200, cc=0), {"type": "ai-title", "aiTitle": "Demo task"}])
    t = tailer(tmp_path)
    assert t.poll() == [str(f)]
    m = t.snapshot()["sess-1"]
    assert m["tokens_out"] == 60 and m["tokens_in"] == 20  # r1 counted once (40), r2 once (20)
    assert m["cache_read"] == 300 and m["cache_creation"] == 50
    assert m["context"] == 10 + 200 + 0  # the last request only
    assert m["model"] == "claude-opus-9" and m["context_window"] == 200000
    assert m["last_line"] == "x" * 140
    assert m["tool"] == "Bash" and m["cwd"] == "C:/work/demo" and m["title"] == "Demo task"
    assert m["updated_at"] == "2026-11-05T10:00:00.000Z" and m["kind"] == "session" and m["parent"] is None
    expect = (20 * 10 + 60 * 50 + 300 * 1 + 50 * 12.5) / 1e6
    assert abs(m["cost_eq"] - expect) < 1e-6


def test_tool_clears_when_result_arrives_and_unknown_model_has_no_cost(tmp_path):
    f = tmp_path / "s" / "a.jsonl"
    write(f, [asst("r1", 5, tool=("t1", "Read"), model="claude-sonnet-9")])
    t = tailer(tmp_path)
    t.poll()
    assert t.snapshot()["a"]["tool"] == "Read" and t.snapshot()["a"]["cost_eq"] is None  # null prices
    append(f, [result("t1")])
    t.poll()
    assert t.snapshot()["a"]["tool"] is None


def test_unknown_family_and_1m_window(tmp_path):
    write(tmp_path / "s" / "a.jsonl", [asst("r1", 5, model="other-model")])
    write(tmp_path / "s" / "b.jsonl", [asst("r1", 5, model="claude-opus-9[1m]")])
    t = tailer(tmp_path)
    t.poll()
    snap = t.snapshot()
    assert snap["a"]["cost_eq"] is None and snap["a"]["context_window"] == 200000
    assert snap["b"]["context_window"] == 1000000 and snap["b"]["cost_eq"] is not None


def test_truncated_last_line_completes_on_next_poll(tmp_path):
    f = tmp_path / "s" / "a.jsonl"
    write(f, [asst("r1", 5)])
    line = json.dumps(asst("r2", 7)) + "\n"
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(line[:40])
    t = tailer(tmp_path)
    t.poll()
    assert t.snapshot()["a"]["tokens_out"] == 5
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(line[40:])
    t.poll()
    assert t.snapshot()["a"]["tokens_out"] == 12


def test_incremental_reads_only_new_bytes_and_reset_on_truncation(tmp_path):
    f = tmp_path / "s" / "a.jsonl"
    write(f, [asst("r1", 5), asst("r2", 6)])
    t = tailer(tmp_path)
    t.poll()
    off = t.files[str(f)]["offset"]
    assert off == f.stat().st_size
    append(f, [asst("r3", 10)])
    t.poll()
    assert t.snapshot()["a"]["tokens_out"] == 21
    write(f, [asst("n1", 3)])  # replaced by a shorter file
    t.poll()
    assert t.snapshot()["a"]["tokens_out"] == 3


def test_subagent_file_and_meta(tmp_path):
    sub = tmp_path / "slug" / "sess-9" / "subagents"
    write(sub / "agent-abc123.jsonl", [asst("r1", 9, text="Done.")])
    (sub / "agent-abc123.meta.json").write_text(json.dumps(
        {"agentType": "general-purpose", "description": "Check the thing", "finished": True}), encoding="utf-8")
    t = tailer(tmp_path)
    t.poll()
    m = t.snapshot()["sess-9/abc123"]
    assert m["kind"] == "subagent" and m["parent"] == "sess-9" and m["session_id"] == "sess-9"
    assert m["agent_id"] == "abc123" and m["agent_type"] == "general-purpose"
    assert m["description"] == "Check the thing" and m["finished"] is True and m["tokens_out"] == 9


def test_old_files_skipped_on_first_scan_until_they_grow(tmp_path):
    f = tmp_path / "s" / "old.jsonl"
    write(f, [asst("r1", 5)])
    old = time.time() - 30 * 86400
    os.utime(f, (old, old))
    t = Tailer(roots=[tmp_path], prices=PRICES, recent_days=7)
    t.poll()
    assert t.reads == 0 and "old" not in t.snapshot()
    append(f, [asst("r2", 8)])
    t.poll()
    assert t.snapshot()["old"]["tokens_out"] == 13  # now active: read whole file


def test_byte_budget_leaves_the_rest_for_the_next_poll(tmp_path):
    f = tmp_path / "s" / "a.jsonl"
    write(f, [asst(f"r{i}", 1, text="y" * 200) for i in range(40)])
    t = tailer(tmp_path, max_bytes=3000)
    t.poll()
    first = t.snapshot()["a"]["tokens_out"]
    assert 0 < first < 40
    for _ in range(40):
        t.poll()
    assert t.snapshot()["a"]["tokens_out"] == 40


def test_idle_poll_over_200_files_only_stats(tmp_path):
    for i in range(200):
        write(tmp_path / "slug" / f"s{i}.jsonl", [asst("r1", 1)])
    t = tailer(tmp_path)
    t.poll()
    assert t.reads == 200
    t.poll()
    t.poll()
    assert t.reads == 200  # no file was opened again
    assert len(t.snapshot()) == 200


def test_bad_lines_and_missing_roots_are_ignored(tmp_path):
    f = tmp_path / "s" / "a.jsonl"
    f.parent.mkdir()
    f.write_text("not json\n[1,2]\n" + json.dumps(asst("r1", 4)) + "\n", encoding="utf-8")
    t = Tailer(roots=[tmp_path / "nowhere", tmp_path], prices=PRICES, recent_days=None)
    t.poll()
    assert t.snapshot()["a"]["tokens_out"] == 4


def test_shipped_prices_file_is_valid():
    p = transcripts.load_prices()
    assert "_note" in p and set(p["families"]) >= {"claude-opus-5", "claude-sonnet-5", "claude-haiku-5", "claude-fable-5"}
    assert transcripts.request_cost("claude-opus-5-5", 1_000_000, 0, 0, 0, p) == 4
    assert transcripts.context_window("claude-opus-5-5", p) == 1000000
    assert transcripts.context_window("claude-haiku-4-5", p) == 200000


def test_newest_files_get_the_read_budget_first(tmp_path):
    import os, time
    old = tmp_path / "proj" / "old.jsonl"
    new = tmp_path / "proj" / "new.jsonl"
    old.parent.mkdir(parents=True)
    line = json.dumps({"type": "user", "message": {"role": "user", "content": "x" * 2000}}) + "\n"
    old.write_bytes((line * 5).encode("utf-8"))
    new.write_bytes((line * 5).encode("utf-8"))
    t0 = time.time()
    os.utime(old, (t0 - 3600, t0 - 3600))
    os.utime(new, (t0, t0))
    t = Tailer(roots=[tmp_path], prices=PRICES, recent_days=None, max_bytes=new.stat().st_size)
    assert t.poll() == [str(new)]  # the whole budget went to the newest file; the old one waits
