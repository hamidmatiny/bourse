# 0006 — Submit-tool structured output; startup capability probe

## Context
Operator observation: Ollama 0.40.6 returns HTTP 501 for every structured-output `format` path,
so JSON-schema-constrained decoding is unavailable. Tool calling works.

## Decision
- Structured output uses one tool named `submit` whose parameters are the target Pydantic model's
  JSON schema. Arguments are validated with that model. On a validation error or a missing call,
  the error text is appended as a user message and the call is retried, up to `max_attempts`
  (default 3); then `SubmitFailed(attempts, last_error)` is raised.
- `bourse probe` records server version, installed models, the HTTP status of a `format: "json"`
  request, whether a `submit` tool call succeeds with `think: false`, resident bytes from `/api/ps`
  and latency, and writes `data/probe/probe-<utc>.json`.

## Consequences
- No code path sends `format`. If a later Ollama supports it, switching needs a new ADR and a probe
  result showing status 200.

## Status
Accepted (Phase 0).
