"""Agent error-kind mapping for openai SDK errors raised through CrewAI (ADR-0049)."""

import httpx2 as httpx  # openai 3.x types its requests/responses with httpx2
import openai
import pytest

from bourse.agents.common.executor import failure_kind
from bourse.trust.exam import is_exam_infra_failure

REQ = httpx.Request("POST", "http://meter:55200/v1/chat/completions")


def _status_error(cls: type[openai.APIStatusError], status: int, body: str) -> Exception:
    resp = httpx.Response(status, request=REQ, text=body)
    return cls(body, response=resp, body={"error": body})


def test_crewai_rate_limit_is_infra() -> None:
    exc = _status_error(openai.RateLimitError, 429, "contention: queue_wait 52000ms")
    kind = failure_kind(exc)
    assert kind == "llm_unavailable"
    assert is_exam_infra_failure(error_kind=kind, error="RateLimitError")


def test_wrapped_rate_limit_is_infra() -> None:
    inner = _status_error(openai.RateLimitError, 429, "contention")
    try:
        try:
            raise inner
        except openai.RateLimitError as e:
            raise RuntimeError("crew kickoff failed") from e
    except RuntimeError as outer:
        assert failure_kind(outer) == "llm_unavailable"


@pytest.mark.parametrize(
    ("cls", "status", "body"),
    [
        (openai.RateLimitError, 429, "insufficient_quota"),
        (openai.AuthenticationError, 401, "bad key"),
        (openai.PermissionDeniedError, 403, "forbidden"),
    ],
)
def test_billing_and_auth_are_not_infra(
    cls: type[openai.APIStatusError], status: int, body: str
) -> None:
    kind = failure_kind(_status_error(cls, status, body))
    assert kind == "provider_auth"
    assert not is_exam_infra_failure(error_kind=kind)


def test_server_error_and_connection_are_infra() -> None:
    assert failure_kind(_status_error(openai.InternalServerError, 503, "down")) == (
        "llm_unavailable"
    )
    assert failure_kind(openai.APIConnectionError(request=REQ)) == "llm_unavailable"
    assert failure_kind(openai.APITimeoutError(request=REQ)) == "llm_unavailable"


def test_other_errors_stay_internal() -> None:
    assert failure_kind(KeyError("x")) == "internal"
    assert failure_kind(_status_error(openai.BadRequestError, 400, "bad")) == "internal"
