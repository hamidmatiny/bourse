# 0004 — Pydantic contracts as the sole interface; closed skill taxonomy

## Context
Components (registry, broker, agents, ledger, trust runtime, bench) are built in separate phases.
Untyped dicts between them hide drift, and an open skill vocabulary lets untrusted cards define
what the broker plans for.

## Decision
- Components exchange only Pydantic v2 models from `src/bourse/contracts/`, each with
  `model_config = ConfigDict(extra="forbid", frozen=True)`.
- Skills form a closed `StrEnum` `SkillId` with 5 members: `schema.profile`, `stats.compute`,
  `pii.detect`, `units.normalize`, `report.compose`. Unknown skills fail validation.
- `TaskDAG` validates unique ids, existing dependencies and acyclicity at construction.

## Consequences
- Adding a skill requires an ADR and a contract change.
- Unit tests cover JSON round-trip and extra-field rejection for every contract model.

## Status
Accepted (Phase 0).
