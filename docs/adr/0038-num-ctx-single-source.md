# 0038 — num_ctx single source

## Context
ADR-0033 set num_ctx 4096 through the meter, yet the Phase 4 live probe showed
qwen3.5:27b-q4_K_M at context_length 8192. Cause: `run_probe` defaulted to num_ctx=8192 and
the live probe test called it without a value; Ollama loaded qwen with that context.

## Decision
- `BOURSE_NUM_CTX_BY_MODEL` ("model=n,...", "*" fallback) is the only source. `OllamaProvider`
  sends `options.num_ctx` from it on every request (meter, probe, live tests, any router
  chain); `Sampling` no longer has num_ctx. A unit test fails on any num_ctx literal or
  4096/6144/8192 literal in src/ outside config.py and meter/stats.py, and a fake-upstream test
  checks that the direct provider, the meter and the probe send the same value per model.
- Values from `bourse usage maxcall` (Phase 5a operator run over the host database, rule:
  smallest of 4096/6144/8192 >= 2 x the model's max tokens per call):
  - gemma4:12b: 60 calls, max 526, p50 405, p95 526 -> 2 x 526 = 1052 -> 4096
  - qwen3.5:27b-q4_K_M: 51 calls, max 1666, p50 843, p95 1560 -> 2 x 1666 = 3332 -> 4096
  "models where configured != recommended: 0".
  Default: `qwen3.5:4b=4096,gemma4:e4b=4096,gemma4:12b=4096,qwen3.5:27b-q4_K_M=4096,*=4096`
  (qwen3.5:4b and gemma4:e4b have no maxcall rows yet; 4096 is the fallback value). `bourse usage maxcall` now prints the
  recommendation and the configured value per model.
- The resident monitor counts model reloads (a model that appears after the first sample or
  changes size between samples) and run summaries print "model reloads n".

## Open issue: resident size over the limit (Phase 5a)
- Measured in the first compose acceptance run, both models at context_length 4096:
  qwen3.5:27b-q4_K_M 15,501,812,038 bytes + gemma4:12b 14,943,006,372 bytes = 30.4 GB resident;
  e2e resident peak 30,265,755,882 and 30,444,818,410 bytes. The resident guard (ADR-0033)
  tripped the breaker and aborted 2 of 3 runs in both e2e invocations.
- gemma4:12b's reported size grows with use: 7.8 GB (Phase 3) -> 10.3 GB (Phase 4) -> 13.7,
  14.7, 16.3 GB before an Ollama restart today, 14.9 GB within 10 minutes after it.
- gemma4:e4b is not a smaller replacement: 9.5 GB on disk (8.1B parameters, nvfp4) and
  9,387,631,096 bytes resident at context 4096 on first load, versus about 7.8 GB for a fresh
  gemma4:12b. Not adopted.
- Growth measured with 30 distinct exam-bank prompts each, num_ctx 4096: gemma4:12b
  8,342,642,596 (1st) -> 14,589,345,700 (10th) -> 16,716,386,212 (20th) -> 16,662,290,340
  (30th); qwen3.5:4b 4,082,202,112 -> 8,959,161,856 (30th, still rising); qwen3.5:27b-q4_K_M
  15,501,812,038 flat over 20.
- `/api/ps` reports gemma4:12b (safetensors) on Ollama's `mlx` runner and qwen3.5:27b (GGUF)
  on `llamacpp`. Setting `LLAMA_ARG_CACHE_RAM=0` (llama.cpp's host prompt cache) therefore had
  no effect on gemma: with it set, the next compose run again showed gemma4:12b at
  14,709,489,316 bytes and an e2e resident peak of 30,032,238,826 / 30,211,301,354 bytes.
- Tried (operator, Phase 5a): `OLLAMA_MAX_LOADED_MODELS=1`. The next compose run peaked at
  15,996,559,268 / 16,407,295,908 bytes, but every qwen3.5:27b call through the meter hit the
  120 s deadline (24 of 24 calls 504; reporter exam 0/5; pii-precise and reporter never ACTIVE).
  Reproduced directly against Ollama 0.40.6, no BOURSE code involved: with one model allowed, a
  qwen request that arrives while gemma4:12b is loading is never scheduled; 7 gemma requests
  sent around it completed in 2-4 s, the qwen request timed out at 150 s and a fresh qwen
  request then timed out at 60 s with gemma idle. Unloading gemma (`keep_alive: 0`) released
  it (qwen loaded in 5 s). Sequential swaps work (2-13 s). Not adopted.
- Decision (operator, Phase 5a): `OLLAMA_MAX_LOADED_MODELS=2` and the small model is
  qwen3.5:4b (`BOURSE_MODEL_SMALL`) for profiler, pii_fast and normalizer. Measured on first
  load at context 4096: 4,065,424,916 bytes, `mlx` runner; returns a valid `submit` tool call
  with `think: false`. Risk: the growth measurement above reached 8,959,161,856 bytes after 30
  prompts and was still rising; the headroom next to qwen3.5:27b is 26,000,000,000 -
  15,501,812,038 = 10,498,187,962 bytes. The resident guard (ADR-0033) aborts a run that
  exceeds the limit for 30 s. The admission exam decides whether the three agents still pass.
- First compose runs with this decision: the same stall with `OLLAMA_MAX_LOADED_MODELS=2`,
  also when qwen3.5:27b was loaded first (16,544,767,671 bytes). The qwen3.5:4b agents passed
  (profiler 10/10, pii-fast 5/5); every qwen3.5:27b call hit the 120 s deadline.
- Cause, from the Ollama log (`OLLAMA_DEBUG=1`): system memory, not the model count. Ollama
  0.40.6 checks free system RAM as well as Metal memory. With qwen3.5:27b loaded it saw
  `system memory total="48.0 GiB" free="2.8 GiB"`, unloaded qwen3.5:27b to load qwen3.5:4b,
  then logged `llama-server model predicted to exceed available memory, evicting
  predicted="16.2 GiB" ... system_free="4.0 GiB" system_limited=true` and "waiting for pending
  requests to complete and unload to occur" for qwen3.5:4b, which kept receiving requests and
  was never unloaded, so qwen3.5:27b waited indefinitely. The memory was held by Docker Desktop:
  `MemoryMiB: 49152` (all 48 GiB of RAM allowed), and its VM process used 33-36 GB, even with
  only Postgres running. The `OLLAMA_MAX_LOADED_MODELS=1` stall above fits the same
  mechanism.
- Requirement: Docker Desktop's memory limit is set so that the two models (about 16.2 + 8 GiB)
  fit in free system RAM next to the containers; README Quickstart states the value.

## Consequences
- Phase 5a acceptance run after setting Docker Desktop to 12288 MiB (MemTotal 12,525,584,384):
  Ollama saw 34.0 GiB free before loading and 17.2 GiB after qwen3.5:27b loaded; no eviction.
  Admission exams passed 5/5 agents; e2e runs succeeded 3/3 (planner llm 3/3) in each of two
  invocations; resident peak 23,810,512,726 and 23,872,165,718 bytes (limit 26,000,000,000);
  model reloads 6 and 5; qwen3.5:4b resident 8,369,608,208 bytes at the end.
- `bourse usage maxcall` on that run: qwen3.5:27b-q4_K_M 33 calls, max 1617, p50 863, p95 1614;
  qwen3.5:4b 32 calls, max 753, p50 704, p95 753; both -> 4096, configured != recommended: 0.

## Status
Accepted (Phase 5a).
