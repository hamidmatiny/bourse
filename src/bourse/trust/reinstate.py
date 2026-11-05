"""Reinstatement: QUARANTINED → exam → REINSTATE_AGENT approval → ACTIVE (ADR-0048)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from bourse.approval.base import Approver
from bourse.approval.facts import make_request
from bourse.contracts.approval import (
    ApprovalDecision,
    ApprovalFacts,
    ApprovalTrigger,
    ViolationKindCount,
)
from bourse.contracts.exam import ExamReport
from bourse.contracts.registry import StateTransition
from bourse.contracts.trust import TrustState
from bourse.registry.repo import Repository
from bourse.registry.state import IllegalTransition, UnknownAgent, transition
from bourse.trust.reliability_epoch import bump_rel_epoch


class ReinstateExamRunner(Protocol):
    """What reinstatement needs from bourse.trust.exam.ExamRunner."""

    async def run_for_reinstate(self, agent_id: str) -> ExamReport | None: ...


@dataclass(frozen=True)
class ReinstateResult:
    status: Literal[
        "active",
        "denied",
        "exam_failed",
        "revoked",
        "not_quarantined",
        "unknown",
    ]
    agent_id: str
    exam: ExamReport | None = None
    decision: ApprovalDecision | None = None
    transition: StateTransition | None = None
    detail: str = ""


async def _audit_stay_quarantined(
    repo: Repository,
    agent_id: str,
    *,
    actor: str,
    reason: str,
    approval: ApprovalDecision | None,
    now: datetime,
) -> StateTransition:
    """Write an audit row without changing state (same-state transition not allowed).

    Uses a synthetic ACTIVE→QUARANTINED-style approach: we append a StateTransition with
    from=to=QUARANTINED via save_transition after a no-op check bypass — not allowed by
    check_transition. Instead: store the denial/exam-fail as a transition reason by
    writing through repo.save_transition with from_state=QUARANTINED, to_state=QUARANTINED
    only if the repository accepts it. Prefer recording via an explicit audit helper.
    """
    agent = await repo.get_agent(agent_id)
    assert agent is not None
    record = StateTransition(
        agent_id=agent_id,
        from_state=TrustState.QUARANTINED,
        to_state=TrustState.QUARANTINED,
        actor=actor,
        reason=reason,
        approval=approval,
        at=now,
    )
    # Bypass check_transition: audit-only same-state row for reinstate outcomes.
    await repo.save_transition(agent.model_copy(update={"updated_at": now}), record)
    return record


async def reinstate_agent(
    repo: Repository,
    exam_runner: ReinstateExamRunner,
    approver: Approver,
    agent_id: str,
    *,
    reason: str,
    timeout_s: float,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    exposure: int | None = None,
) -> ReinstateResult:
    """Full reinstatement flow. REVOKED never reinstatable."""
    agent = await repo.get_agent(agent_id)
    if agent is None:
        return ReinstateResult(status="unknown", agent_id=agent_id, detail="unknown agent")
    if agent.state is TrustState.REVOKED:
        return ReinstateResult(
            status="revoked", agent_id=agent_id, detail="REVOKED is never reinstatable"
        )
    if agent.state is not TrustState.QUARANTINED:
        return ReinstateResult(
            status="not_quarantined",
            agent_id=agent_id,
            detail=f"state={agent.state.value}; only QUARANTINED may reinstate",
        )

    violations = await repo.list_violations(agent_id)
    kind_counts: dict[str, int] = {}
    for v in violations:
        kind_counts[v.kind] = kind_counts.get(v.kind, 0) + 1
    violation_kinds = tuple(
        ViolationKindCount(kind=k, count=c) for k, c in sorted(kind_counts.items())
    )

    exam = await exam_runner.run_for_reinstate(agent_id)
    now = clock()
    if exam is None:
        audit = await _audit_stay_quarantined(
            repo,
            agent_id,
            actor="reinstate",
            reason=f"reinstate exam not run; operator reason={reason!r}",
            approval=None,
            now=now,
        )
        return ReinstateResult(
            status="exam_failed",
            agent_id=agent_id,
            exam=None,
            transition=audit,
            detail="exam returned None",
        )
    if not exam.passed:
        audit = await _audit_stay_quarantined(
            repo,
            agent_id,
            actor="reinstate",
            reason=f"reinstate exam failed {exam.n}/{exam.total}; operator reason={reason!r}",
            approval=None,
            now=now,
        )
        return ReinstateResult(
            status="exam_failed",
            agent_id=agent_id,
            exam=exam,
            transition=audit,
            detail=f"exam {exam.n}/{exam.total}",
        )

    facts = ApprovalFacts(
        agent_id=agent_id,
        vendor=agent.vendor_domain,
        violation_kinds=violation_kinds,
        exposure=exposure,
        exam_n=exam.n,
        exam_total=exam.total,
    )
    req = make_request(ApprovalTrigger.REINSTATE_AGENT, facts, created_at=now)
    decision = await approver.request(req, timeout_s)
    now = clock()
    if not decision.approved:
        audit = await _audit_stay_quarantined(
            repo,
            agent_id,
            actor="reinstate",
            reason=(
                f"reinstate denied by {decision.by}: {decision.reason}; "
                f"exam {exam.n}/{exam.total}; operator reason={reason!r}"
            ),
            approval=decision,
            now=now,
        )
        return ReinstateResult(
            status="denied",
            agent_id=agent_id,
            exam=exam,
            decision=decision,
            transition=audit,
            detail=decision.reason,
        )

    try:
        record = await transition(
            repo,
            agent_id,
            TrustState.ACTIVE,
            actor="reinstate",
            reason=(
                f"reinstated by {decision.by}; exam {exam.n}/{exam.total}; "
                f"operator reason={reason!r}"
            ),
            approval=decision,
            now=now,
            agent_updates={"tags": bump_rel_epoch(agent.tags)},
        )
    except (IllegalTransition, UnknownAgent) as exc:
        return ReinstateResult(
            status="denied",
            agent_id=agent_id,
            exam=exam,
            decision=decision,
            detail=str(exc),
        )
    return ReinstateResult(
        status="active",
        agent_id=agent_id,
        exam=exam,
        decision=decision,
        transition=record,
        detail=f"ACTIVE exam {exam.n}/{exam.total}",
    )
