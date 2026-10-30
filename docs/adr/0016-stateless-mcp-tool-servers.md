# 0016 — Stateless pure MCP tool servers, caps, deadlines

## Context
Agents use MCP for their own tools (ADR-0003). Shared server-side state between calls would make
results depend on call order and would leak data between agents.

## Decision
- Three servers built on `mcp==2.3.0` `MCPServer`, protocol 2026-07-28:
  - `table`: `profile_columns`, `column_stats`, `sample_rows` (n ≤ 20). Each call parses its own
    `csv_text` with Python's csv module into a fresh in-memory DuckDB (`duckdb==2.0.0`).
  - `pii`: `regex_scan` (whole-value match; EMAIL, PHONE, CARD 13–19 digits, SIN 9 digits),
    `luhn_check`, `sin_check` (9 digits, Luhn-valid, first digit not 0 or 8). No LLM.
  - `units`: `parse_header_unit` (suffix after the last `_`, case-insensitive aliases) and
    `convert` (own table, affine temperature, no pint).
- Tools are pure: all input per call, no session state. Pydantic-typed inputs and outputs (MCP
  output schemas come from the return models).
- Limits per call: 256 KiB of input text (`ToolError` above it), 10 s wall-clock deadline
  (`runtime.deadline`; blocking work runs in a worker thread that is abandoned on timeout), HTTP
  body cap 1 MiB.
- Serving: `bourse mcp serve {table|pii|units}` → streamable HTTP, `stateless_http=True`,
  `json_response=True`, bound to 127.0.0.1, default ports 55101/55102/55103. Tests use the
  in-process path (`mcp.Client(server)`).

## Consequences
- A deadline cannot stop a running DuckDB query; the thread is abandoned and the caller gets a
  tool error at the deadline.
- `regex_scan` does not find PII embedded in free text; PERSON_NAME is not detected by these tools.

## Status
Accepted (Phase 2a).
