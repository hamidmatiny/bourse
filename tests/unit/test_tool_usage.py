"""MCP tool usage is recorded server-side by agent key; unknown keys are refused."""

import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette

from bourse.mcp_adapter.adapter import ToolCallFailed, ToolClient, mcp_tools_as_langchain
from bourse.mcp_servers.common import MAX_REQUEST_BODY_BYTES
from bourse.mcp_servers.registry import build_server
from bourse.mcp_servers.usage import InMemoryToolUsageRepository, ToolUsageMiddleware

URL = "http://127.0.0.1:55102/mcp"  # loopback host passes the server's DNS-rebinding check
KEY = secrets.token_hex(16)


@asynccontextmanager
async def served() -> AsyncIterator[tuple[ToolUsageMiddleware, InMemoryToolUsageRepository]]:
    """Used inside each test: the MCP lifespan must exit in the task that entered it."""
    inner: Starlette = build_server("pii").streamable_http_app(
        stateless_http=True, json_response=True, max_request_body_size=MAX_REQUEST_BODY_BYTES
    )
    repo = InMemoryToolUsageRepository()
    app = ToolUsageMiddleware(inner, server="pii", keys={KEY: "agent-x"}, repo=repo)
    async with inner.router.lifespan_context(inner):
        yield app, repo


def factory(
    app: ToolUsageMiddleware, headers: dict[str, str]
) -> Callable[[], AbstractAsyncContextManager[ToolClient]]:
    @asynccontextmanager
    async def connect() -> AsyncIterator[ToolClient]:
        async with (
            httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers=headers) as http,
            Client(streamable_http_client(URL, http_client=http)) as client,
        ):
            yield client

    return connect


async def test_tool_calls_recorded_by_server() -> None:
    async with served() as (app, repo):
        await _recorded(app, repo)


async def _recorded(app: ToolUsageMiddleware, repo: InMemoryToolUsageRepository) -> None:
    headers = {"X-Bourse-Agent-Key": KEY, "X-Bourse-Task-Id": "run-1:pii:1"}
    tools = {
        t.name: t
        for t in await mcp_tools_as_langchain(
            URL, {"luhn_check", "regex_scan"}, client_factory=factory(app, headers)
        )
    }
    out = await tools["luhn_check"].ainvoke({"numbers": ["4111111111111111", "123"]})
    assert out == {"results": [True, False]}
    await tools["regex_scan"].ainvoke({"values": ["a@b.co"]})
    assert [(r.agent_id, r.task_id, r.tool, r.ok) for r in repo.rows] == [
        ("agent-x", "run-1:pii:1", "pii.luhn_check", True),
        ("agent-x", "run-1:pii:1", "pii.regex_scan", True),
    ]
    assert len(await repo.list_tool_usage(task_id="run-1:pii:1")) == 2
    assert len(await repo.list_tool_usage(run_id="run-1")) == 2


async def test_failed_tool_call_recorded_not_ok() -> None:
    async with served() as (app, repo):
        await _failed(app, repo)


async def _failed(app: ToolUsageMiddleware, repo: InMemoryToolUsageRepository) -> None:
    headers = {"X-Bourse-Agent-Key": KEY, "X-Bourse-Task-Id": "run-2:pii:1"}
    (tool,) = await mcp_tools_as_langchain(
        URL, {"regex_scan"}, client_factory=factory(app, headers)
    )
    with pytest.raises(ToolCallFailed):
        await tool.ainvoke({"values": ["x" * (300 * 1024)]})  # over the 256 KiB input cap
    assert [(r.tool, r.ok) for r in repo.rows] == [("pii.regex_scan", False)]


@pytest.mark.parametrize("key", [None, "wrong-key-" + "0" * 16])
async def test_unknown_key_refused(key: str | None) -> None:
    async with served() as (app, repo):
        await _refused(app, repo, key)


async def _refused(
    app: ToolUsageMiddleware, repo: InMemoryToolUsageRepository, key: str | None
) -> None:
    headers: dict[str, Any] = {"X-Bourse-Task-Id": "run-3:pii:1"}
    if key is not None:
        headers["X-Bourse-Agent-Key"] = key
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)) as http:
        resp = await http.post(
            URL,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "luhn_check", "arguments": {"numbers": ["1"]}},
            },
        )
    assert resp.status_code == 401
    assert repo.rows == []
