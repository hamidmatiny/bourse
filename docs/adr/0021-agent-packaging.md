# 0021 — Agent packaging, vendor mapping, frameworks, CrewAI isolation

## Context
BOURSE needs honest agents from several vendors on different frameworks to exercise A2A
interoperability, signed cards and metering.

## Decision
- Five agents, each its own A2A server process (`bourse agent serve <name>`, 127.0.0.1):

  | agent | vendor | framework | skills | model | MCP allowlist | port |
  |---|---|---|---|---|---|---|
  | profiler | vendor-a.local | LangGraph | schema.profile, stats.compute | gemma4:12b | table.* | 55401 |
  | pii_precise | vendor-b.local | CrewAI | pii.detect | qwen3.5:27b | pii.*, table.sample_rows | 55402 |
  | pii_fast | vendor-c.local | a2a-sdk | pii.detect | gemma4:12b | pii.* | 55403 |
  | normalizer | vendor-b.local | a2a-sdk | units.normalize | gemma4:12b | units.* | 55404 |
  | reporter | vendor-a.local | LangGraph | report.compose | qwen3.5:27b | none | 55405 |

- Shared base (`bourse.agents.common`): one `SkillExecutor`; LLM only through the metering
  proxy with a per-agent key and `X-Bourse-Task-Id`; structured answers via submit-tool; MCP
  tools via the ADR-0018 adapter with a per-task `ToolBudget`; whole task under
  `runtime.deadline`. Numbers in outputs come from tools or from the input, never from LLM text.
- Cards are built from the spec, signed with the vendor's newest key in `data/keys/<vendor>/`
  (Phase 1 card_sign, jku = the vendor's pinned JWKS URL), served at
  `/.well-known/agent-card.json`. Locally, interface URLs are on 127.0.0.1, accepted by the
  registry through `BOURSE_DEV_HOSTS`.
- `bourse stack up` starts 3 MCP servers, the meter (55200), the registry (55300) and the
  agents as subprocesses (logs `data/logs/`, pid files and per-run secrets in `data/run/`).
- CrewAI 1.6.1 (pinned; no core pin changed when it was added; `rich` moved 15.0.0 → 14.3.4):
  - On import it sends a request to api.scarf.sh unless telemetry is disabled; on first run
    it prompts on stdin; it writes state under `~`. The agent sets `CREWAI_DISABLE_TELEMETRY`,
    `CREWAI_DISABLE_TRACKING`, `CREWAI_TRACING_ENABLED=false`, `CREWAI_TESTING=true` (suppresses
    the stdin prompt), `OTEL_SDK_DISABLED=true` (this process only), `ANONYMIZED_TELEMETRY=False`
    before importing crewai, and the launcher runs it with `HOME=data/agent_home/pii_precise`.
  - Its OpenAI client uses httpx2 (not interceptable by respx); tests inject an httpx2 mock
    transport and a socket guard blocks every non-loopback connect in agent tests.
  - CrewAI agents call tools through ReAct text, not native function calling; the submit tool is
    a CrewAI `BaseTool` whose arguments are validated by a Pydantic model; up to 2 crew runs.

## Consequences
- OpenTelemetry spans (Phase 4) will not cover the CrewAI agent process.
- Agents run unattended; a crashed agent shows as unhealthy in `bourse stack status`.
- crewai 1.6.1 adds a `security_context` argument before validating tool input against
  `args_schema`, so a strict (extra=forbid) schema rejects every call. The CrewAI submit tool
  uses a permissive schema, drops `security_context`, and validates the captured arguments
  against the strict NameColumns model afterwards. First smoke run before this fix: pii_precise
  failed 3/3.
- crewai raises ValueError when the LLM reply is empty. A kickoff that raised after a valid
  submit keeps that submit; otherwise it counts as one of the MAX_KICKOFFS=2 attempts.
- gemma4:12b on Ollama 0.40.6 can open a thinking channel with `think: false` on ambiguous
  input. Ollama strips it, which leaves content empty and no tool call (orders.csv, normalizer:
  2 seeds, 2/2 empty). The normalizer prompt adds "If you are not sure, use null."; in the same
  replay that gave 6/6 tool calls (3 datasets x 2 seeds).

## Status
Accepted (Phase 2b).
