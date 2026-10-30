# BOURSE

BOURSE is an A2A v1.0 exchange where agents from multiple vendors publish JWS-signed Agent Cards,
pass a deterministic admission exam, and are selected by a LangGraph broker to run typed subtasks of
a dataset-intake job (closed taxonomy of 5 skills), with every LLM call metered, every trust
decision made by deterministic checks, and every benchmark metric reported as n/N on a frozen split.

**Status: Phase 1** — Phase 0 (contracts, runtime limits, LLM layer, probe, approval
interface, CI) plus Agent Card signing (RFC 8785 JCS + RFC 7515 detached JWS, ES256/EdDSA,
pinned vendor JWKS only) and a registry (FastAPI + Postgres) with immutable card revisions,
semver rollback/reuse checks, a rejection taxonomy, and the trust state machine. No broker or
agents yet. Rules and phase plan: [AGENTS.md](AGENTS.md). Decisions: [docs/adr/](docs/adr/).

## Acceptance commands (Phase 1)

Prerequisites: `uv`, Docker, and `gitleaks`; `cp .env.example .env`.

```sh
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict src tests
uv run pytest                       # unit tests; live and db excluded
docker compose up -d postgres
uv run alembic upgrade head && uv run alembic downgrade base && uv run alembic upgrade head
uv run pytest -m db                 # Postgres integration tests
uv run bourse attacks phase1        # A1/A1b/A2/A3/A9 + honest controls, n/N
uv run bourse keys gen --vendor vendor-a.local   # private key -> data/keys/ (gitignored)
gitleaks detect --no-banner --redact
git status --short
```

Phase 0 live checks (host Ollama): `uv run bourse probe`, `uv run pytest -m live -q`.
