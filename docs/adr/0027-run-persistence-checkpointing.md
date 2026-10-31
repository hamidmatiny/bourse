# 0027 — Run persistence and checkpointing

## Context
Runs must be auditable after the fact and resumable after a crash without delegating completed
work twice.

## Decision
- Alembic 0003: `runs(run_id, request_text, plan jsonb, planner_used, status, started_at,
  ended_at, wall_ms)` and `delegations(run_id, subtask_id, skill, agent_id, revision_digest,
  attempt, state, error_kind, wall_ms, at)`; `delegations` is append-only through the
  `bourse_reject_mutation()` trigger from 0001.
- Graph: LangGraph StateGraph `plan -> schedule -> delegate (one dependency level per step) ->
  assemble -> persist`, compiled with a checkpointer, `thread_id = run_id`. State values are
  JSON only.
- Checkpointer: `langgraph-checkpoint-postgres==3.2.0` (AsyncPostgresSaver). Adding it resolved
  176 packages with 2 new ones (langgraph-checkpoint-postgres 3.2.0, psycopg-pool 3.3.3) and no
  version change to any existing package, so the SQLite fallback was not needed. Its tables are
  created by `saver.setup()` and tracked by LangGraph's own migration table, not by Alembic.
  Unit tests use LangGraph's InMemorySaver.
- Resume (`bourse runs resume <run_id>`): invoke the graph with no input on the same thread.
  The checkpoint after level k holds every result of levels <= k, so those subtasks are not
  delegated again. If the plan node is re-run after the run row was written, the stored plan is
  reused instead of a second LLM call.
- `IntakeReport` totals: tokens from `llm_usage` rows whose task_id is run_id (planner) or starts
  with `run_id:` (delegations); wall_ms from the runs row.

## Consequences
- A kill in the middle of a level re-runs that level. Attempt numbers continue from the
  subtask's existing delegation rows, so a task_id is never reused; earlier failed attempts
  count toward the limit of 2 and their agents are skipped. A subtask that
  completed in the killed level but was not checkpointed is delegated again (its output was not
  saved). The unit test covers a kill after level 0: each level-0 subtask has exactly 1
  delegation row after resume.
- `alembic downgrade base` does not drop the LangGraph checkpoint tables.

## Status
Accepted (Phase 3).
