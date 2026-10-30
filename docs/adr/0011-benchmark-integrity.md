# 0011 — Benchmark integrity

## Context
Iterating against the test split and reporting percentages without denominators produce numbers
that cannot be checked.

## Decision
- BOURSE-Bench has a dev split (iteration) and a frozen test split. The test split ships with a
  manifest of file hashes that is verified before every run.
- `--final` runs only from a clean git worktree and writes a lock file containing the commit SHA and
  a dirty flag.
- Every metric is reported as n/N (with the percentage optional, never alone).

## Consequences
- A final number is traceable to one commit and one manifest.

## Status
Accepted (Phase 0). Implementation: Phases 6–7.
