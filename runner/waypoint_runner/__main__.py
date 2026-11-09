"""python -m waypoint_runner [--once] [--folder DIR] [--grace S] [--interval S]"""
from __future__ import annotations

import argparse
import sys
import time

from . import lifecycle


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="waypoint_runner", description="Waypoint runner: runs until idle, then exits.")
    p.add_argument("--folder", help="data folder (default: WAYPOINT_DATA_DIR, then ~/.waypoint/data-dir pointer)")
    p.add_argument("--once", action="store_true", help="run one tick and exit")
    p.add_argument("--grace", type=float, default=lifecycle.DEFAULT_GRACE_S,
                   help="seconds of continuous idleness before exit")
    p.add_argument("--interval", type=float, default=lifecycle.DEFAULT_INTERVAL_S, help="seconds between ticks")
    return p


class LiveTick:
    """The real tick (actions, answers, runs, jobs, schedules, dispatch) plus the stale-tag sweep once a minute.

    ``python -m waypoint_runner`` is what the CLI and the mod start on demand; until 8 Nov 2026 it wired
    ``lifecycle.noop_tick``, so queued actions were never applied (waypoint-v6/auto-live-updates)."""

    STALE_EVERY_S = 60.0

    def __init__(self, folder, clock=time.time):
        from .tick import Tick
        self.folder = folder
        self.clock = clock
        self.tick = Tick(folder)
        self._stale_at = None

    def __call__(self, source=None):
        out = self.tick()
        now = self.clock()
        if self._stale_at is None or now - self._stale_at >= self.STALE_EVERY_S:
            self._stale_at = now
            try:
                from . import stale
                stale.sweep(self.folder)
            except Exception as exc:  # noqa: BLE001 - the sweep never stops the runner
                print(f"waypoint_runner: stale sweep failed: {exc!r}", file=sys.stderr)
        return out


def main(argv=None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:
        return lifecycle.EXIT_USAGE if e.code else 0
    if args.grace < 0 or args.interval <= 0:
        print("waypoint_runner: --grace must be >= 0 and --interval > 0", file=sys.stderr)
        return lifecycle.EXIT_USAGE
    folder = args.folder or lifecycle.default_data_dir()
    source = lifecycle.FolderWorkSource(folder, horizon_s=args.grace)
    runner = lifecycle.Runner(folder, source, LiveTick(folder), grace_s=args.grace, interval_s=args.interval,
                              once=args.once, log=lambda m: print(f"waypoint_runner: {m}", file=sys.stderr))
    code = runner.run()
    if code == lifecycle.EXIT_BUSY:
        print("waypoint_runner: runner already running (busy)")
    return code


if __name__ == "__main__":
    sys.exit(main())
