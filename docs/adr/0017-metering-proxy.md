# 0017 — Metering proxy: OpenAI facade, Ollama-native upstream, enforced limits, measured usage

## Context
Agents from different frameworks (LangGraph, CrewAI, plain a2a-sdk) speak the OpenAI chat API.
Limits and accounting must not depend on agent cooperation.

## Decision
- `bourse meter serve` (127.0.0.1, default port 55200) exposes `POST /v1/chat/completions`.
  Upstream is the Phase 0 `llm.router`: Ollama native `/api/chat` first, OmniRoute fallback when
  `OMNIROUTE_MODEL` is set, paid never (Phase 2a).
- Enforced by the proxy:
  - Auth: `Authorization: Bearer <key>`; keys map to agent ids via `BOURSE_METER_KEYS`
    (`agent_id:key,...`, keys ≥ 16 chars, only in `.env`). Unknown key → 401, no usage row.
  - Model allowlist: `BOURSE_MODEL_LARGE`, `BOURSE_MODEL_SMALL`; anything else → 400.
  - `think: false` on every Ollama call; `max_tokens = min(requested, max_tokens_per_call)`.
  - `response_format` other than `{"type": "text"}` → 400 "use submit-tool pattern (ADR-0006)".
  - Global circuit breaker: billing/auth/quota upstream errors → 503 now and on every later call
    with no upstream contact. Deadline (default 120 s) → 504. Fallback exhausted → 502.
- `X-Bourse-Task-Id` is optional now (stored as null when absent) and becomes required in
  Phase 3.
- Translation: OpenAI messages (system/developer/user/assistant/tool, text parts), `tools`,
  assistant `tool_calls` and tool results ↔ Ollama native (`tool_calls[].function.arguments` as
  objects, tool turns carry `tool_name` resolved from the matching `tool_call_id`). Ignored:
  `temperature`, `top_p`, `tool_choice`, and other sampling fields. `n` must be 1.
- `stream: true`: the upstream call is non-streaming; the proxy returns the whole completion
  as one `chat.completion.chunk` SSE event followed by `data: [DONE]`.
- Usage is measured from the upstream response (Ollama `prompt_eval_count`/`eval_count`,
  OpenAI `usage` for OmniRoute), never from the client. One `llm_usage` row per authenticated
  request: agent_id, task_id, provider, model, tokens_in, tokens_out, wall_ms, status,
  error_kind, at. The table is append-only (trigger, migration 0002). Repository protocol with
  in-memory (unit) and SQL (runtime, db tests) implementations.
- Each request gets its own budget ledger; per-agent run budgets come with the Phase 4 ledger.

## Consequences
- Streaming clients see no token-by-token output.
- Sampling parameters sent by agents have no effect.

## Status
Accepted (Phase 2a).
