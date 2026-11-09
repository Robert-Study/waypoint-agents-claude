# Waypoint API 1.1

This is the reference for the HTTP API of the Waypoint engine. The engine is the single source of
truth. The `waypoint` CLI, the Claude Code mod and any script you write use this API.

- [Basics](#basics)
- [Credentials and actors](#credentials-and-actors)
- [Errors](#errors)
- [Revision checking](#revision-checking)
- [Core routes](#core-routes)
- [Extension routes](#extension-routes)
- [Live stream (SSE)](#live-stream-sse)
- [Data model](#data-model)

## Basics

### Base URL

```
http://127.0.0.1:8751
```

The engine binds to `127.0.0.1` only. It refuses to bind any other address. The port is 8751 unless
you start the engine with another port (`waypoint engine start --port N`, or env `WAYPOINT_PORT`).
Clients read the base URL from env `WAYPOINT_URL`, else `http://127.0.0.1:<WAYPOINT_PORT or 8751>`.

### Versioning

- The API version is `1.1` (major 1, minor 1). The name is "Waypoint API 1". Version 1.1 (Waypoint Core 1.1)
  added the receipt, trace, worker-context and headless-dispatch routes, the node `files` field and the SSE
  kinds `receipt` and `trace`. Nothing from 1.0 changed.
- `GET /health` reports it as `"api": 1` and `"api_version": "1.1"`.
- Every JSON answer carries the header `X-Waypoint-API: 1.1`.
- A **minor** change is additive only: a new route, a new optional field in a request, a new field
  in an answer, a new SSE event kind. Clients must ignore fields and event kinds they do not know.
- A **major** change breaks something: a route removed or renamed, a field removed or retyped, a
  changed meaning. A major change gets a CHANGELOG entry.
- The CLI reads `api` from `/health` and stops with exit code 6 when the major is not 1. When the
  field is missing it treats the engine as API 1.

### Request and answer format

- Requests and answers are JSON (UTF-8). Send `Content-Type: application/json` on POST.
- A POST body must be one JSON object. The limit is 1 MiB (larger: 413).
- Query values are plain strings. Flags are on when they equal `1` (some also accept `true`/`yes`).
- `PUT`, `PATCH` and `DELETE` are treated exactly like `POST` on the same path.
- `OPTIONS` answers a CORS preflight (204). It allows `GET, POST, OPTIONS` and the headers
  `Content-Type, X-CC-Token`.
- Answers carry `Cache-Control: no-store`.
- Times are ISO 8601 strings in UTC (for example `2026-11-05T10:00:00+00:00`).

### Headers

| Header | Direction | Meaning |
|---|---|---|
| `X-Waypoint-API` | answer | API version, `1.1`. |
| `X-CC-Token` | request | Router credential (contents of `data/engine.token`). |
| `X-CC-Owner-Token` | request | Owner credential (contents of `owner.token`; see below for where it lives). |
| `Origin` | request | Browser origin. Only the allowlist below is accepted on writes. |
| `Host` | request | Must be `127.0.0.1`, `localhost` or `[::1]` with the engine's own port, on every request. |
| `Access-Control-Allow-Origin` | answer | Echoed only for an allowlisted `Origin`. |

## Credentials and actors

Reads (`GET`) need no credential.

Every state-changing request (`POST`, `PUT`, `PATCH`, `DELETE`) needs one credential. The engine
takes the actor from the credential, never from the body. It checks in this order:

1. The `Host` header must be `127.0.0.1:<port>`, `localhost:<port>` or `[::1]:<port>`, where `<port>`
   is the port the engine listens on. This applies to every request: GET, the SSE stream, POST and
   OPTIONS. Otherwise 403 `bad_host`. This blocks DNS-rebinding pages.
2. If an `Origin` header is present, it must be on the allowlist (`http://127.0.0.1:3080`,
   `http://localhost:3080`, the optional web page). An allowlisted origin is the **owner**. Any
   other origin gets 403 `origin_not_allowed`.
3. `X-CC-Owner-Token` equal to `data/owner.token` is the **owner** (credential `claude-mod`). A wrong
   value gets 403 `forbidden`.
4. `X-CC-Token` equal to `data/engine.token` is the **router**.
5. No credential: 403 `forbidden`.

Both token files are random (32 bytes, URL-safe) and readable by the current Windows user only.
They sit in the data directory, unless `CC_TOKEN_FILE` names the router token file; `owner.token` then
sits beside that file. The engine, the `waypoint` CLI and the Claude mod all use this one rule. The
engine writes a missing `owner.token` before it binds the port, so it exists as soon as `/health`
answers; a missing `engine.token` is written at start-up too. Never put a token in a URL or a log.

The settings file (`~/.waypoint/config.json`, or the file `WAYPOINT_CONFIG` names) is read as UTF-8
with or without a byte-order mark.

### Actors

| Actor | Who | May |
|---|---|---|
| `owner` | The person at the keyboard: the mod, `waypoint --owner`, the web page. | Everything. |
| `router` | Automation: workers, scripts, the CLI without `--owner`. | Report progress, add to-dos, ask questions, import plans as drafts, claim tasks. |

The router may never mark a task `done`, answer a question, change a goal, check or brief, accept a
plan, start a run, cancel a shutdown or turn power-off on. A plan the router imports lands as a
`draft` until the owner accepts it.

### Owner-only routes

A router request to one of these gets 403 `owner_only`:

`POST /api/node/status` with `status: "done"` · `POST /api/plan/accept` · `POST /api/run` ·
`POST /api/sprint` · `POST /api/sprint/update` · `POST /api/question/answer` ·
`POST /api/power/cancel` · `POST /api/version/family` · `POST /api/sync/import` ·
`POST /api/todos/update` · `POST /api/todos/click` · `POST /api/todos/handoff` ·
`POST /api/todos/merge` · `POST /api/project/archive` · `POST /api/project/unarchive` ·
`POST /api/node/run_owner` · `POST /api/sprints/plan` · `POST /api/sprints/update` ·
`POST /api/sprints/cancel` · `POST /api/sprints/start-now` · `POST /api/autonomous/settings` ·
`POST /api/autonomous/run` · `POST /api/autonomous/cancel` · `POST /api/autonomous/sprint` ·
`POST /api/agents/send` · `POST /api/agents/stop` · `POST /api/models/toggle` ·
`POST /api/models/gemini_keys/switch` · `POST /api/sync/mode` · `POST /api/sync/pull` ·
`POST /api/model_health/probe` · `POST /api/subagent_chats/reply` ·
`POST /api/state_snapshot/resume` · `POST /api/toggles/set` · `POST /api/plan/deep/start` ·
`POST /api/plan/deep/continue` · `POST /api/dispatch/headless`.

All other writes accept the owner or the router.

## Errors

Every error answer is a JSON object with `error` (a short code) and usually `detail` (one plain
sentence). Some add fields, such as `revision` on a stale write.

```json
{"error": "bad_request", "detail": "missing project, id"}
```

| Status | `error` codes | When |
|---|---|---|
| 400 | `bad_request`, `bad_plan` | Missing or bad field, bad JSON, bad plan file. |
| 403 | `forbidden`, `owner_only`, `origin_not_allowed`, `bad_host`, `goal_locked`, `not_allowed`, `disabled` | Credential missing or refused; action not allowed for this actor. |
| 404 | `not_found` | Unknown route, project, node, run, to-do or session. |
| 409 | `stale` (with `revision`) | The `revision` you sent is not the current one. |
| 409 | `integrity`, `claimed`, `sprint_closed`, `confirm`, `done`, `busy`, `closed`, `already_handed_off`, `already_running`, `project_busy`, `not_runnable`, `no_match`, `usage_floor`, `confirm_usage`, `confirm_codex`, `agent_disabled`, `refused`, `not_resumable` | The request conflicts with the current state. |
| 413 | `too_large` | Body over 1 MiB, or a message over its limit. |
| 500 | `internal` | Unexpected engine error. `detail` names the exception. |
| 503 | `unavailable` | An optional module or tool is not present or not running. |

## Revision checking

The engine keeps one integer, the **revision**. It goes up by one on every change to a plan or a
graph (task status, plan import, version roll-over). Telemetry, events, to-dos and usage never bump
it.

Writes that change a plan need the `revision` you last read (an integer). When it is not the
current revision, the engine changes nothing and answers:

```json
HTTP 409
{"error": "stale", "revision": 42}
```

Read again (`GET /api/board`), check that your change still makes sense, and send it with the new
revision. The CLI exits with code 3 on a stale write (`--retry` re-reads once and sends again).

Routes that need `revision`: `POST /api/node/status`, `POST /api/plan/import`,
`POST /api/version/new`, `POST /api/version/family`, `POST /api/version/decide`. It is optional on
`POST /api/plan/accept` and `POST /api/todos/handoff` (`to: "project"`).

---

## Core routes

These live in `engine/cc_engine/api.py`. Built-in routes always win over an extension route with
the same path.

### `GET /health`

Also `GET /api/health`. Is the engine up, and which version?

```json
{"ok": true, "service": "cc-engine", "version": "1.0.0", "api": 1, "api_version": "1.1", "revision": 12}
```

### `GET /api/board`

Query: `project` (default: the default project). The graph of one project, kept small (under
50 KB at 300 tasks). Archived projects stay readable.

Nodes are **row-encoded**: `fields` names the columns and each entry of `nodes` is a list in that
order. Trailing columns that are null are left off a row; treat a missing column as null.

| Column | Meaning |
|---|---|
| `id`, `title` | Task id; title cut to 80 characters. |
| `status` | Stored status (see [status set](#status-set)). |
| `display_status` | `status`, except `not_started` becomes `ready` when every dependency is `done`. Nothing in a draft shows as ready. |
| `deps` | Ids this task depends on. |
| `wave` | Longest-path depth from the roots (0-based). |
| `pool`, `model`, `effort` | Routing hints. |
| `attempts`, `actor`, `lane` | Tries so far, last actor, lane name. |
| `checks` | `[passed, failed, last]` done-check results, or null when the check never ran. |
| `recommended` | The planner's first-choice model id. |
| `claim` | `{by, worktree, cwd, environment, since, refreshed_at, stale}` while someone works on it. |
| `kind` | `splitter`, `code` or `gate`; missing means `worker`. |
| `crosses` | What crosses each incoming edge. |

```json
{"project": {"id": "demo", "title": "Demo", "cwd": null, "family": null, "family_title": null,
             "version": null, "status": "active", "draft": false, "archived": false},
 "revision": 1,
 "fields": ["id", "title", "status", "display_status", "deps", "wave", "pool", "model", "effort",
            "attempts", "actor", "lane", "checks", "recommended", "claim", "kind", "crosses"],
 "nodes": [["A1", "Write the parser", "not_started", "ready", [], 0, null, null, null, 0],
           ["A2", "Test the parser", "not_started", "not_started", ["A1"], 1, null, null, null, 0]],
 "edges": [["A1", "A2"]],
 "counts": {"not_started": 1, "ready": 1, "running": 0, "submitted": 0, "done": 0, "error": 0,
            "needs_you": 0, "skipped": 0, "carried": 0, "total": 2},
 "integrity_ok": true, "integrity": []}
```

`edges` is the transitive reduction (display edges). `integrity` lists locked text that no longer
matches its hash. Errors: 404 when the project does not exist.

### `GET /api/node`

Query: `project`, `id`. One task in full, with its last 10 events and its history.

```json
{"node": {"project_id": "demo", "id": "A1", "title": "Write the parser", "goal": "Parse the input file.",
          "goal_hash": "bec8...", "check_hash": "9918...", "check": {"type": "files_exist", "paths": ["out.txt"]},
          "deps": [], "owner": null, "lane": null, "pool": null, "model": null, "effort": null,
          "recommended": null, "status": "not_started", "attempts": 0, "actor": null, "evidence": null,
          "updated_at": "2026-11-05T10:00:00+00:00", "display_status": "ready", "checks": null, "claim": null},
 "events": [], "revision": 1, "history": []}
```

Errors: 404 for an unknown project or node.

### `GET /api/projects`

Query: `all=1` to include archived projects. Each project adds its family fields.

```json
{"projects": [{"id": "demo", "title": "Demo", "brief": "A sample plan.", "goal_hash": "7128...",
               "cwd": null, "created_at": "2026-11-05T10:00:00+00:00", "archived": 0, "status": "active",
               "family": null, "family_title": null, "version": null, "versions": 0}]}
```

### `GET /api/history`

Query: `family` or `project` (one is required). The versions of one product and its skipped ideas.
A project with no family answers with itself as the only version (`family.id` null).

```json
{"family": {"id": "demo", "title": "Demo"}, "current": "demo-v2",
 "versions": [{"project": "demo", "title": "Demo", "version": 1, "archived": true, "status": "active",
               "created_at": "2026-11-05T10:00:00+00:00",
               "counts": {"done": 0, "open": 0, "skipped": 1, "carried": 1}},
              {"project": "demo-v2", "title": "Demo v2", "version": 2, "archived": false, "status": "active",
               "created_at": "2026-11-05T11:00:00+00:00",
               "counts": {"done": 0, "open": 2, "skipped": 0, "carried": 0}}],
 "skipped": [{"project": "demo", "version": 1, "id": "A2", "title": "Test the parser",
              "goal": "Add tests.", "reason": "not needed now", "at": "2026-11-05T11:00:00+00:00"}]}
```

`versions` is oldest first. `skipped` is newest version first. Errors: 400 without `family` or
`project`; 404 when unknown.

### `GET /api/events`

Query: `project` (optional), `since` (sequence number, default 0), `latest=N` (newest N, up to
5000, in ascending order), `auto=1` (same as `latest=500`). At most 500 events per answer.

```json
{"events": [{"seq": 1, "at": "2026-11-05T10:00:00+00:00", "project_id": "demo", "node_id": null,
             "kind": "import", "actor": "owner", "summary": "Imported the plan 'Demo' with 2 tasks.",
             "data": {"nodes": 2, "reset": [], "draft": false, "revision": 1}}]}
```

Errors: 400 when `since` or `latest` is not an integer.

### `GET /api/usage`

How much of each subscription pool is left. The engine never invents a number: a pool it cannot
read is `UNKNOWN` with `remaining_percent: null`.

```json
{"pools": [{"pool": "codex", "remaining_percent": 62.0, "observed_at": "2026-11-05T09:00:00+00:00",
            "resets_at": "2026-11-05T14:00:00+00:00", "status": "FRESH", "source": "telemetry"}],
 "clock": {"next": null, "resets": []}}
```

`status` is `FRESH`, `STALE` or `UNKNOWN`. `clock` lists upcoming resets.

### `GET /api/runs`

The newest 50 runs, newest first.

```json
{"runs": [{"id": "run-20261105100000-1a2b", "project_id": "demo", "status": "queued",
           "settings": {"preset": "balanced"}, "created_at": "2026-11-05T10:00:00+00:00",
           "started_at": null, "ended_at": null}]}
```

### `GET /api/questions`

Query: `open=1` (only unanswered), `project` (optional). Needs-you questions.

```json
{"questions": [{"id": "q-5a6e390fe0", "project_id": "demo", "node_id": "A2", "kind": "select",
                "prompt": "Which test runner?", "scale": null, "purpose": "generic",
                "options": [{"value": "pytest", "label": "pytest"}, {"value": "unittest", "label": "unittest"}],
                "proposal": null, "allowed": ["ignore", "answer"], "answer": null, "answer_json": null,
                "answered_at": null, "captured": 0}]}
```

### `GET /api/brain`

Query: `refresh=1` to rebuild. A graph of notes and files (optional module). Answer:
`{nodes: [{id, label, kind, cluster, path, provenance, weight}], links: [{source, target, kind}]}`.
Errors: 503 `unavailable` when the module is not present.

### `GET /api/learn`

Concept graph, next step and stories (optional module). Answer:
`{concept_graph, next_step, stories}`. Errors: 503 when the module is not present.

### `GET /api/stream`

Server-Sent Events. See [Live stream](#live-stream-sse).

### `POST /api/node/status`

Set one task's status. Owner or router; only the owner may set `done`.

```json
{"project": "demo", "id": "A1", "status": "submitted", "evidence": "out.txt written", "revision": 1}
```

```json
{"ok": true, "revision": 2}
```

Errors: 400 for a missing field, an unknown status, or `skipped`/`carried` (only a version
roll-over sets those); 403 `owner_only` for `done` by the router; 404 unknown node; 409 `stale`.

### `POST /api/plan/import`

Import a plan file or an inline plan. Body: `{path, revision}` (an existing `.json` file) or
`{plan: {...}, revision}`. A router import lands as a draft.

```json
{"ok": true, "project": "demo", "revision": 3, "status": "draft", "runnable": false}
```

Errors: 400 `bad_request` (path not an existing `.json` file), 400 `bad_plan` (invalid plan), 403
`goal_locked` (the router tried to change a goal or brief), 409 `stale`.

### `POST /api/plan/accept`

Owner only. Accept a drafted plan so its tasks may run. Body: `{project, revision?}`.

```json
{"ok": true, "project": "demo", "status": "active", "revision": 4}
```

Errors: 404 unknown project; 409 `integrity` (locked text no longer matches its hash, with
`mismatches`); 409 `stale`.

### `POST /api/run`

Owner only. Queue a run for one project. Body: `{project, settings}`. `settings.preset` is one of
`frugal`, `balanced` (default), `push`, `max`; other keys tune pools, effort and concurrency.

```json
{"ok": true, "id": "run-20261105100000-1a2b", "status": "queued",
 "settings": {"raw": {"preset": "balanced"}, "expanded": {"model_ladder": {"...": "..."}, "effort": "medium", "max_concurrency": 2, "...": "..."}},
 "requested_by": "owner", "runnable": true}
```

Errors: 400 (bad settings, unknown preset); 403 `owner_only`; 404 unknown project.

### `POST /api/run/cancel`

Cancel a run and stop its live jobs. Body: `{id}`.

```json
{"ok": true, "id": "run-20261105100000-1a2b", "status": "cancelled"}
```

Errors: 404 unknown run.

### `POST /api/sprint`

Owner only. Add a sprint to a run. Body: `{run_id, pool, start_at, end_at?}` (ISO time or epoch
seconds).

```json
{"ok": true, "id": "s-0a1b2c3d4e", "start_at": "2026-11-05T14:01:00+00:00"}
```

Errors: 400 bad time; 404 unknown run.

### `POST /api/sprint/update`

Owner only. Move a sprint. Body: `{id, start_at}`. Answer: `{ok, id, start_at}`. Errors: 400, 404.

### `POST /api/question`

Ask a needs-you question. Body: `{prompt, project?, node?, kind?, options?, scale?, allowed?,
proposal?, purpose?}`. `kind` is `likert`, `select` or `text` (default). Options never carry a
preselected flag.

```json
{"ok": true, "question": {"id": "q-5a6e390fe0", "kind": "select", "prompt": "Which test runner?", "answer": null}}
```

Errors: 400 bad kind, missing prompt.

### `POST /api/question/answer`

Owner only. Body: `{id, answer}` where `answer` is `{type, value}` or a bare value; or
`{id, type, value}`. `type` is one of `accept`, `edit`, `reply`, `ignore`, `answer`. `ignore`
closes the question with no side effect.

```json
{"ok": true, "question": {"id": "q-5a6e390fe0", "answer": {"type": "answer", "value": "pytest"},
                          "answered_at": "2026-11-05T10:05:00+00:00"}}
```

Errors: 400 missing answer or a type the question does not allow; 404 unknown question.

### `POST /api/learn/record`

Record a learning outcome. Body: `{concept, outcome}`. Answer: `{ok, result}`. Errors: 503 when the
module is not present.

### `POST /api/power/cancel`

Owner only. Cancel a planned shutdown (`shutdown /a`). Body: `{project?}`. Answer: `{ok}` (false
when nothing was pending).

### `POST /api/prompt`

Build a copy-and-paste prompt packet for one task: goal, brief, done-check, dependency evidence and
rules. Body: `{project, id}`.

```json
{"prompt": "# Task A1: Write the parser\nProject: Demo (demo)\n...\n## Rules\n- Do not change the goal.\n..."}
```

Errors: 404 unknown project or node.

### `POST /api/version/new`

Owner or router. Start the next version of a product. Body:
`{family? | project?, title?, id?, decisions?, revision}`. `decisions` maps a task id to
`{"action": "carry"}` or `{"action": "skip", "reason": "..."}`. Undecided unfinished tasks are
carried. The new version gets a discovery task `Q1`. The old version is archived.

```json
{"ok": true, "project": "demo-v2", "version": 2, "carried": ["A1"], "skipped": [{"id": "A2", "reason": "not needed now"}], "revision": 4}
```

Errors: 400 (skip without a reason; naming an earlier version: "only the current version (<id>)
can roll over"); 404 unknown family or project; 409 `stale`.

### `POST /api/version/family`

Owner only. Group existing projects as versions of one family. Body:
`{family, title, members: [ids, oldest first], revision}`. Archives every member but the last.
Idempotent. Errors: 400, 404, 409 `stale`.

### `POST /api/version/decide`

Owner or router. Carry or skip unfinished tasks of an earlier (archived) version. Body:
`{project, decisions, revision}`.

```json
{"ok": true, "carried": [{"from": "A3", "to": "A3"}], "skipped": [{"id": "A4", "reason": "out of scope"}], "revision": 6}
```

Errors: 400 when the project is the current version or a task is not unfinished; 409 `stale`.

---

## Extension routes

Extension modules add routes through a `ROUTES` table. The engine loads them lazily and re-scans
every 10 seconds. A module that is missing or fails to import is skipped; its routes then answer
404. Extension writes pass the same credential rule as core writes.

### To-dos (`todo`)

A to-do is a line for the computer (section `computer`) or for you (section `human`). Statuses:
`open`, `handed_off`, `done`, `dropped`. Text is 1 to 2000 characters. A to-do linked to a task
(`node`) is ticked by the engine when that task is `done`. A to-do first opened 24 hours ago or more
is archived.

#### `GET /api/todos`

Query: `archived=1` to include archived items.

```json
{"todos": [{"id": "td-e9431c", "text": "Review the draft", "status": "open", "section": "human",
            "position": 1, "link": null, "node": null, "deliverables": [], "handoffs": [], "done": false,
            "created_by": "router", "done_by": null, "merged_into": null, "first_clicked_at": null,
            "archived_at": null, "archive_due_at": null,
            "created_at": "2026-11-05T10:00:00+00:00", "updated_at": "2026-11-05T10:00:00+00:00"}],
 "sections": {"computer": [], "human": ["..."]},
 "counts": {"open": 1, "total": 1, "human_open": 1, "computer_open": 0, "archived": 0}}
```

#### `POST /api/todos`

Owner or router. Add a to-do. Body: `{text, section?, position?, link?, node?: {project, id},
deliverables?: [{label, ...}], triage?}`. A `computer` item with no `node` is handed to the best
matching project, or to a one-task run, unless `triage` is false.

```json
{"ok": true, "todo": {"id": "td-e9431c", "text": "Review the draft", "section": "human", "status": "open"}}
```

Errors: 400 empty or too long text, bad section, bad node.

#### `POST /api/todos/update`

Owner only. Body: `{id, text?, status?, done?, section?, position?, link?, node?, deliverables?}`.
Answer: `{ok, todo}`. Errors: 400 nothing to change or bad value; 404 unknown to-do.

#### `POST /api/todos/click`

Owner only. Record the first click on a to-do (starts its 24-hour archive clock). Body: `{id}`.
Answer: `{ok, todo}`. Errors: 404.

#### `POST /api/todos/reorder`

Owner or router. Body: `{section, ids: [...]}`. Sets the order and section of the listed items.
Answer: `{ok, section, todos}`. Errors: 400 bad section or `ids` not a list; 404 unknown id.

#### `POST /api/todos/handoff`

Owner only. Send a to-do on. Body: `{id, to, again?, project?, revision?}`. `to` is `plan` (start a
planning session), `project` (add one task to a project) or `run` (make a one-task project).

```json
{"ok": true, "result": {"to": "project", "project": "demo", "node": "T1", "title": "Review the draft",
                        "handoff": {"to": "project", "at": "2026-11-05T10:00:00+00:00", "by": "owner"}},
 "todo": {"id": "td-e9431c", "status": "handed_off"}}
```

Errors: 400 bad `to`; 404; 409 `closed`, `already_handed_off`, `not_runnable`, `no_match`, `stale`;
503 when planning is not available.

#### `POST /api/todos/merge`

Owner only. Body: `{keep, merge: [ids]}`. The merged items become `dropped` with `merged_into`.
Answer: `{ok, todo, merged}`. Errors: 400, 404.

### Claims (`claims`)

A claim says that a session or worker is working on a task now. Claimable statuses: `not_started`,
`ready`, `error`, `needs_you`.

#### `POST /api/claim`

Body: `{project, node (or id), by, worktree?, cwd?, environment?}`. Claiming again by the same
holder refreshes the claim.

```json
{"ok": true, "claim": {"by": "worker-1", "worktree": null, "cwd": null, "environment": "subagent",
                       "since": "2026-11-05T10:00:00+00:00", "refreshed_at": "2026-11-05T10:00:00+00:00",
                       "stale": false}, "refreshed": false}
```

Errors: 400 missing field or bad name (`owner` and `router` are reserved); 404 unknown project; 409
`claimed` (someone else holds it).

#### `POST /api/release`

Body: `{project, node (or id), by?}`. A task still `running` under the claim returns to
`not_started`. Answer: `{ok, released, status, by}`.

#### `GET /api/claims`

Query: `project`. Answer: `{project, claims: {<node id>: claim}}`.

### Node history (`history`)

#### `GET /api/node/history`

Query: `project`, `id` (or `node`). Answer: `{project, node, history: [...]}`, a clean list of what
happened to the task. Errors: 404.

### Projects (`owner_runs`)

#### `POST /api/project/archive`

Owner only. Body: `{project}`. Archiving cancels the project's active runs and closes its open
questions. Answer: `{ok, project, archived, cancelled_runs, closed_questions}`. Errors: 400, 404.

#### `POST /api/project/unarchive`

Owner only. Body: `{project}`. Same answer as archive. Errors: 400, 404.

#### `POST /api/node/run_owner`

Owner only, and only without `X-CC-Token`. Start a Codex run on one task. Body:
`{project, id, route: "codex", confirm: true}`. Answer: `{ok, run_id, route, status, pid}`.
Errors: 400 `bad_route`; 403 `owner_only`, `disabled`; 404 unknown task; 409 `confirm`, `busy`,
`done`, `usage_floor`; 503 `unavailable` (the codex CLI is not installed).

### Cloud sync inbox (`cloud_sync`)

A cloud session cannot reach the engine. It writes `sync/inbox/*.json` files (schema
`waypoint.sync.v1`, written by `waypoint sync ...`) to a private repo. The engine imports them.

#### `POST /api/sync/import`

Owner only. Body: `{repo?}` (default: the configured repo). Files are read in name order and moved
to `sync/processed/`. A conflict is recorded, never applied.

```json
{"files": 1, "applied": 2, "skipped": 0, "conflicts": 1, "details": ["..."]}
```

### Repository sync (`sync`)

Keeps local git clones up to date with their remote. Modes: `Cloud`, `Local` (default).

#### `GET /api/sync/status`

Query: `fetch=1` to fetch first. Answer: `{mode, repos: [{name, state, ...}], needs_merge, last,
checked_at}`. `state` `diverged` means a merge is needed.

#### `POST /api/sync/mode`

Owner only. Body: `{mode}`. Answer: the status plus `{ok, mode}`. Errors: 400 bad mode.

#### `POST /api/sync/open`

Owner or router. Fetch and fast-forward every clean repo that is strictly behind (at most every 10
minutes; `force: true` from the owner skips the wait). Answer: the status plus `{ok}`.

#### `POST /api/sync/pull`

Owner only. Body: `{repos?: [names]}`. Fast-forward or merge each repo the remote is ahead of.
Answer: `{ok, ...}` with what was updated, skipped or in conflict. Errors: 400; 404 unknown repo.

### Sprints and Autonomous runs (`sprints`)

A sprint is a time window in which one pool (for example `claude_code` or `codex`) works on a run.

#### `GET /api/sprints/timeline`

Query: `hours` (6 to 168, default set by the engine), `project?`.

```json
{"now": "2026-11-05T10:00:00+00:00", "from": "2026-11-05T10:00:00+00:00", "to": "2026-11-06T10:00:00+00:00",
 "lookback_from": "2026-11-05T04:00:00+00:00", "hours": 24, "pools": [], "sprints": [], "runs": [],
 "next_up": [], "recently_finished": [], "next_wake_at": null}
```

Errors: 400 when `hours` is not an integer.

#### `POST /api/sprints/plan`

Owner only. Body: `{project, pool, start_at?, duration_min?, preset?}`. Creates one sprint run.
Answer: `{ok, run_id, sprint_id, start_at, end_at, aligned}`. Errors: 400, 404.

#### `POST /api/sprints/update`

Owner only. Body: `{id, start_at, duration_min?}`. Answer: `{ok, id, start_at, end_at}`.
Errors: 400 (missing field, time in the past), 404, 409 `sprint_closed`.

#### `POST /api/sprints/cancel`

Owner only. Body: `{id}`. Answer: `{ok, id, run_cancelled}`. Errors: 400, 404, 409
`sprint_closed` (already finished or cancelled).

#### `POST /api/sprints/start-now`

Owner only. Body: `{id}`. Moves the sprint to now and starts a scheduler tick. Answer:
`{ok, id, start_at, end_at, tick_started}`.

#### `GET /api/autonomous`

The Autonomous controls: `{settings, models, models_source, usage, runs: [{id, project_id,
project_title, status, row, waiting}], running, kinds}`.

#### `POST /api/autonomous/settings`

Owner only. Body: `{settings}`. Answer: `{ok, settings}` (normalised). Errors: 400.

#### `POST /api/autonomous/run`

Owner only. Body: `{settings}` with a `sequence` of rows of projects. Creates one queued run per
project; later rows wait for earlier rows. Answer: `{ok, runs: [{id, project, row, after_runs}],
tick_started, settings}`. Errors: 400 (empty sequence, no model); 409 `already_running`,
`project_busy`.

#### `POST /api/autonomous/cancel`

Owner only. Stops every Autonomous run. Answer: `{ok, cancelled: [run ids]}`.

#### `POST /api/autonomous/sprint`

Owner only. Body: `{kind, confirm?}`. `kind` is `claude`, `codex`, `dual` or `auto_dual`. Adds
sprints at the next free time. Answer: `{ok, sprints}`. Errors: 400 bad kind; 403 `disabled` (pool
switched off); 409 `confirm_codex` (send `confirm: true` after checking usage).

#### `POST /api/autonomous/prompt`

Owner or router. Body: `{settings?}`. Builds a prompt that describes the Autonomous plan. Answer:
`{prompt}`. Changes nothing.

### Agents and chats (`agents`, `subagent_chats`)

#### `GET /api/agents`

Query: `refresh=1` to re-check availability. Answer: `{agents: [{id, name, glyph, route, note,
available, availability_detail, needs_confirm, busy, floor_percent, models, efforts, ...usage}],
connections, folders, default_folder}`.

#### `GET /api/agents/sessions`

Query: `agent?`. Answer: `{sessions: [{id, agent, ..., active_turn}]}` (newest 200).

#### `GET /api/agents/messages`

Query: `session`. Answer: `{session, messages, active}`. Errors: 400 missing session; 404.

#### `POST /api/agents/send`

Owner only. Body: `{agent, message, session_id?, cwd?, model?, effort?, confirm?}`. Starts one turn
in the background. Answer: `{turn_id, session_id, agent}`. Errors: 400 unknown agent or empty
message; 404 unknown session; 409 `agent_disabled`, `busy`, `usage_floor`, `confirm_usage`; 413
message over 30000 characters; 503 `unavailable` (the agent's tool is not installed).

#### `POST /api/agents/stop`

Owner only. Body: `{turn_id}`. Answer: `{stopped, turn_id}` or `{stopped: false, detail}`.

#### `GET /api/subagent_chats`

Query: `agent?`. Answer: `{chats: [{id, agent, title, cwd, prompt, output, steps, files, resumable,
resume_note, ...}]}`.

#### `POST /api/subagent_chats/reply`

Owner only. Body: `{id, message, model?, effort?, confirm?}`. Continues a worker's chat as an agent
session. Answer: the `POST /api/agents/send` answer plus `chat`. Errors: 400, 404, 409
`not_resumable`.

### Models, switches and picking (`models`, `toggles`, `pick`, `model_health`)

#### `GET /api/models`

The model registry: `{models: [...], kinds: ["chat", "subagent", "agent", "manual"], efforts,
state_file}`. Each model row names its provider, kind, whether it is enabled and runnable, and its
usage.

#### `GET /api/models/lookup`

Query: `provider`, `model`. Answer: `{model}`. Errors: 404.

#### `POST /api/models/toggle`

Owner only. Body: `{id}` or `{ids: [...]}`, and `enabled` (true or false). Answer:
`{ok, models, model?}`. Errors: 400; 404 unknown id.

#### `GET /api/models/gemini_keys`

Which Google AI Studio key slot is active, and which slots exist. Key values are never returned.

#### `POST /api/models/gemini_keys/switch`

Owner only. Body: `{slot}`. Answer: the key status plus `{ok}`. Errors: 400; 409 `refused`.

#### `GET /api/toggles`

The on/off switches for agents, routes and add-ons: `{toggles, enabled, aliases, disabled,
defaults_off, cascade_kinds, addons}`. A switched-off item is never called.

#### `POST /api/toggles/set`

Owner only. Body: `{id, enabled}`. Answer: `{ok, toggle}`. Errors: 400.

#### `GET /api/pick`

Query: `kind` (`task` default, `chat`, `plan`), `project?`, `id?`. Picks a model for the job from
the enabled, working models. Answer: `{model, effort, why, ...}`; `model` is null when nothing fits.
Errors: 400 bad kind.

#### `GET /api/model_health`

Answer: `{routes, lists, updated_at, running, probe_every_s}`, the last probe of the free chat
routes.

#### `POST /api/model_health/probe`

Owner only. Body: `{force?}`. Starts a probe in the background. Answer: `{ok, started, running}`.

### Planning (`planner`, `deep_plan`, `failure_doctor`)

#### `POST /api/planner/start`

Owner or router. Body: `{title, goal, cwd?}`. Starts a planning session (questions first, then a
draft). Answer: `{ok, session}`. Errors: 400 (missing title or goal, folder does not exist).

#### `POST /api/planner/draft`

Owner or router. Body: `{session (or id), note?}`. Asks the planner for the draft. Answer:
`{ok, session}`.

#### `GET /api/planner/session`

Query: `id` (or `session`). Answer: `{id, status, title, goal, cwd, phase, project, runnable,
questions, answered, draft, problems, error}`. Errors: 400 missing id.

#### `GET /api/planner/sessions`

Answer: `{sessions: [{id, title, status, project, created_at}]}`.

#### `GET /api/plan/deep`

Query: `id` (or `session`). One deep-plan session: `{id, status, title, goal, cwd, ...}`. Errors:
400 missing id.

#### `GET /api/plan/deep/sessions`

Answer: `{sessions: [{id, title, status, model, project, created_at}]}`.

#### `GET /api/plan/deep/options`

Answer: `{models, efforts, default}`, the choices for a deep plan.

#### `GET /api/plan/deep/brief`

Query: `project`, `id`. A task brief: `{project, id, text, files, ...}`. Errors: 400.

#### `POST /api/plan/deep/start`

Owner only. Body: `{title, goal, cwd?, model?, effort?, critic?, research_model?, candidates?}`.
Answer: `{ok, session}`. Errors: 400.

#### `POST /api/plan/deep/continue`

Owner only. Body: `{id (or session)}`. Answer: `{ok, session}`. Errors: 400.

#### `POST /api/check/draft`

Owner or router. Body: `{goal}`. Drafts a done-check for a goal and changes nothing. Answer:
`{check, machine_checkable}`.

### Snapshots (`state_snapshot`)

A snapshot is a JSON copy of a project board kept in the repo (`plans/state/<project>.json`), so
the plan travels with git.

#### `GET /api/state_snapshot`

Answer: `{root, following, files}`.

#### `POST /api/state_snapshot/write`

Owner or router. Body: `{project?}` (default: all projects). Answer: `{ok, written: [paths]}`.
Errors: 400, 404.

#### `POST /api/state_snapshot/resume`

Owner only. Body: `{path}` or `{snapshot: {...}}`. Imports a snapshot. It never undoes local
progress. Answer: `{ok, project, created, restored, ...}`. Errors: 400.

### Briefing and constraints (`brief`, `constraints`, `brain_overlaps`)

#### `GET /api/brief`

Query: `since?` (ISO time), `hours?`. A short report of what happened: `{since, until, hours,
paragraph, done, failed, needs_you, done_items, failed_items, usage, triage}`. Errors: 400.

#### `GET /api/constraints`

Query: `project?`. Answer: `{project, constraints: [...]}`. Errors: 404 unknown project.

#### `GET /api/brain/overlaps`

Query: `refresh=1` to rebuild. Overlaps found between notes. Answer: the findings plus
`source_count`.

### Learning and interview practice (`learn`)

#### `GET /api/interview`

Answer: a snapshot of the practice state.

#### `GET /api/interview/prep`

Query: `q` (or `text`, or `company` and `role`). Answer: practice material for that text.
Errors: 400 when empty.

#### `GET /api/learn/live_prompt`

Query: `company?`, `role?`, `q?`. Answer: a prompt to use in a live session.

#### `GET /api/interview/live_prompt`

Same as `GET /api/learn/live_prompt`.

#### `GET /api/interview/question`

Query: `id`. Answer: the next step for that question. Errors: 400 missing id.

#### `POST /api/interview/rate`

Owner or router. Body: `{question (or concept), rating (or outcome)}`. Answer: `{ok, result}`.
Errors: 400.

### Receipts and question files (`receipts`, 1.1)

Agents write files; the engine does the bookkeeping. The file format and the processing rules are in
[FILE_CONTRACT.md](FILE_CONTRACT.md) (`waypoint.receipt.v1`, `waypoint.question.v1`). The engine also watches
`<folder>/.waypoint/receipts/` and `.../questions/` under project, claim and traced-session folders and the
inbox `~/.waypoint/inbox/`, every 3 seconds, so these routes are optional for an agent.

#### `POST /api/receipt`

Body: `{path?, receipt?, cwd?, session?}`. Either `path` (a `.json` file inside a `.waypoint` folder) or the
`receipt` object. Owner or router. Each content is processed once (by SHA-256).

```json
{"ok": true, "id": "3f2a...", "status": "accepted"}
```

`status` is `accepted`, `duplicate` or `invalid` (with `reason`). A `done` receipt runs the node's own
done-check; it never sets `done` by itself. Errors: 400 no receipt, or a path outside a `.waypoint` folder.

#### `GET /api/receipts`

Query: `limit` (default 50). Answer: `{receipts: [{id, path, name, project, node, session, status, claimed,
outcome, summary, reason, ingested_at}]}`, newest first.

### Agent trace (`trace`, 1.1)

A zero-token view of every agent: sessions, their subagents and headless workers. The mod's hooks post
events; the engine also tails `~/.claude/projects/**/*.jsonl` for token, context and cost-equivalent meters
and reads the desktop app's session records for parent links. No model is called.

#### `POST /api/trace`

Body: `{events: [{event, session_id, agent_id?, parent?, cwd?, title?, tool?, model?, transcript?, usage?, at?}]}`,
at most 50 events. `event` is `session.start`, `session.end`, `turn.complete`, `tool.call`,
`subagent.start`, `subagent.stop` or `heartbeat`; others are ignored. `usage` may carry `context_tokens`,
`context_window`, `cost_usd` and `rate_limits` (`[{kind, percentUsed, resetsAt}]`, which feeds the Claude rows
of `GET /api/usage`). Owner or router. Answer: `{ok, accepted}`.

#### `POST /api/trace/link`

Body: `{child, parent}`: an explicit parent link, for agents whose parent the desktop data does not show.
Answer: `{ok, child, parent}`.

#### `GET /api/trace/tree`

Query: `since` (ISO time; default the last 24 hours; running agents are always included). Answer:

```json
{"agents": [{"id": "5b1c...", "kind": "session", "parent": null, "session_id": "5b1c...", "title": "Lead",
             "project": null, "node": null, "cwd": "C:\work", "status": "running", "tool": "Bash",
             "tokens_in": 1200, "tokens_out": 3400, "cache_read": 880000, "context": 61000,
             "context_window": 1000000, "cost_eq": 0.41, "model": "claude-opus-5-5",
             "last_line": "Tests pass.", "transcript": "C:\Users\you\.claude\projects\...\5b1c....jsonl",
             "started_at": "...", "updated_at": "..."}],
 "roots": ["5b1c..."], "at": "2026-11-06T20:00:00+00:00"}
```

`kind` is `session`, `subagent` (id `<session>/<agentId>`) or `headless` (id `headless-<id>`). `status` is
`running`, `idle`, `waiting`, `done`, `error` or `ended`. `cost_eq` is a list-price estimate, not a bill.

### Workers (`headless`, 1.1)

#### `GET /api/worker/context`

Query: `session`, `cwd`, and optionally `node` (`<project>/<node>`) with `by`. The node claimed by that session
(claim `by`) or whose claim folder is `cwd`:

```json
{"node": {"project": "docs-v2", "id": "B3", "title": "API guide", "goal": "...", "check": {"type": "command", "run": "..."},
          "files": ["core/docs/API.md"]},
 "receipt_path": "C:\work\.waypoint\receipts\docs-v2.B3.json", "claimed_by": "worker-1"}
```

No claim: `{node: null, receipt_path: null, claimed_by: null}`. The mod reads this once per session to inject
the brief and guard owned files.

With `node`, only that node is answered, and only when its live claim is held by `by` or by `session`. A
headless worker sends `node` and `by` from its `WAYPOINT_NODE` and `WAYPOINT_CLAIM_BY` variables. The folder
match skips claims whose `by` starts with `headless-` and only answers when exactly one live claim has that
folder. When several do, the answer is `node: null` with `ambiguous: true`.

#### `POST /api/dispatch/headless`

Owner only. Body: `{project, node, engine: "claude"|"codex", parent?}`. Refused with 409 and a plain `detail`
when the route is switched off, its pool is below its usage floor, a pool with a floor above 0 (Codex) has a
usage reading that is missing, stale or unconfirmed, the node's `model` is not a plain model name
(`model name not allowed`), a `.cmd` launcher would get an unsafe argument, or the node is claimed. Claims the node as
`headless-<id>`, runs `claude -p --output-format stream-json` or `codex exec --json` in the project folder
with the Copy Prompt packet plus the receipt path, and streams progress into the agent tree. Answer:
`{ok, id}`. The worker gets `WAYPOINT_NODE=<project>/<node>` and `WAYPOINT_CLAIM_BY=headless-<id>` in its
environment. A worker that ends without writing a receipt during that run has its claim released; an older
receipt file does not count. Each worker has a time limit (2 hours, or the engine's `headless_timeout`): past
it the process tree is killed, the row shows `error` with "Stopped after the time limit", and the claim is
released. Closing the engine stops every running worker.

### App shell (`app_shell`)

These serve a small installable web page, not JSON.

#### `GET /app/`

HTML page.

#### `GET /app/manifest.webmanifest`

Web app manifest.

#### `GET /app/sw.js`

Service worker script.

#### `GET /app/icon-256.png`

Icon, 256 px. 404 when the file is missing.

#### `GET /app/icon-128.png`

Icon, 128 px. 404 when the file is missing.

### Waypoint Claude bridge (`wpc_bridge`)

The dsh Waypoint tab shows the Waypoint Claude page with the same data: these routes run the Waypoint Claude MCP
tools (`runner/waypoint_runner/mcp_claude.py`) in-process over the 1.2 data folder.

#### `GET /api/wpc/page`

`artifacts/waypoint-claude.html`, unchanged, as `text/html`.

#### `POST /api/wpc/call`

Body `{"tool": name, "input": {...}}`; answer `{"payload": ..., "isError": bool}` (a tool refusal is `isError: true`
with HTTP 200). Reads: `board_read`, `ops_read`, `toolbox_read`, `pref_list`, `dashboard_read` (any credential).
Writes: `answer`, `todo_tick`, `toggle_set`, `floor_set`, `pref_approve`, `pref_reject`, `pref_edit` (owner only, else
403 `owner_only`). Any other tool: 400 `unknown_tool`.

---

## Live stream (SSE)

`GET /api/stream` keeps the connection open and sends Server-Sent Events.

- Query: `project` (optional). With it, `state` events are only sent for that project.
- First event: `state` for the project (or the default project).
- A comment line `: heartbeat` every 15 seconds when nothing else happens.
- The engine also notices changes made by other processes (for example the scheduler tick) about
  once a second and sends `state`.
- Reconnect when the stream ends. The mod waits 15 seconds and polls every 60 seconds as a
  fallback.

```
curl.exe -s -N http://127.0.0.1:8751/api/stream?project=demo
```

```
event: state
id: 12
data: {"project": {"id": "demo", ...}, "revision": 12, "fields": [...], "nodes": [...], ...}

event: todo
data: {"id": "td-e9431c", "section": "human", "status": "open"}

: heartbeat
```

| Event | `data` | Sent when |
|---|---|---|
| `state` | The board payload (as `GET /api/board`, without `kind`/`crosses`); `id:` is the revision. | The plan changed. |
| `run` | `{id, project?, status?, sprint?, start_at?, ...}` | A run or sprint was queued, moved or cancelled. |
| `question` | The question object. | A question was asked or answered. |
| `todo` | `{id, section, status}` | A to-do was added, changed, ticked or reordered. |
| `agent` | `{agent, session_id, turn_id, ...}` | An agent turn started, streamed or ended. |
| `planner` | `{id, status}` | A planning session changed. |
| `deep_plan` | `{id, status, stage, ...}` | A deep-plan stage changed. |
| `models` | `{ids, enabled}` or `{gemini_key}` | A model was switched or the key slot changed. |
| `toggles` | `{id, enabled}` | A switch changed. |
| `receipt` | The receipt row (as `GET /api/receipts`). | A receipt or question file was processed (1.1). |
| `trace` | `{agents: [rows]}` | Agents in the trace changed (1.1). |

New event kinds may appear in a minor version. Ignore kinds you do not know.

---

## Data model

The engine stores everything in one SQLite file (`<data dir>/cc.sqlite`, WAL mode).

### Status set

Task statuses (exact strings):

| Status | Meaning |
|---|---|
| `not_started` | Not begun. Shown as `ready` when every dependency is `done`. |
| `ready` | Derived for display; may also be stored. |
| `running` | A worker is on it. |
| `submitted` | Work is handed in; the done-check has not passed yet. |
| `done` | The done-check passed, or the owner marked it done. Only `done` satisfies a dependency. |
| `error` | The last attempt failed. |
| `needs_you` | Waiting on a person. |
| `skipped` | Not pursued in this version (evidence `skipped: <reason>`). Set only by the versions routes. |
| `carried` | Moved to a later version (evidence `carried to <project>/<node>`). Set only by the versions routes. |

Closed statuses: `done`, `skipped`, `carried`. Every other status is unfinished.

Only a passing done-check (actor `check:<type>`) or the owner sets `done`.

Project status: `draft` (imported by the router, not runnable) or `active`. A project may also be
archived (`archived: 1`); an earlier version of a family is always archived.

### Done-checks

Every task carries a done-check, written before the work.

| `type` | Fields | Passes when |
|---|---|---|
| `command` | `run`, `expect_stdout?` | The command exits 0 (and prints exactly `expect_stdout`). |
| `files_exist` | `paths` | Every path exists. |
| `text_present` | `path`, `text` | The file contains the text. |
| `human` | `prompt` | Never automatically. The owner confirms. |

### Plan file

```json
{"id": "demo", "title": "Demo", "brief": "A sample plan.", "cwd": null,
 "nodes": [{"id": "A1", "title": "Write the parser", "goal": "Parse the input file.",
            "check": {"type": "files_exist", "paths": ["out.txt"]}, "deps": [], "owner": "worker", "lane": "core"},
           {"id": "A2", "title": "Test the parser", "goal": "Add tests.",
            "check": {"type": "command", "run": "python -m pytest -q"}, "deps": ["A1"]}]}
```

Optional node fields: `files` (owned-file globs, 1.1), `pool`, `model`, `effort`, `recommended`, `kind` (`splitter`, `worker`,
`code`, `gate`), `script`. A dependency may be written as `{"id": "A1", "crosses": "the parsed
table"}`. Goal text and the brief are hash-locked: only the owner may change them.

### Tables

| Table | Holds |
|---|---|
| `meta` | Key/value pairs, including `revision`. |
| `projects` | `id`, `title`, `brief`, `cwd`, `status`, `archived`, `family`, `version`, `created_at`. |
| `families` | `id`, `title`, `created_at`. |
| `nodes` | One row per task: goal, check, deps, routing hints, `status`, `attempts`, `actor`, `evidence`, `files` (owned-file globs, 1.1). |
| `events` | `seq`, `at`, `project_id`, `node_id`, `kind`, `actor`, `summary` (one plain sentence), `data`. |
| `runs`, `sprints` | Runs and their time windows. |
| `questions` | Needs-you questions: `kind` (`likert`, `select`, `text`), `prompt`, `options`, `scale`, `purpose`, `answer`. |
| `receipts` | Processed receipt and question files (1.1): hash id, path, project, node, status, outcome. |
| `agents` | The agent trace (1.1): one row per session, subagent or headless worker with its meters. |
| `todos` | To-dos: `text`, `section` (`computer`, `human`), `status`, `position`, `link`, `node`, `deliverables`, `handoffs`. |

Question `purpose` is one of `preference`, `manual_result`, `failure_choice`, `confirm`, `replan`,
`generic` (default). A Likert scale is 1 to 9 unless set.

## CLI bridge (port 8753)

A separate OpenAI-compatible loopback server (`GET /v1/models`, `POST /v1/chat/completions`, open
`GET /health`) that runs your own `claude` and `codex` CLIs. It is not part of Waypoint API 1 and
can be turned off with `CC_CLI_BRIDGE=0` (`CC_CLI_BRIDGE_PORT` moves it).

- Every request must carry a `Host` of `127.0.0.1`, `localhost` or `[::1]` with the bridge's port and
  no `Origin` header at all (403 otherwise).
- Every route except `GET /health` needs the router token, as `X-CC-Token` or
  `Authorization: Bearer <token>` (401 `invalid_api_key` otherwise).
- A fixed transition key, `waypoint-cli-local`, used by an older chat-app setup, is **off by default**.
  It is accepted only while the settings file has `"cli_bridge_legacy_key": true` (read on each
  request) or `CC_CLI_BRIDGE_LEGACY_KEY=1`; `CC_CLI_BRIDGE_LEGACY_KEY=0` refuses it whatever the file
  says. While it is on, the bridge logs one warning line at start-up.
