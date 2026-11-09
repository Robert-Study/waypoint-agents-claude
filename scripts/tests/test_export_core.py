"""scripts/export_core.py: the Waypoint Core export (allowlist, rewrites, excluded tests, --into sync) and its scanner.

The scanner tests run everywhere (the product ships export_core.py for `--scan`); the export tests need the source
repo and skip in the product. Personal strings in fixtures are built from rot13 text so this file scans clean."""
import codecs
import importlib.util
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("export_core", REPO / "scripts" / "export_core.py")
ec = importlib.util.module_from_spec(spec)
sys.modules["export_core"] = ec
spec.loader.exec_module(ec)

SOURCE = (REPO / "scripts" / "export_standalone.py").is_file()
needs_source = pytest.mark.skipif(not SOURCE, reason="export needs the source repo")
B = chr(92)


def r13(s):
    return codecs.decode(s, "rot13")


# ---------------------------------------------------------------- scanner (runs in the product too)
def test_scan_catches_personal_strings_employers_mail_and_secrets(tmp_path):
    (tmp_path / "a.py").write_text("p = 'C:" + B + "Users" + B + "home" + B + "x'\n", encoding="utf-8")
    (tmp_path / "b.md").write_text(r13("frr Zl Qevir naq NV-Rkpunatr") + "\n", encoding="utf-8")
    (tmp_path / "c.ts").write_text("const e = '" + r13("Unegerr") + "';\n", encoding="utf-8")
    (tmp_path / "d.json").write_text('{"x": "' + r13("OynpxEbpx") + ' analyst"}\n', encoding="utf-8")
    (tmp_path / "e.txt").write_text(r13("znvy@eboregtneqare") + ".example\n" + r13("eboregthltneqare") + "\n",
                                    encoding="utf-8")
    (tmp_path / "f.txt").write_text("key = AIza" + "SyD" + "a" * 32 + "\nghp_" + "c" * 36 + "\n", encoding="utf-8")
    (tmp_path / "LICENSE").write_text("Copyright " + r13("Eboreg") + "\n", encoding="utf-8")
    (tmp_path / "ok.txt").write_text("nothing here\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "x.md").write_text(r13("Zl Qevir") + "\n", encoding="utf-8")  # .git is never scanned
    found = ec.scan(tmp_path)
    files = {f["file"] for f in found}
    assert {"a.py", "b.md", "c.ts", "d.json", "e.txt", "f.txt", "LICENSE"} <= files, found
    assert "ok.txt" not in files and ".git/x.md" not in files
    rules = {f["rule"] for f in found}
    assert {"employer", "email"} <= rules
    assert all("c" * 20 not in f["match"] for f in found)  # secrets are masked


def test_scan_allows_only_the_exact_license_line(tmp_path):
    line = r13("Pbclevtug (p) 2026 Eboreg Tneqare")
    (tmp_path / "LICENSE").write_text("MIT License\n\n" + line + "\r\n", encoding="utf-8")
    assert ec.scan(tmp_path) == []
    (tmp_path / "README.md").write_text(line + "\n", encoding="utf-8")  # same line elsewhere: found
    (tmp_path / "LICENSE").write_text(line + " and " + r13("Eboreg") + "\n", encoding="utf-8")  # not exact: found
    assert {f["file"] for f in ec.scan(tmp_path)} == {"README.md", "LICENSE"}


@needs_source
def test_rewrites_keep_the_allowed_license_line():
    line = r13("Pbclevtug (p) 2026 Eboreg Tneqare")
    out = ec.rewrite_text("LICENSE", "MIT License\n" + line + "\nby " + r13("Eboreg Tneqare") + "\n")
    assert out.splitlines() == ["MIT License", line, "by the owner"]
    assert line not in ec.rewrite_text("README.md", line + "\n")


def test_scan_cli_exit_codes(tmp_path):
    (tmp_path / "ok.md").write_text("fine\n", encoding="utf-8")
    assert ec.main(["--scan", str(tmp_path)]) == 0
    (tmp_path / "bad.md").write_text(r13("Unegerr") + "\n", encoding="utf-8")
    assert ec.main(["--scan", str(tmp_path)]) == 1
    assert ec.main(["--scan", str(tmp_path / "missing")]) == 2
    assert ec.main([]) == 2


def test_export_core_scans_itself_clean():
    tmp = [p for p in ec.scan(REPO / "scripts") if p["file"] in ("export_core.py", "tests/test_export_core.py",
                                                                 "tests/test_core_install.py")]
    assert tmp == [], tmp


@needs_source
def test_bundled_rules_equal_export_standalone():
    es = ec.load_standalone()
    assert [(n, rx.pattern, rx.flags) for n, rx in ec.BUNDLED_PERSONAL] == \
        [(n, rx.pattern, rx.flags) for n, rx in es.PERSONAL]
    assert [(n, rx.pattern, rx.flags) for n, rx in ec.BUNDLED_SECRETS] == \
        [(n, rx.pattern, rx.flags) for n, rx in es.SECRETS]


# ---------------------------------------------------------------- export (source repo only)
@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    if not SOURCE:
        pytest.skip("export needs the source repo")
    target = tmp_path_factory.mktemp("core-export") / "out"
    return target, ec.export(REPO, target)


@needs_source
def test_export_scans_clean(exported):
    target, result = exported
    assert result["findings"] == [], result["findings"][:10]
    assert ec.scan(target) == []


@needs_source
def test_install_docs_point_at_the_public_repo_and_its_folder(exported):
    """Regression: the owner rewrite once turned the clone URL into a dead account, and `cd` named the old folder."""
    import re
    target, _ = exported
    owner = r13("Eboreg-Fghql")
    bad = []
    for p in target.rglob("*.md"):
        if ".git" in p.parts:
            continue
        text = p.read_text(encoding="utf-8")
        if r13("Bjare-Fghql") in text or r13("bjare-fghql") in text.lower():
            bad.append(f"{p.name}: stray account name")
        for m in re.finditer(r"git clone https://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?\s*\n(?:\s*)cd (\S+)", text):
            if m.group(1) != owner or m.group(3) != m.group(2):
                bad.append(f"{p.name}: {m.group(0)!r}")
    assert bad == [], bad
    readme = (target / "README.md").read_text(encoding="utf-8")
    assert f"git clone https://github.com/{owner}/waypoint-agents.git\ncd waypoint-agents\n" in readme


def test_public_urls_survive_rewrite_and_pass_the_scan(tmp_path):
    url = "https://" + ec.PUBLIC_URLS[1] + ".git"
    if SOURCE:
        assert url in ec.rewrite_text("README.md", "git clone " + url + "\n")
        assert ec.PUBLIC_URLS[0] in ec.rewrite_text("README.md", ec.PUBLIC_URLS[0] + "\n")
    (tmp_path / "README.md").write_text("git clone " + url + "\n", encoding="utf-8")
    assert ec.scan(tmp_path) == []
    (tmp_path / "README.md").write_text(r13("Eboreg") + " " + url + "\n", encoding="utf-8")
    assert ec.scan(tmp_path) != []  # only the address is allowed, not the name elsewhere on the line


@needs_source
def test_export_file_set(exported):
    target, _ = exported
    files = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()}
    for need in ("engine/cc_engine/api.py", "engine/cc_engine/learn_seed.json", "engine/pytest.ini",
                 "engine/tests/conftest.py", "scripts/waypoint_cli.py", "scripts/waypoint.cmd",
                 "scripts/waypoint_config.ps1", "scripts/Start-Engine.ps1", "scripts/Stop-Engine.ps1",
                 "scripts/Register-Engine.ps1", "scripts/Register-Tick.ps1", "scripts/Register-DbBackup.ps1",
                 "scripts/backup_db.py", "scripts/waypoint_spend_guard.py", "scripts/export_core.py",
                 "scripts/tests/test_export_core.py", "scripts/tests/test_core_install.py",
                 "claude_mod/.claude-plugin/marketplace.json", "claude_mod/waypoint/.claude-plugin/plugin.json",
                 "claude_mod/tests/waypoint.test.mjs", "install.ps1", "uninstall.ps1", "bin/waypoint.cmd"):
        assert need in files, need
    for name in ("README.md", "INSTALL.md", "LICENSE", ".gitignore", ".github/workflows/ci.yml",
                 "RELEASE_CHECKLIST.md", "docs/API.md", "docs/CLI.md"):
        if (REPO / "core" / name).is_file():
            assert name in files, name
    bad_roots = ("plans/", "data/", "plugin/", "standalone/", "sync/", "engine/proposals/")
    assert not [f for f in files if f.startswith(bad_roots)]
    assert not [f for f in files if f.startswith("core/") and not f.startswith("core/waypoint-data/")]
    assert "AGENTS.md" not in files and "CONTRACT.md" not in files
    assert not [f for f in files if f.startswith("docs/") and not (REPO / "core" / f).is_file()]
    assert not [f for f in files if "/data/" in f or "__pycache__" in f
                or f.endswith((".pyc", ".sqlite", ".log", ".token", ".pid"))]
    assert "scripts/export_standalone.py" not in files and "scripts/tests/test_export_standalone.py" not in files
    seed = json.loads((target / "engine/cc_engine/learn_seed.json").read_text(encoding="utf-8"))
    assert seed["sources"] == []  # the neutral example seed


@needs_source
def test_excluded_tests_are_left_out_and_each_has_a_reason(exported):
    target, result = exported
    assert result["excluded_tests"] == ec.EXCLUDED_TESTS
    ini = (target / "engine" / "pytest.ini").read_text(encoding="utf-8")
    for rel, reason in ec.EXCLUDED_TESTS.items():
        assert rel.startswith(("engine/tests/", "scripts/tests/", "claude_mod/")) and len(reason) > 10, rel
        if "::" in rel:  # the file ships; that one test is deselected in the exported pytest.ini
            assert rel.startswith("engine/tests/") and (target / rel.split("::")[0]).is_file(), rel
            assert "--deselect=" + rel[len("engine/"):] in ini, rel
        else:
            assert not (target / rel).exists(), rel


@needs_source
def test_target_inside_repo_is_refused():
    with pytest.raises(ValueError):
        ec.export(REPO, REPO / "data" / "export-core-test")


@needs_source
def test_into_syncs_a_git_checkout_without_touching_git_or_data(tmp_path):
    dest = tmp_path / "product"
    dest.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(dest), check=True)
    head = (dest / ".git" / "HEAD").read_bytes()
    (dest / "stale.txt").write_text("old\n", encoding="utf-8")
    (dest / "engine").mkdir()
    (dest / "engine" / "old_module.py").write_text("x = 1\n", encoding="utf-8")
    (dest / "data").mkdir()
    (dest / "data" / "keep.sqlite").write_bytes(b"keep")
    (dest / "scripts").mkdir()
    (dest / "scripts" / "waypoint_cli.py").write_text("changed\n", encoding="utf-8")
    t0 = time.time()
    first = ec.sync_into(REPO, dest)
    assert first["findings"] == []
    assert "stale.txt" in first["removed"] and "engine/old_module.py" in first["removed"]
    assert "scripts/waypoint_cli.py" in first["replaced"]
    assert (dest / ".git" / "HEAD").read_bytes() == head
    assert (dest / "data" / "keep.sqlite").read_bytes() == b"keep"
    tree = {p.relative_to(dest).as_posix() for p in dest.rglob("*")
            if p.is_file() and p.relative_to(dest).parts[0] not in (".git", "data")}
    assert tree == set(first["files"])
    second = ec.sync_into(REPO, dest)
    assert second["removed"] == []
    # other sessions may edit source files meanwhile; only those may change on a second sync
    src = {t.as_posix(): s for s, t in ec.plan_core(REPO)}
    changed = [r for r in second["added"] + second["replaced"] if r not in src or src[r].stat().st_mtime < t0 - 5]
    assert changed == [], changed
    assert second["unchanged"] > len(second["files"]) // 2


@needs_source
def test_into_refuses_a_folder_without_git(tmp_path):
    with pytest.raises(ValueError):
        ec.sync_into(REPO, tmp_path)


# ---------------------------------------------------------------- review fixes: git file list, stricter scan, CI
def test_scan_reports_undecodable_unknown_and_env_files(tmp_path):
    home = "C:" + B + "Users" + B + "home" + B + "x"
    (tmp_path / "utf16.ps1").write_bytes(("$p = '" + home + "'\r\n").encode("utf-16"))  # a Windows editor's UTF-16
    (tmp_path / "cp1252.md").write_bytes(b"caf\xe9 " + home.encode("ascii") + b"\n")
    (tmp_path / "rows.jsonl").write_text('{"p": "' + home.replace(B, B + B) + '"}\n', encoding="utf-8")
    (tmp_path / ".env.local").write_text("X=1\n", encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(bytes(range(256)))
    (tmp_path / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(256)))
    (tmp_path / "ok.md").write_text("fine\n", encoding="utf-8")
    found = ec.scan(tmp_path)
    by_file = {}
    for f in found:
        by_file.setdefault(f["file"], set()).add(f["kind"])
    assert "unscanned" in by_file.get("utf16.ps1", set()), found
    assert "unscanned" in by_file.get("cp1252.md", set()), found
    assert {"personal", "unknown"} <= by_file.get("rows.jsonl", set()), found  # read and reported
    assert by_file.get(".env.local"), found
    assert "unknown" in by_file.get("blob.bin", set()), found
    assert "shot.png" not in by_file and "ok.md" not in by_file, found
    assert ec.main(["--scan", str(tmp_path)]) == 1  # --scan needs no git


def _fake_source(tmp_path):
    """A small source repo: export_core reads it with the real export_standalone rules."""
    repo = tmp_path / "src"
    for rel, text in (("engine/cc_engine/__init__.py", "x = 1\n"), ("engine/pytest.ini", "[pytest]\n"),
                      ("engine/tests/test_a.py", "def test_a():\n    pass\n"),
                      ("scripts/waypoint_cli.py", "print(1)\n"), ("claude_mod/README.md", "mod\n"),
                      ("core/README.md", "core\n")):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
    for rel in ("engine/cc_engine/stray.py", "claude_mod/.env.local", "core/notes.md"):  # never added to git
        (repo / rel).write_text("untracked\n", encoding="utf-8")
    return repo


@needs_source
def test_export_copies_only_git_tracked_files(tmp_path):
    repo = _fake_source(tmp_path)
    result = ec.export(repo, tmp_path / "out")
    files = set(result["files"])
    assert {"engine/cc_engine/__init__.py", "scripts/waypoint_cli.py", "claude_mod/README.md", "README.md"} <= files
    assert not files & {"engine/cc_engine/stray.py", "claude_mod/.env.local", "notes.md"}, files
    on_disk = {p.relative_to(tmp_path / "out").as_posix() for p in (tmp_path / "out").rglob("*") if p.is_file()}
    assert on_disk == files


@needs_source
def test_export_from_a_tree_without_git_is_refused(tmp_path):
    repo = tmp_path / "src"
    (repo / "engine" / "cc_engine").mkdir(parents=True)
    (repo / "engine" / "cc_engine" / "__init__.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="git"):
        ec.export(repo, tmp_path / "out")


def test_scripts_tests_conftest_ships_and_the_venv_test_is_deselected():
    assert "conftest.py" in ec.CORE_SCRIPT_TESTS and "test_install_scripts.py" in ec.CORE_SCRIPT_TESTS
    key = "engine/tests/test_ps_config_helper.py::test_python_uses_the_original_venv_only_when_it_exists"
    assert key in ec.EXCLUDED_TESTS and "drop" in ec.EXCLUDED_TESTS[key]


@needs_source
def test_conftest_is_in_the_export_once_tracked(exported):
    target, result = exported
    tracked = ec.tracked_files(REPO)
    for rel in ("scripts/tests/conftest.py", "scripts/tests/test_install_scripts.py"):
        if rel in tracked:  # new files ship once they are committed (or added to the git index)
            assert rel in result["files"], rel


def test_ci_runs_every_test_step_even_after_a_failure():
    ci = REPO / ("core/.github/workflows/ci.yml" if SOURCE else ".github/workflows/ci.yml")
    steps, cur = [], None
    for line in ci.read_text(encoding="utf-8").splitlines():
        if line.startswith("      - "):
            cur = [line]
            steps.append(cur)
        elif cur is not None and line.startswith("        "):
            cur.append(line)
    test_steps = [s for s in steps if any(l.strip().startswith("run:") and ("-m pytest" in l or "node --test" in l
                                                                             or "--scan" in l) for l in s)]
    assert len(test_steps) >= 7, test_steps
    for s in test_steps:
        assert any(l.strip() == "if: success() || failure()" for l in s), s[0]


@needs_source
def test_export_ships_the_waypoint_server_and_it_serves(exported, tmp_path):
    """Waypoint for dsh 2.0 starts Core's runner/waypoint_runner/wpc_http.py: the Core product must carry it and it must
    run from the exported tree (the data client sits at core/waypoint-data/py there, as in the source repo)."""
    import socket
    import subprocess
    import time
    import urllib.request
    target, _ = exported
    for need in ("runner/waypoint_runner/wpc_http.py", "runner/waypoint_runner/mcp_board.py",
                 "runner/waypoint_runner/ops_tools.py", "runner/waypoint_runner/__init__.py",
                 "core/waypoint-data/py/waypoint_data/store.py"):
        assert (target / need).is_file(), need
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    data = tmp_path / "data"
    shutil.copytree(target / "core" / "waypoint-data" / "fixtures" / "sample-folder", data)
    import os
    home = tmp_path / "home"  # a new user: no ~/.waypoint settings, no add-on folders
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("WAYPOINT_")}
    env.update(HOME=str(home), USERPROFILE=str(home))
    proc = subprocess.Popen([sys.executable, "-m", "waypoint_runner.wpc_http", "--folder", str(data), "--port", str(port)],
                            cwd=str(target / "runner"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    try:
        body = None
        for _ in range(50):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/wpc/features", timeout=1) as r:
                    body = json.loads(r.read())
                break
            except OSError:
                time.sleep(0.2)
        assert body is not None, proc.stdout.read1(4000) if proc.poll() is not None else "no answer"
        assert "board_read" in body["tools"] and body["addons"]["preferences"] is False
    finally:
        proc.kill()
        proc.wait()
