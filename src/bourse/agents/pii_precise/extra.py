"""pii-precise 1.1.0 extra detection (ADR-0052): the LLM proposes, deterministic checks decide.

After the deterministic step (regex + Luhn + SIN) and the name crew, two submit-tool calls run
through the meter under the same task budget:

1. column kinds: header + up to 5 samples of every column the regex did not flag and that is
   not a name column -> EMAIL | PHONE | FREE_TEXT | NONE per column. EMAIL / PHONE columns
   propose every non-empty cell.
2. free text: for each FREE_TEXT column, all values with their row index -> the rows whose
   text contains a phone number or an email address, with the kind.

Every proposed (row, column, kind) goes through the MCP `contact_check` tool; only accepted
pairs are added. The LLM can only add findings: deterministic findings are never removed.
The steps are best-effort: if a call fails (invalid output, task budget, provider), the step
adds nothing and the deterministic + name findings are returned. BreakerOpen propagates.
"""

import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from bourse.agents.common.context import TaskContext
from bourse.agents.common.llm_json import samples, user_json
from bourse.contracts.skills_io import PiiFinding
from bourse.llm.base import ChatMessage
from bourse.llm.router import AllProvidersFailed
from bourse.llm.submit_tool import SubmitFailed, submit

log = logging.getLogger(__name__)

ColumnKind = Literal["EMAIL", "PHONE", "FREE_TEXT", "NONE"]
TextKind = Literal["EMAIL", "PHONE"]
KIND_MAX_TOKENS = 512
TEXT_MAX_TOKENS = 768
MAX_FREE_TEXT_COLUMNS = 3
MAX_FREE_TEXT_ROWS = 200

KIND_INSTRUCTION = (
    "For each column, say what its values contain. EMAIL: email addresses, including written "
    "out forms such as 'name [at] host [dot] com'. PHONE: phone numbers, including "
    "international numbers with a + country code. FREE_TEXT: notes or comments that may "
    "contain a phone number or email inside other words. NONE: anything else (codes, colors, "
    "amounts, ids). Call the submit tool once with one entry per column."
)
TEXT_INSTRUCTION = (
    "Each item is a row index and the text of one cell. List the rows whose text contains a "
    "phone number or an email address, with kind PHONE or EMAIL. Call the submit tool once; "
    "an empty list if none."
)


class ColumnLabel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    column: str
    kind: ColumnKind


class ColumnKinds(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    columns: list[ColumnLabel] = Field(default_factory=list, max_length=100)


class TextHit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    row: int = Field(ge=0)
    kind: TextKind


class TextHits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    hits: list[TextHit] = Field(default_factory=list, max_length=MAX_FREE_TEXT_ROWS)


SYSTEM = ChatMessage(role="system", content="Answer only by calling the submit tool.")


async def _kinds(ctx: TaskContext, cand: dict[str, list[str]]) -> dict[str, ColumnKind]:
    messages = [SYSTEM, user_json(KIND_INSTRUCTION, {"columns": cand})]
    out = await submit(
        ctx.router(), messages, ColumnKinds, max_attempts=2, max_tokens=KIND_MAX_TOKENS
    )
    return {c.column: c.kind for c in out.columns if c.column in cand}


async def _text_hits(ctx: TaskContext, values: list[str | None]) -> list[TextHit]:
    items = [{"row": i, "text": v} for i, v in enumerate(values) if v is not None]
    if not items:
        return []
    messages = [SYSTEM, user_json(TEXT_INSTRUCTION, {"cells": items[:MAX_FREE_TEXT_ROWS]})]
    out = await submit(ctx.router(), messages, TextHits, max_attempts=2, max_tokens=TEXT_MAX_TOKENS)
    return [h for h in out.hits if h.row < len(values) and values[h.row] is not None]


async def extra_findings(
    ctx: TaskContext,
    columns: dict[str, list[str | None]],
    skip: set[str],
) -> list[PiiFinding]:
    """Findings proposed by the LLM and accepted by `contact_check`, in columns not in `skip`
    (regex-flagged and name columns)."""
    cand = {h: v for h, v in samples(columns, 5).items() if h not in skip and v}
    if not cand:
        return []
    proposed: list[tuple[int, str, TextKind]] = []
    try:
        kinds = await _kinds(ctx, cand)
    except (SubmitFailed, AllProvidersFailed) as exc:
        log.warning("pii_extra step=column_kinds task=%s failed: %s", ctx.task_id, exc)
        return []
    for column, kind in kinds.items():
        if kind in ("EMAIL", "PHONE"):
            proposed += [(i, column, kind) for i, v in enumerate(columns[column]) if v is not None]
    free_text = [c for c, k in kinds.items() if k == "FREE_TEXT"][:MAX_FREE_TEXT_COLUMNS]
    for column in free_text:
        try:
            hits = await _text_hits(ctx, columns[column])
        except (SubmitFailed, AllProvidersFailed) as exc:
            log.warning("pii_extra step=free_text task=%s column=%s failed: %s",
                        ctx.task_id, column, exc)  # fmt: skip
            continue
        proposed += [(h.row, column, h.kind) for h in hits]
    proposed = sorted(set(proposed))
    if not proposed:
        return []
    res = await ctx.call_tool(
        "pii",
        "contact_check",
        values=[columns[c][r] or "" for r, c, _ in proposed],
        kinds=[k for _, _, k in proposed],
    )
    return [
        PiiFinding(row=r, column=c, kind=k)
        for (r, c, k), ok in zip(proposed, res["results"], strict=True)
        if ok
    ]
