"""Admission exam result contracts (ADR-0037, ADR-0049)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ExamTaskOutcome(BaseModel):
    """One exam task slot (final attempt after INFRA retries).

    outcome: passed | grader-failed | ERROR | INFRA | EXAM_TIMEOUT
    A timed-out task is ERROR (agent fault) or INFRA by meter evidence (ADR-0049 amendment 2).
    infra_retries: how many INFRA failures were retried before this final outcome.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    skill: str
    task_id: str
    outcome: str
    detail: str = ""
    infra_retries: int = Field(default=0, ge=0)
    # ADR-0049 amendment 2: who is at fault for a failed/timed-out task, the rule that decided
    # (A1..A4 agent, I1..I5 infra; see bourse.trust.exam_fault) and the meter evidence.
    fault: Literal["agent", "infra"] | None = None
    fault_rule: str | None = None
    evidence: str = ""


class ExamReport(BaseModel):
    """Last finished exam for an agent (exposed on GET /agents/{id}/exam-result)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str
    model: str
    passed: bool
    inconclusive: bool = False
    per_skill: dict[str, int] = Field(default_factory=dict)
    n: int = Field(ge=0)
    total: int = Field(ge=0)
    tasks: tuple[ExamTaskOutcome, ...] = ()


class ExamProgress(BaseModel):
    """Live state of one exam (GET /exams/progress), for wait-active progress lines.

    status: queued (waiting for the global exam slot) | running.
    task_n is 1-based over all skills (k per skill); 0 while queued.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str
    mode: str  # admission | reinstate
    status: str  # queued | running
    skill: str = ""
    task_n: int = Field(default=0, ge=0)
    task_total: int = Field(default=0, ge=0)
    attempt: int = Field(default=0, ge=0)
    exam_elapsed_s: float = Field(default=0.0, ge=0)
    task_elapsed_s: float = Field(default=0.0, ge=0)
    task_deadline_s: float = Field(default=0.0, ge=0)
