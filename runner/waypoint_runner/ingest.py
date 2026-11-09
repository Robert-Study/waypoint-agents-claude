"""Command-line entry point: ingest one receipt file into a data folder (Waypoint 1.2 integration).

    python -m waypoint_runner.ingest --data <folder> --receipt <file>

A thin wrapper over receipts_ingest.ingest_receipt. Prints one JSON object. Exit 0 when the receipt was ingested
(or was already), 1 when it was rejected as invalid, 2 on a bad argument.
"""
import argparse
import json
import sys
from pathlib import Path

from . import receipts_ingest as ri
from waypoint_data.store import Store


def main(argv=None):
    ap = argparse.ArgumentParser(prog="waypoint_runner.ingest")
    ap.add_argument("--data", required=True)
    ap.add_argument("--receipt", required=True)
    a = ap.parse_args(argv)
    path = Path(a.receipt)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        print(json.dumps({"ingested": False, "status": "unreadable", "reason": str(exc)}))
        return 2
    res = ri.ingest_receipt(Store(a.data, writer=ri.ACTOR), raw, path.name)
    ok = res["status"] in ("accepted", "duplicate")
    print(json.dumps({"ingested": ok, **res}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
