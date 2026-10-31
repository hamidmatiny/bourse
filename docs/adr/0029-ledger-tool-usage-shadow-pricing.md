# 0029 — Ledger, measured tool usage, shadow pricing

## Context
Selection needs cost and reliability per agent. Agents are untrusted, so nothing they report
about their own usage can be used.

## Decision
- LLM tokens: `llm_usage`, written by the metering proxy (ADR-0017).
- Tool calls: `tool_usage(id, agent_id, task_id, tool, wall_ms, ok, at)` (alembic 0004,
  append-only), written by the MCP servers. An ASGI middleware in front of each tool server
  requires `X-Bourse-Agent-Key` (the agent's per-agent key, the same keys the proxy uses);
  unknown or missing key -> 401 and the tool is not called. For `tools/call` it writes the row
  before the response is released, so it exists when the agent's delegation returns. The MCP
  adapter sends the key, `X-Bourse-Task-Id` and `traceparent` on every connection. In-process
  test servers carry no headers and record nothing.
- `ledger_entries` (alembic 0005, append-only): one row per delegation attempt with run_id,
  subtask_id, attempt, agent_id, skill, revision_digest, agent_major, state, error_kind,
  tokens_in/out (sum of llm_usage by task_id), tool_calls (count of tool_usage by task_id),
  shadow_usd, price_table_version, wall_ms, spot_check_passed, at.
- Shadow prices: `config/prices.toml`, per model USD per 1M tokens in/out plus USD per tool call,
  with a `version` string stored on every row. The values are operator placeholders, not
  measurements, and nothing is billed. Unknown models are priced at the highest listed rates.
- `bourse ledger export` writes parquet through DuckDB after validating the frame with a
  pandera schema (column set, order, types, state/skill enums, digest pattern, non-negative
  numbers). This adds `pandas==3.1.0` (177 packages locked, 1 new, no version changes).

## Consequences
- Every ledger number traces to rows written by BOURSE components, never by agents.
- Changing prices needs a version bump; old rows keep the version they were priced with.

## Status
Accepted (Phase 4).
