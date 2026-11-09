# What lives where

Code and state hand over through git. Docs and human outputs hand over through Drive for desktop. Cloud sessions reach both through connectors, never localhost.

| thing | lives in | local access | cloud access | handover |
| --- | --- | --- | --- | --- |
| Code (engine, runner, web, core) | `C:\Users\you\workspace\dsh-control-centre` (private GitHub) | main folder, or a worktree under `workspace\worktrees` | GitHub connector, read the repo | branch, merge to `main`, push `main` to private remote |
| Waypoint data folder (state, claims, events) | Root from `core/waypoint-data/src/paths.ts`: `WAYPOINT_DATA_DIR`, else `~/.waypoint/data-dir`, else `~/.waypoint/data` | local only, git-ignored | none directly | runner reads it; cloud output comes back as a branch |
| Receipts and questions | `<cwd>/.waypoint/receipts` and `questions`, inbox under `~/.waypoint/inbox` | local, git-ignored | cloud run commits one receipt on `cloud/<node>-<run>` | runner fetches `cloud/*` and imports it (`core/docs/cloud/PROTOCOL.md`) |
| Sync inbox | `sync/` in the repo, moved by `waypoint sync` | git | GitHub connector | commit and push |
| Cloud protocol and handoff rules | `core/docs/cloud/` in the repo | git | GitHub connector | read on `main` |
| Drive handoff packets | `C:\Users\you\Notes\notes` (HANDOFF, ACK, RECEIPT files) | Drive for desktop file path | Drive connector | one-shot files (`core/docs/cloud/DRIVE_HANDOFF.md`) |
| Rules, preferences, plans (AGENTS.md etc.) | `C:\Users\you\Notes\notes` | Drive for desktop | Drive connector | edit on disk, Drive syncs |
| Human outputs | `C:\Users\you\Notes\Human-Outputs` | Drive for desktop | Drive connector | Drive syncs |
| Engine API | `127.0.0.1:8751` | local only | never localhost; cloud uses files and git | use files, not the API |
| Live SQLite, .venv, worktrees | `C:\Users\you\workspace` | local only | none | not on Drive; Drive does not sync this folder |
| Machine code-root map | `machines/<id>.json` in the data root | local only, git-ignored | none | never leaves the machine |

- Git is the handover for code and state. Drive for desktop is the handover for docs and human outputs.
- Cloud sessions use the GitHub and Drive connectors. They never reach localhost.
- Live databases, environments and worktrees stay on disk under `workspace`, never on Drive.
- Shared data stores a working folder as `{machine, rel}`, never an absolute drive path.
- Conflicted Drive copies are reported, never read or deleted.
