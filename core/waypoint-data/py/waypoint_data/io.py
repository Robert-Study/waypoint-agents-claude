"""File primitives for the Waypoint data folder (docs/data/CONCURRENCY.md). Stdlib only.

Atomic write (temp file in the same folder, fsync, rename with the Windows retry loop), exclusive create,
append, tolerant JSON read, and the one rule for which names are entities.
"""
import errno
import json
import os
import random
import re
import secrets
import time

MAX_BYTES = 1024 * 1024  # 1 MiB entity cap
RENAME_BACKOFF_MS = (10, 20, 40, 80, 160, 250, 250, 250)  # 8 retries, plus up to 20 ms jitter
TORN_RETRIES = 5
TORN_WAIT_S = 0.05
STALE_TEMP_S = 600


class WaypointIOError(Exception):
    code = "io_error"


class WriteLocked(WaypointIOError):
    code = "write_locked"


class ReadTorn(WaypointIOError):
    code = "read_torn"


class FileTooLarge(WaypointIOError):
    code = "file_too_large"


class LockBusy(WaypointIOError):
    code = "lock_busy"


def _sleep(seconds):  # indirection so tests can run the retry loop instantly
    time.sleep(seconds)


# ------------------------------------------------------------------ names

# Conflicted copies made by Drive: `name (1).json`, `name - Copy.json`, `name (conflicted copy host date).json`.
CONFLICT_RE = re.compile(r"^(.+?)( \(\d+\)| - Copy( \(\d+\))?| \([^)]*conflict[^)]*\))(\.[A-Za-z0-9]+)$", re.IGNORECASE)
IGNORED_SUFFIXES = (".tmp", ".lock", ".partial")


def is_conflicted_copy_name(name):
    m = CONFLICT_RE.match(name)
    return bool(m) and not (m.group(1) + m.group(4)).startswith(".")


def is_ignored_name(name):
    """True for names that are never entities: dotfiles, temp, lock, partial, stale and conflicted copies."""
    n = name.lower()
    return (
        name.startswith(".")
        or n.endswith(IGNORED_SUFFIXES)
        or ".stale." in n
        or bool(CONFLICT_RE.match(name))
    )


def list_names(directory):
    """Sorted entity file names in a folder; [] if the folder does not exist. Folders are not entities."""
    try:
        names = os.listdir(directory)
    except (FileNotFoundError, NotADirectoryError):
        return []
    return sorted(n for n in names if not is_ignored_name(n) and os.path.isfile(os.path.join(directory, n)))


# ------------------------------------------------------------------ writes

def temp_name(target, writer):
    d, base = os.path.split(target)
    return os.path.join(d, f".{base}.{writer or 'py'}.{os.getpid()}.{secrets.token_hex(4)}.tmp")


def _fsync_dir(directory):
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return  # Windows cannot open a folder: best effort only
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_temp(target, data, writer):
    if isinstance(data, str):
        data = data.encode("utf-8")
    if len(data) > MAX_BYTES:
        raise FileTooLarge(f"{len(data)} bytes is over the {MAX_BYTES} byte entity limit")
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    tmp = temp_name(target, writer)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o644)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        _silent_remove(tmp)
        raise
    return tmp


def _silent_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def atomic_write(target, data, writer="py"):
    """Replace `target` with `data` (str or bytes, UTF-8, caller supplies \\n line ends). Never partial.

    Rename over an existing file retries on EPERM/EACCES/EBUSY (Windows readers, antivirus, Drive), then WriteLocked.
    """
    tmp = _write_temp(target, data, writer)
    for attempt in range(len(RENAME_BACKOFF_MS) + 1):
        try:
            os.replace(tmp, target)
            _fsync_dir(os.path.dirname(target) or ".")
            return
        except PermissionError:
            pass
        except OSError as e:
            if e.errno not in (errno.EACCES, errno.EPERM, errno.EBUSY):
                _silent_remove(tmp)
                raise
        if attempt == len(RENAME_BACKOFF_MS):
            break
        _sleep((RENAME_BACKOFF_MS[attempt] + random.uniform(0, 20)) / 1000.0)
    _silent_remove(tmp)
    raise WriteLocked(f"could not replace {target}")


def create_exclusive(target, data, writer="py"):
    """Create `target` with complete content, or fail with FileExistsError. Never overwrites.

    Content is written to a temp file first and the final name appears by hard link (all-or-nothing), so a reader
    never sees an empty file. Where hard links are not supported it falls back to exclusive create.
    """
    tmp = _write_temp(target, data, writer)
    try:
        try:
            os.link(tmp, target)
            return
        except FileExistsError:
            raise
        except OSError as e:
            if os.path.exists(target):
                raise FileExistsError(errno.EEXIST, "exists", target) from e
        payload = data.encode("utf-8") if isinstance(data, str) else data
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o644)
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
    finally:
        _silent_remove(tmp)


def append_line(path, line):
    """Append one NDJSON line (no embedded newline, at most 16 KiB) to a single-writer log file."""
    if "\n" in line:
        raise ValueError("a log line must not contain a newline")
    data = (line + "\n").encode("utf-8")
    if len(data) > 16 * 1024:
        raise FileTooLarge("log line over 16 KiB")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def remove_if_exists(path):
    """Delete a file. On Windows a reader holding it open blocks the delete: retry like the rename, then WriteLocked."""
    for attempt in range(len(RENAME_BACKOFF_MS) + 1):
        try:
            os.remove(path)
            return True
        except FileNotFoundError:
            return False
        except PermissionError:
            if attempt == len(RENAME_BACKOFF_MS):
                raise WriteLocked(f"could not delete {path}") from None
            _sleep((RENAME_BACKOFF_MS[attempt] + random.uniform(0, 20)) / 1000.0)


# ------------------------------------------------------------------ reads

def read_text(path):
    """Text of a file, or None when it does not exist."""
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8-sig")
    except FileNotFoundError:
        return None


def read_json(path, torn_wait_s=TORN_WAIT_S, retries=TORN_RETRIES):
    """Parsed JSON of a file, or None when missing. A torn or empty read is retried, then ReadTorn."""
    for attempt in range(retries + 1):
        try:
            text = read_text(path)
            if text is None:
                return None
            return json.loads(text)
        except (json.JSONDecodeError, UnicodeDecodeError):
            if attempt == retries:
                break
            _sleep(torn_wait_s)
        except PermissionError:
            if attempt == retries:
                raise WriteLocked(f"cannot read {path}")
            _sleep(torn_wait_s)
    raise ReadTorn(f"{path} is torn or not valid JSON")
