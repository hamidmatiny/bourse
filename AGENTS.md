# AGENTS.md — rules for any coding agent working on BOURSE

BOURSE is an A2A v1.0 multi-vendor agent exchange. Vendors publish JWS-signed A2A Agent Cards
(pinned vendor keys). Agents must pass a deterministic admission exam before listing. A LangGraph
broker decomposes a dataset-intake job into typed subtasks from a CLOSED skill taxonomy, selects
agents deterministically by expected utility (reliability lower bound − cost − latency), and
delegates over A2A; agents use MCP for their own tools. All agent LLM calls go through a metering
proxy. Trust is deterministic; untrusted card text never enters an LLM prompt. BOURSE-Bench reports
every metric as n/N on a frozen test split.

## Hard rules

1. Python 3.11, `uv` only. Always `uv run ...`. Never activate a venv. Never `pip install`.
2. Typed Pydantic v2 contracts in `src/bourse/contracts/` are the ONLY interface between components.
   All contract models: `model_config = ConfigDict(extra="forbid", frozen=True)`.
3. Every architectural decision gets an ADR in `docs/adr/NNNN-title.md` (Context/Decision/
   Consequences/Status).
4. One phase at a time. Each phase ends with acceptance commands the operator runs, then a commit
   with a summary message, then push. Never claim something works without real output.
5. Free models first. Ollama (host, http://127.0.0.1:11434) is primary. OmniRoute
   (http://127.0.0.1:20128, OpenAI-compatible, key in OMNIROUTE_API_KEY) is fallback/overflow.
   Paid providers only with `--allow-paid --usd-cap X`; default is hard-disabled.
6. Circuit breaker: any billing/auth/quota error (HTTP 401, 402, 403, 429 with quota/credit/billing
   wording, or bodies containing insufficient_quota / credit balance / billing) trips a GLOBAL
   breaker that stops the whole run immediately. Plain 5xx and timeouts do NOT trip it.
7. Hard limits from day one: per-call deadline that is a real wall-clock deadline around the whole
   call (anyio.fail_after / asyncio.timeout), NOT an httpx read timeout; max-token cap on every call;
   wall budget per run; watchdog that kills a run whose heartbeat goes stale.
8. Ollama 0.40.6: gemma4:12b returns 501 on format paths; qwen3.5:27b accepts format=json (200) but
   it does not enforce our schema. Structured output uses the SUBMIT-TOOL pattern for ALL models: the
   model must call a single tool `submit` whose JSON args are validated by a Pydantic model; on
   validation error, retry with the error message, max N attempts, then a typed failure. A startup
   capability probe records what the server supports (ADR-0019).
9. Local Qwen/Gemma calls always send `think: false`. At most 2 models resident
   (OLLAMA_MAX_LOADED_MODELS=2), combined resident size < 26 GB (probe verifies via /api/ps).
10. Safety comes from deterministic checks only. No LLM participates in any trust, quarantine, or
    grading decision. Untrusted Agent Card free text is never placed in any LLM prompt.
11. Benchmark integrity (later phases, but write the rule now): frozen test split with hash manifest;
    dev split for iteration only; `--final` only from a clean git worktree, writing a lock with commit
    SHA and dirty flag; every metric reported as n/N.
12. Long jobs run in background with a log file; never block a foreground command > 10 minutes;
    print progress lines with ETA.
13. Never commit `.env` or `data/`. gitleaks runs in CI; allowlist only proven false positives,
    each with a comment explaining why.
14. CI must pass on a clean Linux runner with no local services. Live tests are marked
    `@pytest.mark.live` and excluded by default.
15. Every claim carries a real number. No marketing language in docs or commit messages.
16. Concurrency: no Ray. Use an `Executor` protocol with explicit pools: LLM-bound asyncio semaphore
    (size = OLLAMA_NUM_PARALLEL), CPU-bound ProcessPoolExecutor. (Implemented in Phase 6; the
    protocol stub is Phase 0.)

## Phase plan

| Phase | Scope |
|---|---|
| 0 | Skeleton, contracts, runtime limits, LLM provider layer (submit-tool), probe, approval interface, CI |
| 1 | Card crypto (JCS + JWS) + registry |
| 2 | MCP servers + metering proxy + 5 honest agents (LangGraph, CrewAI, plain a2a-sdk) |
| 3 | Broker (planner, selector, A2A delegation) |
| 4 | Ledger + reliability + OTel |
| 5 | Trust runtime + adversaries A4–A8 + SlackApprover |
| 6 | Bench harness + Executor pools |
| 7 | `--final` run + RESULTS.md |

Do not implement anything from a later phase early.

## How to report

- The agent does not run acceptance commands and does not describe their results. It prints the
  exact commands; the operator runs them and pastes real output back.
- Report only what was written or observed: files added/changed, test count, open issues.
  Every number comes from real output (test counts, timings, token counts, n/N).
- If a step was skipped or a rule could not be followed, say so explicitly with the reason.
- Commit only after the operator confirms green output. Commit message: one summary line, then a
  body listing modules added/changed and the test count. No marketing language.
- If a command the agent needs (e.g. remote setup, login) fails, stop and print the exact command
  for the operator.
