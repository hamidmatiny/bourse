"""Baseline B0 (ADR-0051): one generalist agent does every subtask. No broker, no A2A,
no marketplace, no MCP tools, no trust controls.

For each subtask of the template plan, in dependency order, one submit-tool call (ADR-0006)
to the roster model through the meter. The prompt holds the skill name, the skill's input JSON
(built from the dataset and B0's own earlier outputs, as the broker does) and nothing from any
Agent Card. Output must validate as the skill's SKILL_IO output model.
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import anyio

from bourse.broker.delegate import build_input
from bourse.broker.graph import dependency_levels
from bourse.broker.planner import SKILL_TAXONOMY, TEMPLATE_PLAN, to_dag
from bourse.contracts.broker import SubtaskStatus
from bourse.contracts.skills import SkillId
from bourse.contracts.skills_io import SKILL_IO, DatasetRef
from bourse.contracts.tasks import TaskDAG
from bourse.llm.base import ChatMessage
from bourse.llm.submit_tool import ChatClient, submit

SYSTEM = "You are a data analyst. Answer only by calling the submit tool exactly once."


@dataclass(frozen=True)
class SoloSubtask:
    subtask_id: str
    skill: SkillId
    status: SubtaskStatus
    output: dict[str, Any] | None
    error_kind: str | None
    wall_s: float


def template_dag(run_id: str = "bench-b0") -> TaskDAG:
    return to_dag(run_id, TEMPLATE_PLAN)


def solo_messages(skill: SkillId, payload: dict[str, Any]) -> list[ChatMessage]:
    instruction = (
        f"Task: {skill.value}. {SKILL_TAXONOMY[skill]} "
        "Work from the input JSON below and call submit with the complete result."
    )
    return [
        ChatMessage(role="system", content=SYSTEM),
        ChatMessage(role="user", content=f"{instruction}\n\n{json.dumps(payload, sort_keys=True)}"),
    ]


async def run_solo(
    llm_for: Callable[[str], ChatClient],
    run_id: str,
    dataset: DatasetRef,
    *,
    max_tokens: int,
    deadline_s: float,
    max_attempts: int = 2,
) -> list[SoloSubtask]:
    """Run the template plan sequentially; a failed dependency skips its dependants."""
    dag = template_dag(run_id)
    by_id = {s.id: s for s in dag.subtasks}
    done: dict[str, SoloSubtask] = {}
    for level in dependency_levels(dag):
        for sid in level:
            sub = by_id[sid]
            started = time.monotonic()
            blocked = [d for d in sub.depends_on if done[d].status is not SubtaskStatus.COMPLETED]
            if blocked:
                done[sid] = SoloSubtask(
                    sid, sub.skill, SubtaskStatus.SKIPPED, None, "dependency_not_completed", 0.0
                )
                continue
            upstream: dict[SkillId, dict[str, Any]] = {}
            for d in sub.depends_on:
                out = done[d].output
                if out is not None:
                    upstream[done[d].skill] = out
            payload = build_input(sub.skill, dataset, upstream)
            out_model = SKILL_IO[sub.skill][1]
            task_id = f"{run_id}:{sid}:1"
            try:
                with anyio.fail_after(deadline_s):
                    result = await submit(
                        llm_for(task_id),
                        solo_messages(sub.skill, payload.model_dump(mode="json")),
                        out_model,
                        max_attempts=max_attempts,
                        max_tokens=max_tokens,
                        deadline_s=deadline_s,
                    )
                done[sid] = SoloSubtask(
                    sid,
                    sub.skill,
                    SubtaskStatus.COMPLETED,
                    result.model_dump(mode="json"),
                    None,
                    time.monotonic() - started,
                )
            except TimeoutError:
                done[sid] = SoloSubtask(
                    sid,
                    sub.skill,
                    SubtaskStatus.FAILED,
                    None,
                    "deadline",
                    time.monotonic() - started,
                )
            except Exception as exc:  # typed submit failure, breaker, transport: this subtask only
                done[sid] = SoloSubtask(
                    sid,
                    sub.skill,
                    SubtaskStatus.FAILED,
                    None,
                    type(exc).__name__,
                    time.monotonic() - started,
                )
    return [done[s.id] for s in dag.subtasks]
