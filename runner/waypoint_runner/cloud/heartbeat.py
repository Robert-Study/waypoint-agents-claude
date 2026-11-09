"""PC heartbeat and shutdown hand-off to the cloud (Waypoint 1.2, node cloud-heartbeat).

The PC writes heartbeats/<host>.json into the data folder on a timer. On shutdown or sleep, `hand_off_running` moves
every running node to the cloud: it writes location `cloud` and status `ready` (the cloud worker only picks ready
cloud nodes), then releases the PC claim by its token. It claims nothing and defines no second claim protocol: it
uses waypoint_data Claims (CONCURRENCY.md "Claims"; PROTOCOL.md "Reconcile").

Time limits: Windows gives a shutdown hook little time, so the hand-off has a budget (default 8 s). Nodes not reached
stay running with their claim; their heartbeat then goes stale and the cloud worker reclaims them by timeout
(staleness fallback, reported as `fallback: "staleness"`). A heartbeat file on Drive syncs late, so staleness is
judged with a tolerance on top of the claim window. Stdlib only, no network.
"""
import argparse
import json
import os
import socket
import sys
import time

try:
    import waypoint_data  # noqa: F401
except ImportError:  # running from a checkout: the library sits in core/waypoint-data/py
    _here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.abspath(os.path.join(_here, "..", "..", "..", "core", "waypoint-data", "py")))

from waypoint_data import io as wio  # noqa: E402
from waypoint_data.claims import STALE_S, Claims  # noqa: E402
from waypoint_data.store import RevConflict, Store, iso, parse_iso, serialize  # noqa: E402

SCHEMA = "waypoint.pc_heartbeat.v1"
TOLERANCE_S = 120  # Drive sync lag and clock skew allowance on top of the claim window
DEFAULT_BUDGET_S = 8.0
WRITER = "pc-heartbeat"


def default_host():
    return "".join(c if c.isalnum() or c in "._-" else "-" for c in socket.gethostname()) or "pc"


def _hb_path(root, host):
    return os.path.join(os.path.abspath(root), "heartbeats", f"{host}.json")


def stale_after_s():
    """Seconds after which a heartbeat counts as stale: the runner claim window plus the sync tolerance."""
    return STALE_S + TOLERANCE_S


# ------------------------------------------------------------------ heartbeat

def write_heartbeat(root, host=None, now=time.time, state="alive"):
    host = host or default_host()
    path = _hb_path(root, host)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = {"schema": SCHEMA, "host": host, "at": iso(now()), "state": state}
    wio.atomic_write(path, serialize(body), writer=WRITER)
    return body


def read_heartbeat(root, host=None):
    """The heartbeat body, or None when missing, torn or unreadable. Conflicted copies are never read."""
    try:
        body = wio.read_json(_hb_path(root, host or default_host()))
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def is_stale(root, host=None, now=time.time, stale_s=None, tolerance_s=TOLERANCE_S):
    """True when no usable heartbeat exists or it is older than window + tolerance. A timestamp slightly in the
    future (the PC clock ahead of the reader) is fresh; compare with tolerance, never exactly."""
    hb = read_heartbeat(root, host)
    at = parse_iso(hb.get("at")) if hb else None
    if at is None:
        return True
    window = STALE_S if stale_s is None else stale_s
    return (now() - at) > window + tolerance_s


# ------------------------------------------------------------------ hand-off

def running_nodes(root):
    """Every (project, node body) with status running and location not already cloud."""
    store = Store(root)
    out = []
    try:
        projects = sorted(d for d in os.listdir(store.path("projects"))
                          if not wio.is_ignored_name(d) and os.path.isdir(store.path(f"projects/{d}")))
    except OSError:
        projects = []
    for project in projects:
        names = store.list_names(f"projects/{project}/nodes")
        for n in sorted(names):
            if not n.endswith(".json"):
                continue
            body = store.read(f"projects/{project}/nodes/{n}")
            if not isinstance(body, dict) or body.get("status") != "running":
                continue
            if (body.get("location") or {}).get("where") == "cloud":
                continue
            out.append((project, body))
    return out


def _hand_off_one(store, claims, project, node, now):
    nid = node["id"]
    claim = claims.read(project, nid)
    if claim and str(claim.get("owner", "")).startswith("cloud:"):
        return False  # a cloud run owns it; not ours to move
    rel = f"projects/{project}/nodes/{nid}.json"
    for _ in range(2):  # one retry on a rev conflict with the runner
        cur = store.read(rel)
        if not isinstance(cur, dict) or cur.get("status") != "running":
            return False
        new = dict(cur, status="ready", location={"where": "cloud", "changed_at": iso(now())})
        try:
            store.write(rel, new, expected_rev=cur.get("rev", 0))
            break
        except RevConflict:
            continue
    else:
        raise RuntimeError("rev conflict")
    if claim and claim.get("token"):
        claims.release(project, nid, claim["token"])
    return True


def hand_off_running(root, now=time.time, budget_s=DEFAULT_BUDGET_S, host=None, monotonic=time.monotonic):
    """Move each running PC node to the cloud and release its claim. Stops at the budget; the rest are `incomplete`
    and fall back to staleness detection by the cloud worker."""
    host = host or default_host()
    store = Store(root, writer=WRITER, now=now)
    claims = Claims(root, now=now, writer=WRITER)
    start = monotonic()
    handed, incomplete = [], []
    todo = running_nodes(root)
    for i, (project, node) in enumerate(todo):
        key = f"{project}/{node['id']}"
        if monotonic() - start > budget_s:
            incomplete.extend(f"{p}/{n['id']}" for p, n in todo[i:])
            break
        try:
            if _hand_off_one(store, claims, project, node, now):
                handed.append(key)
        except Exception:
            incomplete.append(key)
    out = {"handed_off": handed, "incomplete": incomplete}
    if incomplete:
        out["fallback"] = "staleness"
    try:
        store.append_event(WRITER, {"kind": "pc.handoff", "summary": f"{len(handed)} node(s) handed to cloud",
                                    "data": {"host": host, "handed_off": handed, "incomplete": incomplete}})
        write_heartbeat(root, host, now=now, state="handed_off")
    except OSError:
        pass
    return out


# ------------------------------------------------------------------ entry point

def main(argv=None):
    ap = argparse.ArgumentParser(description="Waypoint PC heartbeat and shutdown hand-off")
    ap.add_argument("command", choices=["beat", "shutdown", "check"])
    ap.add_argument("--data", default=os.environ.get("WAYPOINT_DATA_ROOT", "."), help="data folder")
    ap.add_argument("--host", default=None)
    ap.add_argument("--budget", type=float, default=DEFAULT_BUDGET_S, help="shutdown time budget in seconds")
    a = ap.parse_args(argv)
    if a.command == "beat":
        print(json.dumps(write_heartbeat(a.data, a.host)))
        return 0
    if a.command == "check":
        stale = is_stale(a.data, a.host)
        print(json.dumps({"stale": stale}))
        return 1 if stale else 0
    out = hand_off_running(a.data, budget_s=a.budget, host=a.host)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
