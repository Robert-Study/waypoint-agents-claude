# Drive-folder handoff to a cloud session

For a project whose working folder is in Drive, the runner can hand a node to a cloud agent session (one with a Drive
connector) and take its receipt back. It follows the `cloud-local-bridge` idea: Drive is a blackboard, every file has
one writer, and nothing on this machine listens on a port. Code: `runner/waypoint_runner/cloud/drive_handoff.py`.
Test: `runner/tests/test_cloud_drive.py` (a fake Drive folder, no network).

Input from cloud-01: the Drive connector reaches the `notes` folder (read tested; write not tested in the spike).
Actions jobs have no Drive credential, so this route is for a cloud session with the connector, not for Actions.

## Files (in the Drive handoff folder, exact names only)

| File | Writer | Meaning |
|---|---|---|
| `HANDOFF.<project>.<node>.<id>.json` | runner | One-shot task packet (`waypoint.drive_handoff.v1`): brief, receipt name, ack name. |
| `ACK.<project>.<node>.<id>.json` | cloud agent | Optional "I started". |
| `RECEIPT.<project>.<node>.<id>.json` | cloud agent | `waypoint.receipt.v1` plus `handoff_id`. |

`<id>` is `h-` plus 8 hex characters unless the caller passes one.

## Flow

1. `send(project, node, brief, run)`: refuses if the handoff name, or a conflicted copy of it, already exists; claims
   the node through the shared claim protocol (owner `cloud:<run>`, `claimed_by` `drive:<id>`, so the node location is
   `cloud`); writes the handoff with exclusive create. If the claim is held it returns `reason: "held"`; if the file
   appeared meanwhile it rolls the claim back and returns `reason: "exists"`.
2. The cloud agent reads the handoff through the connector, does the node, writes the receipt (and may write an ack).
3. `ingest(project, node)`, called on each runner pass: an ack or a valid receipt means the receiver acted, so the
   handoff file is deleted (one-shot), but only if it still carries our `handoff_id`. A valid receipt (same project,
   node and `handoff_id`) is stored as `receipts/<project>.<node>.json`, with status `done` imported as `submitted`
   (`error` and `needs_you` pass through), and the claim is released with its token.
4. `location(project, node)` is `cloud` while the claim is held. `conflicts(project, node)` lists conflicted copies.

## Drive sync traps

- Drive can sync late: a torn or empty file is not an error, `ingest` returns nothing and tries again next pass.
- Conflicted copies (`name (1).json`, `name - Copy.json`, `name (conflicted copy ...).json`) follow the data-folder
  name rule: never read, never overwritten, never deleted. They are only reported by `conflicts()`.
- Never overwrite a newer file: handoff creation is exclusive, the handoff is deleted only when its content is ours,
  and an existing stored receipt is kept (the cloud one goes to `receipts/<project>.<node>.cloud-<id>.json`).
- A cloud claim is stale after 10 minutes without a heartbeat (CONCURRENCY.md); the cloud side should refresh it or
  the runner may reclaim.

## Limits

Handoff and receipt are plain JSON. No secret goes in a handoff. The cloud agent needs only the Drive connector. The
runner decides when to call `send` and `ingest`; wiring into dispatch is outside this node.
