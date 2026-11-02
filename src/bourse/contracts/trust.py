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


class EgressEvent(BaseModel):
    """One connection caught by the egress sinkhole (append-only egress_events, ADR-0035)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    src_ip: str
    agent_id: str | None  # identity from the static IP map; None = unattributed
    host: str | None  # HTTP Host header or TLS SNI
    port: int
    bytes: int = Field(ge=0)
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    honeytoken_hits: int = Field(ge=0)
    at: datetime
