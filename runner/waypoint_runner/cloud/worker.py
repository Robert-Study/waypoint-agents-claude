"""Cloud worker: claim one ready cloud node from the data folder, run it, write one receipt, push by git.

Protocol: core/docs/cloud/PROTOCOL.md. The claim protocol is the shared one (waypoint_data.claims, CONCURRENCY.md
"Claims"); this module defines no second one. It claims as owner `cloud:<run>` with location `cloud`.
GitHub Actions has no persistent disk, so all state returns through git: claim, events and receipt are files in the
data folder of the private clone, committed to a task branch and pushed. Never pushes main or a public remote.
Stdlib only.
"""
import argparse
import json
import os
import secrets
import shlex
import subprocess
import sys
import time
from pathlib import Path

_PY = Path(__file__).resolve().parents[3] / "core" / "waypoint-data" / "py"
if str(_PY) not in sys.path:
    sys.path.insert(0, str(_PY))

from waypoint_data import io as wio  # noqa: E402
from waypoint_data.claims import Claims  # noqa: E402
from waypoint_data.store import Store, parse_iso, serialize  # noqa: E402

LOCATION = "cloud"
RECEIPT_SCHEMA = "waypoint.receipt.v1"
CHECK_TIMEOUT_S = 20 * 60


class PushRefused(Exception):
    pass


def owner_for(run_id):
    return f"cloud:{run_id}"


# ------------------------------------------------------------------ nodes

def pick_ready_node(root, project, node=None):
    """First `ready` node whose location is cloud (in id order), or the named one if it qualifies. None if no match."""
    store = Store(root)
    for n in sorted(store.list_names(f"projects/{project}/nodes")):
        if not n.endswith(".json"):
            continue
        body = store.read(f"projects/{project}/nodes/{n}")
        if not isinstance(body, dict) or body.get("status") != "ready":
            continue
        if (body.get("location") or {}).get("where") != LOCATION:
            continue
        if node is None or body.get("id") == node:
            return body
    return None


# ------------------------------------------------------------------ claims (shared protocol)

def claim_node(root, project, node, run_id, now=time.time, token=None):
    """Claim a node as cloud:<run>. One of two racing claimants wins; a stale claim is reclaimed after its timeout."""
    return Claims(root, now=now, writer=f"cloud-{run_id}").claim(
        project, node, owner_for(run_id), LOCATION, token or secrets.token_hex(8), claimed_by=owner_for(run_id))


def heartbeat(root, project, node, token, now=time.time, run_id="py"):
    return Claims(root, now=now, writer=f"cloud-{run_id}").heartbeat(project, node, token)


def release_claim(root, project, node, token, run_id="py"):
    return Claims(root, writer=f"cloud-{run_id}").release(project, node, token)


def claim_loses(mine, other):
    """Git has no exclusive create across clones. After a merge shows two claims on one path, the later `since`
    loses; equal `since` is decided by token order. The loser stops before doing work."""
    a = (parse_iso(mine.get("since") or mine.get("heartbeat_at")) or 0, str(mine.get("token")))
    b = (parse_iso(other.get("since") or other.get("heartbeat_at")) or 0, str(other.get("token")))
    return a > b


# ------------------------------------------------------------------ receipt (write once)

def write_receipt(root, project, node, status, summary, run_id, evidence=None, outputs=None, todo=None):
    """Write receipts/<project>.<node>.json exactly once. The same run writing again gets its first file back.
    A later run (a retry) writes <project>.<node>.<n>.json; no existing receipt is ever edited."""
    session = owner_for(run_id)
    rdir = Path(root) / "receipts"
    body = {"schema": RECEIPT_SCHEMA, "project": project, "node": node, "status": status,
            "summary": summary[:300] or "Cloud run finished.", "evidence": (evidence or [])[:20],
            "outputs": (outputs or [])[:20], "todo": todo, "session": session, "agent": "cloud-worker"}
    n = 1
    os.makedirs(rdir, exist_ok=True)
    while True:
        name = f"{project}.{node}.json" if n == 1 else f"{project}.{node}.{n}.json"
        path = rdir / name
        try:
            wio.create_exclusive(str(path), serialize(body), writer=f"cloud-{run_id}")
            return str(path)
        except FileExistsError:
            existing = wio.read_json(str(path))
            if isinstance(existing, dict) and existing.get("session") == session:
                return str(path)
            n += 1


# ------------------------------------------------------------------ running

def default_run_fn(cwd):
    """No-model node: run its check command in `cwd` (the spike proved Actions has no agent CLI)."""
    def run(node):
        cmd = (node.get("check") or {}).get("run")
        if not cmd:
            return False, ["node has no check command to run"]
        try:
            p = subprocess.run(shlex.split(cmd), cwd=str(cwd), capture_output=True, text=True, timeout=CHECK_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, [f"run failed: {type(e).__name__}"]
        tail = [ln for ln in (p.stdout + p.stderr).strip().splitlines() if ln][-3:] or ["(no output)"]
        return p.returncode == 0, [f"exit {p.returncode}: {ln}"[:500] for ln in tail]
    return run


def run_once(root, project, run_id, node=None, run_fn=None, ci_fn=None, now=time.time):
    """Claim one ready cloud node, run it, apply the optional CI gate, write one receipt, release the claim.
    Returns {"status": idle|held|done|error, "node", "receipt"}. The receipt says `done` only when the run and the
    CI gate both pass; the runner imports a cloud `done` as `submitted`."""
    picked = pick_ready_node(root, project, node)
    if picked is None:
        return {"status": "idle", "node": node, "receipt": None}
    nid = picked["id"]
    writer = f"cloud-{run_id}"
    store = Store(root, writer=writer, now=now)
    res = claim_node(root, project, nid, run_id, now=now)
    if not res:
        return {"status": "held", "node": nid, "receipt": None}
    token = res.claim["token"]
    if res.reclaimed_from:
        old = res.reclaimed_from.get("owner")
        store.append_event(writer, {"kind": "claim.reclaimed", "project": project, "node": nid,
                                    "summary": f"reclaimed from {old}", "data": {"old_owner": old}})
    store.append_event(writer, {"kind": "cloud.claimed", "project": project, "node": nid, "summary": "claimed"})
    try:
        ok, evidence = (run_fn or default_run_fn(root))(picked)
        if ok and ci_fn is not None:
            ok, ci_ev = ci_fn(picked)
            evidence = list(evidence) + list(ci_ev)
    except Exception as e:  # a crash is an error receipt, never a silent exit
        ok, evidence = False, [f"worker crashed: {type(e).__name__}"]
    status = "done" if ok else "error"
    word = "passed" if ok else "failed"
    receipt = write_receipt(root, project, nid, status, f"Cloud run {run_id} {word} for {nid}.", run_id,
                            evidence=evidence)
    store.append_event(writer, {"kind": "cloud.receipt", "project": project, "node": nid, "summary": status})
    release_claim(root, project, nid, token, run_id)
    return {"status": status, "node": nid, "receipt": receipt}


# ------------------------------------------------------------------ git (transport)

def _git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=check)


def commit_paths(repo, paths, message, branch):
    """Commit only the named paths on a task branch (bounded touch paths, single writer per task file)."""
    _git(repo, "checkout", "-q", "-B", branch)
    _git(repo, "add", "--", *paths)
    _git(repo, "commit", "-q", "-m", message)


def gh_is_private():
    r = subprocess.run(["gh", "api", f"repos/{os.environ.get('GITHUB_REPOSITORY', '')}", "--jq", ".private"],
                       capture_output=True, text=True)
    return r.returncode == 0 and r.stdout.strip() == "true"


def push_branch(repo, branch, private_check=gh_is_private):
    """Push a task branch to origin. Refuses main/master and any remote not known to be private."""
    if branch in ("main", "master") or not branch.startswith("cloud/"):
        raise PushRefused("only cloud/* task branches are pushed")
    if not private_check():
        raise PushRefused("remote is not confirmed private")
    _git(repo, "push", "-q", "origin", f"{branch}:{branch}")


# ------------------------------------------------------------------ entry point

def main(argv=None):
    ap = argparse.ArgumentParser(description="Waypoint cloud worker (one node per run)")
    ap.add_argument("--project", required=True)
    ap.add_argument("--node")
    ap.add_argument("--data", default=os.environ.get("WAYPOINT_DATA_ROOT", "."), help="data folder in the clone")
    ap.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", secrets.token_hex(4)))
    ap.add_argument("--ci-cmd", help="command that must pass before a done receipt")
    ap.add_argument("--no-push", action="store_true")
    a = ap.parse_args(argv)
    ci_fn = None
    if a.ci_cmd:
        def ci_fn(_node):
            p = subprocess.run(shlex.split(a.ci_cmd), capture_output=True, text=True)
            return p.returncode == 0, [f"ci exit {p.returncode}"]
    out = run_once(a.data, a.project, a.run_id, node=a.node, ci_fn=ci_fn)
    print(json.dumps(out))
    if out["status"] in ("done", "error") and not a.no_push:
        branch = f"cloud/{out['node']}-{a.run_id}"
        rel = [os.path.relpath(out["receipt"], a.data), f"events/cloud-{a.run_id}"]
        commit_paths(a.data, rel, f"cloud({out['node']}): receipt {out['status']} run {a.run_id}", branch)
        push_branch(a.data, branch)
    return 0 if out["status"] in ("done", "idle") else 1


if __name__ == "__main__":
    sys.exit(main())
