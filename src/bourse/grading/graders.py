"""Deterministic graders (ADR-0023). No LLM. `truth` is a dataset's *.truth.json dict.

Thresholds live here and nowhere else.
"""

import math
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel, ValidationError

from bourse.contracts.grading import Grade
from bourse.contracts.skills import SkillId
from bourse.contracts.skills_io import (
    PiiOut,
    ReportOut,
    SchemaProfileOut,
    StatsOut,
    UnitsOut,
)

STATS_TOL = 1e-6
UNITS_TOL = 1e-9
REPORT_TOL = 1e-6
PII_F1_THRESHOLD = 0.95
REPORT_MIN_CITATIONS = 3
REPORT_MAX_SUMMARY = 2000
STAT_KEYS = ("mean", "std", "min", "max", "p50")


def close(a: float | None, b: float | None, tol: float) -> bool:
    """abs or relative tolerance; None only matches None."""
    if a is None or b is None:
        return a is None and b is None
    if math.isnan(a) or math.isnan(b):
        return False
    return abs(a - b) <= max(tol, tol * abs(b))


def _ratio(n: int, total: int) -> float:
    return 1.0 if total == 0 else n / total


def _parse(model: type[BaseModel], output: BaseModel | Mapping[str, Any]) -> BaseModel:
    return output if isinstance(output, model) else model.model_validate(output)


def grade_profile(output: SchemaProfileOut, truth: Mapping[str, Any]) -> Grade:
    want = {c["name"]: c for c in truth["profile"]["columns"]}
    got = {c.name: c for c in output.columns}
    n = 0
    wrong: list[str] = []
    for name, w in want.items():
        g = got.get(name)
        if g is not None and (g.dtype, g.null_count, g.distinct_count) == (
            w["dtype"],
            w["null_count"],
            w["distinct_count"],
        ):
            n += 1
        else:
            wrong.append(name)
    extra = sorted(set(got) - set(want))
    passed = n == len(want) and not extra
    detail = f"columns correct {n}/{len(want)}; wrong={wrong}; extra={extra}"
    return Grade(passed=passed, score=_ratio(n, len(want)), n=n, N=len(want), detail=detail)


def grade_stats(output: StatsOut, truth: Mapping[str, Any]) -> Grade:
    want = {s["column"]: s for s in truth["stats"]["stats"]}
    got = {s.column: s for s in output.stats}
    n = 0
    total = len(want) * len(STAT_KEYS)
    wrong: list[str] = []
    for column, w in want.items():
        g = got.get(column)
        for key in STAT_KEYS:
            if g is not None and close(getattr(g, key), w[key], STATS_TOL):
                n += 1
            else:
                wrong.append(f"{column}.{key}")
    extra = sorted(set(got) - set(want))
    passed = n == total and not extra
    detail = f"values correct {n}/{total}; wrong={wrong[:10]}; extra={extra}"
    return Grade(passed=passed, score=_ratio(n, total), n=n, N=total, detail=detail)


def grade_pii(output: PiiOut, truth: Mapping[str, Any]) -> Grade:
    want = {(f["row"], f["column"], f["kind"]) for f in truth["pii"]["findings"]}
    got = {(f.row, f.column, f.kind) for f in output.findings}
    tp = len(want & got)
    precision = _ratio(tp, len(got)) if got else (1.0 if not want else 0.0)
    recall = _ratio(tp, len(want))
    f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
    detail = (
        f"tp={tp} fp={len(got - want)} fn={len(want - got)} "
        f"precision={precision:.4f} recall={recall:.4f} f1={f1:.4f}"
    )
    return Grade(passed=f1 >= PII_F1_THRESHOLD, score=f1, n=tp, N=len(want), detail=detail)


def grade_units(output: UnitsOut, truth: Mapping[str, Any]) -> Grade:
    want = {c["column"]: c for c in truth["units"]["conversions"]}
    got = {c.column: c for c in output.conversions}
    n = 0
    wrong: list[str] = []
    for column, w in want.items():
        g = got.get(column)
        ok = (
            g is not None
            and (g.dimension, g.from_unit, g.to_unit)
            == (w["dimension"], w["from_unit"], w["to_unit"])
            and close(g.factor, w["factor"], UNITS_TOL)
            and close(g.offset, w["offset"], UNITS_TOL)
        )
        if ok:
            n += 1
        else:
            wrong.append(column)
    extra = sorted(set(got) - set(want))
    passed = n == len(want) and not extra
    detail = f"conversions correct {n}/{len(want)}; wrong={wrong}; extra={extra}"
    return Grade(passed=passed, score=_ratio(n, len(want)), n=n, N=len(want), detail=detail)


def _truth_values(truth: Mapping[str, Any]) -> dict[tuple[str, str], float | None]:
    values: dict[tuple[str, str], float | None] = {}
    for s in truth["stats"]["stats"]:
        for key in STAT_KEYS:
            values[(s["column"], key)] = s[key]
    for c in truth["profile"]["columns"]:
        values[(c["name"], "null_count")] = float(c["null_count"])
        values[(c["name"], "distinct_count")] = float(c["distinct_count"])
    return values


def grade_report(output: ReportOut, truth: Mapping[str, Any]) -> Grade:
    values = _truth_values(truth)
    n = 0
    wrong: list[str] = []
    for c in output.cited:
        want = values.get((c.column, c.stat))
        if want is not None and close(c.value, want, REPORT_TOL):
            n += 1
        else:
            wrong.append(f"{c.column}.{c.stat}")
    total = len(output.cited)
    within = len(output.summary) <= REPORT_MAX_SUMMARY
    passed = n == total and total >= REPORT_MIN_CITATIONS and within
    detail = f"citations correct {n}/{total}; wrong={wrong}; summary_chars={len(output.summary)}"
    return Grade(passed=passed, score=_ratio(n, total), n=n, N=total, detail=detail)


_GRADERS: dict[SkillId, tuple[type[BaseModel], Callable[[Any, Mapping[str, Any]], Grade]]] = {
    SkillId.SCHEMA_PROFILE: (SchemaProfileOut, grade_profile),
    SkillId.STATS_COMPUTE: (StatsOut, grade_stats),
    SkillId.PII_DETECT: (PiiOut, grade_pii),
    SkillId.UNITS_NORMALIZE: (UnitsOut, grade_units),
    SkillId.REPORT_COMPOSE: (ReportOut, grade_report),
}


def grade(skill: SkillId, output: BaseModel | Mapping[str, Any], truth: Mapping[str, Any]) -> Grade:
    """Grade one output. An output that does not parse as the skill's model scores 0/1."""
    model, fn = _GRADERS[skill]
    try:
        parsed = _parse(model, output)
    except ValidationError as exc:
        return Grade(
            passed=False, score=0.0, n=0, N=1, detail=f"invalid output: {exc.error_count()} errors"
        )
    return fn(parsed, truth)
