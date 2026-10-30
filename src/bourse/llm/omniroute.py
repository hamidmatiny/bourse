"""OpenAI-compatible /v1/chat/completions provider (OmniRoute; also reused by paid.py)."""

import time
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import httpx

from bourse.contracts.metering import Usage
from bourse.llm.base import (
    ChatMessage,
    ChatResult,
    ToolCall,
    ToolSpec,
    parse_arguments,
    tool_payload,
)
from bourse.llm.ollama import DEFAULT_HTTP_TIMEOUT
from bourse.runtime.breaker import ProviderHTTPError
from bourse.runtime.deadline import deadline


class OpenAICompatProvider:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        client: httpx.AsyncClient | None = None,
        provider_name: str = "omniroute",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=DEFAULT_HTTP_TIMEOUT)
        self._name = provider_name

    @property
    def name(self) -> str:
        return self._name

    @property
    def paid(self) -> bool:
        return False

    def price(self, tokens_in: int, tokens_out: int) -> Decimal:
        return Decimal(0)

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        max_tokens: int,
        deadline_s: float,
    ) -> ChatResult:
        body: dict[str, Any] = {
            "model": self._model,
            "messages": [m.model_dump() for m in messages],
            "max_tokens": max_tokens,
            "stream": False,
        }
        if tools:
            body["tools"] = tool_payload(tools)
        headers = {"Authorization": f"Bearer {self._api_key}"}
        started = time.monotonic()
        async with deadline(deadline_s):
            resp = await self._client.post(
                f"{self._base_url}/v1/chat/completions", json=body, headers=headers
            )
        wall_ms = (time.monotonic() - started) * 1000
        if resp.status_code >= 400:
            raise ProviderHTTPError(self.name, resp.status_code, resp.text)
        data: dict[str, Any] = resp.json()
        choices: list[dict[str, Any]] = data.get("choices") or [{}]
        message: dict[str, Any] = choices[0].get("message") or {}
        calls = tuple(
            ToolCall(
                name=str(c.get("function", {}).get("name", "")),
                arguments=parse_arguments(c.get("function", {}).get("arguments", "{}")),
            )
            for c in (message.get("tool_calls") or [])
        )
        raw_usage: dict[str, Any] = data.get("usage") or {}
        tokens_in = int(raw_usage.get("prompt_tokens") or 0)
        tokens_out = int(raw_usage.get("completion_tokens") or 0)
        usage = Usage(
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            wall_ms=wall_ms,
            model=str(data.get("model") or self._model),
            provider=self.name,
        )
        return ChatResult(
            text=str(message.get("content") or ""),
            tool_calls=calls,
            usage=usage,
            usd=self.price(tokens_in, tokens_out),
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class OmniRouteProvider(OpenAICompatProvider):
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(base_url, model, api_key, client, provider_name="omniroute")
