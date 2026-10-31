"""Typed contracts: the only interface between BOURSE components (ADR-0004)."""

from bourse.contracts.approval import ApprovalDecision, ApprovalRequest, ApprovalTrigger
from bourse.contracts.grading import Grade
from bourse.contracts.metering import (
    BudgetSpec,
    LedgerEntry,
    LlmUsageRecord,
    ToolCallRecord,
    Usage,
)
from bourse.contracts.probe import ProbeReport
from bourse.contracts.registry import (
    AgentRecord,
    ApiError,
    CardRevision,
    RegisterRequest,
    RegistrationOutcome,
    RejectReason,
    StateChangeRequest,
    StateTransition,
    Vendor,
    VendorCreate,
    VendorKeys,
    VerifyResult,
)
from bourse.contracts.skills import SkillId
from bourse.contracts.tasks import IntakeRequest, Subtask, TaskDAG
from bourse.contracts.trust import Severity, TrustState, Violation

__all__ = [
    "AgentRecord",
    "ApiError",
    "ApprovalDecision",
    "ApprovalRequest",
    "ApprovalTrigger",
    "BudgetSpec",
    "CardRevision",
    "Grade",
    "IntakeRequest",
    "LedgerEntry",
    "LlmUsageRecord",
    "ProbeReport",
    "RegisterRequest",
    "RegistrationOutcome",
    "RejectReason",
    "Severity",
    "SkillId",
    "StateChangeRequest",
    "StateTransition",
    "Subtask",
    "TaskDAG",
    "ToolCallRecord",
    "TrustState",
    "Usage",
    "Vendor",
    "VendorCreate",
    "VendorKeys",
    "VerifyResult",
    "Violation",
]
