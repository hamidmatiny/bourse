# 0028 — Manual activation before the admission exam (temporary)

## Context
The broker selects only ACTIVE agents. Registration leaves agents PENDING, and the admission
exam that should move them to ACTIVE is built in Phase 5.

## Decision
`bourse stack activate --all` performs the admin transition PENDING -> ACTIVE for every PENDING
agent through `POST /agents/{id}/state`, with the audit reason "pre-exam manual activation
(Phase 3; replaced by admission exam in Phase 5)". Each transition writes a state_transitions
row (ADR-0013).

## Consequences
- Until Phase 5, an agent is ACTIVE because an operator said so, not because it passed a test.
  The audit rows make every such activation identifiable later.

## Status
Accepted (Phase 3). Superseded by the admission exam in Phase 5.
