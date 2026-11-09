# Waypoint file contract (`waypoint.receipt.v1`, `waypoint.question.v1`)

Waypoint Core 1.2. Agents write a few small files at natural moments. Code does everything else. An agent never has to
call the engine API or read the board by hand, unless the step needs judgement (claiming a node it chose, answering a
question for Owner).

This page is the agent-facing subset: the receipt and question files an agent writes. The full shared data folder that
Waypoint 1.2 reads and writes is described in [Data folder](#data-folder) at the end of this page.

## Where files go

Every agent writes into a `.waypoint/` folder inside its working folder (its `cwd`, worktree or project folder):

```
<cwd>/.waypoint/
  receipts/<name>.json     one per finished piece of work (schema waypoint.receipt.v1)
  questions/<name>.json    a question for Owner, written as soon as the agent is blocked (waypoint.question.v1)
```

- `<name>` is free text of letters, digits, `.`, `_` and `-`. Use `<project>.<node>` for node work (`docs-v2.B3`) and a
  short task name otherwise (`release-notes`). Writing the same name again replaces the earlier file.
- Write the file whole, then rename it into place (or write it once). The engine ignores files that do not parse and
  tries them again when they change.
- Agents with no useful `cwd` may write to the inbox `~/.waypoint/inbox/receipts/` and `~/.waypoint/inbox/questions/`.
- The engine writes `.waypoint/.gitignore` (`*`) the first time it sees a `.waypoint/` folder, so these files are never
  committed.
- Outputs (the real deliverables) stay where the work puts them. The receipt lists them.

## Receipt: `waypoint.receipt.v1`

```json
{
  "schema": "waypoint.receipt.v1",
  "project": "docs-v2",
  "node": "B3",
  "status": "done",
  "summary": "The API guide covers all 41 routes and the route check passes.",
  "evidence": ["pytest engine/tests/test_api_docs.py: 3 passed"],
  "outputs": ["core/docs/API.md"],
  "todo": {"text": "Read the new API guide and say if it is clear.", "section": "human", "link": "core/docs/API.md"},
  "questions": [],
  "metrics": {"tokens": 41000}
}
```

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `schema` | string | yes | Exactly `waypoint.receipt.v1`. |
| `status` | `done`, `error`, `needs_you` | yes | What the agent believes. `done` is a claim, not a verdict. |
| `summary` | string, at most 300 characters | yes | One plain sentence for Owner. |
| `project`, `node` | string | no | The Waypoint node this work was for. Without them the receipt is a session receipt (logged only). |
| `evidence` | list of strings | no | Command output lines or file paths that prove the result. At most 20 items, 500 characters each. |
| `outputs` | list of strings | no | Paths (relative to `cwd`, or absolute) or https links of deliverables. At most 20. |
| `todo` | object or null | no | At most one to-do: `text` (required), `section` `human` or `computer` (default `computer`), `link`. |
| `questions` | list | no | Up to 3 questions, each shaped like a question file without `schema`. |
| `metrics` | object | no | Free numbers (tokens, seconds, files). Kept, never interpreted. |
| `session`, `agent` | string | no | Filled in by the mod or the engine when known. Agents leave them out. |

Unknown fields are kept and ignored. A file over 64 KB is refused.

## Question: `waypoint.question.v1`

```json
{"schema": "waypoint.question.v1", "project": "docs-v2", "node": "B3", "kind": "select",
 "prompt": "Which style should the guide use?", "options": ["Reference tables", "Worked examples"]}
```

`kind` is `select` (needs `options`), `likert` (optional `scale`, default 9) or `text`. Options never carry a
preselected flag. The engine queues it (`needs.ask`), and the mod raises a desktop notification once per question.

## What the engine does with them (code only)

The engine watches `.waypoint/receipts` and `.waypoint/questions` under: every non-archived project's `cwd`, every live
claim's `cwd` and worktree, every `cwd` a traced session reported in the last 48 hours, and the inbox. It polls with
`os.stat` every few seconds; no model is ever called. The mod also posts a receipt the moment a turn ends
(`POST /api/receipt`), so the watcher is the fallback for workers without the mod (`codex exec`, other harnesses).

Each file is processed once per content hash and claim state (`receipts` table). The same bytes again are a
duplicate. If the node was claimed afresh in between (a retry), the same receipt is processed again. The watcher never
follows symlinks or junctions, never reads a file over 64 KB (it is logged as `invalid`), and counts only changed files
against its per-scan budget of 500 reads, so later folders and the inbox are always looked at. Checks run on two worker
threads and never twice at once for the same node. For a receipt:

1. Validate it. A bad file is recorded as `invalid` with the reason (`GET /api/receipts`) and never changes the plan.
2. With `project` and `node`: read the node's live claim first. Then
   - Release the claim only when the receipt plausibly comes from its holder: the receipt's `session` or `agent` equals
     the claim's `by`, or the receipt file lies inside the claim's worktree or cwd, or the claim is stale. Otherwise the
     claim stays, the status is still processed, and the outcome says "claim kept: held by X".
   - A node that is already `done` ignores `error` and `needs_you` receipts (outcome "node already done; status
     ignored"). Their to-do and questions are still handled.
   - `done`: run the node's own done-check (`checks.complete_node`). The check comes from the plan, never from the
     receipt, so a receipt cannot run commands or mark work done by itself. It runs in the claim's worktree (else its
     cwd), read before the claim is released; with no claim, in the project's `cwd`. The receipt, the request `cwd`, the
     watched folder and traced folders never choose it. Pass sets `done`; fail sets `error`.
   - `error`: set `error` with the evidence (actor `receipt`).
   - `needs_you`: set `needs_you` and queue the questions.
   - Linked computer to-dos then close by themselves (`todo.housekeep`). Human to-dos are never auto-ticked.
3. Add the one to-do, if any, with the outputs as its deliverables. A relative output path is resolved against the
   request `cwd` (or the watched folder).
4. Queue the questions.
5. Write an event (one plain sentence) and push SSE `receipt`.

`POST /api/receipt` with `path` accepts only a local absolute path to a `.json` file inside a `.waypoint` folder (resolved
first). Relative paths, UNC paths (`\server\share`) and `//` paths are refused. `.gitignore` is written only when nothing
exists at that path, and never inside a `.waypoint` folder that is a link.

## Rules for agents (the food chain)

- The engine does all bookkeeping: status, done-checks, to-do closing, run logs, meters and the agent tree.
- An orchestrator does judgement upkeep only when an event arrives (Owner's priorities, which to-do matters). It never
  polls the board.
- A session writes one receipt at the end and at most one to-do.
- A subagent reports only to its parent, in its final message. It writes no receipt unless its brief names a node.

## Data folder

Waypoint 1.2 keeps its state in one shared data folder of small JSON files. The mod, the web page, the runner, the
standalone DSH shell and cloud workers all read and write it. The engine is optional: nothing here needs it, and the
mod works without it. An agent still writes only the receipt and question files above; the receipt is one of the
entities below, and the other entities are written by the Waypoint programs.

### Where it lives

The root is the first of: the `WAYPOINT_DATA_DIR` environment variable, the first line of `~/.waypoint/data-dir`,
then `~/.waypoint/data`. Stored working folders are `{machine, rel}` pairs, never absolute paths; the absolute code
root for each machine is in `machines/<id>.json`, which stays local and is not shared. See
[WHAT_LIVES_WHERE.md](WHAT_LIVES_WHERE.md) for what stays on disk, what syncs and how it is backed up, and
[TOKEN_ECONOMY.md](TOKEN_ECONOMY.md) for why bookkeeping is done by code and costs no tokens.

### Layout

```
<data folder>/
  projects/<id>.json         one project: its plan, nodes and checks (waypoint.project.v1, waypoint.node.v1)
  runs/<run>.json            one run of a project; runs/<run>/events.ndjson is its own log
  claims/<node>.json         who holds a node right now (exclusive create)
  receipts/<project>.<node>.json   write-once result of a piece of work (waypoint.receipt.v1)
  questions/  todos/  actions/  dispatch/      things for a person, requests and button presses
  agents/  orchestrators/  sessions/  settings/  usage/  health/   who can work, how much is left, how it is going
  versions/  families/  plan_stages/  snapshots/  briefs/  brain/  learn/  schedules/  chats/   history and context
  events/<writer>/<day>.ndjson     append-only logs, one file per writer
  machines/<id>.json         this machine's code root (local only)
  runner.lock.json           present while the runner is up
```

### Entities

One entity is one JSON file with a `schema` name and a `rev` counter. These are the schemas in
[waypoint-data/schema](../core/waypoint-data/schema/): action, agent, brain index, brief, chat, check, claim, dispatch
request, event, family, floor, health, learn, memory, node, orchestrator, plan stage, project, question, receipt, run,
schedule, session, settings, snapshot, todo, usage and version. Each has a sample under
[waypoint-data/fixtures/sample-folder](../core/waypoint-data/fixtures/sample-folder/).

A floor is a setting: a share of a pool's weekly usage that must be left over. No floor is built in; a floor that is
not set means 0% (use it or lose it).

### How writers behave

[data/CONCURRENCY.md](data/CONCURRENCY.md) is the rulebook: whole-file replace through a temp file and rename, a
revision check on every write, exclusive-create locks and claims, and one log file per writer so nothing conflicts
when Drive or git carries the folder between machines. The data library (`core/waypoint-data`, with a Python twin in
`core/waypoint-data/py`) is the only code that touches the folder.

### Answers go back to the work

Before an agent asks Owner anything, it writes the question (a question file, or `questions` in a `needs_you`
receipt) so the question and the work it blocks are in Waypoint first. The ingest keeps the asking session's id
on `questions/<id>.json` as `session` (the receipt's `session`, which the mod fills in, or a `session` field in the
question file).

When Owner answers, the runner routes the answer once (`runner/waypoint_runner/resume.py`, a tick step) and writes
the decision into the same question file as `resume`:

| Asking session | `resume.mode` / `action` | What happens |
| --- | --- | --- |
| Active within `settings.resume_cold_after_minutes` (default 60) | `direct` / `deliver` | The mod in that session appends one user row with the question and answer, then sets `resume.delivered_at`. The node is left as it is. |
| Idle longer, ended, or unknown; the question names a node | `fresh` / `restart_node` | The cold chat is not woken. A `needs_you` node goes back to `ready` with evidence `answered in questions/<id>.json; resume <project> from Waypoint`, and the runner starts a fresh worker whose brief opens with that line and the answers. |
| Idle longer, ended, or unknown; no node | `fresh` / `clear_session` | The lead clears that session and gives it the one line `resume <project> from Waypoint`, then sets `resume.delivered_at`. |

Last activity is the newest of `sessions/<id>.json` (`updated_at`, `started_at`, last event) and the desktop app's own
session file (`lastActivityAt`, read only). A node with several open questions is routed only when the last one is
answered. The other `resume` fields are `session`, `line`, `idle_minutes`, `cold_after_minutes`, `reason`,
`node_status_before` and `decided_at` (schema: `question.schema.json`). Each routing also writes one
`question.resume` event. So the answer and the blocked work are readable from the folder alone: the question file
(prompt, answer, `project`, `node`, `resume`) and `projects/<project>/nodes/<node>.json`.

### Related protocols

- [runner/RUNNER.md](runner/RUNNER.md): the runner starts on demand, works through ready nodes and exits when idle.
- [cloud/PROTOCOL.md](cloud/PROTOCOL.md): handing a node to GitHub Actions, claiming it, and bringing the receipt home.
- [cloud/SPIKE_RESULT.md](cloud/SPIKE_RESULT.md): what was measured about running nodes in Actions on a private repo.
