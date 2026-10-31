"""llm_usage migration and append-only checks. Run with `uv run pytest -m db`."""

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from bourse.config import get_settings
from bourse.contracts.metering import LlmUsageRecord
from bourse.meter.repo_sql import SqlMeterRepository

pytestmark = pytest.mark.db

URL = get_settings().bourse_database_url
ROOT = Path(__file__).resolve().parents[2]


def _alembic() -> Config:
    return Config(str(ROOT / "alembic.ini"))


def _has_table(name: str) -> bool:
    engine = create_engine(URL)
    try:
        return name in inspect(engine).get_table_names()
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def migrated() -> Iterator[None]:
    command.upgrade(_alembic(), "head")
    yield


@pytest.fixture
async def repo(migrated: None) -> AsyncIterator[SqlMeterRepository]:
    engine = create_async_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE llm_usage RESTART IDENTITY"))
    yield SqlMeterRepository(engine)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE llm_usage RESTART IDENTITY"))
    await engine.dispose()


def test_llm_usage_migration_up_down_up(migrated: None) -> None:
    cfg = _alembic()
    assert _has_table("llm_usage")
    command.downgrade(cfg, "0001_registry")
    assert not _has_table("llm_usage")
    assert _has_table("agents")  # 0001 tables untouched
    command.upgrade(cfg, "head")
    assert _has_table("llm_usage")


async def test_usage_rows_append_only(repo: SqlMeterRepository) -> None:
    row = LlmUsageRecord(
        agent_id="agent-a",
        task_id=None,
        provider="ollama",
        model="gemma4:12b",
        tokens_in=123,
        tokens_out=45,
        wall_ms=12.5,
        status=200,
        error_kind=None,
        at=datetime.now(UTC),
    )
    await repo.add_usage(row)
    assert await repo.list_usage("agent-a") == [row]
    engine = create_async_engine(URL)
    try:
        for sql in ("UPDATE llm_usage SET tokens_in = 0", "DELETE FROM llm_usage"):
            with pytest.raises(DBAPIError, match="append-only"):
                async with engine.begin() as conn:
                    await conn.execute(text(sql))
    finally:
        await engine.dispose()
    assert await repo.list_usage() == [row]
