# ADR-0046: In-run resident growth and candidate mitigations (measure only)

## Context

Acceptance baseline aborted with `RESIDENT_LIMIT` peak **29,467,613,418 bytes**
(29.47 GB / 27.44 GiB) after exams had already peaked at **28.68 GB**. The one-shot
pair probe (ADR-0045) under-measured at ~23.2 GB. In-run `ResidentMonitor` peak is
authoritative. Roster (ADR-0021) and the 26,000,000,000-byte limit are **not** changed
here.

This run's `data/metrics/resident-20261103T232536Z.jsonl` stored **totals only**
(no per-model fields). `ResidentMonitor` now writes `models[{name,size,runner,context_length}]`
per sample (forward-looking).

## Decision

Operator chooses any follow-up mitigation from the candidate table below.
Agents must not change `ROSTER_MODELS` or `RESIDENT_LIMIT_BYTES` in this ADR.

## Consequences

Status: Accepted (measurement record). Roster/limit unchanged.

## This-run totals timeline (pre per-model logging)

File: `data/metrics/resident-20261103T232536Z.jsonl` (15 samples, keys=`at,resident_bytes`).

| at (UTC) | resident_bytes | GB |
|---|---:|---:|
| 23:25:36 → 23:25:56 (5 samples) | 28675899626 | 28.68 |
| 23:26:01 | 29290111210 | 29.29 |
| 23:26:06 → 23:26:46 (9 samples) | 29467613418 | 29.47 |

Growth during this broker session: **+791.7 MB** (28.68 → 29.47 GB) over ~30 s starting
~25 s after monitor start (baseline run after exams). **Which model grew cannot be named
from this file** (totals only). Forward samples include per-model size/runner/context_length.

Live `/api/ps` after the abort still showed both residents: `gemma4:12b` **mlx** (~14.0 GB,
ctx 4096) + `qwen3.5:27b-q4_K_M` **llamacpp** (~15.5 GB, ctx 4096).

## gemma4:12b MLX / num_ctx check

| field | value |
|---|---|
| show.format | safetensors |
| show.runner | mlx |
| show.quant | nvfp4 |
| requested num_ctx | 4096 (`BOURSE_NUM_CTX_BY_MODEL`) |
| /api/ps context_length before 4k-ish call | 4096 |
| /api/ps context_length after | 4096 (honored / reported) |
| size before | 7,666,573,236 |
| size after | 8,394,071,988 |
| size_delta | **+727,498,752 (~694 MiB)** |

Conclusion: gemma4:12b runs on the **MLX** runner. `context_length` in `/api/ps` stays at
the requested 4096. Resident **size still grows** after a large prompt under MLX (KV/working
set), so num_ctx alone does not freeze bytes. `OLLAMA_KV_CACHE_TYPE` is host-only / not
exposed by the Ollama 0.40.6 HTTP API (ADR-0006 `unverified`); cannot confirm it is honored
for MLX from `/api/ps`.

## Candidate measurements (throwaway; no committed config change)

`OLLAMA_NUM_PARALLEL` is a **host** Ollama setting. Measure scripts only align the meter
gate; they do not start/stop/restart host Ollama. Docker Desktop VM was reduced to ~8 GiB
after a mid-session socket death.

Reference (NUM_PARALLEL=2, prior acceptance): exam wait-active wall **601 s**, exam peak
**28,675,899,626** bytes (28.68 GB). Raw JSON: `data/probe/adr-0046-candidates.json`.

| id | setup | available? | exam peak (bytes) | baseline peak (bytes) | exam wall_s | baseline wall_s | exam n/N (per skill) | notes |
|---|---|---|---:|---:|---:|---:|---|---|
| a | gemma4:12b **GGUF/llama.cpp** + qwen27b, `OLLAMA_NUM_PARALLEL=2` | **no** | — | — | — | — | — | No local GGUF gemma4:12b tag. Installed `gemma4:12b` is safetensors/mlx. Tags `gemma4:12b-gguf`, `…-q4_K_M` → 404. |
| b | current gemma4:12b (mlx) + qwen27b, host+meter `OLLAMA_NUM_PARALLEL=1` | **yes** | **26,216,038,634** | **26,216,038,634** | **577.4** | **86.7** (1 run then stop) | profiler schema 3/5 stats 3/5; reporter 5/5; normalizer 2/5; pii_precise 5/5; pii_fast 2/5 | Peak still **> 26e9**. Baseline **0/3** `ABORTED` `RESIDENT_LIMIT` after first run. In-run per-model (`resident-20261104T023200Z.jsonl`): gemma4:12b mlx **10,714,226,596** ctx 4096; qwen3.5:27b-q4_K_M llamacpp **15,501,812,038** ctx 4096. Exam wall **577.4 s** vs parallel=2 **601 s** (−23.6 s). |
| c | qwen3.5:4b (profiler/pii-fast/normalizer) + qwen27b large, host+meter parallel=1 | **yes** (throwaway) | **21,118,801,734** | **21,118,801,734** | **584.5** | **84.1** (1 run then stop) | profiler schema 2/5 stats 4/5; reporter 5/5; normalizer 3/5; pii_precise 5/5; pii_fast 2/5 | Gemma unloaded before measure (`keep_alive=0`). Peak **under** 26e9. In-run per-model (`resident-20261104T025730Z.jsonl`): qwen3.5:4b mlx **5,616,989,696** ctx 4096; qwen3.5:27b-q4_K_M llamacpp **15,501,812,038** ctx 4096. Baseline **0/3** `FAILED` (`no_candidates` — small agents left PENDING after exam fails). First c attempt without unload was invalid (gemma stayed resident under `MAX_LOADED_MODELS=2`); discarded. Roster restored to gemma4:12b after measure. |
| b-failure-types | (analysis of b exam tasks; parallel=1) | — | — | — | — | — | passed **20/30**; failed **10/30** | **Do not conclude model quality**: same gemma agents scored 5/5 with parallel=2. All 10 failures = `agent FAILED + error_kind=llm_unavailable` (AllProvidersFailed). No `EXAM_TIMEOUT`, no `grader-failed`. wall_ms ≈ **60000** each; queue_wait_ms_sum = **0** (llm_usage `error_kind=contention` on the call row). Agents: profiler×4 (schema×2, stats×2), normalizer×3, pii_fast×3. Detail: `data/probe/adr-0046-failure-types.json`. |
| c-failure-types | (analysis of clean-c exam tasks; parallel=1) | — | — | — | — | — | passed **21/30**; failed **9/30** | Same caveat (1-slot pressure). All 9 failures = `agent FAILED + error_kind=llm_unavailable`. No `EXAM_TIMEOUT`, no `grader-failed`. wall_ms ≈ **60000**; queue_wait_ms_sum = **0**. Agents: profiler×4 (schema×3, stats×1), normalizer×2, pii_fast×3. Model on failed calls = qwen3.5:4b (throwaway). |
| d | **all 5 agents** qwen3.5:27b-q4_K_M; host+meter `OLLAMA_NUM_PARALLEL=2`; gemma unloaded | **yes** (throwaway) | **15,501,812,038** | **15,501,812,038** | **386.0** | **162.8** | **30/30** (all skills 5/5); failed **0** | `/api/ps` after unload and during run: **only** qwen3.5:27b-q4_K_M. Per-model peak: qwen27b llamacpp **15,501,812,038** ctx 4096. Baseline **3/3** SUCCEEDED. Tokens in/out (exams+baseline window): profiler 10048/1480; **pii_fast 3883/222** (8 calls); **pii_precise 14146/1582** (18 calls); normalizer 5111/500; reporter 9320/2754; broker 1998/327. Exam-summary “model” column still showed roster labels for small agents (registry `model_for_agent` map); llm_usage confirms all calls were qwen27b. Roster+meter gate restored to gemma4:12b / parallel=**2** after measure (cards re-registered at 1.0.2). Raw: `data/probe/adr-0046-candidate-d.json`. |

### b / c failed-task listing (compact)

Every failed task in b and c was the same type. Full rows (agent, model, skill, wall_ms,
queue_wait_ms_sum, task_id) are in `data/probe/adr-0046-failure-types.json`.

| config | failed | by type | n/N passed |
|---|---:|---|---|
| b | 10 | llm_unavailable×10 | 20/30 |
| c | 9 | llm_unavailable×9 | 21/30 |
| d | 0 | — | 30/30 |

## Default parallel restored

Meter gate + all `data/run/env/*.env` set back to **`OLLAMA_NUM_PARALLEL=2`** (config default
was already 2). parallel=1 was test-only. Host Ollama restart with NUM_PARALLEL=2 is the
operator’s responsibility (not done by scripts).

## InfraNotReady exit

Raising `InfraNotReady` inside the `ResidentMonitor` TaskGroup produced a rich
`ExceptionGroup` traceback. Fix: defer the raise until after `broker_from_settings` exits;
CLI also unwraps nested `BaseExceptionGroup` to print one `INFRA NOT READY:` line and rc=1.

## Operator next step

Choose mitigation (roster/limit still unchanged here). **b** over limit; **c** under limit
but exams/baseline incomplete under 1-slot pressure (not a model-quality verdict); **d**
single-model qwen27b clears the limit with 30/30 exams and 3/3 baseline in this throwaway.

Written 2026-11-04 (measurement session).
