"""ADR-0047 timeout ordering: agent LLM >= meter < exam task <= exam wall."""

import pytest
from pydantic import ValidationError

from bourse.config import Settings


def test_default_timeout_ordering_holds() -> None:
    s = Settings()
    assert s.bourse_agent_llm_timeout_s == 180.0
    assert s.bourse_meter_deadline_s == 120.0
    assert s.bourse_exam_deadline_s == 300.0
    assert s.bourse_exam_timeout_s == 900.0
    assert (
        s.bourse_agent_llm_timeout_s
        >= s.bourse_meter_deadline_s
        < s.bourse_exam_deadline_s
        <= s.bourse_exam_timeout_s
    )


def test_timeout_ordering_rejects_meter_ge_exam_task() -> None:
    with pytest.raises(ValidationError, match="timeout ordering violated"):
        Settings(
            bourse_agent_llm_timeout_s=180.0,
            bourse_meter_deadline_s=300.0,
            bourse_exam_deadline_s=300.0,
            bourse_exam_timeout_s=900.0,
        )


def test_timeout_ordering_rejects_agent_below_meter() -> None:
    with pytest.raises(ValidationError, match="timeout ordering violated"):
        Settings(
            bourse_agent_llm_timeout_s=60.0,
            bourse_meter_deadline_s=120.0,
            bourse_exam_deadline_s=300.0,
            bourse_exam_timeout_s=900.0,
        )


def test_timeout_ordering_rejects_exam_task_above_exam_wall() -> None:
    with pytest.raises(ValidationError, match="timeout ordering violated"):
        Settings(
            bourse_agent_llm_timeout_s=180.0,
            bourse_meter_deadline_s=120.0,
            bourse_exam_deadline_s=1000.0,
            bourse_exam_timeout_s=900.0,
        )


def test_timeout_ordering_error_includes_values() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings(
            bourse_agent_llm_timeout_s=10.0,
            bourse_meter_deadline_s=20.0,
            bourse_exam_deadline_s=30.0,
            bourse_exam_timeout_s=40.0,
        )
    msg = str(exc.value)
    assert "agent=10" in msg or "agent=10.0" in msg
    assert "meter=20" in msg or "meter=20.0" in msg
