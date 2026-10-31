"""Deterministic part of pii.detect, shared by both PII agents.

EMAIL and PHONE candidates are findings; CARD only when luhn_check passes; SIN only when
sin_check passes. All via MCP pii tools. PERSON_NAME columns come from the agent's LLM step.
"""

from pydantic import BaseModel, ConfigDict, Field

from bourse.agents.common.context import TaskContext
from bourse.agents.common.llm_json import read_columns
from bourse.contracts.skills_io import PiiFinding


class NameColumns(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name_columns: list[str] = Field(default_factory=list, max_length=50)


NAME_INSTRUCTION = (
    "Which columns contain names of people (customers, users, employees)? Deterministic tools "
    "already handle emails, phones, card numbers and SINs; do not list those columns. "
    "Call the submit tool once with name_columns, an empty list if there are none."
)


async def regex_findings(
    ctx: TaskContext, csv_text: str
) -> tuple[list[PiiFinding], dict[str, list[str | None]], set[str]]:
    """Return (findings, columns, columns that had any regex candidate)."""
    header, columns = read_columns(csv_text)
    findings: list[PiiFinding] = []
    flagged: set[str] = set()
    for column in header:
        values = columns[column]
        scan = await ctx.call_tool("pii", "regex_scan", values=[v or "" for v in values])
        candidates = scan["candidates"]
        if candidates:
            flagged.add(column)
        cards = [c["index"] for c in candidates if c["kind"] == "CARD"]
        sins = [c["index"] for c in candidates if c["kind"] == "SIN"]
        valid: set[int] = set()
        if cards:
            res = await ctx.call_tool("pii", "luhn_check", numbers=[values[i] or "" for i in cards])
            valid |= {i for i, ok in zip(cards, res["results"], strict=True) if ok}
        if sins:
            res = await ctx.call_tool("pii", "sin_check", numbers=[values[i] or "" for i in sins])
            valid |= {i for i, ok in zip(sins, res["results"], strict=True) if ok}
        for c in candidates:
            if c["kind"] in ("EMAIL", "PHONE") or c["index"] in valid:
                findings.append(PiiFinding(row=c["index"], column=column, kind=c["kind"]))
    return findings, columns, flagged


def name_findings(
    name_columns: list[str], columns: dict[str, list[str | None]], flagged: set[str]
) -> list[PiiFinding]:
    out: list[PiiFinding] = []
    for column in name_columns:
        if column not in columns or column in flagged:
            continue
        out.extend(
            PiiFinding(row=i, column=column, kind="PERSON_NAME")
            for i, v in enumerate(columns[column])
            if v is not None
        )
    return out


def ordered(findings: list[PiiFinding], header: list[str]) -> list[PiiFinding]:
    pos = {h: i for i, h in enumerate(header)}
    return sorted(findings, key=lambda f: (f.row, pos.get(f.column, len(pos)), f.kind))
