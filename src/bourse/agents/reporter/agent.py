"""Reporter (LangGraph): report.compose with qwen via the meter.

The LLM drafts a summary and cites numbers; a deterministic validator checks every cited
(column, stat, value) against the input stats/profile (1e-6) and requires >= 3 citations.
On failure the errors are fed back; after MAX_ATTEMPTS drafts the task FAILS.
"""

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel

from bourse.agents.common.context import TaskContext
from bourse.agents.common.errors import TaskFailed
from bourse.agents.common.executor import SkillExecutor
from bourse.agents.common.llm_json import user_json
from bourse.contracts.skills_io import ReportIn, ReportOut
from bourse.grading.graders import REPORT_MIN_CITATIONS, REPORT_TOL, STAT_KEYS, close
from bourse.llm.base import ChatMessage
from bourse.llm.submit_tool import SubmitFailed, submit

MAX_ATTEMPTS = 3  # first draft + 2 retries

SYSTEM = (
    "You write a short factual report (at most 1200 characters) about a dataset using ONLY the "
    "numbers provided. Cite at least 3 numbers in `cited` as (column, stat, value), copying "
    "each value exactly from the input. stat is one of mean, std, min, max, p50, null_count, "
    "distinct_count. Answer only by calling the submit tool."
)


def allowed_values(inp: ReportIn) -> dict[tuple[str, str], float | None]:
    values: dict[tuple[str, str], float | None] = {}
    if inp.stats is not None:
        for s in inp.stats.stats:
            for key in STAT_KEYS:
                values[(s.column, key)] = getattr(s, key)
    if inp.profile is not None:
        for c in inp.profile.columns:
            values[(c.name, "null_count")] = float(c.null_count)
            values[(c.name, "distinct_count")] = float(c.distinct_count)
    return values


def citation_errors(report: ReportOut, allowed: dict[tuple[str, str], float | None]) -> list[str]:
    errors = [
        f"{c.column}.{c.stat} is not an input value"
        for c in report.cited
        if not close(c.value, allowed.get((c.column, c.stat)), REPORT_TOL)
    ]
    if len(report.cited) < REPORT_MIN_CITATIONS:
        errors.append(f"cite at least {REPORT_MIN_CITATIONS} numbers")
    return errors


class ReporterState(TypedDict, total=False):
    messages: list[ChatMessage]
    attempts: int
    report: ReportOut | None
    errors: list[str]


def build_graph(ctx: TaskContext, allowed: dict[tuple[str, str], float | None]) -> Any:
    async def draft(state: ReporterState) -> ReporterState:
        attempts = state.get("attempts", 0) + 1
        try:
            report = await submit(
                ctx.router(), state["messages"], ReportOut, max_attempts=1, max_tokens=900
            )
        except SubmitFailed as exc:
            return {"attempts": attempts, "report": None, "errors": [exc.last_error[:300]]}
        return {"attempts": attempts, "report": report, "errors": citation_errors(report, allowed)}

    def route(state: ReporterState) -> str:
        if state.get("report") is not None and not state["errors"]:
            return "done"
        return "retry" if state["attempts"] < MAX_ATTEMPTS else "give_up"

    async def retry(state: ReporterState) -> ReporterState:
        feedback = "Your report was rejected: " + "; ".join(state["errors"][:10])
        feedback += ". Call submit again, citing only values copied from the input."
        return {"messages": [*state["messages"], ChatMessage(role="user", content=feedback)]}

    graph = StateGraph(ReporterState)
    graph.add_node("draft", draft)
    graph.add_node("retry", retry)
    graph.add_edge(START, "draft")
    graph.add_conditional_edges("draft", route, {"done": END, "retry": "retry", "give_up": END})
    graph.add_edge("retry", "draft")
    return graph.compile()


class ReporterExecutor(SkillExecutor):
    async def run_skill(self, ctx: TaskContext, payload: BaseModel) -> BaseModel:
        assert isinstance(payload, ReportIn)
        allowed = allowed_values(payload)
        facts: dict[str, Any] = {
            "rows": payload.dataset.n_rows,
            "columns": payload.dataset.n_cols,
            "profile": payload.profile.model_dump() if payload.profile else None,
            "stats": payload.stats.model_dump() if payload.stats else None,
            "pii_findings_by_kind": _pii_counts(payload),
            "units": payload.units.model_dump() if payload.units else None,
        }
        messages = [ChatMessage(role="system", content=SYSTEM), user_json("Input:", facts)]
        final = await build_graph(ctx, allowed).ainvoke({"messages": messages, "attempts": 0})
        report = final.get("report")
        if report is None or final.get("errors"):
            raise TaskFailed(
                "invalid_llm_output", f"report rejected after {final.get('attempts')} drafts"
            )
        assert isinstance(report, ReportOut)
        return report


def _pii_counts(payload: ReportIn) -> dict[str, int]:
    counts: dict[str, int] = {}
    if payload.pii is not None:
        for f in payload.pii.findings:
            counts[f.kind] = counts.get(f.kind, 0) + 1
    return counts
