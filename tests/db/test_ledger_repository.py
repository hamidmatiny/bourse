"""Postgres tests for tool_usage, ledger_entries, selection_decisions. `uv run pytest -m db`."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from bourse.broker.select import PriorConfig, PriorPolicy
from bourse.config import get_settings
from bourse.contracts.broker import DelegationState, SelectionRecord
from bourse.contracts.metering import ToolUsageRecord
from bourse.contracts.skills import SkillId
from bourse.contracts.tasks import Subtask
from bourse.ledger.repo_sql import SqlLedgerRepository
from bourse.mcp_servers.usage import SqlToolUsageRepository
from tests.ledger_helpers import T0, row

pytestmark = pytest.mark.db

URL = get_settings().bourse_database_url
ROOT = Path(__file__).resolve().parents[2]
NEW = {"tool_usage", "ledger_entries", "selection_decisions"}
TRUNCATE = "TRUNCATE tool_usage, ledger_entries, selection_decisions RESTART IDENTITY"


def _alembic() -> Config:
    return Config(str(ROOT / "alembic.ini"))


def _tables() -> set[str]:
    engine = create_engine(URL)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def migrated() -> Iterator[None]:
    command.upgrade(_alembic(), "head")
    yield


@pytest.fixture
async def engine(migrated: None) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(URL)
    async with eng.begin() as conn:
        await conn.execute(text(TRUNCATE))
    yield eng
    async with eng.begin() as conn:
        await conn.execute(text(TRUNCATE))
    await eng.dispose()


def test_migrations_0004_0005_up_down_up(migrated: None) -> None:
    cfg = _alembic()
    assert _tables() >= NEW
    command.downgrade(cfg, "0004_tool_usage")
    assert "tool_usage" in _tables()
    assert not ({"ledger_entries", "selection_decisions"} & _tables())
    command.downgrade(cfg, "0003_broker_runs")
    assert not (NEW & _tables())
    assert {"runs", "delegations", "llm_usage", "agents"} <= _tables()
    command.upgrade(cfg, "head")
    assert _tables() >= NEW


async def test_tool_usage_round_trip_and_run_prefix(engine: AsyncEngine) -> None:
    repo = SqlToolUsageRepository(engine)
    for task in ("run-1:pii:1", "run-1:pii:1", "run-10:pii:1"):
        await repo.add_tool_usage(
            ToolUsageRecord(
                agent_id="a", task_id=task, tool="pii.luhn_check", wall_ms=1.5, ok=True, at=T0
            )
        )
    assert len(await repo.list_tool_usage(task_id="run-1:pii:1")) == 2
    assert len(await repo.list_tool_usage(run_id="run-1")) == 2


async def test_ledger_and_selection_round_trip(engine: AsyncEngine) -> None:
    repo = SqlLedgerRepository(engine)
    rows = [row("a", i=0), row("b", state=DelegationState.FAILED_SPOTCHECK, i=1)]
    for r in rows:
        await repo.add_row(r)
    assert await repo.list_rows(run_id="run-h") == rows
    assert await repo.list_rows(skill=SkillId.STATS_COMPUTE) == []
    decision = PriorPolicy(PriorConfig()).choose(
        Subtask(id="pii", skill=SkillId.PII_DETECT, input_ref="dataset"), []
    )
    record = SelectionRecord(
        run_id="run-h",
        subtask_id="pii",
        skill=SkillId.PII_DETECT,
        candidates=decision.candidates,
        chosen=None,
        reason=decision.reason,
        at=T0,
    )
    await repo.add_selection(record)
    assert await repo.list_selections(skill=SkillId.PII_DETECT) == [record]


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE tool_usage SET ok = false",
        "DELETE FROM tool_usage",
        "UPDATE ledger_entries SET state = 'COMPLETED'",
        "DELETE FROM ledger_entries",
        "UPDATE selection_decisions SET reason = 'x'",
        "DELETE FROM selection_decisions",
    ],
)
async def test_append_only(engine: AsyncEngine, statement: str) -> None:
    await SqlToolUsageRepository(engine).add_tool_usage(
        ToolUsageRecord(
            agent_id="a", task_id="t", tool="pii.luhn_check", wall_ms=1.0, ok=True, at=T0
        )
    )
    ledger = SqlLedgerRepository(engine)
    await ledger.add_row(row("a"))
    await ledger.add_selection(
        SelectionRecord(
            run_id="r",
            subtask_id="s",
            skill=SkillId.PII_DETECT,
            candidates=(),
            chosen=None,
            reason="x",
            at=T0,
        )
    )
    with pytest.raises(DBAPIError, match="append-only"):
        async with engine.begin() as conn:
            await conn.execute(text(statement))
