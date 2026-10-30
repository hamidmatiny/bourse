"""Fetch /.well-known/agent-card.json: 5 s wall-clock deadline, 64 KiB cap while streaming,
JSON content types only, no redirects."""

import json
from typing import Any
from urllib.parse import urlsplit

import httpx

from bourse.runtime.deadline import DeadlineExceeded, deadline

WELL_KNOWN_PATH = "/.well-known/agent-card.json"
MAX_CARD_BYTES = 64 * 1024
FETCH_DEADLINE_S = 5.0
ALLOWED_CONTENT_TYPES = frozenset({"application/json", "application/a2a+json"})


class FetchFailed(Exception):
    pass


class CardTooLarge(FetchFailed):
    pass


def check_card_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FetchFailed(f"unsupported card URL scheme/host: {url!r}")
    if parts.path != WELL_KNOWN_PATH or parts.query or parts.fragment:
        raise FetchFailed(f"card URL must be <origin>{WELL_KNOWN_PATH}")


async def fetch_card(
    client: httpx.AsyncClient,
    url: str,
    *,
    deadline_s: float = FETCH_DEADLINE_S,
    max_bytes: int = MAX_CARD_BYTES,
) -> dict[str, Any]:
    check_card_url(url)
    body = bytearray()
    try:
        async with (
            deadline(deadline_s),
            client.stream(
                "GET",
                url,
                headers={"Accept": "application/a2a+json, application/json"},
                follow_redirects=False,
            ) as resp,
        ):
            if resp.status_code != 200:
                raise FetchFailed(f"HTTP {resp.status_code}")
            ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            if ctype not in ALLOWED_CONTENT_TYPES:
                raise FetchFailed(f"content type {ctype!r} not allowed")
            declared = resp.headers.get("content-length")
            if declared is not None and declared.isdigit() and int(declared) > max_bytes:
                raise CardTooLarge(f"content-length {declared} > {max_bytes}")
            async for chunk in resp.aiter_bytes():
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise CardTooLarge(f"body exceeds {max_bytes} bytes")
    except DeadlineExceeded as exc:
        raise FetchFailed(f"deadline {deadline_s}s exceeded") from exc
    except httpx.HTTPError as exc:
        raise FetchFailed(f"transport error: {exc!r}") from exc
    try:
        data = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FetchFailed("body is not JSON") from exc
    if not isinstance(data, dict):
        raise FetchFailed("card JSON is not an object")
    return data
