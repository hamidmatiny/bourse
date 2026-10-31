"""Planner (ADR-0024): request text -> TaskDAG over the closed skill taxonomy.

The LLM prompt holds exactly two inputs: the operator's request_text and SKILL_TAXONOMY below,
which is written in this file. No Agent Card text and no registry data are ever passed in.
The LLM answers through the submit tool; PlanSubmission validates the graph (TaskDAG) and the
plan policy, so policy errors are fed back to the model by the submit-tool retry loop. After
the retries, or on any LLM failure, the deterministic template plan is used.
"""

import json
from dataclasses import dataclass
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from bourse.contracts.skills import SkillId
from bourse.contracts.tasks import IntakeRequest, Subtask, TaskDAG
from bourse.llm.base import ChatMessage
from bourse.llm.router import AllProvidersFailed
from bourse.llm.submit_tool import ChatClient, SubmitFailed, submit
from bourse.runtime.breaker import BreakerOpen, ProviderHTTPError
from bourse.runtime.budget import BudgetExceeded
from bourse.runtime.deadline import DeadlineExceeded

PLANNER_MAX_ATTEMPTS = 3  # first answer + 2 retries
PLANNER_MAX_TOKENS = 512
PLANNER_DEADLINE_S = 120.0
INPUT_REF = "dataset"

SKILL_TAXONOMY: dict[SkillId, str] = {
    SkillId.SCHEMA_PROFILE: "column names, dtypes, null and distinct counts of the dataset",
    SkillId.STATS_COMPUTE: "mean, std, min, max, median of numeric columns",
    SkillId.PII_DETECT: "cells holding personal data: emails, phones, card numbers, SINs, names",
    SkillId.UNITS_NORMALIZE: "unit conversions of measured columns to canonical units",
    SkillId.REPORT_COMPOSE: "short written report citing numbers from the other skills' outputs",
}

PLAN_RULES = (
    "Plan the subtasks needed for the request below. Rules: use only the skills listed; use "
    "each skill at most once; always include report.compose; report.compose must depend on "
    "the schema.profile and stats.compute subtasks; report.compose only sees the outputs of "
    "subtasks it depends on, directly or through other subtasks; subtask ids are short "
    "lowercase words. Call the submit tool once with the subtasks."
)


class PlannedSubtask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    skill: SkillId
    depends_on: list[str] = Field(default_factory=list, max_length=len(SkillId))


class PlanSubmission(BaseModel):
    """What the planner LLM submits. Validation = TaskDAG graph checks + plan policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    subtasks: list[PlannedSubtask] = Field(min_length=1, max_length=len(SkillId))

    @model_validator(mode="after")
    def _policy(self) -> "PlanSubmission":
        to_dag("validate", self)  # duplicate ids, unknown deps, cycles
        policy_errors(self)
        return self


def policy_errors(plan: PlanSubmission) -> None:
    skills = [s.skill for s in plan.subtasks]
    repeated = sorted({s.value for s in skills if skills.count(s) > 1})
    if repeated:
        raise ValueError(f"each skill at most once; repeated: {repeated}")
    by_skill = {s.skill: s for s in plan.subtasks}
    report = by_skill.get(SkillId.REPORT_COMPOSE)
    if report is None:
        raise ValueError("plan must contain report.compose")
    for needed in (SkillId.SCHEMA_PROFILE, SkillId.STATS_COMPUTE):
        dep = by_skill.get(needed)
        if dep is None or dep.id not in report.depends_on:
            raise ValueError(f"report.compose must depend on a {needed.value} subtask")


def to_dag(job_id: str, plan: PlanSubmission) -> TaskDAG:
    return TaskDAG(
        job_id=job_id,
        subtasks=tuple(
            Subtask(id=s.id, skill=s.skill, depends_on=tuple(s.depends_on), input_ref=INPUT_REF)
            for s in plan.subtasks
        ),
    )


TEMPLATE_PLAN = PlanSubmission(
    subtasks=[
        PlannedSubtask(id="profile", skill=SkillId.SCHEMA_PROFILE),
        PlannedSubtask(id="stats", skill=SkillId.STATS_COMPUTE, depends_on=["profile"]),
        PlannedSubtask(id="pii", skill=SkillId.PII_DETECT),
        PlannedSubtask(id="units", skill=SkillId.UNITS_NORMALIZE),
        PlannedSubtask(
            id="report",
            skill=SkillId.REPORT_COMPOSE,
            depends_on=["profile", "stats", "pii", "units"],
        ),
    ]
)


def plan_messages(request_text: str) -> list[ChatMessage]:
    taxonomy = {s.value: text for s, text in SKILL_TAXONOMY.items()}
    return [
        ChatMessage(role="system", content="You plan data jobs. Answer only by calling submit."),
        ChatMessage(
            role="user",
            content=(
                f"{PLAN_RULES}\n\nSkills:\n{json.dumps(taxonomy, sort_keys=True)}\n\n"
                f"Request:\n{json.dumps(request_text)}"
            ),
        ),
    ]


@dataclass(frozen=True)
class PlanResult:
    dag: TaskDAG
    planner_used: Literal["llm", "template"]
    llm_error: str | None = None


LLM_FAILURES = (
    SubmitFailed,
    AllProvidersFailed,
    BreakerOpen,
    ProviderHTTPError,
    BudgetExceeded,
    DeadlineExceeded,
    httpx.HTTPError,
)


async def make_plan(run_id: str, request: IntakeRequest, llm: ChatClient) -> PlanResult:
    """`llm` must be a router to the metering proxy tagged with X-Bourse-Task-Id = run_id."""
    try:
        submitted = await submit(
            llm,
            plan_messages(request.request_text),
            PlanSubmission,
            max_attempts=PLANNER_MAX_ATTEMPTS,
            max_tokens=PLANNER_MAX_TOKENS,
            deadline_s=PLANNER_DEADLINE_S,
        )
    except LLM_FAILURES as exc:
        return PlanResult(to_dag(run_id, TEMPLATE_PLAN), "template", type(exc).__name__)
    return PlanResult(to_dag(run_id, submitted), "llm")
