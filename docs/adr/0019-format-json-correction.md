# 0019 — Correction: format=json support differs by model; submit-tool for all models

## Context
ADR-0006 and AGENTS.md rule 8 stated that Ollama 0.40.6 returns 501 on every `format` path.
The Phase 2a live probe (operator-run, 2026-10-30) recorded:
- qwen3.5:27b-q4_K_M: `format_json_status=200`
- gemma4:12b: `format_json_status=501`

## Decision
- Rule 8 now reads: gemma4:12b returns 501 on format paths; qwen3.5:27b accepts format=json (200)
  but does not enforce our schema. Structured output uses the submit-tool pattern for ALL models.
- No code path sends `format`; the probe keeps recording the status per model.
- This supersedes the "501 for every format path" sentence of ADR-0006; the rest of ADR-0006
  stands.

## Consequences
- One structured-output path for both models; behaviour does not depend on which model a task
  is routed to.

## Status
Accepted (Phase 2b). Supersedes part of ADR-0006.
