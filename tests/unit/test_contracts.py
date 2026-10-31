from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from bourse.contracts import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalTrigger,
    BudgetSpec,
    IntakeRequest,
    LedgerEntry,
    ProbeReport,
    Severity,
    SkillId,
    Subtask,
    TaskDAG,
    Usage,
    Violation,
)
from bourse.contracts.skills_io import DatasetRef

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
SPEC = BudgetSpec(max_tokens_per_call=512, max_tokens_run=20_000, wall_budget_s=300.0)
USAGE = Usage(tokens_in=10, tokens_out=5, wall_ms=12.5, model="m", provider="ollama")

INSTANCES: list[BaseModel] = [
    Violation(kind="card_sig", severity=Severity.HARD, agent_id="a1", evidence="bad sig", at=NOW),
    Subtask(id="s1", skill=SkillId.PII_DETECT, depends_on=(), input_ref="data://x"),
    TaskDAG(
        job_id="j1",
        subtasks=(
            Subtask(id="a", skill=SkillId.SCHEMA_PROFILE, input_ref="r"),
            Subtask(id="b", skill=SkillId.STATS_COMPUTE, depends_on=("a",), input_ref="r"),
        ),
    ),
    IntakeRequest(
        dataset=DatasetRef.from_csv("a,b\n1,2\n"),
        request_text="audit this dataset",
        budget=SPEC,
    ),
    USAGE,
    LedgerEntry(
        run_id="run", seq=0, usage=USAGE, usd=Decimal("0.0012"), tokens_run_total=15, at=NOW
    ),
    SPEC,
    BudgetSpec(
        max_tokens_per_call=1,
        max_tokens_run=1,
        wall_budget_s=1.0,
        usd_cap=Decimal("2.50"),
        allow_paid=True,
    ),
    ApprovalRequest(
        request_id="q1",
        trigger=ApprovalTrigger.PAID_CALL,
        subject_id="paid",
        summary="one paid call",
        created_at=NOW,
    ),
    ApprovalDecision(approved=False, by="cli", at=NOW, reason="timeout"),
    ProbeReport(
        ollama_version="0.40.6",
        models=("gemma4:12b",),
        format_json_status=501,
        tool_call_ok=True,
        think_false_ok=True,
        resident_bytes=10,
        resident_ok=True,
        latency_ms=123.0,
    ),
]


@pytest.mark.parametrize("instance", INSTANCES, ids=lambda m: type(m).__name__)
def test_json_round_trip(instance: BaseModel) -> None:
    restored = type(instance).model_validate_json(instance.model_dump_json())
    assert restored == instance


@pytest.mark.parametrize("instance", INSTANCES, ids=lambda m: type(m).__name__)
def test_extra_fields_rejected(instance: BaseModel) -> None:
    data: dict[str, Any] = instance.model_dump(mode="json")
    data["unexpected"] = 1
    with pytest.raises(ValidationError, match="unexpected"):
        type(instance).model_validate(data)


@pytest.mark.parametrize("instance", INSTANCES, ids=lambda m: type(m).__name__)
def test_models_are_frozen(instance: BaseModel) -> None:
    field = next(iter(type(instance).model_fields))
    with pytest.raises(ValidationError):
        setattr(instance, field, None)


def test_taskdag_rejects_cycle() -> None:
    with pytest.raises(ValidationError, match="cycle"):
        TaskDAG(
            job_id="j",
            subtasks=(
                Subtask(id="a", skill=SkillId.SCHEMA_PROFILE, depends_on=("c",), input_ref="r"),
                Subtask(id="b", skill=SkillId.STATS_COMPUTE, depends_on=("a",), input_ref="r"),
                Subtask(id="c", skill=SkillId.REPORT_COMPOSE, depends_on=("b",), input_ref="r"),
            ),
        )


def test_taskdag_rejects_self_dependency() -> None:
    with pytest.raises(ValidationError, match="cycle"):
        TaskDAG(
            job_id="j",
            subtasks=(
                Subtask(id="a", skill=SkillId.SCHEMA_PROFILE, depends_on=("a",), input_ref="r"),
            ),
        )


def test_taskdag_rejects_duplicate_ids() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        TaskDAG(
            job_id="j",
            subtasks=(
                Subtask(id="a", skill=SkillId.SCHEMA_PROFILE, input_ref="r"),
                Subtask(id="a", skill=SkillId.STATS_COMPUTE, input_ref="r"),
            ),
        )


def test_taskdag_rejects_missing_dependency() -> None:
    with pytest.raises(ValidationError, match="unknown ids"):
        TaskDAG(
            job_id="j",
            subtasks=(
                Subtask(id="a", skill=SkillId.SCHEMA_PROFILE, depends_on=("zzz",), input_ref="r"),
            ),
        )


def test_skill_id_rejects_unknown_skill() -> None:
    with pytest.raises(ValueError):
        SkillId("web.browse")
    with pytest.raises(ValidationError):
        Subtask.model_validate({"id": "s", "skill": "web.browse", "input_ref": "r"})


def test_skill_taxonomy_is_closed_at_five() -> None:
    assert {s.value for s in SkillId} == {
        "schema.profile",
        "stats.compute",
        "pii.detect",
        "units.normalize",
        "report.compose",
    }


def test_paid_enabled_requires_flag_and_cap() -> None:
    assert not SPEC.paid_enabled
    assert not SPEC.model_copy(update={"allow_paid": True}).paid_enabled
    assert SPEC.model_copy(update={"allow_paid": True, "usd_cap": Decimal("1")}).paid_enabled
