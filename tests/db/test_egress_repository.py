"""egress_events / honeytokens (alembic 0006) and ledger_entries.kind. `uv run pytest -m db`."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from bourse.config import get_settings
from bourse.contracts.trust import EgressEvent
from bourse.ledger.repo_sql import SqlLedgerRepository
from bourse.trust.honeytokens import Honeytoken
from bourse.trust.repo import SqlEgressRepository, SqlHoneytokenRepository
from tests.ledger_helpers import T0, row

pytestmark = pytest.mark.db

URL = get_settings().bourse_database_url
ROOT = Path(__file__).resolve().parents[2]
NEW = {"egress_events", "honeytokens"}
TRUNCATE = "TRUNCATE egress_events, honeytokens, ledger_entries RESTART IDENTITY"


def _alembic() -> Config:
    return Config(str(ROOT / "alembic.ini"))


def _tables() -> set[str]:
    engine = create_engine(URL)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _ledger_columns() -> set[str]:
    engine = create_engine(URL)
    try:
        return {c["name"] for c in inspect(engine).get_columns("ledger_entries")}
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


def _event(agent_id: str | None = "a@v.local") -> EgressEvent:
    return EgressEvent(
        src_ip="172.30.55.51",
        agent_id=agent_id,
        host="attacker.example",
        port=443,
        bytes=517,
        payload_sha256="0" * 64,
        honeytoken_hits=0,
        at=T0,
    )


def test_migration_0006_up_down_up(migrated: None) -> None:
    cfg = _alembic()
    assert _tables() >= NEW and "kind" in _ledger_columns()
    command.downgrade(cfg, "0005_ledger")
    assert not (NEW & _tables()) and "kind" not in _ledger_columns()
    assert {"ledger_entries", "tool_usage", "runs"} <= _tables()
    command.upgrade(cfg, "head")
    assert _tables() >= NEW and "kind" in _ledger_columns()


async def test_round_trips_and_ledger_kind_default(engine: AsyncEngine) -> None:
    egress = SqlEgressRepository(engine)
    await egress.add_event(_event())
    await egress.add_event(_event(None))
    assert [e.agent_id for e in await egress.list_events()] == ["a@v.local", None]
    assert len(await egress.list_events("a@v.local")) == 1
    tokens = SqlHoneytokenRepository(engine)
    token = Honeytoken(task_id="run-1:pii:1", card="4" * 16, email="ht@mail.example.org", at=T0)
    await tokens.add_token(token)
    assert await tokens.list_tokens() == [token]
    ledger = SqlLedgerRepository(engine)
    await ledger.add_row(row("a"))
    await ledger.add_row(row("a").model_copy(update={"kind": "canary"}))
    assert [r.kind for r in await ledger.list_rows()] == ["task", "canary"]


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE egress_events SET honeytoken_hits = 0",
        "DELETE FROM egress_events",
        "UPDATE honeytokens SET email = 'x'",
        "DELETE FROM honeytokens",
    ],
)
async def test_append_only(engine: AsyncEngine, statement: str) -> None:
    await SqlEgressRepository(engine).add_event(_event())
    await SqlHoneytokenRepository(engine).add_token(
        Honeytoken(task_id="t", card="4" * 16, email="e@mail.example.org", at=T0)
    )
    with pytest.raises(DBAPIError, match="append-only"):
        async with engine.begin() as conn:
            await conn.execute(text(statement))
