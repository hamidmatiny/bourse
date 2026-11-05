"""Human approval requests (ADR-0010, ADR-0048). Every request times out to deny."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ApprovalTrigger(StrEnum):
    REINSTATE_AGENT = "REINSTATE_AGENT"
    PAID_CALL = "PAID_CALL"
    HIGH_REP_VIOLATION = "HIGH_REP_VIOLATION"
    ABORT_NOTICE = "ABORT_NOTICE"


class ViolationKindCount(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(min_length=1)
    count: int = Field(ge=0)


class ApprovalFacts(BaseModel):
    """Structured facts only. Never card free text or agent-produced text (ADR-0048)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str | None = None
    vendor: str | None = None
    violation_kinds: tuple[ViolationKindCount, ...] = ()
    strikes: int | None = Field(default=None, ge=0)
    exposure: int | None = Field(default=None, ge=0)
    exam_n: int | None = Field(default=None, ge=0)
    exam_total: int | None = Field(default=None, ge=0)
    projected_usd: Decimal | None = None
    usd_cap: Decimal | None = None
    usd_spent: Decimal | None = None
    abort_prefix: str | None = None
    wilson_lb: float | None = Field(default=None, ge=0.0, le=1.0)


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1)
    trigger: ApprovalTrigger
    subject_id: str
    summary: str
    created_at: datetime
    facts: ApprovalFacts = Field(default_factory=ApprovalFacts)


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    approved: bool
    by: str
    at: datetime
    reason: str
    # Which request this decides, and which button produced it (approve | deny | timeout |
    # unreachable). Persisted in state_transitions.approval for audit. None on legacy rows.
    request_id: str | None = None
    action: str | None = None
