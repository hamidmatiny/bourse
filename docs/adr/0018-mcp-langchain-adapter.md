# 0018 — MCP → LangChain adapter with allowlist and tool budgets

## Context
`langchain-mcp-adapters` does not support mcp 2.x (ADR-0003). Agents must only reach the tools
they are configured for, with bounded cost.

## Decision
- `bourse.mcp_adapter.mcp_tools_as_langchain(server, allowlist, deadline_s, max_calls, *,
  budget, on_call, client_factory)` uses `mcp.Client` directly (URL or in-process server) and
  returns one `StructuredTool` per allowlisted tool, with the tool's JSON input schema.
- Allowlist: only listed names are exposed; a name the server does not list raises
  `ToolNotAllowed` at construction.
- Per call: a `ToolBudget` shared per agent (`ToolBudgetExceeded` before any server contact once
  spent) → wall-clock deadline around connect + call (`DeadlineExceeded`) → `is_error` results
  raise `ToolCallFailed` → structured content is validated against the tool's output schema with
  jsonschema Draft 2020-12 (`ToolResultInvalid`; mcp's own client-side check is mapped to the
  same error) → a `ToolCallRecord(tool, wall_ms, ok, error_kind)` goes to `on_call` (persisted in
  Phase 4).
- Servers are stateless (ADR-0016), so each call opens its own client connection.

## Consequences
- One connection per call costs a discovery round trip; acceptable at Phase 2 call volumes.
- Pagination of `tools/list` is not followed; the three servers expose ≤ 3 tools each.

## Status
Accepted (Phase 2a).
