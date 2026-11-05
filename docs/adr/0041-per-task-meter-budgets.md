# 0041 — Per-task meter budgets and usage misreport

## Context
The meter capped per-call tokens but an agent could loop calls or invent a task_id. Artifact
metadata could claim arbitrary usage.

## Decision
- Before each delegation the broker `POST /budgets {task_id, max_tokens, max_calls}` with the
  broker meter key only. Defaults: 8192 tokens, 20 calls (`BOURSE_TASK_MAX_TOKENS` /
  `BOURSE_TASK_MAX_CALLS`). The admission exam (registry) posts the same budgets before each
  exam task; without that, agents get 403 and stay PENDING.
- Production meter sets `require_task_budget=True`. An agent request whose task_id is not
  registered -> HTTP 403 `unregistered_task`, no upstream call.
- Over the registered ceiling -> HTTP 429, `error.code=task_budget_exceeded`, usage row
  `status=429` and `error_kind=BUDGET`, no upstream call.
- The broker key is privileged and may call without a budget (planner).
- If artifact metadata carries `tokens_in`/`tokens_out` and the claimed total differs from
  measured usage for that task_id by more than 10% (or claimed > 0 when measured is 0), the
  broker records a SOFT `USAGE_MISREPORT` violation.

## Consequences
- Token-burner loops die on 429 and the A2A task fails. High measured shadow cost also lowers
  utility on later selection.
- Unit tests keep `require_task_budget=False` unless they exercise 403/429.

## Status
Accepted (Phase 5b).
