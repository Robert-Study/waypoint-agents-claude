"""Drive-folder handoff to a cloud session (Waypoint 1.2, node cloud-drive).

For a project whose working folder is in Drive, the runner hands a node to a cloud agent session that has a Drive
connector, using the cloud-local-bridge blackboard idea: one writer per file, distinct request and receipt paths.

Files in the Drive handoff folder (all names are exact; conflicted copies are never read, overwritten or deleted):
  HANDOFF.<project>.<node>.<id>.json   written by the runner, one-shot, deleted once the receiver acted
  ACK.<project>.<node>.<id>.json       optional, written by the cloud agent when it starts
  RECEIPT.<project>.<node>.<id>.json   written by the cloud agent when done (waypoint.receipt.v1 plus handoff_id)

Marking the node "cloud" is the shared claim (claims/<node>.json, owner cloud:<run>, claimed_by drive:<id>), made by
the runner, the single creator of cloud claims. A cloud `done` is imported as `submitted`. Stdlib only, no network.
Rules: core/docs/data/CONCURRENCY.md (names, claims) and core/docs/cloud/DRIVE_HANDOFF.md.
"""
import os
import re
import secrets
import sys
import time

try:
    import waypoint_data  # noqa: F401
except ImportError:  # running from a checkout: the library sits in core/waypoint-data/py
    _here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.abspath(os.path.join(_here, "..", "..", "..", "core", "waypoint-data", "py")))

from waypoint_data import io as wio  # noqa: E402
from waypoint_data.claims import Claims  # noqa: E402
from waypoint_data.store import RevConflict, Store, iso  # noqa: E402

HANDOFF_SCHEMA = "waypoint.drive_handoff.v1"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RUN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_HID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
DRIVE_PREFIX = "drive:"


class HandoffInfo:
    def __init__(self, ok, reason=None, id=None, token=None, filename=None, receipt_name=None, ack_name=None,
                 holder=None):
        self.ok = ok
        self.reason = reason
        self.id = id
        self.token = token
        self.filename = filename
        self.receipt_name = receipt_name
        self.ack_name = ack_name
        self.holder = holder

    def __bool__(self):
        return self.ok

    def __repr__(self):
        return f"HandoffInfo(ok={self.ok}, reason={self.reason}, id={self.id})"


def _names(project, node, hid):
    stem = f"{project}.{node}.{hid}.json"
    return f"HANDOFF.{stem}", f"RECEIPT.{stem}", f"ACK.{stem}"


class DriveHandoff:
    """`drive_dir` is the synced handoff folder, `data_root` the Waypoint data folder (claims, receipts)."""

    def __init__(self, drive_dir, data_root, now=time.time, writer="runner"):
        self.drive = os.path.abspath(drive_dir)
        self.data = os.path.abspath(data_root)
        self.now = now
        self.writer = writer
        self._claims = Claims(self.data, now=now, writer=writer)
        self._store = Store(self.data, writer=writer, now=now)

    # ------------------------------------------------------------ helpers
    def _live_claim(self, project, node):
        c = self._claims.read(project, node)
        if not isinstance(c, dict) or not str(c.get("owner", "")).startswith("cloud:"):
            return None
        if not str(c.get("claimed_by") or "").startswith(DRIVE_PREFIX):
            return None
        return c

    def _listing(self):
        try:
            return sorted(os.listdir(self.drive))
        except (FileNotFoundError, NotADirectoryError):
            return []

    def _blocking_copy(self, filename):
        """Is the exact name, or any conflicted copy of it, already in the Drive folder?"""
        stem = filename[:-5]
        return any(n == filename or (n.startswith(stem) and wio.is_conflicted_copy_name(n)) for n in self._listing())

    def _read_json(self, path):
        try:
            return wio.read_json(path)
        except (wio.WaypointIOError, OSError):
            return None  # torn or locked (Drive still syncing): try again next time

    # ------------------------------------------------------------ public
    def location(self, project, node):
        """"cloud" while a live Drive handoff claim is held for the node, else None."""
        return "cloud" if self._live_claim(project, node) else None

    def send(self, project, node, brief, run, handoff_id=None):
        """Claim the node as cloud and write the one-shot handoff file. Never overwrites a Drive file."""
        if not _ID.match(str(project)) or not _ID.match(str(node)):
            raise ValueError("project and node must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
        if not _RUN.match(str(run)):
            raise ValueError("run must match ^[A-Za-z0-9._-]{1,64}$")
        hid = handoff_id or f"h-{secrets.token_hex(4)}"
        if not _HID.match(hid):
            raise ValueError("handoff id must match ^[A-Za-z0-9-]{1,64}$")
        fn, rn, an = _names(project, node, hid)
        if self._blocking_copy(fn):
            return HandoffInfo(False, "exists", id=hid, filename=fn)
        token = secrets.token_hex(8)
        res = self._claims.claim(project, node, f"cloud:{run}", f"cloud-drive-{run}", token,
                                 claimed_by=f"{DRIVE_PREFIX}{hid}")
        if not res:
            return HandoffInfo(False, res.reason or "held", id=hid, holder=res.holder)
        body = {"schema": HANDOFF_SCHEMA, "handoff_id": hid, "project": project, "node": node, "run": run,
                "created_at": iso(self.now()), "brief": brief, "receipt_name": rn, "ack_name": an,
                "protocol": "cloud-local-bridge",
                "instructions": ("Do only this node. Write ack_name when you start (optional) and receipt_name when "
                                 "done, as waypoint.receipt.v1 plus handoff_id. Do not edit this file.")}
        try:
            wio.create_exclusive(os.path.join(self.drive, fn), Store.serialize(body), writer=self.writer)
        except FileExistsError:
            self._claims.release(project, node, token)
            return HandoffInfo(False, "exists", id=hid, filename=fn)
        except BaseException:
            self._claims.release(project, node, token)
            raise
        return HandoffInfo(True, id=hid, token=token, filename=fn, receipt_name=rn, ack_name=an)

    def conflicts(self, project, node):
        """Names of conflicted copies Drive made of this handoff's files. Reported, never touched."""
        c = self._live_claim(project, node)
        if not c:
            return []
        hid = c["claimed_by"][len(DRIVE_PREFIX):]
        stems = [n[:-5] for n in _names(project, node, hid)]
        return [n for n in self._listing()
                if wio.is_conflicted_copy_name(n) and any(n.startswith(s) for s in stems)]

    def ingest(self, project, node):
        """Act on what the cloud agent left. Returns the imported receipt, or None when there is none yet.

        An ack or receipt means the receiver acted, so the handoff file is deleted (only if it is still ours).
        A valid receipt is stored as receipts/<project>.<node>.json (never over an existing one) and the claim is
        released. Only exact file names count; conflicted copies and torn files are left for a later pass.
        """
        c = self._live_claim(project, node)
        if not c:
            return None
        hid = c["claimed_by"][len(DRIVE_PREFIX):]
        fn, rn, an = _names(project, node, hid)
        names = set(self._listing())
        rec = None
        if rn in names:
            rec = self._read_json(os.path.join(self.drive, rn))
            if not (isinstance(rec, dict) and rec.get("project") == project and rec.get("node") == node
                    and rec.get("handoff_id") == hid):
                rec = None
        if rec is None and an not in names:
            return None
        self._delete_handoff(fn, hid, names)
        if rec is None:
            return None
        imported = {k: v for k, v in rec.items() if k != "handoff_id"}
        imported["schema"] = "waypoint.receipt.v1"
        if imported.get("status") == "done":
            imported["status"] = "submitted"
        imported["imported_from"] = f"drive:{hid}"
        try:
            self._store.write(f"receipts/{project}.{node}.json", imported, expected_rev=0)
        except RevConflict:  # a newer receipt is already there: keep both
            try:
                self._store.write(f"receipts/{project}.{node}.cloud-{hid}.json", imported, expected_rev=0)
            except RevConflict:
                pass
        self._claims.release(project, node, c.get("token"))
        return imported

    def _delete_handoff(self, fn, hid, names):
        if fn not in names:
            return
        path = os.path.join(self.drive, fn)
        cur = self._read_json(path)
        if isinstance(cur, dict) and cur.get("handoff_id") == hid:
            wio.remove_if_exists(path)
