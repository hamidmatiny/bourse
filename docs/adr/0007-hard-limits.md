# 0007 — Hard limits: wall-clock deadline, max tokens, run wall budget, watchdog

## Context
An httpx read timeout resets on every received byte, so a server that drips bytes never times out.
Local models can also run until the context window fills.

## Decision
- `runtime.deadline.deadline(seconds)` wraps the whole call in an anyio cancel scope and raises
  `DeadlineExceeded`. Provider HTTP clients have no read timeout (connect timeout 5 s only); the
  deadline is the call limit. Router and providers both apply it.
- Every call sends a max-token cap (`options.num_predict` for Ollama, `max_tokens` for
  OpenAI-compatible), clamped to `BudgetSpec.max_tokens_per_call`.
- `runtime.watchdog.Watchdog` is a daemon thread that calls `on_kill(reason)` once when the
  heartbeat is older than `heartbeat_timeout_s` or the run exceeds `wall_budget_s`.

## Consequences
- Unit test: a fake server dripping 1 byte / 0.5 s is cancelled by `deadline(1.0)` with measured
  elapsed in [1.0, 1.3] s.
- Long-running code must call `heartbeat()`.

## Status
Accepted (Phase 0).
