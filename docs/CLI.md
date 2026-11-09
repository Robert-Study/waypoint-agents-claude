# waypoint CLI

Waypoint Core 1.2. `waypoint` is the command line over the Waypoint [data folder](FILE_CONTRACT.md#data-folder).
It reads and writes the folder through the data client (`core/waypoint-data/py`). There is no engine: the 1.1
engine on port 8751 is retired. Owner actions that need the runner (`run`, `version new`, `version family`,
`version decide`) write a run or an action file and start the runner on demand
([runner/RUNNER.md](runner/RUNNER.md)). Python 3.10 or newer, no packages.

`install.ps1` puts the clone's `bin` folder (it holds `waypoint.cmd`) on your user PATH. Without the installer, run
`bin\waypoint.cmd`, `scripts\waypoint.cmd` or `python scripts\waypoint_cli.py` from the clone.

## Usage

```
waypoint <command> [options]
waypoint help <command>
waypoint --version
```

`waypoint` with no command, `-h` or `--help` prints the short help. `waypoint help todo add` prints the help for one
command.

## Global flags

These work before or after the command.

| Flag | Meaning |
|---|---|
| `--json` | Print the answer as JSON instead of text. |
| `--owner` | Act as the owner. Needs the file `owner.token` (beside `CC_TOKEN_FILE` when set, else in the data folder). Only its presence is checked; nothing is sent anywhere. Writes are then recorded as `owner`, else as `cli`. |
| `--version` | Print the CLI version and the data folder, then exit 0. |

## Settings

| Setting | Default | Used for |
|---|---|---|
| `CC_DATA_DIR` or `WAYPOINT_DATA_DIR` | `data_dir` in the config file, else the first line of `~/.waypoint/data-dir`, else `~/.waypoint/data` | The data folder. |
| `WAYPOINT_CONFIG` | `~\.waypoint\config.json` | The optional config file (`data_dir`, `python`). |
| `WAYPOINT_PYTHON` | `python` in the config file, else the interpreter running the CLI | Interpreter that starts the runner, and the one `waypoint.cmd` uses. |
| `CC_TOKEN_FILE` | not set | When set, `owner.token` is looked for beside this file. |
| `WAYPOINT_SESSION` | random | Session id for `waypoint sync` files. |
| `WAYPOINT_CLI_MODE` | not set | `folder` forces the data folder; `engine` forces the 1.1 CLI (`scripts/waypoint_cli_engine.py`) on a machine that is not cut over yet. |

Before the 1.2 cutover (no `backups/cutover-receipt.json` in the data folder, and no data-folder variable set) the
CLI hands every command to the 1.1 engine CLI. After it, the folder is the only source. The CLI never prints a token.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | OK. |
| 1 | Other error: no such project or task, a refused claim or release, a failed `doctor` check, a failed git step. |
| 2 | Usage error: bad arguments, bad id, bad `--node`, bad `--skip`. |
| 3 | Stale: the file changed since you read it (revision check). Run again, or use `--retry`. |
| 4 | Forbidden: an owner action without `--owner`, or without `owner.token`; `sync push` to a repo not marked private. |
| 5 | `waypoint engine ...`: the engine is retired. |
| 6 | Unused in 1.2 (kept so the numbers keep their meaning). |

## Commands

| Command | Does |
|---|---|
| `board [project]` | Without a project: list projects. With one: each task with `status`, `display_status` (a claimed waiting task shows `running`), revision, slot, deps, float and claim. |
| `node <project> <id>` | One task: goal, check, evidence and recent events. |
| `done <project> <id> --evidence TEXT [--retry]` | Mark a task done (owner: add `--owner`). `--retry` reads again and retries once on a stale revision. |
| `status <project> <id> <status> --evidence TEXT [--retry]` | Set `not_started`, `ready`, `running`, `submitted`, `error`, `needs_you` or `done` (owner). `skipped` and `carried` are refused; use `version`. |
| `todo list [--section review\|decisions\|hands_on\|human] [--all]` | Open to-dos by category; `--all` adds done, dropped and merged ones with a status tag. `human` means `hands_on`. |
| `todo add TEXT [--human] [--category C] [--position N] [--link URL] [--node project/id]` | Add a to-do (`hands_on` unless `--category`). |
| `todo tick ID` | Tick an open to-do off. |
| `todo reorder --section C ID...` | Put a category's to-dos in this order. |
| `receipt write --status done\|error\|needs_you --summary TEXT [...]` | Write `<cwd>/.waypoint/receipts/<name>.json` (`waypoint.receipt.v1`, see [FILE_CONTRACT.md](FILE_CONTRACT.md)). Same limits as the runner's ingest. `done` is a claim: the task's own check decides. |
| `receipt list [--limit N]` | Recent receipts, newest first. |
| `tree` | Agents from `sessions/*.json` as an indented tree. |
| `claim <project> <node> --by NAME [--cwd DIR] [--worktree DIR] [--env TEXT]` | Claim a task through the shared claim protocol (`claims/<node>.json`). The same name refreshes it; a fresh claim by someone else exits 1. |
| `release <project> <node> [--by NAME]` | Drop a claim. |
| `run <project> [--settings JSON] [--no-runner]` | Owner: queue a run (`runs/<id>.json`) and start the runner. |
| `usage` | Remaining share per pool from `usage/*.json`. An unreadable pool shows `unknown`. |
| `brief <project> [--append TEXT \| --set-file PATH] [--rev N]` | Show the project brief (`brief` in `projects/<id>/project.json`): direction, decisions and open state. `--append` adds one line; `--set-file` replaces it with a UTF-8 file. Each edit is revision-checked: `--rev` is the revision you read (else the one read now); a change in between exits 3 and writes nothing. Writes one `project.brief` event. |
| `version history TARGET` | Every version of a family (or of a project's family) and the skipped ideas. A version shows by its own label (`V10`, `Core 1.2`) when it has one, else by its ordinal (`v2`). |
| `version new TARGET [--label L] [--title T] [--skip ID=reason]... [--carry ID]... [--dry-run] [--no-runner]` | Owner: queue the next version; unfinished tasks carry unless skipped. Only the current version can roll over. `--label` is the product's own version name: it is stored as `version_label` and names the new project id (`my-family-v10`); without it the id is `my-family-v<ordinal>`. `--title` replaces the default title (family title and label). |
| `version label PROJECT [LABEL \| --clear] [--no-runner]` | Show a version's label, or (owner) set or clear it. The ordinal `version` stays for order. |
| `version decide PROJECT [--skip ID=reason]... [--carry ID]... [--no-runner]` | Owner: carry or skip unfinished tasks of an earlier version. |
| `version family FAMILY [--title T] MEMBER... [--no-runner]` | Owner: group projects as versions, oldest first. |
| `sync status\|todo\|receipt\|push` | Cloud sessions: queue items in `sync/inbox/` of the repo (`waypoint.sync.v1`), then `push` (needs `git config backup.github`; never pulls or forces). |
| `engine start\|stop\|status` | Retired: prints that the engine is retired and exits 5. |
| `doctor` | Checks Python, the data folder, the runner, `owner.token` and the Claude Code mod. Exit 1 when a check fails; `skip` is not a failure. |
| `help [command...]` | Help for a command, with examples. |

Answering a question is done in the Waypoint panes or the web page, not the CLI. After an answer the runner routes it
once: a warm asking session gets it directly; a cold one (idle over `resume_cold_after_minutes`, default 60) is
not woken and the work resumes from Waypoint. See [FILE_CONTRACT.md](FILE_CONTRACT.md#answers-go-back-to-the-work).

## Examples

```
waypoint doctor
waypoint board
waypoint board demo --json
waypoint status demo A1 running --evidence "started"
waypoint done demo A1 --evidence "tests pass" --owner --retry
waypoint todo add "Check the numbers" --human
waypoint run demo --owner
waypoint version history demo
```

```
> waypoint --version
waypoint 1.2.0 (engine retired; data folder C:\Users\you\.waypoint\data)
```
