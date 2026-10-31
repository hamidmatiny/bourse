"""PII-Precise (CrewAI): deterministic regex/Luhn/SIN via MCP; a CrewAI crew (qwen via the
meter) picks PERSON_NAME columns from table.sample_rows output.

crewai 1.6.1 agents invoke tools through ReAct text (Action / Action Input), not native
function calling, so the submit tool is a CrewAI BaseTool whose arguments are validated by
the NameColumns model (ADR-0021). The crew runs in a worker thread under the task deadline.

crewai validates tool input against args_schema after adding its own `security_context`
argument, so the tool's schema ignores extra keys; `_run` drops `security_context` and the
captured arguments are validated against the strict NameColumns model afterwards.
"""

import json
from typing import Any

from anyio import to_thread
from pydantic import BaseModel, ConfigDict, ValidationError

from bourse.agents.common.context import TaskContext
from bourse.agents.common.errors import TaskFailed
from bourse.agents.common.executor import SkillExecutor
from bourse.agents.common.llm_json import read_columns
from bourse.agents.common.pii_pipeline import (
    NAME_INSTRUCTION,
    NameColumns,
    name_findings,
    ordered,
    regex_findings,
)
from bourse.agents.common.spec import METER_TASK_HEADER
from bourse.agents.pii_precise import isolation
from bourse.contracts.skills_io import PiiIn, PiiOut

MAX_KICKOFFS = 2
CREWAI_INJECTED_ARGS = frozenset({"security_context"})


def run_crew(
    *,
    model: str,
    base_url: str,
    api_key: str,
    task_id: str,
    max_tokens: int,
    timeout_s: float,
    client_params: dict[str, Any] | None,
    prompt: str,
) -> list[dict[str, Any]]:
    """Run one crew synchronously; return every argument set passed to `submit`."""
    isolation.apply()
    from crewai import LLM, Agent, Crew, Process, Task
    from crewai.tools import BaseTool

    submitted: list[dict[str, Any]] = []

    class SubmitArgs(BaseModel):
        model_config = ConfigDict(extra="ignore")

        name_columns: list[str]

    class SubmitTool(BaseTool):
        name: str = "submit"
        description: str = "Submit the final answer: name_columns, a list of column names."
        args_schema: type[BaseModel] = SubmitArgs

        def _run(self, *args: Any, **kwargs: Any) -> str:
            submitted.append({k: v for k, v in kwargs.items() if k not in CREWAI_INJECTED_ARGS})
            return "submitted"

    llm = LLM(
        model=model,
        provider="openai",
        base_url=f"{base_url.rstrip('/')}/v1",
        api_key=api_key,
        default_headers={METER_TASK_HEADER: task_id},
        max_retries=0,
        timeout=timeout_s,
        max_tokens=max_tokens,
        client_params=client_params,
    )
    analyst = Agent(
        role="PII column analyst",
        goal="Identify which dataset columns hold names of people.",
        backstory="Deterministic tools already found emails, phones, card numbers and SINs.",
        llm=llm,
        tools=[SubmitTool()],
        allow_delegation=False,
        max_iter=4,
        verbose=False,
    )
    task = Task(
        description=prompt + "\nAfter calling submit, reply with Final Answer: done.",
        expected_output="done",
        agent=analyst,
    )
    crew = Crew(agents=[analyst], tasks=[task], process=Process.sequential, verbose=False)
    try:
        crew.kickoff()
    except ValueError:
        # crewai raises ValueError on an empty LLM reply. A submit captured before that
        # still counts; without one this kickoff is a failed attempt.
        if not submitted:
            return []
    return submitted


class PiiPreciseExecutor(SkillExecutor):
    async def run_skill(self, ctx: TaskContext, payload: BaseModel) -> BaseModel:
        assert isinstance(payload, PiiIn)
        csv_text = payload.dataset.csv_text
        findings, columns, flagged = await regex_findings(ctx, csv_text)
        header, _ = read_columns(csv_text)
        sample = await ctx.call_tool("table", "sample_rows", csv_text=csv_text, n=5)
        rows = [
            {c: r[i] for i, c in enumerate(sample["columns"]) if c not in flagged}
            for r in sample["rows"]
        ]
        prompt = f"{NAME_INSTRUCTION}\n\nSample rows:\n{json.dumps(rows, sort_keys=True)}"
        names: NameColumns | None = None
        for _ in range(MAX_KICKOFFS):
            calls = await to_thread.run_sync(
                lambda: run_crew(
                    model=self.spec.model,
                    base_url=self.runtime.meter_url,
                    api_key=self.runtime.meter_key,
                    task_id=ctx.task_id,
                    max_tokens=self.spec.max_tokens_per_call,
                    timeout_s=ctx.deadline_s,
                    client_params=self.runtime.crewai_client_params,
                    prompt=prompt,
                ),
                abandon_on_cancel=True,
            )
            for args in calls:
                try:
                    names = NameColumns.model_validate(args)
                except ValidationError:
                    continue
            if names is not None:
                break
        if names is None:
            raise TaskFailed(
                "invalid_llm_output", f"no valid submit after {MAX_KICKOFFS} crew runs"
            )
        findings += name_findings(names.name_columns, columns, flagged)
        return PiiOut(findings=ordered(findings, header))
