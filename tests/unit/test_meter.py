import asyncio
import json
import secrets
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx

from bourse.llm.base import Provider, Sampling
from bourse.llm.ollama import OllamaProvider
from bourse.llm.omniroute import OmniRouteProvider
from bourse.meter.app import MeterConfig, create_meter_app
from bourse.meter.keys import parse_keys
from bourse.meter.repo import InMemoryMeterRepository
from bourse.runtime.breaker import CircuitBreaker
from tests.unit.fakes import OLLAMA, OLLAMA_CHAT, OMNI, OMNI_CHAT, ollama_reply, request_json

MODEL = "qwen3.5:27b-q4_K_M"
OTHER_MODEL = "gemma4:12b"
# Generated per run: no key-shaped literals in the repo (gitleaks generic-api-key).
KEY = secrets.token_hex(16)
HEADERS = {"Authorization": f"Bearer {KEY}", "X-Bourse-Task-Id": "task-1"}
WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Weather for a city",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}


class Harness:
    def __init__(
        self,
        upstream: httpx.AsyncClient,
        *,
        config: MeterConfig | None = None,
        omniroute: bool = False,
    ) -> None:
        self.repo = InMemoryMeterRepository()
        self.breaker = CircuitBreaker()
        providers: dict[str, list[Provider]] = {}
        for model in (MODEL, OTHER_MODEL):
            chain: list[Provider] = [OllamaProvider(OLLAMA, model, upstream)]
            if omniroute:
                chain.append(OmniRouteProvider(OMNI, "omni-model", "dummy", upstream))
            providers[model] = chain
        self.app = create_meter_app(
            providers=providers,
            keys=parse_keys(f"agent-a:{KEY}"),
            repo=self.repo,
            breaker=self.breaker,
            config=config or MeterConfig(max_tokens_per_call=256, deadline_s=5.0),
        )

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://meter.test"
        )


@pytest.fixture
def mock() -> respx.Router:
    return respx.Router(assert_all_called=False)


@pytest.fixture
async def harness(mock: respx.Router) -> AsyncIterator[Harness]:
    yield Harness(httpx.AsyncClient(transport=httpx.MockTransport(mock.async_handler)))


def _body(**extra: Any) -> dict[str, Any]:
    return {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], **extra}


async def _post(h: Harness, body: Any, headers: dict[str, str] | None = None) -> httpx.Response:
    async with h.client() as c:
        return await c.post("/v1/chat/completions", json=body, headers=headers or HEADERS)


async def test_translation_both_ways_and_measured_usage(
    harness: Harness, mock: respx.Router
) -> None:
    route = mock.post(OLLAMA_CHAT).mock(
        return_value=ollama_reply(
            tool_calls=[{"function": {"name": "get_weather", "arguments": {"city": "Montreal"}}}],
            prompt_tokens=123,
            out_tokens=45,
        )
    )
    body = _body(
        messages=[
            {"role": "system", "content": "be brief"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "weather in "},
                    {"type": "text", "text": "Paris?"},
                ],
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_9",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_9", "content": "12C"},
            {"role": "user", "content": "and Montreal?"},
        ],
        tools=[WEATHER_TOOL],
        max_tokens=5000,
        temperature=0.2,
    )
    resp = await _post(harness, body)
    assert resp.status_code == 200, resp.text

    upstream = request_json(route.calls.last.request)
    assert upstream["think"] is False
    assert upstream["stream"] is False
    assert upstream["model"] == MODEL
    assert upstream["options"]["num_predict"] == 256  # min(5000, max_tokens_per_call)
    assert upstream["tools"] == [WEATHER_TOOL]
    assert upstream["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "weather in Paris?"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "get_weather", "arguments": {"city": "Paris"}}}],
        },
        {"role": "tool", "content": "12C", "tool_name": "get_weather"},
        {"role": "user", "content": "and Montreal?"},
    ]

    out = resp.json()
    choice = out["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    call = choice["message"]["tool_calls"][0]
    assert call["type"] == "function"
    assert call["function"]["name"] == "get_weather"
    assert json.loads(call["function"]["arguments"]) == {"city": "Montreal"}
    assert out["usage"] == {"prompt_tokens": 123, "completion_tokens": 45, "total_tokens": 168}

    rows = await harness.repo.list_usage()
    assert len(rows) == 1
    row = rows[0]
    assert (row.agent_id, row.task_id, row.provider, row.model) == (
        "agent-a",
        "task-1",
        "ollama",
        MODEL,
    )
    assert (row.tokens_in, row.tokens_out, row.status, row.error_kind) == (123, 45, 200, None)


async def test_client_reported_usage_is_ignored(harness: Harness, mock: respx.Router) -> None:
    mock.post(OLLAMA_CHAT).mock(return_value=ollama_reply("ok", prompt_tokens=7, out_tokens=3))
    resp = await _post(harness, _body(usage={"prompt_tokens": 1, "completion_tokens": 1}))
    assert resp.status_code == 200
    row = (await harness.repo.list_usage())[0]
    assert (row.tokens_in, row.tokens_out) == (7, 3)


async def test_text_reply_and_missing_task_header(harness: Harness, mock: respx.Router) -> None:
    mock.post(OLLAMA_CHAT).mock(return_value=ollama_reply("hello"))
    resp = await _post(harness, _body(), headers={"Authorization": f"Bearer {KEY}"})
    assert resp.status_code == 200
    message = resp.json()["choices"][0]["message"]
    assert message == {"role": "assistant", "content": "hello"}
    assert resp.json()["choices"][0]["finish_reason"] == "stop"
    assert (await harness.repo.list_usage())[0].task_id is None


async def test_stream_returns_single_sse_chunk(harness: Harness, mock: respx.Router) -> None:
    route = mock.post(OLLAMA_CHAT).mock(return_value=ollama_reply("streamed"))
    resp = await _post(harness, _body(stream=True))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = [e for e in resp.text.split("\n\n") if e]
    assert len(events) == 2 and events[1] == "data: [DONE]"
    chunk = json.loads(events[0].removeprefix("data: "))
    assert chunk["object"] == "chat.completion.chunk"
    assert chunk["choices"][0]["delta"]["content"] == "streamed"
    assert request_json(route.calls.last.request)["stream"] is False


@pytest.mark.parametrize(
    "fmt",
    [{"type": "json_object"}, {"type": "json_schema", "json_schema": {"name": "x", "schema": {}}}],
)
async def test_response_format_rejected(harness: Harness, mock: respx.Router, fmt: Any) -> None:
    route = mock.post(OLLAMA_CHAT).mock(return_value=ollama_reply("x"))
    resp = await _post(harness, _body(response_format=fmt))
    assert resp.status_code == 400
    assert "use submit-tool pattern (ADR-0006)" in resp.json()["error"]["message"]
    assert route.call_count == 0
    assert (await harness.repo.list_usage())[0].error_kind == "response_format"


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": f"Bearer {secrets.token_hex(16)}"}, {"Authorization": KEY}],
    ids=["missing", "wrong", "no-bearer"],
)
async def test_bad_key_is_401(
    harness: Harness, mock: respx.Router, headers: dict[str, str]
) -> None:
    route = mock.post(OLLAMA_CHAT).mock(return_value=ollama_reply("x"))
    async with harness.client() as c:
        resp = await c.post("/v1/chat/completions", json=_body(), headers=headers)
    assert resp.status_code == 401
    assert route.call_count == 0
    assert await harness.repo.list_usage() == []


async def test_model_not_in_allowlist(harness: Harness, mock: respx.Router) -> None:
    route = mock.post(OLLAMA_CHAT).mock(return_value=ollama_reply("x"))
    resp = await _post(harness, _body(model="gpt-4o"))
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "model_not_allowed"
    assert route.call_count == 0


async def test_breaker_402_then_503_without_upstream(harness: Harness, mock: respx.Router) -> None:
    route = mock.post(OLLAMA_CHAT).mock(
        return_value=httpx.Response(402, text="payment required: credit balance too low")
    )
    first = await _post(harness, _body())
    assert first.status_code == 503
    assert route.call_count == 1
    route.mock(return_value=ollama_reply("would succeed"))
    for model in (MODEL, OTHER_MODEL):
        again = await _post(harness, _body(model=model))
        assert again.status_code == 503
    assert route.call_count == 1
    assert mock.calls.call_count == 1
    assert [r.error_kind for r in await harness.repo.list_usage()] == ["breaker_open"] * 3


async def test_deadline_is_504() -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return ollama_reply("late")

    h = Harness(
        httpx.AsyncClient(transport=httpx.MockTransport(slow)),
        config=MeterConfig(max_tokens_per_call=64, deadline_s=0.3),
    )
    resp = await _post(h, _body())
    assert resp.status_code == 504
    assert (await h.repo.list_usage())[0].error_kind == "deadline"


async def test_fallback_to_omniroute_then_502(mock: respx.Router) -> None:
    h = Harness(
        httpx.AsyncClient(transport=httpx.MockTransport(mock.async_handler)), omniroute=True
    )
    mock.post(OLLAMA_CHAT).mock(side_effect=httpx.ConnectError("refused"))
    omni = mock.post(OMNI_CHAT).mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "omni-model",
                "choices": [{"message": {"role": "assistant", "content": "via omni"}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 2},
            },
        )
    )
    resp = await _post(h, _body())
    assert resp.status_code == 200
    row = (await h.repo.list_usage())[0]
    assert (row.provider, row.tokens_in, row.tokens_out) == ("omniroute", 11, 2)
    omni.mock(return_value=httpx.Response(503, text="overloaded"))
    resp = await _post(h, _body())
    assert resp.status_code == 502
    assert (await h.repo.list_usage())[-1].error_kind == "upstream_failed"


def test_parse_keys() -> None:
    k1, k2 = secrets.token_hex(8), secrets.token_hex(8)
    assert parse_keys(f"a:{k1}, b:{k2}") == {k1: "a", k2: "b"}
    for bad in ("a:short", "nocolon", f"a:{k1},b:{k1}"):
        with pytest.raises(ValueError):
            parse_keys(bad)


async def test_proxy_sets_sampling_and_ignores_client_values(
    harness: Harness, mock: respx.Router
) -> None:
    route = mock.post(OLLAMA_CHAT).mock(return_value=ollama_reply("x"))
    resp = await _post(harness, _body(temperature=1.7, seed=99, top_p=0.1))
    assert resp.status_code == 200
    upstream = request_json(route.calls.last.request)
    assert upstream["options"]["temperature"] == 0.0
    assert upstream["options"]["seed"] == 1234
    assert upstream["options"]["num_ctx"] == 8192
    assert "top_p" not in upstream["options"]
    assert upstream["keep_alive"] == "30m"


async def test_seed_header_and_configured_sampling(mock: respx.Router) -> None:
    route = mock.post(OLLAMA_CHAT).mock(return_value=ollama_reply("x"))
    sampling = Sampling(temperature=0.3, seed=7, num_ctx=4096, keep_alive="5m")
    h = Harness(
        httpx.AsyncClient(transport=httpx.MockTransport(mock.async_handler)),
        config=MeterConfig(max_tokens_per_call=64, deadline_s=5.0, sampling=sampling),
    )
    resp = await _post(h, _body(), headers={**HEADERS, "X-Bourse-Seed": "42"})
    assert resp.status_code == 200
    options = request_json(route.calls.last.request)["options"]
    assert (options["temperature"], options["seed"], options["num_ctx"]) == (0.3, 42, 4096)
    bad = await _post(h, _body(), headers={**HEADERS, "X-Bourse-Seed": "abc"})
    assert bad.status_code == 400
