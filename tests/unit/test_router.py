import httpx
import pytest
import respx

from bourse.llm.base import ChatMessage
from bourse.llm.router import AllProvidersFailed
from bourse.runtime.breaker import CircuitBreaker, ProviderHTTPError
from tests.unit.fakes import (
    OLLAMA_CHAT,
    OMNI_CHAT,
    PAID_CHAT,
    make_router,
    make_spec,
    openai_reply,
    request_json,
)

MSGS = [ChatMessage(role="user", content="x")]


async def test_falls_back_to_omniroute_on_503() -> None:
    mock = respx.Router(assert_all_called=False)
    ollama = mock.post(OLLAMA_CHAT).mock(return_value=httpx.Response(503, text="overloaded"))
    omni = mock.post(OMNI_CHAT).mock(return_value=openai_reply("from omniroute"))
    breaker = CircuitBreaker()
    result = await make_router(mock, breaker).chat(MSGS, [], 16, 5.0)
    assert result.text == "from omniroute"
    assert result.usage.provider == "omniroute"
    assert ollama.call_count == 1
    assert omni.call_count == 1
    assert not breaker.tripped
    assert request_json(omni.calls.last.request)["max_tokens"] == 16
    assert omni.calls.last.request.headers["Authorization"] == "Bearer dummy"


async def test_falls_back_to_omniroute_on_connect_error() -> None:
    mock = respx.Router(assert_all_called=False)
    mock.post(OLLAMA_CHAT).mock(side_effect=httpx.ConnectError("connection refused"))
    mock.post(OMNI_CHAT).mock(return_value=openai_reply("ok"))
    result = await make_router(mock, CircuitBreaker()).chat(MSGS, [], 16, 5.0)
    assert result.usage.provider == "omniroute"


async def test_never_reaches_paid_when_not_allowed() -> None:
    mock = respx.Router(assert_all_called=False)
    mock.post(OLLAMA_CHAT).mock(return_value=httpx.Response(503, text="down"))
    mock.post(OMNI_CHAT).mock(return_value=httpx.Response(503, text="down"))
    paid = mock.post(PAID_CHAT).mock(return_value=openai_reply("paid"))
    router = make_router(mock, CircuitBreaker(), make_spec(allow_paid=False), paid=True)
    with pytest.raises(AllProvidersFailed) as info:
        await router.chat(MSGS, [], 16, 5.0)
    assert paid.call_count == 0
    assert [name for name, _ in info.value.errors] == ["ollama", "omniroute"]


async def test_allow_paid_without_cap_still_blocks_paid() -> None:
    mock = respx.Router(assert_all_called=False)
    mock.post(OLLAMA_CHAT).mock(return_value=httpx.Response(503, text="down"))
    mock.post(OMNI_CHAT).mock(return_value=httpx.Response(503, text="down"))
    paid = mock.post(PAID_CHAT).mock(return_value=openai_reply("paid"))
    router = make_router(mock, CircuitBreaker(), make_spec(allow_paid=True), paid=True)
    with pytest.raises(AllProvidersFailed):
        await router.chat(MSGS, [], 16, 5.0)
    assert paid.call_count == 0


async def test_non_retriable_4xx_is_raised_without_fallback() -> None:
    mock = respx.Router(assert_all_called=False)
    mock.post(OLLAMA_CHAT).mock(return_value=httpx.Response(400, text="bad request"))
    omni = mock.post(OMNI_CHAT).mock(return_value=openai_reply())
    with pytest.raises(ProviderHTTPError):
        await make_router(mock, CircuitBreaker()).chat(MSGS, [], 16, 5.0)
    assert omni.call_count == 0
