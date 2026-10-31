# Architecture decision records

Each record has Context, Decision, Consequences and Status. Process: [0001](0001-adr-process-and-agents-md.md).

| ADR | Decision | Phase |
| [0001](0001-adr-process-and-agents-md.md) | ADR process; AGENTS.md is the source of rules | 0 |
| [0002](0002-a2a-v1.md) | A2A v1.0, a2a-sdk 1.2.3, JSON-RPC binding first | 0 |
| [0003](0003-mcp-2026-07-28.md) | MCP 2026-07-28 via mcp 2.x; in-house LangChain tool adapter | 0 |
| [0004](0004-contracts-and-closed-taxonomy.md) | Pydantic contracts as the sole interface; closed skill taxonomy | 0 |
| [0005](0005-free-model-first-budget-breaker.md) | Free-model-first routing, budget ledger, global circuit breaker | 0 |
| [0006](0006-submit-tool-structured-output.md) | Submit-tool structured output; startup capability probe | 0 |
| [0007](0007-hard-limits.md) | Hard limits: wall-clock deadline, max tokens, run wall budget, watchdog | 0 |
| [0008](0008-deterministic-safety.md) | Deterministic safety; no LLM in trust or grading; card text never in prompts | 0 |
| [0009](0009-no-ray-executor.md) | No Ray; Executor protocol with explicit pools | 0 |
| [0010](0010-human-approval.md) | Human approval: Approver protocol, Slack for 4 triggers, deny by default | 0 |
| [0011](0011-benchmark-integrity.md) | Benchmark integrity | 0 |
| [0012](0012-card-signing.md) | Agent Card signing: JCS + detached JWS, pinned JWKS only | 1 |
| [0013](0013-registry-model.md) | Registry model: immutable revisions, semver rules, rejection taxonomy, state machine | 1 |
| [0014](0014-repository-protocol.md) | Repository protocol; in-memory for unit tests, Postgres for db tests and runtime | 1 |
| [0015](0015-skill-io-and-units.md) | Skill I/O contracts and canonical units | 2a |
| [0016](0016-stateless-mcp-tool-servers.md) | Stateless pure MCP tool servers, caps, deadlines | 2a |
| [0017](0017-metering-proxy.md) | Metering proxy: OpenAI facade, Ollama-native upstream, enforced limits, measured usage | 2a |
| [0018](0018-mcp-langchain-adapter.md) | MCP → LangChain adapter with allowlist and tool budgets | 2a |
| [0019](0019-format-json-correction.md) | Correction: format=json support differs by model; submit-tool for all models | 2b |
| [0020](0020-sampling-determinism-memory.md) | Sampling set by the proxy; num_ctx and memory headroom | 2b |
| [0021](0021-agent-packaging.md) | Agent packaging, vendor mapping, frameworks, CrewAI isolation | 2b |
| [0022](0022-a2a-task-contract.md) | A2A request/artifact contract; REJECTED vs FAILED | 2b |
| [0023](0023-graders.md) | Deterministic graders and pass thresholds | 2b |
| [0024](0024-planner.md) | Planner: submit-tool over the closed taxonomy, template fallback, no card text in prompts | 3 |
| [0025](0025-selection-policy.md) | Selection policy interface and Phase 3 priors | 3 |
| [0026](0026-delegation-protocol.md) | Delegation protocol: card-drift check, attempts, fallbacks, contract validation | 3 |
| [0027](0027-run-persistence-checkpointing.md) | Run persistence and checkpointing | 3 |
| [0028](0028-manual-activation.md) | Manual activation before the admission exam (temporary) | 3 |
| [0029](0029-ledger-tool-usage-shadow-pricing.md) | Ledger, measured tool usage, shadow pricing | 4 |
| [0030](0030-reliability-from-spot-checks.md) | Reliability from runtime spot checks, never from graders | 4 |
| [0031](0031-measured-policy-exploration.md) | MeasuredPolicy and exploration quota | 4 |
| [0032](0032-otel-propagation.md) | OpenTelemetry propagation | 4 |
| [0033](0033-memory-guard-num-ctx.md) | Memory guard and num_ctx decision | 4 |
