# 0030 — Reliability from runtime spot checks, never from graders

## Context
Graders compare outputs with ground truth files and exist for the benchmark (ADR-0023). At
runtime there is no ground truth, and a reliability score built from graders would leak the
benchmark into selection.

## Decision
- `broker/spotcheck.py` runs after contract validation on every completed delegation. It uses
  only the delegated input and our own deterministic code; it never imports graders or reads
  `*.truth.json`. Sampling is seeded with the delegation task id.
  - schema.profile: column names and order equal the CSV header; null_count of 2 sampled
    columns equals DuckDB's recount.
  - stats.compute: 2 sampled (column, stat) pairs recomputed within 1e-6.
  - pii.detect: every cell our regex + Luhn + SIN scan marks as EMAIL, CARD or SIN must be in
    the findings (recall floor); every finding must reference an existing row and column.
  - units.normalize: for every header our parser resolves, the agent's conversion must exist
    and match dimension, from, to, factor and offset (factor/offset within 1e-9).
  - report.compose: every cited number must equal the run's stats output (mean, std, min, max,
    p50) within 1e-6. Deviation from the phase brief: citations of null_count/distinct_count
    are checked against the run's profile output, because ReportOut allows those two stats and
    the reporter cites them from the profile.
- A failed check is outcome `FAILED_SPOTCHECK`: counted as a failure and a fallback trigger.
- Reliability per (agent_id, skill, agent_major): s = COMPLETED with spot_check_passed; f =
  every other ledger row of that agent. Broker-side aborts write no ledger row. Score = Wilson
  95% lower bound (z = 1.959963984540054). A new major version starts from s = f = 0.

## Consequences
- Unit tests: Wilson LB exact for (0,0)=0, (1,0)=0.20654931437723742,
  (5,0)=0.5655175352168252, (9,1)=0.5958499732047616, (50,50)=0.4038315303659957; each check
  passes on fixture truth outputs (3 datasets x 3 seeds) and catches a fabricated stat, a
  missing Luhn-valid card, a wrong factor, a mismatched citation and a wrong column name.
- Sampling checks can miss a single wrong value; they bound, not prove, correctness.

## Status
Accepted (Phase 4).
