"""Trust states and violations. Decided by deterministic checks only (ADR-0008)."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class TrustState(StrEnum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    QUARANTINED = "QUARANTINED"
    REVOKED = "REVOKED"


class Severity(StrEnum):
    HARD = "HARD"
    SOFT = "SOFT"


class Violation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(min_length=1)
    severity: Severity
    agent_id: str = Field(min_length=1)
    evidence: str
    at: datetime


class ViolationReport(BaseModel):
    """Body of POST /agents/{agent_id}/violations (admin). The registry stamps agent_id and at."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")
    severity: Severity
    evidence: str = Field(max_length=1000)
