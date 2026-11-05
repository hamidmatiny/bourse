from datetime import UTC, datetime

from fastapi.testclient import TestClient

from bourse.contracts.trust import Severity, Violation
from bourse.registry.aggregate import ViolationAggregator
from bourse.registry.app import create_app
from bourse.registry.rate_limit import TokenBucket
from bourse.registry.repo_memory import InMemoryRepository
from bourse.registry.service import RegistryService


def test_token_bucket_allows_rate_then_429() -> None:
    t = [0.0]
    bucket = TokenBucket(2, clock=lambda: t[0])
    assert bucket.allow("ip:1")
    assert bucket.allow("ip:1")
    assert not bucket.allow("ip:1")
    t[0] = 60.0
    assert bucket.allow("ip:1")


def test_register_rate_limit_http() -> None:
    repo = InMemoryRepository()
    service = RegistryService(repo)
    app = create_app(service, "admin-token-16chars", rate_per_min=2)
    client = TestClient(app)
    body = {"vendor_domain": "no-such.local", "card": {"name": "x"}}
    assert client.post("/agents/register", json=body).status_code == 422
    assert client.post("/agents/register", json=body).status_code == 422
    assert client.post("/agents/register", json=body).status_code == 429


def test_refresh_rate_limit_http() -> None:
    repo = InMemoryRepository()
    service = RegistryService(repo)
    app = create_app(service, "admin-token-16chars", rate_per_min=1)
    client = TestClient(app)
    body = {"vendor_domain": "no-such.local", "card": {"name": "x"}}
    assert client.post("/agents/register", json=body).status_code == 422
    assert client.post("/agents/nobody/refresh").status_code == 429


async def test_inline_violations_aggregate_after_n() -> None:
    repo = InMemoryRepository()
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    agg = ViolationAggregator(repo, max_individual=2, clock=lambda: now)
    v = Violation(
        kind="TAMPERED",
        severity=Severity.HARD,
        agent_id="a@vendor-x.local",
        evidence="e",
        at=now,
    )
    assert await agg.add(v, "vendor-x.local") == "row"
    assert await agg.add(v, "vendor-x.local") == "row"
    assert await agg.add(v, "vendor-x.local") == "aggregate"
    assert len(await repo.list_violations("a@vendor-x.local")) == 2
    hour = now.replace(minute=0, second=0, microsecond=0)
    assert await repo.get_aggregate("vendor-x.local", hour) == 1
