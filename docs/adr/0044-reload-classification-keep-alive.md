# 0044 — Reload classification, keep_alive, upstream timings, Ollama gate

## Context
The resident monitor counted every new or size-changed model after the first sample as a
"reload". The 5a e2e summary printed `model reloads 5`. Probe and some Ollama calls omitted
`keep_alive`, so models expired and were loaded again.

Phase 5b exams for qwen agents (`reporter`, `pii-precise`) recorded meter `504 deadline` with
`queue_wait_ms ≈ 0` while host `OLLAMA_NUM_PARALLEL=2` was shared with small-model traffic.
Isolated qwen `/api/chat` completed in ~10 s cold / <1 s warm — so the 120 s meter deadline was
being burned waiting *inside* Ollama, invisible to the per-agent fair-share gate.

## Decision
- One config value `BOURSE_METER_KEEP_ALIVE` (default `30m`) is sent on meter Ollama chat
  bodies (always, even without a Sampling object) and on both probe `/api/chat` requests.
- `ResidentMonitor` classifies each appearance: `INITIAL_LOAD` (models on the first sample),
  `KEEP_ALIVE_EXPIRY` (reappear after unload with no other model disappearing),
  `PARAM_CHANGE` (same name, different size), `EVICTION` (a load that coincides with another
  model disappearing — typically a third model with `OLLAMA_MAX_LOADED_MODELS=2`).
- Summaries print `model reloads n (INITIAL_LOAD=… KEEP_ALIVE_EXPIRY=… PARAM_CHANGE=…
  EVICTION=…)`. Target: <= 2 reloads of kind INITIAL_LOAD per stack session (the two resident
  models).
- Meter records Ollama upstream timings on each `llm_usage` row: `total_duration_ms`,
  `load_duration_ms`, `prompt_eval_duration_ms`, `eval_duration_ms`, and `reload`
  (`load_duration_ms > 1000`). Migration `0009_upstream_timings`. Compose writes usage to
  database **`bourse_compose`** (migrate); the default `POSTGRES_DB=bourse` has no tables.
- Meter global upstream gate size = `OLLAMA_NUM_PARALLEL` (config, default 2). Waiting for a
  global slot is included in `queue_wait_ms`. Per-agent `max_in_flight` remains.
- `bourse probe --latency N` runs N isolated `/api/chat` samples per configured model and
  prints p50/p95 for total/load/eval seconds; writes `data/probe/latency-latest.json`.
- Exam task deadline for a model = `max(bourse_exam_deadline_s, 3 * p95_total_s)` from that
  file (embedded into compose registry env as `BOURSE_EXAM_DEADLINE_BY_MODEL`). Exam tasks
  for one agent run with concurrency 1; exams for different agents may overlap up to the
  global meter gate. Pass marks (`pass_n`/`k`) are unchanged.
- Activation wait (`stack wait-active` and phase5 preflight) uses one value:
  `exam_timeout_s + 120` (not a hard 600 s cap). Wait until every honest agent is ACTIVE or
  EXAM_FAILED, then print one exam summary table (do not stop at the first EXAM_FAILED).

### Measured latency (`bourse probe --latency 5`, 2026-11-03T16:21:37Z)

Host: `OLLAMA_NUM_PARALLEL=2`, `OLLAMA_MAX_LOADED_MODELS=2`, `OLLAMA_CONTEXT_LENGTH=4096`.
Config `ollama_num_parallel=2` (HTTP API does not expose the host setting).

| model | n | total p50/p95 (s) | load p50/p95 (s) | eval p50/p95 (s) | 3×p95 (s) |
|---|---|---|---|---|---|
| `qwen3.5:4b` | 5 | 0.05 / 1.22 | 0.01 / 1.12 | 0.02 / 0.02 | 3.66 |
| `qwen3.5:27b-q4_K_M` | 5 | 0.26 / 10.33 | 0.00 / 9.67 | 0.10 / 0.14 | 30.99 |

**Exam deadline is the base `bourse_exam_deadline_s` = 300 s**, not a measured value.
`max(300, 3×p95)` is capped by the base because 3×p95 was 3.66 s / 30.99 s (≪ 300). Do not
describe 300 s as measured from probe.

Isolated cold qwen (operator curl, num_ctx=8192): total 10.34 s (load 9.9, prompt_eval 0.33,
eval 0.1). Matches p95 above. Prior failing exams: reporter 10× `504 deadline` with
`queue_wait_ms≈0`; pii_precise 8× `504` + 1× `429 contention` — consistent with Ollama-internal
queueing, not meter fair-share wait. Global OLLAMA_NUM_PARALLEL gate fixed those 504s.

## Consequences
- Existing tests that asserted `model reloads 2` now assert counts by kind.
- First-sample INITIAL_LOAD events are still recorded for diagnostics, but do **not**
  increment `reloads` (per-session baseline; see ADR-0042 fix round 3).
- Diagnose compose usage with
  `docker compose exec postgres psql -U bourse -d bourse_compose -c "…"`.
- Operator runs `bourse probe --latency 5` (or `scripts/acceptance_5b.sh`) before stack up so
  deadlines are embedded; without the file, exams use the base deadline only.

## Status
Accepted (Phase 5b; amended 2026-11-03).
