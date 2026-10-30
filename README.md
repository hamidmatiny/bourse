# BOURSE

BOURSE is an A2A v1.0 exchange where agents from multiple vendors publish JWS-signed Agent Cards,
pass a deterministic admission exam, and are selected by a LangGraph broker to run typed subtasks of
a dataset-intake job (closed taxonomy of 5 skills), with every LLM call metered, every trust
decision made by deterministic checks, and every benchmark metric reported as n/N on a frozen split.

**Status: Phase 2a** — Phases 0–1 (contracts, runtime limits, LLM layer, probe, approval
interface, signed Agent Cards, registry) plus typed per-skill I/O contracts, three stateless MCP
tool servers (table/DuckDB, PII, units), an allowlisted MCP→LangChain adapter with per-agent
tool budgets, and a metering LLM proxy (OpenAI facade over Ollama, upstream-measured usage in
Postgres). No agents or broker yet. Rules and phase plan: [AGENTS.md](AGENTS.md). Decisions:
[docs/adr/](docs/adr/).

## Acceptance commands (Phase 2a)

Prerequisites: `uv`, Docker, `gitleaks`, `cp .env.example .env`. Live tests need host Ollama with
`OLLAMA_MAX_LOADED_MODELS=2 OLLAMA_NUM_PARALLEL=2`.

```sh
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict src tests
uv run pytest                       # unit tests; live and db excluded
docker compose up -d postgres
uv run alembic upgrade head && uv run alembic downgrade base && uv run alembic upgrade head
uv run pytest -m db                 # Postgres integration tests
uv run pytest -m live -q -rA        # host Ollama
gitleaks detect --no-banner --redact
git status --short
```

Services: `bourse mcp serve {table|pii|units}` (ports 55101–55103), `bourse meter serve`
(port 55200), `bourse registry serve` (port 8700). All bind 127.0.0.1.
