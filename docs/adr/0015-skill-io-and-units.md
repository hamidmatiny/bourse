# 0015 — Skill I/O contracts and canonical units

## Context
Agents (Phase 2b) and the broker (Phase 3) exchange data per skill. Outputs must be checkable
against ground truth without an LLM (ADR-0008).

## Decision
- `bourse.contracts.skills_io` defines an input and an output model per `SkillId`; `SKILL_IO`
  maps all 5 skills (unit test asserts completeness). All models `extra="forbid", frozen=True`.
- `DatasetRef(csv_text, sha256, n_rows, n_cols)`: csv_text ≤ 256 KiB; the validator recomputes
  SHA-256 and the CSV shape (header excluded from n_rows).
- Row indices are 0-based data rows. Empty cells are null.
- Dtype rule over non-null cells, first match wins: bool (true/false, any case) > int
  (`-?[0-9]+`) > float > date (valid `YYYY-MM-DD`) > string; an all-null column is string.
- `stats.compute`: numeric (int/float) columns only; `std` is the sample standard deviation
  (n−1), `None` with fewer than 2 values; `p50` is the median with linear interpolation.
- `pii.detect` kinds: EMAIL, PHONE, CARD (Luhn-valid only), SIN (valid only), PERSON_NAME.
- Canonical units: length m, mass kg, temperature degC, duration s, volume L. A `Conversion`
  maps v to canonical as `v * factor + offset`; offset is non-zero only for temperature
  (degF: factor 5/9, offset −160/9; K: factor 1, offset −273.15). `to_unit` must be canonical.
- `report.compose`: summary ≤ 2000 chars and a list of cited numbers (column, stat, value) so
  numbers in the summary can be checked against `stats`/`profile`.
- Fixtures: 3 hand-built CSVs in `tests/fixtures/datasets/` (30, 25, 20 rows) with ground truth
  computed independently of `bourse` code.

## Consequences
- Changing a rule above changes ground truth; it needs a new ADR and regenerated fixtures.

## Status
Accepted (Phase 2a).
