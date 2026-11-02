"""Ollama native /api/chat provider: stream=false, think=false, options.num_predict=max_tokens."""

import time
from collections.abc import Sequence
from typing import Any

import httpx

from bourse.config import get_settings
from bourse.contracts.metering import Usage
from bourse.llm.base import (
    ChatMessage,
    ChatResult,
    FinishReason,
    Sampling,
    ToolCall,
    ToolSpec,
    ollama_message,
    parse_arguments,
    tool_payload,
)
from bourse.runtime.breaker import ProviderHTTPError
from bourse.runtime.deadline import deadline

# Read timeouts are disabled on purpose; the wall-clock deadline() is the only call limit.
DEFAULT_HTTP_TIMEOUT = httpx.Timeout(None, connect=5.0)


class OllamaProvider:
    def __init__(
        self,
        base_url: str,
        model: str,
        client: httpx.AsyncClient | None = None,
        sampling: Sampling | None = None,
        num_ctx: int | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._client = client or httpx.AsyncClient(timeout=DEFAULT_HTTP_TIMEOUT)
        self._sampling = sampling
        # Single source (ADR-0038): BOURSE_NUM_CTX_BY_MODEL, sent on every request.
        self._num_ctx = num_ctx if num_ctx is not None else get_settings().num_ctx_for(model)

    @property
    def num_ctx(self) -> int:
        return self._num_ctx

    def with_sampling(self, sampling: Sampling) -> "OllamaProvider":
        return OllamaProvider(self._base_url, self._model, self._client, sampling, self._num_ctx)

    @property
    def name(self) -> str:
        return "ollama"

    @property
    def paid(self) -> bool:
        return False

    @property
    def model(self) -> str:
        return self._model

    def build_body(
        self, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec], max_tokens: int
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self._model,
            "messages": [ollama_message(m) for m in messages],
            "stream": False,
            "think": False,
            "options": {"num_predict": max_tokens, "num_ctx": self._num_ctx},
        }
        if self._sampling is not None:
            body["options"].update(
                temperature=self._sampling.temperature,
                seed=self._sampling.seed,
            )
            body["keep_alive"] = self._sampling.keep_alive
        if tools:
            body["tools"] = tool_payload(tools)
        return body

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        max_tokens: int,
        deadline_s: float,
    ) -> ChatResult:
        body = self.build_body(messages, tools, max_tokens)
        started = time.monotonic()
        async with deadline(deadline_s):
            resp = await self._client.post(f"{self._base_url}/api/chat", json=body)
        wall_ms = (time.monotonic() - started) * 1000
        if resp.status_code >= 400:
            raise ProviderHTTPError(self.name, resp.status_code, resp.text)
        data: dict[str, Any] = resp.json()
        message: dict[str, Any] = data.get("message") or {}
        calls = tuple(
            ToolCall(
                name=str(c.get("function", {}).get("name", "")),
                arguments=parse_arguments(c.get("function", {}).get("arguments", {})),
                id=c.get("id"),
            )
            for c in (message.get("tool_calls") or [])
        )
        usage = Usage(
            tokens_in=int(data.get("prompt_eval_count") or 0),
            tokens_out=int(data.get("eval_count") or 0),
            wall_ms=wall_ms,
            model=self._model,
            provider=self.name,
        )
        finish: FinishReason = (
            "tool_calls" if calls else "length" if data.get("done_reason") == "length" else "stop"
        )
        return ChatResult(
            text=str(message.get("content") or ""),
            tool_calls=calls,
            usage=usage,
            finish_reason=finish,
        )

    async def aclose(self) -> None:
        await self._client.aclose()
