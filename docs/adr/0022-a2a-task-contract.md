# 0022 — A2A request/artifact contract; REJECTED vs FAILED

## Context
The broker (Phase 3) and the bench (Phase 6) must treat every agent the same way.

## Decision
- Request: one A2A message with exactly one DataPart holding the skill's `SKILL_IO` input JSON,
  and message metadata `{skill_id, task_id, deadline_s}` (deadline in (0, 600] s; agent default
  300 s). `task_id` is the BOURSE task id used for metering, distinct from the A2A task id.
- Response events: Task (SUBMITTED) → WORKING → one Artifact with one DataPart holding the
  `SKILL_IO` output JSON (artifact metadata `{skill_id, task_id}`) → COMPLETED.
- REJECTED (no work started): `unsupported_skill`, `missing_task_id`, `bad_deadline`,
  `bad_parts`, `invalid_input`.
- FAILED (work started): `deadline`, `tool_error`, `tool_budget`, `invalid_llm_output`,
  `llm_unavailable`, `token_budget`, `tool_not_allowed`, `internal`.
- The status message text is `<kind>: <detail>` and its metadata carries `error_kind`. Details
  name fields and error types only; input values are never echoed.

## Consequences
- A broker can retry FAILED tasks elsewhere and treat REJECTED as a routing error.

## Status
Accepted (Phase 2b).
