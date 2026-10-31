import copy
from typing import Any

import pytest

from bourse.contracts.skills import SkillId
from bourse.contracts.skills_io import (
    CitedNumber,
    PiiFinding,
    PiiOut,
    ReportOut,
    SchemaProfileOut,
    StatsOut,
    UnitsOut,
)
from bourse.grading.graders import PII_F1_THRESHOLD, grade
from tests.datasets import truth

PEOPLE = truth("people")
MEAS = truth("measurements")


def test_profile_perfect_partial_empty_extra() -> None:
    perfect = SchemaProfileOut.model_validate(PEOPLE["profile"])
    g = grade(SkillId.SCHEMA_PROFILE, perfect, PEOPLE)
    assert (g.passed, g.n, g.N, g.score) == (True, 9, 9, 1.0)

    data: dict[str, Any] = copy.deepcopy(PEOPLE["profile"])
    data["columns"][0]["dtype"] = "string"
    data["columns"][1]["null_count"] += 1
    g = grade(SkillId.SCHEMA_PROFILE, data, PEOPLE)
    assert (g.passed, g.n, g.N) == (False, 7, 9)

    g = grade(SkillId.SCHEMA_PROFILE, SchemaProfileOut(columns=[]), PEOPLE)
    assert (g.passed, g.n, g.score) == (False, 0, 0.0)

    extra = copy.deepcopy(PEOPLE["profile"])
    extra["columns"].append({"name": "ghost", "dtype": "int", "null_count": 0, "distinct_count": 1})
    g = grade(SkillId.SCHEMA_PROFILE, extra, PEOPLE)
    assert (g.passed, g.n) == (False, 9)
    assert "ghost" in g.detail


def test_stats_tolerance_partial_empty_extra() -> None:
    g = grade(SkillId.STATS_COMPUTE, StatsOut.model_validate(MEAS["stats"]), MEAS)
    total = len(MEAS["stats"]["stats"]) * 5
    assert (g.passed, g.n, g.N) == (True, total, total)

    near = copy.deepcopy(MEAS["stats"])
    near["stats"][0]["mean"] += 5e-7 * max(1.0, abs(near["stats"][0]["mean"]))
    assert grade(SkillId.STATS_COMPUTE, near, MEAS).passed

    off = copy.deepcopy(MEAS["stats"])
    off["stats"][0]["mean"] += 1.0
    off["stats"][1]["p50"] -= 1.0
    g = grade(SkillId.STATS_COMPUTE, off, MEAS)
    assert (g.passed, g.n) == (False, total - 2)

    g = grade(SkillId.STATS_COMPUTE, StatsOut(stats=[]), MEAS)
    assert (g.passed, g.n) == (False, 0)

    extra = copy.deepcopy(MEAS["stats"])
    extra["stats"].append({**extra["stats"][0], "column": "site"})
    assert not grade(SkillId.STATS_COMPUTE, extra, MEAS).passed


def test_pii_f1_threshold() -> None:
    findings = [PiiFinding.model_validate(f) for f in PEOPLE["pii"]["findings"]]
    g = grade(SkillId.PII_DETECT, PiiOut(findings=findings), PEOPLE)
    assert (g.passed, g.score, g.n, g.N) == (True, 1.0, len(findings), len(findings))

    half = PiiOut(findings=findings[: len(findings) // 2])
    g = grade(SkillId.PII_DETECT, half, PEOPLE)
    assert not g.passed and g.score < PII_F1_THRESHOLD

    g = grade(SkillId.PII_DETECT, PiiOut(findings=[]), PEOPLE)
    assert (g.passed, g.score, g.n) == (False, 0.0, 0)

    fp = [PiiFinding(row=0, column="id", kind="SIN")]
    g = grade(SkillId.PII_DETECT, PiiOut(findings=findings + fp), PEOPLE)
    assert g.passed  # 1 false positive among 127 keeps F1 >= 0.95
    assert "fp=1" in g.detail
    many_fp = [PiiFinding(row=r, column="id", kind="SIN") for r in range(30)]
    assert not grade(SkillId.PII_DETECT, PiiOut(findings=findings + many_fp), PEOPLE).passed


def test_pii_empty_truth_empty_output() -> None:
    g = grade(SkillId.PII_DETECT, PiiOut(findings=[]), MEAS)
    assert (g.passed, g.score, g.N) == (True, 1.0, 0)


def test_units_exact_and_tolerance() -> None:
    g = grade(SkillId.UNITS_NORMALIZE, UnitsOut.model_validate(MEAS["units"]), MEAS)
    assert (g.passed, g.n, g.N) == (True, 4, 4)
    off = copy.deepcopy(MEAS["units"])
    off["conversions"][1]["offset"] += 1e-6
    g = grade(SkillId.UNITS_NORMALIZE, off, MEAS)
    assert (g.passed, g.n) == (False, 3)
    g = grade(SkillId.UNITS_NORMALIZE, UnitsOut(conversions=[]), MEAS)
    assert (g.passed, g.n) == (False, 0)
    extra = copy.deepcopy(MEAS["units"])
    extra["conversions"].append({**extra["conversions"][0], "column": "sample_id"})
    assert not grade(SkillId.UNITS_NORMALIZE, extra, MEAS).passed


def _cite(column: str, stat: str, value: float) -> CitedNumber:
    return CitedNumber.model_validate({"column": column, "stat": stat, "value": value})


def test_report_citations() -> None:
    s = {x["column"]: x for x in MEAS["stats"]["stats"]}
    good = [
        _cite("weight_lb", "mean", s["weight_lb"]["mean"]),
        _cite("temp_F", "max", s["temp_F"]["max"]),
        _cite("dist_km", "null_count", 1.0),
    ]
    g = grade(SkillId.REPORT_COMPOSE, ReportOut(summary="ok", cited=good), MEAS)
    assert (g.passed, g.n, g.N) == (True, 3, 3)
    wrong = [*good[:2], _cite("dist_km", "null_count", 2.0)]
    g = grade(SkillId.REPORT_COMPOSE, ReportOut(summary="ok", cited=wrong), MEAS)
    assert (g.passed, g.n) == (False, 2)
    g = grade(SkillId.REPORT_COMPOSE, ReportOut(summary="ok", cited=good[:2]), MEAS)
    assert not g.passed  # fewer than 3 citations
    unknown = [*good, _cite("ghost", "mean", 1.0)]
    assert not grade(SkillId.REPORT_COMPOSE, ReportOut(summary="ok", cited=unknown), MEAS).passed


@pytest.mark.parametrize("skill", list(SkillId))
def test_unparseable_output_scores_zero(skill: SkillId) -> None:
    g = grade(skill, {"nonsense": True}, PEOPLE)
    assert (g.passed, g.score, g.n) == (False, 0.0, 0)
