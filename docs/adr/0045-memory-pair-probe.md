# ADR-0045: Memory pair probe (measure only)

## Context

Baseline honest runs aborted when resident Ollama models exceeded the hard limit of
**26,000,000,000 bytes** (`limit 26.00 GB / 24.21 GiB`; ADR-0019 / AGENTS.md /
`ResidentMonitor.RESIDENT_LIMIT_BYTES`). ADR-0021 pins the roster to `gemma4:12b` +
`qwen3.5:27b-q4_K_M`. ADR-0038 recorded `gemma4:12b` ≈ 16.7 GiB under MLX. This ADR
records measured resident bytes for candidate pairs and throwaway `gemma4:e4b`
admission-exam n/N.

Pair numbers below are after **one exam-sized submit-tool call per model** (with
reload if Ollama evicted the first model), not sustained-load peaks. The in-run
`ResidentMonitor` peak is authoritative for acceptance and abort decisions.

## Decision

Keep the ADR-0021 roster: `gemma4:12b` (profiler, pii-fast, normalizer) +
`qwen3.5:27b-q4_K_M` (reporter, pii-precise).

Reason: measured pair resident **21.62 GiB (23,216,881,898 bytes)** is under the
26,000,000,000-byte limit; the `gemma4:e4b` pair is larger (**23.18 GiB**) so it gives
no memory benefit; `qwen3.5:4b` previously failed the normalizer exam 3/5 (ADR-0038 /
prior iteration). Do not change `ROSTER_MODELS` or the 26e9-byte limit in this ADR.

## Consequences

Status: Accepted. Roster remains ADR-0021. Limit remains 26,000,000,000 bytes.

## Pair resident bytes (after one exam-sized submit-tool call per model)

Limit = 26,000,000,000 bytes (`26.00 GB / 24.21 GiB`).

| model_a | model_b | bytes | GB | GiB | under_limit | /api/ps names | error |
|---|---|---:|---:|---:|---|---|---|
| `gemma4:12b` | `qwen3.5:27b-q4_K_M` | 23216881898 | 23.22 | 21.62 | True | `qwen3.5:27b-q4_K_M`, `gemma4:12b` |  |
| `gemma4:e4b` | `qwen3.5:27b-q4_K_M` | 24885289774 | 24.89 | 23.18 | True | `qwen3.5:27b-q4_K_M`, `gemma4:e4b` |  |
| `qwen3.5:4b` | `qwen3.5:27b-q4_K_M` | 19575625542 | 19.58 | 18.23 | True | `qwen3.5:27b-q4_K_M`, `qwen3.5:4b` |  |

## Throwaway admission exams on `gemma4:e4b` only

Agents recreated with `BOURSE_MODEL_SMALL=gemma4:e4b` in compose env files (not committed).
Registry rows forced PENDING, admission exams re-run, then models restored to
`gemma4:12b` and state forced ACTIVE. Skills: profiler / pii-fast / normalizer.

| agent | skill | n/N | state | model |
|---|---|---|---|---|
| `normalizer` | `units.normalize` | 5/5 | ACTIVE | `gemma4:e4b` |
| `pii_fast` | `pii.detect` | 5/5 | ACTIVE | `gemma4:e4b` |
| `profiler` | `schema.profile` | 5/5 | ACTIVE | `gemma4:e4b` |
| `profiler` | `stats.compute` | 5/5 | ACTIVE | `gemma4:e4b` |

Written at 2026-11-03T22:57:07Z by `bourse probe --memory`; Decision recorded 2026-11-03 (operator).
