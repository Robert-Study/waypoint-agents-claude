# Security

## Supported versions

| Version | Supported |
|---|---|
| 1.0.x | Yes |
| older | No |

Security fixes go into the newest 1.x release.

## Report a problem

Please report a vulnerability privately. Do not open a public issue.

1. Open the repository on GitHub.
2. Go to **Security**, then **Report a vulnerability** (GitHub private vulnerability reporting).
3. Describe the problem, the version (`waypoint --version`), and the steps to reproduce it.

We aim to reply within 7 days and to agree a fix and a disclosure date with you.

## Threat model

Waypoint Core runs on one Windows machine for one user. It is not a network service.

### What it trusts

- **Processes that run as your Windows user are trusted.** They can read your token files and act
  as you. Waypoint does not defend against malware that already runs as you.
- **The clone and the data folder.** Code in the clone runs as you. Keep both in folders that only
  you can write to.

### What it defends against

- **Other machines.** The engine binds to `127.0.0.1` only and refuses any other address. Nothing
  listens on a network interface.
- **Web pages in your browser.**
  - *DNS rebinding.* Every request (reads, the SSE stream and writes) must carry a `Host` header
    that names `127.0.0.1`, `localhost` or `[::1]` with the engine's own port. Otherwise the engine
    answers 403 `bad_host`.
  - *Cross-site requests.* A write that carries an `Origin` header must come from the allowlist
    (`http://127.0.0.1:3080`, `http://localhost:3080`, the optional web page). Other origins get
    403. CORS headers are sent only to allowlisted origins, so other pages cannot read answers.
- **Other local users and unprivileged programs writing your data.** Every write needs a
  credential:
  - the **router token** in `data/engine.token` (automation: workers, scripts, the CLI), or
  - the **owner token** in `data/owner.token` (you: the Claude Code mod, `waypoint --owner`).
    When `CC_TOKEN_FILE` moves the router token, the owner token sits beside it.

  Both are 32 random bytes. The engine creates them at start-up (the owner token before the port is
  bound) and sets the file ACL so that only your Windows user can read them
  (`icacls /inheritance:r /grant:r <you>:F`). A new token file can keep the data folder's normal ACL
  for about a second while that runs in the background. Tokens are compared in
  constant time and are never put in a URL, a log or an answer.
- **Automation acting beyond its role.** The router can report progress but cannot mark a task
  done, answer a question, change a goal, check or brief, accept a plan, start a run or turn on
  power-off. Plans it imports stay drafts until you accept them. Goals, checks and briefs are
  hash-locked, and the board reports any mismatch.
- **Lost updates.** Plan writes carry a revision number. A stale write is refused with 409 and
  changes nothing.

### Known limits

- **Reads are open on loopback.** `GET` routes need no credential. Any program on the machine,
  including another user's program, can read your plans, to-dos and usage while the engine runs.
  Do not put secrets in task text, evidence or to-dos.
- The database and log in the data folder use the folder's normal ACL. Only the token files are
  restricted. Keep the data folder inside your user profile.
- **The CLI bridge.** The engine also starts a small OpenAI-compatible server on
  `127.0.0.1:8753`. It lets an optional local chat app run your own `claude` and `codex` CLIs on
  your subscription logins. It has the same `Host` check, refuses every request with an `Origin`
  header, and needs the router token (`X-CC-Token` or `Authorization: Bearer <token>`) on every route
  except `GET /health`. Set `CC_CLI_BRIDGE=0` to turn it off if you do not use it.
- **The CLI bridge legacy key (off by default).** An older chat-app setup sends the fixed, published
  key `waypoint-cli-local` instead of the router token. The bridge refuses it unless you turn it on
  with `"cli_bridge_legacy_key": true` in `~/.waypoint/config.json` or `CC_CLI_BRIDGE_LEGACY_KEY=1`
  (`=0` always refuses it). While it is on, any program on the machine, including another Windows
  user's, can run your `claude` and `codex` CLIs on your subscription in your working folder; the
  bridge logs a warning line at start-up. Turn it off as soon as the chat app sends the router token.

### Network use

- **No telemetry.** Waypoint sends no usage data, crash reports or analytics anywhere.
- **No paid API calls.** The engine never calls a metered model API. Agents run through your own
  CLIs and subscription logins; the CLI bridge removes metered API keys from the environment of
  every program it starts. Usage floors stop work before a pool runs out.
- Outbound calls happen only for features you use: `git fetch`/`pull` for repository sync, the
  agents and free model routes you switch on (including an hourly health probe of those routes),
  and `git push` from `waypoint sync push`. `sync push` refuses to run unless the repo is marked
  private (`git config backup.github`).
- The engine reads local usage telemetry from `127.0.0.1` only.

## Good practice

- Do not copy `data/engine.token` or `data/owner.token` anywhere. Do not commit the `data/` folder
  (it is in `.gitignore`).
- To rotate a token, stop the engine, delete the token file and start the engine again.
- Run `waypoint doctor` after an update.
