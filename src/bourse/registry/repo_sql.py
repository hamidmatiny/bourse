"""Postgres Repository: SQLAlchemy 2 async + psycopg 3 (ADR-0014).

card_revisions and state_transitions are append-only: unique(agent_id, version) plus triggers
(created in the migration) that reject UPDATE and DELETE.
Search: skills array containment + full-text search over agent name and skill tags only
(`search_text`), never over descriptions.
"""

from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Computed,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from bourse.contracts.approval import ApprovalDecision
from bourse.contracts.registry import (
    AgentRecord,
    CardRevision,
    StateTransition,
    Vendor,
    VendorKeys,
    VerifyResult,
)
from bourse.contracts.skills import SkillId
from bourse.contracts.trust import Severity, TrustState, Violation
from bourse.registry.repo import RevisionExists, VendorExists

metadata = MetaData()

TS = DateTime(timezone=True)

vendors = Table(
    "vendors",
    metadata,
    Column("domain", Text, primary_key=True),
    Column("name", Text, nullable=False),
    Column("jwks_url", Text, nullable=False),
    Column("jwks", JSONB, nullable=False),
    Column("created_at", TS, nullable=False),
)

agents = Table(
    "agents",
    metadata,
    Column("agent_id", Text, primary_key=True),
    Column("vendor_domain", Text, ForeignKey("vendors.domain"), nullable=False),
    Column("name", Text, nullable=False),
    Column("card_url", Text),
    Column("state", Text, nullable=False),
    Column("current_version", Text, nullable=False),
    Column("current_digest", Text, nullable=False),
    Column("skills", ARRAY(Text), nullable=False),
    Column("tags", ARRAY(Text), nullable=False),
    Column("search_text", Text, nullable=False),
    Column("search_tsv", TSVECTOR, Computed("to_tsvector('simple', search_text)", persisted=True)),
    Column("created_at", TS, nullable=False),
    Column("updated_at", TS, nullable=False),
    CheckConstraint(
        "state IN ('PENDING','ACTIVE','QUARANTINED','REVOKED')", name="ck_agents_state"
    ),
    Index("ix_agents_search_tsv", "search_tsv", postgresql_using="gin"),
    Index("ix_agents_skills", "skills", postgresql_using="gin"),
)

card_revisions = Table(
    "card_revisions",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("agent_id", Text, ForeignKey("agents.agent_id"), nullable=False),
    Column("version", Text, nullable=False),
    Column("digest_sha256", Text, nullable=False),
    Column("verify_result", Text, nullable=False),
    Column("card", JSONB, nullable=False),
    Column("received_at", TS, nullable=False),
    UniqueConstraint("agent_id", "version", name="uq_card_revisions_agent_version"),
)

violations = Table(
    "violations",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("agent_id", Text, nullable=False, index=True),
    Column("kind", Text, nullable=False),
    Column("severity", Text, nullable=False),
    Column("evidence", Text, nullable=False),
    Column("at", TS, nullable=False),
)

state_transitions = Table(
    "state_transitions",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("agent_id", Text, ForeignKey("agents.agent_id"), nullable=False, index=True),
    Column("from_state", Text, nullable=False),
    Column("to_state", Text, nullable=False),
    Column("actor", Text, nullable=False),
    Column("reason", Text, nullable=False),
    Column("approval", JSONB),
    Column("at", TS, nullable=False),
)


def search_text(agent: AgentRecord) -> str:
    return " ".join((agent.name, *agent.tags)).lower()


def _agent_row(agent: AgentRecord) -> dict[str, Any]:
    return {
        "agent_id": agent.agent_id,
        "vendor_domain": agent.vendor_domain,
        "name": agent.name,
        "card_url": agent.card_url,
        "state": agent.state.value,
        "current_version": agent.current_version,
        "current_digest": agent.current_digest,
        "skills": [s.value for s in agent.skills],
        "tags": list(agent.tags),
        "search_text": search_text(agent),
        "created_at": agent.created_at,
        "updated_at": agent.updated_at,
    }


def _to_agent(row: Any) -> AgentRecord:
    return AgentRecord(
        agent_id=row.agent_id,
        vendor_domain=row.vendor_domain,
        name=row.name,
        card_url=row.card_url,
        state=TrustState(row.state),
        current_version=row.current_version,
        current_digest=row.current_digest,
        skills=tuple(SkillId(s) for s in row.skills),
        tags=tuple(row.tags),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def add_vendor(self, vendor: Vendor) -> None:
        try:
            async with self._engine.begin() as conn:
                await conn.execute(
                    vendors.insert().values(
                        domain=vendor.domain,
                        name=vendor.name,
                        jwks_url=vendor.keys.jwks_url,
                        jwks=vendor.keys.jwks,
                        created_at=vendor.created_at,
                    )
                )
        except IntegrityError as exc:
            raise VendorExists(vendor.domain) from exc

    async def get_vendor(self, domain: str) -> Vendor | None:
        async with self._engine.connect() as conn:
            row = (await conn.execute(select(vendors).where(vendors.c.domain == domain))).first()
        if row is None:
            return None
        return Vendor(
            domain=row.domain,
            name=row.name,
            keys=VendorKeys(jwks_url=row.jwks_url, jwks=row.jwks),
            created_at=row.created_at,
        )

    async def get_agent(self, agent_id: str) -> AgentRecord | None:
        async with self._engine.connect() as conn:
            row = (await conn.execute(select(agents).where(agents.c.agent_id == agent_id))).first()
        return None if row is None else _to_agent(row)

    async def _upsert_agent(self, conn: Any, agent: AgentRecord) -> None:
        values = _agent_row(agent)
        stmt = insert(agents).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[agents.c.agent_id],
            set_={k: stmt.excluded[k] for k in values if k not in ("agent_id", "created_at")},
        )
        await conn.execute(stmt)

    async def save_registration(self, agent: AgentRecord, revision: CardRevision) -> None:
        try:
            async with self._engine.begin() as conn:
                await self._upsert_agent(conn, agent)
                await conn.execute(
                    card_revisions.insert().values(
                        agent_id=revision.agent_id,
                        version=revision.version,
                        digest_sha256=revision.digest_sha256,
                        verify_result=revision.verify_result.value,
                        card=revision.card,
                        received_at=revision.received_at,
                    )
                )
        except IntegrityError as exc:
            raise RevisionExists(f"{revision.agent_id} {revision.version}") from exc

    async def save_transition(self, agent: AgentRecord, transition: StateTransition) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                agents.update()
                .where(agents.c.agent_id == agent.agent_id)
                .values(state=agent.state.value, updated_at=agent.updated_at)
            )
            await conn.execute(
                state_transitions.insert().values(
                    agent_id=transition.agent_id,
                    from_state=transition.from_state.value,
                    to_state=transition.to_state.value,
                    actor=transition.actor,
                    reason=transition.reason,
                    approval=(
                        transition.approval.model_dump(mode="json") if transition.approval else None
                    ),
                    at=transition.at,
                )
            )

    async def add_violation(self, violation: Violation) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                violations.insert().values(
                    agent_id=violation.agent_id,
                    kind=violation.kind,
                    severity=violation.severity.value,
                    evidence=violation.evidence,
                    at=violation.at,
                )
            )

    async def list_violations(self, agent_id: str) -> list[Violation]:
        stmt = select(violations).where(violations.c.agent_id == agent_id).order_by(violations.c.id)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [
            Violation(
                kind=r.kind,
                severity=Severity(r.severity),
                agent_id=r.agent_id,
                evidence=r.evidence,
                at=r.at,
            )
            for r in rows
        ]

    async def list_revisions(self, agent_id: str) -> list[CardRevision]:
        stmt = (
            select(card_revisions)
            .where(card_revisions.c.agent_id == agent_id)
            .order_by(card_revisions.c.id)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [
            CardRevision(
                agent_id=r.agent_id,
                version=r.version,
                digest_sha256=r.digest_sha256,
                verify_result=VerifyResult(r.verify_result),
                received_at=r.received_at,
                card=r.card,
            )
            for r in rows
        ]

    async def list_transitions(self, agent_id: str) -> list[StateTransition]:
        stmt = (
            select(state_transitions)
            .where(state_transitions.c.agent_id == agent_id)
            .order_by(state_transitions.c.id)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [
            StateTransition(
                agent_id=r.agent_id,
                from_state=TrustState(r.from_state),
                to_state=TrustState(r.to_state),
                actor=r.actor,
                reason=r.reason,
                approval=ApprovalDecision.model_validate(r.approval) if r.approval else None,
                at=r.at,
            )
            for r in rows
        ]

    async def search(
        self,
        skill: SkillId | None = None,
        state: TrustState | None = None,
        q: str | None = None,
        limit: int = 50,
    ) -> list[AgentRecord]:
        stmt = select(agents)
        if skill is not None:
            stmt = stmt.where(agents.c.skills.contains([skill.value]))
        if state is not None:
            stmt = stmt.where(agents.c.state == state.value)
        if q:
            stmt = stmt.where(agents.c.search_tsv.op("@@")(func.plainto_tsquery("simple", q)))
        stmt = stmt.order_by(agents.c.agent_id).limit(limit)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [_to_agent(r) for r in rows]
