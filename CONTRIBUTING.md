# Contributing

Thank you for helping. Keep changes small and tested.

## Rules

- **Python standard library only.** No new packages in the engine, CLI or scripts. `pytest` is the
  only tool the tests need.
- **Python 3.10 or newer.** Do not use features from later versions.
- **The mod uses no npm packages** and no build step.
- **The engine is the single source of truth.** Clients write only through its revision-checked API.
- **Keep the API stable.** A minor API change is additive only. A breaking change needs a new API
  major, an entry in `CHANGELOG.md` and an update to `docs/API.md`. Every route must appear in
  `docs/API.md`; a test checks this.
- **No real provider calls in tests.** Use fake executables and injected runners.
- **Tests never use the live data folder.** Use `tmp_path`. Never stop an engine you did not start;
  test engines use other ports and temporary data folders.
- **No personal data or secrets** in code, tests, fixtures or docs. Use generic examples
  (project `demo`, task `A1`).

## Run the tests

From the repository root:

```powershell
python -m pytest engine/tests -q -p no:cacheprovider
python -m pytest scripts/tests -q -p no:cacheprovider
python -m pytest engine/tests/test_api_docs.py -q -p no:cacheprovider
```

Scan the tree for personal strings and secrets before you open a pull request:

```powershell
python scripts/export_core.py --scan .
```

## Style

- Plain English in docs, messages and event text. Short sentences. One idea per sentence.
- Event summaries are one plain sentence that says who did what.
- Python: PEP 8, 4-space indents, lines up to about 120 characters, type hints where they help.
- Errors from the API use `{"error": "<code>", "detail": "<one sentence>"}`.
- Write the test first. Make it fail, then make it pass.

## Pull requests

1. Fork the repository and create a branch.
2. Make the change with its tests and docs.
3. Run the commands above.
4. Open a pull request that says what changed and why.

By contributing you agree that your work is licensed under the MIT License.
