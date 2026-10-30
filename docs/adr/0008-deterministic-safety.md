# 0008 — Deterministic safety; no LLM in trust or grading; card text never in prompts

## Context
Agent Cards and agent outputs are attacker-controlled. An LLM that reads them can be steered by
prompt injection, and an LLM judge is not reproducible.

## Decision
- Trust state (`PENDING`, `ACTIVE`, `QUARANTINED`, `REVOKED`), quarantine and grading decisions are
  made by deterministic code only. No LLM call participates.
- Free text from Agent Cards (name, description, skill descriptions, examples) is never placed in
  any LLM prompt. The broker plans over the closed `SkillId` taxonomy (ADR-0004) and structured card
  fields only.
- Violations are recorded as `Violation(kind, severity HARD|SOFT, agent_id, evidence, at)`.

## Consequences
- Some semantic checks are not possible; they are out of scope rather than delegated to an LLM.
- Phase 5 adds tests that inject adversarial card text and assert it is absent from every prompt.

## Status
Accepted (Phase 0). Enforcement tests: Phase 5.
