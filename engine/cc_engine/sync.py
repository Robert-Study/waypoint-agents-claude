"""Cloud / Local sync (S2): keep Owner's local repos level with GitHub without ever losing work.

Owner (4 Nov 2026): one simple Cloud | Local control; after working in Cloud mode, opening the app
shows a clear button to update the local files. Drive syncs by itself, so only git repos matter.

Repos: every git repo directly under the workspace root (cc_engine.config.WORKSPACE_ROOT) (``CC_SYNC_ROOT`` changes the
root). A configured list overrides discovery: ``set_repos(store, [paths])`` (meta ``sync:repos``)
or ``CC_SYNC_REPOS`` (paths joined by ``os.pathsep``).

What happens
- On open (``on_open``, ``POST /api/sync/open``; throttled to once per ``OPEN_INTERVAL``): fetch,
  then fast-forward every repo whose tracked files are clean and which is strictly behind its
  upstream. Nothing else changes by itself.
- ``pull`` (``POST /api/sync/pull``, owner only, the 'Update local files' button): fast-forward
  when behind; when histories diverged, ``git merge --no-edit @{u}``. If that merge conflicts it is
  aborted (``git merge --abort``) and Owner gets one needs-you question naming the files.
- A dirty working tree (tracked edits), a merge already in progress, a detached HEAD or a branch
  with no upstream is reported and skipped. Never force, reset, stash, clean, checkout or push.
- The mode (``Cloud`` | ``Local``) is stored in meta ``sync:mode`` and never bumps the plan revision.

Routes (extension module; api.py loads ``ROUTES``):
    GET  /api/sync/status[?fetch=1]  {mode, repos[], needs_merge, last}
    POST /api/sync/mode   {mode}     owner only
    POST /api/sync/open   {force?}   owner or router: fetch + fast-forward clean repos
    POST /api/sync/pull   {repos?}   owner only: also merge diverged repos

The git runner is injectable: ``runner(args, cwd, timeout=None) -> (returncode, stdout, stderr)``
(``app.sync_git`` or module ``GIT_RUNNER`` for the routes; ``git=`` for the functions).
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from . import config

MODES = ("Cloud", "Local")
DEFAULT_MODE = "Local"
DEFAULT_ROOT = config.WORKSPACE_ROOT
MODE_KEY = "sync:mode"
REPOS_KEY = "sync:repos"
LAST_KEY = "sync:last"
OPENED_KEY = "sync:opened_at"
CONFLICT_KEY = "sync:conflict:"  # + repo path -> {files, question, at}
OPEN_INTERVAL = 600  # seconds between automatic fetch rounds
FETCH_TIMEOUT = 90
GIT_TIMEOUT = 30
ACTOR = "sync"
BRAND = "Waypoint"  # product name in event text (plugin CC.BRAND)
GIT_RUNNER = None  # tests/lead may set a module-wide runner

_LOCK = threading.RLock()


# ---------------------------------------------------------------- git runner
def default_git(args, cwd, timeout=None):
    """Run ``git <args>`` in ``cwd``; never prompts, never raises. Returns (code, out, err)."""
    env = dict(os.environ)
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_MERGE_AUTOEDIT": "no", "GCM_INTERACTIVE": "never",
                "LC_ALL": "C"})
    try:
        out = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                             encoding="utf-8", errors="replace", env=env, timeout=timeout or GIT_TIMEOUT,
                             stdin=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return out.returncode, (out.stdout or "").strip(), (out.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return 124, "", "git timed out"
    except (OSError, ValueError) as e:
        return 127, "", str(e)


def _runner(git):
    return git or GIT_RUNNER or default_git


def _run(git, args, cwd, timeout=None):
    try:
        code, out, err = git(list(args), cwd, timeout=timeout)
    except Exception as e:  # an injected runner must never break the engine
        return 1, "", str(e)
    return int(code), (out or "").strip(), (err or "").strip()


def _first_line(text):
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:200]
    return ""


# ---------------------------------------------------------------- settings
def _now():
    return datetime.now(timezone.utc)


def _iso(dt=None):
    return (dt or _now()).astimezone(timezone.utc).isoformat(timespec="seconds")


def get_mode(store) -> str:
    value = store.get_meta(MODE_KEY)
    return value if value in MODES else DEFAULT_MODE


def set_mode(store, mode, actor="owner") -> str:
    match = [m for m in MODES if isinstance(mode, str) and m.lower() == mode.strip().lower()]
    if not match:
        raise ValueError(f"mode must be one of {list(MODES)}")
    new = match[0]
    old = get_mode(store)
    store.set_meta(MODE_KEY, new)
    if new != old:
        who = "Owner" if actor == "owner" else actor
        store.event(None, None, "sync", actor, f"{who} switched {BRAND} to {new} mode.", {"mode": new})
        store.notify("run", {"sync": "mode", "mode": new})
    _forget_cache()
    return new


def set_repos(store, paths):
    """Configure the repo list (``None`` or empty goes back to discovery)."""
    if not paths:
        store.set_meta(REPOS_KEY, "")
    else:
        store.set_meta(REPOS_KEY, json.dumps([str(p) for p in paths]))
    _forget_cache()


def discover(root) -> list:
    """Every git repo (``.git`` folder or worktree file) directly under ``root``, sorted by name."""
    root = Path(root)
    try:
        kids = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name.lower())
    except OSError:
        return []
    return [str(p) for p in kids if (p / ".git").exists()]


def repo_paths(store, root=None) -> list:
    raw = store.get_meta(REPOS_KEY)
    if raw:
        try:
            listed = json.loads(raw)
            if isinstance(listed, list) and listed:
                return [str(p) for p in listed]
        except ValueError:
            pass
    env = os.environ.get("CC_SYNC_REPOS")
    if env:
        return [p for p in env.split(os.pathsep) if p.strip()]
    return discover(root or os.environ.get("CC_SYNC_ROOT") or DEFAULT_ROOT)


def _load(store, key, default):
    raw = store.get_meta(key)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


# ---------------------------------------------------------------- one repo
def _merge_head(path, git):
    dot = Path(path) / ".git"
    if dot.is_dir():
        return (dot / "MERGE_HEAD").exists()
    return _run(git, ["rev-parse", "-q", "--verify", "MERGE_HEAD"], path)[0] == 0


def inspect(path, git=None, fetch=False) -> dict:
    """Where one repo stands against its upstream, from one ``git status --porcelain=v2 --branch``
    (git is slow to start on Windows). Never changes the working tree."""
    git = _runner(git)
    path = str(path)
    row = {"name": Path(path).name, "path": path, "branch": None, "upstream": None, "ahead": 0, "behind": 0,
           "dirty": False, "cloud_ahead": False, "state": "error", "detail": None}
    if not Path(path).is_dir():
        row["detail"] = "folder not found"
        return row
    if fetch:
        code, _, err = _run(git, ["fetch", "--quiet"], path, timeout=FETCH_TIMEOUT)
        if code != 0:
            row["fetch_error"] = _first_line(err) or "fetch failed"
    code, out, err = _run(git, ["status", "--porcelain=v2", "--branch", "--untracked-files=no"], path)
    if code != 0:
        row["detail"] = _first_line(err) or "not a git repo"
        return row
    heads, changes = {}, []
    for line in out.splitlines():
        if line.startswith("# "):
            key, _, value = line[2:].partition(" ")
            heads[key] = value.strip()
        elif line.strip():
            changes.append(line)
    branch = heads.get("branch.head")
    if not branch or branch == "(detached)":
        row["state"], row["detail"] = "detached", "HEAD is detached"
        return row
    row["branch"] = branch
    upstream = heads.get("branch.upstream")
    if not upstream:
        row["state"], row["detail"] = "no_upstream", "branch has no GitHub upstream"
        return row
    row["upstream"] = upstream
    try:
        plus, minus = heads["branch.ab"].split()
        ahead, behind = int(plus.lstrip("+")), int(minus.lstrip("-"))
    except (KeyError, ValueError):
        row["detail"] = "upstream branch is missing"
        return row
    row["ahead"], row["behind"], row["cloud_ahead"] = ahead, behind, behind > 0
    row["dirty"] = bool(changes)
    merging = any(c.startswith("u ") for c in changes) or _merge_head(path, git)
    if merging:
        row["state"], row["detail"] = "merging", "a merge is already in progress"
    elif row["dirty"]:
        row["state"], row["detail"] = "dirty", "has local edits"
    elif ahead and behind:
        row["state"] = "diverged"
    elif behind:
        row["state"] = "behind"
    elif ahead:
        row["state"] = "ahead"
    else:
        row["state"] = "up_to_date"
    return row


def _fast_forward(path, git):
    before = _run(git, ["rev-parse", "HEAD"], path)[1]
    code, _, err = _run(git, ["merge", "--ff-only", "--quiet", "@{u}"], path)
    after = _run(git, ["rev-parse", "HEAD"], path)[1]
    return code == 0 and before != after, _first_line(err)


def _merge(path, git):
    """Merge the upstream; on a conflict abort it. Returns (result, files, reason)."""
    code, out, err = _run(git, ["merge", "--no-edit", "--quiet", "@{u}"], path)
    if code == 0:
        return "merged", [], None
    files = [f for f in _run(git, ["diff", "--name-only", "--diff-filter=U"], path)[1].splitlines() if f.strip()]
    if _run(git, ["rev-parse", "-q", "--verify", "MERGE_HEAD"], path)[0] == 0:
        _run(git, ["merge", "--abort"], path)
    if files:
        return "conflict", files, None
    return "skipped", [], _first_line(err) or _first_line(out) or "git refused the merge"


# ---------------------------------------------------------------- questions
def _conflict(store, path):
    return _load(store, CONFLICT_KEY + str(path), None)


def _clear_conflict(store, path):
    if store.get_meta(CONFLICT_KEY + str(path)):
        store.set_meta(CONFLICT_KEY + str(path), "")


def _ask_conflict(store, name, path, files):
    """One open needs-you question per conflicting repo; reused while it stays open."""
    from . import needs
    prior = _conflict(store, path) or {}
    qid = prior.get("question")
    if qid:
        q = needs.get_question(store, qid)
        if q is not None and not q.get("answered_at"):
            store.set_meta(CONFLICT_KEY + str(path), json.dumps(dict(prior, files=files, at=_iso())))
            return qid
    shown = ", ".join(files[:5]) + (f" and {len(files) - 5} more" if len(files) > 5 else "")
    prompt = (f"{name}: cloud and local changes clash in {shown}. Merge them by hand, then press "
              f"Update local files again.")
    qid = needs.ask(store, kind="text", prompt=prompt, purpose="generic")
    store.set_meta(CONFLICT_KEY + str(path), json.dumps({"files": files, "question": qid, "at": _iso()}))
    store.event(None, None, "sync", ACTOR, f"{name} could not merge by itself, so {BRAND} asked Owner.",
                {"repo": name, "files": files, "question": qid})
    return qid


# ---------------------------------------------------------------- status / open / pull
_CACHE = {}
CACHE_SECONDS = 30.0


def _forget_cache():
    _CACHE.clear()


def _rows(store, git, repos, fetch):
    # git takes 3-5 s to answer per repo on Windows, so inspect them side by side (read-only);
    # conflict bookkeeping below stays sequential because it writes to the store.
    from concurrent.futures import ThreadPoolExecutor
    repos = list(repos)
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(repos)))) as pool:
        inspected = list(pool.map(lambda p: inspect(p, git=git, fetch=fetch), repos))
    rows = []
    for p, row in zip(repos, inspected):
        c = _conflict(store, p)
        if c and row["state"] in ("diverged", "merging", "dirty"):
            row["conflict"] = {"files": c.get("files") or [], "question": c.get("question")}
        elif c and row["state"] in ("up_to_date", "ahead", "behind"):
            _clear_conflict(store, p)
        rows.append(row)
    return rows


def _payload(store, rows):
    return {"mode": get_mode(store), "repos": rows,
            "needs_merge": any(r["state"] == "diverged" for r in rows),
            "last": _load(store, LAST_KEY, None), "checked_at": _iso()}


def status(store, git=None, repos=None, fetch=False, root=None) -> dict:
    git = _runner(git)
    repos = list(repos) if repos is not None else repo_paths(store, root)
    key = (str(getattr(store, "path", id(store))), tuple(repos), id(git))
    hit = _CACHE.get(key)
    if not fetch and hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return dict(hit[1], mode=get_mode(store), last=_load(store, LAST_KEY, None))
    with _LOCK:
        out = _payload(store, _rows(store, git, repos, fetch))
    _CACHE[key] = (time.monotonic(), out)
    return out


def _record(store, kind, rows, updated, skipped, conflicts):
    last = {"at": _iso(), "kind": kind, "mode": get_mode(store), "updated": updated, "skipped": skipped,
            "needs_merge": [r["name"] for r in rows if r["state"] == "diverged"], "conflicts": conflicts}
    store.set_meta(LAST_KEY, json.dumps(last))
    return last


def _summary_names(items):
    names = [i["name"] for i in items]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def on_open(store, git=None, repos=None, force=False, root=None, now=None) -> dict:
    """Fetch and fast-forward every clean repo that is strictly behind (throttled unless forced)."""
    git = _runner(git)
    repos = list(repos) if repos is not None else repo_paths(store, root)
    t = time.time() if now is None else now
    try:
        opened = float(store.get_meta(OPENED_KEY) or 0)
    except ValueError:
        opened = 0.0
    if not force and t - opened < OPEN_INTERVAL:
        return status(store, git=git, repos=repos)
    with _LOCK:
        store.set_meta(OPENED_KEY, str(t))
        updated, skipped = [], []
        for p in repos:
            row = inspect(p, git=git, fetch=True)
            if row["state"] == "behind":
                ok, reason = _fast_forward(p, git)
                if ok:
                    updated.append({"name": row["name"], "commits": row["behind"]})
                else:
                    skipped.append({"name": row["name"], "reason": reason or "fast-forward refused"})
            elif row["state"] in ("dirty", "merging") and row["behind"]:
                skipped.append({"name": row["name"], "reason": row["detail"]})
        _forget_cache()
        rows = _rows(store, git, repos, False)
        last = _record(store, "open", rows, updated, skipped, [])
        if updated:
            store.event(None, None, "sync", ACTOR,
                        f"{BRAND} brought {_summary_names(updated)} up to date with GitHub.", {"updated": updated})
        out = _payload(store, rows)
        out["last"] = last
    return out


def pull(store, git=None, repos=None, actor="owner", root=None) -> dict:
    """The 'Update local files' button: fast-forward or merge each repo GitHub is ahead of."""
    git = _runner(git)
    repos = list(repos) if repos is not None else repo_paths(store, root)
    with _LOCK:
        results, updated, skipped, conflicts = [], [], [], []
        for p in repos:
            row = inspect(p, git=git, fetch=True)
            res = {"name": row["name"], "path": p, "result": "unchanged"}
            if row["state"] == "behind":
                ok, reason = _fast_forward(p, git)
                res.update(result="fast_forwarded" if ok else "skipped", reason=None if ok else reason)
            elif row["state"] == "diverged":
                result, files, reason = _merge(p, git)
                res.update(result=result, files=files, reason=reason)
                if result == "conflict":
                    res["question"] = _ask_conflict(store, row["name"], p, files)
                    conflicts.append({"name": row["name"], "files": files, "question": res["question"]})
                elif result == "merged":
                    _clear_conflict(store, p)
            elif row["state"] in ("dirty", "merging") and row["behind"]:
                res.update(result="skipped", reason=row["detail"])
            elif row["state"] in ("error", "no_upstream", "detached"):
                res.update(result="skipped" if row["state"] != "error" else "error", reason=row["detail"])
            if res["result"] in ("fast_forwarded", "merged"):
                updated.append({"name": row["name"], "commits": row["behind"], "how": res["result"]})
            elif res["result"] == "skipped" and row["behind"]:
                skipped.append({"name": row["name"], "reason": res.get("reason")})
            results.append(res)
        _forget_cache()
        rows = _rows(store, git, repos, False)
        last = _record(store, "pull", rows, updated, skipped, conflicts)
        if updated:
            who = "Owner" if actor == "owner" else actor
            store.event(None, None, "sync", actor, f"{who} updated {_summary_names(updated)} from GitHub.",
                        {"updated": updated})
        out = _payload(store, rows)
        out.update(last=last, results=results)
    return out


# ---------------------------------------------------------------- routes
def _http(status_code, body):
    from .api import HttpError  # lazy: api imports extension modules
    return HttpError(status_code, body)


def _owner(h, what):
    from .api import _owner_only
    _owner_only(h, what)


def _app_git(h):
    return getattr(h.app, "sync_git", None)


def _flag(q, key):
    v = q.get(key) if isinstance(q, dict) else None
    if isinstance(v, list):
        v = v[0] if v else None
    return str(v or "").lower() in ("1", "true", "yes")


def g_status(h, q):
    return status(h.app.store, git=_app_git(h), fetch=_flag(q, "fetch"))


def p_mode(h, q):
    _owner(h, "switch between Cloud and Local")
    b = h._json_body()
    try:
        mode = set_mode(h.app.store, b.get("mode"), actor="owner")
    except ValueError as e:
        raise _http(400, {"error": "bad_request", "detail": str(e)})
    return dict(status(h.app.store, git=_app_git(h)), ok=True, mode=mode)


def p_open(h, q):
    b = h._json_body()
    force = bool(b.get("force")) and getattr(h, "actor", None) == "owner"
    return dict(on_open(h.app.store, git=_app_git(h), force=force), ok=True)


def p_pull(h, q):
    _owner(h, "update the local files")
    b = h._json_body()
    store = h.app.store
    repos = None
    wanted = b.get("repos")
    if wanted:
        if not isinstance(wanted, list):
            raise _http(400, {"error": "bad_request", "detail": "repos must be a list of repo names"})
        known = {Path(p).name: p for p in repo_paths(store)}
        missing = [n for n in wanted if n not in known]
        if missing:
            raise _http(404, {"error": "not_found", "detail": "unknown repo " + ", ".join(map(str, missing))})
        repos = [known[n] for n in wanted]
    return dict(pull(store, git=_app_git(h), repos=repos, actor="owner"), ok=True)


ROUTES = {
    "GET /api/sync/status": g_status,
    "POST /api/sync/mode": p_mode,
    "POST /api/sync/open": p_open,
    "POST /api/sync/pull": p_pull,
}
