# 0025 — Selection policy interface and Phase 3 priors

## Context
Several ACTIVE agents can offer the same skill (pii.detect: pii_precise and pii_fast). Measured
reliability, cost and latency arrive with the ledger in Phase 4; Phase 3 needs a deterministic
choice now behind an interface that Phase 4 can replace.

## Decision
- `SelectionPolicy.choose(subtask, candidates) -> SelectionDecision(candidates with
  UtilityTerms{reliability_lb, cost, latency, utility}, chosen, fallbacks, reason)`.
- Candidates come only from the registry API: `GET /agents?skill=X&state=ACTIVE`.
- `PriorPolicy`: utility = reliability_lb − λ·cost − μ·latency, ranked by utility descending,
  ties broken by agent_id ascending. reliability_lb = 0.5 for every agent. cost = pinned shadow
  price of the agent's model (USD per 1M tokens, not billed); latency = pinned prior (s).
  Defaults (config, `BOURSE_BROKER_*`): gemma4:12b 0.10 / 5 s, qwen3.5:27b-q4_K_M 0.25 / 60 s,
  λ = 1.0, μ = 0.005. These numbers are placeholders, not measurements.
- The Agent Card does not state a model, so the operator pins agent_id → model in config. An
  agent or model not in the table gets the highest configured price and latency.

## Consequences
- With the defaults, pii_fast (utility 0.5 − 0.10 − 0.025 = 0.375) ranks before pii_precise
  (0.5 − 0.25 − 0.30 = −0.05); pii_precise is the fallback.
- Same candidates + same config always give the same decision (unit-tested, including ties).
- Phase 4 replaces PriorPolicy with measured terms without changing the broker.

## Status
Accepted (Phase 3).
