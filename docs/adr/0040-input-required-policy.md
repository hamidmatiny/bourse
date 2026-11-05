# 0040 — INPUT_REQUIRED policy

## Context
A2A `TASK_STATE_INPUT_REQUIRED` was mapped to `INTERRUPTED` with no schema check. An agent
could smuggle instructions in free-text status messages (including asking for raw rows).

## Decision
- Accept INPUT_REQUIRED only when the status message is exactly one DataPart whose JSON
  matches a closed set: `ConfirmColumns{columns: list[str] subset of the dataset header}` or
  `ConfirmUnits{column, candidates: list[str] from the unit table}`.
- Max one input round per task_id. A second round is rejected.
- Anything else (free text, extra parts, unknown schema, columns/units off the tables) ->
  `CancelTask`, delegation outcome `SESSION_POLICY`, SOFT violation `SESSION_SMUGGLING`.
- Agent-produced text in status messages, artifacts and error messages is captured as
  untrusted and is never placed in any LLM prompt (extends ADR-0008 / Phase 3 sentinel).

## Consequences
- A valid ConfirmColumns/ConfirmUnits still ends the attempt as INTERRUPTED (no auto-reply;
  answering is Phase 5c). The point of 5b is to reject smuggling, not to complete the round.

## Status
Accepted (Phase 5b).
