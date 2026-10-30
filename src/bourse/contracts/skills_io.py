"""Typed input/output for each skill in the closed taxonomy (ADR-0015).

Row indices are 0-based over data rows (the header is not a row). Canonical units:
length m, mass kg, temperature degC, duration s, volume L. A conversion maps a raw value v
to canonical units as v * factor + offset (offset is non-zero only for temperature).
"""

import csv
import hashlib
import io
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bourse.contracts.skills import SkillId

MAX_DATASET_BYTES = 256 * 1024

Dtype = Literal["int", "float", "string", "date", "bool"]
PiiKind = Literal["EMAIL", "PHONE", "CARD", "SIN", "PERSON_NAME"]
Dimension = Literal["length", "mass", "temperature", "duration", "volume"]
CANONICAL_UNITS: dict[str, str] = {
    "length": "m",
    "mass": "kg",
    "temperature": "degC",
    "duration": "s",
    "volume": "L",
}


def csv_shape(csv_text: str) -> tuple[int, int]:
    """(data rows, columns) of a CSV with a header row."""
    rows = list(csv.reader(io.StringIO(csv_text)))
    if not rows:
        return 0, 0
    return len(rows) - 1, len(rows[0])


class DatasetRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    csv_text: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    n_rows: int = Field(ge=0)
    n_cols: int = Field(ge=0)

    @model_validator(mode="after")
    def _verify(self) -> "DatasetRef":
        data = self.csv_text.encode("utf-8")
        if len(data) > MAX_DATASET_BYTES:
            raise ValueError(f"csv_text is {len(data)} bytes > {MAX_DATASET_BYTES}")
        if hashlib.sha256(data).hexdigest() != self.sha256:
            raise ValueError("sha256 does not match csv_text")
        if csv_shape(self.csv_text) != (self.n_rows, self.n_cols):
            raise ValueError("n_rows/n_cols do not match csv_text")
        return self

    @classmethod
    def from_csv(cls, csv_text: str) -> "DatasetRef":
        n_rows, n_cols = csv_shape(csv_text)
        digest = hashlib.sha256(csv_text.encode("utf-8")).hexdigest()
        return cls(csv_text=csv_text, sha256=digest, n_rows=n_rows, n_cols=n_cols)


# --- schema.profile ------------------------------------------------------------------------


class SchemaProfileIn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset: DatasetRef


class ColumnProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    dtype: Dtype
    null_count: int = Field(ge=0)
    distinct_count: int = Field(ge=0)


class SchemaProfileOut(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    columns: list[ColumnProfile]


# --- stats.compute -------------------------------------------------------------------------


class StatsIn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset: DatasetRef


class ColumnStats(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    column: str
    mean: float
    std: float | None  # sample standard deviation (n - 1); None when fewer than 2 values
    min: float
    max: float
    p50: float  # median, linear interpolation


class StatsOut(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stats: list[ColumnStats]


# --- pii.detect ----------------------------------------------------------------------------


class PiiIn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset: DatasetRef


class PiiFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    row: int = Field(ge=0)
    column: str
    kind: PiiKind


class PiiOut(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    findings: list[PiiFinding]


# --- units.normalize -----------------------------------------------------------------------


class UnitsIn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset: DatasetRef


class Conversion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    column: str
    dimension: Dimension
    from_unit: str
    to_unit: str
    factor: float
    offset: float

    @model_validator(mode="after")
    def _canonical_target(self) -> "Conversion":
        if CANONICAL_UNITS[self.dimension] != self.to_unit:
            raise ValueError(
                f"to_unit for {self.dimension} must be {CANONICAL_UNITS[self.dimension]}"
            )
        return self


class UnitsOut(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    conversions: list[Conversion]


# --- report.compose ------------------------------------------------------------------------


class ReportIn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset: DatasetRef
    profile: SchemaProfileOut | None = None
    stats: StatsOut | None = None
    pii: PiiOut | None = None
    units: UnitsOut | None = None


class CitedNumber(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    column: str
    stat: Literal["mean", "std", "min", "max", "p50", "null_count", "distinct_count"]
    value: float


class ReportOut(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(max_length=2000)
    cited: list[CitedNumber]


SKILL_IO: dict[SkillId, tuple[type[BaseModel], type[BaseModel]]] = {
    SkillId.SCHEMA_PROFILE: (SchemaProfileIn, SchemaProfileOut),
    SkillId.STATS_COMPUTE: (StatsIn, StatsOut),
    SkillId.PII_DETECT: (PiiIn, PiiOut),
    SkillId.UNITS_NORMALIZE: (UnitsIn, UnitsOut),
    SkillId.REPORT_COMPOSE: (ReportIn, ReportOut),
}
