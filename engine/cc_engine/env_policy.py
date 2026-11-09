"""Automatic environment choice (Owner, 4 Nov 2026): the agent decides where a task runs.

``choose(task, live_state)`` is pure: no files, no network, no clock. The rules come from the
workspace AGENTS.md ("main is the only long-lived branch; one chat per repo on main; each extra chat
uses its own worktree"; "one parent owns integration; concurrent writers never share files"):

1. The task needs this conversation: ``fork`` when the context is short, ``new_chat`` (with a
   handover note) when it is large. A forked writer shares a repo with another live writer, so its
   ``workdir`` becomes ``worktree``.
2. Read-only work is a ``subagent`` (it writes nothing, so it never collides).
3. A write task with another live writer on the same repo (main folder, claim not stale) gets its own
   ``worktree``.
4. A bounded write task on a free repo is a ``subagent``.
5. Otherwise ``main``; when this conversation's context is already large, a ``new_chat`` instead.

``task``: ``repo`` (path), ``read_only``, ``bounded``, ``needs_conversation``, ``context_tokens`` (or
``context``: ``"short"|"large"``).
``live_state``: ``writers`` (list of ``{repo, id, worktree, stale}``) or ``live_writers`` (int),
``self`` (this session's id, never counted), ``context_tokens`` (fallback for the task's).

``live_state["enabled_agents"]`` (ids that are switched on, e.g. from toggles.is_enabled) feeds ``candidates``
for a ``subagent`` result; Codex is named there only when it is enabled (the caller reads the toggles; this
module stays pure).

Result: ``{"env", "why", "instructions", "workdir", "candidates"}``. ``instructions`` is always the Drive AGENTS.md.
"""
from __future__ import annotations
from . import config

ENVS = ("main", "worktree", "new_chat", "fork", "subagent")
INSTRUCTIONS = config.INSTRUCTIONS
WORKTREES_ROOT = config.WORKTREES_ROOT
LARGE_CONTEXT_TOKENS = 60_000


def _repo_key(path):
    text = str(path or "").strip().replace("\\", "/").rstrip("/")
    return text.casefold()


def _is_main(worktree):
    return worktree in (None, "", "main") or str(worktree).strip().lower() == "main"


def _other_writers(task, state) -> int:
    """Live writers on this repo's main folder, not counting this session itself."""
    if isinstance(state.get("live_writers"), int) and not isinstance(state.get("live_writers"), bool):
        return max(0, state["live_writers"])
    repo = _repo_key(task.get("repo"))
    me = state.get("self")
    count = 0
    for w in state.get("writers") or []:
        if not isinstance(w, dict) or w.get("stale"):
            continue
        if me is not None and w.get("id") == me:
            continue
        if repo and w.get("repo") and _repo_key(w["repo"]) != repo:
            continue
        if not _is_main(w.get("worktree")):
            continue  # a writer inside its own worktree does not occupy main
        count += 1
    return count


def writers_from_claims(claims, repo=None) -> list:
    """Turn ``store.claims(project)`` (``{node: {by, worktree, cwd, stale}}``) into ``writers`` entries."""
    out = []
    for node_id, c in (claims or {}).items():
        if not isinstance(c, dict):
            continue
        out.append({"repo": repo or c.get("cwd"), "id": c.get("by") or node_id,
                    "worktree": c.get("worktree"), "stale": bool(c.get("stale"))})
    return out


def _is_large(task, state) -> bool:
    tokens = task.get("context_tokens")
    if tokens is None:
        tokens = state.get("context_tokens")
    if isinstance(tokens, (int, float)) and not isinstance(tokens, bool):
        return tokens > LARGE_CONTEXT_TOKENS
    label = str(task.get("context") or state.get("context") or "").strip().lower()
    return label in ("large", "long", "big")


def _candidates(env, state):
    if env != "subagent":
        return []
    enabled = state.get("enabled_agents")
    if not isinstance(enabled, (list, tuple, set, frozenset)):
        return []
    return [a for a in enabled if isinstance(a, str) and a.strip()]


def _result(env, why, workdir="main", state=None):
    return {"env": env, "why": why, "instructions": INSTRUCTIONS, "workdir": workdir,
            "candidates": _candidates(env, state or {})}


def choose(task, live_state=None) -> dict:
    if not isinstance(task, dict):
        raise ValueError("task must be a dict")
    state = live_state if isinstance(live_state, dict) else {}
    writers = _other_writers(task, state)
    write = not task.get("read_only")
    large = _is_large(task, state)
    workdir = "worktree" if (write and writers) else "main"

    if task.get("needs_conversation"):
        if large:
            return _result("new_chat", "The task needs this conversation but its context is large: "
                           "start a new chat with a handover note.", workdir)
        if workdir == "worktree":
            return _result("fork", "The task needs this short conversation and another writer is on the "
                           "repo: fork it into its own worktree.", workdir)
        return _result("fork", "The task needs this short conversation: fork it.", workdir)
    if task.get("read_only"):
        return _result("subagent", "Read-only work cannot collide: hand it to a subagent.", state=state)
    if writers:
        return _result("worktree", f"{writers} other live writer(s) on this repo: use a worktree under "
                       f"{WORKTREES_ROOT}.", "worktree")
    if task.get("bounded"):
        return _result("subagent", "A bounded task on a free repo suits a subagent.", state=state)
    if large:
        return _result("new_chat", "No other writer, but this context is large: start a new chat with "
                       "a handover note.")
    return _result("main", "No other live writer on this repo: work on main.")
