"""Typed contracts: the only interface between BOURSE components (ADR-0004)."""

from bourse.contracts.approval import ApprovalDecision, ApprovalRequest, ApprovalTrigger
from bourse.contracts.broker import (
    CandidateScore,
    DelegationRecord,
    DelegationState,
    IntakeReport,
    RunRecord,
    RunStatus,
    SelectionDecision,
    SubtaskResult,
    SubtaskStatus,
    UtilityTerms,
)
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
from bourse.contracts.trust import Severity, TrustState, Violation, ViolationReport

__all__ = [
    "AgentRecord",
    "ApiError",
    "ApprovalDecision",
    "ApprovalRequest",
    "ApprovalTrigger",
    "BudgetSpec",
    "CandidateScore",
    "CardRevision",
    "DelegationRecord",
    "DelegationState",
    "Grade",
    "IntakeReport",
    "IntakeRequest",
    "LedgerEntry",
    "LlmUsageRecord",
    "ProbeReport",
    "RegisterRequest",
    "RegistrationOutcome",
    "RejectReason",
    "RunRecord",
    "RunStatus",
    "SelectionDecision",
    "Severity",
    "SkillId",
    "StateChangeRequest",
    "StateTransition",
    "Subtask",
    "SubtaskResult",
    "SubtaskStatus",
    "TaskDAG",
    "ToolCallRecord",
    "TrustState",
    "Usage",
    "UtilityTerms",
    "Vendor",
    "VendorCreate",
    "VendorKeys",
    "VerifyResult",
    "Violation",
    "ViolationReport",
]
