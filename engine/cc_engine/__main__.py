"""CLI: ``python -m cc_engine serve [--port 8751] [--db PATH]``,
``python -m cc_engine import <plan.json> [--db PATH]``, ``python -m cc_engine tick ...``,
``python -m cc_engine claim --project P --node N --by WHO [--worktree W] [--cwd C] [--env E]`` and
``python -m cc_engine release --project P --node N [--by WHO]`` (see ``claims.py``) and
``python -m cc_engine snapshot [--db P] [--out DIR] [--project ID] [--resume FILE]`` (see ``state_snapshot.py``)."""
from __future__ import annotations

import argparse
import sys

from . import default_db_path


def _serve(argv):
    ap = argparse.ArgumentParser(prog="cc_engine serve")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8751)
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("refusing to bind a non-loopback address", file=sys.stderr)
        return 2
    from .api import serve
    from .store import Store

    store = Store(args.db or default_db_path())

    def cli_bridge():
        """Best effort: dsh's main chat reaches claude / codex through it (CC_CLI_BRIDGE=0 disables).
        Runs in the engine's warm-up thread, after the port is bound and /health answers."""
        try:
            from . import cli_bridge as bridge
            bridge.start_background()
        except Exception as exc:
            print(f"cli-bridge: not started: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)

    serve(store, args.host, args.port, background=[cli_bridge])
    return 0


def _import(argv):
    ap = argparse.ArgumentParser(prog="cc_engine import")
    ap.add_argument("plan")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)
    from .plan import import_plan
    from .store import Store

    store = Store(args.db or default_db_path())
    try:
        pid = import_plan(store, args.plan, actor="import")
        print(f"imported {pid}: {len(store.list_nodes(pid))} nodes, revision {store.revision()}")
    finally:
        store.close()
    return 0


def _tick(argv):
    try:
        from . import scheduler
    except ImportError as exc:
        print(f"scheduler unavailable: {exc}", file=sys.stderr)
        return 3
    return scheduler.main(argv) or 0


def _claim(argv):
    from .claims import cli_claim
    return cli_claim(argv)


def _release(argv):
    from .claims import cli_release
    return cli_release(argv)


def _snapshot(argv):
    from .state_snapshot import cli
    return cli(argv)


def _sync_import(argv):
    from .cloud_sync import cli
    return cli(argv)


COMMANDS = {"sync-import": _sync_import, "serve": _serve, "import": _import, "tick": _tick, "claim": _claim, "release": _release,
            "snapshot": _snapshot}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in COMMANDS:
        print("usage: python -m cc_engine {serve|import <plan.json>|tick|claim|release|snapshot|sync-import} [options]", file=sys.stderr)
        return 2
    return COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main())
