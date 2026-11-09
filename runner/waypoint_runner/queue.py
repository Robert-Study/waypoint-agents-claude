"""Per-project prompt queue: prompts wait here in the order they were added, one file per project
(queue/<project>.json), written only through core/waypoint-data/py. The runner pops the head when the
project is free to take the next prompt.
"""
from __future__ import annotations

import secrets
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_DATA_PY = _REPO / "core" / "waypoint-data" / "py"
if _DATA_PY.is_dir() and str(_DATA_PY) not in sys.path:
    sys.path.append(str(_DATA_PY))

from waypoint_data.store import RevConflict, Store, iso  # noqa: E402

__all__ = ["PromptQueue"]

WRITER = "runner"


class PromptQueue:
    def __init__(self, root, now=time.time):
        self.root = Path(root)
        self.now = now
        self.store = Store(self.root, writer=WRITER, now=now)

    @staticmethod
    def _rel(project):
        return f"queue/{project}.json"

    def _read(self, project):
        return self.store.read(self._rel(project)) or {"schema": "waypoint.prompt_queue.v1", "project": project,
                                                       "items": []}

    def _mutate(self, project, fn):
        """Compare-and-swap loop: ``fn(items)`` edits the list and returns the result."""
        for _ in range(8):
            cur = self._read(project)
            items = [dict(i) for i in cur["items"]]
            result = fn(items)
            try:
                self.store.write(self._rel(project), dict(cur, items=items), expected_rev=cur.get("rev", 0))
                return result
            except RevConflict:
                continue
        raise RuntimeError("prompt queue busy")

    def add(self, project, prompt):
        text = (prompt or "").strip()
        if not text:
            raise ValueError("empty prompt")
        entry = {"id": secrets.token_hex(4), "prompt": text, "added_at": iso(self.now())}

        def op(items):
            items.append(entry)
            return entry
        return self._mutate(project, op)

    def list(self, project):
        return [dict(i) for i in self._read(project)["items"]]

    def peek(self, project):
        items = self._read(project)["items"]
        return dict(items[0]) if items else None

    def pop(self, project):
        def op(items):
            return items.pop(0) if items else None
        return self._mutate(project, op)

    def remove(self, project, entry_id):
        def op(items):
            for i, e in enumerate(items):
                if e["id"] == entry_id:
                    del items[i]
                    return True
            return False
        return self._mutate(project, op)

    def move(self, project, entry_id, index):
        def op(items):
            for i, e in enumerate(items):
                if e["id"] == entry_id:
                    items.insert(max(0, min(index, len(items) - 1)), items.pop(i))
                    return True
            return False
        return self._mutate(project, op)
