"""Worker brief and prompt packet builders: the Python port of core/waypoint-data/src/brief.ts.

Pure functions, no I/O. ``tests/test_brief.py`` replays core/waypoint-data/fixtures/brief.json so this port and
the TypeScript original produce identical text. Change both together or neither.
"""
from __future__ import annotations

import json
import re

__all__ = ["RULES", "scrub", "receipt_path", "describe_check", "build_worker_brief", "build_prompt_packet"]

RULES = [
    "Do not change the goal.",
    "Do only this task. Write only the owned files (and the test files the check names).",
    "Write the check first if it does not exist, see it fail, then make it pass. Never weaken or skip it.",
    "No paid routes, no new credentials, never push public.",
    "Report evidence: the exact command output or files that prove the check passes.",
]

_A = re.ASCII  # JS \b, \w and \d are ASCII-only
_REDACTIONS = [
    (re.compile(r"\b(X-CC-(?:Owner-)?Token\s*[:=]\s*)\S+", re.I | _A), r"\1[redacted]"),
    (re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{20,}"
                r"|xox[abprs]-[A-Za-z0-9-]{10,})", _A), "[redacted-key]"),
    (re.compile(r"\b[0-9a-fA-F]{24,}\b", _A), "[redacted-token]"),
    (re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+[^\s\"'`)\]]*", re.I), "<path>"),
    (re.compile(r"/(?:home|Users)/[^\s\"'`)\]]*"), "<path>"),
]


def scrub(text: str) -> str:
    """Remove tokens, keys and absolute personal paths."""
    out = text
    for rx, rep in _REDACTIONS:
        out = rx.sub(rep, out)
    return out


def receipt_path(node) -> str:
    return f".waypoint/receipts/{node['project']}.{node['id']}.json"


def describe_check(check) -> str:
    check = check or {}
    kind = check.get("type")
    if kind == "command":
        s = f"Run `{check.get('run') or ''}`; it must exit 0"
        if check.get("expect_stdout"):
            s += f' and print "{check["expect_stdout"]}"'
        return s + "."
    if kind == "files_exist":
        return f"These paths must exist: {', '.join(check.get('paths') or [])}."
    if kind == "text_present":
        return f"File {check.get('path') or ''} must contain \"{check.get('text') or ''}\"."
    if kind == "human":
        return f"Owner confirms: {check.get('prompt') or ''}"
    return "(unknown check type)"


def _quote(text: str) -> str:
    return "\n".join(">" if line == "" else "> " + line for line in text.split("\n"))


def _downstream(node, graph):
    res = []
    for n in (graph or {}).get("nodes") or []:
        for d in n.get("deps") or []:
            if d.get("id") == node["id"]:
                res.append({"id": n["id"], "title": n.get("title") or n["id"], "crosses": d.get("crosses")})
                break
    return res


def _files_block(node):
    files = node.get("files") or []
    return [f"- {f}" for f in files] if files else ["- (none named)"]


def _deps_block(node):
    deps = node.get("deps") or []
    return [f"- {d['id']}: {d.get('crosses')}" for d in deps] if deps else ["- (none)"]


def build_worker_brief(node, graph=None, todos=()) -> str:
    """Context text for a worker: goal, check, owned files, what crosses in and out, rules."""
    out = _downstream(node, graph)
    open_todos = [t for t in (todos or ()) if t.get("status") == "open" and t.get("node")
                  and t["node"].get("project") == node["project"] and t["node"].get("id") == node["id"]]
    lines = [
        f"# Your node: {node['id']} ({node.get('title')})",
        f"Project: {node['project']}",
        "",
        "## Goal (fixed)",
        _quote(node["goal"]),
        "",
        "## Done-check",
        describe_check(node.get("check")),
        "",
        "## Owned files (write only these)",
        *_files_block(node),
        "",
        "## Comes in",
        *_deps_block(node),
        "",
        "## Goes out",
        *([f"- {o['id']}: {o['crosses']}" for o in out] or ["- (nothing depends on this node)"]),
    ]
    if open_todos:
        lines += ["", "## Open to-dos on this node", *[f"- {t['text']}" for t in open_todos]]
    lines += ["", "## Receipt", f"Write {receipt_path(node)} when finished.", "", "## Rules", *[f"- {r}" for r in RULES]]
    return scrub("\n".join(lines)) + "\n"


def build_prompt_packet(node, graph=None) -> str:
    """Self-contained copy-paste packet for one node."""
    out = _downstream(node, graph)
    lines = [
        f"# Task {node['id']}: {node.get('title')}",
        f"Project: {node['project']}",
        "",
        "## Goal (locked, quoted exactly)",
        _quote(node["goal"]),
        "",
        "## Done-check (must pass)",
        describe_check(node.get("check")),
        "```json",
        json.dumps(node.get("check"), indent=1, ensure_ascii=False),
        "```",
        "",
        "## Inputs (what crosses in)",
        *_deps_block(node),
        "",
        "## Owned files (write only these)",
        *_files_block(node),
        "",
        "## What crosses out",
        *([f"- {o['id']} ({o['title']}): {o['crosses']}" for o in out] or ["- (nothing depends on this node)"]),
    ]
    prompt = node.get("prompt")
    if prompt and prompt.strip():
        lines += ["", "## Plan prompt", prompt]
    receipt = {"schema": "waypoint.receipt.v1", "project": node["project"], "node": node["id"], "status": "done",
               "summary": "One plain sentence.", "evidence": ["command: result"], "outputs": ["path/you/wrote"],
               "todo": None}
    lines += [
        "",
        "## Exit criteria",
        "- The done-check passes on your branch.",
        "- Only the owned files changed.",
        "- The deliverable is complete, not empty.",
        "",
        "## Receipt format",
        f"Write one file, {receipt_path(node)}:",
        "```json",
        json.dumps(receipt, indent=1, ensure_ascii=False),
        "```",
        "",
        "## Rules",
        *[f"- {r}" for r in RULES],
    ]
    return scrub("\n".join(lines)) + "\n"
