Stage: revise
You are the lead planner. A second model reviewed your plan and found the issues below. Fix every
one of them and return the whole revised graph.

Project: {{title}}
Brief: {{brief}}

Issues
{{issues}}

Current graph (scores: {{scores}})
{{graph}}

Rules
- Keep everything that was not criticised. Same rules as before: checks first, every task lists the
  "files" it writes, no two parallel tasks write one file, dependencies only where output is
  consumed, 3-{{max_nodes}} tasks.
- Keep "kind" on every task ("code" with a "script" when no judge, decide, assess or summarise is
  needed) and "crosses" on every dependency: {"id": "A1", "crosses": "<the output this task reads>"}.
- For new or changed tasks you may add "inputs" and "exit" lists (as in a task brief).
{{models}}

Reply with ONLY this JSON object, no prose and no code fence:
{"title": "...", "brief": "...", "nodes": [{"id": "A1", "title": "...", "goal": "...", "kind": "worker",
  "check": {...}, "deps": [{"id": "...", "crosses": "..."}], "files": ["path"], "lane": "...",
  "recommended": "<model id>", "inputs": [], "exit": []}]}
