"""Egress events and honeytokens: protocols, in-memory and Postgres (alembic 0006)."""

from typing import Protocol

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Identity,
    Integer,
    MetaData,
    Table,
    Text,
    insert,
    select,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from bourse.contracts.trust import EgressEvent
from bourse.trust.honeytokens import Honeytoken

metadata = MetaData()

egress_events = Table(
    "egress_events",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("src_ip", Text, nullable=False),
    Column("agent_id", Text),
    Column("host", Text),
    Column("port", Integer, nullable=False),
    Column("bytes", Integer, nullable=False),
    Column("payload_sha256", Text, nullable=False),
    Column("honeytoken_hits", Integer, nullable=False),
    Column("at", DateTime(timezone=True), nullable=False),
)

honeytokens = Table(
    "honeytokens",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("task_id", Text, nullable=False),
    Column("card", Text, nullable=False),
    Column("email", Text, nullable=False),
    Column("at", DateTime(timezone=True), nullable=False),
)


class EgressRepository(Protocol):
    async def add_event(self, event: EgressEvent) -> None: ...

    async def list_events(self, agent_id: str | None = None) -> list[EgressEvent]: ...


class InMemoryEgressRepository:
    def __init__(self) -> None:
        self.events: list[EgressEvent] = []

    async def add_event(self, event: EgressEvent) -> None:
        self.events.append(event)

    async def list_events(self, agent_id: str | None = None) -> list[EgressEvent]:
        return [e for e in self.events if agent_id is None or e.agent_id == agent_id]


class SqlEgressRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def add_event(self, event: EgressEvent) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(insert(egress_events).values(**event.model_dump()))

    async def list_events(self, agent_id: str | None = None) -> list[EgressEvent]:
        stmt = select(egress_events).order_by(egress_events.c.id)
        if agent_id is not None:
            stmt = stmt.where(egress_events.c.agent_id == agent_id)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
        return [EgressEvent.model_validate({k: v for k, v in r.items() if k != "id"}) for r in rows]


class SqlHoneytokenRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def add_token(self, token: Honeytoken) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(insert(honeytokens).values(**token.model_dump()))

    async def list_tokens(self) -> list[Honeytoken]:
        async with self._engine.connect() as conn:
            rows = (await conn.execute(select(honeytokens).order_by(honeytokens.c.id))).mappings()
            return [
                Honeytoken.model_validate({k: v for k, v in r.items() if k != "id"}) for r in rows
            ]


class TokenStore(Protocol):
    async def add_token(self, token: Honeytoken) -> None: ...

    async def list_tokens(self) -> list[Honeytoken]: ...
