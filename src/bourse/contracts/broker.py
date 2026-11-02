"""Broker contracts: plans, selection decisions, delegations, runs, reports (ADR-0024..0027)."""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from bourse.contracts.skills import SkillId
from bourse.contracts.tasks import TaskDAG

PlannerUsed = Literal["llm", "template"]


class UtilityTerms(BaseModel):
    """utility = reliability_lb - lambda * cost - mu * latency (ADR-0025).

    MeasuredPolicy (ADR-0031) also fills s, f, the normalized cost/latency it used, and whether
    cost/latency are measured (EWMA) or cold-start priors."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reliability_lb: float = Field(ge=0, le=1)
    cost: float = Field(ge=0)
    latency: float = Field(ge=0)
    utility: float
    s: int = Field(default=0, ge=0)
    f: int = Field(default=0, ge=0)
    cost_norm: float | None = None
    latency_norm: float | None = None
    measured: bool = False


class CandidateScore(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str
    terms: UtilityTerms


class SelectionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    subtask_id: str
    skill: SkillId
    candidates: tuple[CandidateScore, ...]
    chosen: str | None
    fallbacks: tuple[str, ...]
    reason: str


class DelegationState(StrEnum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"  # agent reported TASK_STATE_FAILED
    REJECTED = "REJECTED"  # agent reported TASK_STATE_REJECTED
    FAILED_CONTRACT = "FAILED_CONTRACT"  # artifact missing or not the SKILL_IO output model
    FAILED_SPOTCHECK = "FAILED_SPOTCHECK"  # valid output failed a deterministic spot check
    TIMEOUT = "TIMEOUT"  # per-delegation wall-clock deadline
    INTERRUPTED = "INTERRUPTED"  # INPUT_REQUIRED / AUTH_REQUIRED (policy in Phase 5)
    CARD_DRIFT = "CARD_DRIFT"  # live card digest != registry digest; no message sent
    ERROR = "ERROR"  # card fetch or transport failure


class DelegationRecord(BaseModel):
    """One delegation attempt. Append-only (alembic 0003 trigger)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    subtask_id: str
    skill: SkillId
    agent_id: str
    revision_digest: str
    attempt: int = Field(ge=1)
    state: DelegationState
    error_kind: str | None = None
    wall_ms: float = Field(ge=0)
    at: datetime


class SubtaskStatus(StrEnum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"  # a dependency did not complete
    ABORTED = "ABORTED"  # run stopped by breaker, watchdog or wall budget


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


class SubtaskResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    subtask_id: str
    skill: SkillId
    status: SubtaskStatus
    agent_id: str | None = None
    attempts: int = Field(ge=0)
    error_kind: str | None = None
    output: dict[str, Any] | None = None


class RunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    request_text: str
    plan: TaskDAG
    planner_used: PlannerUsed
    status: RunStatus
    started_at: datetime
    ended_at: datetime | None = None
    wall_ms: float | None = None


class IntakeReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    status: RunStatus
    planner_used: PlannerUsed
    plan: TaskDAG
    subtasks: tuple[SubtaskResult, ...]
    outputs: dict[str, dict[str, Any]]  # SkillId value -> validated output (completed only)
    delegations: tuple[DelegationRecord, ...]
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    wall_ms: float = Field(ge=0)
    abort_reason: str | None = None
    trace_id: str | None = None


class SelectionRecord(BaseModel):
    """One persisted selection decision (append-only selection_decisions table)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    subtask_id: str
    skill: SkillId
    candidates: tuple[CandidateScore, ...]
    chosen: str | None
    reason: str
    at: datetime


class LedgerRow(BaseModel):
    """One delegation attempt with measured usage (append-only ledger_entries, ADR-0029)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    subtask_id: str
    attempt: int = Field(ge=1)
    agent_id: str
    skill: SkillId
    revision_digest: str
    agent_major: int = Field(ge=0)
    state: DelegationState
    error_kind: str | None = None
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    shadow_usd: float = Field(ge=0)
    price_table_version: str
    wall_ms: float = Field(ge=0)
    spot_check_passed: bool | None = None
    at: datetime
    kind: Literal["task", "canary"] = "task"  # canaries count in reliability (ADR-0037)


class AgentScore(BaseModel):
    """Reliability and EWMA cost/latency for one (agent, skill, major) (ADR-0030)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str
    skill: SkillId
    agent_major: int = Field(ge=0)
    s: int = Field(ge=0)
    f: int = Field(ge=0)
    wilson_lb: float = Field(ge=0, le=1)
    ewma_usd: float | None = None
    ewma_wall_ms: float | None = None
