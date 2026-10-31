"""Deterministic grade of one skill output against ground truth (ADR-0023)."""

from pydantic import BaseModel, ConfigDict, Field


class Grade(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: bool
    score: float = Field(ge=0, le=1)
    n: int = Field(ge=0)
    N: int = Field(ge=0)  # n/N is the reporting convention (rule 11)
    detail: str
