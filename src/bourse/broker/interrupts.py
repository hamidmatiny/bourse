"""INPUT_REQUIRED policy (ADR-0040).

Accept TASK_STATE_INPUT_REQUIRED only when the status message is exactly one DataPart matching
ConfirmColumns or ConfirmUnits, the fields sit on the closed tables (dataset header / unit
table), and this is the first input round for the task. Anything else -> SESSION_POLICY and a
SOFT SESSION_SMUGGLING violation. Agent-produced text is never placed in any LLM prompt.
"""

import csv
import io
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from bourse.contracts.interrupts import INPUT_ADAPTER, ConfirmColumns, ConfirmUnits
from bourse.mcp_servers.units import ALIASES, UNITS

SESSION_SMUGGLING = "SESSION_SMUGGLING"
ALLOWED_UNITS: frozenset[str] = frozenset(UNITS) | frozenset(ALIASES)


@dataclass(frozen=True)
class InterruptDecision:
    accepted: bool
    request: ConfirmColumns | ConfirmUnits | None = None
    detail: str = ""


def dataset_header(payload: BaseModel) -> list[str]:
    dataset = getattr(payload, "dataset", None)
    csv_text = getattr(dataset, "csv_text", None)
    if not isinstance(csv_text, str) or not csv_text:
        return []
    rows = list(csv.reader(io.StringIO(csv_text)))
    return rows[0] if rows else []


def evaluate_interrupt(
    *,
    data_parts: list[dict[str, Any]],
    text_parts: list[str],
    extra_parts: int,
    payload: BaseModel,
    round_index: int,
) -> InterruptDecision:
    if round_index >= 1:
        return InterruptDecision(False, detail="second input round")
    if extra_parts or text_parts:
        return InterruptDecision(False, detail="free text or extra parts")
    if len(data_parts) != 1:
        return InterruptDecision(False, detail="expected exactly one DataPart")
    try:
        parsed = INPUT_ADAPTER.validate_python(data_parts[0])
    except ValidationError as exc:
        return InterruptDecision(False, detail=f"unknown schema: {exc.error_count()} error(s)")
    header = dataset_header(payload)
    if isinstance(parsed, ConfirmColumns):
        unknown = [c for c in parsed.columns if c not in header]
        if unknown:
            return InterruptDecision(False, detail=f"columns not in header: {unknown[:5]}")
        return InterruptDecision(True, parsed)
    if parsed.column not in header:
        return InterruptDecision(False, detail=f"column {parsed.column!r} not in header")
    bad = [u for u in parsed.candidates if u not in ALLOWED_UNITS]
    if bad:
        return InterruptDecision(False, detail=f"candidates not in unit table: {bad[:5]}")
    if not parsed.candidates:
        return InterruptDecision(False, detail="candidates empty")
    return InterruptDecision(True, parsed)
