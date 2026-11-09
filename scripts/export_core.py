"""Build the Waypoint Core product tree, or scan any tree for personal strings and secrets.

    python scripts/export_core.py <target>             new folder outside this repo
    python scripts/export_core.py --into <repo folder>  sync an existing git checkout (its .git is never touched)
    python scripts/export_core.py --scan <dir>          scan any tree (the shipped product uses this mode)

Export copies an allowlist (see ``plan_core``): engine/cc_engine (the neutral learn seed ships as learn_seed.json),
engine/tests and engine/pytest.ini, the scripts the core needs and their tests, claude_mod/ (never its data/), and
core/* at the root (core/docs becomes docs/). Tests that need parts the export leaves out are dropped by the explicit
``EXCLUDED_TESTS`` list, each with its reason. Personal defaults are rewritten (export_standalone.REWRITES plus
``CORE_REWRITES``), then the exported tree is scanned. Exit 0 clean, 1 findings, 2 refused or bad usage.

In the source repo the scan reuses export_standalone's rules; the product has no export_standalone.py, so this file
carries a copy of those rules (rot13-encoded, so this file never matches itself). A test keeps both copies equal.
"""
from __future__ import annotations

import argparse
import codecs
import fnmatch
import importlib.util
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

CORE_SCRIPTS = (
    "waypoint_cli.py", "waypoint_cli_engine.py", "waypoint.cmd", "waypoint.ps1", "waypoint_config.ps1", "Start-Engine.ps1", "Stop-Engine.ps1",
    "Register-Engine.ps1", "Register-Tick.ps1", "Register-DbBackup.ps1", "backup_db.py", "waypoint_spend_guard.py",
    "export_core.py",
)
CORE_SCRIPT_TESTS = (
    "conftest.py", "test_waypoint_cli.py", "test_spend_guard.py", "test_db_backup.py", "test_export_core.py",
    "test_core_install.py", "test_install_scripts.py", "test_cli_engine_e2e.py", "test_cli_receipts.py",
)

# Tests that need parts the export leaves out (plans/, plugin/, standalone/, the original install's paths, scripts
# that do not ship). Exported path -> reason. A file entry drops the file; a "file::test" entry (engine tests only)
# keeps the file and deselects that test in the exported engine/pytest.ini. Owners may fix a test and remove it here.
EXCLUDED_TESTS = {
    "engine/tests/test_api.py::test_api_plan_import_route_requires_revision":
        "imports plans/build_plan.json (plans/ does not ship)",
    "engine/tests/test_app_shell.py::test_app_shell_routes": "serves plugin/assets (plugin/ does not ship)",
    "engine/tests/test_learn.py::test_routes_are_declared_and_work":
        "preps from the owner's personal learn seed (the export ships the neutral example seed)",
    "engine/tests/test_learn.py::test_real_seed_shape": "checks the owner's personal learn seed (not shipped)",
    "engine/tests/test_learn.py::test_real_seed_cites_the_canonical_evidence_matrix":
        "checks the owner's personal learn seed (not shipped)",
    "engine/tests/test_learn.py::test_real_seed_prep_for_acme_risk":
        "checks the owner's personal learn seed (not shipped); name as exported",
    "engine/tests/test_ps_config_helper.py::test_launcher_default_is_the_original_install":
        "asserts the original install's default paths (the export rewrites them)",
    "engine/tests/test_ps_config_helper.py::test_python_uses_the_original_venv_only_when_it_exists":
        "expects the original install's venv line, which the export drops on purpose (# export: drop line)",
    "engine/tests/test_plan.py": "imports plans/build_plan.json (plans/ does not ship)",
    "engine/tests/test_loop_crosses.py": "reads plan files under plans/ (plans/ does not ship)",
    "engine/tests/test_projects_import.py": "loads scripts/import_projects.py and plans/projects (neither ships)",
    "engine/tests/test_state_snapshot.py": "checks the committed plans/state snapshots (plans/ does not ship)",
    "engine/tests/test_gemini_live.py": "reads plugin/src/page_learn.js (plugin/ does not ship)",
    "engine/tests/test_ui_contract.py": "reads plugin/src/page_create.js (plugin/ does not ship)",
    "engine/tests/test_no_chatmode.py": "scans plugin/src and plugin/lib (plugin/ does not ship)",
    "engine/tests/test_versions_review.py": "scans plugin/src and plugin/lib (plugin/ does not ship)",
    "engine/tests/test_standalone_clean_env.py": "exports with export_standalone.py and reads standalone/ (neither ships)",
    "engine/tests/test_standalone_settings.py": "reads standalone/addons.json (standalone/ does not ship)",
    "engine/tests/test_review_standalone_config.py": "reviews the standalone product's config (standalone/ does not ship)",
    "engine/tests/test_config_paths.py": "asserts the original install's default paths and lists scripts that do not ship",
    "engine/tests/test_config_standalone_keys.py": "asserts the original install's default paths (the export rewrites them)",
    "engine/tests/test_env_policy.py": "asserts the original install's instruction-file path (the export rewrites it)",
    "scripts/tests/test_cli_engine_e2e.py": "drives the retired 1.1 engine CLI against a live engine (fails in the source too)",
}

# Scripts tests that fail in the source repo too (they drive the retired 1.1 engine CLI or expect its output), left out
# of the product by a generated scripts/pytest.ini: node id relative to scripts/ -> reason. A whole file is listed in
# EXCLUDED_TESTS instead. Owners may fix a test and remove it here.
SCRIPT_EXCLUDED_TESTS = {
    "tests/test_cli_receipts.py::test_receipt_list": "expects the 1.1 engine CLI (engine retired in 1.2)",
    "tests/test_cli_receipts.py::test_tree_plain_text": "expects the 1.1 engine CLI (engine retired in 1.2)",
    "tests/test_cli_receipts.py::test_claim_and_release": "expects the 1.1 engine CLI (engine retired in 1.2)",
    "tests/test_core_install.py::test_engine_answers_api_1_on_its_port": "starts the retired 1.1 engine (engine retired in 1.2)",
    "tests/test_core_install.py::test_stop_engine_leaves_other_engines_alone": "needs the retired 1.1 engine",
    "tests/test_core_install.py::test_uninstall_reverses_install_and_stops_its_engine": "needs the retired 1.1 engine",
    "tests/test_install_scripts.py::test_real_scripts_command_runs_the_cli_from_a_non_ascii_clone":
        "expects the 1.1 version line and a clone without the data library",
}

# Extra rewrites for the core export, applied after export_standalone.REWRITES: (path glob or None, regex, text).
# Employer names in examples become a neutral company (lower case stays lower case); job-document words become
# neutral ones. Patterns are rot13-encoded so this file never matches its own scan.
CORE_REWRITES = [
    (None, "(?i)" + codecs.decode(r"(?:OynpxEbpx|Unegerr)", "rot13"), "Acme"),
    (None, codecs.decode(r"\oPIf\o", "rot13"), "Documents"),
    (None, codecs.decode(r"\oPI\o", "rot13"), "report"),
    ("*/cc_engine/config.py", codecs.decode(r'"cebwrpgf", "P--Hfref-ubzr-Zl-Qevir", "zrzbel"', "rot13"), '"memory"'),  # Claude memory folder
    # the brain graph's last topic cluster names the owner's job-search topics: a neutral delivery cluster ships instead
    ("*/cc_engine/brain_index.py",
     r'"Projects & career": \{\n\s+"career", "portfolio", "v9", "tables", "interview-prep", "verbal-feedback",\n'
     r'\s+"story-extraction", "anki", "phrasing", "learning", "release", "publishing",\n',
     '"Projects & delivery": {\n        "portfolio", "tables", "learning", "release", "publishing",\n'),
    ("*.md", r"\]\(\.\./waypoint-data/", "](../core/waypoint-data/"),  # docs/ links to the data library
    ("CHANGELOG.md", r"in `waypoint-data/`", "in `core/waypoint-data/`"),
    (None, codecs.decode(r"\oBjare-Fghql\o", "rot13"), "your-account"),  # the owner rewrite hit a GitHub account outside PUBLIC_URLS
]

# Applied BEFORE export_standalone's owner-name rewrites: wording that would read badly once the owner's name is
# replaced, and the one owner-machine path in the dashboard page. (path glob, regex, text). Patterns that hold the
# owner's name are rot13-encoded so this file scans clean.
PRE_REWRITES = [
    ("artifacts/waypoint-claude.html",
     r'\(rel \? "&folder=" \+ encodeURIComponent\("C:\\\\Users\\\\home\\\\" \+ rel\.replace\(/\\//g, "\\\\"\)\) : ""\)', '""'),
    ("artifacts/waypoint-claude.html", codecs.decode(r"Lbh ner Eboreg'f ghgbe vafvqr uvf Jnlcbvag Pynhqr cntr", "rot13"),
     "You are the learner's tutor inside their Waypoint Claude page"),
    ("artifacts/waypoint-claude.html", codecs.decode(r"Erpbeq bar pynvz Eboreg abj ubyqf", "rot13"), "Record one claim the learner now holds"),
    ("artifacts/waypoint-claude.html", codecs.decode(r"Xabjyrqtr: Eboreg'f cersreraprf", "rot13"), "Knowledge: your preferences"),
    ("artifacts/waypoint-claude.html", codecs.decode(r"Gur gjb Eboreg gntf", "rot13"), "The two tags"),
    ("artifacts/waypoint-claude.html", codecs.decode(r"erny rssbeg sebz Eboreg \(", "rot13"), "real effort from you ("),
    ("artifacts/waypoint-claude.html", codecs.decode(r" \(Eboreg, 8 Abi 2026\)", "rot13"), ""),
    ("artifacts/waypoint-claude.html", r"\bwhere he is\b", "where they are"),
    ("artifacts/waypoint-claude.html", r"\bwhen he asks\b", "when they ask"),
    ("artifacts/waypoint-claude.html", r"\bclaim he now holds\b", "claim they now hold"),
    ("artifacts/waypoint-claude.html", r"\bclaim he does not hold\b", "claim they do not hold"),
    ("artifacts/waypoint-claude.html", r"\bhis\b", "their"),
    ("artifacts/waypoint-claude.html", r"\bhim\b", "them"),
]

# Engine-only export (``include_mod=False``): the public engine trees carry no claude_mod/, so the shared documents
# lose their mod passages. (path, regex, replacement), applied with re.S after the rewrites. A pattern that no longer
# matches is an error (the document changed: update the edit), so a mod passage can never slip into a public tree.
ENGINE_ONLY_DROP = {"RELEASE_CHECKLIST.md"}  # the 1.0 release checklist is mod-centred
ENGINE_ONLY_DROP_PREFIX = ("docs/screenshots/",)  # screenshots of the retired mod; each public tree adds its own
ENGINE_ONLY_EDITS = [
    (".github/workflows/ci.yml", r"      - name: Mod tests\n.*?run: node --test claude_mod/tests\n\n", ""),
    ("CONTRIBUTING.md", r"The mod \(needs Node 24 and Claude Code\):\n\n```powershell\n.*?```\n\n", ""),
    ("INSTALL.md", r"\| Claude Code \| optional \|[^\n]*\n\| Node \| 24, optional \|[^\n]*\n", ""),
    ("INSTALL.md", r"\| `-NoMod` \|[^\n]*\n", ""),
    # the engine is shared by two repositories, so its install guide cannot name either clone address
    ("INSTALL.md", r"git clone https://github\.com/[^\n]*\ncd [^\n]*\n", "cd <your clone of this repository>\n"),
    ("INSTALL.md", r"5\. \*\*Claude Code mod\.\*\* Only when.*?--scope user`\.\n",
     "5. **Mod.** Not part of this tree; the installer skips this step.\n"),
    ("INSTALL.md", r"\*\*Mod not showing\.\*\*.*?(?=\*\*PATH not refreshed)", ""),
    ("INSTALL.md", r", the Claude Code mod and the\nautostart task", " and the\nautostart task"),
    ("docs/ARCHITECTURE.md", r"\| Claude Code mod  \|", "| dashboards       |"),
    ("docs/ARCHITECTURE.md", r"\| claude_mod/      \|", "| (see README)     |"),
    ("docs/ARCHITECTURE.md", r", vendored into the mod as `claude_mod/waypoint/vendor/waypoint-data/`\)", ")"),
    ("docs/ARCHITECTURE.md", r"## Claude Code mod\n.*?(?=\n## )", ""),
    ("CHANGELOG.md", r" Each\n  capability, used or not and why, is in `claude_mod/CAPABILITIES\.md`\.", ""),
]

# The only personal text allowed in the product: exact lines, by exported path. Rewrites leave these lines alone and
# the scan ignores them (rot13-encoded so this file stays clean).
SCAN_ALLOW = {
    ("LICENSE", codecs.decode("Pbclevtug (p) 2026 Eboreg Tneqare", "rot13")),
}

# The public repos' own addresses: the owner name inside them is the GitHub account, not personal text. Rewrites
# leave them alone (else the owner rewrite turns them into dead links) and the scan ignores them (rot13 as above).
PUBLIC_URLS = tuple(codecs.decode(u, "rot13") for u in (
    "tvguho.pbz/Eboreg-Fghql/jnlcbvag-ntragf-pynhqr",
    "tvguho.pbz/Eboreg-Fghql/jnlcbvag-ntragf",
))
_URL_HOLD = "\x00PUBLIC_URL_{}\x00"

SKIP_PARTS = {"__pycache__", "node_modules", ".git", ".pytest_cache", ".venv", "data", "proposals"}
SKIP_SUFFIX = (".pyc", ".pyo", ".sqlite", ".sqlite3", ".sqlite-wal", ".sqlite-shm", ".db", ".log", ".tmp", ".token",
               ".pid", ".bak")
SKIP_NAMES = {".env", "learn_seed.json", "engine.token", "owner.token", "engine.pid", "engine.stop"}
TEXT_SUFFIX = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".json", ".ndjson", ".md", ".ps1", ".psm1", ".psd1", ".cmd",
               ".bat", ".sh", ".yml", ".yaml", ".txt", ".html", ".css", ".toml", ".ini", ".cfg", ".svg", ".xml", ".sha256"}
TEXT_NAMES = {"LICENSE", ".gitignore", ".gitattributes", ".editorconfig", "ENGINE_VERSION", "ORDER"}
# Binary files the product may ship (screenshots, icons, fonts). Any other file that is neither a known text type
# nor one of these is a scan finding ("unknown file type"), so nothing ships unreviewed.
KNOWN_BINARY = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".bmp", ".woff", ".woff2", ".ttf", ".otf", ".pdf"}
ENV_EXAMPLES = (".example", ".sample", ".template")
SCAN_SKIP_PARTS = {".git", "node_modules", "__pycache__", ".venv", ".pytest_cache", "data"}
# Local folders --into never removes (git-ignored state of a working clone).
KEEP_ON_SYNC = {".git", "data", ".venv", "node_modules", "__pycache__", ".pytest_cache"}

# ---------------------------------------------------------------- scan rules
# Bundled copy of export_standalone.PERSONAL / SECRETS (rot13-encoded names and patterns), used when that module is
# not importable (the shipped product). (name, pattern, ignore case).
_BS = chr(92)
_PERSONAL_ROT13 = (
    ("ubzr cngu", "P:(?:" + _BS * 4 + "|" + _BS * 2 + "|/)Hfref(?:" + _BS * 4 + "|" + _BS * 2 + "|/)ubzr", True),
    ("Zl Qevir", "Zl Qevir", False),
    ("NV-Rkpunatr", "NV-Rkpunatr", False),
    ("NV-Qvfx-Npprff", "NV-Qvfx-Npprff|nv-qvfx-npprff-jbexfcnpr", True),
    ("snzn-serapu ercb", "snzn-serapu", True),
    ("nppbhag/anzr", _BS.join(("eboregthltneqare|Eboreg-Fghql|", "oeboreg", "o|", "otneqare", "o")), True),
    ("fpbcr", "@ebo/", False),
)
_SECRETS_PLAIN = (
    ("google api key", r"AIza[0-9A-Za-z_\-]{35}", False),
    ("sk- key", r"\bsk-[A-Za-z0-9_\-]{20,}", False),
    ("groq key", r"\bgsk_[A-Za-z0-9]{20,}", False),
    ("github token", r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}", False),
    ("slack token", r"\bxox[abprs]-[A-Za-z0-9\-]{10,}", False),
    ("aws key id", r"\bAKIA[0-9A-Z]{16}\b", False),
    ("private key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----", False),
    ("jwt", r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}", False),
    ("stripe key", r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,}", False),
    ("hugging face token", r"\bhf_[A-Za-z0-9]{30,}", False),
    ("npm token", r"\bnpm_[A-Za-z0-9]{30,}", False),
    ("google oauth token", r"\bya29\.[A-Za-z0-9_\-]{20,}", False),
    ("assigned secret",
     r"""(?i)\b(?:api[_-]?key|secret|token|password)\b\s*[:=]\s*['"][A-Za-z0-9_\-/+]{24,}['"]""", False),
)
# Core-only additions: employer names, the owner's mail addresses, job-document words.
_EXTRA_ROT13 = (
    ("rzcyblre", "Unegerr|OynpxEbpx", True),
    ("rznvy", "eboregthltneqare|eboregtneqare|znvy@eboregtneqare", True),
    ("zatyrq ubzr cngu", "P--Hfref", False),
    ("wbo/PI", _BS + "oPIf?" + _BS + "o", False),
)


def _compile(rows, rot13):
    out = []
    for name, pattern, icase in rows:
        if rot13:
            name, pattern = codecs.decode(name, "rot13"), codecs.decode(pattern, "rot13")
        out.append((name, re.compile(pattern, re.I if icase else 0)))
    return out


BUNDLED_PERSONAL = _compile(_PERSONAL_ROT13, True)
BUNDLED_SECRETS = _compile(_SECRETS_PLAIN, False)
EXTRA_PERSONAL = _compile(_EXTRA_ROT13, True)


def load_standalone():
    """export_standalone as a module, or None when it is not beside this file (the shipped product)."""
    path = Path(__file__).resolve().with_name("export_standalone.py")
    if not path.is_file():
        return None
    if "export_standalone" in sys.modules:
        return sys.modules["export_standalone"]
    spec = importlib.util.spec_from_file_location("export_standalone", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["export_standalone"] = mod
    spec.loader.exec_module(mod)
    return mod


def rules():
    """``(personal, secrets)``: export_standalone's rules when importable, else the bundled copy; plus the extras."""
    es = load_standalone()
    personal = list(es.PERSONAL) if es else list(BUNDLED_PERSONAL)
    secrets = list(es.SECRETS) if es else list(BUNDLED_SECRETS)
    return personal + EXTRA_PERSONAL, secrets


def _mask(text: str) -> str:
    return text[:4] + "***" if len(text) > 8 else "***"


def _is_text(p: Path) -> bool:
    return p.suffix.lower() in TEXT_SUFFIX or p.name in TEXT_NAMES


def _is_env_file(name: str) -> bool:
    """``.env``, ``.env.local``, ``prod.env`` ... (never ``.env.example``): local settings that must not ship."""
    low = name.lower()
    if low.endswith(ENV_EXAMPLES):
        return False
    return low == ".env" or low.startswith(".env.") or low.endswith(".env")


def scan(root) -> list:
    """Personal strings and secret patterns under ``root`` (skipping .git, data/, caches):
    ``[{file, line, kind, rule, match}]``; secret matches are masked.

    Nothing is skipped silently. A known text file that is not UTF-8 (UTF-16 or cp1252 from a Windows editor) is a
    finding of kind ``unscanned``. A file of unknown type (not a known text suffix or name, not a known binary such
    as .png) is a finding of kind ``unknown``, and is still read when it decodes. An env file is a finding."""
    root = Path(root)
    personal, secrets = rules()
    found = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if not p.is_file() or set(rel.parts[:-1]) & SCAN_SKIP_PARTS:
            continue
        name = rel.as_posix()
        if p.suffix.lower() in KNOWN_BINARY:
            continue
        if _is_env_file(p.name):
            found.append({"file": name, "line": 0, "kind": "env", "rule": "env file", "match": p.name})
            continue
        known = _is_text(p)
        if not known:
            found.append({"file": name, "line": 0, "kind": "unknown", "rule": "unknown file type",
                          "match": p.suffix or p.name})
        try:
            text = p.read_bytes().decode("utf-8")
            if "\x00" in text:
                raise UnicodeDecodeError("utf-8", b"\x00", 0, 1, "NUL byte")
        except (UnicodeDecodeError, OSError) as exc:
            if known:
                found.append({"file": name, "line": 0, "kind": "unscanned", "rule": "not utf-8 text",
                              "match": type(exc).__name__})
            continue
        text = text.lstrip("\ufeff")
        for no, line in enumerate(text.splitlines(), 1):
            if (name, line.rstrip()) in SCAN_ALLOW:
                continue
            for url in PUBLIC_URLS:
                line = line.replace(url, "github.com/public-repo")
            for kind, rows in (("personal", personal), ("secret", secrets)):
                for rule, rx in rows:
                    m = rx.search(line)
                    if m:
                        found.append({"file": name, "line": no, "kind": kind, "rule": rule,
                                      "match": _mask(m.group(0)) if kind == "secret" else m.group(0)})
    return found


# ---------------------------------------------------------------- export
def _is_inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _walk(src_dir: Path, dest_prefix: str, out: list):
    if not src_dir.is_dir():
        return
    for p in sorted(src_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(src_dir)
        if set(rel.parts) & SKIP_PARTS or rel.name in SKIP_NAMES or rel.suffix.lower() in SKIP_SUFFIX:
            continue
        out.append((p, Path(dest_prefix) / rel if dest_prefix else rel))


def tracked_files(repo) -> set | None:
    """The files git tracks in ``repo`` (``git ls-files``: committed or added to the index), as repo-relative
    POSIX paths; None when ``repo`` is not a git checkout or git is missing."""
    try:
        r = subprocess.run(["git", "-C", str(repo), "ls-files", "-z"], capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return {x for x in r.stdout.decode("utf-8", "surrogateescape").split("\0") if x}


def plan_core(repo, include_mod: bool = True) -> list:
    """``[(source, relative target)]`` for the core product: the allowlist below, limited to files git tracks in
    the source repo, so untracked or ignored files (local settings, scratch notes) never ship."""
    repo = Path(repo).resolve()
    es = load_standalone()
    if es is None:
        raise ValueError("export needs the source repo (scripts/export_standalone.py); only --scan works here")
    tracked = tracked_files(repo)
    if tracked is None:
        raise ValueError(f"export needs a git checkout of the source repo (git ls-files failed in {repo}); "
                         "only --scan works on a tree without git")
    out = [(s, t) for s, t in es._plan(repo, False, False) if t.parts[:2] == ("engine", "cc_engine")]
    if (repo / "engine" / "pytest.ini").is_file():
        out.append((repo / "engine" / "pytest.ini", Path("engine/pytest.ini")))
    _walk(repo / "engine" / "tests", "engine/tests", out)
    for name in CORE_SCRIPTS:
        if (repo / "scripts" / name).is_file():
            out.append((repo / "scripts" / name, Path("scripts") / name))
    for name in CORE_SCRIPT_TESTS:
        if (repo / "scripts" / "tests" / name).is_file():
            out.append((repo / "scripts" / "tests" / name, Path("scripts/tests") / name))
    if include_mod:
        _walk(repo / "claude_mod", "claude_mod", out)
    # The Waypoint server and board tools (Waypoint for dsh 2.0 starts runner/waypoint_runner/wpc_http.py).
    _walk(repo / "runner" / "waypoint_runner", "runner/waypoint_runner", out)
    core_files = []
    _walk(repo / "core", "", core_files)  # core/README.md -> README.md, core/docs/x -> docs/x
    # core/waypoint-data stays where it is: the CLI and the runner load the data client from core/waypoint-data/py.
    out += [(s, Path("core") / t if t.parts[0] == "waypoint-data" else t) for s, t in core_files]
    if not include_mod:
        out = [(s, t) for s, t in out if t.as_posix() not in ENGINE_ONLY_DROP
               and not t.as_posix().startswith(ENGINE_ONLY_DROP_PREFIX)]
    out = [(s, t) for s, t in out if t.as_posix() not in EXCLUDED_TESTS]  # file entries only
    out = [(s, t) for s, t in out if s.is_file() and _rel_posix(s, repo) in tracked]
    seen, uniq = set(), []
    for s, t in out:  # a core/ file wins over nothing; first entry per target wins
        if t.as_posix() not in seen:
            seen.add(t.as_posix())
            uniq.append((s, t))
    return uniq


def _rel_posix(path: Path, repo: Path) -> str:
    try:
        return Path(path).resolve().relative_to(repo).as_posix()
    except ValueError:
        return ""


def rewrite_text(rel: str, text: str) -> str:
    allowed = {line for path, line in SCAN_ALLOW if path == rel.replace("\\", "/")}
    if allowed:  # rewrite line by line, keeping the allowed lines exactly as written
        return "".join(ln if ln.rstrip() in allowed else _rewrite(rel, ln) for ln in text.splitlines(True))
    return _rewrite(rel, text)


def _rewrite(rel: str, text: str) -> str:
    es = load_standalone()
    for i, url in enumerate(PUBLIC_URLS):  # longest first, so the -claude address is held whole
        text = text.replace(url, _URL_HOLD.format(i))
    path = "/" + rel.replace("\\", "/")
    for glob, pattern, repl in PRE_REWRITES:
        if fnmatch.fnmatch(path, "*" + glob.lstrip("*")):
            text = re.sub(pattern, lambda _m, r=repl: r, text, flags=re.M)
    text = es.rewrite_text(rel, text)
    for glob, pattern, repl in CORE_REWRITES:
        if glob is None or fnmatch.fnmatch(path, "*" + glob.lstrip("*")):
            text = re.sub(pattern, lambda m, r=repl: r.lower() if m.group(0).islower() else r, text, flags=re.M)
    for i, url in enumerate(PUBLIC_URLS):
        text = text.replace(_URL_HOLD.format(i), url)
    return text


def _scripts_pytest_ini() -> str:
    lines = ["[pytest]", "addopts = -p no:cacheprovider"]
    lines += ["    --deselect=" + n for n in sorted(SCRIPT_EXCLUDED_TESTS)]
    return "\n".join(lines) + "\n"


def engine_only_text(rel: str, text: str) -> str:
    """``text`` of exported file ``rel`` without its claude_mod passages (ENGINE_ONLY_EDITS)."""
    for path, pattern, repl in ENGINE_ONLY_EDITS:
        if path == rel:
            text, n = re.subn(pattern, lambda _m, r=repl: r, text, count=1, flags=re.S)
            if not n:
                raise ValueError(f"engine-only edit no longer matches {rel}: {pattern[:50]!r}")
    return text


def _deselected(text: str) -> str:
    """engine/pytest.ini with a --deselect line per "engine/tests/x.py::test" entry of EXCLUDED_TESTS."""
    nodes = sorted(k[len("engine/"):] for k in EXCLUDED_TESTS if "::" in k and k.startswith("engine/"))
    if not nodes:
        return text
    extra = "".join("\n    --deselect=" + n for n in nodes)
    if re.search(r"^addopts\s*=.*$", text, flags=re.M):
        return re.sub(r"^(addopts\s*=.*)$", lambda m: m.group(1) + extra, text, count=1, flags=re.M)
    return text.rstrip("\n") + "\naddopts =" + extra + "\n"


def _write_tree(plan, target: Path, include_mod: bool = True):
    if any(rel.parts[:2] == ("scripts", "tests") for _, rel in plan) and SCRIPT_EXCLUDED_TESTS:
        (target / "scripts").mkdir(parents=True, exist_ok=True)
        (target / "scripts" / "pytest.ini").write_text(_scripts_pytest_ini(), encoding="utf-8", newline="")
    for src, rel in plan:
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if _is_text(src):
            try:
                text = src.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                shutil.copy2(src, dest)
                continue
            text = rewrite_text(rel.as_posix(), text)
            if not include_mod:
                text = engine_only_text(rel.as_posix(), text)
            if rel.as_posix() == "engine/pytest.ini":
                text = _deselected(text)
            dest.write_text(text, encoding="utf-8", newline="")
        else:
            shutil.copy2(src, dest)


def _check_target(repo: Path, target: Path):
    if _is_inside(target, repo) or _is_inside(repo, target):
        raise ValueError("the target folder must be outside the repository (and not contain it)")


def export(repo, target, include_mod: bool = True) -> dict:
    """Copy the core product into a new (missing or empty) folder outside the repo, then scan it.
    ``include_mod=False`` is the engine-only tree (no claude_mod/) that scripts/build_public.py builds on."""
    repo, target = Path(repo).resolve(), Path(target).resolve()
    _check_target(repo, target)
    if target.exists() and any(target.iterdir()):
        raise ValueError(f"target is not empty: {target}")
    plan = plan_core(repo, include_mod)
    _write_tree(plan, target, include_mod)
    return {"files": [rel.as_posix() for _, rel in plan], "findings": scan(target),
            "excluded_tests": dict(EXCLUDED_TESTS)}


def _same(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_size == b.stat().st_size and a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def sync_into(repo, target, include_mod: bool = True) -> dict:
    """Make ``target`` (an existing git checkout) equal the export: add, replace and remove files. Never touches
    .git, and never removes git-ignored local folders (data/, .venv/, node_modules/, caches)."""
    repo, target = Path(repo).resolve(), Path(target).resolve()
    _check_target(repo, target)
    if not target.is_dir() or not (target / ".git").exists():
        raise ValueError(f"--into needs an existing git checkout (no .git in {target})")
    plan = plan_core(repo, include_mod)
    added, replaced, removed, same = [], [], [], 0
    with tempfile.TemporaryDirectory(prefix="waypoint-core-stage-") as tmp:
        stage = Path(tmp)
        _write_tree(plan, stage, include_mod)
        wanted = {p.relative_to(stage).as_posix() for p in stage.rglob("*") if p.is_file()}
        for rel in sorted(wanted):
            src, dest = stage / rel, target / rel
            if dest.is_file():
                if _same(src, dest):
                    same += 1
                    continue
                replaced.append(rel)
            else:
                if dest.is_dir():
                    shutil.rmtree(dest)
                added.append(rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
    for p in sorted(target.rglob("*"), reverse=True):
        rel = p.relative_to(target)
        if rel.parts[0] in KEEP_ON_SYNC or set(rel.parts) & {".git"}:
            continue
        if p.is_file() and rel.as_posix() not in wanted:
            p.unlink()
            removed.append(rel.as_posix())
        elif p.is_dir() and not any(p.iterdir()):
            p.rmdir()
    return {"files": sorted(wanted), "added": added, "replaced": replaced, "removed": sorted(removed),
            "unchanged": same, "findings": scan(target), "excluded_tests": dict(EXCLUDED_TESTS)}


def _print_findings(findings) -> int:
    if findings:
        print(f"scan: FAILED, {len(findings)} finding(s):")
        for f in findings:
            print(f"  {f['kind']:8} {f['rule']:18} {f['file']}:{f['line']}  {f['match']}")
        return 1
    print("scan: clean (no personal strings or secret patterns found)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("target", nargs="?", help="new folder outside the repo (missing or empty)")
    ap.add_argument("--into", metavar="DIR", help="existing git checkout to sync")
    ap.add_argument("--scan", metavar="DIR", help="only scan this tree")
    ap.add_argument("--engine-only", action="store_true", help="export without claude_mod/ (the public engine)")
    args = ap.parse_args(argv)
    if sum(bool(x) for x in (args.target, args.into, args.scan)) != 1:
        ap.print_usage()
        print("export_core: give exactly one of <target>, --into DIR, --scan DIR")
        return 2
    if args.scan:
        root = Path(args.scan)
        if not root.is_dir():
            print(f"scan: refused: not a folder: {root}")
            return 2
        return _print_findings(scan(root))
    try:
        if args.into:
            r = sync_into(REPO, args.into, include_mod=not args.engine_only)
            print(f"export: synced {len(r['files'])} files into {args.into} (added {len(r['added'])}, "
                  f"replaced {len(r['replaced'])}, removed {len(r['removed'])}, unchanged {r['unchanged']})")
        else:
            r = export(REPO, args.target, include_mod=not args.engine_only)
            print(f"export: {len(r['files'])} files copied to {args.target}")
    except ValueError as exc:
        print(f"export: refused: {exc}")
        return 2
    print(f"export: {len(EXCLUDED_TESTS)} tests left out (see EXCLUDED_TESTS)")
    return _print_findings(r["findings"])


if __name__ == "__main__":
    sys.exit(main())
