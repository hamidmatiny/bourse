"""Profiler (LangGraph): schema.profile, stats.compute.

Numbers come only from the table tools; the assembler copies them from tool JSON. The LLM
(gemma via the meter) only decides dtypes for ambiguous columns and which numeric columns to
report stats for.
"""

import re
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field

from bourse.agents.common.context import TaskContext
from bourse.agents.common.executor import SkillExecutor
from bourse.agents.common.llm_json import user_json
from bourse.contracts.skills import SkillId
from bourse.contracts.skills_io import (
    ColumnStats,
    Dtype,
    SchemaProfileIn,
    SchemaProfileOut,
    StatsIn,
    StatsOut,
)
from bourse.llm.base import ChatMessage
from bourse.llm.submit_tool import submit

NUMERIC = ("int", "float")
# A string column is ambiguous when at least half of its sampled values look numeric once
# currency, thousands separators and percent signs are stripped.
_NUMBERISH = re.compile(r"^\s*[$€£]?\s*-?[\d,]+(?:\.\d+)?\s*%?\s*$")
AMBIGUOUS_SHARE = 0.5


class ProfileReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dtype_overrides: dict[str, Dtype] = Field(default_factory=dict)
    numeric_columns: list[str] = Field(default_factory=list)
    comment: str = Field(default="", max_length=500)


class ProfilerState(TypedDict, total=False):
    csv_text: str
    profile: dict[str, Any]
    samples: dict[str, list[str | None]]
    stats: dict[str, Any]
    ambiguous: list[str]
    review: ProfileReview
    output: BaseModel


SYSTEM = (
    "You review a column profile computed by deterministic tools. You never change numbers. "
    "Answer only by calling the submit tool. dtype_overrides: only for columns listed as "
    "ambiguous, choose one of int, float, string, date, bool. numeric_columns: every column "
    "whose dtype is int or float, including identifier columns."
)


def ambiguous_columns(profile: dict[str, Any], sample: dict[str, list[str | None]]) -> list[str]:
    out: list[str] = []
    for col in profile["columns"]:
        if col["dtype"] != "string":
            continue
        values = [v for v in sample.get(col["name"], []) if v is not None]
        if (
            values
            and sum(bool(_NUMBERISH.match(v)) for v in values) / len(values) >= AMBIGUOUS_SHARE
        ):
            out.append(col["name"])
    return out


def build_graph(ctx: TaskContext) -> Any:
    async def profile(state: ProfilerState) -> ProfilerState:
        prof = await ctx.call_tool("table", "profile_columns", csv_text=state["csv_text"])
        sample = await ctx.call_tool("table", "sample_rows", csv_text=state["csv_text"], n=20)
        columns = sample["columns"]
        by_col: dict[str, list[str | None]] = {
            c: [r[i] for r in sample["rows"]] for i, c in enumerate(columns)
        }
        return {"profile": prof, "samples": by_col, "ambiguous": ambiguous_columns(prof, by_col)}

    async def stats(state: ProfilerState) -> ProfilerState:
        numeric = [c["name"] for c in state["profile"]["columns"] if c["dtype"] in NUMERIC]
        if not numeric:
            return {"stats": {"stats": []}}
        out = await ctx.call_tool(
            "table", "column_stats", csv_text=state["csv_text"], columns=numeric
        )
        return {"stats": out}

    async def review(state: ProfilerState) -> ProfilerState:
        columns = [
            {k: c[k] for k in ("name", "dtype", "null_count", "distinct_count")}
            for c in state["profile"]["columns"]
        ]
        ambiguous = {
            a: [v for v in state["samples"].get(a, []) if v is not None][:5]
            for a in state["ambiguous"]
        }
        messages = [
            ChatMessage(role="system", content=SYSTEM),
            user_json(
                "Profile and ambiguous columns with sample values:",
                {"columns": columns, "ambiguous": ambiguous},
            ),
        ]
        result = await submit(ctx.router(), messages, ProfileReview, max_attempts=2, max_tokens=512)
        return {"review": result}

    async def assemble(state: ProfilerState) -> ProfilerState:
        rev = state["review"]
        if ctx.skill is SkillId.SCHEMA_PROFILE:
            cols = []
            for c in state["profile"]["columns"]:
                dtype = c["dtype"]
                if c["name"] in state["ambiguous"] and c["name"] in rev.dtype_overrides:
                    dtype = rev.dtype_overrides[c["name"]]
                cols.append({**c, "dtype": dtype})
            return {"output": SchemaProfileOut.model_validate({"columns": cols})}
        chosen = set(rev.numeric_columns)
        tool_stats = [ColumnStats.model_validate(s) for s in state["stats"]["stats"]]
        # An empty list means "no opinion": report every tool-numeric column.
        kept = [s for s in tool_stats if not chosen or s.column in chosen]
        return {"output": StatsOut(stats=kept)}

    graph = StateGraph(ProfilerState)
    graph.add_node("profile", profile)
    graph.add_node("stats", stats)
    graph.add_node("review", review)
    graph.add_node("assemble", assemble)
    graph.add_edge(START, "profile")
    if ctx.skill is SkillId.STATS_COMPUTE:
        graph.add_edge("profile", "stats")
        graph.add_edge("stats", "review")
    else:
        graph.add_edge("profile", "review")
    graph.add_edge("review", "assemble")
    graph.add_edge("assemble", END)
    return graph.compile()


class ProfilerExecutor(SkillExecutor):
    async def run_skill(self, ctx: TaskContext, payload: BaseModel) -> BaseModel:
        assert isinstance(payload, SchemaProfileIn | StatsIn)
        final = await build_graph(ctx).ainvoke({"csv_text": payload.dataset.csv_text})
        output: BaseModel = final["output"]
        return output
