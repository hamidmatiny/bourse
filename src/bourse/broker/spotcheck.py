"""Runtime spot checks (ADR-0030): deterministic, no ground truth.

Reliability never uses graders or *.truth.json (those are benchmark-only). Each check
recomputes a small part of the answer from the delegated input with our own deterministic
code (DuckDB table, regex + Luhn + SIN, unit table). Sampling is seeded by the delegation's
task id, so the same delegation always checks the same items.
"""

import csv
import io
import random
from dataclasses import dataclass

from pydantic import BaseModel

from bourse.contracts.skills import SkillId
from bourse.contracts.skills_io import (
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
from bourse.mcp_servers.pii import classify, luhn_valid, sin_valid
from bourse.mcp_servers.table import column_stats_impl, profile_columns_impl
from bourse.mcp_servers.units import parse_header_unit_impl

STATS_TOL = 1e-6
REPORT_TOL = 1e-6
UNITS_TOL = 1e-9
SAMPLE_COLUMNS = 2
SAMPLE_STATS = 2
STAT_NAMES = ("mean", "std", "min", "max", "p50")
PROFILE_CITATIONS = ("null_count", "distinct_count")


@dataclass(frozen=True)
class SpotCheck:
    passed: bool
    detail: str = ""


OK = SpotCheck(True)


def _fail(detail: str) -> SpotCheck:
    return SpotCheck(False, detail)


def _rows(csv_text: str) -> tuple[list[str], list[list[str]]]:
    rows = list(csv.reader(io.StringIO(csv_text)))
    return (rows[0], rows[1:]) if rows else ([], [])


def _close(a: float | None, b: float | None, tol: float) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= tol


def check_profile(inp: SchemaProfileIn, out: SchemaProfileOut, rng: random.Random) -> SpotCheck:
    header, _ = _rows(inp.dataset.csv_text)
    names = [c.name for c in out.columns]
    if names != header:
        return _fail(f"column names {names[:5]} != header {header[:5]}")
    local = {c.name: c for c in profile_columns_impl(inp.dataset.csv_text).columns}
    for col in rng.sample(out.columns, min(SAMPLE_COLUMNS, len(out.columns))):
        if col.null_count != local[col.name].null_count:
            return _fail(f"{col.name}.null_count {col.null_count} != {local[col.name].null_count}")
    return OK


def check_stats(inp: StatsIn, out: StatsOut, rng: random.Random) -> SpotCheck:
    pairs = [(s, stat) for s in out.stats for stat in STAT_NAMES]
    for s, stat in rng.sample(pairs, min(SAMPLE_STATS, len(pairs))):
        try:
            local = column_stats_impl(inp.dataset.csv_text, [s.column]).stats[0]
        except ValueError as exc:
            return _fail(f"{s.column}: {exc}")
        got, want = getattr(s, stat), getattr(local, stat)
        if not _close(got, want, STATS_TOL):
            return _fail(f"{s.column}.{stat} {got} != {want}")
    return OK


def check_pii(inp: PiiIn, out: PiiOut, rng: random.Random) -> SpotCheck:
    header, rows = _rows(inp.dataset.csv_text)
    columns = set(header)
    found = {(f.row, f.column, f.kind) for f in out.findings}
    for f in out.findings:
        if f.column not in columns or not 0 <= f.row < len(rows):
            return _fail(f"finding at row {f.row} column {f.column!r} does not exist")
    for r, row in enumerate(rows):
        for c, value in zip(header, row, strict=False):
            kind = classify(value)
            valid = (
                (kind == "EMAIL")
                or (kind == "CARD" and luhn_valid(value))
                or (kind == "SIN" and sin_valid(value))
            )
            if valid and (r, c, kind) not in found:
                return _fail(f"missed {kind} at row {r} column {c!r}")
    return OK


def check_units(inp: UnitsIn, out: UnitsOut, rng: random.Random) -> SpotCheck:
    header, _ = _rows(inp.dataset.csv_text)
    got = {c.column: c for c in out.conversions}
    for column in got:
        if column not in header:
            return _fail(f"conversion for unknown column {column!r}")
    for h in header:
        ours = parse_header_unit_impl(h).conversion
        if ours is None:
            continue
        theirs = got.get(h)
        if theirs is None:
            return _fail(f"no conversion for {h!r}")
        same = (theirs.dimension, theirs.from_unit, theirs.to_unit) == (
            ours.dimension,
            ours.from_unit,
            ours.to_unit,
        )
        if not (
            same
            and _close(theirs.factor, ours.factor, UNITS_TOL)
            and _close(theirs.offset, ours.offset, UNITS_TOL)
        ):
            return _fail(f"{h}: conversion does not match the unit table")
    return OK


def check_report(inp: ReportIn, out: ReportOut, rng: random.Random) -> SpotCheck:
    stats = {s.column: s for s in inp.stats.stats} if inp.stats else {}
    profile = {c.name: c for c in inp.profile.columns} if inp.profile else {}
    for cite in out.cited:
        source: BaseModel | None
        if cite.stat in PROFILE_CITATIONS:
            source = profile.get(cite.column)
        else:
            source = stats.get(cite.column)
        value = getattr(source, cite.stat, None) if source is not None else None
        if value is None or not _close(float(value), cite.value, REPORT_TOL):
            return _fail(f"cited {cite.column}.{cite.stat}={cite.value} not in the run's outputs")
    return OK


def spot_check(skill: SkillId, inp: BaseModel, out: BaseModel, seed: str) -> SpotCheck:
    """`inp`/`out` are the validated SKILL_IO models of one delegation; seed = its task id."""
    rng = random.Random(f"{seed}:{skill.value}")
    if skill is SkillId.SCHEMA_PROFILE:
        assert isinstance(inp, SchemaProfileIn) and isinstance(out, SchemaProfileOut)
        return check_profile(inp, out, rng)
    if skill is SkillId.STATS_COMPUTE:
        assert isinstance(inp, StatsIn) and isinstance(out, StatsOut)
        return check_stats(inp, out, rng)
    if skill is SkillId.PII_DETECT:
        assert isinstance(inp, PiiIn) and isinstance(out, PiiOut)
        return check_pii(inp, out, rng)
    if skill is SkillId.UNITS_NORMALIZE:
        assert isinstance(inp, UnitsIn) and isinstance(out, UnitsOut)
        return check_units(inp, out, rng)
    assert isinstance(inp, ReportIn) and isinstance(out, ReportOut)
    return check_report(inp, out, rng)
