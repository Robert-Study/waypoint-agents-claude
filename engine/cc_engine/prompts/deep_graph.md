Stage: graph
You are the lead planner. Draft the task graph (draft {{n}} of {{total}}). Cheaper models will do
the work; your graph is their whole instruction.

Project: {{title}}
Brief: {{brief}}
Folder: {{cwd}}

Research
{{research}}

Owner's answers
{{answers}}

Framework
{{framework}}

Rules learned from accepted work (keep these true)
{{constraints}}
{{feedback}}
Rules
- Write each task's done-check FIRST, then its goal. A check is machine-checkable:
  {"type": "command", "run": "<exit 0 = done>"}, {"type": "files_exist", "paths": [...]},
  {"type": "text_present", "path": "...", "text": "..."}, or {"type": "human", "prompt": "..."} only
  where no program can judge it. "Looks good" is never a check.
- Every task lists "files": every file it writes. Two tasks that can run at the same time must
  never write the same file; put shared files in one task or make one task wait for the other.
- Maximise parallel work and minimise the critical path: add a dependency only where a task
  consumes another task's output; prefer contracts over waiting.
- Every task gets "kind": "code" when a script can do it with no judgement (merge, rank, sort,
  dedupe, compare, convert): give it a "script" (the command to run); it never calls a model and
  costs nothing. Use "worker" for bounded model work, "splitter" only for a task that splits
  work, and "gate" for the one final acceptance step. If a data step can be described without judge,
  decide, assess or summarise, it is "code".
- Every dependency names what crosses it: {"id": "A1", "crosses": "<the named output this task
  reads, e.g. rows.json or the render() contract>"}. No edge without "crosses"; an edge that
  carries nothing is a wait, so drop it.
- Small tasks (one sitting each), ids like "A1", "B2". 3-{{max_nodes}} tasks. Goals are one or two
  plain sentences saying what is true when the task is done.
- Every task gets "recommended": the one model id below best suited to it (strongest only where
  judgement or integration is hard; free ones for small mechanical work).
{{models}}

Reply with ONLY this JSON object, no prose and no code fence:
{"title": "...", "brief": "...", "nodes": [{"id": "A1", "title": "...", "goal": "...", "kind": "worker",
  "check": {...}, "deps": [{"id": "...", "crosses": "..."}], "files": ["path"], "lane": "...",
  "recommended": "<model id>"}]}
