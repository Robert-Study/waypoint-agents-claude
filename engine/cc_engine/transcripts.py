"""Zero-token transcript meters (Waypoint Core 1.1).

``Tailer`` follows Claude Code transcripts (``*.jsonl`` and ``<session>/subagents/agent-*.jsonl``) by byte
offset. It never calls a model. A poll stats every file first and reads only the ones that grew, at most
``max_bytes`` per poll. Per file it keeps token meters (deduped by ``requestId``), the context size, an
estimated cost at public list prices, the last text line and the tool still waiting for its result.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

PRICES_PATH = Path(__file__).with_name("model_prices.json")
DEFAULT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_RECENT_DAYS = 1  # first scan: history older than this is skipped (the tree shows 24 h); read whole if it grows
MAX_LINE = 16 * 1024 * 1024
CHUNK = 1024 * 1024
LAST_LINE_MAX = 140
TITLE_TYPES = ("ai-title", "custom-title", "summary")


def load_prices(path=None) -> dict:
    try:
        data = json.loads(Path(path or PRICES_PATH).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def default_roots():
    return [Path.home() / ".claude" / "projects"]


def context_window(model, prices) -> int:
    """The family's own window when the prices file names one (Claude 5 and 4.6+ have 1M), else 1M for a
    ``[1m]`` model id, else the default."""
    if model and str(model).endswith("[1m]"):
        return int(prices.get("context_window_1m") or 1000000)
    row = family_prices(model, prices) or {}
    if isinstance(row.get("context_window"), int) and row["context_window"] > 0:
        return row["context_window"]
    return int(prices.get("context_window_default") or 200000)


def family_prices(model, prices):
    """Price row for the longest family prefix of ``model``, or None."""
    if not model:
        return None
    base = re.sub(r"\[.*\]$", "", str(model))
    best = None
    for fam, row in (prices.get("families") or {}).items():
        if base.startswith(fam) and isinstance(row, dict) and (best is None or len(fam) > len(best[0])):
            best = (fam, row)
    return best[1] if best else None


def request_cost(model, tin, tout, cread, cwrite, prices):
    """USD estimate for one request, or None when the model or a price is unknown."""
    if not (tin or tout or cread or cwrite):
        return 0.0
    row = family_prices(model, prices)
    if not row:
        return None
    p = [row.get("input"), row.get("output"), row.get("cache_read"), row.get("cache_write")]
    if any(not isinstance(v, (int, float)) for v in p):
        return None
    return (tin * p[0] + tout * p[1] + cread * p[2] + cwrite * p[3]) / 1_000_000


def _num(v) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


def _new_file(path, kind="session"):
    return {"path": str(path), "offset": 0, "mtime": 0.0, "size": 0, "kind": kind, "skipped": False,
            "requests": {}, "pending": {}, "model": None, "cwd": None, "title": None, "last_line": None,
            "updated_at": None, "started_at": None, "last_req": None, "meta": {}, "meta_mtime": None,
            "rows": 0, "wait": None}


class Tailer:
    def __init__(self, roots=None, state=None, max_bytes=DEFAULT_MAX_BYTES, recent_days=DEFAULT_RECENT_DAYS,
                 prices=None, clock=time.time):
        self.roots = [Path(r) for r in (roots if roots is not None else default_roots())]
        self.state = state if state is not None else {}
        self.files = self.state.setdefault("files", {})
        self.max_bytes = int(max_bytes)
        self.recent_days = recent_days
        self.prices = prices if prices is not None else load_prices()
        self.clock = clock
        self.reads = 0  # file opens for reading (tests assert an idle poll opens nothing)
        self.first_scan_done = bool(self.files)

    # ------------------------------------------------------------ discovery
    def _discover(self):
        for root in self.roots:
            try:
                slugs = list(os.scandir(root))
            except OSError:
                continue
            for slug in slugs:
                try:
                    if not slug.is_dir():
                        continue
                    entries = list(os.scandir(slug.path))
                except OSError:
                    continue
                for e in entries:
                    try:
                        if e.is_file() and e.name.endswith(".jsonl"):
                            yield e.path, "session", e.stat()
                        elif e.is_dir():
                            sub = os.path.join(e.path, "subagents")
                            if not os.path.isdir(sub):
                                continue
                            for s in os.scandir(sub):
                                if s.name.startswith("agent-") and s.name.endswith(".jsonl") and s.is_file():
                                    yield s.path, "subagent", s.stat()
                    except OSError:
                        continue

    # ------------------------------------------------------------ polling
    def poll(self):
        """Read what grew. Returns the file paths that changed."""
        now = self.clock()
        cutoff = now - self.recent_days * 86400 if self.recent_days else None
        first = not self.first_scan_done
        budget = self.max_bytes
        changed = []
        # Newest first: live sessions get the read budget before old history does.
        for path, kind, st in sorted(self._discover(), key=lambda x: x[2].st_mtime, reverse=True):
            f = self.files.get(path)
            if f is None:
                f = self.files[path] = _new_file(path, kind)
                f["mtime"], f["size"] = st.st_mtime, st.st_size
                if first and cutoff is not None and st.st_mtime < cutoff:
                    f["offset"], f["skipped"] = st.st_size, True
                    continue
            else:
                if st.st_size == f["size"] and st.st_mtime == f["mtime"] and (f["offset"] >= f["size"] or f["wait"] == f["size"]):
                    if kind == "subagent" and self._meta_changed(f):
                        changed.append(path)
                    continue
                f["mtime"], f["size"] = st.st_mtime, st.st_size
            if st.st_size < f["offset"]:  # truncated or replaced
                self._reset(f)
            elif f["skipped"]:
                f["skipped"], f["offset"] = False, 0
            if f["offset"] < st.st_size and budget > 0:
                used = self._read(f, st.st_size, budget)
                budget -= used
                if used:
                    changed.append(path)
            if kind == "subagent" and self._meta_changed(f) and path not in changed:
                changed.append(path)
        self.first_scan_done = True
        return changed

    def _reset(self, f):
        keep = {k: f[k] for k in ("path", "kind", "mtime", "size")}
        f.clear()
        f.update(_new_file(keep["path"], keep["kind"]))
        f.update(keep)

    def _meta_changed(self, f) -> bool:
        meta_path = f["path"][:-len(".jsonl")] + ".meta.json"
        try:
            mt = os.stat(meta_path).st_mtime
        except OSError:
            return False
        if mt == f["meta_mtime"]:
            return False
        f["meta_mtime"] = mt
        try:
            self.reads += 1
            meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
            f["meta"] = meta if isinstance(meta, dict) else {}
        except (OSError, ValueError):
            f["meta"] = {}
        return True

    def _read(self, f, size, budget) -> int:
        """Parse the complete lines after f['offset'] (at most ``budget`` bytes, unless one line is longer).
        Returns bytes consumed."""
        try:
            fh = open(f["path"], "rb")
        except OSError:
            return 0
        self.reads += 1
        try:
            fh.seek(f["offset"])
            data = fh.read(min(budget, max(size - f["offset"], 0)))
            cut = data.rfind(b"\n")
            while cut < 0 and f["offset"] + len(data) < size and len(data) < MAX_LINE:
                more = fh.read(CHUNK)
                if not more:
                    break
                data += more
                cut = data.rfind(b"\n")
            if cut < 0:
                if len(data) >= MAX_LINE:  # absurd line: skip it
                    f["offset"] += len(data)
                    return len(data)
                f["wait"] = size
                return 0  # a partial last line: wait for the rest
            for line in data[:cut].split(b"\n"):
                if line.strip():
                    self._row(f, line)
            f["offset"] += cut + 1
            return cut + 1
        finally:
            fh.close()

    # ------------------------------------------------------------ rows
    def _row(self, f, line):
        try:
            row = json.loads(line)
        except ValueError:
            return
        if not isinstance(row, dict):
            return
        f["rows"] += 1
        if isinstance(row.get("timestamp"), str):
            f["updated_at"] = row["timestamp"]
            if f["started_at"] is None:
                f["started_at"] = row["timestamp"]
        if isinstance(row.get("cwd"), str) and row["cwd"]:
            f["cwd"] = row["cwd"]
        rtype = row.get("type")
        if rtype in TITLE_TYPES:
            t = row.get("aiTitle") or row.get("customTitle") or row.get("title") or row.get("summary")
            if isinstance(t, str) and t.strip():
                f["title"] = t.strip()[:120]
        msg = row.get("message")
        if not isinstance(msg, dict):
            return
        content = msg.get("content")
        if rtype == "assistant" or msg.get("role") == "assistant":
            self._assistant(f, row, msg, content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    f["pending"].pop(str(block.get("tool_use_id")), None)

    def _assistant(self, f, row, msg, content):
        model = msg.get("model")
        usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
        if isinstance(model, str) and model and not model.startswith("<"):
            f["model"] = model
        rid = row.get("requestId") or msg.get("id") or row.get("uuid")
        if rid is not None and usage:
            u = [_num(usage.get("input_tokens")), _num(usage.get("output_tokens")),
                 _num(usage.get("cache_read_input_tokens")), _num(usage.get("cache_creation_input_tokens"))]
            old = f["requests"].get(rid)
            if old is None or u[1] >= old[1]:
                f["requests"][rid] = u + [model if isinstance(model, str) else None]
            f["last_req"] = rid
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    first = next((ln.strip() for ln in block["text"].splitlines() if ln.strip()), "")
                    if first:
                        f["last_line"] = first[:LAST_LINE_MAX]
                elif block.get("type") == "tool_use" and block.get("id"):
                    f["pending"][str(block["id"])] = block.get("name")

    # ------------------------------------------------------------ results
    def snapshot(self, paths=None) -> dict:
        """``{key: meters}`` for every file with rows. A session key is the session id, a subagent key is
        ``<session>/<agentId>``."""
        out = {}
        for path, f in self.files.items():
            if (paths is not None and path not in paths) or (not f["rows"] and not f["meta"]):
                continue
            p = Path(path)
            if f["kind"] == "subagent":
                session = p.parent.parent.name
                agent = p.stem[len("agent-"):]
                key = f"{session}/{agent}"
            else:
                session, agent, key = p.stem, None, p.stem
            reqs = list(f["requests"].values())
            costs = [request_cost(r[4], r[0], r[1], r[2], r[3], self.prices) for r in reqs]
            cost = None if (not costs or any(c is None for c in costs)) else round(sum(costs), 6)
            last = f["requests"].get(f["last_req"])
            meta = f["meta"]
            pending = list(f["pending"].values())
            out[key] = {
                "id": key, "kind": f["kind"], "session_id": session, "agent_id": agent,
                "parent": session if agent else None, "agent_type": meta.get("agentType"),
                "description": meta.get("description"), "finished": bool(meta.get("finished")),
                "cwd": f["cwd"], "title": f["title"], "model": f["model"],
                "tokens_in": sum(r[0] for r in reqs), "tokens_out": sum(r[1] for r in reqs),
                "cache_read": sum(r[2] for r in reqs), "cache_creation": sum(r[3] for r in reqs),
                "context": (last[0] + last[2] + last[3]) if last else 0,
                "context_window": context_window(f["model"], self.prices), "cost_eq": cost,
                "last_line": f["last_line"], "tool": pending[-1] if pending else None,
                "updated_at": f["updated_at"], "started_at": f["started_at"], "transcript": path,
            }
        return out
