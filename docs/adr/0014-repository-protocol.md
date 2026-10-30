# 0014 — Repository protocol; in-memory for unit tests, Postgres for db tests and runtime

## Context
Registry logic must be unit-testable without a database, and CI's default job has no services
(rule 14).

## Decision
- `bourse.registry.repo.Repository` protocol. Writes that must be atomic are single methods:
  `save_registration(agent, revision)` and `save_transition(agent, transition)`.
- `InMemoryRepository` for unit tests and `bourse attacks phase1`.
- `SqlRepository`: SQLAlchemy 2 async + psycopg 3 against Postgres 16. Tables `vendors`, `agents`,
  `card_revisions` (unique agent_id+version), `violations`, `state_transitions`. `card_revisions`
  and `state_transitions` have a trigger that rejects UPDATE and DELETE.
- Search: `skills` text array with GIN index (containment), Postgres FTS (`simple` config) over a
  stored `search_text` = agent name + skill tags only. Descriptions are not indexed.
- Schema managed by Alembic (`alembic/`, URL from `BOURSE_DATABASE_URL`).
- Tests marked `@pytest.mark.db` run only in the CI `db` job (postgres:16 service container) and
  locally against docker compose; default `pytest` excludes them.

## Consequences
- The in-memory search tokenizes on `[a-z0-9]+`; Postgres uses the `simple` parser. Results can
  differ on punctuation-heavy queries; the db tests cover the Postgres behaviour.

## Status
Accepted (Phase 1).
