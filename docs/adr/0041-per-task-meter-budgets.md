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

## Amendment: hard cap (Phase 6b, operator decision 2026-11-06)
ADR-0052 amendment 2 found that the cap was soft. A call was admitted when
`used + max_tokens <= ceiling`, so the new call's prompt was not counted, and 2/10 dev tasks
ended above 8192 (8317, 8529).

- **Admission rule.** Before every budgeted call the meter computes a deterministic prompt
  estimate (`meter/estimate.py`) and admits the call only if
  `estimate + max_tokens <= ceiling - used`.
  - Otherwise: HTTP 429, `error.code = budget_exhausted`, usage row `status=429`,
    `error_kind=BUDGET`, no upstream call.
  - The max-calls limit keeps `task_budget_exceeded`.
  - Neither body contains quota/credit/billing wording, so neither trips the global breaker
    (unit test).
- **Estimate.**
  - Every ASCII digit counts 1 token, since Qwen's pre-tokenizer splits digits.
  - Every other UTF-8 byte counts 1/2.
  - Plus 16 per message, 16 per tool plus its JSON schema, 256 when tools are present (the
    template's tool preamble), and 32 per request.
  - Prose is about 4 bytes per token, so the estimate is about 2x the real count there. It is
    meant to be at or above the real count, but it is not a tokenizer.
- **Measured, not assumed.**
  - Every usage row stores `prompt_tokens_est` (migration 0012).
  - At the end of each run, `bench run` prints and stores how many successful calls measured
    more prompt tokens than estimated (`estimate-<ts>.json`).
  - A call where the estimate is below the real count could still end a task above its ceiling.
    That count is the evidence for or against the "hard" claim.
- **Test.** A hermetic test runs the real A8a token-burner executor against the real meter app
  with a fake upstream. With the ceiling set so that the old rule admits a 7th call (which would
  end above the ceiling), the hard cap stops it after 6 calls, at 6504 <= 7558 tokens. With the
  rule reverted to the old one, the test fails.
- **no-metercap.** The ablation profile (ADR-0053) turns the whole per-task mechanism off;
  global per-call caps stay on.

## Status
Accepted (Phase 5b). Hard-cap amendment: Phase 6b, implemented and unit-tested. The estimate's
under-count rate is first measured in the 6b dev validation.
