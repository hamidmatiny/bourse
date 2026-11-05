# 0039 — Trust engine and strike policy

## Context
Phase 5a recorded attributed HARD violations (egress, exam, card drift) but did not
automatically quarantine. The broker could keep delegating to an agent after evidence that
should have taken it out of the roster.

## Decision
- `TrustEngine` is the only path that moves ACTIVE -> QUARANTINED. It consumes violations from
  the registry, sinkhole, broker and meter. No LLM participates.
- HARD + attributed + ACTIVE -> QUARANTINED immediately (actor `trust`).
- SOFT + attributed -> strike. k strikes (config `BOURSE_TRUST_STRIKES`, default 3) within
  window W (config `BOURSE_TRUST_WINDOW_S`, default 86400) -> QUARANTINED.
- Unattributed (pushed inline card) violations never change state.
- Selection reads ACTIVE at `choose()` and the delegator re-fetches the agent immediately
  before each send. If the agent is no longer ACTIVE the attempt is `CANCELED_QUARANTINE` and
  no A2A message is sent; the broker falls back.
- On quarantine, in-flight A2A tasks to that agent are `CancelTask`'d
  (`CANCELED_QUARANTINE`).
- QUARANTINED -> ACTIVE still requires an approved `ApprovalDecision` (ADR-0010). DenyApprover
  keeps the agent quarantined. Slack reinstatement is Phase 5c.

## Consequences
- Three SOFT failures (spot check, session smuggling, usage misreport, canary fail, card drift)
  in 24h take an agent off the roster without a human.
- A race between choose and send cannot deliver work to a just-quarantined agent.

## Status
Accepted (Phase 5b).
