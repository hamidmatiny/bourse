# 0023 — Deterministic graders and pass thresholds

## Context
Smoke runs now and the bench later need pass/fail per call without an LLM judge (ADR-0008).

## Decision
`bourse.grading.graders.grade(skill, output, truth) -> Grade(passed, score, n, N, detail)`.
Thresholds are constants in that module only.
- schema.profile: per truth column, (dtype, null_count, distinct_count) exact; n/N columns;
  passed iff n == N and no extra columns.
- stats.compute: per (column, stat) for mean/std/min/max/p50, within abs or rel 1e-6;
  passed iff all match and no extra columns.
- pii.detect: cell-level (row, column, kind) precision, recall, F1; n = true positives,
  N = truth findings; passed iff F1 ≥ 0.95.
- units.normalize: exact (dimension, from_unit, to_unit), factor and offset within 1e-9;
  passed iff all match and no extra conversions.
- report.compose: every cited (column, stat, value) must match truth stats/profile within 1e-6;
  n/N cited correct; passed iff n == N, N ≥ 3 and summary ≤ 2000 chars.
- An output that does not parse as the skill's output model scores 0/1.
- Smoke builds reporter input from truth so the reporter is graded in isolation.

## Consequences
- A grade below threshold on a completed call is a quality result, not a failure of the run.

## Status
Accepted (Phase 2b).
