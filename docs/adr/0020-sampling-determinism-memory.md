# 0020 — Sampling set by the proxy; num_ctx and memory headroom

## Context
Phase 0 showed gemma4:12b answering in text instead of calling a tool when sampling at the
default temperature. Phase 2a live probes recorded resident_bytes 25,348,385,263 and
25,410,775,535 with both models loaded, against the 26,000,000,000-byte limit (rule 9).

## Decision
- The metering proxy sets sampling on every upstream call; clients cannot override it
  (request-body `temperature`, `top_p`, `seed` are ignored):
  - `options.temperature` = `BOURSE_METER_TEMPERATURE` (default 0)
  - `options.seed` = `X-Bourse-Seed` header if present (integer, else 400), otherwise
    `BOURSE_METER_SEED` (default 1234)
  - `options.num_ctx` = `BOURSE_METER_NUM_CTX` (default 8192)
  - `keep_alive` = `BOURSE_METER_KEEP_ALIVE` (default "30m")
  OpenAI-compatible upstreams receive `temperature` and `seed`.
- `bourse probe` uses the same num_ctx, so its resident_bytes reflects the agents' context size.
- If resident_ok turns false, the fallback is host Ollama with `OLLAMA_FLASH_ATTENTION=1` and
  `OLLAMA_KV_CACHE_TYPE=q8_0` (smaller KV cache), then a lower num_ctx.

## Amendment (Phase 3 acceptance)
- Observed: Ollama 0.40.6 starts each model's llama-server with `-c 32768 -np 1` and does not
  reload when a request sends `options.num_ctx` 8192; `/api/ps` reports context_length 32768
  for both models. The per-request num_ctx above therefore has no effect on this version.
- After the Phase 3 live suite the probe measured resident_bytes 26,443,292,906 and
  26,509,451,498 (resident_ok false, limit 26,000,000,000); Phase 2b had measured 23,993,702,388.
- Applying the fallback above at the server: `OLLAMA_FLASH_ATTENTION=1`,
  `OLLAMA_KV_CACHE_TYPE=q8_0` and `OLLAMA_CONTEXT_LENGTH=8192` in the Ollama server environment,
  so the server-side context matches the 8192 the proxy assumes.
- Result with these settings: `/api/ps` context_length 8192 for both models; live probes
  resident_bytes 25,834,482,143 and 25,869,399,519 (resident_ok true). Open issue: `/api/ps`
  after the full live suite showed 10,318,314,420 (gemma4:12b) + 15,816,395,323
  (qwen3.5:27b-q4_K_M) = 26,134,709,743 bytes, above the limit. Resident size grows with use;
  the probe measures one point in time. Headroom is below 0.2 GB.

## Consequences
- Same input + same seed gives repeatable outputs up to Ollama's own nondeterminism.
- Agents that need long inputs are bounded by 8192 tokens of context.

## Status
Accepted (Phase 2b).
