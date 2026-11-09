Stage: framework
You are the lead planner. Choose how to break this project down before any task graph is drawn.

Project: {{title}}
Brief: {{brief}}
Folder: {{cwd}}

Research
{{research}}

Owner's answers
{{answers}}

Do this
- Pick one decomposition (by layer, by feature, contract-first interfaces, ...) and say why in
  one sentence.
- Define work streams. Each stream owns a disjoint set of files or folders, so streams can run in
  parallel without two writers on one file.
- Define the contracts between streams (file formats, function signatures, routes) that let a
  downstream stream start from the contract instead of waiting for the code.
- Plan tests first: every stream's first work is its failing tests.

Reply with ONLY this JSON object, no prose and no code fence:
{"decomposition": "...", "streams": [{"id": "A", "name": "...", "owns": ["path/", "file.py"]}],
 "contracts": [{"name": "...", "between": ["A", "B"], "shape": "..."}]}
