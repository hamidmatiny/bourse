import asyncio
import time
from collections.abc import AsyncIterator

import httpx
import pytest

from bourse.llm.base import ChatMessage
from bourse.llm.ollama import OllamaProvider
from bourse.runtime.deadline import DeadlineExceeded, deadline


class DripStream(httpx.AsyncByteStream):
    """Sends one byte every 0.5 s forever, so a per-read timeout never fires."""

    def __init__(self) -> None:
        self.chunks = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            await asyncio.sleep(0.5)
            self.chunks += 1
            yield b" "


async def test_deadline_cancels_dripping_provider() -> None:
    stream = DripStream()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    # 2 s read timeout would never fire against a 0.5 s drip; only the deadline can stop this.
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=2.0)
    provider = OllamaProvider("http://ollama.test", "fake", client)
    started = time.monotonic()
    with pytest.raises(DeadlineExceeded):
        async with deadline(1.0):
            await provider.chat([ChatMessage(role="user", content="hi")], [], 16, 60.0)
    elapsed = time.monotonic() - started
    assert 1.0 <= elapsed <= 1.3, f"elapsed={elapsed:.3f}s"
    assert stream.chunks >= 1  # bytes were flowing when the deadline fired


async def test_provider_own_deadline_fires() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=DripStream())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OllamaProvider("http://ollama.test", "fake", client)
    started = time.monotonic()
    with pytest.raises(DeadlineExceeded):
        await provider.chat([ChatMessage(role="user", content="hi")], [], 16, 0.6)
    assert 0.6 <= time.monotonic() - started <= 0.9


async def test_deadline_passes_fast_body() -> None:
    async with deadline(1.0):
        await asyncio.sleep(0.01)
