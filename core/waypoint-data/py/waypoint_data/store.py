"""Entity store for the Waypoint data folder: canonical serialization, revisions (compare-and-swap), locks,
sync hygiene (quarantine, stale temp cleanup) and append-only logs. Rules: docs/data/CONCURRENCY.md. Stdlib only."""
import datetime as _dt
import json
import os
import random
import secrets
import socket
import time
from decimal import Decimal

from . import io as wio
from .io import LockBusy, ReadTorn, WriteLocked  # noqa: F401  (re-exported)

LOCK_STALE_S = 30.0
LOCK_WAIT_S = 5.0


class RevConflict(Exception):
    """The file's rev is not the expected_rev. `current` is the entity on disk (None if it does not exist)."""
    code = "rev_conflict"

    def __init__(self, message, current=None):
        super().__init__(message)
        self.current = current


# ------------------------------------------------------------------ serialization

def _js_number(x):
    """Number text exactly as JavaScript's JSON.stringify prints it."""
    if isinstance(x, int):
        return str(x)
    if x != x or x in (float("inf"), float("-inf")):
        raise ValueError("NaN and Infinity are not valid JSON")
    if x == int(x) and abs(x) < 1e21:
        return str(int(x))
    r = repr(x)
    if "e" not in r and "E" not in r:
        return r
    d = Decimal(r)
    exp10 = d.adjusted()
    if -7 <= exp10 < 21:
        return format(d, "f")
    mant, _, e = r.lower().partition("e")
    return f"{mant}e{'+' if int(e) >= 0 else '-'}{abs(int(e))}"


def _dump(v, ind, out):
    if v is None:
        out.append("null")
    elif v is True:
        out.append("true")
    elif v is False:
        out.append("false")
    elif isinstance(v, (int, float)):
        out.append(_js_number(v))
    elif isinstance(v, str):
        out.append(json.dumps(v, ensure_ascii=False))
    elif isinstance(v, (list, tuple)):
        if not v:
            out.append("[]")
            return
        pad = "  " * (ind + 1)
        out.append("[\n")
        for i, item in enumerate(v):
            out.append(pad)
            _dump(item, ind + 1, out)
            out.append(",\n" if i < len(v) - 1 else "\n")
        out.append("  " * ind + "]")
    elif isinstance(v, dict):
        if not v:
            out.append("{}")
            return
        pad = "  " * (ind + 1)
        out.append("{\n")
        items = list(v.items())
        for i, (k, item) in enumerate(items):
            out.append(pad + json.dumps(str(k), ensure_ascii=False) + ": ")
            _dump(item, ind + 1, out)
            out.append(",\n" if i < len(items) - 1 else "\n")
        out.append("  " * ind + "}")
    else:
        raise TypeError(f"cannot serialize {type(v).__name__}")


def serialize(entity):
    """Canonical text: JSON.stringify(entity, null, 2) + "\\n", key order preserved, UTF-8, no ASCII escapes."""
    out = []
    _dump(entity, 0, out)
    out.append("\n")
    return "".join(out)


def iso(ts):
    """ISO 8601 UTC with milliseconds and Z, from epoch seconds."""
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(ts * 1000) % 1000:03d}Z"


def parse_iso(text):
    """Epoch seconds from an ISO 8601 string with Z or an offset; None if it does not parse."""
    try:
        t = _dt.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=_dt.timezone.utc)
    return t.timestamp()


# ------------------------------------------------------------------ locks

def _pid_alive(pid):
    if os.name == "nt":
        return True  # os.kill would terminate the process on Windows: judge by time only
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


class Lock:
    def __init__(self, path, token):
        self.path = path
        self.token = token

    def release(self):
        """Delete the lock only if the token is still ours. False means it was broken: redo read-check-write."""
        try:
            cur = wio.read_json(self.path, torn_wait_s=0.01, retries=1)
        except (ReadTorn, WriteLocked):
            return False
        if not isinstance(cur, dict) or cur.get("token") != self.token:
            return False
        return wio.remove_if_exists(self.path)


def _lock_is_stale(path, stale_s, now):
    try:
        cur = wio.read_json(path, torn_wait_s=0.01, retries=1)
    except (ReadTorn, WriteLocked):
        cur = None  # empty or unparseable: treated as held until it ages out by mtime
    if cur is None and not os.path.exists(path):
        return False
    if isinstance(cur, dict):
        if cur.get("host") == socket.gethostname() and isinstance(cur.get("pid"), int) and not _pid_alive(cur["pid"]):
            return True
        beat = parse_iso(cur.get("heartbeat_at") or cur.get("acquired_at") or cur.get("at"))
        if beat is not None:
            return now() - beat > stale_s
    try:
        return now() - os.path.getmtime(path) > stale_s
    except OSError:
        return False


def _break_lock(path):
    """Rename a stale lock aside (only one breaker wins), then delete it."""
    aside = f"{path}.stale.{secrets.token_hex(4)}"
    try:
        os.rename(path, aside)
    except OSError:
        return False
    wio.remove_if_exists(aside)
    return True


def acquire_lock(path, owner="py", wait_s=LOCK_WAIT_S, stale_s=LOCK_STALE_S, now=time.time):
    """Exclusive-create `<path>` (a `.lock` file). Waits with backoff up to wait_s, then raises LockBusy."""
    token = secrets.token_hex(8)
    deadline = time.monotonic() + wait_s
    delay = 0.025
    while True:
        t = iso(now())
        body = serialize({"schema": "waypoint.lock.v1", "owner": owner, "pid": os.getpid(), "host": socket.gethostname(),
                          "token": token, "acquired_at": t, "heartbeat_at": t, "at": t})
        try:
            wio.create_exclusive(path, body, writer=owner)
            return Lock(path, token)
        except FileExistsError:
            if _lock_is_stale(path, stale_s, now):
                _break_lock(path)
                continue
        if time.monotonic() >= deadline:
            raise LockBusy(f"{path} is held")
        time.sleep(min(delay, 0.5) + random.uniform(0, 0.01))
        delay *= 2


# ------------------------------------------------------------------ store

_VOLATILE = ("rev", "updated_at", "updated_by")


class Store:
    """Reads and writes entity files under `root`. `writer` is the writer id (mod, web, runner, cloud-<run>)."""

    def __init__(self, root, writer=None, now=time.time, torn_retry_s=wio.TORN_WAIT_S):
        self.root = os.path.abspath(root)
        self.writer = writer
        self.now = now
        self.torn_retry_s = torn_retry_s

    serialize = staticmethod(serialize)

    # -- paths
    def path(self, rel):
        parts = [p for p in str(rel).replace("\\", "/").split("/") if p not in ("", ".")]
        if not parts or ".." in parts or (len(parts[0]) == 2 and parts[0][1] == ":"):
            raise ValueError(f"bad relative path: {rel}")
        return os.path.join(self.root, *parts)

    # -- reads
    def read(self, rel):
        """Parsed entity, or None if the file does not exist. Retries a torn read, then raises ReadTorn."""
        return wio.read_json(self.path(rel), torn_wait_s=self.torn_retry_s)

    def exists(self, rel):
        return os.path.isfile(self.path(rel))

    def list_names(self, rel_dir):
        return wio.list_names(self.path(rel_dir))

    def list_entities(self, rel_dir):
        out = []
        for n in self.list_names(rel_dir):
            if n.endswith(".json"):
                e = self.read(f"{rel_dir}/{n}")
                if e is not None:
                    out.append(e)
        return out

    # -- writes
    def _next(self, entity, rev):
        new = dict(entity)
        new["rev"] = rev
        if self.writer:
            new["updated_at"] = iso(self.now())
            new["updated_by"] = self.writer
        return new

    @staticmethod
    def _same(a, b):
        strip = lambda e: {k: v for k, v in e.items() if k not in _VOLATILE}  # noqa: E731
        return strip(a) == strip(b)

    def write(self, rel, entity, expected_rev=None):
        """Write one whole entity. expected_rev=0 creates (exclusive), N>0 is compare-and-swap, None bumps blindly.

        Returns the stored entity. Raises RevConflict (carrying the current entity) when the file is not at expected_rev.
        """
        target = self.path(rel)
        creating = expected_rev == 0
        if expected_rev is None and not os.path.exists(target):
            creating = True
        if creating:
            new = self._next(entity, 1)
            text = serialize(new)
            try:
                wio.create_exclusive(target, text, writer=self.writer or "py")
                return new
            except FileExistsError:
                if expected_rev == 0:
                    raise RevConflict(f"{rel} already exists", self.read(rel))
        lock = acquire_lock(target + ".lock", owner=self.writer or "py", now=self.now)
        try:
            cur = self.read(rel)
            if cur is None:
                if expected_rev not in (None,):
                    raise RevConflict(f"{rel} does not exist", None)
                cur_rev = 0
            else:
                cur_rev = cur.get("rev", 0) if isinstance(cur, dict) else 0
                if expected_rev is not None and cur_rev != expected_rev:
                    raise RevConflict(f"{rel} is at rev {cur_rev}, expected {expected_rev}", cur)
                if isinstance(cur, dict) and isinstance(entity, dict) and self._same(cur, entity) and expected_rev is not None:
                    return cur
            new = self._next(entity, cur_rev + 1)
            wio.atomic_write(target, serialize(new), writer=self.writer or "py")
            return new
        finally:
            lock.release()

    # -- logs
    def append_event(self, writer, event, day=None):
        """Append one event to events/<writer>/<day>.ndjson (this writer's own file). `seq` is assigned here."""
        day = day or _dt.datetime.fromtimestamp(self.now(), _dt.timezone.utc).strftime("%Y-%m-%d")
        path = self.path(f"events/{writer}/{day}.ndjson")
        try:
            with open(path, "rb") as f:
                seq = sum(1 for _ in f) + 1
        except FileNotFoundError:
            seq = 1
        line = {"seq": seq, "at": iso(self.now()), "kind": event["kind"], "actor": writer,
                "summary": event.get("summary", ""), "data": event.get("data")}
        for k in ("project", "node"):
            if event.get(k) is not None:
                line[k] = event[k]
        wio.append_line(path, json.dumps(line, ensure_ascii=False))
        return line

    def read_log(self, rel):
        """Parsed lines of an NDJSON log. A last line without newline or with a parse error is skipped."""
        text = wio.read_text(self.path(rel)) or ""
        lines = text.split("\n")
        out = []
        for i, ln in enumerate(lines):
            if not ln.strip():
                continue
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                if i != len(lines) - 1 and not (i == len(lines) - 2 and lines[-1] == ""):
                    continue
        return out

    # -- sync hygiene
    def quarantine_conflicts(self, rel_dir):
        """Move conflicted copies to _conflicts/<path.with.dots>.<time>, log sync.conflict. Never merges or deletes."""
        d = self.path(rel_dir)
        moved = []
        try:
            names = sorted(os.listdir(d))
        except FileNotFoundError:
            return moved
        stamp = _dt.datetime.fromtimestamp(self.now(), _dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
        for n in names:
            src = os.path.join(d, n)
            if not wio.is_conflicted_copy_name(n) or not os.path.isfile(src):
                continue
            m = wio.CONFLICT_RE.match(n)
            live_rel = f"{rel_dir}/{m.group(1)}{m.group(4)}".replace("\\", "/").strip("/")
            rel = f"{rel_dir}/{n}".replace("\\", "/").strip("/")
            copy_rev = live_rev = None
            try:
                c = wio.read_json(src, torn_wait_s=0.01, retries=1)
                copy_rev = c.get("rev") if isinstance(c, dict) else None
            except (ReadTorn, WriteLocked):
                pass
            try:
                lv = self.read(live_rel)
                live_rev = lv.get("rev") if isinstance(lv, dict) else None
            except (ReadTorn, WriteLocked):
                pass
            dest = self.path(f"_conflicts/{rel.replace('/', '.')}.{stamp}")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            i = 0
            final = dest
            while os.path.exists(final):
                i += 1
                final = f"{dest}.{i}"
            os.replace(src, final)
            higher = isinstance(copy_rev, int) and (live_rev is None or (isinstance(live_rev, int) and copy_rev > live_rev))
            self.append_event(self.writer or "py", {
                "kind": "sync.conflict", "summary": f"Conflicted copy of {live_rel} quarantined.",
                "data": {"original": rel, "live": live_rel, "copy_rev": copy_rev, "live_rev": live_rev,
                         "copy_mtime": iso(os.path.getmtime(final)), "higher_rev": bool(higher)}})
            moved.append(os.path.relpath(final, self.root).replace("\\", "/"))
        return moved

    def clean_stale_temps(self, rel_dir, older_than_s=wio.STALE_TEMP_S):
        """Remove this host's temp files older than 10 minutes whose writer pid is not running. Returns names."""
        d = self.path(rel_dir)
        removed = []
        try:
            names = os.listdir(d)
        except FileNotFoundError:
            return removed
        for n in names:
            if not (n.startswith(".") and n.endswith(".tmp")):
                continue
            p = os.path.join(d, n)
            try:
                if self.now() - os.path.getmtime(p) <= older_than_s:
                    continue
            except OSError:
                continue
            parts = n[1:-4].rsplit(".", 2)  # <name>.<writer>.<pid>.<random8>
            pid = int(parts[-2]) if len(parts) == 3 and parts[-2].isdigit() else None
            if pid is not None and pid != os.getpid() and _pid_alive(pid) and os.name != "nt":
                continue
            if wio.remove_if_exists(p):
                removed.append(n)
        return removed
