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

## Consequences
- Same input + same seed gives repeatable outputs up to Ollama's own nondeterminism.
- Agents that need long inputs are bounded by 8192 tokens of context.

## Status
Accepted (Phase 2b).
