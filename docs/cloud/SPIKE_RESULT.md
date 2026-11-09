# cloud-01 spike result (7 Nov 2026)

Question: can a Waypoint node run in GitHub Actions on a private repo? Short answer: yes for a no-model code node, with no secret. Agent CLIs are not on the runner.

RUN_URL: https://github.com/your-account/dsh-control-centre/actions/runs/42369398946

Workflow: `.github/workflows/cloud-spike.yml` (manual `workflow_dispatch`, plus push on branch `wp12/cloud-01`). One job on `ubuntu-latest`, 11 s, free minutes, no paid runner. It checked out the repo, ran `python3 scripts/waypoint_cli.py --help` as the no-model node, and recorded runner facts. Repo `your-account/dsh-control-centre` reported visibility=private.

## Items

- auth: PASS - the job used only the built-in `github.token` (permissions: contents read); checkout and `gh api` calls worked with it (run 42369398946).
- secrets: PASS - none needed for a no-model node; the job saw 0 model-provider secret variables (ANTHROPIC/OPENAI/GEMINI/GROQ/COPILOT_TOKEN). No secret or token was created or set.
- agent CLIs: FAIL - on the stock ubuntu-latest runner (image ubuntu26 20261101.186.1) claude, codex, gemini, copilot and ollama are absent; gh 2.103.3, node v24.22.2, python 3.14.4 and git 2.56.0 are present. A finding, not a reason to add a paid service.
- usage limits: UNKNOWN - observed only the GitHub API core rate limit (4999/5000 remaining) and a job time of 11 s. The private-repo free Actions minute quota and any agent-CLI usage caps were not read; billing and agent usage were not queried.
- drive reachability: PASS - read-only: the Drive connector from a connector-equipped session found the folder `notes` (id 1LYh6U5FsG1BiV6mWd2lUU-ZvtpwmNSq0) by metadata search. Write was not tested (read-only first). Reachability from inside an Actions job is UNKNOWN: it has no Drive credential, so cloud-local-bridge (cloud agent with a Drive connector, GitHub as blackboard) is the route, not Actions.

## Agent-CLI credential proposal: prepared, not performed

To run an agent CLI in Actions, Owner would have to authorize and create one repository secret, for example `CLAUDE_CODE_OAUTH_TOKEN` (Settings, Secrets and variables, Actions, private repo only), then add a step installing the CLI and reading it from `secrets.`. Floor and cost rules apply: it should use only the subscription already paid for, with no API-key billing. Nothing was created or set. Needs Owner's authorization; until then Actions nodes stay no-model.

## Notes

- A first push failed instantly (0 s, no jobs) because of a YAML colon in a plain scalar; fixed in the second commit. The run is not the RUN_URL.
- The workflow was triggered by push on the private branch rather than `gh workflow run`, because a `workflow_dispatch` workflow that exists only on a non-default branch cannot be dispatched by name. Nothing was pushed to main or any public remote.
