"""Desktop session links from fixture folders only."""
import json
import os

from cc_engine import desktop_sessions as ds


def rec(folder, local, cli, **extra):
    d = {"sessionId": f"local_{local}", "cliSessionId": cli, "cwd": "C:/work/demo", "lastActivityAt": 1793869411811,
         "isArchived": False, "title": f"Title {local}"}
    d.update(extra)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"local_{local}.json").write_text(json.dumps(d), encoding="utf-8")


def test_find_roots_from_injected_env(tmp_path):
    local, roaming = tmp_path / "local", tmp_path / "roaming"
    (local / "Claude-Data" / "acct1" / "claude-code-sessions").mkdir(parents=True)
    (local / "Claude-Data" / "acct2").mkdir(parents=True)  # no sessions folder: ignored
    (roaming / "Claude" / "claude-code-sessions").mkdir(parents=True)
    roots = ds.find_roots({"LOCALAPPDATA": str(local), "APPDATA": str(roaming)})
    assert len(roots) == 2 and roots[0].endswith("claude-code-sessions")
    assert ds.find_roots({}) == []


def test_parent_link_and_fields(tmp_path):
    acct = tmp_path / "org" / "acct"
    rec(acct, "p1", "cli-parent", title="Lead")
    rec(acct, "c1", "cli-child", spawnedFrom={"sessionId": "local_p1", "name": "Worker E2"}, isArchived=True,
        postTurnSummary={"status_category": "done"})
    links = ds.read_links([tmp_path], min_interval=0)
    assert links["cli-child"]["parent_cli_session_id"] == "cli-parent"
    assert links["cli-child"]["name"] == "Worker E2" and links["cli-child"]["archived"] is True
    assert links["cli-child"]["status_category"] == "done" and links["cli-child"]["local_id"] == "local_c1"
    assert links["cli-parent"]["parent_cli_session_id"] is None and links["cli-parent"]["title"] == "Lead"
    assert links["cli-parent"]["last_activity_at"].startswith("2026-")


def test_bad_json_deleted_and_missing_keys_are_skipped(tmp_path):
    acct = tmp_path / "org" / "acct"
    rec(acct, "ok", "cli-ok")
    (acct / "local_bad.json").write_text("{nope", encoding="utf-8")
    (acct / "local_nocli.json").write_text(json.dumps({"sessionId": "local_nocli"}), encoding="utf-8")
    (acct / "deleted_x.json").write_text(json.dumps({"cliSessionId": "cli-del"}), encoding="utf-8")
    rec(acct, "orph", "cli-orph", spawnedFrom={"sessionId": "local_gone"})
    links = ds.read_links([tmp_path], min_interval=0)
    assert set(links) == {"cli-ok", "cli-orph"}
    assert links["cli-orph"]["parent_cli_session_id"] is None


def test_cached_between_scans_and_rescans_on_change(tmp_path):
    acct = tmp_path / "org" / "acct"
    rec(acct, "a", "cli-a", title="One")
    now = [100.0]
    first = ds.read_links([tmp_path], min_interval=15, clock=lambda: now[0], force=True)
    rec(acct, "a", "cli-a", title="Two")
    st = (acct / "local_a.json").stat()
    os.utime(acct / "local_a.json", (st.st_atime, st.st_mtime + 5))
    now[0] = 105.0
    assert ds.read_links([tmp_path], min_interval=15, clock=lambda: now[0]) is first  # within 15 s: cached
    now[0] = 120.0
    assert ds.read_links([tmp_path], min_interval=15, clock=lambda: now[0])["cli-a"]["title"] == "Two"
