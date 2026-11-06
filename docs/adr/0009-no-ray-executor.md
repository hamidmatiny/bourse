# 0009 — No Ray; Executor protocol with explicit pools

## Context
BOURSE runs on one node with one GPU. LLM throughput is bounded by the Ollama server
(`OLLAMA_NUM_PARALLEL`), not by worker count. Ray adds a scheduler, object store and failure modes
without removing that bottleneck.

## Decision
- No Ray dependency.
- `runtime.executor.Executor` protocol: `submit_llm` (asyncio semaphore sized
  `OLLAMA_NUM_PARALLEL`), `submit_cpu` (`ProcessPoolExecutor`), `map(kind, ...)`, `aclose`.
  Phase 0 ships only the protocol.
- Phase 6 implements the pools and reports measured speedup at 1/2/4/8 workers.
- A Ray backend can be added later behind the same protocol (new ADR).

## Consequences
- Concurrency limits are explicit and testable.

## Amendment (Phase 6a, ADR-0051)
`PoolExecutor` implements the protocol: `submit_llm` behind an anyio semaphore of
`llm_slots`, `submit_cpu` in a lazily created `ProcessPoolExecutor`, `map` keeping input order.
The broker uses it with `subtask_slots` from the bench config (default `OLLAMA_NUM_PARALLEL`).
Speedup is measured on the same cases at 1 and 2 slots only: the meter gate is 2, so 4 and 8
would measure meter queueing (ADR-0051).

### Measured (dev pilot `pilot-6a`, 2026-11-05)
`B3` (`subtask_slots: 2`) vs `B3-serial` (`subtask_slots: 1`), same 5 dev cases, paired:
wall 407 s serial -> 399 s parallel, total ratio 1.02x, per-case paired ratio p50 1.00x.
Summed LLM compute per case rose with 2 slots (p50 95.0 s vs 73.9 s): two concurrent requests
share one GPU, so each takes longer and the sum of per-call times grows while wall time does
not fall. With `OLLAMA_NUM_PARALLEL=2` on this machine (Apple M3 Max, one GPU, one resident
27B model) subtask parallelism gives no measured throughput gain. The pool stays (it bounds
concurrency and costs nothing measurable); the 1.02x is reported as measured, not as a
speedup claim.

## Status
Accepted (Phase 0). Implementation: Phase 6a.
