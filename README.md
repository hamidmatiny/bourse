# BOURSE

BOURSE is an A2A v1.0 exchange where agents from multiple vendors publish JWS-signed Agent Cards,
pass a deterministic admission exam, and are selected by a LangGraph broker to run typed subtasks of
a dataset-intake job (closed taxonomy of 5 skills), with every LLM call metered, every trust
decision made by deterministic checks, and every benchmark metric reported as n/N on a frozen split.

**Status: Phase 0** — repo skeleton, typed contracts, runtime limits (deadline, budget, circuit
breaker, watchdog), Ollama/OmniRoute provider layer with submit-tool structured output, Ollama
capability probe, approval interface, docker compose, CI. No broker, agents, or registry yet.
Rules and phase plan: [AGENTS.md](AGENTS.md). Decisions: [docs/adr/](docs/adr/).

## Acceptance commands (Phase 0)

Prerequisites: `uv`, Docker, Ollama on the host at `http://127.0.0.1:11434` with the models named
in `.env` (`cp .env.example .env`), and `gitleaks`.

```sh
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict src tests
uv run pytest                 # unit tests; live tests excluded
uv run bourse doctor
docker compose up -d && docker compose ps
uv run bourse probe           # writes data/probe/probe-<utc>.json
uv run pytest -m live -q      # needs host Ollama
gitleaks detect --no-banner --redact
git status --short
```
