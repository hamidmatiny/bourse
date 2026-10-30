"""Registry contracts: vendors, agents, immutable card revisions, outcomes (ADR-0013)."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bourse.contracts.approval import ApprovalDecision
from bourse.contracts.skills import SkillId
from bourse.contracts.trust import TrustState, Violation

# JWK members that only exist on private or symmetric keys. A pinned JWKS must not carry them.
PRIVATE_JWK_MEMBERS = frozenset({"d", "p", "q", "dp", "dq", "qi", "oth", "k"})


class VerifyResult(StrEnum):
    OK = "OK"
    NO_SIGNATURE = "NO_SIGNATURE"
    BAD_SIGNATURE = "BAD_SIGNATURE"
    UNKNOWN_KID = "UNKNOWN_KID"
    UNTRUSTED_JKU = "UNTRUSTED_JKU"
    UNSUPPORTED_ALG = "UNSUPPORTED_ALG"
    MALFORMED = "MALFORMED"


class RejectReason(StrEnum):
    FORGED_SIGNATURE = "FORGED_SIGNATURE"
    TAMPERED = "TAMPERED"
    UNTRUSTED_JKU = "UNTRUSTED_JKU"
    UNSUPPORTED_ALG = "UNSUPPORTED_ALG"
    UNSIGNED = "UNSIGNED"
    MALFORMED_CARD = "MALFORMED_CARD"
    UNKNOWN_SKILL = "UNKNOWN_SKILL"
    DOMAIN_MISMATCH = "DOMAIN_MISMATCH"
    VERSION_ROLLBACK = "VERSION_ROLLBACK"
    VERSION_REUSE = "VERSION_REUSE"
    CARD_TOO_LARGE = "CARD_TOO_LARGE"
    FETCH_FAILED = "FETCH_FAILED"
    UNKNOWN_VENDOR = "UNKNOWN_VENDOR"
    UNKNOWN_AGENT = "UNKNOWN_AGENT"
    AGENT_REVOKED = "AGENT_REVOKED"


class VendorKeys(BaseModel):
    """Pinned verification keys. Keys are never fetched from a card's `jku`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    jwks_url: str = Field(min_length=1)
    jwks: dict[str, Any]

    @field_validator("jwks")
    @classmethod
    def _public_only(cls, value: dict[str, Any]) -> dict[str, Any]:
        keys = value.get("keys")
        if not isinstance(keys, list) or not keys:
            raise ValueError("jwks must contain a non-empty 'keys' list")
        for key in keys:
            if not isinstance(key, dict) or not isinstance(key.get("kid"), str):
                raise ValueError("every JWK must be an object with a string 'kid'")
            leaked = sorted(PRIVATE_JWK_MEMBERS & key.keys())
            if leaked:
                raise ValueError(f"jwks contains private key members {leaked}")
        return value


class Vendor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    domain: str = Field(min_length=1, pattern=r"^[a-z0-9.-]+$")
    name: str = Field(min_length=1)
    keys: VendorKeys
    created_at: datetime


class AgentRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str
    vendor_domain: str
    name: str
    card_url: str | None
    state: TrustState
    current_version: str
    current_digest: str
    skills: tuple[SkillId, ...]
    tags: tuple[str, ...]
    created_at: datetime
    updated_at: datetime


class CardRevision(BaseModel):
    """One accepted card version. Immutable: unique per (agent_id, version)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str
    version: str
    digest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verify_result: VerifyResult
    received_at: datetime
    card: dict[str, Any]


class StateTransition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str
    from_state: TrustState
    to_state: TrustState
    actor: str
    reason: str
    approval: ApprovalDecision | None = None
    at: datetime


class RegistrationOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    accepted: bool
    reason: RejectReason | None = None
    agent_id: str | None = None
    version: str | None = None
    verify_result: VerifyResult | None = None
    state: TrustState | None = None
    unchanged: bool = False
    violation: Violation | None = None

    @model_validator(mode="after")
    def _reason_iff_rejected(self) -> "RegistrationOutcome":
        if self.accepted == (self.reason is not None):
            raise ValueError("reason must be set exactly when accepted is False")
        return self


class VendorCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    domain: str = Field(min_length=1, pattern=r"^[a-z0-9.-]+$")
    name: str = Field(min_length=1)
    keys: VendorKeys


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    vendor_domain: str
    card_url: str | None = None
    card: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "RegisterRequest":
        if (self.card_url is None) == (self.card is None):
            raise ValueError("exactly one of card_url or card is required")
        return self


class StateChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    to_state: TrustState
    reason: str = Field(min_length=1)
    approval: ApprovalDecision | None = None


class ApiError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: int
    detail: str
