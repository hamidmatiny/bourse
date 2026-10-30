# 0001 — ADR process; AGENTS.md is the source of rules

## Context
Work is done by coding agents driven by an operator who runs every command. Rules and decisions must
survive across sessions and agents without relying on chat history.

## Decision
- `AGENTS.md` holds the hard rules, phase plan and reporting rules. `CLAUDE.md` contains only
  `@AGENTS.md` so every agent reads the same file.
- Every architectural decision is an ADR at `docs/adr/NNNN-title.md` with sections Context,
  Decision, Consequences, Status. ADRs are numbered sequentially and never renumbered.
- A decision that changes an earlier ADR is a new ADR that marks the old one "Superseded by NNNN".

## Consequences
- Rule changes are visible in git history of one file.
- Adding a skill to the taxonomy, a provider, or a trust check requires an ADR first.

## Status
Accepted (Phase 0).
