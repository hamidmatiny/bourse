# 0005 — Free-model-first routing, budget ledger, global circuit breaker

## Context
Runs must not spend money by accident. Local Ollama is free; OmniRoute is a free/overflow
OpenAI-compatible gateway; paid APIs cost money and can fail with billing errors mid-run.

## Decision
- Provider order: Ollama → OmniRoute → paid. Paid is skipped unless `allow_paid=True` and
  `usd_cap > 0` (`BudgetSpec.paid_enabled`); `PaidProvider` also raises `PaidDisabled` itself.
- Fallthrough to the next provider on transport errors, HTTP 5xx and plain 429. Other 4xx raise.
- `BudgetLedger` (thread-safe) caps `max_tokens` per call, charges usage after each call, and raises
  `BudgetExceeded` past `max_tokens_run` or `usd_cap`.
- `CircuitBreaker` trips on HTTP 401/402/403, 429 with quota/credit/billing wording, or any body
  containing `insufficient_quota`, `credit balance` or `billing`. It does not trip on plain 5xx,
  plain 429, timeouts or connection errors. Once tripped, `guard()` raises `BreakerOpen` and the
  router makes no further requests. One process-global instance (`GLOBAL_BREAKER`).

## Consequences
- A billing error stops the run instead of falling through to another provider.
- A body containing the word "billing" on a 5xx also trips; this is intentional (fail closed).

## Status
Accepted (Phase 0).
