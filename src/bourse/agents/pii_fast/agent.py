"""PII-Fast (plain a2a-sdk): deterministic regex/Luhn/SIN via MCP, one short LLM call
(gemma via the meter) for PERSON_NAME columns only."""

from pydantic import BaseModel

from bourse.agents.common.context import TaskContext
from bourse.agents.common.executor import SkillExecutor
from bourse.agents.common.llm_json import read_columns, samples, user_json
from bourse.agents.common.pii_pipeline import (
    NAME_INSTRUCTION,
    NameColumns,
    name_findings,
    ordered,
    regex_findings,
)
from bourse.contracts.skills_io import PiiIn, PiiOut
from bourse.llm.base import ChatMessage
from bourse.llm.submit_tool import submit


class PiiFastExecutor(SkillExecutor):
    async def run_skill(self, ctx: TaskContext, payload: BaseModel) -> BaseModel:
        assert isinstance(payload, PiiIn)
        csv_text = payload.dataset.csv_text
        findings, columns, flagged = await regex_findings(ctx, csv_text)
        header, _ = read_columns(csv_text)
        candidates = {h: v for h, v in samples(columns, 3).items() if h not in flagged}
        messages = [
            ChatMessage(role="system", content="Answer only by calling the submit tool."),
            user_json(NAME_INSTRUCTION, {"columns": candidates}),
        ]
        names = await submit(ctx.router(), messages, NameColumns, max_attempts=2, max_tokens=256)
        findings += name_findings(names.name_columns, columns, flagged)
        return PiiOut(findings=ordered(findings, header))
