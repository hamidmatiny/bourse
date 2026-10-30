# 0010 — Human approval: Approver protocol, Slack for 4 triggers, deny by default

## Context
A few actions need a human: reinstating a quarantined agent, spending money, acting on a violation
by a high-reputation agent, and being told about an abort. Unattended runs and CI have no human.

## Decision
- `approval.base.Approver` protocol: `async request(ApprovalRequest, timeout_s) -> ApprovalDecision`.
- Four triggers only: `REINSTATE_AGENT`, `PAID_CALL`, `HIGH_REP_VIOLATION`, `ABORT_NOTICE`.
- Implementations: `CliApprover` (Phase 0; only "y"/"yes" approves), `DenyApprover` (CI/unattended;
  reason "unattended"), `SlackApprover` (Phase 5; free workspace, Socket Mode, no public URL).
- Every approval times out to deny.

## Consequences
- No approval path can block a run indefinitely.
- Slack tokens are only read from `.env`; never committed.

## Status
Accepted (Phase 0). SlackApprover: Phase 5.
