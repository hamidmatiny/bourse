"""ADR-0049 amendment 2: one attribution function for failed/timed-out exam tasks.

One test per rule (A1..A4 agent, I1..I5 infra), plus the order between rules.
"""

import pytest

from bourse.contracts.metering import TaskBudgetStatus
from bourse.trust.exam_fault import FaultEvidence, MeterRow, attribute_exam_failure


def _m(**kw: object) -> TaskBudgetStatus:
    base: dict[str, object] = {
        "task_id": "t",
        "registered": True,
        "max_tokens": 8192,
        "max_calls": 20,
    }
    base.update(kw)
    return TaskBudgetStatus.model_validate(base)


OK = MeterRow(status=200)


def _att(**kw: object) -> tuple[str, str]:
    a = attribute_exam_failure(FaultEvidence(**kw))  # type: ignore[arg-type]
    return a.fault, a.rule


@pytest.mark.parametrize("code", ["budget_exhausted", "task_budget_exceeded"])
def test_a1_meter_refusal_is_agent_fault(code: str) -> None:
    assert _att(timed_out=False, meter=_m(refusals=(code,)), rows=(OK,)) == ("agent", "A1")
    assert _att(timed_out=True, meter=_m(refusals=(code,), in_flight=1)) == ("agent", "A1")


def test_a1_budget_refusal_row_without_meter_status() -> None:
    row = MeterRow(status=429, error_kind="BUDGET")
    assert _att(timed_out=False, meter=None, rows=(row,), claim="token_budget") == ("agent", "A1")


@pytest.mark.parametrize(
    "row",
    [
        MeterRow(status=429, error_kind="contention"),
        MeterRow(status=429, error_kind="rate_limit_unsatisfiable"),
        MeterRow(status=502, error_kind="upstream_failed"),
        MeterRow(status=503, error_kind="breaker_open"),
        MeterRow(status=504, error_kind="deadline"),
    ],
)
def test_i1_meter_429_5xx_contention_is_infra(row: MeterRow) -> None:
    assert _att(timed_out=True, meter=_m(), rows=(OK, row)) == ("infra", "I1")
    assert _att(timed_out=False, meter=_m(), rows=(row,), claim="internal") == ("infra", "I1")


def test_i2_queue_wait_over_fraction_is_infra() -> None:
    rows = (MeterRow(status=200, queue_wait_ms=60_000),)
    assert _att(timed_out=True, meter=_m(), rows=rows, task_deadline_s=100.0) == ("infra", "I2")
    low = (MeterRow(status=200, queue_wait_ms=1_000),)
    assert _att(timed_out=True, meter=_m(calls=1), rows=low, task_deadline_s=100.0) == (
        "agent",
        "A4",
    )


@pytest.mark.parametrize("claim", ["llm_unavailable AllProvidersFailed", "contention", "HTTP 503"])
def test_i3_agent_reported_infra_on_failed_task(claim: str) -> None:
    assert _att(timed_out=False, meter=_m(), claim=claim) == ("infra", "I3")


def test_a2_other_failed_task_is_agent_fault() -> None:
    assert _att(timed_out=False, meter=_m(), claim="internal scripted failure") == ("agent", "A2")
    assert _att(timed_out=False, meter=None, claim="deadline 300s") == ("agent", "A2")


def test_i4_deadline_without_meter_status_is_infra() -> None:
    assert _att(timed_out=True, meter=None) == ("infra", "I4")


def test_a3_deadline_with_calls_beyond_task_budget_is_agent_fault() -> None:
    # no-metercap: nothing refuses the burner; a call is in flight at the deadline.
    assert _att(timed_out=True, meter=_m(calls=21, in_flight=1)) == ("agent", "A3")
    assert _att(timed_out=True, meter=_m(calls=3, tokens=9000, in_flight=1)) == ("agent", "A3")


def test_i5_deadline_with_llm_call_in_flight_is_infra() -> None:
    assert _att(timed_out=True, meter=_m(calls=2, tokens=900, in_flight=1)) == ("infra", "I5")


def test_a4_deadline_healthy_meter_no_call_in_flight_is_agent_fault() -> None:
    # slow-loris: no LLM call at all, meter healthy.
    assert _att(timed_out=True, meter=_m()) == ("agent", "A4")
    assert _att(timed_out=True, meter=_m(calls=2), rows=(OK, OK)) == ("agent", "A4")


def test_infra_rows_win_over_deadline_rules_but_refusal_wins_over_all() -> None:
    bad = MeterRow(status=502, error_kind="upstream_failed")
    assert _att(timed_out=True, meter=_m(calls=30), rows=(bad,)) == ("infra", "I1")
    both = _m(calls=30, refusals=("task_budget_exceeded",))
    assert _att(timed_out=True, meter=both, rows=(bad,)) == ("agent", "A1")


def test_evidence_string_carries_the_meter_numbers() -> None:
    a = attribute_exam_failure(
        FaultEvidence(timed_out=True, meter=_m(calls=25, tokens=51200, in_flight=1))
    )
    assert a.evidence.startswith("deadline, calls beyond task budget;")
    assert "calls=25/20" in a.evidence and "tokens=51200/8192" in a.evidence
    assert "in_flight=1" in a.evidence
