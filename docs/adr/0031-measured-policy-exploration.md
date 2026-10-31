# 0031 — MeasuredPolicy and exploration quota

## Context
Phase 3 selected by priors only (ADR-0025). With a ledger, selection can use measured
reliability, cost and latency, but an agent that is never chosen never gets measured.

## Decision
- `SelectionPolicy` gains `async refresh(skill, candidates)`, called before `choose`;
  PriorPolicy's is a no-op and PriorPolicy stays for baselines (`policy="prior"`).
- `MeasuredPolicy` (default): utility = LB − λ·cost_norm − μ·latency_norm, LB = Wilson lower
  bound (ADR-0030); cost = EWMA of shadow_usd, latency = EWMA of wall_ms over the agent's rows
  for the skill and current major (oldest first, α = `BOURSE_SELECT_EWMA_ALPHA`, default 0.3).
  cost_norm and latency_norm divide by the maximum among the candidates. Cold start (no rows):
  the Phase 3 priors converted to the same units: shadow price per 1M tokens x 2000 tokens / 1e6
  USD, and latency prior x 1000 ms. Defaults λ = 0.2, μ = 0.2. Ties by agent_id.
- Exploration quota: if any candidate has fewer than `min_trials` (default 5) rows for the
  skill and there are at least 2 candidates, every `explore_every`-th (default 4) selection
  decision for that skill — counted from persisted selection_decisions — goes to the
  least-tried candidate (ties by agent_id); reason "exploration".
- Every decision is persisted in `selection_decisions(run_id, subtask_id, skill, candidates
  jsonb with all terms, chosen, reason, at)` (alembic 0005, append-only).

## Consequences
- The same ledger and decision history always give the same choice (unit-tested over 12
  consecutive decisions: exploration at decisions 4, 8 and 12).
- λ and μ are configuration, not learned values.

## Status
Accepted (Phase 4).
