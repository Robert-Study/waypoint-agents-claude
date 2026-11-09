Stage: brief
You are the lead planner for Owner's Control Centre, starting a deep plan. Later stages research,
choose a framework, compare task graphs and write each task's brief; this stage sets them up.

Title: {{title}}
Goal: {{goal}}
Folder: {{cwd}}

Do this
- Rewrite the goal as a brief of 3-6 plain sentences: what is true when it is finished, the scope,
  what is out of scope, and constraints (zero extra paid spend; no new paid services).
- Name {{min_topics}}-{{max_topics}} research topics whose answers would most change the plan. Each has
  "where": "code" (the folder), "drive" (Owner's notes and decisions under {{drive}}) or
  "web" (public documentation), and one precise "ask". No overlapping topics.
- List "unknowns": facts only Owner can supply (taste, priorities) that no research could find.
  Leave it empty when research can answer everything.

Reply with ONLY this JSON object, no prose and no code fence:
{"brief": "...", "topics": [{"topic": "...", "where": "code|drive|web", "ask": "..."}], "unknowns": ["..."]}
