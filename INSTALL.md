# Install Waypoint Core

## Requirements

| Need | Version | Why |
|---|---|---|
| Windows | 10 or 11 | The scripts use PowerShell 5.1, `icacls` and Task Scheduler. |
| Python | 3.10 or newer | Runs the engine and the CLI. Standard library only. |
| Git | any | To clone and update. |

You do not need administrator rights.

## Install

```powershell
cd <your clone of this repository>
powershell -ExecutionPolicy Bypass -File install.ps1
```

Then open a **new** terminal and run `waypoint doctor`.

The installer finds Python in this order: env `WAYPOINT_PYTHON`, then `py -3`, then `python` on
PATH. It needs 3.10 or newer.

Optional: `pip install tzdata` for that Python. Windows has no time zone database, and the usage
panel needs one to show reset times given in named zones such as `Europe/London`. Without it those
reset times show as unknown; the usage numbers themselves still show.

### Switches

| Switch | Effect |
|---|---|
| `-NoPath` | Do not add `<clone>\bin` to PATH (run `<clone>\bin\waypoint.cmd` directly). |
| `-NoStart` | Do not start the engine. |
| `-AutoStart` | Start the engine at every logon (a Task Scheduler task). |
| `-Port N` | Use port N instead of 8751. |

The installer ends with a short summary. Exit code 0 means every step worked; 1 means a step failed
(the summary says which).

### What the installer changes

This is the complete list. Each step runs only when its switch allows it.

1. **Config file.** Writes `python` (the interpreter it found) and `data_dir` (`<clone>\data`) to
   `%USERPROFILE%\.waypoint\config.json`, each only when the file or that key is missing. It never
   overwrites a value. The Claude Code mod needs `data_dir`: Claude Code runs the mod from its plugin
   cache, so without it the mod cannot find `owner.token`. The file is read and written as UTF-8
   without a BOM, so folder names with accents are kept as they are.
2. **Command.** The clone ships `bin\waypoint.cmd`, which runs `<clone>\scripts\waypoint.cmd`. The
   installer writes no command file. If an older install left `%LOCALAPPDATA%\Waypoint\bin\waypoint.cmd`,
   it is deleted, and so is its PATH entry.
3. **User PATH.** Adds `<clone>\bin` to your user PATH once (`HKCU\Environment`). It does not touch
   the system PATH. Moving the clone means running the installer again from the new folder.
4. **Engine.** Starts the engine with `scripts\Start-Engine.ps1`. On first start the engine creates
   the data folder (default `<clone>\data`) with `cc.sqlite`, `engine.token`, `owner.token` and
   `engine.log`. The token files are readable by your Windows user only.
5. **Mod.** Not part of this tree; the installer skips this step.
6. **Autostart.** Only with `-AutoStart`: registers a Task Scheduler task for your user
   (`scripts\Register-Engine.ps1`). It starts the engine 30 s after logon and restarts it up to 3
   times if it fails. No elevation.

Nothing else changes: no services, no firewall rules, no system settings, no downloads.

## Configuration

Every setting is read in this order: environment variable, then the config file, then the default.
The config file is `%USERPROFILE%\.waypoint\config.json`, or the file named by `WAYPOINT_CONFIG`.
Keys are lower case.

```json
{"python": "C:\\Python312\\python.exe", "data_dir": "D:\\waypoint-data", "owner_name": "Sam"}
```

### Main keys

| Key | Environment variable | Default | Meaning |
|---|---|---|---|
| `python` | `WAYPOINT_PYTHON` | The Python that runs the engine | Interpreter for the engine and scripts. |
| `data_dir` | `CC_DATA_DIR`, `WAYPOINT_DATA_DIR` | `<clone>\data` | Database, tokens and log. |
| `owner_name` | `WAYPOINT_OWNER_NAME` | `Owner` | Your name in event text ("Sam set task A1 to done"). |
| `user_home` | `WAYPOINT_USER_HOME` | Your home folder | Base for the defaults below. |

### Optional integration keys

You only need these if you use the matching optional feature.

| Key | Environment variable | Meaning |
|---|---|---|
| `workspace_root` | `AI_WORKSPACE_ROOT`, `AI_DISK_ROOT` | Folder that holds your code repos (repo sync, agent folders). |
| `worktrees_root` | `WAYPOINT_WORKTREES_ROOT` | Folder for git worktrees. |
| `workspace_dir` | `WAYPOINT_WORKSPACE_DIR` | A tools workspace repo. |
| `tools_dir` | `WAYPOINT_TOOLS_DIR` | Helper scripts used by optional routes. |
| `fama_french_repo` | `WAYPOINT_FF_REPO` | An extra repo offered as an agent folder. |
| `env_file` | `WAYPOINT_ENV_FILE` | A `.env` file with keys for optional free model routes. |
| `drive_root` | `AI_DRIVE_ROOT` | Root of a notes folder. |
| `exchange_dir` | `WAYPOINT_EXCHANGE_DIR` | Name of the notes hub folder under `drive_root`. |
| `instructions` | `WAYPOINT_INSTRUCTIONS` | Instruction file given to agents. |
| `claude_memory_index` | `WAYPOINT_CLAUDE_MEMORY_INDEX` | A Claude Code memory index read by the brain view. |
| `learn_seed` | `WAYPOINT_LEARN_SEED` | Seed file for the learning view. |
| `dsh_dir` | `WAYPOINT_DSH_DIR` | Settings folder of the optional web app. |
| `launcher` | `WAYPOINT_LAUNCHER` | Launcher script of the optional web app. |
| `npm_root` | `WAYPOINT_NPM_ROOT` | Global npm folder. |
| `agy_exe` | `WAYPOINT_AGY_EXE` | Path of the Antigravity CLI. |

The config file may also hold an `addons` object, for example `{"addons": {"db_backup": false}}`.
An add-on is on unless set to `false`.

### Other environment variables

| Variable | Meaning |
|---|---|
| `WAYPOINT_CONFIG` | Path of the config file. |
| `WAYPOINT_PORT` | Engine port (default 8751). |
| `WAYPOINT_URL` | Full engine URL for the CLI (overrides `WAYPOINT_PORT`). |
| `CC_DB` | Database file (default `<data dir>\cc.sqlite`). |
| `CC_TOKEN_FILE` | Router token file (default `<data dir>\engine.token`). |
| `CC_CLI_BRIDGE` | `0` turns off the CLI bridge on port 8753 (see SECURITY.md). |
| `CC_CLI_BRIDGE_PORT` | Moves the CLI bridge to another port. |
| `WAYPOINT_SESSION` | Session id for `waypoint sync` files. |

## Start and stop

```powershell
waypoint engine start      # starts in the background, or reuses a running engine
waypoint engine status     # exit 0 when it answers
waypoint engine stop       # stops only a Waypoint engine on that port
```

Add `--port N` to use another port. The engine log is `<data dir>\engine.log` (rolled at 5 MB).

Without the CLI:

```powershell
powershell -NoProfile -File scripts\Start-Engine.ps1 [-Port N]
powershell -NoProfile -File scripts\Stop-Engine.ps1 [-Port N]
```

Or in the foreground, from the `engine` folder: `python -m cc_engine serve [--port N] [--db PATH]`.

### Autostart

```powershell
powershell -NoProfile -File scripts\Register-Engine.ps1            # at logon
powershell -NoProfile -File scripts\Register-Engine.ps1 -DryRun    # show the task, change nothing
powershell -NoProfile -File scripts\Register-Engine.ps1 -Unregister
```

A deliberate stop does not trigger a restart.

### Scheduler tick (optional)

Runs and sprints move forward on a "tick". `scripts\Register-Tick.ps1` registers a task that runs
`python -m cc_engine tick` every 10 minutes, at logon and on resume from sleep. Ticks are safe to
overlap. You only need this if you use runs.

## Update

```powershell
waypoint engine stop
git pull --ff-only
waypoint engine start
waypoint doctor
```

Your data folder is not in git, so an update never touches it. The database upgrades itself on
start. Run `powershell -ExecutionPolicy Bypass -File install.ps1` again if the release notes say
so; it is safe to run twice.

## Uninstall

```powershell
waypoint engine stop
powershell -ExecutionPolicy Bypass -File uninstall.ps1
```

`uninstall.ps1` removes the `<clone>\bin` PATH entry (and an older install's shim) and the
autostart task. It **never deletes** the data folder. Delete it yourself if you want your data gone.
Then delete the clone.

## Troubleshooting

**Port busy.** `engine start` says `port 8751 is held by pid N but /health is not 200`. Another
program uses the port. Stop that program, or use another port:

```powershell
waypoint engine start --port 8752
$env:WAYPOINT_PORT = "8752"      # so the CLI finds it; set it in your profile to keep it
```

Install with `-Port 8752` to make that the default for the installer's start step.

**Engine slow to start.** `engine start` waits up to 60 s for `/health`. On a busy machine the first
start can take longer. Run `waypoint engine status` again after a few seconds. If it still fails,
read the last lines of `<data dir>\engine.log`; the start script prints them too. A message about
another start in progress means two starts ran at once; wait and try again.

**PATH not refreshed.** `waypoint` is not found right after install. Programs that were already
open keep the old PATH. Open a new terminal. If it is still missing, check that
`<clone>\bin` is in your user PATH, or run `<clone>\bin\waypoint.cmd` directly.

**No Python found.** Install Python 3.10 or newer from python.org, or set `WAYPOINT_PYTHON` to the
full path of `python.exe`, and run the installer again.

**Forbidden (exit 4).** The engine refused the credential. Owner actions need `--owner`. If
`--owner` fails too, the CLI is reading a different data folder from the engine: set
`CC_DATA_DIR` (or `data_dir` in the config file) to the engine's data folder.
