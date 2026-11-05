"""Build ApprovalRequest from structured facts only (ADR-0048)."""

from datetime import UTC, datetime
from uuid import uuid4

from bourse.contracts.approval import ApprovalFacts, ApprovalRequest, ApprovalTrigger


def format_summary(trigger: ApprovalTrigger, facts: ApprovalFacts) -> str:
    """Deterministic one-line summary from facts. No free text."""
    parts: list[str] = [trigger.value]
    if facts.agent_id:
        parts.append(f"agent={facts.agent_id}")
    if facts.vendor:
        parts.append(f"vendor={facts.vendor}")
    if facts.violation_kinds:
        kinds = ",".join(f"{v.kind}:{v.count}" for v in facts.violation_kinds)
        parts.append(f"violations={kinds}")
    if facts.strikes is not None:
        parts.append(f"strikes={facts.strikes}")
    if facts.exposure is not None:
        parts.append(f"exposure={facts.exposure}")
    if facts.exam_n is not None and facts.exam_total is not None:
        parts.append(f"exam={facts.exam_n}/{facts.exam_total}")
    if facts.projected_usd is not None:
        parts.append(f"projected_usd={facts.projected_usd}")
    if facts.usd_cap is not None:
        parts.append(f"usd_cap={facts.usd_cap}")
    if facts.usd_spent is not None:
        parts.append(f"usd_spent={facts.usd_spent}")
    if facts.abort_prefix:
        parts.append(f"abort={facts.abort_prefix}")
    if facts.wilson_lb is not None:
        parts.append(f"wilson_lb={facts.wilson_lb:.4f}")
    return " ".join(parts)


def make_request(
    trigger: ApprovalTrigger,
    facts: ApprovalFacts,
    *,
    request_id: str | None = None,
    created_at: datetime | None = None,
) -> ApprovalRequest:
    subject = facts.agent_id or facts.abort_prefix or trigger.value
    return ApprovalRequest(
        request_id=request_id or uuid4().hex,
        trigger=trigger,
        subject_id=subject,
        summary=format_summary(trigger, facts),
        created_at=created_at or datetime.now(UTC),
        facts=facts,
    )


NOTIFY_TRIGGERS = frozenset({ApprovalTrigger.HIGH_REP_VIOLATION, ApprovalTrigger.ABORT_NOTICE})
APPROVAL_TRIGGERS = frozenset({ApprovalTrigger.REINSTATE_AGENT, ApprovalTrigger.PAID_CALL})
