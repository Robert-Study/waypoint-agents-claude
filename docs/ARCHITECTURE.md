# Architecture

Waypoint Core 1.2. All state lives in one shared **data folder** of small JSON files. Every part of Waypoint reads
and writes that folder through the data library; none keeps its own copy of the plan. The 1.1 engine
(`engine/cc_engine`, HTTP on 127.0.0.1:8751, SQLite) is retired: nothing in 1.2 needs it, and `waypoint engine`
answers exit 5.

```
 +------------------+  +----------------+  +------------------+  +---------------------------+
 | dashboards       |  | waypoint CLI   |  | web page         |  | workers (claude -p, codex,|
 | (see README)     |  | scripts/       |  | web/waypoint-web |  | free pools, cloud Actions)|
 +--------+---------+  +-------+--------+  +--------+---------+  +-------------+-------------+
          | vendored TS        | Python          | folder HTTP API           | receipt and question
          | data library       | data library    | (served by the runner)    | files in <cwd>/.waypoint/
          v                    v                 v                           v
 +-------------------------------------------------------------------------------------------+
 | data folder  (WAYPOINT_DATA_DIR, else ~/.waypoint/data-dir pointer, else ~/.waypoint/data) |
 |  projects/  runs/  claims/  receipts/  questions/  todos/  actions/  dispatch/  sessions/  |
 |  agents/  settings/  usage/  events/<writer>/<day>.ndjson  runner.lock.json  ...           |
 +-------------------------------------------------------------------------------------------+
          ^
          | ticks while work exists, exits when idle
 +--------+-----------------------------------------------------------------------------------+
 | runner/waypoint_runner  (python -m waypoint_runner; started on demand by the mod or CLI)    |
 |  tick.py: actions -> answer routing -> jobs -> schedules -> dispatch -> finish runs         |
 +-------------------------------------------------------------------------------------------+
```

## Data library

`core/waypoint-data` holds the schemas (`schema/*.schema.json`), a golden sample folder (`fixtures/sample-folder`),
the TypeScript library (`src/`) and its Python
twin (`py/waypoint_data`). It is the only code that touches the folder. Rules ([data/CONCURRENCY.md](data/CONCURRENCY.md)):
whole-file replace through a temp file and rename; a `rev` counter checked on every write (a stale write is refused);
exclusive-create claims and locks; one append-only log file per writer. `node core/waypoint-data/schema/check.mjs`
validates the schemas and the sample folder.

## Data flow

1. **Plan.** A project is `projects/<id>/project.json` plus one file per node (`nodes/<node>.json`) with its goal,
   done-check, deps (each saying what crosses) and status.
2. **Run.** The owner presses Run (mod, web page) or runs `waypoint run`: a `runs/<id>.json` is queued and the runner
   is started. Each tick starts every ready node of an active run, on routes whose switch is on (`agents/*.json`)
   and whose pool is above its floor (`settings/floors.json`; an unset floor is 0%).
3. **Dispatch.** `dispatch_core.py` claims the node (`claims/<node>.json`), writes `jobs/<job>/spec.json` with the
   worker brief, and starts an adapter: headless Claude, Codex CLI, free pools, or a mod route (`session_subagent`,
   `side_session`) through `dispatch/requests/<id>.json`, which the mod picks up.
4. **Receipts.** A worker writes one receipt (`<cwd>/.waypoint/receipts/*.json`, `waypoint.receipt.v1`) and, when
   blocked, questions. `receipts_ingest.py` validates them into `receipts/` and `questions/` and raises to-dos. The
   node's own done-check decides `done`; a receipt never does. See [FILE_CONTRACT.md](FILE_CONTRACT.md).
5. **Questions and answers.** Owner answers in the mod's panes or the web page (`questions/<id>.json` gets
   `answer`, revision-checked). The runner then routes each answer once (`resume.py`): when the asking session was
   active within `settings.resume_cold_after_minutes` (default 60, the prompt-cache life) it gets the answer
   directly (the mod in that session appends it); when it is cold or ended, it is not woken: the node goes back to
   `ready` and a fresh worker starts with the line `resume <project> from Waypoint` and the answers, or, for a
   question with no node, the lead clears that session and sends the line. The decision is written into the
   question file (`resume`), so the answer and the blocked work are readable from the folder alone.
6. **Callbacks and notices.** A finished job writes one callback to its parent's inbox (`callbacks.py`) and a
   desktop notice when no Claude window is open (`notify.py`). The mod shows counts, a band and toasts.
7. **Cloud.** A cloud session queues `sync/inbox/*.json` (`waypoint sync`); `actions_sync.py` imports them. A
   no-model node can run in GitHub Actions and bring its receipt home ([cloud/PROTOCOL.md](cloud/PROTOCOL.md)).

## Food chain

Each layer does only what it is best at. Every message between agents wakes a full model turn, so bookkeeping is code.

| Layer | Does | Never does |
|---|---|---|
| Runner | All bookkeeping: dispatch, done-checks, claim release, to-do closing, answer routing, callbacks, run logs, usage readings. | Call a model itself. |
| Mod | Panes and buttons; dispatch pickup; injects the claimed node's brief; guards owned files, paid routes and floors; posts receipts; delivers answers to its own warm session. | Decide anything. |
| Orchestrator | Judgement only, on events: priorities, which node next, rework after a failed check, clearing a cold session. | Poll the board or update status by hand. |
| Session | The work, then one receipt and at most one to-do; writes its question to Waypoint before it asks. | Bookkeeping. |
| Subagent | Its task; reports only to its parent. | Write receipts (unless its brief names a node) or message anyone else. |

## Runner modules

`runner/waypoint_runner/`, standard library only.

| Module | Role |
|---|---|
| `__main__.py`, `lifecycle.py`, `lock.py` | Start on demand, single-instance lock, tick while work exists, exit when idle ([runner/RUNNER.md](runner/RUNNER.md)). |
| `tick.py` | One pass: actions, answer routing, jobs, schedules, dispatch, finished runs. |
| `actions*.py` | Handlers for queued `actions/<id>.json` (run, retry, stop, accept, plan, projects, to-dos, sync, chat, probes). |
| `resume.py` | Routes answered questions: warm session gets the answer, cold one resumes from Waypoint. |
| `dispatch_core.py`, `policy.py`, `adapters/` | Pick an allowed route within its floor; start, poll and stop jobs. |
| `receipts_ingest.py`, `ingest.py`, `node_checks.py` | Receipt and question ingest; done-checks. |
| `recovery.py`, `callbacks.py`, `notify.py` | Retry ledger, child-to-parent callbacks, desktop notices. |
| `schedule.py`, `queue.py`, `usage.py`, `memory.py` | Scheduled runs, prompt queue, usage readings, memory floor. |
| `serve.py` | The web page and the folder HTTP API on loopback, alive as long as the runner. |
| `mcp_board.py`, `runners_mcp.py` | MCP tools for agents: board and claims; one tool per non-native route (agy, codex, groq, studio). |
| `compat_shim.py` | Optional read-only answers on the old 8751 address; never started by default. |
| `cloud/`, `cloud_sync.py` | GitHub Actions hand-off and the sync inbox. |

## CLI

`scripts/waypoint_cli.py` is one stdlib file over the data folder ([CLI.md](CLI.md)). Before a machine is cut over it
hands commands to the 1.1 engine CLI (`scripts/waypoint_cli_engine.py`).


## Where data lives

See [WHAT_LIVES_WHERE.md](WHAT_LIVES_WHERE.md). In short: the data folder is outside git and is backed up on its
own; stored working folders are `{machine, rel}` pairs resolved through `machines/<id>.json`, which stays local.
