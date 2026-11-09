"""Claim protocol (docs/data/CONCURRENCY.md, Claims): one file per node at claims/<node>.json, created exclusively.
Stdlib only."""
import os
import re
import secrets
import time

from . import io as wio
from .store import LockBusy, RevConflict, acquire_lock, iso, parse_iso, serialize  # noqa: F401

SCHEMA = "waypoint.claim.v1"
STALE_S = 5 * 60  # runner / mod window
CLOUD_STALE_S = 10 * 60  # cloud: owners write through git or Drive with delay
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_OWNER = re.compile(r"^(mod|runner|cloud:[A-Za-z0-9._-]{1,64})$")


class ClaimResult:
    def __init__(self, ok, claim=None, reason=None, holder=None, reclaimed_from=None):
        self.ok = ok
        self.claim = claim
        self.reason = reason
        self.holder = holder
        self.reclaimed_from = reclaimed_from

    def __bool__(self):
        return self.ok

    def __repr__(self):
        return f"ClaimResult(ok={self.ok}, reason={self.reason})"


def stale_window(owner, configured=None):
    """Stale window for the owner kind. A configured value is never shorter than the kind's minimum."""
    if str(owner).startswith("cloud:"):
        return CLOUD_STALE_S if configured is None else max(CLOUD_STALE_S, configured)
    return STALE_S if configured is None else configured


class Claims:
    """`stale_s` overrides the runner/mod window (cloud owners never go below 10 minutes). `now` returns epoch seconds."""

    def __init__(self, root, stale_s=None, now=time.time, writer="py"):
        self.root = os.path.abspath(root)
        self.stale_s = stale_s
        self.now = now
        self.writer = writer

    def _path(self, node):
        return os.path.join(self.root, "claims", f"{node}.json")

    def read(self, project, node):
        """The live claim for a node, or None. Conflicted copies and temp names are never read."""
        name = f"{node}.json"
        if wio.is_ignored_name(name):
            return None
        c = wio.read_json(self._path(node))
        if isinstance(c, dict) and c.get("project") not in (None, project):
            return None
        return c

    def list_claims(self):
        d = os.path.join(self.root, "claims")
        out = []
        for n in wio.list_names(d):
            if n.endswith(".json"):
                c = wio.read_json(os.path.join(d, n))
                if isinstance(c, dict):
                    out.append(c)
        return out

    def is_stale(self, claim):
        beat = parse_iso(claim.get("heartbeat_at"))
        if beat is None:
            return True
        window = stale_window(claim.get("owner", ""), self.stale_s)
        return self.now() - beat > window

    def claim(self, project, node, owner, location, token, claimed_by=None, environment=None):
        if not _ID.match(str(project)) or not _ID.match(str(node)):
            raise ValueError("project and node must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
        if not _OWNER.match(str(owner)):
            raise ValueError("owner must be mod, runner or cloud:<run>")
        if not isinstance(token, str) or len(token) < 8:
            raise ValueError("token must be a string of at least 8 characters")
        if not location:
            raise ValueError("location is required")
        path = self._path(node)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            lock = acquire_lock(path + ".lock", owner=self.writer, wait_s=0, now=self.now)
        except LockBusy:
            return ClaimResult(False, reason="held")
        try:
            t = iso(self.now())
            body = {"schema": SCHEMA, "project": project, "node": node, "owner": owner, "location": location,
                    "token": token, "heartbeat_at": t, "since": t}
            if claimed_by:
                body["claimed_by"] = claimed_by
            if environment:
                body["environment"] = environment
            try:
                wio.create_exclusive(path, serialize(body), writer=self.writer)
                return ClaimResult(True, claim=body)
            except FileExistsError:
                pass
            cur = wio.read_json(path)
            if not isinstance(cur, dict):
                return ClaimResult(False, reason="held")
            if cur.get("token") == token:  # same claimer: a refresh
                body = dict(cur, heartbeat_at=t, location=location)
                wio.atomic_write(path, serialize(body), writer=self.writer)
                return ClaimResult(True, claim=body)
            if not self.is_stale(cur):
                return ClaimResult(False, reason="held", holder=cur)
            aside = f"{path}.stale.{cur.get('token') or secrets.token_hex(4)}"
            try:
                os.replace(path, aside)
            except OSError:
                return ClaimResult(False, reason="held", holder=cur)
            try:
                wio.create_exclusive(path, serialize(body), writer=self.writer)
            except FileExistsError:
                return ClaimResult(False, reason="held")
            return ClaimResult(True, claim=body, reclaimed_from=cur)
        finally:
            lock.release()

    def heartbeat(self, project, node, token):
        """Refresh heartbeat_at if the token still matches. False when the claim is gone or belongs to someone else."""
        path = self._path(node)
        try:
            lock = acquire_lock(path + ".lock", owner=self.writer, wait_s=2.0, now=self.now)
        except LockBusy:
            return False
        try:
            cur = wio.read_json(path)
            if not isinstance(cur, dict) or cur.get("token") != token or cur.get("project") != project:
                return False
            wio.atomic_write(path, serialize(dict(cur, heartbeat_at=iso(self.now()))), writer=self.writer)
            return True
        finally:
            lock.release()

    def release(self, project, node, token):
        """Delete the claim only if the token matches. A later claim of the node gets a new token."""
        path = self._path(node)
        try:
            lock = acquire_lock(path + ".lock", owner=self.writer, wait_s=2.0, now=self.now)
        except LockBusy:
            return False
        try:
            cur = wio.read_json(path)
            if not isinstance(cur, dict) or cur.get("token") != token or cur.get("project") != project:
                return False
            return wio.remove_if_exists(path)
        finally:
            lock.release()
