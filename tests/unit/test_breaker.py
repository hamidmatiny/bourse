import httpx
import pytest
import respx

from bourse.llm.base import ChatMessage
from bourse.runtime.breaker import BreakerOpen, CircuitBreaker, ProviderHTTPError, Verdict
from bourse.runtime.deadline import DeadlineExceeded
from tests.unit.fakes import OLLAMA_CHAT, OMNI_CHAT, make_router, ollama_reply, openai_reply

MSGS = [ChatMessage(role="user", content="x")]


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (401, "unauthorized"),
        (402, "payment required"),
        (403, "forbidden"),
        (429, "You exceeded your current quota"),
        (400, '{"error": {"code": "insufficient_quota"}}'),
        (400, "Your credit balance is too low"),
        (500, "billing account suspended"),
    ],
)
def test_trips(status: int, body: str) -> None:
    assert CircuitBreaker.classify(httpx.Response(status, text=body)) is Verdict.TRIP
    assert CircuitBreaker.classify(ProviderHTTPError("p", status, body)) is Verdict.TRIP


@pytest.mark.parametrize(
    ("status", "body"),
    [(500, "internal error"), (503, "overloaded"), (429, "rate limit exceeded, retry later")],
)
def test_does_not_trip_on_transient_status(status: int, body: str) -> None:
    assert CircuitBreaker.classify(httpx.Response(status, text=body)) is Verdict.IGNORE


@pytest.mark.parametrize(
    "exc",
    [httpx.ReadTimeout("slow"), httpx.ConnectError("refused"), DeadlineExceeded(1.0)],
    ids=["read-timeout", "connect-error", "deadline"],
)
def test_does_not_trip_on_timeouts(exc: BaseException) -> None:
    breaker = CircuitBreaker()
    assert breaker.record(exc) is Verdict.IGNORE
    assert not breaker.tripped


def test_guard_raises_once_tripped() -> None:
    breaker = CircuitBreaker()
    breaker.guard()
    breaker.record(httpx.Response(402, text="pay"))
    assert breaker.tripped
    with pytest.raises(BreakerOpen, match="402"):
        breaker.guard()


async def test_router_refuses_all_calls_after_trip() -> None:
    mock = respx.Router(assert_all_called=False)
    ollama = mock.post(OLLAMA_CHAT).mock(return_value=httpx.Response(401, text="bad key"))
    omni = mock.post(OMNI_CHAT).mock(return_value=openai_reply())
    breaker = CircuitBreaker()
    router = make_router(mock, breaker)

    with pytest.raises(BreakerOpen):
        await router.chat(MSGS, [], 16, 5.0)
    assert breaker.tripped
    assert ollama.call_count == 1
    assert omni.call_count == 0  # a trip stops the run; no fallback

    ollama.mock(return_value=ollama_reply("would succeed"))
    for _ in range(3):
        with pytest.raises(BreakerOpen):
            await router.chat(MSGS, [], 16, 5.0)
    assert ollama.call_count == 1
    assert omni.call_count == 0
    assert mock.calls.call_count == 1


async def test_breaker_is_shared_across_routers() -> None:
    mock = respx.Router(assert_all_called=False)
    mock.post(OLLAMA_CHAT).mock(
        return_value=httpx.Response(429, text='{"error":"insufficient_quota"}')
    )
    breaker = CircuitBreaker()
    first = make_router(mock, breaker, omniroute=False)
    second = make_router(mock, breaker, omniroute=False)
    with pytest.raises(BreakerOpen):
        await first.chat(MSGS, [], 16, 5.0)
    with pytest.raises(BreakerOpen):
        await second.chat(MSGS, [], 16, 5.0)
    assert mock.calls.call_count == 1
