"""Postgres integration tests. Run with `uv run pytest -m db` against BOURSE_DATABASE_URL."""

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
from bourse.contracts.approval import ApprovalDecision
from bourse.contracts.registry import RegisterRequest, RejectReason
from bourse.contracts.skills import SkillId
from bourse.contracts.trust import TrustState
from bourse.registry.repo import RevisionExists
from bourse.registry.repo_sql import SqlRepository
from bourse.registry.service import RegistryService
from bourse.registry.state import transition
from tests.registry_helpers import Party, load_card, make_party, make_service

pytestmark = pytest.mark.db

URL = get_settings().bourse_database_url
ROOT = Path(__file__).resolve().parents[2]
TABLES = {"vendors", "agents", "card_revisions", "violations", "state_transitions"}
PROFILER_ID = "vendor-a-schema-profiler@vendor-a.local"


def _alembic() -> Config:
    return Config(str(ROOT / "alembic.ini"))


def _tables() -> set[str]:
    engine = create_engine(URL)
    try:
        return set(inspect(engine).get_table_names()) & TABLES
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def migrated() -> Iterator[None]:
    command.upgrade(_alembic(), "head")
    yield


@pytest.fixture
async def repo(migrated: None) -> AsyncIterator[SqlRepository]:
    engine = create_async_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(sorted(TABLES))} RESTART IDENTITY CASCADE"))
    yield SqlRepository(engine)
    await engine.dispose()


async def _service(repo: SqlRepository, *parties: Party) -> RegistryService:
    return await make_service(*parties, repo=repo)


def test_migrations_up_down_up(migrated: None) -> None:
    cfg = _alembic()
    assert _tables() == TABLES
    command.downgrade(cfg, "base")
    assert _tables() == set()
    command.upgrade(cfg, "head")
    assert _tables() == TABLES


async def test_register_and_revision_immutability(repo: SqlRepository) -> None:
    a = make_party("vendor-a.local")
    service = await _service(repo, a)
    card = load_card("honest-a-profiler.json")
    outcome = await service.register(RegisterRequest(vendor_domain=a.domain, card=a.sign(card)))
    assert outcome.accepted
    revisions = await repo.list_revisions(PROFILER_ID)
    assert [r.version for r in revisions] == ["1.0.0"]
    agent = await repo.get_agent(PROFILER_ID)
    assert agent is not None

    # Unique (agent_id, version): a second insert of 1.0.0 is refused by the database.
    with pytest.raises(RevisionExists):
        await repo.save_registration(agent, revisions[0])
    # Append-only trigger: UPDATE and DELETE are refused.
    engine = create_async_engine(URL)
    try:
        for sql in (
            "UPDATE card_revisions SET version = '9.9.9'",
            "DELETE FROM card_revisions",
        ):
            with pytest.raises(DBAPIError, match="append-only"):
                async with engine.begin() as conn:
                    await conn.execute(text(sql))
    finally:
        await engine.dispose()
    assert [r.version for r in await repo.list_revisions(PROFILER_ID)] == ["1.0.0"]

    # Service-level: same version, changed content -> VERSION_REUSE, nothing stored.
    reused = await service.register(
        RegisterRequest(vendor_domain=a.domain, card=a.sign(dict(card, description="changed")))
    )
    assert reused.reason is RejectReason.VERSION_REUSE
    assert len(await repo.list_revisions(PROFILER_ID)) == 1
    assert [v.kind for v in await repo.list_violations(PROFILER_ID)] == ["VERSION_REUSE"]


async def test_fts_search_name_and_tags_only(repo: SqlRepository) -> None:
    a = make_party("vendor-a.local")
    b = make_party("vendor-b.local")
    service = await _service(repo, a, b)
    for party, name in (
        (a, "honest-a-profiler.json"),
        (a, "honest-a-stats.json"),
        (b, "honest-b-pii.json"),
    ):
        outcome = await service.register(
            RegisterRequest(vendor_domain=party.domain, card=party.sign(load_card(name)))
        )
        assert outcome.accepted, outcome

    async def ids(
        skill: SkillId | None = None, state: TrustState | None = None, q: str | None = None
    ) -> list[str]:
        return [x.agent_id for x in await repo.search(skill=skill, state=state, q=q)]

    assert await ids(q="profiler") == [PROFILER_ID]
    assert len(await ids(q="dataset")) == 3
    assert await ids(q="Profiles") == []  # description words are not indexed
    assert await ids(q="personnelles") == []  # nor non-ASCII description text
    assert await ids(skill=SkillId.PII_DETECT) == ["vendor-b-pii-detector@vendor-b.local"]
    assert await ids(skill=SkillId.REPORT_COMPOSE) == []
    assert len(await ids(state=TrustState.PENDING)) == 3
    assert await ids(state=TrustState.ACTIVE) == []


async def test_transitions_write_audit_rows(repo: SqlRepository) -> None:
    a = make_party("vendor-a.local")
    service = await _service(repo, a)
    await service.register(
        RegisterRequest(vendor_domain=a.domain, card=a.sign(load_card("honest-a-profiler.json")))
    )
    now = datetime.now(UTC)
    await transition(repo, PROFILER_ID, TrustState.ACTIVE, actor="admin", reason="exam")
    await transition(repo, PROFILER_ID, TrustState.QUARANTINED, actor="registry", reason="hard")
    decision = ApprovalDecision(approved=True, by="operator", at=now, reason="fixed")
    await transition(
        repo, PROFILER_ID, TrustState.ACTIVE, actor="admin", reason="reinstate", approval=decision
    )
    audit = await repo.list_transitions(PROFILER_ID)
    assert [(t.from_state, t.to_state) for t in audit] == [
        (TrustState.PENDING, TrustState.ACTIVE),
        (TrustState.ACTIVE, TrustState.QUARANTINED),
        (TrustState.QUARANTINED, TrustState.ACTIVE),
    ]
    assert audit[-1].approval == decision
    agent = await repo.get_agent(PROFILER_ID)
    assert agent is not None and agent.state is TrustState.ACTIVE
    engine = create_async_engine(URL)
    try:
        with pytest.raises(DBAPIError, match="append-only"):
            async with engine.begin() as conn:
                await conn.execute(text("DELETE FROM state_transitions"))
    finally:
        await engine.dispose()
