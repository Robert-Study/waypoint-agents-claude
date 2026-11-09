"""Location-neutral path resolver (Python twin of src/paths.ts, replayed from fixtures/paths.json).

Shared data stores cwd as {machine, rel}; the absolute code root per machine lives in <data root>/machines/<id>.json
(local-only). Never store an absolute drive path in shared data. Pure string work; stdlib only."""
import json
import ntpath
import posixpath
import re

_SEP = re.compile(r"[\\/]+")
_TRAILING = re.compile(r"(.)[\\/]+$")


class PathError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _p(platform):
    return ntpath if platform == "win32" else posixpath


def _strip(s):
    return _TRAILING.sub(r"\1", s)


def _resolve(p, *parts):
    """Node's path.resolve for absolute inputs: join, then normalise. No working directory is consulted."""
    out = parts[0]
    for part in parts[1:]:
        out = p.join(out, part)
    return p.normpath(out)


def resolve_data_root(env, home, read_file, platform="win32"):
    """Order: WAYPOINT_DATA_DIR, then <home>/.waypoint/data-dir (first non-empty line), then <home>/.waypoint/data."""
    p = _p(platform)
    from_env = (env.get("WAYPOINT_DATA_DIR") or "").strip()
    if from_env:
        return {"root": _strip(p.normpath(from_env)), "source": "env"}
    text = read_file(p.join(home, ".waypoint", "data-dir"))
    if text is not None:
        text = text.lstrip("﻿")
        line = next((s.strip() for s in re.split(r"\r?\n", text) if s.strip() != ""), None)
        val = re.sub(r'^"(.*)"$', r"\1", line).strip() if line is not None else None
        if val:
            return {"root": _strip(_resolve(p, home, val)), "source": "pointer"}
    return {"root": p.join(home, ".waypoint", "data"), "source": "default"}


def machine_file_path(data_root, machine_id, platform="win32"):
    return _p(platform).join(data_root, "machines", f"{machine_id}.json")


def parse_machine_file(text):
    """The code root out of the text of machines/<id>.json."""
    try:
        j = json.loads(text)
    except ValueError:
        raise PathError("bad_machine_file", "machine file is not valid JSON") from None
    root = j.get("code_root") if isinstance(j, dict) else None
    if not isinstance(root, str) or not root.strip():
        raise PathError("bad_machine_file", "machine file has no code_root")
    return root


def _check_rel(rel):
    if re.match(r"^[A-Za-z]:", rel) or re.match(r"^[\\/]", rel):
        raise PathError("bad_rel", f"rel must be relative: {rel}")
    parts = [s for s in _SEP.split(rel) if s not in ("", ".")]
    if ".." in parts:
        raise PathError("bad_rel", f"rel must stay inside the code root: {rel}")
    return parts


def to_local_path(cwd, machines, platform="win32"):
    """Maps a stored {machine, rel} to a local path. The drive need not exist."""
    p = _p(platform)
    root = machines.get(cwd["machine"])
    if root is None:
        raise PathError("unknown_machine", f"no machine file for {cwd['machine']}")
    parts = _check_rel(cwd["rel"])
    if parts:
        return p.normpath(p.join(root, *parts))
    return _strip(p.normpath(root))


def to_cwd_ref(machine, code_root, abs_path, platform="win32"):
    """Maps an absolute local path to the shareable {machine, rel}. Refuses anything outside the code root."""
    p = _p(platform)
    fold = (lambda s: s.lower()) if platform == "win32" else (lambda s: s)
    root = p.normpath(code_root)
    target = p.normpath(abs_path)
    try:
        rel = p.relpath(fold(target), fold(root))
    except ValueError:  # different drives
        raise PathError("outside_root", f"{abs_path} is outside {code_root}") from None
    if rel == ".":
        return {"machine": machine, "rel": ""}
    if rel.startswith("..") or p.isabs(rel):
        raise PathError("outside_root", f"{abs_path} is outside {code_root}")
    tail = target[len(root):].lstrip("\\/")
    return {"machine": machine, "rel": "/".join(_SEP.split(tail))}
