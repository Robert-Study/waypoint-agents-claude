# addons/

An empty slot. Nothing here is loaded by default.

A later, minimal add-on folder can go here: a small preferences module and a set of skills that sit beside the
engine and stay out of its core. This folder is part of the shared engine, so it is identical in
`waypoint-agents` and `waypoint-agents-claude`.

## What would live here

- **Preferences.** A folder with `mcp_preferences.py`. It adds four tools to the Waypoint MCP server
  (`pref_list`, `pref_approve`, `pref_reject`, `pref_edit`) and the Preferences review pages in the dashboard and in
  Waypoint for dsh.
- **Skills.** Plain text skill files that an agent reads when a task needs them.

## How an add-on is found

The preferences module is looked up in this order:

1. the environment variable `WAYPOINT_PREFS_TOOLS` (a folder that contains `mcp_preferences.py`),
2. `prefs_tools` in `~/.waypoint/config.json`.

If neither names a folder with that file, the add-on is off. The `pref_*` tools are not listed, the pages hide their
Preferences section, and nothing logs an error. To try it, put a folder here (for example `addons/preferences/`),
then point either setting at it and restart the Waypoint server.

## Contract for `mcp_preferences.py`

It must define `FUNCS` (tool name to a function that takes the arguments), `tools_list()` (the MCP tool
descriptions) and `handle(message)` (answers one MCP `tools/call` message). See `runner/waypoint_runner/mcp_claude.py`
for the code that loads it.
