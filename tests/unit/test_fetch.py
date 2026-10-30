import asyncio
import time
from collections.abc import AsyncIterator

import httpx
import pytest

from bourse.registry.fetch import MAX_CARD_BYTES, CardTooLarge, FetchFailed, fetch_card

URL = "https://vendor-a.local/.well-known/agent-card.json"


class _Stream(httpx.AsyncByteStream):
    def __init__(self, chunks: int, size: int, delay: float) -> None:
        self.chunks, self.size, self.delay = chunks, size, delay

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(self.chunks):
            if self.delay:
                await asyncio.sleep(self.delay)
            yield b" " * self.size


def _client(response: httpx.Response) -> httpx.AsyncClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        return response

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_fetch_ok_both_content_types() -> None:
    for ctype in ("application/json", "application/a2a+json; charset=utf-8"):
        resp = httpx.Response(200, content=b'{"name": "x"}', headers={"content-type": ctype})
        assert await fetch_card(_client(resp), URL) == {"name": "x"}


async def test_65_kib_body_rejected_without_content_length() -> None:
    # 65 chunks of 1 KiB streamed, no content-length header: the cap fires while streaming.
    resp = httpx.Response(
        200, stream=_Stream(65, 1024, 0.0), headers={"content-type": "application/json"}
    )
    with pytest.raises(CardTooLarge):
        await fetch_card(_client(resp), URL)


async def test_declared_oversize_rejected() -> None:
    body = b"{" + b" " * MAX_CARD_BYTES + b"}"
    resp = httpx.Response(200, content=body, headers={"content-type": "application/json"})
    with pytest.raises(CardTooLarge):
        await fetch_card(_client(resp), URL)


async def test_exactly_64_kib_accepted() -> None:
    body = b"{" + b" " * (MAX_CARD_BYTES - 2) + b"}"
    resp = httpx.Response(200, content=body, headers={"content-type": "application/json"})
    assert await fetch_card(_client(resp), URL) == {}


async def test_slow_drip_cancelled_by_deadline() -> None:
    resp = httpx.Response(
        200, stream=_Stream(10_000, 1, 0.1), headers={"content-type": "application/json"}
    )
    started = time.monotonic()
    with pytest.raises(FetchFailed, match="deadline"):
        await fetch_card(_client(resp), URL, deadline_s=0.5)
    assert 0.5 <= time.monotonic() - started <= 0.8


@pytest.mark.parametrize("ctype", ["text/html", "application/javascript", ""])
async def test_wrong_content_type(ctype: str) -> None:
    resp = httpx.Response(200, content=b"{}", headers={"content-type": ctype})
    with pytest.raises(FetchFailed, match="content type"):
        await fetch_card(_client(resp), URL)


async def test_redirect_not_followed() -> None:
    resp = httpx.Response(302, headers={"location": "https://attacker.local/card.json"})
    with pytest.raises(FetchFailed, match="HTTP 302"):
        await fetch_card(_client(resp), URL)


@pytest.mark.parametrize(
    "url",
    [
        "https://vendor-a.local/card.json",
        "file:///etc/passwd",
        "https://vendor-a.local/.well-known/agent-card.json?x=1",
    ],
)
async def test_bad_url_rejected_before_request(url: str) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(FetchFailed):
        await fetch_card(client, url)
    assert calls == 0


async def test_non_object_json_rejected() -> None:
    resp = httpx.Response(200, content=b"[1]", headers={"content-type": "application/json"})
    with pytest.raises(FetchFailed, match="object"):
        await fetch_card(_client(resp), URL)
