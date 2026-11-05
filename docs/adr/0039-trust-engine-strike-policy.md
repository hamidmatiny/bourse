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
- Strikes are counted per (agent, `rel_epoch`) (Phase 5c amendment). Every stored violation
  carries the agent's `rel_epoch` at insert time (`violations.rel_epoch`, alembic 0011; rows
  from before 0011 are epoch 0). The SOFT window counts only violations whose `rel_epoch`
  equals the agent's current epoch. Reinstatement (ADR-0048) bumps `rel_epoch`, so it resets
  strikes the same way it resets reliability. HARD is unaffected (still immediate).
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
- After reinstatement an agent needs k fresh SOFT strikes to be quarantined again. Observed
  before this amendment: A5a re-catch quarantined after 1 new strike because 2 pre-reinstate
  strikes were still in the 24h window, and the phase5 invariant failed (strikes=1 < k=3).
- Possible future policy, not implemented: probation on reinstatement (e.g. k=1 or a HARD
  threshold for the first N deliveries of the new epoch).

## Status
Accepted (Phase 5b). Amended Phase 5c: strikes per (agent, rel_epoch).
