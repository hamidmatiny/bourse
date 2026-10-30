import pytest
from pydantic import ValidationError

from bourse.contracts.skills import SkillId
from bourse.contracts.skills_io import (
    MAX_DATASET_BYTES,
    SKILL_IO,
    Conversion,
    DatasetRef,
    PiiOut,
    ReportOut,
    SchemaProfileOut,
    StatsOut,
    UnitsOut,
)
from tests.datasets import NAMES, csv_text, truth


def test_skill_io_covers_every_skill() -> None:
    assert set(SKILL_IO) == set(SkillId)
    for inp, out in SKILL_IO.values():
        for model in (inp, out):
            assert model.model_config.get("extra") == "forbid"
            assert model.model_config.get("frozen") is True


@pytest.mark.parametrize("name", NAMES)
def test_dataset_ref_and_truth_parse(name: str) -> None:
    ref = DatasetRef.from_csv(csv_text(name))
    t = truth(name)
    assert (ref.n_rows, ref.n_cols) == (t["n_rows"], t["n_cols"])
    SchemaProfileOut.model_validate(t["profile"])
    StatsOut.model_validate(t["stats"])
    PiiOut.model_validate(t["pii"])
    UnitsOut.model_validate(t["units"])


def test_dataset_ref_rejects_wrong_hash_and_shape() -> None:
    ref = DatasetRef.from_csv(csv_text("orders"))
    with pytest.raises(ValidationError, match="sha256"):
        DatasetRef(
            csv_text=ref.csv_text + "x", sha256=ref.sha256, n_rows=ref.n_rows, n_cols=ref.n_cols
        )
    with pytest.raises(ValidationError, match="n_rows"):
        DatasetRef(
            csv_text=ref.csv_text, sha256=ref.sha256, n_rows=ref.n_rows + 1, n_cols=ref.n_cols
        )


def test_dataset_ref_size_cap() -> None:
    text = "a\n" + "x\n" * (MAX_DATASET_BYTES // 2)
    with pytest.raises(ValidationError, match="bytes"):
        DatasetRef.from_csv(text)


def test_conversion_target_must_be_canonical() -> None:
    with pytest.raises(ValidationError, match="kg"):
        Conversion(column="w", dimension="mass", from_unit="lb", to_unit="g", factor=1, offset=0)


def test_report_summary_cap() -> None:
    with pytest.raises(ValidationError):
        ReportOut(summary="x" * 2001, cited=[])
