# 0033 — Memory guard and num_ctx decision

## Context
Rule 9: combined resident size < 26,000,000,000 bytes. Phase 3: probes passed at
25,834,482,143 and 25,869,399,519 bytes, but `/api/ps` after the live suite showed
26,134,709,743 (ADR-0020). Ollama 0.40.6 ignores per-request `num_ctx`; the server context comes
from `OLLAMA_CONTEXT_LENGTH`.

## Decision
- Host Ollama runs with `OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0
  OLLAMA_MAX_LOADED_MODELS=2 OLLAMA_NUM_PARALLEL=2 OLLAMA_CONTEXT_LENGTH=4096`. The probe
  records these as "unverified" because the Ollama API does not expose them; it records each
  loaded model's `context_length` from `/api/ps`, which it does expose.
- `ResidentMonitor` (runtime/resident.py) samples `/api/ps` every 5 s during `bourse run`,
  `bourse e2e all` and `bourse runs resume`, writes data/metrics/resident-<utc>.jsonl, and
  tracks the peak. Over the limit continuously for >= 30 s -> it trips the run breaker, so the
  run is ABORTED with zero further delegations (ADR-0026). Every run summary prints
  "resident peak <bytes> (limit 26000000000)"; `e2e all` exits non-zero if the peak is over.
- num_ctx: `bourse usage maxcall` prints max/p50/p95 of tokens_in + tokens_out per model.
  Rule: the smallest of 4096/6144/8192 that is >= 2 x the observed max. Measured on the 98
  llm_usage rows from the Phase 3 runs: gemma4:12b 58 calls, max 526, p50 409, p95 526;
  qwen3.5:27b-q4_K_M 40 calls, max 1666, p50 776, p95 1560. 2 x 1666 = 3332 -> 4096.
  `BOURSE_METER_NUM_CTX` default and the host `OLLAMA_CONTEXT_LENGTH` move from 8192 to 4096.
  If a later `bourse usage maxcall` shows a max above 2048, the rule picks 6144 or 8192 and
  this ADR is amended.

## Consequences
- A sustained memory overrun stops a run instead of letting the host swap; a spike under 30 s
  only raises the peak (unit-tested with a fake clock).
- Prompts above 4096 tokens fail at the model, not silently truncate in BOURSE.

## Status
Accepted (Phase 4).
