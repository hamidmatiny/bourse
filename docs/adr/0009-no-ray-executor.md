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

## Status
Accepted (Phase 0). Implementation: Phase 6.
