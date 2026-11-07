import json
import secrets

import httpx
import pytest
import respx

from bourse.llm.base import ChatMessage, Provider
from bourse.llm.ollama import OllamaProvider
from bourse.meter.app import (
    BUDGET_EXHAUSTED,
    TASK_BUDGET_EXCEEDED,
    MeterConfig,
    create_meter_app,
)
from bourse.meter.estimate import estimate_prompt_tokens
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
        assert first.json()["error"]["code"] == BUDGET_EXHAUSTED
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


async def _put(client: httpx.AsyncClient, task_id: str, max_tokens: int, max_calls: int) -> None:
    put = await client.post(
        "/budgets",
        json={"task_id": task_id, "max_tokens": max_tokens, "max_calls": max_calls},
        headers={"Authorization": f"Bearer {BROKER_KEY}"},
    )
    assert put.status_code == 201


def _agent(task_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {AGENT_KEY}", "X-Bourse-Task-Id": task_id}


async def test_hard_cap_counts_the_prompt_estimate(mock: respx.Router) -> None:
    """The pre-6b rule (used + max_tokens <= cap) would admit the third call and end above the
    ceiling; the hard cap also counts the estimated prompt and refuses it."""
    mock.post(OLLAMA_CHAT).mock(return_value=ollama_reply("x", prompt_tokens=60, out_tokens=100))
    client, repo = await _app(mock)
    body = _body(max_tokens=100)
    est = estimate_prompt_tokens([ChatMessage(role="user", content="hi")], [])
    assert est <= 60  # the fake upstream reports at least the estimate
    cap = 2 * 160 + 100 + 30
    assert 2 * 160 + 100 <= cap < 3 * 160  # old rule admits call 3, which ends above cap
    assert 2 * 160 + est + 100 > cap  # hard cap refuses it
    async with client:
        await _put(client, "t2", max_tokens=cap, max_calls=10)
        codes = []
        for _ in range(3):
            resp = await client.post("/v1/chat/completions", json=body, headers=_agent("t2"))
            codes.append(resp.status_code)
        third = resp.json()
    assert codes == [200, 200, 429]
    assert third["error"]["code"] == BUDGET_EXHAUSTED
    assert mock.calls.call_count == 2
    rows = await repo.list_usage(task_id="t2")
    assert sum(r.tokens_in + r.tokens_out for r in rows) == 320 <= cap


async def test_max_calls_keeps_its_own_code(mock: respx.Router) -> None:
    mock.post(OLLAMA_CHAT).mock(return_value=ollama_reply("x", prompt_tokens=5, out_tokens=5))
    client, _repo = await _app(mock)
    async with client:
        await _put(client, "t3", max_tokens=100_000, max_calls=1)
        ok = await client.post("/v1/chat/completions", json=_body(), headers=_agent("t3"))
        again = await client.post("/v1/chat/completions", json=_body(), headers=_agent("t3"))
    assert ok.status_code == 200
    assert again.status_code == 429
    assert again.json()["error"]["code"] == TASK_BUDGET_EXCEEDED


async def test_usage_rows_record_the_prompt_estimate(mock: respx.Router) -> None:
    mock.post(OLLAMA_CHAT).mock(return_value=ollama_reply("x", prompt_tokens=7, out_tokens=3))
    client, repo = await _app(mock)
    async with client:
        await _put(client, "t4", max_tokens=100_000, max_calls=5)
        resp = await client.post("/v1/chat/completions", json=_body(), headers=_agent("t4"))
    assert resp.status_code == 200
    rows = await repo.list_usage(task_id="t4")
    assert len(rows) == 1 and rows[0].prompt_tokens_est is not None
    assert rows[0].prompt_tokens_est >= rows[0].tokens_in


async def test_budget_refusal_does_not_trip_the_breaker(mock: respx.Router) -> None:
    """The refusal body has no quota/credit/billing wording (ADR-0005 classification)."""
    mock.post(OLLAMA_CHAT).mock(return_value=ollama_reply("x"))
    client, _repo = await _app(mock)
    async with client:
        await _put(client, "t5", max_tokens=10, max_calls=5)
        resp = await client.post(
            "/v1/chat/completions", json=_body(max_tokens=256), headers=_agent("t5")
        )
    breaker = CircuitBreaker()
    breaker.record(resp)
    assert resp.status_code == 429 and not breaker.tripped


async def test_no_metercap_profile_removes_task_budgets_only(mock: respx.Router) -> None:
    """no-metercap (ADR-0053): no per-task ceiling and no task registration needed; the global
    per-call cap stays."""
    mock.post(OLLAMA_CHAT).mock(return_value=ollama_reply("x", prompt_tokens=60, out_tokens=100))
    store = InMemoryMeterRepository()
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(mock.async_handler))
    providers: dict[str, list[Provider]] = {MODEL: [OllamaProvider(OLLAMA, MODEL, upstream)]}
    app = create_meter_app(
        providers=providers,
        keys=parse_keys(f"broker:{BROKER_KEY},agent-a:{AGENT_KEY}"),
        repo=store,
        breaker=CircuitBreaker(),
        config=MeterConfig(
            max_tokens_per_call=256,
            deadline_s=5.0,
            require_task_budget=False,
            stack_profile="no-metercap",
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://meter.test"
    ) as client:
        codes = [
            (
                await client.post(
                    "/v1/chat/completions",
                    json=_body(max_tokens=100),
                    headers=_agent("never-registered"),
                )
            ).status_code
            for _ in range(5)
        ]
        too_big = await client.post(
            "/v1/chat/completions", json=_body(max_tokens=5000), headers=_agent("t")
        )
        profile = (await client.get("/stack-profile")).json()
    assert codes == [200] * 5
    # The global per-call cap still clamps the output (runtime.budget), with or without budgets.
    assert too_big.status_code == 200
    sent = json.loads(mock.calls.last.request.content)
    assert sent["options"]["num_predict"] == 256
    assert profile == {"profile": "no-metercap", "require_task_budget": False}
