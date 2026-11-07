"""Who is at fault when an exam task fails or times out (ADR-0049 amendment 2).

Before this amendment every task timeout was INFRA, so an agent that stalls (A8b slow-loris) or
burns tokens without a meter cap (A8a under no-metercap) was retried, then INCONCLUSIVE, and
never got a verdict. Attribution now comes from meter-side evidence for the task, not from the
agent's own report. The rules, in order (the first that applies decides):

  A1 agent  the meter refused this task with budget_exhausted / task_budget_exceeded.
  I1 infra  the meter recorded 429 (not a budget refusal) / 5xx / contention for this task, or
            the upstream was unreachable (connect error / breaker open).
  I2 infra  meter queue wait for this task > contention_queue_fraction x task deadline.
  -- the agent answered with a failed task (no timeout):
  I3 infra  the agent reported llm_unavailable / contention / HTTP 5xx (ADR-0049, kept: a false
            claim can only make the exam INCONCLUSIVE, never passed).
  A2 agent  any other failure.
  -- the task deadline passed:
  I4 infra  no meter status for the task (meter unreachable or not wired): health unproven.
  A3 agent  the task made more calls or used more tokens than its task budget (burning past
            the ceiling; under no-metercap nothing refuses it).
  I5 infra  an LLM call of this task was inside the meter (gate queue or upstream) at the
            deadline: the agent was waiting on the model.
  A4 agent  the meter was healthy and every LLM call of the task had completed: the agent
            stalled on its own (slow-loris).

Agent fault -> the task is ERROR (counts toward n/N, can make the exam EXAM_FAILED).
Infra -> INFRA (retried up to infra_max_retries, then the exam is EXAM_INCONCLUSIVE).
The verdict and its evidence string are stored on the exam task record.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from bourse.contracts.metering import TaskBudgetStatus

Fault = Literal["agent", "infra"]
REFUSAL_CODES = frozenset({"budget_exhausted", "task_budget_exceeded"})
BUDGET_KIND = "BUDGET"  # meter error_kind on a budget refusal row (status 429)
INFRA_KINDS = frozenset(
    {
        "contention",
        "rate_limit_unsatisfiable",
        "upstream_failed",
        "upstream_error",
        "breaker_open",
        "deadline",
    }
)
_CLAIM_MARKERS = ("llm_unavailable", "contention", " status 50", " 500", " 502", " 503", " 504")


@dataclass(frozen=True)
class MeterRow:
    status: int
    error_kind: str | None = None
    queue_wait_ms: float = 0.0


@dataclass(frozen=True)
class FaultEvidence:
    timed_out: bool  # True: task deadline passed; False: the agent returned a failed task
    meter: TaskBudgetStatus | None  # None: meter status not available
    rows: tuple[MeterRow, ...] = ()
    task_deadline_s: float = 0.0
    contention_queue_fraction: float = 0.5
    claim: str = ""  # the agent's own failure report (state / error_kind / error)


@dataclass(frozen=True)
class Attribution:
    fault: Fault
    rule: str
    evidence: str


def rows_from_usage(usage: Iterable[Any]) -> tuple[MeterRow, ...]:
    return tuple(
        MeterRow(
            status=int(getattr(u, "status", 0) or 0),
            error_kind=getattr(u, "error_kind", None),
            queue_wait_ms=float(getattr(u, "queue_wait_ms", 0) or 0),
        )
        for u in usage
    )


def _is_infra_row(r: MeterRow) -> bool:
    if r.error_kind == BUDGET_KIND:
        return False
    return r.status == 429 or r.status >= 500 or (r.error_kind or "") in INFRA_KINDS


def _meter_bits(ev: FaultEvidence) -> str:
    m = ev.meter
    ok = sum(r.status == 200 for r in ev.rows)
    qw = max((r.queue_wait_ms for r in ev.rows), default=0.0)
    if m is None:
        return f"meter=unavailable rows={len(ev.rows)} ok={ok} queue_wait_ms={qw:.0f}"
    return (
        f"calls={m.calls}/{m.max_calls if m.max_calls is not None else '-'} "
        f"tokens={m.tokens}/{m.max_tokens if m.max_tokens is not None else '-'} "
        f"in_flight={m.in_flight} rows={len(ev.rows)} ok={ok} queue_wait_ms={qw:.0f}"
    )


def attribute_exam_failure(ev: FaultEvidence) -> Attribution:
    """Rules A1..A4 / I1..I5 above. Pure and deterministic."""
    bits = _meter_bits(ev)
    refused = [c for c in (ev.meter.refusals if ev.meter else ()) if c in REFUSAL_CODES]
    if not refused and any(r.status == 429 and r.error_kind == BUDGET_KIND for r in ev.rows):
        refused = ["budget refusal row"]
    if refused:
        return Attribution("agent", "A1", f"meter refused {refused[0]}; {bits}")
    infra_rows = [r for r in ev.rows if _is_infra_row(r)]
    if infra_rows:
        r = infra_rows[0]
        return Attribution(
            "infra", "I1", f"meter {r.status} {r.error_kind or '-'} x{len(infra_rows)}; {bits}"
        )
    qw = max((r.queue_wait_ms for r in ev.rows), default=0.0)
    if qw > ev.task_deadline_s * ev.contention_queue_fraction * 1000.0:
        return Attribution("infra", "I2", f"queue wait over fraction; {bits}")
    if not ev.timed_out:
        claim = f" {ev.claim}".lower()
        if any(m in claim for m in _CLAIM_MARKERS):
            return Attribution("infra", "I3", f"agent reported infra ({ev.claim[:120]}); {bits}")
        return Attribution("agent", "A2", f"agent failed ({ev.claim[:120]}); {bits}")
    m = ev.meter
    if m is None:
        return Attribution("infra", "I4", f"deadline, meter health unproven; {bits}")
    over_calls = m.max_calls is not None and m.calls > m.max_calls
    over_tokens = m.max_tokens is not None and m.tokens > m.max_tokens
    if over_calls or over_tokens:
        return Attribution("agent", "A3", f"deadline, calls beyond task budget; {bits}")
    if m.in_flight > 0:
        return Attribution("infra", "I5", f"deadline, LLM call in flight; {bits}")
    return Attribution("agent", "A4", f"deadline, meter healthy, no LLM call in flight; {bits}")
