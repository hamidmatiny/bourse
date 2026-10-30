"""Human approval requests (ADR-0010). Every request times out to deny."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ApprovalTrigger(StrEnum):
    REINSTATE_AGENT = "REINSTATE_AGENT"
    PAID_CALL = "PAID_CALL"
    HIGH_REP_VIOLATION = "HIGH_REP_VIOLATION"
    ABORT_NOTICE = "ABORT_NOTICE"


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1)
    trigger: ApprovalTrigger
    subject_id: str
    summary: str
    created_at: datetime


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    approved: bool
    by: str
    at: datetime
    reason: str
