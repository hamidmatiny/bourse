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

## Amendment (Phase 6a, ADR-0051)
The manifest stores seeds and per-case sha256s (cases are regenerated and must match); its own
sha256 is pinned in `<manifest>.sha256`. `--final` also requires the roster model digests from
Ollama `/api/ps` to match the config, and records git sha, manifest hash, digests and the Ollama
environment. The git check runs on the host (`bench preflight`); the container re-checks the
manifest and digests. Loading the test split without the final guard is refused in code.

## Status
Accepted (Phase 0). Implementation: Phases 6a–7.
