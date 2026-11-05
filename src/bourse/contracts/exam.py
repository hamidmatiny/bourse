"""Admission exam result contracts (ADR-0037)."""

from pydantic import BaseModel, ConfigDict, Field


class ExamTaskOutcome(BaseModel):
    """One exam task: passed, grader-failed (with detail), EXAM_TIMEOUT, or ERROR."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    skill: str
    task_id: str
    outcome: str  # passed | grader-failed | EXAM_TIMEOUT | ERROR
    detail: str = ""


class ExamReport(BaseModel):
    """Last finished exam for an agent (exposed on GET /agents/{id}/exam-result)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str
    model: str
    passed: bool
    per_skill: dict[str, int] = Field(default_factory=dict)
    n: int = Field(ge=0)
    total: int = Field(ge=0)
    tasks: tuple[ExamTaskOutcome, ...] = ()
