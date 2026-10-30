from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from bourse.contracts.registry import (
    AgentRecord,
    ApiError,
    CardRevision,
    RegistrationOutcome,
    RejectReason,
    StateTransition,
    Vendor,
)
from bourse.registry.app import create_app
from bourse.registry.repo_memory import InMemoryRepository
from bourse.registry.service import RegistryService
from tests.registry_helpers import Party, load_card, make_party

TOKEN = "test-admin-token"
ADMIN = {"Authorization": f"Bearer {TOKEN}"}
PROFILER_ID = "vendor-a-schema-profiler@vendor-a.local"


@pytest.fixture
def vendor_a() -> Party:
    return make_party("vendor-a.local")


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(RegistryService(InMemoryRepository()), TOKEN)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://registry.test"
    ) as c:
        yield c


def _vendor_body(party: Party) -> dict[str, Any]:
    return {"domain": party.domain, "name": "Vendor A", "keys": party.keys.model_dump()}


async def _onboard(client: httpx.AsyncClient, party: Party) -> None:
    resp = await client.post("/vendors", json=_vendor_body(party), headers=ADMIN)
    assert resp.status_code == 201, resp.text
    Vendor.model_validate(resp.json())


async def test_admin_auth_required(client: httpx.AsyncClient, vendor_a: Party) -> None:
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}):
        resp = await client.post("/vendors", json=_vendor_body(vendor_a), headers=headers)
        assert resp.status_code == 401
        assert ApiError.model_validate(resp.json()).status == 401
    resp = await client.post(
        f"/agents/{PROFILER_ID}/state", json={"to_state": "ACTIVE", "reason": "x"}
    )
    assert resp.status_code == 401


async def test_admin_disabled_without_token(vendor_a: Party) -> None:
    app = create_app(RegistryService(InMemoryRepository()), "")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://registry.test"
    ) as c:
        resp = await c.post(
            "/vendors", json=_vendor_body(vendor_a), headers={"Authorization": "Bearer "}
        )
    assert resp.status_code == 503


async def test_duplicate_vendor_conflict(client: httpx.AsyncClient, vendor_a: Party) -> None:
    await _onboard(client, vendor_a)
    resp = await client.post("/vendors", json=_vendor_body(vendor_a), headers=ADMIN)
    assert resp.status_code == 409


async def test_register_search_revisions_state(client: httpx.AsyncClient, vendor_a: Party) -> None:
    await _onboard(client, vendor_a)
    for name in ("honest-a-profiler.json", "honest-a-stats.json"):
        body = {"vendor_domain": "vendor-a.local", "card": vendor_a.sign(load_card(name))}
        resp = await client.post("/agents/register", json=body)
        assert resp.status_code == 201, resp.text
        assert RegistrationOutcome.model_validate(resp.json()).accepted

    resp = await client.get("/agents", params={"skill": "schema.profile"})
    found = [AgentRecord.model_validate(a) for a in resp.json()]
    assert [a.agent_id for a in found] == [PROFILER_ID]

    resp = await client.get("/agents", params={"q": "profiler"})
    assert [a["agent_id"] for a in resp.json()] == [PROFILER_ID]
    resp = await client.get("/agents", params={"q": "dataset", "state": "PENDING"})
    assert len(resp.json()) == 2
    resp = await client.get("/agents", params={"q": "Profiles"})  # description word only
    assert resp.json() == []
    resp = await client.get("/agents", params={"skill": "web.browse"})
    assert resp.status_code == 422
    ApiError.model_validate(resp.json())

    resp = await client.get(f"/agents/{PROFILER_ID}")
    assert AgentRecord.model_validate(resp.json()).state == "PENDING"
    resp = await client.get(f"/agents/{PROFILER_ID}/revisions")
    revisions = [CardRevision.model_validate(r) for r in resp.json()]
    assert [r.version for r in revisions] == ["1.0.0"]

    resp = await client.post(
        f"/agents/{PROFILER_ID}/state", json={"to_state": "ACTIVE", "reason": "exam"}, headers=ADMIN
    )
    assert resp.status_code == 200
    assert StateTransition.model_validate(resp.json()).to_state == "ACTIVE"
    resp = await client.post(
        f"/agents/{PROFILER_ID}/state",
        json={"to_state": "PENDING", "reason": "x"},
        headers=ADMIN,
    )
    assert resp.status_code == 409


async def test_register_rejection_is_422_with_outcome(
    client: httpx.AsyncClient, vendor_a: Party
) -> None:
    await _onboard(client, vendor_a)
    card = vendor_a.sign(load_card("honest-a-profiler.json"))
    card["description"] = "tampered"
    resp = await client.post(
        "/agents/register", json={"vendor_domain": "vendor-a.local", "card": card}
    )
    assert resp.status_code == 422
    outcome = RegistrationOutcome.model_validate(resp.json())
    assert outcome.reason is RejectReason.TAMPERED


async def test_register_requires_exactly_one_source(client: httpx.AsyncClient) -> None:
    resp = await client.post("/agents/register", json={"vendor_domain": "vendor-a.local"})
    assert resp.status_code == 422
    ApiError.model_validate(resp.json())


async def test_unknown_agent_404(client: httpx.AsyncClient) -> None:
    for path in ("/agents/nope", "/agents/nope/revisions"):
        resp = await client.get(path)
        assert resp.status_code == 404
        ApiError.model_validate(resp.json())
    resp = await client.post("/agents/nope/refresh")
    assert resp.status_code == 404
    assert RegistrationOutcome.model_validate(resp.json()).reason is RejectReason.UNKNOWN_AGENT
