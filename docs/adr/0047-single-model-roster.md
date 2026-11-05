# ADR-0047: Single-model roster (all agents on qwen3.5:27b-q4_K_M)

## Context

ADR-0021 (amended Phase 5b) pinned a two-model roster: `gemma4:12b` (profiler,
pii-fast, normalizer) + `qwen3.5:27b-q4_K_M` (pii-precise, reporter). In-run
resident peaks with both loaded exceeded the 26,000,000,000-byte limit (ADR-0046
baseline abort at 29.47 GB; candidate **b** still 26.22 GB at `OLLAMA_NUM_PARALLEL=1`).

ADR-0046 candidate **d** (throwaway, operator-approved): all five honest agents on
`qwen3.5:27b-q4_K_M`, host+meter `OLLAMA_NUM_PARALLEL=2`, gemma unloaded.

| metric | value |
|---|---|
| in-run peak | **15,501,812,038** bytes (qwen27b only) |
| exams | **30/30** |
| baseline | **3/3** SUCCEEDED |
| exam wall | **386.0 s** |
| pii_fast tokens in/out | **3883 / 222** (8 calls) |
| pii_precise tokens in/out | **14146 / 1582** (18 calls) |

## Decision

1. **Supersede** the ADR-0021 model columns: every honest agent uses
   `qwen3.5:27b-q4_K_M`. Code SSO: `bourse.agents.catalog.ROSTER_MODELS` (and
   `ROSTER_MODEL` / `ROSTER_SMALL_MODEL` / `ROSTER_LARGE_MODEL` aliases, all equal).
2. **Trade-off (plain):** one LLM for all agents. Diversity is **framework + agent
   design + vendor**, not model family. Cost differences between agents come from
   **agent design**, measured in **tokens** (e.g. pii_precise ≫ pii_fast in ADR-0046 d).
3. Keep `OLLAMA_NUM_PARALLEL=2` as the default meter gate / config value.
4. Probe and stack warn if `/api/ps` shows any loaded model outside the roster set
   (name + size).
5. **Timeout ordering** (single source in `Settings`, fail-fast at startup):

   | knob | value (s) | role |
   |---|---:|---|
   | `bourse_agent_llm_timeout_s` | **180** | agent→meter HTTP/wall client timeout per LLM call |
   | `bourse_meter_deadline_s` | **120** | meter per-call wall deadline |
   | `bourse_exam_deadline_s` | **300** | per exam-task deadline (base; may rise via 3×p95) |
   | `bourse_exam_timeout_s` | **900** | whole-exam wall |

   Invariant: `agent_llm_timeout_s >= meter_deadline_s < exam_deadline_s <= exam_timeout_s`.

## Consequences

- Two-model resident pressure is removed for the honest roster; adversaries may still
  load other tags if misconfigured — the `/api/ps` warning surfaces that.
- Shadow prices / latency priors for gemma / qwen4b remain in `prices.toml` for
  historical ledger rows; new work prices at the qwen27b row.
- ADR-0021 packaging (vendors, frameworks, ports, CrewAI isolation) is unchanged;
  only the **model** column is superseded here.
- Status: **Accepted** (operator-approved 2026-11-04). Roster/limit otherwise
  unchanged (limit still 26e9).
