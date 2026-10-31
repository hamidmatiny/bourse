# 0032 — OpenTelemetry propagation

## Context
A run crosses processes: broker, agents, metering proxy, MCP servers, Ollama. Debugging needs
one trace per run.

## Decision
- W3C `traceparent` via the default OTel propagator. Outgoing: an httpx request hook on the
  broker, agent and meter clients; the MCP adapter adds `trace_headers()` per connection.
  Incoming: an ASGI middleware opens one SERVER span per POST ("agent task" on agents,
  "llm call" on the meter); the MCP tool-usage middleware opens "tool call".
- Broker spans: "run" (root), "plan", "subtask", "delegation attempt". Attributes: run_id,
  subtask_id, agent_id, skill, attempt, outcome, tokens_in/out, shadow_usd (delegation
  attempt, from the ledger row); task_id and tokens on llm call; tool and outcome on tool call.
- Export: OTLP gRPC to the compose collector (`OTEL_EXPORTER_OTLP_ENDPOINT`, default
  127.0.0.1:54317) -> Jaeger (UI 127.0.0.1:56686). The CrewAI process keeps
  `OTEL_SDK_DISABLED=true` (ADR-0021): it emits no spans, but the incoming parent is passed on to
  the meter and MCP calls, so those spans stay in the run's trace.
- The trace id is returned in `IntakeReport.trace_id` and printed per run.

## Consequences
- Unit test with an in-memory exporter across ASGI apps: one run gives one trace id;
  plan and 5 subtask spans are children of run; each delegation attempt is a child of a
  subtask; each of the 5 agent task spans is a child of a delegation attempt. A second test
  checks that a tool call span is a child of the calling span.
- Ollama ignores the header; the trace ends at the meter's upstream call.

## Status
Accepted (Phase 4).
