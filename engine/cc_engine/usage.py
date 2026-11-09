"""Per-pool usage, served from a cache that a background refresher keeps up to date.

Sources (all local, read-only, zero model calls; see engine/proposals/usage_sources.py):

* Claude Max  ``claude_5h``/``claude_week``: ``claude -p /usage --output-format json
  --no-session-persistence`` (about 43 s, so every 10 min). The JSON envelope is re-checked
  on every call; any model usage makes the rows UNKNOWN.
* Codex       ``codex_5h``/``codex_week``: the ``rate_limits`` block of the newest native
  ``~/.codex/sessions`` rollout JSONL (passive, as fresh as Codex's last turn; every 2 min).
* Antigravity: ``agy --print /usage`` run by us (every 5 min), but only when ``agy`` is signed
  in (detected from its local token file; a sign-in is never triggered); signed out -> UNKNOWN.
  A row that prints ``disabled`` (or anything else that is not ``N%``) is UNKNOWN on its own.
* Gemini app: no source, always UNKNOWN (manual).

``read_usage()`` only reads ``data/usage_cache.json``; it never runs a slow source, never
touches the store (so never bumps its revision) and never invents numbers. Status is
computed at read time from the observation age: FRESH when age <= 2 x the source's refresh
interval, STALE when older (or upstream says STALE, or the reset has passed), UNKNOWN when
there is no verified number. ``start_refresher()`` is called from ``api.serve`` only.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

CLAUDE_TIMEOUT = 90
AGY_PRINT_TIMEOUT = "25s"  # agy's own limit for the query
AGY_HARD_TIMEOUT = 35     # backstop: our child tree is killed after this
_HOME = Path(os.environ.get("USERPROFILE", str(Path.home())))
CODEX_HOME = _HOME / ".codex"
AGY_TOKEN = _HOME / ".gemini" / "jetski-standalone-oauth-token"
AGY_EXE = Path(config.AGY_EXE)
CACHE_SCHEMA = "usage_cache.v1"
CLOCK_PREFIXES = ("codex_", "claude_")

# source -> refresh interval (s), FRESH window (s; default 2 x interval) and owned pools.
CLAUDE_POOLS = ("claude_5h", "claude_week")
CODEX_POOLS = ("codex_5h", "codex_week")
AGY_POOLS = ("antigravity_week", "antigravity_gemini_5h", "antigravity_claude_week",
             "antigravity_claude_5h", "antigravity_argon_week")
SOURCES = {
    "claude": {"interval": 600, "pools": CLAUDE_POOLS},
    "codex": {"interval": 120, "pools": CODEX_POOLS},
    "antigravity": {"interval": 300, "pools": AGY_POOLS},
}
LABELS = {
    "claude_5h": "Claude 5 hours", "claude_week": "Claude week (all models)",
    "codex_5h": "Codex 5 hours", "codex_week": "Codex week",
    "antigravity_week": "Antigravity Gemini week", "antigravity_gemini_5h": "Antigravity Gemini 5 hours",
    "antigravity_claude_week": "Antigravity Claude/GPT week",
    "antigravity_claude_5h": "Antigravity Claude/GPT 5 hours",
    "antigravity_argon_week": "Antigravity Gemini 4 Argon week", "gemini_app": "Gemini app",
}
POOL_ORDER = CLAUDE_POOLS + CODEX_POOLS + AGY_POOLS + ("gemini_app",)


# ---------------------------------------------------------------- helpers
def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def _parse_time(value):
    """ISO text or epoch seconds -> aware UTC datetime, else None."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if value <= 0:
            return None
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _pct(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value), 1) if 0 <= value <= 100 else None


def obs(pool, remaining, observed, resets, source, upstream=None):
    """One cached observation. A number is kept only together with its observation time."""
    remaining = _pct(remaining)
    if observed is None:
        remaining = None
    return {"pool": pool, "remaining_percent": remaining, "observed_at": _iso(observed),
            "resets_at": _iso(resets), "source": source, "upstream_status": upstream}


def unknown_obs(pool, source):
    return obs(pool, None, None, None, source)


# ---------------------------------------------------------------- Claude Max
CLAUDE_CMD = ["claude", "-p", "/usage", "--output-format", "json", "--no-session-persistence"]
_RESET = r"resets\s+(?:([A-Z][a-z]{2} \d{1,2}),?\s+)?(\d{1,2}(?::\d{2})?\s*[ap]m)\s*\(([^)]+)\)"
_CLAUDE_LINES = (
    ("claude_5h", re.compile(r"Current session:?\s*(\d{1,3})%\s*used" + r"(?:\W+" + _RESET + ")?")),
    ("claude_week", re.compile(r"Current week \(all models\):?\s*(\d{1,3})%\s*used" + r"(?:\W+" + _RESET + ")?")),
)


_UTC_NAMES = {"utc", "gmt", "z", "etc/utc", "etc/gmt", "etc/universal", "universal", "zulu"}


def _zone(name):
    """A tzinfo for ``name``, or None. UTC needs no tz database. Named zones (Europe/London) need one, which
    Windows lacks unless the optional ``tzdata`` package is installed; then the reset time is unknown."""
    name = name.strip()
    if name.lower() in _UTC_NAMES:
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return None


def _local_reset(day, clock, zone, at):
    """'Nov 4' + '2:19pm' in ``zone`` -> UTC. No day means the next such time after ``at``."""
    tz = _zone(zone)
    if tz is None:
        return None
    clock = clock.replace(" ", "")
    tfmt = "%I:%M%p" if ":" in clock else "%I%p"
    local_at = at.astimezone(tz)
    try:
        if day:
            local = datetime.strptime(f"{local_at.year} {day} {clock}", "%Y %b %d " + tfmt).replace(tzinfo=tz)
            if local < local_at - timedelta(days=1):  # Dec -> Jan roll-over
                local = local.replace(year=local.year + 1)
        else:
            t = datetime.strptime(clock, tfmt)
            local = local_at.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
            if local <= local_at:
                local += timedelta(days=1)
    except ValueError:
        return None
    return local.astimezone(timezone.utc)


def _zero_model_usage(envelope):
    """True only if the CLI envelope proves no model call was made."""
    u = envelope.get("usage") or {}
    try:
        tokens = sum(int(u.get(k) or 0) for k in ("input_tokens", "output_tokens",
                                                   "cache_creation_input_tokens", "cache_read_input_tokens"))
    except (TypeError, ValueError):
        return False
    return (envelope.get("num_turns") == 0 and not envelope.get("total_cost_usd")
            and tokens == 0 and not envelope.get("modelUsage"))


def parse_claude_usage(envelope, at, source="claude -p /usage"):
    if not isinstance(envelope, dict) or envelope.get("is_error"):
        return [unknown_obs(p, source + ": CLI error") for p in CLAUDE_POOLS]
    if not _zero_model_usage(envelope):
        return [unknown_obs(p, source + ": refused, envelope shows model usage") for p in CLAUDE_POOLS]
    text = str(envelope.get("result") or "")
    out = []
    for pool, pattern in _CLAUDE_LINES:
        m = pattern.search(text)
        if not m:
            out.append(unknown_obs(pool, source + ": line not found"))
            continue
        used = int(m.group(1))
        resets = _local_reset(m.group(2), m.group(3), m.group(4), at) if m.group(3) else None
        out.append(obs(pool, 100 - used if used <= 100 else None, at, resets, source))
    return out


def _run_tree(argv, timeout, stdin=subprocess.DEVNULL, stdin_text=None):
    """Run ``argv`` and return ``(exit code, stdout)``; on timeout kill its whole process tree.

    ``subprocess.run(timeout=)`` kills only the direct child. Through ``claude.cmd`` that is
    cmd.exe: the real claude.exe lives on, keeps the stdout pipe open, and run() then waits for it
    forever. On 5 Nov 2026 that froze the engine's Claude usage thread for good (a 14:09 probe was
    still alive at 14:35), so Claude usage went STALE and every Claude sprint would have held.
    """
    proc = subprocess.Popen(list(argv), stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, encoding="utf-8", errors="replace", cwd=tempfile.gettempdir(),
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        out, _ = proc.communicate(stdin_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            proc.kill()
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:  # a grandchild outlived the tree kill: stop waiting on it
            for pipe in (getattr(proc, "stdout", None), getattr(proc, "stdin", None)):
                if pipe:
                    pipe.close()
        raise
    return proc.returncode, out


def _default_claude_runner(cmd, timeout=CLAUDE_TIMEOUT):
    exe = cmd[0]
    if os.name == "nt":
        exe = shutil.which("claude.exe") or shutil.which("claude.cmd") or shutil.which("claude") or exe
    # stdin must be closed: `claude -p` otherwise waits for piped input.
    return _run_tree([exe] + list(cmd[1:]), timeout)


def claude_source(runner=None, now=None):
    """Run the local /usage slash command via ``runner(cmd) -> (exit_code, stdout)``."""
    try:
        code, out = (runner or _default_claude_runner)(list(CLAUDE_CMD))
        at = now or _now()  # observed when the CLI answered
        if code != 0:
            raise RuntimeError(f"exit {code}")
        return parse_claude_usage(json.loads(out), at)
    except Exception as exc:
        why = f"claude -p /usage failed ({type(exc).__name__}: {str(exc)[:80]})"
        return [unknown_obs(p, why) for p in CLAUDE_POOLS]


# ---------------------------------------------------------------- Codex
CODEX_SOURCE = "codex rollout rate_limits"
CODEX_WINDOWS = (("codex_5h", "primary", 300), ("codex_week", "secondary", 10080))


def _last_rate_limits(path, tail_bytes):
    """Last non-null ``rate_limits`` event in one rollout file, as ``(event, rate_limits)`` or None."""
    size = path.stat().st_size
    with open(path, "rb") as fh:
        fh.seek(max(0, size - tail_bytes))
        lines = fh.read().decode("utf-8", "replace").splitlines()
    for line in reversed(lines):
        if '"rate_limits"' not in line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        rl = (ev.get("payload") or {}).get("rate_limits") if isinstance(ev, dict) else None
        if isinstance(rl, dict) and (rl.get("primary") or rl.get("secondary")):
            return ev, rl
    return None


def codex_source(codex_home=CODEX_HOME, tail_bytes=262144, max_files=8):
    """Passive and read-only: the last non-null ``rate_limits`` in the newest native Codex rollout.

    A newest file that has no limits yet (a session that has not answered) is skipped for the
    next newest, up to ``max_files``. Nothing found -> UNKNOWN, never a guess.
    """
    try:
        files = sorted(Path(codex_home, "sessions").rglob("*.jsonl"), key=lambda f: f.stat().st_mtime,
                       reverse=True)
        if not files:
            raise FileNotFoundError("no rollouts")
        hit = None
        for f in files[:max_files]:
            try:
                hit = _last_rate_limits(f, tail_bytes)
            except OSError:
                continue
            if hit:
                break
        if not hit:
            raise LookupError("no non-null rate_limits")
    except Exception as exc:
        return [unknown_obs(p, f"{CODEX_SOURCE}: {type(exc).__name__}") for p in CODEX_POOLS]
    ev, rl = hit
    observed = _parse_time(ev.get("timestamp"))
    out = []
    for pool, key, mins in CODEX_WINDOWS:
        w = rl.get(key) or {}
        used = _pct(w.get("used_percent")) if w.get("window_minutes") == mins else None
        out.append(obs(pool, None if used is None else 100 - used, observed, _parse_time(w.get("resets_at")),
                       CODEX_SOURCE))
    return out


# ---------------------------------------------------------------- Antigravity / Gemini
_AGY = {("Gemini Models", "Weekly Limit Remaining"): "antigravity_week",
        ("Gemini Models", "Five Hour Limit Remaining"): "antigravity_gemini_5h",
        ("Claude and GPT models", "Weekly Limit Remaining"): "antigravity_claude_week",
        ("Claude and GPT models", "Five Hour Limit Remaining"): "antigravity_claude_5h",
        ("Gemini 4 Argon", "Gemini 4 Argon Weekly Limit"): "antigravity_argon_week"}
AGY_SOURCE = "agy --print /usage"
_AGY_SIGNED_OUT = re.compile(r"(?i)sign[ -]?in|log[ -]?in|logged in|authenticat|oauth|unauthori[sz]ed")


def agy_signed_in(token_path=AGY_TOKEN) -> bool:
    """True only if agy's local token file holds a refresh token. Returns no secret."""
    try:
        data = json.loads(Path(token_path).read_text(encoding="utf-8-sig"))
        token = data.get("token") if isinstance(data, dict) else None
        refresh = token.get("refresh_token") if isinstance(token, dict) else None
        return isinstance(refresh, str) and bool(refresh.strip())
    except Exception:
        return False


def _agy_unknown(why):
    return [unknown_obs(pid, f"{AGY_SOURCE}: {why}") for pid in AGY_POOLS]


def parse_agy_report(text, observed):
    """Parse the five-row TSV row by row; a row that is not ``N%`` is UNKNOWN on its own."""
    found = {}
    for line in str(text).splitlines():
        f = [c.strip() for c in line.split("\t")]
        pool = _AGY.get(tuple(f[:2])) if len(f) >= 3 else None
        if not pool:
            continue
        m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*%", f[2])
        reset = _parse_time(f[3]) if len(f) > 3 else None
        found[pool] = obs(pool, float(m.group(1)) if m else None, observed, reset,
                          AGY_SOURCE if m else f"{AGY_SOURCE}: '{f[2][:20]}'")
    return found


def _default_agy_runner(cmd, timeout=AGY_HARD_TIMEOUT):
    """Run agy with an empty, closed stdin (agy hangs on NUL/inherited stdin); kill our child tree on timeout."""
    return _run_tree(cmd, timeout, stdin=subprocess.PIPE, stdin_text="")


def agy_source(runner=None, token_path=AGY_TOKEN, exe=None, now=None):
    """Run ``agy --print /usage`` ourselves, but only when agy is already signed in.

    Signed out (or output that asks for a sign-in) -> every pool UNKNOWN 'signed out'. The CLI is
    never started when the token file says signed out, so no sign-in can be triggered. 25 s limit.
    """
    if not agy_signed_in(token_path):
        return _agy_unknown("signed out")
    exe = str(exe or shutil.which("agy") or AGY_EXE)
    if not (Path(exe).is_file() or shutil.which(exe)):
        return _agy_unknown("not installed")
    try:
        code, out = (runner or _default_agy_runner)([exe, "--print", "/usage", "--print-timeout", AGY_PRINT_TIMEOUT],
                          AGY_HARD_TIMEOUT)
        observed = now or _now()  # observed when the CLI answered
        if code != 0:
            raise RuntimeError(f"exit {code}")
    except Exception as exc:
        return _agy_unknown(f"failed ({type(exc).__name__}: {str(exc)[:80]})")
    found = parse_agy_report(out, observed)
    if not found:
        return _agy_unknown("signed out" if _AGY_SIGNED_OUT.search(str(out)) else "no usage rows in output")
    return [found.get(pid) or unknown_obs(pid, f"{AGY_SOURCE}: row missing") for pid in AGY_POOLS]


GEMINI_APP_SOURCE = "no local or official usage source for the Gemini app (manual)"


# ---------------------------------------------------------------- cache
def default_cache_path() -> Path:
    return config.data_dir() / "usage_cache.json"


def load_cache(path=None) -> dict:
    try:
        data = json.loads(Path(path or default_cache_path()).read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("sources"), dict):
            return data
    except Exception:
        pass
    return {"schema": CACHE_SCHEMA, "sources": {}}


_WRITE_LOCK = threading.Lock()


def _write_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    for attempt in range(5):  # Windows: a reader holding the file briefly blocks os.replace
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.05 * (attempt + 1))


def store_source(path, name, rows, refreshed_at, error=None):
    """Merge one source's fresh rows into the cache file.

    A verified older number is kept (it ages into STALE) when the new reading has none;
    the failure is recorded beside it, never turned into a number.
    """
    with _WRITE_LOCK:
        cache = load_cache(path)
        prev = {r.get("pool"): r for r in (cache["sources"].get(name) or {}).get("rows", [])
                if isinstance(r, dict)}
        merged = []
        for r in rows:
            old = prev.get(r["pool"])
            if r["remaining_percent"] is None and old and old.get("remaining_percent") is not None:
                kept = dict(old)
                kept["last_error"] = r["source"]
                merged.append(kept)
            else:
                merged.append(dict(r))
        cache["schema"] = CACHE_SCHEMA
        cache["sources"][name] = {"refreshed_at": _iso(refreshed_at), "error": error, "rows": merged}
        _write_atomic(path, cache)


MIN_REST = 60.0  # seconds a source rests after each run
SESSION_WINDOWS = {"five_hour": "claude_5h", "seven_day": "claude_week"}
SESSION_SOURCE = "Claude session usage (mod)"


def record_session_rate_limits(limits, at=None, cache_path=None) -> int:
    """Store Claude's rate-limit windows as a session reported them (``$.session.usage().rateLimits``:
    ``[{kind, percentUsed, resetsAt}]``), posted by the mod at the end of a turn. Zero cost, fresher than
    ``claude -p /usage``; the refresher then skips its own run until this reading ages past its interval.
    Returns the number of pools stored. Unknown kinds and bad numbers are ignored."""
    now = _now()
    at = _parse_time(at) or now
    if at > now + timedelta(seconds=60):  # a future time would park the refresher and age the row oddly
        at = now
    rows = []
    for item in limits or ():
        if not isinstance(item, dict):
            continue
        pool = SESSION_WINDOWS.get(str(item.get("kind") or ""))
        used = item.get("percentUsed")
        if pool is None or isinstance(used, bool) or not isinstance(used, (int, float)) or not 0 <= used <= 100:
            continue
        rows.append(obs(pool, round(100 - used, 1), at, _parse_time(item.get("resetsAt")), SESSION_SOURCE))
    if not rows:
        return 0
    path = cache_path or default_cache_path()
    with _WRITE_LOCK:  # the window this reading lacks keeps its last row
        have = {r["pool"] for r in rows}
        prev = (load_cache(path)["sources"].get("claude") or {}).get("rows", [])
    keep = [dict(r) for r in prev if isinstance(r, dict) and r.get("pool") in CLAUDE_POOLS and r.get("pool") not in have]
    store_source(path, "claude", rows + keep, at)
    return len(rows)


def _status(o, fresh_after, now):
    remaining = o.get("remaining_percent")
    observed = _parse_time(o.get("observed_at"))
    resets = _parse_time(o.get("resets_at"))
    if remaining is None or observed is None:
        return "UNKNOWN", None, None
    age = int((now - observed).total_seconds())
    if (o.get("upstream_status") == "STALE" or age < -60 or age > fresh_after
            or (resets is not None and resets <= now)):
        return "STALE", remaining, age
    return "FRESH", remaining, age


def _row(o, status, remaining, age, refreshed_at=None):
    pool = o["pool"]
    out = {"pool": pool, "label": LABELS.get(pool, pool), "remaining_percent": remaining,
           "observed_at": o.get("observed_at") if status != "UNKNOWN" else None,
           "resets_at": o.get("resets_at"), "status": status, "source": o.get("source") or "unknown",
           "age_seconds": age, "upstream_status": o.get("upstream_status"), "refreshed_at": refreshed_at}
    if o.get("last_error"):
        out["last_error"] = o["last_error"]
    return out


def read_usage(cache_path=None, now=None) -> list:
    """``[{pool, label, remaining_percent|None, observed_at|None, resets_at|None,
    status: FRESH|STALE|UNKNOWN, source, age_seconds, ...}]`` from the cache file only."""
    now = now or _now()
    cache = load_cache(cache_path)
    rows, seen = [], set()
    for name, cfg in SOURCES.items():
        entry = cache["sources"].get(name) or {}
        fresh_after = cfg.get("fresh_after", 2 * cfg["interval"])
        for o in entry.get("rows", []):
            if not isinstance(o, dict) or not o.get("pool") or o["pool"] in seen:
                continue
            seen.add(o["pool"])
            rows.append(_row(o, *_status(o, fresh_after, now), refreshed_at=entry.get("refreshed_at")))
        for pool in cfg["pools"]:
            if pool not in seen:
                seen.add(pool)
                rows.append(_row(unknown_obs(pool, f"{name} usage not yet refreshed"), "UNKNOWN", None, None))
    rows.append(_row(unknown_obs("gemini_app", GEMINI_APP_SOURCE), "UNKNOWN", None, None))
    order = {p: i for i, p in enumerate(POOL_ORDER)}
    rows.sort(key=lambda r: (order.get(r["pool"], len(order)), r["pool"]))
    return rows


# ---------------------------------------------------------------- resets
def merged_resets(rows, now=None) -> list:
    """Upcoming resets across pools, soonest first: ``[{pool, resets_at}]``."""
    now = now or _now()
    out = {}
    for r in rows or []:
        t = _parse_time(r.get("resets_at"))
        if r.get("pool") and t and t > now:
            out[(r["pool"], t)] = {"pool": r["pool"], "resets_at": _iso(t)}
    return [v for _, v in sorted(out.items(), key=lambda kv: (kv[0][1], kv[0][0]))]


def reset_clock(rows, now=None, prefixes=CLOCK_PREFIXES) -> dict:
    """Merged Codex/Claude reset punch clock (used by ``/api/usage``)."""
    now = now or _now()
    upcoming = []
    status = {r.get("pool"): r.get("status") for r in rows or []}
    for m in merged_resets([r for r in rows or [] if str(r.get("pool", "")).startswith(tuple(prefixes))], now):
        t = _parse_time(m["resets_at"])
        upcoming.append(dict(m, status=status.get(m["pool"]), in_seconds=int((t - now).total_seconds())))
    return {"next": upcoming[0] if upcoming else None, "resets": upcoming}


# ---------------------------------------------------------------- background refresher
def default_fetchers():
    return {"claude": claude_source, "codex": codex_source, "antigravity": agy_source}


class UsageRefresher:
    """One daemon thread per source; each runs its fetcher then waits its interval.

    A source refreshed within its interval (per the cache file) is not re-run on start-up,
    so engine restarts do not re-launch the 43 s Claude CLI.
    """

    def __init__(self, cache_path=None, fetchers=None, intervals=None, clock=_now):
        self.cache_path = Path(cache_path or default_cache_path())
        self.fetchers = dict(fetchers if fetchers is not None else default_fetchers())
        self.intervals = {n: (intervals or {}).get(n, SOURCES.get(n, {}).get("interval", 600))
                          for n in self.fetchers}
        self.clock = clock
        self._stop = threading.Event()
        self.threads = []

    def refresh(self, name):
        """Run one source now and store its rows. Never raises."""
        try:
            rows = self.fetchers[name]()
            error = None
            if all(r.get("remaining_percent") is None for r in rows):
                error = "; ".join(sorted({r["source"] for r in rows}))[:300]
        except Exception as exc:  # a fetcher bug must not kill the thread or invent rows
            pools = SOURCES.get(name, {}).get("pools", ())
            error = f"{type(exc).__name__}: {str(exc)[:120]}"
            rows = [unknown_obs(p, f"{name} refresh failed ({error})") for p in pools]
        try:
            store_source(self.cache_path, name, rows, self.clock(), error)
        except Exception:
            pass
        return rows

    def first_delay(self, name):
        entry = load_cache(self.cache_path)["sources"].get(name) or {}
        last = _parse_time(entry.get("refreshed_at"))
        if last is None:
            return 0.0
        return max(0.0, self.intervals[name] - (self.clock() - last).total_seconds())

    def _loop(self, name):
        # The wait is re-read from the cache each time: a fresher reading pushed in by another writer (the
        # mod's ``$.session.usage`` for Claude) postpones the slow source instead of running it again.
        while not self._stop.is_set():
            delay = self.first_delay(name)
            if delay > 0:
                if self._stop.wait(delay):
                    return
                continue
            self.refresh(name)
            # Always rest after a run, whatever the cache says: an unwritable cache must not make the
            # slow source (claude -p /usage) run back to back.
            if self._stop.wait(min(self.intervals[name], MIN_REST)):
                return

    def start(self):
        for name in self.fetchers:
            t = threading.Thread(target=self._loop, args=(name,), name=f"usage-{name}", daemon=True)
            t.start()
            self.threads.append(t)
        return self

    def stop(self, timeout=1.0):
        self._stop.set()
        for t in self.threads:
            t.join(timeout)


def start_refresher(cache_path=None, **kwargs) -> UsageRefresher:
    """Start background usage refreshing (called from ``api.serve`` only, never in tests)."""
    return UsageRefresher(cache_path, **kwargs).start()
