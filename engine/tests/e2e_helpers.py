"""Shared helpers for the end-to-end version tests: a real in-process engine on an ephemeral port.

Everything lives under a temp directory: the SQLite database, the router token and the owner token.
The live engine (port 8751) and ``data/cc.sqlite`` are never touched. Requests go over real HTTP (urllib).

``who`` selects the credential of a request:
  ``router``       X-CC-Token with the router token (actor router)
  ``owner``        X-CC-Owner-Token with the owner token (actor owner, the Claude mod)
  ``origin``       the allowlisted dsh Origin (actor owner)
  ``none``         no credential
  ``bad-owner``    X-CC-Owner-Token with a wrong value
  ``router-as-owner`` the router token sent in the owner header
  ``owner-as-router`` the owner token sent in the router header
"""
from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO = ENGINE.parent
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

from cc_engine import api  # noqa: E402
from cc_engine.store import Store  # noqa: E402

OWNER_ORIGIN = "http://127.0.0.1:3080"


def node(i, deps=(), **extra):
    n = {"id": i, "title": f"Task {i}", "goal": f"Goal of {i}.", "check": {"type": "command", "run": "exit 0"},
         "deps": list(deps), "owner": "w", "lane": "core"}
    n.update(extra)
    return n


def plan_dict(pid, title, nodes):
    return {"id": pid, "title": title, "brief": f"Brief of {title}.", "cwd": None, "nodes": nodes}


class Engine:
    """An engine thread over a temp store. Use as ``Engine(tmp_path)``; call ``close()`` when done."""

    def __init__(self, base: Path):
        base = Path(base)
        base.mkdir(parents=True, exist_ok=True)
        self.base = base
        self.db_path = base / "cc.sqlite"
        self.token_path = base / "engine.token"
        self.owner_token_path = base / "owner.token"
        self.store = Store(self.db_path)
        self.server = api.make_server(self.store, host="127.0.0.1", port=0, usage_reader=lambda: [],
                                      heartbeat=0.5, poll_interval=0, token_path=self.token_path,
                                      owner_token_path=self.owner_token_path)
        self.router_token = self.server.app.token
        self.owner_token = self.server.app.owner_token()
        assert self.router_token and self.owner_token and self.router_token != self.owner_token
        self.port = self.server.server_address[1]
        assert self.port != api.DEFAULT_PORT
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
                                        daemon=True)
        self._thread.start()

    # ------------------------------------------------------------ HTTP
    def headers(self, who):
        h = {"Accept": "application/json"}
        if who == "router":
            h["X-CC-Token"] = self.router_token
        elif who == "owner":
            h["X-CC-Owner-Token"] = self.owner_token
        elif who == "origin":
            h["Origin"] = OWNER_ORIGIN
        elif who == "bad-owner":
            h["X-CC-Owner-Token"] = "not-the-owner-token"
        elif who == "router-as-owner":
            h["X-CC-Owner-Token"] = self.router_token
        elif who == "owner-as-router":
            h["X-CC-Token"] = self.owner_token
        elif who != "none":
            raise ValueError(who)
        return h

    def request(self, method, path, body=None, who="router"):
        headers = self.headers(who)
        data = None
        if method != "GET":
            data = json.dumps(body or {}).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"raw": raw}

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, body, who="router"):
        return self.request("POST", path, body, who)

    # ------------------------------------------------------------ conveniences
    def revision(self):
        status, d = self.get("/api/health")
        assert status == 200
        return d["revision"]

    def import_plan(self, pid, title, nodes, who="owner"):
        status, d = self.post("/api/plan/import", {"plan": plan_dict(pid, title, nodes),
                                                   "revision": self.revision()}, who)
        assert status == 200, d
        return d

    def set_status(self, project, nid, status, who="owner", evidence="e2e"):
        return self.post("/api/node/status", {"project": project, "id": nid, "status": status,
                                              "evidence": evidence, "revision": self.revision()}, who)

    def group(self, family, title, members, who="owner"):
        return self.post("/api/version/family", {"family": family, "title": title, "members": members,
                                                 "revision": self.revision()}, who)

    def statuses(self, project):
        status, board = self.get(f"/api/board?project={project}")
        assert status == 200
        return {n["id"]: n for n in api.decode_nodes(board)}

    def status_map(self, project):
        return {k: v["status"] for k, v in self.statuses(project).items()}

    def node(self, project, nid):
        status, d = self.get(f"/api/node?project={project}&id={nid}")
        assert status == 200, d
        return d["node"]

    def events(self, project, kind=None):
        status, d = self.get(f"/api/events?project={project}&latest=500")
        assert status == 200
        return [e for e in d["events"] if kind is None or e["kind"] == kind]

    def env(self, **extra):
        """Environment for a CLI subprocess pointed at this engine (router token file)."""
        import os
        env = dict(os.environ)
        env.pop("CC_DB", None)
        env["WAYPOINT_URL"] = self.url
        env["CC_TOKEN_FILE"] = str(self.token_path)
        env.update(extra)
        return env

    def close(self):
        try:
            self.server.shutdown()
        finally:
            self.server.server_close()
            self.store.close()


def build_alpha(engine, group=True):
    """The shared scenario: alpha-v1 (A done; B<-A, C<-B, D, F open) and alpha-v2 (F, G, H<-F open; I done).

    Node ``F`` exists in both versions so a later ``decide`` from v1 hits an id collision once v2's F has
    been carried into v3.
    """
    engine.import_plan("alpha-v1", "Alpha v1", [node("A"), node("B", ["A"]), node("C", ["B"]), node("D"),
                                                node("F")])
    engine.import_plan("alpha-v2", "Alpha v2", [node("F"), node("G"), node("H", ["F"]), node("I")])
    status, _ = engine.set_status("alpha-v1", "A", "done")
    assert status == 200
    status, _ = engine.set_status("alpha-v2", "I", "done")
    assert status == 200
    if group:
        status, d = engine.group("alpha", "Alpha", ["alpha-v1", "alpha-v2"])
        assert status == 200, d
