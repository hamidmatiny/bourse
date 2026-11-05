import secrets

import httpx
import pytest
import respx

from bourse.llm.base import Provider
from bourse.llm.ollama import OllamaProvider
from bourse.meter.app import TASK_BUDGET_EXCEEDED, MeterConfig, create_meter_app
from bourse.meter.keys import parse_keys
from bourse.meter.repo import InMemoryMeterRepository
from bourse.runtime.breaker import CircuitBreaker
from tests.unit.fakes import OLLAMA, OLLAMA_CHAT, ollama_reply
from tests.unit.test_meter import MODEL, _body

BROKER_KEY = secrets.token_hex(16)
AGENT_KEY = secrets.token_hex(16)


@pytest.fixture
def mock() -> respx.Router:
    return respx.Router(assert_all_called=False)


async def _app(
    mock: respx.Router, repo: InMemoryMeterRepository | None = None
) -> tuple[httpx.AsyncClient, InMemoryMeterRepository]:
    store = repo or InMemoryMeterRepository()
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(mock.async_handler))
    providers: dict[str, list[Provider]] = {MODEL: [OllamaProvider(OLLAMA, MODEL, upstream)]}
    app = create_meter_app(
        providers=providers,
        keys=parse_keys(f"broker:{BROKER_KEY},agent-a:{AGENT_KEY}"),
        repo=store,
        breaker=CircuitBreaker(),
        config=MeterConfig(max_tokens_per_call=256, deadline_s=5.0, require_task_budget=True),
    )
    return (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://meter.test"),
        store,
    )


async def test_unregistered_task_is_403(mock: respx.Router) -> None:
    mock.post(OLLAMA_CHAT).mock(return_value=ollama_reply("x"))
    client, _repo = await _app(mock)
    async with client:
        resp = await client.post(
            "/v1/chat/completions",
            json=_body(),
            headers={"Authorization": f"Bearer {AGENT_KEY}", "X-Bourse-Task-Id": "invented"},
        )
    assert resp.status_code == 403
    assert mock.calls.call_count == 0


async def test_over_budget_is_429_no_upstream(mock: respx.Router) -> None:
    mock.post(OLLAMA_CHAT).mock(return_value=ollama_reply("x"))
    client, repo = await _app(mock)
    async with client:
        put = await client.post(
            "/budgets",
            json={"task_id": "t1", "max_tokens": 10, "max_calls": 1},
            headers={"Authorization": f"Bearer {BROKER_KEY}"},
        )
        assert put.status_code == 201
        first = await client.post(
            "/v1/chat/completions",
            json=_body(max_tokens=256),
            headers={"Authorization": f"Bearer {AGENT_KEY}", "X-Bourse-Task-Id": "t1"},
        )
        assert first.status_code == 429
        assert first.json()["error"]["code"] == TASK_BUDGET_EXCEEDED
        assert mock.calls.call_count == 0
        rows = await repo.list_usage(task_id="t1")
        assert rows and rows[0].status == 429 and rows[0].error_kind == "BUDGET"


async def test_agent_cannot_register_budget(mock: respx.Router) -> None:
    client, _repo = await _app(mock)
    async with client:
        resp = await client.post(
            "/budgets",
            json={"task_id": "t1", "max_tokens": 100, "max_calls": 2},
            headers={"Authorization": f"Bearer {AGENT_KEY}"},
        )
    assert resp.status_code == 403
