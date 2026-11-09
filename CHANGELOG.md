# Changelog

All notable changes to Waypoint Core are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow
[Semantic Versioning](https://semver.org/).

The product version (engine, CLI, mod) and the API version are separate. A breaking API change
(a new API major) always gets an entry here.

## [Unreleased]

### Added

- **Product version labels** (`docs/CLI.md`): a project can carry `version_label`, the product's own version name
  (`V10`, `Core 1.2`). History, the board, the web Versions page and the mod show it instead of the family ordinal,
  which stays for order. `waypoint version new --label V10` names the new project `<family>-v10`;
  `waypoint version label PROJECT LABEL --owner` (action `version_label`) sets or clears it. `--title` now sets the
  full title of a new version (it used to be the label).
- **UTF-8 CLI output**: the CLI prints UTF-8 on any console code page, so a title with an arrow no longer crashes
  `waypoint version history` on a cp1252 console.
- **Answers go back to the work** (`docs/FILE_CONTRACT.md`): the runner routes each answered question once
  (`runner/waypoint_runner/resume.py`). A warm asking session (active within `resume_cold_after_minutes`, a new
  setting, default 60) gets the answer appended by the mod (`hooks/answers.ts`). A cold, ended or unknown session is
  not woken: the node goes back to `ready` and a fresh worker starts with `resume <project> from Waypoint` and the
  answers; with no node, the lead clears the session and sends that line. Questions keep the asking `session`; the
  decision is stored as `resume` in the question file.
- **`waypoint brief <project>`** (`docs/CLI.md`): show the project brief (direction, decisions, open state; it
  replaces `decisions.md`), `--append` a line or `--set-file` to replace it; revision-checked (`--rev`, exit 3).

### Changed

- `docs/CLI.md` and `docs/ARCHITECTURE.md` describe the 1.2 data folder, runner and CLI instead of the retired
  engine on 127.0.0.1:8751.

## [1.2.0] - 2026-11-07

Shared data folder. The engine is now optional: the mod works without it and reads and writes the data folder
directly. Engine, CLI and mod all report 1.2.0. Waypoint API 1.1 is unchanged.

### Added

- **Data folder** (`docs/FILE_CONTRACT.md`, "Data folder"): projects, nodes, runs, claims, receipts, to-dos, actions
  and the other entities are small JSON files with a schema name and a revision counter, written whole through a
  temp file and a rename, with exclusive-create claims and one append-only log per writer
  (`docs/data/CONCURRENCY.md`). Schemas and samples are in `core/waypoint-data/`, with a Python twin.
- **Mod 1.2.0**: works with no engine running. Five panes (To-dos, Projects, Graph, Runs, Agents) registered
  together, actions written as files (answer, accept and reject, dispatch, retry and stop, toggle), dispatch pickup
  that injects the brief and writes the receipt, attention signals, and guards for owned files and spend.
- **Runner** (`docs/runner/RUNNER.md`): starts on demand, works through ready nodes, exits when idle, and refuses a
  new worker when memory is short.
- **Cloud nodes** (`docs/cloud/PROTOCOL.md`, `docs/cloud/SPIKE_RESULT.md`): a no-model node can run in GitHub
  Actions on a private repository and bring its receipt home through git.
- **Web page** that reads the data folder: a one-screen dashboard with To-dos, Projects, Graph and Runs.
- **DSH** standalone shell that reads the same folder and keeps chat transcripts in it.
- Cutover tool that reads the old `cc.sqlite` read-only, and writes a backup, manifest and receipt.

### Changed

- Floors are settings you set in Waypoint. An unset floor means 0% (use it or lose it); none is built in.
- Codex and Antigravity adapters (running the mod inside them) are not part of 1.2. Codex CLI stays a worker route.
- Waypoint now uses the data folder as the single place for state; `data/cc.sqlite` is only read at cutover.

## [1.1.1] - 2026-11-06

Engine only (the CLI and the mod stay at 1.1.0). Waypoint API 1.1 is unchanged.

### Fixed

- The transcript tailer read the oldest transcripts first, so on a busy machine (about 770 MB of transcripts in a
  week) live sessions took about 25 minutes to appear in the agent tree. It now reads the newest files first, and
  its first scan skips history older than 24 hours (the tree shows 24 hours); an older file is still read whole
  if it grows.

## [1.1.0] - 2026-11-06

Agent tree and zero-token bookkeeping. Waypoint API 1.1 (additive; nothing from 1.0 changed).
Engine, CLI and mod are all 1.1.0.

### Added

- **File contract** (`docs/FILE_CONTRACT.md`): agents write one receipt file (`waypoint.receipt.v1`) and, when
  blocked, question files (`waypoint.question.v1`) into `<folder>/.waypoint/`. The engine watches those folders
  (and takes `POST /api/receipt`), runs the node's own done-check, sets the status, releases the claim, closes
  linked computer to-dos, queues questions and logs the run. Code only; a receipt can never mark work done.
- **Agent trace**: `POST /api/trace`, `POST /api/trace/link`, `GET /api/trace/tree`, SSE `trace`. Token,
  context and cost-equivalent meters come from tailing Claude Code transcripts (subagents included); parent links
  come from the desktop app's own session records, so the orchestrator -> session -> subagent tree costs no tokens.
- **Workers**: `GET /api/worker/context` (the claimed node's brief, owned files and receipt path) and
  `POST /api/dispatch/headless` (owner only; `claude -p` or `codex exec`, streamed into the tree; switches and
  usage floors respected).
- Node field `files` (owned-file globs), carried through plan import/export and version roll-over.
- Claude usage bars fed by the mod's `$.session.usage` readings; `claude -p /usage` runs only when they are stale.
- `/app/` shows the live agent tree (read-only).
- CLI: `waypoint receipt write|list`, `waypoint tree`, `waypoint claim`, `waypoint release`.
- Mod 1.1.0: the Agents pane (live tree with tool, tokens, context bar, last line and transcript link) and
  dispatch (**Session** asks the lead to start a steerable worker session; **Headless** starts a headless worker);
  hooks that post trace events, usage and receipts; the claimed node's brief injected into the system prompt;
  owned-file, paid-route and usage-floor guards (the spend guard moved into the mod); question notifications;
  `@wp:` node mentions; a `waypoint-worker` subagent type; `userConfig` for the engine URL and port.
- Food-chain rules (`docs/ARCHITECTURE.md`): the engine does all bookkeeping; the orchestrator acts on events with
  judgement only; a session writes one receipt and at most one to-do; subagents report only to their parent.

## [1.0.1] - 2026-11-06

CLI only; the engine and the mod stay at 1.0.0. Waypoint API 1.0 is unchanged.

### Fixed

- `waypoint todo list` listed done and dropped to-dos mixed in with open ones, with no status mark. It now
  shows only open and handed-off to-dos, grouped like the app and the mod (*For you*, then *Queued for
  agents*), in position order.

### Added

- `waypoint todo list --all` lists every to-do with its status (`[open]`, `[handed off]`, `[done]`,
  `[dropped]`, `[merged]`).

## [1.0.0] - 2026-11-06

First release. Waypoint API 1.0.

### Added

- **Engine.** Local HTTP server on `127.0.0.1:8751`, Python standard library only, one SQLite file
  in WAL mode. JSON in and out. Answers carry `X-Waypoint-API: 1.0`; `GET /health` reports
  `api` and `api_version`.
- **Plans and the node graph.** Import a plan (JSON file or inline) of tasks with goals,
  dependencies, lanes and routing hints. The board shows waves (longest-path depth), display
  edges (transitive reduction), derived `ready` status and counts. It is row-encoded to stay small.
- **Done-checks.** Every task has a check written first: `command`, `files_exist`, `text_present`
  or `human`. Only a passing check or the owner sets `done`. Check results are counted per task.
- **Locked goals.** Goal text, checks and the project brief are hash-locked. Only the owner may
  change them. A plan imported by automation lands as a draft until the owner accepts it.
- **Revision-checked writes.** One revision number for plan and graph changes. A write with an old
  revision gets `409 {"error": "stale", "revision": N}` and changes nothing.
- **Owner and router credentials.** Writes need the owner token (`data/owner.token`), the router
  token (`data/engine.token`) or an allowlisted page origin. Token files are readable by the
  current user only. A `Host` check blocks DNS-rebinding writes.
- **Versions and families.** One product is one family; each version is a project. Start a new
  version at any time: every unfinished task is carried or skipped with a reason. Skipped ideas
  keep their goal text. History lists every version. Statuses `skipped` and `carried`.
- **To-dos.** Two lists: for you (`human`) and for the computer (`computer`). Links, deliverables,
  ordering, merge, hand-off to a plan, a project or a one-task run. Items linked to a task tick
  themselves when the task is done. Opened items archive after 24 hours.
- **Needs-you questions.** Likert (1 to 9), select and text questions. Options are never
  preselected. Only the owner answers.
- **Claims.** A session or worker claims a task while it works on it; stale claims are marked.
- **Runs and usage.** Queue runs with presets (`frugal`, `balanced`, `push`, `max`), sprints, a
  scheduler tick and Autonomous sequences. Usage per pool is `FRESH`, `STALE` or `UNKNOWN`; the
  engine never invents a number. Usage floors stop work before extra spend.
- **Live stream.** `GET /api/stream` sends Server-Sent Events: `state`, `run`, `question`, `todo`
  and more, with a heartbeat every 15 seconds.
- **Cloud sync inbox.** Cloud sessions write `waypoint.sync.v1` files to `sync/inbox/`; the engine
  imports them in order. Conflicts are recorded and never overwrite local state. Processed files
  move to `sync/processed/`.
- **Event log.** Every change writes one plain-English sentence with its actor.
- **Prompt packets.** `POST /api/prompt` builds a copy-and-paste brief for one task.
- **Snapshots.** Board snapshots in the repo (`plans/state/`) so a plan travels with git.
- **CLI (`waypoint`).** `--version`, `doctor`, `engine start|stop|status`, `board`, `node`,
  `status`, `done`, `todo`, `run`, `usage`, `version`, `sync`, `help`. `--json` and `--owner` on
  every command. Exit codes 0 to 6.
- **Claude Code mod.** Four panes: To-dos, Graph, Runs and Projects (with History). A one-line band
  above the prompt, a status line under it, and toasts for new items and questions. Live through
  the SSE stream with a 60-second poll as fallback. `/waypoint` commands.
- **Installer.** `install.ps1` and `uninstall.ps1` for one Windows user, no administrator rights.
  Optional autostart at logon.

[Unreleased]: https://github.com/Robert-Study/waypoint-agents/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/Robert-Study/waypoint-agents/releases/tag/v1.0.0
