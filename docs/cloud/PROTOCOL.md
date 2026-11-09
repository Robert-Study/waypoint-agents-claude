# Cloud protocol: handoff, claim, receipt, reconcile

Status: Waypoint 1.2 (7 Nov 2026). Code: `runner/waypoint_runner/cloud/worker.py`. Workflow:
`.github/workflows/waypoint-cloud-node.yml`. Linked from `core/docs/FILE_CONTRACT.md`.
Inputs: `SPIKE_RESULT.md` (Actions works for a no-model node on a private repo; no agent CLI on the runner; no secret),
`core/docs/data/CONCURRENCY.md` ("Claims" is the only claim protocol).

One idea: GitHub Actions has no persistent disk, so every piece of state travels through git. The worker writes the
same files as every other writer, commits them to a task branch and pushes. The runner brings them home.

## 1. Handoff (runner to cloud)

- The runner decides a node goes to the cloud: node `location.where` is `cloud` and `status` is `ready`.
- The runner commits any claim it wants to hold, then triggers the workflow (`workflow_dispatch`, inputs `project`,
  `node`, `data`). Only no-model nodes run in Actions until Owner authorises an agent-CLI secret (SPIKE_RESULT).
- The workflow runs on a private repo only. It uses the built-in `github.token`, no other secret, no paid runner.

## 2. Claim

- The shared protocol, unchanged: `claims/<node>.json`, schema `waypoint.claim.v1`, exclusive create.
  The worker claims with owner `cloud:<run id>` and location `cloud`. It defines no second protocol.
- One of two racing claimants wins; the other gets `held` and does no work (`status: held`).
- Heartbeat is by `waypoint_data` `Claims.heartbeat` (token must match). A claim with no heartbeat is stale after
  10 minutes for `cloud:` owners (5 minutes for mod and runner). It is reclaimed only after that timeout: the old file
  is renamed `claims/<node>.json.stale.<token>`, a new claim is created, and a `claim.reclaimed` event names the old owner.
- Across clones git has no exclusive create, so two cloud claims can both succeed locally. When a merge shows two
  claims on one path, the later `since` loses (equal `since`: token order); the loser stops before work
  (`claim_loses`). In practice the runner is the single creator of `cloud:` claims.
- The worker releases its claim (token must match) after writing its receipt.

## 3. Run and receipt

- The worker runs the node (default: the node's check command, no model) and then an optional CI gate (`--ci-cmd`).
  The receipt says `done` only when both pass; otherwise `error`. A crash is an `error` receipt.
- Receipt: `receipts/<project>.<node>.json`, `waypoint.receipt.v1`, `session: cloud:<run>`, write once by exclusive
  create. The same run writing twice gets its first file back. A retry by a later run writes
  `<project>.<node>.<n>.json`. No receipt is ever edited.
- Events: appended only to `events/cloud-<run>/<day>.ndjson` (own file, never shared).
- The worker commits only the receipt and its own events folder, on a branch `cloud/<node>-<run id>`, and pushes that
  branch to `origin`. It refuses to push `main`, any non `cloud/*` branch, and any remote not confirmed private.
  It never pushes to a public remote and never edits the node file or any other entity.

## 4. Reconcile (cloud back to the runner)

- The runner fetches `cloud/*` branches and, per branch, checks that only the receipt and `events/cloud-<run>` changed.
- It imports the receipt. A cloud `done` is imported as `submitted`, not `done`; the runner then runs the node's own
  done-check locally (or on its CI) and marks the node `done` only if it passes. `error` and `needs_you` import as they are.
- It merges the branch (events and receipt are per-writer files, so no conflict), deletes the `cloud/*` branch locally
  and on the private remote, and releases any leftover claim whose token matches the receipt's run.
- A claim whose heartbeat is stale and that has no receipt is reclaimed by timeout; a late receipt from the old run
  is imported as a duplicate-session receipt and does not change the node if the node already has a newer claim token.
