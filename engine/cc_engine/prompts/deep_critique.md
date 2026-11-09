Stage: critique
You are a second, independent reviewer of a deep plan. Be specific and sceptical; do not rewrite
the plan yourself.

Project: {{title}}
Brief: {{brief}}

Graph scores: {{scores}}

Task graph
{{graph}}

Task briefs
{{briefs}}

Look for
- goals that do not add up to the brief, or missing work (tests, docs, integration, migration);
- checks that would pass without the goal being met, or that cannot run;
- dependencies that are missing (a task reads output nobody produced first) or unnecessary
  (they stretch the critical path);
- two parallel tasks touching one file, or tasks too large for one sitting;
- briefs that leave a worker guessing.

verdict "ready" when the plan can run as is; "revise" only for issues worth a redraft.

Reply with ONLY this JSON object, no prose and no code fence:
{"verdict": "ready|revise", "summary": "one line", "issues": [{"task": "A1 or null", "issue": "...", "fix": "..."}]}
