import httpx
import pytest
import respx
from pydantic import BaseModel, ConfigDict, Field

from bourse.llm.base import ChatMessage
from bourse.llm.submit_tool import SubmitFailed, submit
from bourse.runtime.breaker import CircuitBreaker
from tests.unit.fakes import OLLAMA_CHAT, make_router, ollama_reply, ollama_submit, request_json


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: int = Field(ge=0)
    unit: str


MSGS = [ChatMessage(role="user", content="How many rows? Use submit.")]


async def test_valid_tool_call_is_parsed() -> None:
    mock = respx.Router(assert_all_called=False)
    route = mock.post(OLLAMA_CHAT).mock(return_value=ollama_submit({"value": 3, "unit": "rows"}))
    result = await submit(make_router(mock, CircuitBreaker(), omniroute=False), MSGS, Answer)
    assert result == Answer(value=3, unit="rows")
    assert route.call_count == 1
    body = request_json(route.calls.last.request)
    assert [t["function"]["name"] for t in body["tools"]] == ["submit"]
    assert body["tools"][0]["function"]["parameters"]["required"] == ["value", "unit"]


async def test_tool_call_with_string_arguments_is_parsed() -> None:
    mock = respx.Router(assert_all_called=False)
    mock.post(OLLAMA_CHAT).mock(
        return_value=ollama_reply(
            tool_calls=[{"function": {"name": "submit", "arguments": '{"value": 1, "unit": "x"}'}}]
        )
    )
    result = await submit(make_router(mock, CircuitBreaker(), omniroute=False), MSGS, Answer)
    assert result == Answer(value=1, unit="x")


async def test_invalid_args_retry_with_error_then_success() -> None:
    mock = respx.Router(assert_all_called=False)
    route = mock.post(OLLAMA_CHAT).mock(
        side_effect=[
            ollama_submit({"value": -5, "unit": "rows"}),
            ollama_submit({"value": 5, "unit": "rows"}),
        ]
    )
    result = await submit(make_router(mock, CircuitBreaker(), omniroute=False), MSGS, Answer)
    assert result == Answer(value=5, unit="rows")
    assert route.call_count == 2
    retry_messages = request_json(route.calls[1].request)["messages"]
    assert len(retry_messages) == 2
    feedback = retry_messages[-1]["content"]
    assert retry_messages[-1]["role"] == "user"
    assert "greater than or equal to 0" in feedback
    assert '"value": -5' in feedback


async def test_no_tool_call_three_times_raises_submit_failed() -> None:
    mock = respx.Router(assert_all_called=False)
    route = mock.post(OLLAMA_CHAT).mock(return_value=ollama_reply("I think it is 5."))
    with pytest.raises(SubmitFailed) as info:
        await submit(make_router(mock, CircuitBreaker(), omniroute=False), MSGS, Answer)
    assert info.value.attempts == 3
    assert info.value.last_error == "no submit tool call"
    assert route.call_count == 3


async def test_think_false_in_ollama_request_body() -> None:
    mock = respx.Router(assert_all_called=False)
    route = mock.post(OLLAMA_CHAT).mock(return_value=ollama_submit({"value": 0, "unit": "u"}))
    await submit(make_router(mock, CircuitBreaker(), omniroute=False), MSGS, Answer)
    body = request_json(route.calls.last.request)
    assert body["think"] is False
    assert body["stream"] is False
    assert "format" not in body


async def test_max_attempts_must_be_positive() -> None:
    mock = respx.Router(assert_all_called=False)
    mock.post(OLLAMA_CHAT).mock(return_value=httpx.Response(500))
    with pytest.raises(ValueError):
        await submit(make_router(mock, CircuitBreaker()), MSGS, Answer, max_attempts=0)
