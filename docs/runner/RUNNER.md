# Waypoint runner: start on demand, exit when idle

The runner is not a service. Something starts it when there is work (the Run button through the mod, or the CLI),
it ticks while work exists, and it exits by itself when idle. Nothing needs to stay open.

## Start command (the one the mod, mod-10 and the CLI call)

```
<settings.python> -m waypoint_runner [--once] [--folder DIR] [--grace S] [--interval S]
```

- `settings.python` and `settings.runner_dir` (the `runner` folder put on `PYTHONPATH`) are seeded into
  `settings/settings.json` by the cutover (`--apply`), only for keys not already set; the mod and the CLI read them
  from there. No machine path is stored in shipped code. If `runner_dir` is unknown the mod does not start the
  runner (the queued action file stays safe). Prefer a full interpreter path over bare `python`, which can resolve
  to the Microsoft Store stub on Windows.
- Working directory or `PYTHONPATH` must make the `runner` folder importable (run from the repo's `runner` folder).
- `--folder DIR`: the data folder. Default: `WAYPOINT_DATA_DIR`, then the first line of `~/.waypoint/data-dir`,
  then `~/.waypoint/data` (the data-paths resolver).
- `--once`: take the lock, run exactly one tick, release, exit 0.
- `--grace S` (default 30): seconds of continuous idleness before exit. `--interval S` (default 2): seconds between ticks.

## What counts as work

Read from the data folder only, through `core/waypoint-data/py` (no direct file access, no web page, no board):

- ready nodes: status `ready` in a project that has an active run (`runs/*.json` status running/queued/starting/active);
- live jobs: status `running` nodes of those projects;
- pending scheduled runs: `schedules/*.json` with a `start` (HH:MM) due now or within the grace window whose
  `last_run.slot` is not that slot.

- answered questions not routed yet (`resume.py`): the runner routes each answer once, to the warm asking
  session or, when that session is cold (idle over `settings.resume_cold_after_minutes`, default 60), to a fresh
  worker on the node. See FILE_CONTRACT.md, "Answers go back to the work". The mod starts the runner after an answer.

Idle = none of these for `--grace` seconds in a row. Work that appears during the grace period restarts it.
Idle detection never depends on the web page being open.

## Lock file

`<data folder>/runner.lock.json`: `{pid, host, heartbeat_at, url|null}`. Created atomically (O_EXCL), refreshed every
tick, removed on exit. A lock whose PID is dead (same host) or whose heartbeat is older than 30 s is stale and is
recovered by the next start. See `runner/waypoint_runner/lock.py`.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Idle for the grace period (or `--once` finished). Normal. |
| 1 | Runner error (a tick raised). The lock is released. |
| 2 | Bad usage (unknown flag, bad `--grace`/`--interval`). |
| 3 | Busy: another runner holds the lock. Nothing was touched; callers treat this as success (a runner is already up). |
| 130 | Stopped by SIGINT/SIGTERM. The lock is released. |

## Rules

- No floor or percentage constant is in the runner. Floors are Waypoint settings; unset means 0.
- Tests use fakes, never real model calls or the live board: `python -m pytest runner/tests/test_lifecycle.py -q`
  (with the pinned interpreter).
- The tick is a pluggable callable; dispatch is wired in by later nodes. The lifecycle ships with a no-op tick.
