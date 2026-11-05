"""Fire the four ADR-0010 triggers via an Approver (ADR-0048)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal

from bourse.approval.base import Approver
from bourse.approval.facts import make_request
from bourse.broker.abort import PREFIXES
from bourse.contracts.approval import (
    ApprovalDecision,
    ApprovalFacts,
    ApprovalTrigger,
    ViolationKindCount,
)
from bourse.contracts.broker import DelegationState, LedgerRow
from bourse.contracts.trust import Severity, Violation
from bourse.ledger.recorder import agent_major

log = logging.getLogger(__name__)

HIGH_REP_WILSON = 0.8
PAID_FRACTION = Decimal("0.5")


async def notify_high_rep_violation(
    approver: Approver,
    *,
    agent_id: str,
    vendor: str,
    violation: Violation,
    wilson_lb: float,
    strikes: int | None = None,
    timeout_s: float = 1.0,
) -> ApprovalDecision:
    """Notify-only. HARD + wilson_lb >= 0.8."""
    facts = ApprovalFacts(
        agent_id=agent_id,
        vendor=vendor,
        violation_kinds=(ViolationKindCount(kind=violation.kind, count=1),),
        strikes=strikes,
        wilson_lb=wilson_lb,
    )
    req = make_request(ApprovalTrigger.HIGH_REP_VIOLATION, facts)
    return await approver.request(req, timeout_s)


async def notify_abort(
    approver: Approver,
    *,
    abort_reason: str,
    timeout_s: float = 1.0,
) -> ApprovalDecision | None:
    """Notify-only when abort_reason starts with a closed prefix."""
    prefix = next((p for p in PREFIXES if abort_reason.startswith(p)), None)
    if prefix is None:
        return None
    facts = ApprovalFacts(abort_prefix=prefix)
    req = make_request(ApprovalTrigger.ABORT_NOTICE, facts)
    return await approver.request(req, timeout_s)


async def request_paid_call(
    approver: Approver,
    *,
    projected_usd: Decimal,
    usd_cap: Decimal,
    usd_spent: Decimal,
    timeout_s: float,
    subject_id: str = "paid",
) -> ApprovalDecision:
    """Approval when projected cost > 50% of usd_cap (caller checks the threshold)."""
    facts = ApprovalFacts(
        agent_id=subject_id,
        projected_usd=projected_usd,
        usd_cap=usd_cap,
        usd_spent=usd_spent,
    )
    req = make_request(ApprovalTrigger.PAID_CALL, facts)
    return await approver.request(req, timeout_s)


def paid_call_needs_approval(*, projected_usd: Decimal, usd_cap: Decimal, allow_paid: bool) -> bool:
    if not allow_paid or usd_cap <= 0:
        return False
    return projected_usd > (usd_cap * PAID_FRACTION)


def wilson_lb_from_rows(rows: Sequence[LedgerRow], agent_id: str, version: str) -> float:
    major = agent_major(version)
    scored = [
        r
        for r in rows
        if r.agent_id == agent_id
        and r.agent_major == major
        and r.state is not DelegationState.CONTENTION
    ]
    from bourse.broker.reliability import agent_stats

    return agent_stats(scored, alpha=0.3).wilson_lb if scored else 0.0


def is_high_rep_hard(violation: Violation, wilson_lb: float) -> bool:
    return violation.severity is Severity.HARD and wilson_lb >= HIGH_REP_WILSON


Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)
