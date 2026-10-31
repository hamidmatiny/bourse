"""A2A executor shared by all agents (ADR-0022).

Request: exactly one DataPart holding the skill's SKILL_IO input JSON, and message metadata
{skill_id, task_id, deadline_s}. Unacceptable requests -> REJECTED (no work). Otherwise
WORKING, then one Artifact with a DataPart holding the SKILL_IO output JSON, then COMPLETED.
Errors after work starts -> FAILED with an error kind. Status texts name fields and error
kinds only; input values are never echoed.
"""

import abc
from typing import Any

import httpx
from a2a.helpers.proto_helpers import get_data_parts, new_data_part, new_task_from_user_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Message, Part
from google.protobuf.json_format import MessageToDict
from opentelemetry import trace
from pydantic import BaseModel, ValidationError

from bourse.agents.common.context import TaskContext
from bourse.agents.common.errors import TaskFailed, TaskRejected
from bourse.agents.common.spec import MAX_DEADLINE_S, AgentRuntime, AgentSpec
from bourse.contracts.skills import SkillId
from bourse.contracts.skills_io import SKILL_IO
from bourse.llm.router import AllProvidersFailed
from bourse.llm.submit_tool import SubmitFailed
from bourse.mcp_adapter.adapter import ToolBudgetExceeded, ToolCallFailed, ToolResultInvalid
from bourse.runtime.breaker import BreakerOpen, ProviderHTTPError
from bourse.runtime.budget import BudgetExceeded
from bourse.runtime.deadline import DeadlineExceeded, deadline


def _validation_detail(exc: ValidationError) -> str:
    fields = [".".join(str(p) for p in e["loc"]) + ":" + e["type"] for e in exc.errors()[:10]]
    return "; ".join(fields)


def failure_kind(exc: BaseException) -> str:
    if isinstance(exc, TaskFailed):
        return exc.kind
    if isinstance(exc, DeadlineExceeded):
        return "deadline"
    if isinstance(exc, ToolBudgetExceeded):
        return "tool_budget"
    if isinstance(exc, ToolCallFailed | ToolResultInvalid):
        return "tool_error"
    if isinstance(exc, SubmitFailed):
        return "invalid_llm_output"
    if isinstance(exc, BreakerOpen | AllProvidersFailed | ProviderHTTPError | httpx.HTTPError):
        return "llm_unavailable"
    if isinstance(exc, BudgetExceeded):
        return "token_budget"
    return "internal"


class ParsedRequest(BaseModel):
    skill: SkillId
    task_id: str
    deadline_s: float
    payload: Any


class SkillExecutor(AgentExecutor, abc.ABC):
    def __init__(self, spec: AgentSpec, runtime: AgentRuntime) -> None:
        self.spec = spec
        self.runtime = runtime

    @abc.abstractmethod
    async def run_skill(self, ctx: TaskContext, payload: BaseModel) -> BaseModel:
        """Return the SKILL_IO output model for ctx.skill."""

    def parse(self, message: Message | None) -> ParsedRequest:
        if message is None:
            raise TaskRejected("no_message")
        meta: dict[str, Any] = (
            MessageToDict(message.metadata) if message.HasField("metadata") else {}
        )
        raw_skill = meta.get("skill_id")
        if raw_skill not in {s.value for s in self.spec.skills}:
            raise TaskRejected("unsupported_skill", "skill_id not offered by this agent")
        skill = SkillId(raw_skill)
        task_id = meta.get("task_id")
        if not isinstance(task_id, str) or not task_id or len(task_id) > 200:
            raise TaskRejected("missing_task_id", "metadata.task_id must be a non-empty string")
        raw_deadline = meta.get("deadline_s", self.spec.default_deadline_s)
        if not isinstance(raw_deadline, int | float) or not 0 < raw_deadline <= MAX_DEADLINE_S:
            raise TaskRejected("bad_deadline", f"deadline_s must be in (0, {MAX_DEADLINE_S}]")
        data = get_data_parts(list(message.parts))
        if len(data) != 1 or not isinstance(data[0], dict):
            raise TaskRejected("bad_parts", "exactly one object DataPart is required")
        input_model = SKILL_IO[skill][0]
        try:
            payload = input_model.model_validate(data[0])
        except ValidationError as exc:
            raise TaskRejected("invalid_input", _validation_detail(exc)) from exc
        return ParsedRequest(
            skill=skill, task_id=task_id, deadline_s=float(raw_deadline), payload=payload
        )

    @staticmethod
    def _status(updater: TaskUpdater, kind: str, detail: str) -> Message:
        text = f"{kind}: {detail}" if detail else kind
        return updater.new_agent_message([Part(text=text)], metadata={"error_kind": kind})

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        message = context.message
        task = context.current_task
        if task is None:
            if message is None:
                return
            task = new_task_from_user_message(message)
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        try:
            req = self.parse(message)
        except TaskRejected as rej:
            await updater.reject(self._status(updater, rej.kind, rej.detail))
            return

        span = trace.get_current_span()
        span.set_attribute("skill", req.skill.value)
        span.set_attribute("task_id", req.task_id)
        span.set_attribute("agent_id", self.spec.name)
        await updater.start_work()
        ctx = TaskContext(
            spec=self.spec,
            runtime=self.runtime,
            skill=req.skill,
            task_id=req.task_id,
            deadline_s=req.deadline_s,
        )
        output: BaseModel | None = None
        try:
            async with deadline(req.deadline_s):
                output = await self.run_skill(ctx, req.payload)
            expected = SKILL_IO[req.skill][1]
            if not isinstance(output, expected):
                raise TaskFailed("internal", "output model does not match skill")
        except Exception as exc:  # every failure becomes a typed FAILED status
            kind = failure_kind(exc)
            detail = exc.detail if isinstance(exc, TaskFailed) else type(exc).__name__
            await updater.failed(self._status(updater, kind, detail))
            return
        await updater.add_artifact(
            [new_data_part(output.model_dump(mode="json"))],
            name=f"{req.skill.value}.output",
            metadata={"skill_id": req.skill.value, "task_id": req.task_id},
        )
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        if task is not None:
            await TaskUpdater(event_queue, task.id, task.context_id).cancel()
