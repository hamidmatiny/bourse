"""Spot checks pass on fixture truth outputs and catch each kind of bad answer.

The truth files are used here only as known-good agent outputs; spotcheck.py never reads them.
"""

import pytest

from bourse.broker.spotcheck import spot_check
from bourse.contracts.skills import SkillId
from bourse.contracts.skills_io import (
    DatasetRef,
    PiiIn,
    PiiOut,
    ReportIn,
    ReportOut,
    SchemaProfileIn,
    SchemaProfileOut,
    StatsIn,
    StatsOut,
    UnitsIn,
    UnitsOut,
)
from tests.datasets import NAMES, csv_text, truth

SEEDS = ["run-1:x:1", "run-2:y:1", "run-3:z:2"]


def _dataset(name: str) -> DatasetRef:
    return DatasetRef.from_csv(csv_text(name))


def _report_in(name: str) -> ReportIn:
    t = truth(name)
    return ReportIn(
        dataset=_dataset(name),
        profile=SchemaProfileOut.model_validate(t["profile"]),
        stats=StatsOut.model_validate(t["stats"]),
    )


def _report_out(name: str) -> ReportOut:
    t = truth(name)
    cited = [
        {"column": s["column"], "stat": stat, "value": s[stat]}
        for s in t["stats"]["stats"]
        for stat in ("mean", "max", "p50")
    ]
    cited += [
        {"column": c["name"], "stat": "null_count", "value": c["null_count"]}
        for c in t["profile"]["columns"][:2]
    ]
    return ReportOut.model_validate({"summary": "Summary.", "cited": cited})


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("seed", SEEDS)
def test_truth_outputs_pass(name: str, seed: str) -> None:
    t = truth(name)
    d = _dataset(name)
    cases = [
        (
            SkillId.SCHEMA_PROFILE,
            SchemaProfileIn(dataset=d),
            SchemaProfileOut.model_validate(t["profile"]),
        ),
        (SkillId.STATS_COMPUTE, StatsIn(dataset=d), StatsOut.model_validate(t["stats"])),
        (SkillId.PII_DETECT, PiiIn(dataset=d), PiiOut.model_validate(t["pii"])),
        (SkillId.UNITS_NORMALIZE, UnitsIn(dataset=d), UnitsOut.model_validate(t["units"])),
        (SkillId.REPORT_COMPOSE, _report_in(name), _report_out(name)),
    ]
    for skill, inp, out in cases:
        result = spot_check(skill, inp, out, seed)
        assert result.passed, (skill, result.detail)


@pytest.mark.parametrize("seed", SEEDS)
def test_catches_fabricated_stat(seed: str) -> None:
    t = truth("measurements")
    stats = t["stats"]["stats"]
    fabricated = [
        {
            **s,
            "mean": s["mean"] + 1.0,
            "max": s["max"] + 1.0,
            "min": s["min"] - 1.0,
            "p50": s["p50"] + 1.0,
            "std": (s["std"] or 0.0) + 1.0,
        }
        for s in stats
    ]
    out = StatsOut.model_validate({"stats": fabricated})
    result = spot_check(SkillId.STATS_COMPUTE, StatsIn(dataset=_dataset("measurements")), out, seed)
    assert not result.passed
    assert "!=" in result.detail


def test_catches_missing_luhn_valid_card() -> None:
    t = truth("people")
    cards = [f for f in t["pii"]["findings"] if f["kind"] == "CARD"]
    assert cards, "fixture must contain a Luhn-valid card"
    missing = cards[0]
    kept = [f for f in t["pii"]["findings"] if f != missing]
    out = PiiOut.model_validate({"findings": kept})
    result = spot_check(SkillId.PII_DETECT, PiiIn(dataset=_dataset("people")), out, "s")
    assert not result.passed
    assert result.detail == f"missed CARD at row {missing['row']} column {missing['column']!r}"


def test_catches_finding_outside_the_table() -> None:
    t = truth("people")
    extra = {"row": 10_000, "column": "email", "kind": "EMAIL"}
    out = PiiOut.model_validate({"findings": [*t["pii"]["findings"], extra]})
    result = spot_check(SkillId.PII_DETECT, PiiIn(dataset=_dataset("people")), out, "s")
    assert not result.passed


def test_catches_wrong_unit_factor() -> None:
    t = truth("measurements")
    convs = [dict(c) for c in t["units"]["conversions"]]
    convs[0]["factor"] = convs[0]["factor"] * 2
    out = UnitsOut.model_validate({"conversions": convs})
    result = spot_check(
        SkillId.UNITS_NORMALIZE, UnitsIn(dataset=_dataset("measurements")), out, "s"
    )
    assert not result.passed
    assert convs[0]["column"] in result.detail


def test_catches_missing_unit_conversion() -> None:
    t = truth("measurements")
    out = UnitsOut.model_validate({"conversions": t["units"]["conversions"][1:]})
    result = spot_check(
        SkillId.UNITS_NORMALIZE, UnitsIn(dataset=_dataset("measurements")), out, "s"
    )
    assert not result.passed


def test_catches_mismatched_citation() -> None:
    good = _report_out("measurements")
    cited = [c.model_dump() for c in good.cited]
    cited[0]["value"] += 0.5
    out = ReportOut.model_validate({"summary": "Summary.", "cited": cited})
    result = spot_check(SkillId.REPORT_COMPOSE, _report_in("measurements"), out, "s")
    assert not result.passed
    assert "not in the run's outputs" in result.detail


def test_catches_wrong_column_name() -> None:
    t = truth("people")
    cols = [dict(c) for c in t["profile"]["columns"]]
    cols[0]["name"] = cols[0]["name"] + "_x"
    out = SchemaProfileOut.model_validate({"columns": cols})
    result = spot_check(
        SkillId.SCHEMA_PROFILE, SchemaProfileIn(dataset=_dataset("people")), out, "s"
    )
    assert not result.passed
    assert "column names" in result.detail


def test_catches_wrong_null_count_on_every_column() -> None:
    t = truth("orders")
    cols = [{**c, "null_count": c["null_count"] + 1} for c in t["profile"]["columns"]]
    out = SchemaProfileOut.model_validate({"columns": cols})
    result = spot_check(
        SkillId.SCHEMA_PROFILE, SchemaProfileIn(dataset=_dataset("orders")), out, "s"
    )
    assert not result.passed


def test_sampling_is_deterministic_per_seed() -> None:
    t = truth("measurements")
    stats = [dict(s) for s in t["stats"]["stats"]]
    stats[0]["mean"] += 1.0  # one fabricated value: caught or not depending on the sample
    out = StatsOut.model_validate({"stats": stats})
    inp = StatsIn(dataset=_dataset("measurements"))
    for seed in SEEDS:
        first = spot_check(SkillId.STATS_COMPUTE, inp, out, seed)
        assert spot_check(SkillId.STATS_COMPUTE, inp, out, seed) == first
