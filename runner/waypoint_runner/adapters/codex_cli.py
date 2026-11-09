"""Codex CLI as a worker route (``codex exec --json``), behind its toggle and an editable floor.

Reference: engine/cc_engine/dispatch.py codex_argv. Owner sets the Codex floor in Waypoint; an unset floor
holds the pool (and raises "Set floor for <pool>"), and a stale or unknown reading holds it too. All of that
is decided by ``policy.allowed`` in the shared base. The Codex MOD adapter port is out of 1.2; this is only
the worker route.
"""
from __future__ import annotations

from .headless_claude import AdapterRefused, CliAdapter

__all__ = ["AdapterRefused", "CodexCliAdapter"]

CODEX_SANDBOX = "workspace-write"
CODEX_DROP_ENV = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE", "CODEX_API_KEY",
                  "AZURE_OPENAI_API_KEY", "CODEX_HOME")


class CodexCliAdapter(CliAdapter):
    name = "codex_cli"
    route = "codex"
    drop_env = CODEX_DROP_ENV
    default_exe = "codex"

    def build_argv(self, spec, exe):
        argv = [exe, "exec", "--json", "--skip-git-repo-check", "--sandbox", CODEX_SANDBOX]
        if spec.get("cwd"):
            argv += ["-C", str(spec["cwd"])]
        if spec.get("model"):
            argv += ["-m", str(spec["model"])]
        return argv + ["-"]
