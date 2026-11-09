"""Command-line entry point: one cloud round trip over a data folder (Waypoint 1.2 integration).

    python -m waypoint_runner.cloud_sync --data <folder> --task <id> [--project P]

Hands the task off through cloud.drive_handoff into ``<data>/sync/outbox``, then imports whatever the cloud side left
in ``<data>/sync/inbox`` through actions_sync.sync_import. With WAYPOINT_OFFLINE=1 there is no cloud session to answer,
so a loopback writes the one ``waypoint.sync.v1`` reply a cloud session would push. Prints one JSON object
({"handoff": bool, "handback": bool, ...}); exit 0 when the handoff was made, 1 when it was not, 2 on a bad argument.
"""
import argparse
import json
import os
import sys
from pathlib import Path

from . import actions_sync
from . import receipts_ingest as ri
from .cloud.drive_handoff import DriveHandoff
from waypoint_data import io as wio
from waypoint_data.claims import Claims
from waypoint_data.store import Store


def _loopback_reply(inbox, task):
    """The reply a cloud session would push (docs/v5/cloud_sync_format.md), written once."""
    body = {"schema": actions_sync.SCHEMA, "session": f"loopback-{task}"[:64],
            "items": [{"kind": "todo_add", "text": f"Cloud task {task} handed back (offline loopback)"}]}
    os.makedirs(inbox, exist_ok=True)
    try:
        wio.create_exclusive(os.path.join(inbox, f"reply.{task}.json"), Store.serialize(body), writer="cloud-sync")
    except FileExistsError:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(prog="waypoint_runner.cloud_sync")
    ap.add_argument("--data", required=True)
    ap.add_argument("--task", required=True)
    ap.add_argument("--project", default="demo-v2")
    a = ap.parse_args(argv)
    data = Path(a.data)
    try:
        info = DriveHandoff(data / "sync" / "outbox", data).send(a.project, a.task, {"task": a.task}, run="cloud-sync")
    except ValueError as exc:
        print(json.dumps({"handoff": False, "handback": False, "reason": str(exc)}))
        return 2
    if not info:
        print(json.dumps({"handoff": False, "handback": False, "reason": info.reason}))
        return 1
    if os.environ.get("WAYPOINT_OFFLINE") == "1":
        _loopback_reply(str(data / "sync" / "inbox"), a.task)
    res = actions_sync.sync_import(Store(data, writer=ri.ACTOR))
    Claims(data, writer="cloud-sync").release(a.project, a.task, info.token)
    print(json.dumps({"handoff": True, "handback": res["files"] > 0 and not res["invalid"], "handoff_file": info.filename,
                      "import": {k: res[k] for k in ("files", "applied", "skipped", "conflicts", "invalid")}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
