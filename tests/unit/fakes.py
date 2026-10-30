"""Shared fakes for unit tests. No network: every HTTP call goes through a respx.Router."""

import json
from decimal import Decimal
from typing import Any

import httpx
import respx

from bourse.contracts.metering import BudgetSpec
from bourse.llm.base import Provider
from bourse.llm.ollama import OllamaProvider
from bourse.llm.omniroute import OmniRouteProvider
from bourse.llm.paid import PaidProvider
from bourse.llm.router import Router
from bourse.runtime.breaker import CircuitBreaker
from bourse.runtime.budget import BudgetLedger

OLLAMA = "http://ollama.test"
OMNI = "http://omniroute.test"
PAID = "http://paid.test"
OLLAMA_CHAT = f"{OLLAMA}/api/chat"
OMNI_CHAT = f"{OMNI}/v1/chat/completions"
PAID_CHAT = f"{PAID}/v1/chat/completions"


def make_spec(**overrides: Any) -> BudgetSpec:
    params: dict[str, Any] = {
        "max_tokens_per_call": 256,
        "max_tokens_run": 10_000,
        "wall_budget_s": 60.0,
    }
    params.update(overrides)
    return BudgetSpec.model_validate(params)


def ollama_reply(
    content: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    prompt_tokens: int = 10,
    out_tokens: int = 5,
) -> httpx.Response:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return httpx.Response(
        200,
        json={
            "model": "fake",
            "message": message,
            "done": True,
            "prompt_eval_count": prompt_tokens,
            "eval_count": out_tokens,
        },
    )


def ollama_submit(arguments: dict[str, Any]) -> httpx.Response:
    return ollama_reply(tool_calls=[{"function": {"name": "submit", "arguments": arguments}}])


def openai_reply(content: str = "ok") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "fake-omni",
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3},
        },
    )


def request_json(request: httpx.Request) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(request.content)
    return body


def make_client(mock: respx.Router) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(mock.async_handler))


def make_router(
    mock: respx.Router,
    breaker: CircuitBreaker,
    spec: BudgetSpec | None = None,
    *,
    omniroute: bool = True,
    paid: bool = False,
) -> Router:
    spec = spec or make_spec()
    client = make_client(mock)
    providers: list[Provider] = [OllamaProvider(OLLAMA, "fake-ollama", client)]
    if omniroute:
        providers.append(OmniRouteProvider(OMNI, "fake-omni", "dummy", client))
    if paid:
        providers.append(
            PaidProvider(PAID, "fake-paid", "dummy", spec, Decimal(1), Decimal(1), client)
        )
    return Router(providers, BudgetLedger(spec), breaker)
