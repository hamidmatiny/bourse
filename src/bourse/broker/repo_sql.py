"""Postgres run repository. delegations is append-only (trigger in migration 0003)."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
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
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncEngine

from bourse.contracts.broker import DelegationRecord, RunRecord, RunStatus

metadata = MetaData()

runs = Table(
    "runs",
    metadata,
    Column("run_id", Text, primary_key=True),
    Column("request_text", Text, nullable=False),
    Column("plan", JSONB, nullable=False),
    Column("planner_used", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("ended_at", DateTime(timezone=True)),
    Column("wall_ms", Float),
)

delegations = Table(
    "delegations",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("run_id", Text, nullable=False),
    Column("subtask_id", Text, nullable=False),
    Column("skill", Text, nullable=False),
    Column("agent_id", Text, nullable=False),
    Column("revision_digest", Text, nullable=False),
    Column("attempt", Integer, nullable=False),
    Column("state", Text, nullable=False),
    Column("error_kind", Text),
    Column("wall_ms", Float, nullable=False),
    Column("at", DateTime(timezone=True), nullable=False),
)


class SqlRunRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create_run(self, run: RunRecord) -> None:
        values = run.model_dump()
        values["plan"] = run.plan.model_dump(mode="json")
        values["status"] = run.status.value
        async with self._engine.begin() as conn:
            await conn.execute(insert(runs).values(**values))

    async def finish_run(
        self, run_id: str, status: RunStatus, ended_at: datetime, wall_ms: float
    ) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                update(runs)
                .where(runs.c.run_id == run_id)
                .values(status=status.value, ended_at=ended_at, wall_ms=wall_ms)
            )

    async def get_run(self, run_id: str) -> RunRecord | None:
        async with self._engine.connect() as conn:
            row = (
                (await conn.execute(select(runs).where(runs.c.run_id == run_id))).mappings().first()
            )
        return RunRecord.model_validate(dict(row)) if row is not None else None

    async def add_delegation(self, record: DelegationRecord) -> None:
        values = record.model_dump()
        values["skill"] = record.skill.value
        values["state"] = record.state.value
        async with self._engine.begin() as conn:
            await conn.execute(insert(delegations).values(**values))

    async def list_delegations(self, run_id: str) -> list[DelegationRecord]:
        stmt = select(delegations).where(delegations.c.run_id == run_id).order_by(delegations.c.id)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
        return [
            DelegationRecord.model_validate({k: v for k, v in r.items() if k != "id"}) for r in rows
        ]
