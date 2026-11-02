"""Postgres ledger repository (migration 0005; append-only triggers)."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    Identity,
    Integer,
    MetaData,
    Table,
    Text,
    insert,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncEngine

from bourse.contracts.broker import LedgerRow, SelectionRecord
from bourse.contracts.skills import SkillId

metadata = MetaData()

ledger_entries = Table(
    "ledger_entries",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("run_id", Text, nullable=False),
    Column("subtask_id", Text, nullable=False),
    Column("attempt", Integer, nullable=False),
    Column("agent_id", Text, nullable=False),
    Column("skill", Text, nullable=False),
    Column("revision_digest", Text, nullable=False),
    Column("agent_major", Integer, nullable=False),
    Column("state", Text, nullable=False),
    Column("error_kind", Text),
    Column("tokens_in", Integer, nullable=False),
    Column("tokens_out", Integer, nullable=False),
    Column("tool_calls", Integer, nullable=False),
    Column("shadow_usd", Float, nullable=False),
    Column("price_table_version", Text, nullable=False),
    Column("wall_ms", Float, nullable=False),
    Column("spot_check_passed", Boolean),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("kind", Text, nullable=False),
)

selection_decisions = Table(
    "selection_decisions",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("run_id", Text, nullable=False),
    Column("subtask_id", Text, nullable=False),
    Column("skill", Text, nullable=False),
    Column("candidates", JSONB, nullable=False),
    Column("chosen", Text),
    Column("reason", Text, nullable=False),
    Column("at", DateTime(timezone=True), nullable=False),
)


class SqlLedgerRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def add_row(self, row: LedgerRow) -> None:
        values = row.model_dump()
        values["skill"] = row.skill.value
        values["state"] = row.state.value
        async with self._engine.begin() as conn:
            await conn.execute(insert(ledger_entries).values(**values))

    async def list_rows(
        self, run_id: str | None = None, skill: SkillId | None = None
    ) -> list[LedgerRow]:
        stmt = select(ledger_entries).order_by(ledger_entries.c.id)
        if run_id is not None:
            stmt = stmt.where(ledger_entries.c.run_id == run_id)
        if skill is not None:
            stmt = stmt.where(ledger_entries.c.skill == skill.value)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
        return [LedgerRow.model_validate({k: v for k, v in r.items() if k != "id"}) for r in rows]

    async def add_selection(self, record: SelectionRecord) -> None:
        values = record.model_dump(mode="json")
        values["at"] = record.at
        async with self._engine.begin() as conn:
            await conn.execute(insert(selection_decisions).values(**values))

    async def list_selections(
        self, run_id: str | None = None, skill: SkillId | None = None
    ) -> list[SelectionRecord]:
        stmt = select(selection_decisions).order_by(selection_decisions.c.id)
        if run_id is not None:
            stmt = stmt.where(selection_decisions.c.run_id == run_id)
        if skill is not None:
            stmt = stmt.where(selection_decisions.c.skill == skill.value)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
        return [
            SelectionRecord.model_validate({k: v for k, v in r.items() if k != "id"}) for r in rows
        ]
