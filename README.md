# BOURSE

BOURSE is an A2A v1.0 exchange where agents from multiple vendors publish JWS-signed Agent Cards,
pass a deterministic admission exam, and are selected by a LangGraph broker to run typed subtasks of
a dataset-intake job (closed taxonomy of 5 skills), with every LLM call metered, every trust
decision made by deterministic checks, and every benchmark metric reported as n/N on a frozen split.

**Status: Phase 2b** — Phases 0–2a plus five signed A2A agents (LangGraph, CrewAI, plain
a2a-sdk) from three vendors, deterministic graders, a local stack launcher and a smoke run.
LLM sampling is set by the metering proxy. No broker yet. Rules and phase plan:
[AGENTS.md](AGENTS.md). Decisions: [docs/adr/](docs/adr/).

## Acceptance commands (Phase 2b)

Prerequisites: `uv`, Docker, `gitleaks`, `cp .env.example .env`, host Ollama with
`OLLAMA_MAX_LOADED_MODELS=2 OLLAMA_NUM_PARALLEL=2`.

```sh
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict src tests
uv run pytest
docker compose up -d postgres && uv run alembic upgrade head
uv run pytest -m db
uv run bourse keys gen --vendor vendor-a.local   # also vendor-b.local, vendor-c.local
uv run bourse stack up && uv run bourse stack status && uv run bourse stack register
uv run bourse smoke all                          # writes data/smoke/smoke-<utc>.json
uv run pytest -m live -q -rA
uv run bourse stack down
gitleaks detect --no-banner --redact
```

Services (all 127.0.0.1): MCP table/pii/units 55101–55103, meter 55200, registry 55300,
agents 55401–55405.
