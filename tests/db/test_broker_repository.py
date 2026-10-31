"""Postgres tests for broker runs/delegations. Run with `uv run pytest -m db`."""

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from bourse.broker.planner import TEMPLATE_PLAN, to_dag
from bourse.broker.repo_sql import SqlRunRepository
from bourse.config import get_settings
from bourse.contracts.broker import DelegationRecord, DelegationState, RunRecord, RunStatus
from bourse.contracts.skills import SkillId

pytestmark = pytest.mark.db

URL = get_settings().bourse_database_url
ROOT = Path(__file__).resolve().parents[2]
TABLES = {"runs", "delegations"}
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


def _alembic() -> Config:
    return Config(str(ROOT / "alembic.ini"))


def _tables() -> set[str]:
    engine = create_engine(URL)
    try:
        return set(inspect(engine).get_table_names()) & (TABLES | {"llm_usage", "agents"})
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def migrated() -> Iterator[None]:
    command.upgrade(_alembic(), "head")
    yield


async def _truncate(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE delegations, runs RESTART IDENTITY CASCADE"))


@pytest.fixture
async def engine(migrated: None) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(URL)
    await _truncate(eng)
    yield eng
    await _truncate(eng)
    await eng.dispose()


def _run(run_id: str = "run-db-1") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        request_text="audit this dataset",
        plan=to_dag(run_id, TEMPLATE_PLAN),
        planner_used="template",
        status=RunStatus.RUNNING,
        started_at=NOW,
    )


def _delegation(run_id: str = "run-db-1", attempt: int = 1) -> DelegationRecord:
    return DelegationRecord(
        run_id=run_id,
        subtask_id="pii",
        skill=SkillId.PII_DETECT,
        agent_id="vendor-c-pii-fast@vendor-c.local",
        revision_digest="a" * 64,
        attempt=attempt,
        state=DelegationState.FAILED if attempt == 1 else DelegationState.COMPLETED,
        error_kind="tool_error" if attempt == 1 else None,
        wall_ms=12.5,
        at=NOW,
    )


def test_migration_0003_up_down_up(migrated: None) -> None:
    cfg = _alembic()
    assert _tables() >= TABLES
    command.downgrade(cfg, "0002_llm_usage")
    assert _tables() == {"llm_usage", "agents"}  # 0001/0002 tables untouched
    command.upgrade(cfg, "head")
    assert _tables() >= TABLES


async def test_run_and_delegations_round_trip(engine: AsyncEngine) -> None:
    repo = SqlRunRepository(engine)
    await repo.create_run(_run())
    await repo.add_delegation(_delegation(attempt=1))
    await repo.add_delegation(_delegation(attempt=2))
    await repo.finish_run("run-db-1", RunStatus.SUCCEEDED, NOW, 1234.0)
    run = await repo.get_run("run-db-1")
    assert run is not None
    assert (run.status, run.wall_ms, run.planner_used) == (RunStatus.SUCCEEDED, 1234.0, "template")
    assert run.plan == to_dag("run-db-1", TEMPLATE_PLAN)
    rows = await repo.list_delegations("run-db-1")
    assert rows == [_delegation(attempt=1), _delegation(attempt=2)]
    assert await repo.get_run("run-missing") is None


@pytest.mark.parametrize(
    "statement",
    ["UPDATE delegations SET state = 'COMPLETED'", "DELETE FROM delegations"],
)
async def test_delegations_are_append_only(engine: AsyncEngine, statement: str) -> None:
    repo = SqlRunRepository(engine)
    await repo.create_run(_run())
    await repo.add_delegation(_delegation())
    with pytest.raises(DBAPIError, match="append-only"):
        async with engine.begin() as conn:
            await conn.execute(text(statement))
    assert await repo.list_delegations("run-db-1") == [_delegation()]
