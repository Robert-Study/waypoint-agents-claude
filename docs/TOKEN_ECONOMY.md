# Token economy

- Agents write one compact, fixed-schema receipt and read only their injected brief and owned files.
- Status, trace and meters come from hooks and code, never from model narration or board reads.
- No model polling. Speak only when done, blocked or asking.
- Backend reads and writes stay at an absolute minimum; code does the rest.
- Logs are for code, not for models to read.
- Worker briefs and prompt packets stay under 6000 bytes; receipts stay under 1500 bytes.
- `node scripts/check_token_budget.mjs` enforces the size limits over the brief fixtures.
