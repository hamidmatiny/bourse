"""Typed subtask DAG produced by the broker planner (Phase 3)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bourse.contracts.metering import BudgetSpec
from bourse.contracts.skills import SkillId


class Subtask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    skill: SkillId
    depends_on: tuple[str, ...] = ()
    input_ref: str


class TaskDAG(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str = Field(min_length=1)
    subtasks: tuple[Subtask, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_graph(self) -> "TaskDAG":
        ids = [s.id for s in self.subtasks]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate subtask ids: {dupes}")
        known = set(ids)
        for s in self.subtasks:
            missing = [d for d in s.depends_on if d not in known]
            if missing:
                raise ValueError(f"subtask {s.id!r} depends on unknown ids: {missing}")
        # Kahn's algorithm: every node must be removable for the graph to be acyclic.
        indegree = {s.id: len(set(s.depends_on)) for s in self.subtasks}
        dependents: dict[str, list[str]] = {i: [] for i in ids}
        for s in self.subtasks:
            for d in set(s.depends_on):
                dependents[d].append(s.id)
        ready = [i for i, n in indegree.items() if n == 0]
        removed = 0
        while ready:
            node = ready.pop()
            removed += 1
            for child in dependents[node]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
        if removed != len(ids):
            cyclic = sorted(i for i, n in indegree.items() if n > 0)
            raise ValueError(f"task graph has a cycle among: {cyclic}")
        return self


class IntakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1)
    dataset_uri: str = Field(min_length=1)
    requested_skills: tuple[SkillId, ...] = Field(min_length=1)
    budget: BudgetSpec
    submitted_at: datetime
