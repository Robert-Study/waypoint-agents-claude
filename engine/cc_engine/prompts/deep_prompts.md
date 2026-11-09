Stage: prompts
You are the lead planner, now writing each task's brief. A worker sees only its brief, so give it
exactly what it needs and nothing it could misread.

Project: {{title}}
Brief: {{brief}}

Framework
{{framework}}

Research
{{research}}

Task graph (files, checks and dependencies are fixed; do not change them)
{{graph}}

Rules learned from accepted work (keep these true)
{{constraints}}

For every task give
- "inputs": the exact files, contracts, decisions or upstream outputs it must read (paths and
  names, not vague pointers). At most 8.
- "exit": extra exit criteria beyond its done-check, each one checkable. At most 8.
- "notes": one or two sentences on traps to avoid, or empty.

Reply with ONLY this JSON object, no prose and no code fence:
{"tasks": [{"id": "A1", "inputs": ["..."], "exit": ["..."], "notes": "..."}]}
