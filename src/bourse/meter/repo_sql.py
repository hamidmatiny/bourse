"""Postgres usage repository. llm_usage is append-only (trigger in migration 0002)."""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    Identity,
    Integer,
    MetaData,
    Table,
    Text,
    func,
    or_,
    select,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from bourse.contracts.metering import LlmUsageRecord

metadata = MetaData()

llm_usage = Table(
    "llm_usage",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("agent_id", Text, nullable=False, index=True),
    Column("task_id", Text, index=True),
    Column("provider", Text),
    Column("model", Text, nullable=False),
    Column("tokens_in", Integer, nullable=False),
    Column("tokens_out", Integer, nullable=False),
    Column("wall_ms", Float, nullable=False),
    Column("status", Integer, nullable=False),
    Column("error_kind", Text),
    Column("at", DateTime(timezone=True), nullable=False),
    CheckConstraint("tokens_in >= 0 AND tokens_out >= 0", name="ck_llm_usage_tokens"),
)


class SqlMeterRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def add_usage(self, record: LlmUsageRecord) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(llm_usage.insert().values(**record.model_dump()))

    async def list_usage(
        self, agent_id: str | None = None, task_id: str | None = None, run_id: str | None = None
    ) -> list[LlmUsageRecord]:
        stmt = select(llm_usage).order_by(llm_usage.c.id)
        if agent_id is not None:
            stmt = stmt.where(llm_usage.c.agent_id == agent_id)
        if task_id is not None:
            stmt = stmt.where(llm_usage.c.task_id == task_id)
        if run_id is not None:
            prefix = run_id + ":"
            stmt = stmt.where(
                or_(
                    llm_usage.c.task_id == run_id,
                    func.substr(llm_usage.c.task_id, 1, len(prefix)) == prefix,
                )
            )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
        return [
            LlmUsageRecord.model_validate({k: v for k, v in r.items() if k != "id"}) for r in rows
        ]
