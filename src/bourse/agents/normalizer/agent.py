"""Normalizer (plain a2a-sdk): units.normalize.

parse_header_unit (MCP) decides every header it recognizes. Only headers it returns null for
go to one LLM call (gemma via the meter), which may name a unit symbol; the symbol is then
resolved by the same MCP tool, so factors and offsets always come from the tool table.
"""

from pydantic import BaseModel, ConfigDict, Field

from bourse.agents.common.context import TaskContext
from bourse.agents.common.executor import SkillExecutor
from bourse.agents.common.llm_json import read_columns, samples, user_json
from bourse.contracts.skills_io import Conversion, UnitsIn, UnitsOut
from bourse.llm.base import ChatMessage
from bourse.llm.submit_tool import submit
from bourse.mcp_servers.units import UNITS


class HeaderUnits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    units: dict[str, str | None] = Field(default_factory=dict)


INSTRUCTION = (
    "For each column header below, give the unit symbol its values are measured in, or null "
    "if the column is not a physical quantity (identifiers, names, categories, dates). "
    "If you are not sure, use null. "
    f"Allowed symbols: {', '.join(sorted(UNITS))}. Call the submit tool once with units."
)


class NormalizerExecutor(SkillExecutor):
    async def run_skill(self, ctx: TaskContext, payload: BaseModel) -> BaseModel:
        assert isinstance(payload, UnitsIn)
        header, columns = read_columns(payload.dataset.csv_text)
        found: dict[str, Conversion] = {}
        unresolved: list[str] = []
        for h in header:
            res = await ctx.call_tool("units", "parse_header_unit", header=h)
            if res["conversion"] is None:
                unresolved.append(h)
            else:
                found[h] = Conversion.model_validate(res["conversion"])
        if unresolved:
            sample = samples(columns, 3)
            messages = [
                ChatMessage(role="system", content="Answer only by calling the submit tool."),
                user_json(INSTRUCTION, {h: sample[h] for h in unresolved}),
            ]
            answer = await submit(
                ctx.router(), messages, HeaderUnits, max_attempts=2, max_tokens=256
            )
            for h in unresolved:
                symbol = answer.units.get(h)
                if not symbol:
                    continue
                res = await ctx.call_tool("units", "parse_header_unit", header=f"x_{symbol}")
                if res["conversion"] is not None:
                    found[h] = Conversion.model_validate({**res["conversion"], "column": h})
        return UnitsOut(conversions=[found[h] for h in header if h in found])
